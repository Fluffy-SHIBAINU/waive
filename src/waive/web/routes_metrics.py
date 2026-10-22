"""Public atlas metrics (spec §16): counts, ages, accuracy and credits — never personal data.

Also `/stats` (design §5.5), the counts another Kindred product reads without a session: cases
by where they started and the atlas's sheets by status and state. Counts only, never a case."""

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.metrics import Metrics, atlas_metrics, metrics_json
from waive.db import CaseRow, HospitalRow, SheetRow, session_scope
from waive.web.deps import deps_of, render

router = APIRouter()
# Cases that name no source: the home page's own form, or a post from elsewhere.
OTHER = "other"
# The newest version's statuses the counts report; a draft is nobody's business yet.
COUNTED = ("published", "held")


def public_stats(session: Session) -> dict[str, Any]:
    by_source = {"ccn_link": 0, OTHER: 0}
    for source, count in session.execute(
        select(CaseRow.source, func.count()).group_by(CaseRow.source)
    ):
        key = source if source in by_source else OTHER
        by_source[key] += count
    newest = (
        select(SheetRow.ccn, func.max(SheetRow.version).label("version"))
        .group_by(SheetRow.ccn)
        .subquery()
    )
    by_status = (
        select(HospitalRow.state, SheetRow.status, func.count())
        .select_from(SheetRow)
        .join(newest, (SheetRow.ccn == newest.c.ccn) & (SheetRow.version == newest.c.version))
        .join(HospitalRow, HospitalRow.ccn == SheetRow.ccn)
        .where(HospitalRow.ccn.not_in(list(repo.DEMO_CCNS)))
        .group_by(HospitalRow.state, SheetRow.status)
    )
    totals = dict.fromkeys(COUNTED, 0)
    by_state: dict[str, dict[str, int]] = {}
    for state, status, count in session.execute(by_status):
        if status not in totals:
            continue
        totals[status] += count
        by_state.setdefault(state, dict.fromkeys(COUNTED, 0))[status] += count
    return {
        "cases": {"total": sum(by_source.values()), "by_source": by_source},
        "sheets": {**totals, "by_state": dict(sorted(by_state.items()))},
    }


def _metrics(request: Request) -> Metrics:
    deps = deps_of(request)
    settings = request.app.state.settings
    with session_scope(deps.engine) as session:
        return atlas_metrics(
            session, deps.today_fn(), request.app.state.governor, settings.scout_daily_credits
        )


@router.get("/metrics", response_class=HTMLResponse)
def metrics_page(request: Request) -> HTMLResponse:
    return render(request, "metrics.html", m=_metrics(request))


@router.get("/metrics.json")
def metrics_data(request: Request) -> JSONResponse:
    return JSONResponse(metrics_json(_metrics(request)))


@router.get("/stats")
def stats(request: Request) -> JSONResponse:
    with session_scope(deps_of(request).engine) as session:
        return JSONResponse(public_stats(session))
