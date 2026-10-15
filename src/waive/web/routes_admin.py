"""Admin console: review queue, contributions, sheet versions, scoreboard, budget (spec §10, §16)."""

import hmac
import secrets
import time
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.discover import domain_unconfirmed
from waive.atlas.pipeline import BuildResult, confirm_domain, reject_domain
from waive.atlas.schema import SheetStatus
from waive.db import CaseRow, HospitalRow, ReviewItemRow, SheetRow, session_scope
from waive.learning.contributions import (
    approve_contribution,
    list_contributions,
    rebuild_from_sources,
    reject_contribution,
)
from waive.learning.evidence import audit_evidence, confirm_flag, slip_flag_level, withdraw_slips
from waive.learning.scoreboard import queue_prechecks, scoreboard
from waive.web.deps import deps_of, render
from waive.web.limits import TooManyRequests
from waive.web.signin import from_this_site

router = APIRouter(prefix="/admin")
COOKIE = "waive_admin"
MIN_TOKEN_CHARS = 16
LOGIN_FAILURES = "admin_login_failures"
LOGIN_WINDOW_SECONDS = 15 * 60
SESSION_SECONDS = 12 * 3600
# Signed-in sessions live in process memory (one replica): a restart signs everyone out.
MAX_SESSIONS = 50


class AdminRequired(Exception):
    """No valid admin session, or a wrong token: the app renders the sign-in page with the
    message (status 403), never the senior's "ask your helper" page."""


def _expected(request: Request) -> str:
    token = request.app.state.settings.admin_token
    if token is None or len(token.get_secret_value()) < MIN_TOKEN_CHARS:
        raise AdminRequired("The admin console is switched off (WAIVE_ADMIN_TOKEN is not set).")
    return token.get_secret_value()


def _sessions(request: Request) -> dict[str, float]:
    return request.app.state.admin_sessions


def require_admin(request: Request) -> None:
    # The cookie is SameSite=Strict, but Waive runs at waive.kindred-ai.dev, the same site as
    # kindred-ai.dev and its other subdomains: a page there could still post to the console.
    if request.method not in ("GET", "HEAD") and not from_this_site(request):
        raise AdminRequired(
            "That change came from another site, so it was refused. Open the console and try again."
        )
    sessions = _sessions(request)
    given = request.cookies.get(COOKIE, "")
    expires = sessions.get(given)
    if expires is None or expires < time.time():
        sessions.pop(given, None)
        raise AdminRequired("Sign in to use the console.")


@router.get("/login", response_class=HTMLResponse)
def admin_login_form(request: Request) -> HTMLResponse:
    return render(request, "admin_login.html")


def _secure(request: Request) -> bool:
    """Behind the managed HTTPS front end the scheme arrives via X-Forwarded-Proto; the laptop
    demo runs on plain http and must keep working."""
    return request.url.scheme == "https" or request.app.state.settings.env == "production"


@router.post("/login")
def admin_login(request: Request, token: str = Form(...)):
    # Wrong tokens are counted process-wide (the client address behind the front end is not
    # trustworthy); past the limit nobody signs in until the window passes.
    limiter = request.app.state.limiter
    allowed = request.app.state.settings.admin_login_failures
    if limiter.exceeded(LOGIN_FAILURES, allowed, LOGIN_WINDOW_SECONDS):
        raise TooManyRequests("sign-in attempts")
    if not hmac.compare_digest(token.encode("utf-8"), _expected(request).encode("utf-8")):
        limiter.hit(LOGIN_FAILURES, allowed, LOGIN_WINDOW_SECONDS)
        raise AdminRequired("That token is not right.")
    limiter.reset(LOGIN_FAILURES)
    # The cookie is a random session id, never the secret: disclosure costs one sign-out.
    sessions = _sessions(request)
    now = time.time()
    for stale in [sid for sid, expires in sessions.items() if expires < now]:
        del sessions[stale]
    while len(sessions) >= MAX_SESSIONS:
        del sessions[next(iter(sessions))]
    session_id = secrets.token_urlsafe(32)
    sessions[session_id] = now + SESSION_SECONDS
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        COOKIE,
        session_id,
        httponly=True,
        samesite="strict",
        secure=_secure(request),
        max_age=SESSION_SECONDS,
    )
    return response


