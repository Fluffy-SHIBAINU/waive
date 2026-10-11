"""The steps both links share: camera → read-back → two questions → result, and the hospital's
letter afterwards (spec §9, §10, §11).

Phase 9: every step has one handler, registered under both links. The limited link (/s/{token})
only adds photos and sees the result; the full link (/c/{token}) runs the same steps and, in
routes_caregiver, the check, approve and delete pages. `Flow` says which link a request came in
on, and a token is always checked against that link's scope, so a limited token is refused
everywhere under /c/."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from waive.ai.client import AIOutputError, ZDRRequired
from waive.cases.images import ImageError
from waive.cases.service import (
    CaseContext,
    CaseView,
    authorize,
    confirm_bill,
    limited_token,
    set_household,
    set_income,
    submit_bill,
    submit_income_letter,
    view,
)
from waive.cases.vault import Scope
from waive.db import session_scope
from waive.learning.gaps import answer_gap_ask, pending_gap_ask
from waive.learning.intake import ingest_paper
from waive.learning.outcomes import OutcomeExtract, load_outcome, outcome_text
from waive.learning.triage import TriageResult, triage_for
from waive.web.deps import deps_of, render
from waive.web.signin import keep_offer

router = APIRouter()


def flow_links(token: str, *, full: bool) -> dict[str, str]:
    """Where each step lives for one link. Only the full link has the pages after the result."""
    base = f"/c/{token}" if full else f"/s/{token}"
    links = {
        # The photo step. The limited link's own address is its photo step (and its result page
        # once there is a result); the full link's own address is the check page.
        "start": f"{base}/bill" if full else base,
        "bill": f"{base}/bill",
        "confirm": f"{base}/confirm",
        "household": f"{base}/household",
        "income": f"{base}/income",
        "result": f"{base}/result",
        "paper": f"{base}/paper",
        "skip": f"{base}/paper/skip",
    }
    if full:
        links.update(
            review=base,
            correct=f"{base}/correct",
            approve=f"{base}/approve",
            form=f"{base}/form",
            delete=f"{base}/delete",
            packet=f"{base}/packet.pdf",
            reminders=f"{base}/reminders.ics",
            outcome=f"{base}/outcome",
            check_in=f"{base}/check-in",
            relink=f"{base}/relink",
            save=f"{base}/save",
        )
    return links


@dataclass(frozen=True)
class Flow:
    """The link a request came in on."""

    token: str
    full: bool

    @property
    def scope(self) -> Scope:
        return "caregiver" if self.full else "senior"

    @property
    def links(self) -> dict[str, str]:
        return flow_links(self.token, full=self.full)


def shown_links(request: Request, full_token: str, limited: str) -> dict[str, dict[str, str]]:
    """The two links as the case bar shows them: the path, and the whole address as text that
    can be selected and copied when the clipboard button cannot be used."""
    origin = str(request.base_url).rstrip("/")

    def shown(path: str) -> dict[str, str]:
        return {"path": path, "url": origin + path}

    return {
        "mine": shown(flow_links(full_token, full=True)["review"]),
        "share": shown(flow_links(limited, full=False)["start"]),
    }


def money(value: Decimal | None) -> str:
    return "" if value is None else f"${value:,.2f}"


def long_date(value) -> str:
    return "" if value is None else f"{value:%B} {value.day}, {value:%Y}"


# Digits with an optional decimal part. Decimal() alone also accepts "NaN", "Infinity" and
# exponents ("1e9999999"), none of which may reach the sealed case or the percent-of-poverty sums.
_PLAIN_NUMBER = re.compile(r"-?\d{1,12}(\.\d*)?")


def to_decimal(value: str) -> Decimal | None:
    """A typed dollar amount ("$1,900.50"), or None when it is empty or not a plain number."""
    text = value.replace("$", "").replace(",", "").strip()
    if not _PLAIN_NUMBER.fullmatch(text):
        return None
    return Decimal(text)


# Why a step stopped short: (what the limited link hears before "Your helper will take it from
# here", what the full link hears). Whoever holds the full link is the helper, so that page
# points at the check page, where every detail can be typed in.
WAIT: dict[str, tuple[str, str]] = {
    "off": ("reading is switched off right now", "Reading photos is switched off right now."),
    "setup": ("your helper needs to finish setting things up", "Reading photos is not set up yet."),
    "unread": ("your helper will read the bill", "We could not read that photo."),
    "fix": ("your helper will fix the details", "You can fix the details yourself."),
    "unfinished": ("your helper will finish the check", "We cannot finish the check yet."),
}


@dataclass
class Step:
    """One request on one case: the link it came in on and the case that link opens."""

    request: Request
    flow: Flow
    ctx: CaseContext
    case_id: str

    def view(self) -> CaseView:
        return view(self.ctx, self.case_id)

    def page(self, name: str, shown: CaseView | None = None, **extra) -> HTMLResponse:
        if self.flow.full:
            # The case bar on every page the full link opens: the link to keep, a photo-only
            # link to send to someone else, delete. The limited link gets none of it.
            extra["bar"] = shown_links(
                self.request, self.flow.token, limited_token(self.ctx, self.case_id)
            )
        return render(
            self.request,
            name,
            links=self.flow.links,
            full=self.flow.full,
            case=shown or self.view(),
            money=money,
            long_date=long_date,
            **extra,
        )

    def keep(self, back: str) -> dict[str, str] | None:
        """What the result and check pages offer about keeping this case in an account, when
        sign-in is configured. Only the full link gets the offer: the photo-only link cannot
        put a case into anyone's account."""
        if not self.flow.full:
            return None
        return keep_offer(self.request, self.ctx.session, self.case_id, self.flow.token, back)

    def wait(self, why: str, shown: CaseView | None = None) -> HTMLResponse:
        return self.page("senior_wait.html", shown, reason=WAIT[why][1 if self.flow.full else 0])

    def to(self, link: str, suffix: str = "") -> RedirectResponse:
        return RedirectResponse(self.flow.links[link] + suffix, status_code=303)


