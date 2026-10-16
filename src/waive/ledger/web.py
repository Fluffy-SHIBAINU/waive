"""The ledger's public pages (Phase 10): the ranked list at /ledger, and the template helpers
that put a hospital's ledger on the atlas page and its short lines on the result screen.

The atlas routes pass the LedgerView in themselves; the result screen cannot, because the
senior routes render inside the case's own session, so its helper opens a session of its own.
Nothing here may break a page: the ledger is a side block, and a senior reading her result
must get it whether or not the federal data behaves."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from waive.atlas import repo as atlas_repo
from waive.atlas.schema import SheetStatus
from waive.db import HospitalRow, session_scope
from waive.ledger import repo
from waive.ledger.models import (
    CollectionsMetric,
    ExposureMetric,
    GapLabel,
    GapMetric,
    Metric,
    MetricStatus,
)
from waive.ledger.tables import LedgerMetricsRow
from waive.ledger.view import expect_lines, ledger_view
from waive.web.deps import deps_of, render

log = logging.getLogger(__name__)
router = APIRouter()

# The list's orders, as the select offers them. The first is the default.
SORTS = (("exposure", "Seniors at risk"), ("gap", "Free-care gap"), ("name", "Name"))


def card_class(metric: Metric) -> str:
    """The border colour of a metric's card on the atlas page: the warn colour for a hospital
    that promises more free care than it gives, red for one that sues, reports or sells debt,
    green for a hospital that keeps its promise or reported no such actions, blue once the
    senior exposure is fully known. Nothing while the metric has too little to say."""
    if isinstance(metric, GapMetric):
        if metric.label is GapLabel.PROMISES_MORE:
            return "status-unknown"
        return "status-free" if metric.label is not None else ""
    if isinstance(metric, ExposureMetric):
        return "status-discount" if metric.status is MetricStatus.KNOWN else ""
    if isinstance(metric, CollectionsMetric):
        flags = (
            metric.lawsuits,
            metric.liens,
            metric.credit_reporting,
            metric.sells_debt,
            metric.legal_action,
        )
        if any(flags):
            return "status-bad"
        if metric.flag_line is not None and any(flag is not None for flag in flags):
            return "status-free"
    return ""


def _sheet_status(session: Session, ccn: str) -> tuple[bool, str]:
    found = atlas_repo.latest_sheet(session, ccn)
    if found is None:
        return False, "no policy sheet yet"
    if found[0].status is SheetStatus.PUBLISHED:
        return True, "published"
    return True, "under review"


def _row(session: Session, hospital: HospitalRow, metrics: LedgerMetricsRow) -> dict[str, Any]:
    """One line of the list: who the hospital is, whether its page exists, and the three
    metrics in the words the view gives them (an unknown one is left empty)."""
    view = ledger_view(session, hospital.ccn)
    has_sheet, sheet_status = _sheet_status(session, hospital.ccn)
    gap_headline = collections_words = ""
    if view is not None:
        if view.gap.status is not MetricStatus.UNKNOWN:
            gap_headline = view.gap.headline
        collections_words = view.collections.headline
    return {
        "ccn": hospital.ccn,
        "name": hospital.name,
        "city": hospital.city,
        "state": hospital.state,
        "has_sheet": has_sheet,
        "sheet_status": sheet_status,
        "exposure_label": metrics.exposure_label,
        "exposure_rank": metrics.exposure_rank,
        "gap": metrics.gap,
        "gap_headline": gap_headline,
        "collections_words": collections_words,
    }


def _sorted(rows: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """Highest first for a rank or a gap, with the hospitals that have none at the end; the
    sort is stable, so ties keep the repo's CCN order."""
    if sort == "name":
        return sorted(rows, key=lambda row: row["name"])
    key = "exposure_rank" if sort == "exposure" else "gap"
    return sorted(rows, key=lambda row: (row[key] is None, -(row[key] or 0.0)))


@router.get("/ledger", response_class=HTMLResponse)
def ledger_list(request: Request, state: str = "", sort: str = "exposure") -> HTMLResponse:
    """Every hospital with a metrics row, ranked; the demo hospital never appears. A state that
    is not two letters means all states, and an unknown sort is the default one."""
    state = state.upper() if len(state) == 2 and state.isalpha() else ""
    sort = sort if sort in dict(SORTS) else SORTS[0][0]
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        listed = [
            (hospital, metrics)
            for hospital, metrics in repo.list_metrics(session)
            if not atlas_repo.is_demo(hospital.ccn)
        ]
        states = sorted({hospital.state for hospital, _ in listed if hospital.state})
        rows = [
            _row(session, hospital, metrics)
            for hospital, metrics in listed
            if not state or hospital.state == state
        ]
    return render(
        request,
        "ledger_list.html",
        rows=_sorted(rows, sort),
        states=states,
        state=state,
        sort=sort,
        sorts=SORTS,
    )


def install(templates: Jinja2Templates, engine: Engine) -> None:
    """Give the templates the ledger's helpers: `ledger_card_class` for the block's cards and
    `ledger_expect(ccn)` for the result screen's lines, which opens its own session because the
    screen is rendered inside the case's. It answers with nothing for a CCN it does not know,
    and with nothing rather than an error for anything else: the result page comes first."""

    def ledger_expect(ccn: str | None) -> list[tuple[str, str]]:
        if not ccn:
            return []
        try:
            with session_scope(engine) as session:
                return expect_lines(session, ccn)
        except Exception:
            log.exception("ledger lines for %s could not be built", ccn)
            return []

    templates.env.globals["ledger_expect"] = ledger_expect
    templates.env.globals["ledger_card_class"] = card_class