@router.post("/logout")
def admin_logout(request: Request):
    _sessions(request).pop(request.cookies.get(COOKIE, ""), None)
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(COOKIE, httponly=True, samesite="strict", secure=_secure(request))
    return response


@router.get("", response_class=HTMLResponse)
def admin_home(request: Request) -> HTMLResponse:
    require_admin(request)
    deps = deps_of(request)
    settings = request.app.state.settings
    governor = request.app.state.governor
    tavily_used, _ = governor.summary()["tavily"]
    _, tf_used = governor.summary()["token_factory"]
    with session_scope(deps.engine) as session:
        outcomes = session.scalar(
            select(func.count()).select_from(CaseRow).where(CaseRow.outcome.is_not(None))
        )
        counts = {
            "review": len(repo.open_review_items(session)),
            "contributions": len(list_contributions(session)),
            "outcomes": int(outcomes or 0),
        }
        audit = audit_evidence(session)
    budget = {
        "tavily_used": tavily_used,
        "tavily_cap": settings.tavily_credit_cap,
        "tf_used": tf_used,
        "tf_cap": settings.token_factory_usd_cap,
    }
    return render(request, "admin_home.html", counts=counts, budget=budget, audit=audit)


def _websites_to_confirm(session: Session, items: list[ReviewItemRow]) -> dict[str, Any]:
    """Per hospital with an open `domain_unconfirmed` item, its website and how sure discovery
    was, while nobody has confirmed it (the item is filed only when a sheet is held for it)."""
    websites: dict[str, Any] = {}
    for item in items:
        if item.kind != "domain_unconfirmed" or not item.ccn or item.ccn in websites:
            continue
        row = repo.get_hospital(session, item.ccn)
        if row is not None and domain_unconfirmed(row.website_domain, row.domain_confidence):
            websites[item.ccn] = {"domain": row.website_domain, "confidence": row.domain_confidence}
    return websites


@router.get("/review", response_class=HTMLResponse)
def admin_review(request: Request) -> HTMLResponse:
    require_admin(request)
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        items = repo.open_review_items(session)
        websites = _websites_to_confirm(session, items)
        return render(request, "admin_review.html", items=items, websites=websites)


@router.post("/review/{item_id}")
def admin_resolve(request: Request, item_id: int, status: str = Form(...), verdict: str = Form("")):
    require_admin(request)
    deps = deps_of(request)
    final = status if status in {"resolved", "dismissed"} else "resolved"
    with session_scope(deps.engine) as session:
        item = repo.set_review_status(session, item_id, final)
        if item.kind == "rescout_request" and verdict == "sheet_wrong" and item.ccn:
            withdraw_slips(session, item.ccn)
        elif item.kind == "rescout_request" and verdict == "hospital_slip" and item.ccn:
            confirm_flag(session, item.ccn)
    return RedirectResponse("/admin/review", status_code=303)


@router.get("/contributions", response_class=HTMLResponse)
def admin_contributions(request: Request) -> HTMLResponse:
    require_admin(request)
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        rows = list_contributions(session)
        return render(request, "admin_contributions.html", rows=rows)


@router.post("/contributions/{contribution_id}/approve")
def admin_approve(request: Request, contribution_id: int):
    require_admin(request)
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        approve_contribution(session, contribution_id, deps.today_fn())
    return RedirectResponse("/admin/contributions", status_code=303)


@router.post("/contributions/{contribution_id}/reject")
def admin_reject(request: Request, contribution_id: int):
    require_admin(request)
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        reject_contribution(session, contribution_id)
    return RedirectResponse("/admin/contributions", status_code=303)


