"""The LedgerView for one hospital: the block on the atlas page, the `ledger` key of
`/atlas/{ccn}.json` and the result screen's "What to expect here" lines.

Built from the stored metrics row and the newest loader rows, so the vintages shown under
each card are the ones the numbers came from. Every card says which inputs it has, which are
missing and where each number came from; the words themselves come from `explain`."""

from __future__ import annotations

import re
from datetime import date

from sqlalchemy.orm import Session

from waive.atlas import repo as atlas_repo
from waive.ledger import explain, repo
from waive.ledger.metrics import is_stub_period
from waive.ledger.models import (
    CollectionsMetric,
    ExposureLabel,
    ExposureMetric,
    GapLabel,
    GapMetric,
    Ids,
    LedgerView,
    MetricStatus,
    Source,
)
from waive.ledger.tables import CostReportRow, ExposureRow, LedgerMetricsRow, ScheduleHRow

COST_REPORT_SOURCE = (
    "CMS Hospital Provider Cost Report",
    "https://data.cms.gov/provider-compliance/cost-report/hospital-provider-cost-report",
)
SCHEDULE_H_SOURCE = (
    "IRS Form 990 Schedule H",
    "https://projects.propublica.org/nonprofits/organizations/{ein}",
)
SHEET_SOURCE = ("Waive policy sheet", "/atlas/{ccn}")
INPATIENT_SOURCE = (
    "Medicare Inpatient Hospitals by Provider",
    "https://data.cms.gov/provider-summary-by-type-of-service/medicare-inpatient-hospitals/medicare-inpatient-hospitals-by-provider",
)
SERVICE_AREA_SOURCE = (
    "CMS Hospital Service Area",
    "https://data.cms.gov/provider-summary-by-type-of-service/medicare-inpatient-hospitals/hospital-service-area",
)
ACS_SOURCE = (
    "Census ACS 5-year, table B17024",
    "https://data.census.gov/table/ACSDT5Y2024.B17024",
)

_PERIOD_END = re.compile(r"\d{4}-\d{2}-\d{2} to (\d{4})")
_YEAR = re.compile(r"\d{4}")


def _status(*inputs: object) -> MetricStatus:
    present = sum(value is not None for value in inputs)
    if present == len(inputs):
        return MetricStatus.KNOWN
    return MetricStatus.PARTIAL if present else MetricStatus.UNKNOWN


def _cost_report_missing(cost: CostReportRow | None, cell: str) -> str:
    """Why a cost-report number is absent: no report, only a stub period, or a blank line."""
    if cost is None:
        return explain.MISSING["cost_report"]
    if is_stub_period(cost):
        return explain.MISSING["short_period"]
    return explain.MISSING[cell]


def _cost_report_source(cost: CostReportRow) -> Source:
    when = f"year to {explain.month_year(cost.fy_end)}" if cost.fy_end else "year unknown"
    vintage = ", ".join(part for part in (when, cost.vintage) if part)
    return Source(name=COST_REPORT_SOURCE[0], vintage=vintage, url=COST_REPORT_SOURCE[1])


def _schedule_h_source(filing: ScheduleHRow, ein: str | None) -> Source:
    ein = ein or filing.ein
    return Source(
        name=SCHEDULE_H_SOURCE[0],
        vintage=", ".join(part for part in (f"tax year {filing.tax_year}", filing.vintage) if part),
        url=SCHEDULE_H_SOURCE[1].format(ein=ein) if ein else None,
    )


def _year_of(vintage: str | None) -> str | None:
    """The data year named by a vintage: the end of its period, else the first year in it."""
    if not vintage:
        return None
    found = _PERIOD_END.search(vintage) or _YEAR.search(vintage)
    return found.group(1) if found and found.lastindex else (found.group(0) if found else None)


