"""Admin console: review queue, contributions, sheet versions, scoreboard, budget (spec §10, §16)."""

import hmac
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from waive.atlas import repo
from waive.db import CaseRow, session_scope
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

router = APIRouter(prefix="/admin")
COOKIE = "waive_admin"
MIN_TOKEN_CHARS = 16
LOGIN_FAILURES = "admin_login_failures"
LOGIN_WINDOW_SECONDS = 15 * 60


def _expected(request: Request) -> str:
    token = request.app.state.settings.admin_token
    if token is None or len(token.get_secret_value()) < MIN_TOKEN_CHARS:
        raise PermissionError("the admin console is switched off (WAIVE_ADMIN_TOKEN unset)")
    return token.get_secret_value()


def require_admin(request: Request) -> None:
    given = request.cookies.get(COOKIE, "")
    if not hmac.compare_digest(given.encode("utf-8"), _expected(request).encode("utf-8")):
        raise PermissionError("admin sign-in required")


@router.get("/login", response_class=HTMLResponse)
def admin_login_form(request: Request) -> HTMLResponse:
    return render(request, "admin_login.html")


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
        raise PermissionError("wrong admin token")
    limiter.reset(LOGIN_FAILURES)
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=12 * 3600)
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


@router.get("/review", response_class=HTMLResponse)
def admin_review(request: Request) -> HTMLResponse:
    require_admin(request)
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        items = repo.open_review_items(session)
        return render(request, "admin_review.html", items=items)


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


@router.get("/sheets/{ccn}", response_class=HTMLResponse)
def admin_sheet(request: Request, ccn: str, note: str = "") -> HTMLResponse:
    require_admin(request)
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        hospital = repo.get_hospital(session, ccn)
        if hospital is None:
            raise KeyError(ccn)
        return render(
            request,
            "admin_sheet.html",
            hospital=hospital,
            versions=repo.sheet_versions(session, ccn),
            flag=slip_flag_level(session, ccn),
            note=note,
        )


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