@contextmanager
def opened(request: Request, token: str) -> Iterator[Step]:
    """Check the token against the scope of the link it was used on and open the case's
    session; a token that is invalid, revoked or too weak for this link raises PermissionError
    (the 403 page) before any handler code runs."""
    deps = deps_of(request)
    flow = Flow(token, full=request.url.path.startswith("/c/"))
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, flow.scope)
        yield Step(request, flow, ctx, row.id)


def both(method: str, path: str, **options):
    """Register one handler under both links: /s/{token}{path} and /c/{token}{path}."""

    def register(handler):
        for prefix in ("/s", "/c"):
            router.add_api_route(f"{prefix}/{{token}}{path}", handler, methods=[method], **options)
        return handler

    return register


# Once the case is approved, the steps only show the result (and still take papers from the
# hospital): a forwarded link or a cached, auto-submitting form must not rewrite the packet. The
# full link changes an approved case through the correction form on its check page.
LOCKED_STATUSES = frozenset({"approved"})


def _locked(shown: CaseView) -> bool:
    return shown.status in LOCKED_STATUSES


@router.get("/s/{token}", response_class=HTMLResponse)
def senior_start(request: Request, token: str) -> HTMLResponse:
    """The limited link's own address: the photo step, or the result once there is one."""
    with opened(request, token) as case:
        shown = case.view()
        if shown.tier is not None and shown.status in {"evaluated", "approved"}:
            return case.page(
                "senior_result.html", shown, ask=pending_gap_ask(case.ctx, case.case_id)
            )
        return case.page("senior_start.html", shown)


@both("GET", "/bill", response_class=HTMLResponse)
def senior_bill_form(request: Request, token: str):
    """The photo step by its own address (where a new case starts under the full link)."""
    with opened(request, token) as case:
        shown = case.view()
        if _locked(shown):
            return case.to("result")
        return case.page("senior_start.html", shown)


@both("POST", "/bill", response_class=HTMLResponse)
async def senior_bill(
    request: Request,
    token: str,
    photo: UploadFile = File(...),  # noqa: B008
):
    data = await photo.read()
    with opened(request, token) as case:
        if _locked(case.view()):
            return case.to("result")
        if case.ctx.ai is None:
            return case.wait("off")
        try:
            shown = submit_bill(case.ctx, case.case_id, data)
        except ImageError:
            return case.page(
                "senior_start.html",
                problem="We could not read that photo. Please try again in good light.",
            )
        except ZDRRequired:
            return case.wait("setup")
        except AIOutputError:
            # A traceback here would reach uvicorn's log with the model's answer in it.
            return case.wait("unread")
        return case.page("senior_readback.html", shown)


