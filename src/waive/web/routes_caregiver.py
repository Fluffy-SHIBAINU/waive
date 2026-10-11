"""What only the full link may do: check what was read, correct, approve, download, delete
(spec §9 steps 3 and 9), and where a case starts. The steps it shares with the limited link live
in routes_senior."""

from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from waive.atlas import repo
from waive.cases.packet import build_packet, packet_data
from waive.cases.reminders import build_ics, reminder_events
from waive.cases.service import (
    approve,
    authorize,
    confirm_bill,
    delete_case,
    relink,
    set_household,
    start_case,
    view,
)
from waive.db import session_scope
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
from waive.web.routes_senior import flow_links, opened, shown_links, to_decimal

router = APIRouter()


def caregiver_links(token: str) -> dict[str, str]:
    return flow_links(token, full=True)


@router.post("/cases")
def create_case(request: Request, state: str = Form("MA")) -> RedirectResponse:
    """One link from the start (Phase 9): the new case opens at the photo step under its full
    link. A 303, so the browser's history holds that page's GET and never this POST: Back or a
    reload cannot start a second case. Nothing else is shown; the photo-only link for a second
    person is minted when the case bar is asked for it."""
    deps = deps_of(request)
    # No sign-in here and a new case with its capability link per call: a process-wide ceiling
    # first (no header can dodge it), then a best-effort one per client.
    settings = request.app.state.settings
    limiter = request.app.state.limiter
    limiter.check("cases", settings.cases_per_minute, 60, "new cases")
    limiter.check(
        f"cases:{client_key(request)}", settings.cases_per_client_per_minute, 60, "new cases"
    )
    with session_scope(deps.engine) as session:
        links = start_case(deps.context(session), state)
    return RedirectResponse(caregiver_links(links.caregiver_token)["start"], status_code=303)


@router.get("/c/{token}", response_class=HTMLResponse)
def caregiver_review(request: Request, token: str, fix: str = "") -> HTMLResponse:
    """The "Check what we read" page: the step after the result, and what the full link itself
    opens. `fix` opens the correction form (the read-back's "No, fix it" lands here)."""
    with opened(request, token) as case:
        shown = case.view()
        found = repo.latest_sheet(case.ctx.session, shown.ccn) if shown.ccn else None
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
        found = repo.latest_sheet(case.ctx.session, shown.ccn) if shown.ccn else None
        if shown.status != "approved" or found is None or shown.result is None:
            return case.to("review")
        return case.page(
            "case_form.html", shown, packet=packet_data(shown, found[0], case.ctx.today)
        )


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
    response = render(
        request, "home.html", notice="The case and all its personal data were deleted."
    )
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
        found = repo.latest_sheet(session, row.ccn) if row.ccn else None
        if found is None or shown.result is None:
            raise KeyError("packet not ready")
        pdf = build_packet(packet_data(shown, found[0], ctx.today))
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="waive-application.pdf"'},
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
