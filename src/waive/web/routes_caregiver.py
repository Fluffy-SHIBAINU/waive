"""Caregiver flow: create a case, review, correct, approve, delete (spec §9 steps 3 and 9)."""

from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from waive.atlas import repo
from waive.cases.images import ImageError
from waive.cases.packet import build_packet, packet_data
from waive.cases.reminders import build_ics, reminder_events
from waive.cases.service import (
    approve,
    authorize,
    confirm_bill,
    delete_case,
    set_household,
    start_case,
    view,
)
from waive.db import session_scope
from waive.learning.gaps import pending_gap_ask
from waive.learning.intake import ingest_paper
from waive.learning.outcomes import (
    Decision,
    OutcomeExtract,
    load_outcome,
    pending_check_in,
    record_check_in,
)
from waive.learning.triage import record_outcome, triage_for
from waive.web.deps import deps_of, render
from waive.web.routes_senior import long_date, money, senior_links

router = APIRouter()


def caregiver_links(token: str) -> dict[str, str]:
    base = f"/c/{token}"
    return {
        "review": base,
        "correct": f"{base}/correct",
        "approve": f"{base}/approve",
        "delete": f"{base}/delete",
        "packet": f"{base}/packet.pdf",
        "reminders": f"{base}/reminders.ics",
        "paper": f"{base}/paper",
        "outcome": f"{base}/outcome",
        "check_in": f"{base}/check-in",
    }


@router.post("/cases", response_class=HTMLResponse)
def create_case(request: Request, state: str = Form("MA")) -> HTMLResponse:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        links = start_case(deps.context(session), state)
    return render(
        request,
        "case_created.html",
        senior_url=senior_links(links.senior_token)["start"],
        caregiver_url=caregiver_links(links.caregiver_token)["review"],
    )


@router.get("/c/{token}", response_class=HTMLResponse)
def caregiver_review(request: Request, token: str, note: str = "") -> HTMLResponse:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "caregiver")
        shown = view(ctx, row.id)
        found = repo.latest_sheet(session, row.ccn) if row.ccn else None
        sheet = found[0] if found else None
        sources = {source.id: source for source in sheet.sources} if sheet else {}
        return render(
            request,
            "caregiver_review.html",
            links=caregiver_links(token),
            case=shown,
            sheet=sheet,
            sources=sources,
            money=money,
            long_date=long_date,
            note=note,
            ask=pending_gap_ask(ctx, row.id),
            outcome=load_outcome(ctx, row.id),
            check_in=pending_check_in(ctx, row.id),
            triage_note=triage_for(ctx, row.id),
        )


def _decimal(value: str) -> Decimal | None:
    text = value.replace("$", "").replace(",", "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


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
        amount = _decimal(amount_due)
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
        set_household(ctx, row.id, size, _decimal(annual_income), chosen, allow_approved=True)
    return RedirectResponse(caregiver_links(token)["review"], status_code=303)


@router.post("/c/{token}/approve")
def caregiver_approve(request: Request, token: str):
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "caregiver")
        approve(ctx, row.id)
    return RedirectResponse(caregiver_links(token)["review"], status_code=303)


@router.post("/c/{token}/delete", response_class=HTMLResponse)
def caregiver_delete(request: Request, token: str) -> HTMLResponse:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "caregiver")
        delete_case(ctx, row.id)
    return render(request, "home.html", notice="The case and all its personal data were deleted.")


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


@router.post("/c/{token}/paper")
async def caregiver_paper(
    request: Request,
    token: str,
    photo: UploadFile = File(...),  # noqa: B008
):
    deps = deps_of(request)
    data = await photo.read()
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "caregiver")
        if deps.ai is None:
            note = "Reading is switched off right now."
        else:
            try:
                note = ingest_paper(ctx, row.id, data).message
            except ImageError:
                note = "We could not read that photo. Try again in good light."
    return RedirectResponse(
        f"{caregiver_links(token)['review']}?note={quote(note)}", status_code=303
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