def _gap(
    session: Session,
    ccn: str,
    metrics: LedgerMetricsRow | None,
    cost: CostReportRow | None,
    filing: ScheduleHRow | None,
    ein: str | None,
) -> GapMetric:
    stated = metrics.stated_fpl if metrics else None
    charity = metrics.charity_cost_pct if metrics else None
    charity_990 = metrics.charity_pct_990 if metrics else None
    label = GapLabel(metrics.gap_label) if metrics and metrics.gap_label else None
    sources: list[Source] = []
    if metrics and metrics.stated_source == "sheet":
        published = atlas_repo.published_sheet(session, ccn)
        version = f"version {published[0].version}" if published else "unpublished"
        sources.append(
            Source(name=SHEET_SOURCE[0], vintage=version, url=SHEET_SOURCE[1].format(ccn=ccn))
        )
    if cost and charity is not None:
        sources.append(_cost_report_source(cost))
    if filing and ((metrics and metrics.stated_source == "irs990") or charity_990 is not None):
        sources.append(_schedule_h_source(filing, ein))
    missing = [explain.MISSING["stated"]] if stated is None else []
    if charity is None:
        missing.append(_cost_report_missing(cost, "charity_cell"))
    return GapMetric(
        status=_status(stated, charity),
        headline=explain.gap_headline(label, stated, charity),
        sentence=explain.gap_sentence(
            stated,
            metrics.stated_rank if metrics else None,
            charity,
            metrics.charity_rank if metrics else None,
            cost.fy_end if cost else None,
            charity_990,
        ),
        short=explain.gap_short(label, stated, charity, metrics.charity_rank if metrics else None),
        advice=explain.gap_advice(label),
        missing=missing,
        sources=sources,
        stated_free_care_fpl=stated,
        stated_source=metrics.stated_source if metrics else None,
        stated_rank=metrics.stated_rank if metrics else None,
        stated_peers=metrics.stated_peers if metrics else None,
        charity_cost_pct=charity,
        charity_rank=metrics.charity_rank if metrics else None,
        charity_peers=metrics.charity_peers if metrics else None,
        charity_pct_990=charity_990,
        cost_report_year=metrics.cost_report_year if metrics else None,
        gap=metrics.gap if metrics else None,
        label=label,
    )


def _exposure(metrics: LedgerMetricsRow | None, row: ExposureRow | None) -> ExposureMetric:
    dual = row.dual_discharges if row else None
    poor = row.poor_seniors_served if row else None
    label = ExposureLabel(metrics.exposure_label) if metrics and metrics.exposure_label else None
    sources: list[Source] = []
    if row and dual is not None:
        sources.append(
            Source(
                name=INPATIENT_SOURCE[0],
                vintage=row.inpatient_vintage or "",
                url=INPATIENT_SOURCE[1],
            )
        )
    if row and poor is not None:
        sources.append(
            Source(
                name=SERVICE_AREA_SOURCE[0],
                vintage=row.service_area_vintage or "",
                url=SERVICE_AREA_SOURCE[1],
            )
        )
        sources.append(Source(name=ACS_SOURCE[0], vintage=row.acs_vintage or "", url=ACS_SOURCE[1]))
    missing = [
        explain.MISSING[k] for k, v in (("inpatient", dual), ("service_area", poor)) if v is None
    ]
    return ExposureMetric(
        status=_status(dual, poor),
        headline=explain.exposure_headline(label),
        sentence=explain.exposure_sentence(
            row.discharges if row else None,
            dual,
            row.benes if row else None,
            row.dual_benes if row else None,
            poor,
            _year_of(row.inpatient_vintage) if row else None,
        ),
        short=explain.exposure_short(row.dual_benes if row else None, row.benes if row else None),
        advice=explain.exposure_advice(),
        missing=missing,
        sources=sources,
        discharges=row.discharges if row else None,
        dual_discharges=round(dual) if dual is not None else None,
        poor_seniors_served=round(poor) if poor is not None else None,
        score=metrics.exposure_score if metrics else None,
        rank=metrics.exposure_rank if metrics else None,
        peers=metrics.exposure_peers if metrics else None,
        label=label,
    )


