"""Sign-in with Google: leaving for Google, coming back, and signing out (Phase 9).

Leaving: the state, the nonce, the PKCE verifier and, when the person came from a case, that
case's full link are sealed with the vault key into a ten-minute cookie only these two routes
see. Coming back: the state in the address must match the sealed one before Google is asked for
anything; the ID token must pass every check before a row is written. Whatever goes wrong, the
person gets one short page and nothing was changed.

Nothing here logs a state, a nonce, a code, a token, a link or a subject id."""

import hmac
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from waive.auth import accounts, sessions
from waive.auth.oidc import Attempt, GoogleSignIn, SignInError, new_attempt
from waive.cases.service import CaseContext, authorize
from waive.cases.vault import FieldCipher
from waive.db import CaseRow, session_scope
from waive.web.deps import deps_of, render
from waive.web.limits import client_key
from waive.web.routes_senior import flow_links
from waive.web.signin import (
    SESSION_COOKIE,
    clear_session_cookie,
    from_this_site,
    provider,
    secure_cookies,
    set_session_cookie,
)

log = logging.getLogger(__name__)
router = APIRouter()

STATE_COOKIE = "waive_signin"
STATE_SECONDS = 10 * 60
# The cookie goes to the start and the return route and to nothing else.
STATE_PATH = "/auth/google"
# Marks what the vault key sealed as a sign-in attempt, so no other sealed blob can pass for one.
SEALED_KIND = "signin"
MAX_COOKIE_CHARS = 4096
MAX_STATE_CHARS = 128
MAX_CODE_CHARS = 2048
MAX_CASE_TOKEN_CHARS = 512
# A case that already has an answer goes back to its result page, any other to its check page.
RESULT_STATUSES = frozenset({"evaluated", "approved"})
_TO_COOKIE = str.maketrans("+/", "-_")
_FROM_COOKIE = str.maketrans("-_", "+/")


def now() -> float:
    return time.time()


def _google(request: Request) -> GoogleSignIn:
    """The routes do not exist while sign-in is not configured (the 404 page)."""
    found = provider(request)
    if found is None:
        raise KeyError("sign-in is not configured")
    return found


def _limit(request: Request, step: str) -> None:
    """A process-wide ceiling no header can dodge, then a best-effort one per client."""
    settings = request.app.state.settings
    limiter = request.app.state.limiter
    limiter.check(f"signin:{step}", settings.sign_ins_per_minute, 60, "sign-in attempts")
    limiter.check(
        f"signin:{step}:{client_key(request)}",
        settings.sign_ins_per_client_per_minute,
        60,
        "sign-in attempts",
    )


def redirect_uri(request: Request) -> str:
    """Where Google sends the browser back: the return route at the address this request came
    in on, so a laptop gets http://localhost:8000/... and the deployed site its own https URL.
    Production is always https, also when the front end did not pass the scheme along."""
    url = request.url_for("google_callback")
    if secure_cookies(request):
        url = url.replace(scheme="https")
    return str(url)


def seal_attempt(cipher: FieldCipher, attempt: Attempt, case_token: str) -> str:
    sealed = cipher.encrypt(
        {
            "k": SEALED_KIND,
            "s": attempt.state,
            "n": attempt.nonce,
            "v": attempt.verifier,
            "c": case_token,
            "e": int(now()) + STATE_SECONDS,
        }
    )
    return sealed.translate(_TO_COOKIE).rstrip("=")


def open_attempt(cipher: FieldCipher, value: str) -> tuple[Attempt, str]:
    """The attempt and the case link a state cookie carries, or `SignInError` when the cookie is
    missing, was not sealed here as an attempt, or is older than ten minutes."""
    if not value or len(value) > MAX_COOKIE_CHARS:
        raise SignInError("no_attempt")
    try:
        sealed = cipher.decrypt(value.translate(_FROM_COOKIE) + "=" * (-len(value) % 4))
    except ValueError:
        raise SignInError("attempt_unreadable") from None
    if not isinstance(sealed, dict) or sealed.get("k") != SEALED_KIND:
        raise SignInError("attempt_unreadable")
    if sealed["e"] <= now():
        raise SignInError("attempt_expired")
    return Attempt(state=sealed["s"], nonce=sealed["n"], verifier=sealed["v"]), sealed["c"]


