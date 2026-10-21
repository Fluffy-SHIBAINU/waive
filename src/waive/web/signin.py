"""Who is signed in (Phase 9, re-keyed to Kindred accounts 2026-10-21), and what every route
module needs to ask.

The reader is whoever the Kindred ID wall let in (web/kindred_auth.py puts the Issuer's session
on `request.state.kindred_user`). Its `sub` is Google's own subject id, signed through by the
Issuer, so the account row is the one Waive's own Google sign-in would have made for the same
person: provider "google", the subject hashed under the token secret (auth/accounts.py). That
own sign-in (routes_auth.py) is dormant: its session cookie helpers stay here for it, but no
route reads the cookie to decide who is asking.

Also here, because more than one route module shows them: the history of an account's cases,
the home page for whoever is asking, and what a case page offers about keeping the case."""

from dataclasses import dataclass

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
    """The account id of the Kindred reader on this request, made on first sight. None only
    when no Kindred session reached the request, which the wall never lets happen."""
    user = getattr(request.state, "kindred_user", None)
    if not user:
        return None
    digest = accounts.signer_subject_hash(deps_of(request).signer, accounts.GOOGLE, user["sub"])
    return accounts.get_or_create(session, accounts.GOOGLE, digest).id


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
    """The home page for whoever is asking: as it always was until the reader has a case, then
    with "Your cases" under the start. A page with someone's cases on it is never cached,
    indexed or sent as a referrer."""
    deps = deps_of(request)
    cases: list[HistoryEntry] = []
    with session_scope(deps.engine) as session:
        account_id = signed_in(request, session)
        if account_id is not None:
            cases = history(deps.context(session), account_id)
    response = render(request, "home.html", notice=notice, history=cases)
    # One address, a page per account: a cache between here and the browser must not hand one
    # reader's page to another, the empty one included.
    response.headers["Vary"] = "Cookie"
    return private(response) if cases else response


def keep_offer(
    request: Request, session: Session, case_id: str, back: str
) -> dict[str, str] | None:
    """What a page under a case's full link offers about keeping the case in the reader's
    account: save it with one tap, or the word that it is saved. None when no account is on the
    request (never behind the wall; a wall-exempt /c/ page one day), so the Save button is never
    offered to nobody: the feature document's §7 said "never None", its §13.6 settles it this
    way. `back` names the page to return to after a save."""
    account_id = signed_in(request, session)
    if account_id is None:
        return None
    saved = accounts.is_linked(session, account_id, case_id)
    return {"state": "saved" if saved else "save", "back": back}
