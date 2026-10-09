"""Always-on scouting: a priority queue over the registry and a budgeted job (spec §8 step 8,
§15 daily budget, §16 metrics).

Priority = staleness × demand × (1 − accuracy). Staleness is days since the newest document was
fetched; demand counts open cases and review items asking for this hospital; accuracy comes from
the scoreboard. One job runs inside the app (APScheduler) and scouts at most one hospital per
tick, never past the day's Tavily budget; the same code runs from `waive atlas schedule`.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from waive.ai.client import AIClient
from waive.atlas import repo
from waive.atlas.pipeline import BuildResult, build_hospital
from waive.atlas.refresh import refresh_hospital
from waive.atlas.schema import HospitalRef
from waive.atlas.tavily_gateway import TavilyGateway, make_tavily_gateway
from waive.cases.extract import BillExtract
from waive.cases.match import is_confident, match_hospital
from waive.config import Settings
from waive.db import CaseRow, ReviewItemRow, SourceDocRow, hospital_sources, session_scope
from waive.governor import BudgetExceeded, Governor
from waive.learning.scoreboard import MIN_OUTCOMES, scoreboard

log = logging.getLogger(__name__)

# A hospital nobody has scouted counts as a quarter stale: new coverage matters, but a sheet that
# patients are actively contradicting can outrank it.
NEVER_SCOUTED_DAYS = 90
# Below MIN_OUTCOMES scored outcomes the scoreboard says nothing; assume a coin flip.
DEFAULT_ACCURACY = 0.5
# A perfectly accurate sheet still goes stale: keep a fifth of the staleness pressure.
ACCURACY_TERM_FLOOR = 0.2
# Phase 2 measured 4–5 Tavily credits per hospital (two searches, a Map, one or two Extracts); the
# advanced re-extraction of a page that rendered as navigation only (task 2.8h) adds two, so a
# hospital is started only with seven credits left in the day. A refresh (one Extract, at most one
# advanced pass) fits inside the same reservation.
CREDITS_PER_HOSPITAL = Decimal("7")
# Failed discoveries, empty scouts and structurer refusals (task 7.8: documents stored, no sheet)
# are not retried for a month. Without the pause a hospital with documents but no sheet would be
# sent to the content-hash refresh daily, spending a Tavily credit each time and, the documents
# being unchanged, never reaching the structurer again.
RETRY_AFTER_DAYS = 30
FAILURE_KINDS = ("domain", "no_documents", "structure_failed")
DEMAND_WEIGHTS = {"open_case": 1, "rescout_request": 2, "priority_recheck": 2, "scout_request": 2}
# Overlay sources (ids `state-…`, the shared mass.gov page) say nothing about a hospital's own
# documents; repository copies (ids `repo-…`, task 7.5) do.
FRESHNESS_KINDS = ("hospital_web", "state_repository")
JOB_ID = "scout"

Stop = Literal["queue empty", "limit reached", "daily budget", "credit cap"]


@dataclass(frozen=True)
class QueueEntry:
    ccn: str
    name: str
    state: str
    staleness_days: int
    demand: int
    accuracy: float | None
    priority: float
    reasons: tuple[str, ...]
    has_sources: bool
    rescout_requested: bool


@dataclass
class QueueReport:
    entries: list[QueueEntry]
    skipped: dict[str, str]
    unmatched_requests: list[dict[str, str | None]]
    without_sheet: int


def priority_score(staleness_days: int, demand: int, accuracy: float | None) -> float:
    """staleness × demand × (1 − accuracy), floored so that a fresh, perfect sheet still comes
    back around and a hospital nobody asked about still refreshes on age alone."""
    known = DEFAULT_ACCURACY if accuracy is None else accuracy
    accuracy_term = max(ACCURACY_TERM_FLOOR, 1.0 - known)
    return round(max(1, staleness_days) * max(1, demand) * accuracy_term, 2)


def newest_fetch_by_ccn(session: Session) -> dict[str, date]:
    query = (
        select(hospital_sources.c.ccn, func.max(SourceDocRow.fetched_on))
        .join(SourceDocRow, SourceDocRow.id == hospital_sources.c.source_id)
        .where(SourceDocRow.kind.in_(FRESHNESS_KINDS), SourceDocRow.id.not_like("state-%"))
        .group_by(hospital_sources.c.ccn)
    )
    return {ccn: fetched for ccn, fetched in session.execute(query)}


def _open_cases_by_ccn(session: Session) -> dict[str, int]:
    """Cases matched to a hospital that have no outcome yet: people are waiting on this sheet."""
    query = (
        select(CaseRow.ccn, func.count())
        .where(CaseRow.ccn.is_not(None), CaseRow.outcome.is_(None))
        .group_by(CaseRow.ccn)
    )
    return {ccn: int(count) for ccn, count in session.execute(query)}


def _recent_failures(session: Session, today: date) -> set[str]:
    cutoff = today - timedelta(days=RETRY_AFTER_DAYS)
    rows = session.scalars(
        select(ReviewItemRow).where(
            ReviewItemRow.status == "open", ReviewItemRow.kind.in_(FAILURE_KINDS)
        )
    )
    return {row.ccn for row in rows if row.ccn and row.created_at.date() >= cutoff}


def _request_matches(
    items: list[ReviewItemRow], by_state: dict[str, list[HospitalRef]]
) -> tuple[dict[str, int], list[dict[str, str | None]]]:
    """`scout_request` items carry a bill's hospital name and FAP web address but no CCN; the
    bill matcher attributes them to a registry hospital when it is confident."""
    matched: dict[str, int] = {}
    unmatched: list[dict[str, str | None]] = []
    for item in items:
        detail = item.detail or {}
        refs = by_state.get(str(detail.get("state") or "").upper(), [])
        extract = BillExtract(
            hospital_name=detail.get("hospital_name"), fap_url=detail.get("fap_url")
        )
        candidates = match_hospital(extract, refs) if refs else []
        if candidates and is_confident(candidates):
            # One request per hospital name; `count` is the number of distinct cases behind it.
            bills = int(detail.get("count", 1))
            matched[candidates[0].ccn] = matched.get(candidates[0].ccn, 0) + bills
        else:
            unmatched.append(
                {"hospital_name": detail.get("hospital_name"), "state": detail.get("state")}
            )
    return matched, unmatched


def build_queue(session: Session, today: date, states: tuple[str, ...] = ()) -> QueueReport:
    hospitals = [row for row in repo.list_hospitals(session) if not repo.is_demo(row.ccn)]
    if states:
        wanted = {state.upper() for state in states}
        hospitals = [row for row in hospitals if row.state in wanted]
    newest = newest_fetch_by_ccn(session)
    open_cases = _open_cases_by_ccn(session)
    failures = _recent_failures(session, today)
    sheeted = repo.ccns_with_sheets(session)
    accuracy = {
        score.ccn: score.accuracy for score in scoreboard(session) if score.outcomes >= MIN_OUTCOMES
    }
    items = repo.open_review_items(session)
    signals: dict[str, dict[str, int]] = {}
    for item in items:
        if item.ccn is None or item.kind not in ("rescout_request", "priority_recheck"):
            continue
        weight = int((item.detail or {}).get("count", 1)) if item.kind == "rescout_request" else 1
        kinds = signals.setdefault(item.ccn, {})
        kinds[item.kind] = kinds.get(item.kind, 0) + weight
    by_state: dict[str, list[HospitalRef]] = {}
    for row in hospitals:
        by_state.setdefault(row.state, []).append(repo.hospital_ref(row))
    requests, unmatched = _request_matches(
        [item for item in items if item.kind == "scout_request" and item.ccn is None], by_state
    )

    entries: list[QueueEntry] = []
    skipped: dict[str, str] = {}
    without_sheet = 0
    for row in hospitals:
        without_sheet += row.ccn not in sheeted
        if row.ccn in failures:
            skipped[row.ccn] = f"recent failure; retried after {RETRY_AFTER_DAYS} days"
            continue
        fetched = newest.get(row.ccn)
        staleness = NEVER_SCOUTED_DAYS if fetched is None else (today - fetched).days
        if fetched is not None and staleness <= 0:
            skipped[row.ccn] = "scouted today"
            continue
        demand = 1
        reasons: list[str] = []
        if cases := open_cases.get(row.ccn, 0):
            demand += DEMAND_WEIGHTS["open_case"] * cases
            reasons.append(f"{cases} open case(s)")
        for kind, count in sorted(signals.get(row.ccn, {}).items()):
            demand += DEMAND_WEIGHTS[kind] * count
            reasons.append(f"{kind} ×{count}")
        if bills := requests.get(row.ccn, 0):
            demand += DEMAND_WEIGHTS["scout_request"] * bills
            reasons.append(f"{bills} bill(s) named this hospital")
        if fetched is None:
            reasons.append("never scouted")
        entries.append(
            QueueEntry(
                ccn=row.ccn,
                name=row.name,
                state=row.state,
                staleness_days=staleness,
                demand=demand,
                accuracy=accuracy.get(row.ccn),
                priority=priority_score(staleness, demand, accuracy.get(row.ccn)),
                reasons=tuple(reasons),
                has_sources=fetched is not None,
                rescout_requested="rescout_request" in signals.get(row.ccn, {}),
            )
        )
    entries.sort(key=lambda entry: (-entry.priority, entry.name))
    return QueueReport(entries, skipped, unmatched, without_sheet)


@dataclass
class RunReport:
    today: date
    daily_cap: int
    used_before: Decimal
    used_after: Decimal
    results: list[BuildResult] = field(default_factory=list)
    stopped: Stop = "queue empty"


def _note_scouted(session: Session, ccn: str, today: date) -> None:
    """Mark the items that asked for this scout; they stay open for the admin's verdict."""
    for item in repo.open_review_items(session, ccn):
        if item.kind in ("rescout_request", "priority_recheck"):
            item.detail = {**(item.detail or {}), "rescouted_on": today.isoformat()}
    session.flush()


