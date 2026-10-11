"""Who is signed in (Phase 9): the session cookie, and what every route module needs to ask.

The cookie holds a random session id and nothing else; the account behind it is looked up in
the database on each request (auth/sessions.py). While sign-in is not configured, nobody is
signed in and no cookie is read or set."""

from fastapi import Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from waive.auth import sessions
from waive.auth.oidc import GoogleSignIn

SESSION_COOKIE = "waive_session"
SESSION_SECONDS = int(sessions.LIFETIME.total_seconds())


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
