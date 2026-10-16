"""CMS Hospital Provider Cost Reports (HCRIS) into `ledger_cost_reports` (Phase 10).

data.cms.gov publishes one flat CSV per year ("Hospital Provider Cost Report : <date>", about
6,000 rows, every hospital type). The ledger keeps five money lines per hospital and fiscal year:
charity care at cost, bad debt, uncompensated care, total costs and net patient revenue. The
first and the fourth give the realised side of the free-care gap; the second and the fifth give
the bad-debt share the collections metric ranks."""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from waive.atlas import repo as atlas_repo
from waive.ledger import download
from waive.ledger.repo import upsert_cost_report

log = logging.getLogger(__name__)

COST_REPORT_TITLE = "Hospital Provider Cost Report"
COST_REPORT_LANDING = (
    "https://data.cms.gov/provider-compliance/cost-report/hospital-provider-cost-report"
)
CACHE_SUBDIR = "hcris"

# The CSV's column names, exactly as CMS quotes them in the header.
COL_RECORD = "rpt_rec_num"
COL_CCN = "Provider CCN"
COL_NAME = "Hospital Name"
COL_STATE = "State Code"
COL_CONTROL = "Type of Control"
COL_FY_BEGIN = "Fiscal Year Begin Date"
COL_FY_END = "Fiscal Year End Date"
COL_CHARITY = "Cost of Charity Care"
COL_BAD_DEBT = "Total Bad Debt Expense"
COL_UNCOMPENSATED = "Cost of Uncompensated Care"
COL_TOTAL_COSTS = "Total Costs"
COL_NET_REVENUE = "Net Patient Revenue"

# The files write dates as 10/01/2023; the data API writes 2023-10-01. Both parse.
DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d")


@dataclass(frozen=True)
class CostReportFacts:
    """One row of a cost report file, typed: what the ledger keeps of it."""

    rpt_rec_num: str
    ccn: str
    hospital_name: str
    state: str
    type_of_control: int | None
    fy_begin: date
    fy_end: date
    charity_care_cost: int | None
    bad_debt: int | None
    uncompensated_care_cost: int | None
    total_costs: int | None
    net_patient_revenue: int | None


@dataclass
class HcrisReport:
    """What one `load_cost_reports` run did, for the CLI to print."""

    files: list[str] = field(default_factory=list)
    rows_read: int = 0
    hospitals_loaded: int = 0
    by_year: dict[int, int] = field(default_factory=dict)


def _money(text: str | None) -> int | None:
    """Whole dollars from a cost report cell: blank or unparsable is None, cents are dropped."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return int(Decimal(text))
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _date(text: str | None) -> date | None:
    text = (text or "").strip()
    for pattern in DATE_FORMATS:
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _control(text: str | None) -> int | None:
    text = (text or "").strip()
    return int(text) if text.isdigit() else None


def parse_cost_report_csv(path: Path) -> Iterator[CostReportFacts]:
    """The rows of one cost report CSV. Rows without a CCN or without both fiscal-year dates are
    skipped: nothing can be keyed on them."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ccn = (row.get(COL_CCN) or "").strip()
            fy_begin = _date(row.get(COL_FY_BEGIN))
            fy_end = _date(row.get(COL_FY_END))
            if not ccn or fy_begin is None or fy_end is None:
                continue
            yield CostReportFacts(
                rpt_rec_num=(row.get(COL_RECORD) or "").strip(),
                ccn=ccn,
                hospital_name=(row.get(COL_NAME) or "").strip(),
                state=(row.get(COL_STATE) or "").strip(),
                type_of_control=_control(row.get(COL_CONTROL)),
                fy_begin=fy_begin,
                fy_end=fy_end,
                charity_care_cost=_money(row.get(COL_CHARITY)),
                bad_debt=_money(row.get(COL_BAD_DEBT)),
                uncompensated_care_cost=_money(row.get(COL_UNCOMPENSATED)),
                total_costs=_money(row.get(COL_TOTAL_COSTS)),
                net_patient_revenue=_money(row.get(COL_NET_REVENUE)),
            )


