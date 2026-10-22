"""What only the full link may do: check what was read, correct, approve, download, delete
(spec §9 steps 3 and 9), and where a case starts. The steps it shares with the limited link live
in routes_senior."""

from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from waive.atlas import repo
from waive.auth.accounts import link_case
from waive.cases.packet import build_packet, packet_data
from waive.cases.reminders import build_ics, reminder_events
from waive.cases.service import (
    CaseView,
    approve,
    authorize,
    confirm_bill,
    delete_case,
    relink,
    set_household,
    start_case,
    view,
)
from waive.db import HospitalRow, session_scope
from waive.learning.gaps import pending_gap_ask
from waive.learning.outcomes import (
    Decision,
    OutcomeExtract,
    load_outcome,
    pending_check_in,
    record_check_in,
)
from waive.learning.triage import record_outcome, triage_for
from waive.web.deps import deps_of, render
from waive.web.limits import client_key
from waive.web.routes_senior import flow_links, long_date, opened, shown_links, to_decimal
from waive.web.signin import from_this_site, home_page, signed_in

router = APIRouter()


def caregiver_links(token: str) -> dict[str, str]:
    return flow_links(token, full=True)


# The one start source /stats counts apart from the home page's form (design §5.5): the confirm
# page's hidden field says it, and only this value is ever stored.
CCN_LINK = "ccn_link"


def _start(
    request: Request, state: str, *, ccn: str | None = None, source: str | None = None
) -> RedirectResponse:
    """One link from the start (Phase 9): the new case opens at the photo step under its full
    link. A 303, so the browser's history holds that page's GET and never the POST: Back or a
    reload cannot start a second case. Nothing else is shown; the photo-only link for a second
    person is minted when the case bar is asked for it."""
    deps = deps_of(request)
    # A new case with its capability link per call: a process-wide ceiling first (no header can
    # dodge it), then a best-effort one per client.
    settings = request.app.state.settings
    limiter = request.app.state.limiter
    limiter.check("cases", settings.cases_per_minute, 60, "new cases")
    limiter.check(
        f"cases:{client_key(request)}", settings.cases_per_client_per_minute, 60, "new cases"
    )
    with session_scope(deps.engine) as session:
        # Started from this site's own form: the case goes into the reader's history without
        # asking. Another site's form post starts a case that is in nobody's history.
        account_id = signed_in(request, session) if from_this_site(request) else None
        links = start_case(deps.context(session), state, ccn=ccn, source=source)
        if account_id is not None:
            link_case(session, account_id, links.case_id)
    return RedirectResponse(caregiver_links(links.caregiver_token)["start"], status_code=303)


@router.post("/cases")
def create_case(request: Request, state: str = Form("MA")) -> RedirectResponse:
    """The home page's own form: a case in the chosen state, the hospital read from the bill."""
    return _start(request, state)


def _hospital(session, ccn: str) -> HospitalRow:
    """The registry's row for a CCN from a link or a form, or the 404 page."""
    hospital = repo.get_hospital(session, ccn.strip()) if ccn.strip() else None
    if hospital is None:
        raise KeyError(ccn)
    return hospital


@router.get("/start", response_class=HTMLResponse)
def start_confirm(request: Request, ccn: str = "") -> HTMLResponse:
    """The confirm page another Kindred product's hospital page links to (`/start?ccn=`, design
    §5.5): it names the hospital, says whether its policy is on file, and offers one button.
    Looking creates nothing: a crawler or a browser's prefetch following the link from every
    hospital page mints no case and spends none of the case-per-minute ceiling; only the POST
    below does. An unknown CCN is the 404 page."""
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        hospital = _hospital(session, ccn)
        found = repo.published_sheet(session, hospital.ccn)
        return render(
            request,
            "start_confirm.html",
            hospital=hospital,
            sheet=found[0] if found else None,
            checked=long_date(found[1].created_at.date()) if found else "",
        )


@router.post("/start")
def start_from_link(
    request: Request, ccn: str = Form(""), source: str = Form("")
) -> RedirectResponse:
    """The confirm page's one button: the case starts in the hospital's own state with the
    hospital chosen, counted on /stats under the source the form names. An unknown CCN starts
    nothing (the 404 page)."""
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        hospital = _hospital(session, ccn)
        state, chosen = hospital.state, hospital.ccn
    return _start(request, state, ccn=chosen, source=CCN_LINK if source == CCN_LINK else None)


@router.get("/c/{token}", response_class=HTMLResponse)
def caregiver_review(request: Request, token: str, fix: str = "") -> HTMLResponse:
    """The "Check what we read" page: the step after the result, and what the full link itself
    opens. `fix` opens the correction form (the read-back's "No, fix it" lands here)."""
    with opened(request, token) as case:
        shown = case.view()
        found = repo.published_sheet(case.ctx.session, shown.ccn) if shown.ccn else None
        sheet = found[0] if found else None
        sources = {source.id: source for source in sheet.sources} if sheet else {}
        return case.page(
            "caregiver_review.html",
            shown,
            sheet=sheet,
            sources=sources,
            fix=bool(fix),
            ask=pending_gap_ask(case.ctx, case.case_id),
            outcome=load_outcome(case.ctx, case.case_id),
            check_in=pending_check_in(case.ctx, case.case_id),
            triage_note=triage_for(case.ctx, case.case_id),
            keep=case.keep("review"),
        )