@both("POST", "/confirm")
def senior_confirm(request: Request, token: str, answer: str = Form(...)):
    with opened(request, token) as case:
        if _locked(case.view()):
            return case.to("result")
        if answer == "yes":
            confirm_bill(case.ctx, case.case_id, {})
            return case.to("household")
        if case.flow.full:
            # Whoever holds the full link fixes it on the check page, with the form open.
            return case.to("review", "?fix=1#fix")
        return case.wait("fix")


@both("GET", "/household", response_class=HTMLResponse)
def senior_household_form(request: Request, token: str):
    with opened(request, token) as case:
        shown = case.view()
        if _locked(shown):
            return case.to("result")
        return case.page("senior_household.html", shown)


@both("POST", "/household")
def senior_household(
    request: Request, token: str, size: int = Form(...), programs: str = Form("none")
):
    with opened(request, token) as case:
        shown = case.view()
        if _locked(shown):
            return case.to("result")
        chosen = () if programs == "none" else tuple(programs.split(","))
        set_household(case.ctx, case.case_id, size, shown.annual_income, chosen)
        return case.to("income")


@both("GET", "/income", response_class=HTMLResponse)
def senior_income_form(request: Request, token: str):
    with opened(request, token) as case:
        shown = case.view()
        if _locked(shown):
            return case.to("result")
        return case.page("senior_income.html", shown)


@both("POST", "/income")
async def senior_income(
    request: Request,
    token: str,
    photo: UploadFile | None = File(None),  # noqa: B008
    skip: str | None = Form(None),
    monthly: str = Form(""),
):
    data = await photo.read() if photo is not None and photo.filename else b""
    with opened(request, token) as case:
        if _locked(case.view()):
            return case.to("result")
        if case.flow.full and monthly.strip():
            # One link, no helper to fill the income in later: the amount can be typed here.
            amount = to_decimal(monthly)
            if amount is None or amount < 0:
                return case.page(
                    "senior_income.html",
                    problem="Please type the amount as a number, like 1900.",
                )
            set_income(case.ctx, case.case_id, amount * 12)
        elif data and case.ctx.ai is not None:
            try:
                submit_income_letter(case.ctx, case.case_id, data)
            except (ImageError, ZDRRequired, AIOutputError):
                problem = "We could not read that letter. You can skip this step."
                if case.flow.full:
                    problem = (
                        "We could not read that letter. You can type the amount below, "
                        "or skip this step."
                    )
                return case.page("senior_income.html", problem=problem)
        return case.to("result")


@both("GET", "/result", response_class=HTMLResponse)
def senior_result(request: Request, token: str) -> HTMLResponse:
    with opened(request, token) as case:
        shown = case.view()
        if shown.tier is None:
            return case.wait("unfinished", shown)
        return case.page(
            "senior_result.html",
            shown,
            ask=pending_gap_ask(case.ctx, case.case_id),
            keep=case.keep("result"),
        )


def _letter(
    case: Step,
    message: str = "",
    outcome: OutcomeExtract | None = None,
    triage: TriageResult | None = None,
) -> HTMLResponse:
    """The letter step, "Did the hospital answer?": the photo button and, on the same page, what
    the hospital's answer says once one was read."""
    return case.page(
        "senior_paper.html",
        message=message,
        answer=outcome_text(outcome) if outcome else None,
        triage_note=triage,
    )


@both("GET", "/paper", response_class=HTMLResponse)
def senior_paper_form(request: Request, token: str) -> HTMLResponse:
    with opened(request, token) as case:
        return _letter(
            case,
            outcome=load_outcome(case.ctx, case.case_id),
            triage=triage_for(case.ctx, case.case_id),
        )


@both("POST", "/paper", response_class=HTMLResponse)
async def senior_paper(
    request: Request,
    token: str,
    photo: UploadFile = File(...),  # noqa: B008
) -> HTMLResponse:
    data = await photo.read()
    with opened(request, token) as case:
        if case.ctx.ai is None:
            return case.wait("off")
        try:
            result = ingest_paper(case.ctx, case.case_id, data)
        except ImageError:
            return _letter(case, "We could not read that photo. Please try again in good light.")
        except ZDRRequired:
            return case.wait("setup")
        # What was just read is shown at once; a paper on another route only has its message.
        return _letter(case, result.message, result.outcome, result.triage)


@both("POST", "/paper/skip")
def senior_paper_skip(request: Request, token: str):
    with opened(request, token) as case:
        answer_gap_ask(case.ctx, case.case_id, "skipped")
        return case.to("result")