def _collections(
    metrics: LedgerMetricsRow | None,
    cost: CostReportRow | None,
    filing: ScheduleHRow | None,
    ein: str | None,
) -> CollectionsMetric:
    m = metrics
    flags = (
        (m.lawsuits, m.liens, m.credit_reporting, m.sells_debt, m.legal_action)
        if m
        else (None, None, None, None, None)
    )
    flag_line = m.flag_line if m else None
    bad_debt = m.bad_debt_pct if m else None
    bad_debt_rank = m.bad_debt_rank if m else None
    permitted = (
        [a.strip() for a in (m.permitted_actions or "").split(",") if a.strip()] if m else []
    )
    if any(flags):
        any_engaged: bool | None = True
    elif flag_line is not None and any(flag is not None for flag in flags):
        any_engaged = False
    else:
        any_engaged = None
    sources: list[Source] = []
    if filing and flag_line is not None:
        sources.append(_schedule_h_source(filing, ein))
    if cost and bad_debt is not None:
        sources.append(_cost_report_source(cost))
    missing = [explain.MISSING["schedule_h"]] if flag_line is None else []
    if bad_debt is None:
        missing.append(_cost_report_missing(cost, "bad_debt_cell"))
    return CollectionsMetric(
        status=_status(flag_line, bad_debt),
        headline=explain.collections_headline(*flags, bad_debt),
        sentence=explain.collections_sentence(
            m.schedule_h_year if m else None, *flags, permitted, bad_debt, bad_debt_rank
        ),
        short=explain.collections_short(*flags),
        advice=explain.collections_advice(any_engaged),
        missing=missing,
        sources=sources,
        lawsuits=flags[0],
        liens=flags[1],
        credit_reporting=flags[2],
        sells_debt=flags[3],
        legal_action=flags[4],
        flag_line=flag_line,
        permitted_actions=permitted,
        schedule_h_year=m.schedule_h_year if m else None,
        bad_debt_pct=bad_debt,
        bad_debt_rank=bad_debt_rank,
        bad_debt_peers=m.bad_debt_peers if m else None,
    )


def ledger_view(session: Session, ccn: str) -> LedgerView | None:
    """The hospital's ledger, or None when Waive has neither an identity nor a metric for it
    (the page then shows no block at all rather than three empty cards)."""
    if atlas_repo.is_demo(ccn):
        return None  # the fictional demo hospital has no federal filings to show
    ids_row = repo.get_hospital_id(session, ccn)
    metrics = repo.get_metrics(session, ccn)
    if ids_row is None and metrics is None:
        return None
    cost = repo.best_cost_report(session, ccn)
    filing = repo.latest_schedule_h(session, ccn)
    exposure = repo.get_exposure(session, ccn)
    ein = ids_row.ein if ids_row else None
    ids = Ids(
        ein=ein,
        npi=ids_row.npi if ids_row else None,
        match_method=ids_row.match_method if ids_row else None,
        match_confidence=ids_row.match_confidence if ids_row else None,
    )
    stamps: list[date] = []
    if metrics:
        stamps = [d for d in (metrics.behaviour_computed_on, metrics.exposure_computed_on) if d]
    return LedgerView(
        ccn=ccn,
        ids=ids,
        gap=_gap(session, ccn, metrics, cost, filing, ein),
        exposure=_exposure(metrics, exposure),
        collections=_collections(metrics, cost, filing, ein),
        computed_on=max(stamps) if stamps else None,
    )


def ledger_json(session: Session, ccn: str) -> dict | None:
    view = ledger_view(session, ccn)
    return view.model_dump(mode="json") if view else None


def expect_lines(session: Session, ccn: str) -> list[tuple[str, str]]:
    view = ledger_view(session, ccn)
    return view.expect_lines() if view else []