def pick_one_per_year(
    facts: Iterable[CostReportFacts],
) -> dict[tuple[str, int], CostReportFacts]:
    """One report per (ccn, fiscal year), the year being the one the period ends in. A hospital
    that changed its fiscal year files two reports ending in the same year; the longer period
    says more, and between equal lengths the later one is the current one. A full tie keeps
    the report seen first."""
    picked: dict[tuple[str, int], CostReportFacts] = {}
    for fact in facts:
        key = (fact.ccn, fact.fy_end.year)
        current = picked.get(key)
        if current is None or _rank(fact) > _rank(current):
            picked[key] = fact
    return picked


def _rank(fact: CostReportFacts) -> tuple[int, date]:
    return ((fact.fy_end - fact.fy_begin).days, fact.fy_end)


def load_cost_reports(
    session: Session,
    http: httpx.Client,
    cache_dir: Path,
    catalog: list[dict[str, Any]],
    *,
    today: date,
    years: int = 3,
    refresh: bool = False,
    ccns: set[str] | None = None,
) -> HcrisReport:
    """Fetch the newest `years` cost report files and upsert one row per registry hospital and
    fiscal year. The files overlap (a report ending in June 2024 may sit in the 2023 file or the
    2024 one), so the pick runs over every file at once; identical rows in two files count as
    the newest file's, so the vintage stored is the freshest CMS stamp for that report.
    `ccns` narrows the run to some registry hospitals; a CCN the registry lacks is never loaded
    (the row points at `hospitals`), and the demo hospital stays out either way."""
    keep = _registry_ccns(session)
    if ccns is not None:
        keep &= set(ccns)
    keep = {ccn for ccn in keep if not atlas_repo.is_demo(ccn)}
    report = HcrisReport()
    # fact -> (dataset, file name); newest file first, so setdefault keeps the newest origin.
    origins: dict[CostReportFacts, tuple[download.DatasetRef, str]] = {}
    for ref in download.datasets_named(catalog, COST_REPORT_TITLE)[:years]:
        if not ref.csv_url:
            log.warning("%s has no CSV; skipped", ref.title)
            continue
        path = download.fetch_file(http, ref.csv_url, cache_dir / CACHE_SUBDIR, refresh=refresh)
        report.files.append(path.name)
        read = 0
        for fact in parse_cost_report_csv(path):
            read += 1
            if fact.ccn in keep:
                origins.setdefault(fact, (ref, path.name))
        report.rows_read += read
        log.info("%s: %d rows", path.name, read)
    loaded: set[str] = set()
    for (ccn, fiscal_year), fact in pick_one_per_year(origins).items():
        ref, source_file = origins[fact]
        upsert_cost_report(
            session,
            ccn,
            fiscal_year,
            rpt_rec_num=fact.rpt_rec_num,
            fy_begin=fact.fy_begin,
            fy_end=fact.fy_end,
            charity_care_cost=fact.charity_care_cost,
            bad_debt=fact.bad_debt,
            uncompensated_care_cost=fact.uncompensated_care_cost,
            total_costs=fact.total_costs,
            net_patient_revenue=fact.net_patient_revenue,
            type_of_control=fact.type_of_control,
            hospital_name=fact.hospital_name,
            source_file=source_file,
            vintage=download.vintage_of(ref),
            loaded_on=today,
        )
        loaded.add(ccn)
        report.by_year[fiscal_year] = report.by_year.get(fiscal_year, 0) + 1
    report.hospitals_loaded = len(loaded)
    return report


def _registry_ccns(session: Session) -> set[str]:
    return {row.ccn for row in atlas_repo.list_hospitals(session)}


def charity_share(row: Any) -> float | None:
    """Charity care at cost as a percent of total costs, or None without a positive total."""
    return _percent(row.charity_care_cost, row.total_costs)


def bad_debt_share(row: Any) -> float | None:
    """Bad debt as a percent of net patient revenue, or None without positive revenue."""
    return _percent(row.bad_debt, row.net_patient_revenue)


def _percent(part: int | None, whole: int | None) -> float | None:
    if part is None or whole is None or whole <= 0:
        return None
    return part / whole * 100