def _set_state(response: Response, request: Request, value: str) -> None:
    response.set_cookie(
        STATE_COOKIE,
        value,
        max_age=STATE_SECONDS,
        path=STATE_PATH,
        httponly=True,
        samesite="lax",
        secure=secure_cookies(request),
    )


def _clear_state(response: Response, request: Request) -> None:
    response.delete_cookie(
        STATE_COOKIE,
        path=STATE_PATH,
        httponly=True,
        samesite="lax",
        secure=secure_cookies(request),
    )


def _case(ctx: CaseContext, token: str) -> CaseRow | None:
    """The case a carried link opens, when it is a full link that works right now."""
    if not token or len(token) > MAX_CASE_TOKEN_CHARS:
        return None
    try:
        return authorize(ctx, token, "caregiver")
    except PermissionError:
        return None


@router.get("/auth/google/start")
def google_start(request: Request, case: str = "") -> RedirectResponse:
    """The "Sign in with Google" button. `case` is the full link of the case the person was on,
    to keep it in the account; anything else in its place is dropped and sign-in goes on
    without it."""
    google = _google(request)
    _limit(request, "start")
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        kept = case if _case(deps.context(session), case) is not None else ""
    attempt = new_attempt()
    response = RedirectResponse(
        google.authorization_url(attempt, redirect_uri(request)), status_code=302
    )
    _set_state(response, request, seal_attempt(deps.cipher, attempt, kept))
    return response


def _failed(request: Request, reason: str) -> HTMLResponse:
    log.info("sign-in failed: %s", reason)
    response = render(
        request,
        "error.html",
        status_code=400,
        title="We could not sign you in",
        message=(
            "Sign-in did not work. Nothing was changed. "
            "You can try again, or go on without signing in."
        ),
    )
    _clear_state(response, request)  # one cookie, one attempt
    return response


@router.get("/auth/google/callback", response_class=HTMLResponse)
def google_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    """Google sends the browser back here with the state and a code (or an error)."""
    google = _google(request)
    _limit(request, "callback")
    deps = deps_of(request)
    try:
        attempt, case_token = open_attempt(deps.cipher, request.cookies.get(STATE_COOKIE, ""))
        if not state or len(state) > MAX_STATE_CHARS:
            raise SignInError("state_mismatch")
        if not hmac.compare_digest(state.encode("utf-8"), attempt.state.encode("utf-8")):
            raise SignInError("state_mismatch")
        if error or not code or len(code) > MAX_CODE_CHARS:
            raise SignInError("refused_at_provider")
        subject = google.subject(code, attempt, redirect_uri(request))
    except SignInError as failure:
        return _failed(request, failure.reason)

    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        digest = accounts.signer_subject_hash(deps.signer, accounts.GOOGLE, subject)
        account = accounts.get_or_create(session, accounts.GOOGLE, digest)
        target = "/account"
        row = _case(ctx, case_token)
        if row is not None:
            accounts.link_case(session, account.id, row.id)
            links = flow_links(case_token, full=True)
            target = links["result"] if row.status in RESULT_STATUSES else links["review"]
        sessions.purge_expired(session)
        # A browser signs in again, perhaps as someone else: its old session ends here.
        sessions.end(session, request.cookies.get(SESSION_COOKIE, ""))
        session_id = sessions.create(session, account.id)
    response = RedirectResponse(target, status_code=303)
    set_session_cookie(response, request, session_id)
    _clear_state(response, request)
    return response


@router.post("/auth/signout")
def sign_out(request: Request) -> RedirectResponse:
    """End the session on the server and in the browser, then go home."""
    response = RedirectResponse("/", status_code=303)
    session_id = request.cookies.get(SESSION_COOKIE, "")
    if session_id and from_this_site(request):
        with session_scope(deps_of(request).engine) as session:
            sessions.end(session, session_id)
        clear_session_cookie(response, request)
        # On a shared computer the signed-in pages leave the browser's cache with the session.
        response.headers["Clear-Site-Data"] = '"cache"'
    return response