def _scout(
    session: Session,
    gateway: TavilyGateway,
    ai: AIClient,
    entry: QueueEntry,
    today: date,
) -> BuildResult:
    """Refresh by content hash when documents are stored and nobody contradicted the sheet; a
    full re-scout (search, map, extract) for never-scouted hospitals, for re-scout requests, and
    when every stored document is unreachable."""
    if entry.has_sources and not entry.rescout_requested:
        refreshed = refresh_hospital(session, gateway, ai, entry.ccn, today)
        if refreshed.outcome == "restructured" and refreshed.build is not None:
            return refreshed.build
        if refreshed.outcome == "unchanged" and len(refreshed.unreachable) < refreshed.checked:
            return BuildResult(
                entry.ccn,
                entry.name,
                "skipped",
                notes=[f"unchanged ({refreshed.checked} documents checked)"],
            )
    return build_hospital(session, gateway, ai, entry.ccn, today, reuse_sources=False)


def run_entries(
    session: Session,
    gateway: TavilyGateway,
    ai: AIClient,
    governor: Governor,
    today: date,
    entries: list[QueueEntry],
    *,
    daily_cap: int,
    limit: int | None = None,
) -> RunReport:
    """Scout `entries` in order until the list, the limit or the day's credits run out. The hard
    cap still applies inside the gateway (`BudgetExceeded` stops the run)."""
    used = governor.tavily_used_today(today)
    report = RunReport(today, daily_cap, used, used)
    for entry in entries:
        if limit is not None and len(report.results) >= limit:
            report.stopped = "limit reached"
            break
        if Decimal(daily_cap) - report.used_after < CREDITS_PER_HOSPITAL:
            report.stopped = "daily budget"
            break
        try:
            result = _scout(session, gateway, ai, entry, today)
        except BudgetExceeded as error:
            session.rollback()
            report.results.append(BuildResult(entry.ccn, entry.name, "failed", notes=[str(error)]))
            report.stopped = "credit cap"
            break
        except Exception as error:  # keep the run going; the failure is in the report
            session.rollback()
            result = BuildResult(entry.ccn, entry.name, "failed", notes=[type(error).__name__])
        else:
            _note_scouted(session, entry.ccn, today)
        report.results.append(result)
        session.commit()
        report.used_after = governor.tavily_used_today(today)
        log.info("scout %s %s: %s", entry.ccn, entry.name, result.outcome)
    return report