@router.post("/rebuild/{ccn}")
def admin_rebuild(request: Request, ccn: str):
    require_admin(request)
    deps = deps_of(request)
    if deps.ai is None:
        note = "No model key is configured; nothing was rebuilt."
    else:
        with session_scope(deps.engine) as session:
            result = rebuild_from_sources(session, deps.ai, ccn, deps.today_fn())
            note = f"{result.outcome}; version {result.version}; " + "; ".join(result.notes)
    return RedirectResponse(f"/admin/sheets/{ccn}?note={quote(note)}", status_code=303)


def _website_to_confirm(
    session: Session, hospital: HospitalRow, versions: list[SheetRow]
) -> dict[str, Any] | None:
    """The website a held sheet waits on (decision 7.9a-1): its domain, how sure discovery was,
    what the place check said of its documents, and the site the sheet was built from when that
    is another one (confirming then publishes nothing: the sheet waits for a build that scouts
    the confirmed site). None where no held sheet waits on a website."""
    if not versions or versions[0].status != SheetStatus.HELD.value:
        return None
    domain = hospital.website_domain
    if not domain_unconfirmed(domain, hospital.domain_confidence):
        return None
    place = [
        item.detail.get("reason")
        for item in repo.open_review_items(session, hospital.ccn)
        if item.kind == "place_unconfirmed" and item.detail.get("domain") == domain
    ]
    built_from = (versions[0].body.get("hospital") or {}).get("website_domain")
    return {
        "domain": domain,
        "confidence": hospital.domain_confidence,
        "place": list(dict.fromkeys(place)),
        "elsewhere": built_from != domain,
        "built_from": built_from,
        "state": hospital.state,
    }


@router.get("/sheets/{ccn}", response_class=HTMLResponse)
def admin_sheet(request: Request, ccn: str, note: str = "") -> HTMLResponse:
    require_admin(request)
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        hospital = repo.get_hospital(session, ccn)
        if hospital is None:
            raise KeyError(ccn)
        versions = repo.sheet_versions(session, ccn)
        return render(
            request,
            "admin_sheet.html",
            hospital=hospital,
            versions=versions,
            flag=slip_flag_level(session, ccn),
            note=note,
            website=_website_to_confirm(session, hospital, versions),
        )


def _account(result: BuildResult) -> str:
    """One line for the sheet page: the outcome, the version, then the notes."""
    version = [] if result.version is None else [f"version {result.version}"]
    return "; ".join([result.outcome, *version, *result.notes])


@router.post("/sheets/{ccn}/confirm-website")
def admin_confirm_website(request: Request, ccn: str, domain: str = Form(...)):
    """The admin checked that the website is the hospital's own: confidence 1.0, then the held
    sheet is re-verified from stored documents (no paid call) and published if nothing else
    holds it."""
    require_admin(request)
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        if repo.get_hospital(session, ccn) is None:
            raise KeyError(ccn)
        note = _account(confirm_domain(session, ccn, domain))
    return RedirectResponse(f"/admin/sheets/{ccn}?note={quote(note)}", status_code=303)


@router.post("/sheets/{ccn}/wrong-website")
def admin_wrong_website(
    request: Request, ccn: str, domain: str = Form(...), reason: str = Form("")
):
    """The admin found that the website is not the hospital's: it is cleared, its documents
    detached and the sheet held without the fields they backed (no paid call)."""
    require_admin(request)
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        if repo.get_hospital(session, ccn) is None:
            raise KeyError(ccn)
        note = _account(reject_domain(session, ccn, domain, reason.strip()[:200]))
    return RedirectResponse(f"/admin/sheets/{ccn}?note={quote(note)}", status_code=303)


@router.get("/scoreboard", response_class=HTMLResponse)
def admin_scoreboard(request: Request, queued: int | None = None) -> HTMLResponse:
    require_admin(request)
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        scores = scoreboard(session)
    return render(request, "admin_scoreboard.html", scores=scores, queued=queued)


@router.post("/scoreboard/prechecks")
def admin_prechecks(request: Request):
    require_admin(request)
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        queued = queue_prechecks(session)
    return RedirectResponse(f"/admin/scoreboard?queued={len(queued)}", status_code=303)
