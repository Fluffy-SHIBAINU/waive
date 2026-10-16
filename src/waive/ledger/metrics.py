"""The three ledger metrics, computed from the loaders' tables into `ledger_metrics`.

Ranks are percentiles against the other registry hospitals that have the same number, so a
hospital is compared only with hospitals whose data CMS or the IRS actually published. The
gap and collections columns come from `compute_behaviour` (`waive ledger behaviour`), the
exposure columns from `compute_exposure_metrics` (`waive ledger exposure`); each run writes
only its own columns, so the two can run on different days without erasing each other.
The demo hospital never gets a row: it would rank against real ones."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from waive.atlas import repo as atlas_repo
from waive.ledger import repo
from waive.ledger.models import ExposureLabel, GapLabel

# A hospital whose promise rank and delivery rank differ by a quarter of the field or more
# is called out either way; anything closer is "delivers what it promises".
GAP_THRESHOLD = 0.25


def percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    """key -> the share of the other keys with a strictly lower value, 0 to 1. Ties share a
    rank. One hospital cannot be ranked against itself, so a single value gives no ranks."""
    if len(values) < 2:
        return {}
    ordered = sorted(values.values())
    others = len(values) - 1
    return {key: bisect_left(ordered, value) / others for key, value in values.items()}


def gap_label(gap: float) -> GapLabel:
    if gap >= GAP_THRESHOLD:
        return GapLabel.PROMISES_MORE
    if gap <= -GAP_THRESHOLD:
        return GapLabel.PROMISES_LESS
    return GapLabel.DELIVERS


def exposure_label(rank: float) -> ExposureLabel:
    if rank >= 2 / 3:
        return ExposureLabel.HIGH
    if rank >= 1 / 3:
        return ExposureLabel.MEDIUM
    return ExposureLabel.LOW


def _share(part: int | None, whole: int | None) -> float | None:
    """part as a percent of whole, when both are stated and the whole is positive. Four
    decimals: enough to rank on, and 36 of 1,000 reads 3.6 rather than 3.5999999999999996."""
    if part is None or whole is None or whole <= 0:
        return None
    return round(part / whole * 100, 4)


def _registry(session: Session) -> list[str]:
    return [h.ccn for h in atlas_repo.list_hospitals(session) if not atlas_repo.is_demo(h.ccn)]


@dataclass
class BehaviourReport:
    hospitals: int = 0
    with_stated: int = 0
    stated_from_sheet: int = 0
    stated_from_990: int = 0
    with_cost_report: int = 0
    with_schedule_h: int = 0
    gap_known: int = 0
    gap_partial: int = 0
    collections_known: int = 0
    collections_partial: int = 0


@dataclass
class _Behaviour:
    """One hospital's inputs for metric 1 and metric 3 before ranking."""

    stated_fpl: float | None = None
    stated_source: str | None = None
    charity_cost_pct: float | None = None
    charity_pct_990: float | None = None
    cost_report_year: int | None = None
    lawsuits: bool | None = None
    liens: bool | None = None
    credit_reporting: bool | None = None
    sells_debt: bool | None = None
    legal_action: bool | None = None
    flag_line: str | None = None
    permitted_actions: str | None = None
    schedule_h_year: int | None = None
    bad_debt_pct: float | None = None


