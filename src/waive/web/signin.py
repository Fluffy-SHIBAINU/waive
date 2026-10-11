"""Who is signed in (Phase 9): the session cookie, and what every route module needs to ask.

The cookie holds a random session id and nothing else; the account behind it is looked up in
the database on each request (auth/sessions.py). While sign-in is not configured, nobody is
signed in and no cookie is read or set.

Also here, because more than one route module shows them: the history of an account's cases,
the home page for whoever is asking, and what a case page offers about keeping the case."""

from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from waive.auth import accounts, sessions
from waive.auth.oidc import GoogleSignIn
from waive.cases.service import CaseContext, summarize
from waive.db import session_scope
from waive.web.deps import deps_of, render
from waive.web.headers import PRIVATE_HEADERS

SESSION_COOKIE = "waive_session"
SESSION_SECONDS = int(sessions.LIFETIME.total_seconds())
# How far a case got, in the words the history shows (never the status column's own).
STATUS_WORDS = {
    "new": "Waiting for a photo of the bill",
    "bill_read": "Bill read. Check what we read.",
    "confirmed": "Some questions are left",
    "evaluated": "Result ready. Next: make your form.",
    "approved": "Form ready to print and send",
}
ANSWER_WORDS = {
    "approved": "The hospital approved it",
    "partial": "The hospital gave a discount",
    "denied": "The hospital said no",
    "more_info": "The hospital asks for more documents",
}


def provider(request: Request) -> GoogleSignIn | None:
    """The Google client, or None while sign-in is not configured."""
    return request.app.state.google


def secure_cookies(request: Request) -> bool:
    """Behind the managed HTTPS front end the scheme arrives via X-Forwarded-Proto; a laptop
    runs on plain http and must keep working."""
    return request.url.scheme == "https" or request.app.state.settings.env == "production"


def from_this_site(request: Request) -> bool:
    """False when the browser says another site made this request. The session cookie is
    SameSite=Lax, which already keeps it off another site's form posts; this also stops a
    neighbour on the same registrable domain, which Lax lets through. (The Origin header cannot
    be used for this: pages sent with `Referrer-Policy: no-referrer` post with `Origin: null`.)
    A browser too old to send the header is covered by SameSite alone."""
    return request.headers.get("sec-fetch-site", "same-origin") in ("same-origin", "none")


def signed_in(request: Request, session: Session) -> int | None:
    """The account id of the signed-in browser, or None."""
    if provider(request) is None:
        return None
    return sessions.read(session, request.cookies.get(SESSION_COOKIE, ""))


def set_session_cookie(response: Response, request: Request, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=SESSION_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=secure_cookies(request),
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=secure_cookies(request)
    )


def private(response: Response) -> Response:
    """The headers of a page with personal data on it (web/headers.py), for the one such page
    whose address is not private by itself: the home page of someone signed in."""
    for name, value in PRIVATE_HEADERS:
        response.headers[name.decode("ascii")] = value.decode("ascii")
    return response


def start_link(case_token: str = "") -> str:
    """Where "Sign in with Google" goes; with a case's full link, sign-in keeps that case."""
    return "/auth/google/start" + (f"?{urlencode({'case': case_token})}" if case_token else "")


@dataclass(frozen=True)
class HistoryEntry:
    """One case in "Your cases", ready to show."""

    case_id: str
    started: str
    hospital: str
    status: str


def history(ctx: CaseContext, account_id: int) -> list[HistoryEntry]:
    """The account's cases, newest first: the day each started, its hospital once a bill was
    read, and how far it got in plain words. No link to a case is made here."""
    entries = []
    for row in accounts.cases_of(ctx.session, account_id):
        found = summarize(ctx, row)
        words = ANSWER_WORDS.get(found.decision or "") or STATUS_WORDS.get(found.status)
        entries.append(
            HistoryEntry(
                case_id=found.case_id,
                started=f"{found.started:%B} {found.started.day}, {found.started:%Y}",
                hospital=found.hospital_name or "Hospital not read yet",
                status=words or "In progress",
            )
        )
    return entries


def home_page(request: Request, notice: str = "") -> HTMLResponse:
    """The home page for whoever is asking: as it always was while sign-in is not configured;
    with "Sign in with Google" when it is; with the person's cases and "Sign out" when they are
    signed in. A page with someone's cases on it is never cached, indexed or sent as a referrer."""
    deps = deps_of(request)
    sign_in = provider(request) is not None
    cases: list[HistoryEntry] | None = None
    if sign_in:
        with session_scope(deps.engine) as session:
            account_id = signed_in(request, session)
            if account_id is not None:
                cases = history(deps.context(session), account_id)
    response = render(
        request,
        "home.html",
        notice=notice,
        sign_in=sign_in,
        signed_in=cases is not None,
        history=cases or [],
    )
    if sign_in:
        # One address, two pages: a cache between here and the browser must not hand the
        # signed-out one to someone signed in.
        response.headers["Vary"] = "Cookie"
    return private(response) if cases is not None else response


def keep_offer(
    request: Request, session: Session, case_id: str, token: str, back: str
) -> dict[str, str] | None:
    """What a page under a case's full link offers about keeping the case in an account: sign in
    (and keep it on the way back), save it with one tap, or the word that it is saved. None
    while sign-in is not configured. `back` names the page to return to after a save."""
    if provider(request) is None:
        return None
    account_id = signed_in(request, session)
    if account_id is None:
        state = "sign_in"
    elif accounts.is_linked(session, account_id, case_id):
        state = "saved"
    else:
        state = "save"
    return {"state": state, "back": back, "start": start_link(token)}