def run_once(
    session: Session,
    gateway: TavilyGateway,
    ai: AIClient,
    governor: Governor,
    today: date,
    *,
    daily_cap: int,
    states: tuple[str, ...] = (),
    limit: int | None = None,
) -> RunReport:
    queue = build_queue(session, today, states)
    return run_entries(
        session, gateway, ai, governor, today, queue.entries, daily_cap=daily_cap, limit=limit
    )


def scheduler_states(settings: Settings) -> tuple[str, ...]:
    return tuple(s.strip().upper() for s in settings.scheduler_states.split(",") if s.strip())


def scout_tick(
    engine: Engine, settings: Settings, governor: Governor, ai: AIClient | None
) -> RunReport | None:
    """One scheduler tick: at most one hospital, inside the daily budget. None when a key is
    missing — the app runs without scouting and nothing is spent."""
    if ai is None or settings.tavily_api_key is None:
        log.info("scout tick skipped: no model key or no Tavily key")
        return None
    gateway = make_tavily_gateway(settings, governor)
    today = datetime.now(UTC).date()
    with session_scope(engine) as session:
        report = run_once(
            session,
            gateway,
            ai,
            governor,
            today,
            daily_cap=settings.scout_daily_credits,
            states=scheduler_states(settings),
            limit=1,
        )
    log.info(
        "scout tick: %d built, %s credits used today, stopped: %s",
        len(report.results),
        report.used_after,
        report.stopped,
    )
    return report


def make_scheduler(engine: Engine, settings: Settings, governor: Governor, ai: AIClient | None):
    """A background scheduler with the one scouting job, not yet started."""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        scout_tick,
        IntervalTrigger(minutes=settings.scheduler_interval_minutes),
        args=[engine, settings, governor, ai],
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    return scheduler