def compute_behaviour(session: Session, today: date) -> BehaviourReport:
    """Fill the gap and collections columns for every registry hospital with at least one
    input: a published sheet's free-care limit, a cost report or a Schedule H. The sheet's
    limit wins over the filing's because it is what the hospital tells patients today."""
    report = BehaviourReport()
    cost_reports = repo.all_best_cost_reports(session)
    filings = repo.all_latest_schedule_h(session)
    inputs: dict[str, _Behaviour] = {}
    for ccn in _registry(session):
        report.hospitals += 1
        found = _Behaviour()
        published = atlas_repo.published_sheet(session, ccn)
        limit = published[0].eligibility.free_care_max_fpl if published else None
        filing = filings.get(ccn)
        cost = cost_reports.get(ccn)
        if limit is not None and limit.value is not None:
            found.stated_fpl, found.stated_source = float(limit.value), "sheet"
            report.stated_from_sheet += 1
        elif filing is not None and filing.free_care_fpl_pct is not None:
            found.stated_fpl, found.stated_source = float(filing.free_care_fpl_pct), "irs990"
            report.stated_from_990 += 1
        if cost is not None:
            report.with_cost_report += 1
            found.charity_cost_pct = _share(cost.charity_care_cost, cost.total_costs)
            found.cost_report_year = cost.fiscal_year
            found.bad_debt_pct = _share(cost.bad_debt, cost.net_patient_revenue)
        if filing is not None:
            report.with_schedule_h += 1
            found.charity_pct_990 = filing.charity_pct_of_expense
            found.lawsuits = filing.lawsuits
            found.liens = filing.liens
            found.credit_reporting = filing.credit_reporting
            found.sells_debt = filing.sells_debt
            found.legal_action = filing.legal_action
            found.flag_line = filing.flag_line
            found.permitted_actions = filing.permitted_actions
            found.schedule_h_year = filing.tax_year
        if found.stated_fpl is not None or cost is not None or filing is not None:
            inputs[ccn] = found
    report.with_stated = report.stated_from_sheet + report.stated_from_990

    stated = {ccn: f.stated_fpl for ccn, f in inputs.items() if f.stated_fpl is not None}
    charity = {
        ccn: f.charity_cost_pct for ccn, f in inputs.items() if f.charity_cost_pct is not None
    }
    bad_debt = {ccn: f.bad_debt_pct for ccn, f in inputs.items() if f.bad_debt_pct is not None}
    stated_ranks = percentile_ranks(stated)
    charity_ranks = percentile_ranks(charity)
    bad_debt_ranks = percentile_ranks(bad_debt)

    for ccn, found in inputs.items():
        stated_rank = stated_ranks.get(ccn)
        charity_rank = charity_ranks.get(ccn)
        gap = None if stated_rank is None or charity_rank is None else stated_rank - charity_rank
        have_gap = (found.stated_fpl is not None) + (found.charity_cost_pct is not None)
        have_collections = (found.flag_line is not None) + (found.bad_debt_pct is not None)
        report.gap_known += have_gap == 2
        report.gap_partial += have_gap == 1
        report.collections_known += have_collections == 2
        report.collections_partial += have_collections == 1
        repo.upsert_metrics(
            session,
            ccn,
            stated_fpl=found.stated_fpl,
            stated_source=found.stated_source,
            stated_rank=stated_rank,
            stated_peers=len(stated) if stated_rank is not None else None,
            charity_cost_pct=found.charity_cost_pct,
            charity_rank=charity_rank,
            charity_peers=len(charity) if charity_rank is not None else None,
            charity_pct_990=found.charity_pct_990,
            cost_report_year=found.cost_report_year,
            gap=gap,
            gap_label=gap_label(gap).value if gap is not None else None,
            lawsuits=found.lawsuits,
            liens=found.liens,
            credit_reporting=found.credit_reporting,
            sells_debt=found.sells_debt,
            legal_action=found.legal_action,
            flag_line=found.flag_line,
            permitted_actions=found.permitted_actions,
            schedule_h_year=found.schedule_h_year,
            bad_debt_pct=found.bad_debt_pct,
            bad_debt_rank=bad_debt_ranks.get(ccn),
            bad_debt_peers=len(bad_debt) if ccn in bad_debt_ranks else None,
            behaviour_computed_on=today,
        )
    return report


@dataclass
class ExposureMetricsReport:
    hospitals: int = 0
    scored: int = 0
    with_dual: int = 0
    with_poor_seniors: int = 0


def compute_exposure_metrics(session: Session, today: date) -> ExposureMetricsReport:
    """Fill the exposure columns: the score is dual-eligible discharges plus the poor seniors
    in the service area, or whichever of the two CMS published, ranked over the hospitals
    that have a score. A hospital with neither number gets no row from this run."""
    report = ExposureMetricsReport()
    exposure = repo.all_exposure(session)
    scores: dict[str, float] = {}
    for ccn in _registry(session):
        report.hospitals += 1
        row = exposure.get(ccn)
        if row is None:
            continue
        parts = [v for v in (row.dual_discharges, row.poor_seniors_served) if v is not None]
        report.with_dual += row.dual_discharges is not None
        report.with_poor_seniors += row.poor_seniors_served is not None
        if parts:
            scores[ccn] = float(sum(parts))
    report.scored = len(scores)
    ranks = percentile_ranks(scores)
    for ccn, score in scores.items():
        rank = ranks.get(ccn)
        repo.upsert_metrics(
            session,
            ccn,
            exposure_score=score,
            exposure_rank=rank,
            exposure_peers=len(scores) if rank is not None else None,
            exposure_label=exposure_label(rank).value if rank is not None else None,
            exposure_computed_on=today,
        )
    return report