@router.post("/c/{token}/correct")
def caregiver_correct(
    request: Request,
    token: str,
    hospital_ccn: str = Form(""),
    amount_due: str = Form(""),
    statement_date: str = Form(""),
    first_statement_date: str = Form(""),
    is_first_statement: str = Form(""),
    size: int = Form(1),
    annual_income: str = Form(""),
    programs: str = Form(""),
):
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "caregiver")
        corrections: dict = {}
        amount = to_decimal(amount_due)
        if amount is not None:
            corrections["amount_due"] = str(amount)
        if statement_date:
            corrections["statement_date"] = date.fromisoformat(statement_date).isoformat()
        if first_statement_date:
            corrections["first_statement_date"] = date.fromisoformat(
                first_statement_date
            ).isoformat()
        if is_first_statement:
            corrections["is_first_statement"] = True
        # The caregiver may re-correct and re-approve; the senior link may not (see _unlocked).
        confirm_bill(ctx, row.id, corrections, ccn=hospital_ccn or None, allow_approved=True)
        chosen = tuple(p.strip() for p in programs.split(",") if p.strip())
        set_household(ctx, row.id, size, to_decimal(annual_income), chosen, allow_approved=True)
    return RedirectResponse(caregiver_links(token)["review"], status_code=303)


@router.post("/c/{token}/approve")
def caregiver_approve(request: Request, token: str):
    """The "Looks right, make my form" button. Approving again changes nothing, and a case with
    nothing checked yet has no form to make: both land on the page that fits."""
    with opened(request, token) as case:
        if case.view().result is None:
            return case.to("review")
        approve(case.ctx, case.case_id)
        return case.to("form")


@router.get("/c/{token}/form", response_class=HTMLResponse)
def case_form(request: Request, token: str):
    """The "Your form and reminders" page: the packet, the calendar file, where to send it."""
    with opened(request, token) as case:
        shown = case.view()
        if shown.status != "approved" or shown.result is None:
            return case.to("review")
        sheet = _packet_sheet(case.ctx.session, shown)
        if sheet is None and shown.next_step != "cost_sharing":
            return case.to("review")
        # An ambulance bill has no letter from Waive: the page points to SHIP.
        packet = None if shown.next_step == "ship" else packet_data(shown, sheet, case.ctx.today)
        return case.page("case_form.html", shown, packet=packet)


def _packet_sheet(session, shown: CaseView):
    found = repo.published_sheet(session, shown.ccn) if shown.ccn else None
    return found[0] if found else None


@router.post("/c/{token}/save")
def caregiver_save(request: Request, token: str, back: str = Form("review")):
    """The "Save this case to my account" button. The full link shows the case may be opened;
    the Kindred session says whose account. Only this site's own form puts a case into the
    reader's history (as /cases and /account/delete do): the Kindred cookie is SameSite=Lax, so
    a neighbour on kindred-ai.dev that holds the full link could otherwise post here with it.
    A post from elsewhere, or one with no account on it (never behind the wall; a wall-exempt
    /c/ page one day), changes nothing and goes back to the case page, never home."""
    with opened(request, token) as case:
        landing = "result" if back == "result" else "review"
        account_id = signed_in(request, case.ctx.session) if from_this_site(request) else None
        if account_id is not None:
            link_case(case.ctx.session, account_id, case.case_id)
        return case.to(landing)


@router.post("/c/{token}/relink", response_class=HTMLResponse)
def caregiver_relink(request: Request, token: str) -> HTMLResponse:
    """The "Make new links" button: the old links, this one and any photo-only link that was
    sent included, stop working at once, so the page with the new ones is rendered here rather
    than redirected to."""
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "caregiver")
        links = relink(ctx, row.id)
    return render(
        request,
        "case_links.html",
        fresh=shown_links(request, links.caregiver_token, links.senior_token),
    )


@router.post("/c/{token}/delete", response_class=HTMLResponse)
def caregiver_delete(request: Request, token: str) -> HTMLResponse:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "caregiver")
        delete_case(ctx, row.id)
    response = home_page(request, notice="The case and all its personal data were deleted.")
    # One-tap delete reaches the browser too: cached copies of the review pages go with the case.
    response.headers["Clear-Site-Data"] = '"cache"'
    return response


@router.get("/c/{token}/packet.pdf")
def caregiver_packet(request: Request, token: str) -> Response:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "caregiver")
        shown = view(ctx, row.id)
        sheet = _packet_sheet(session, shown)
        if shown.result is None or shown.next_step == "ship":
            raise KeyError("packet not ready")
        if sheet is None and shown.next_step != "cost_sharing":
            raise KeyError("packet not ready")
        pdf = build_packet(packet_data(shown, sheet, ctx.today))
    name = "waive-application.pdf" if shown.next_step == "fap" else "waive-letter.pdf"
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/c/{token}/reminders.ics")
def caregiver_reminders(request: Request, token: str) -> Response:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "caregiver")
        shown = view(ctx, row.id)
        ics = build_ics(
            reminder_events(ctx.today, shown.deadlines, shown.hospital_name or "the hospital")
        )
    return Response(
        ics,
        media_type="text/calendar",
        headers={"Content-Disposition": 'attachment; filename="waive-reminders.ics"'},
    )


@router.post("/c/{token}/outcome")
def caregiver_outcome(
    request: Request, token: str, decision: str = Form(...), discount_percent: str = Form("")
):
    deps = deps_of(request)
    try:
        chosen = Decision(decision)
    except ValueError as error:
        raise KeyError(decision) from error
    percent = int(discount_percent) if discount_percent.strip().isdigit() else None
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "caregiver")
        record_outcome(ctx, row.id, OutcomeExtract(decision=chosen, discount_percent=percent))
    return RedirectResponse(caregiver_links(token)["review"], status_code=303)


@router.post("/c/{token}/check-in")
def caregiver_check_in(request: Request, token: str, day: int = Form(...)):
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "caregiver")
        record_check_in(ctx, row.id, day, "no_answer")
    return RedirectResponse(caregiver_links(token)["review"], status_code=303)
