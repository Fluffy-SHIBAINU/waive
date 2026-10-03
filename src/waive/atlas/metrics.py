"""Atlas and cost metrics for the public /metrics page and the national report (spec §16)."""

import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.schedule import newest_fetch_by_ccn
from waive.atlas.schema import ProcedureSheet
from waive.db import SheetRow
from waive.governor import Governor
from waive.learning.scoreboard import scoreboard

RUN_LOG_HEADING = "## Run log"
RUN_LOG_HEADER = (
    RUN_LOG_HEADING
    + "\n\n| Date (ET) | Batch | Hospitals | Published | Held | Credits before → after | Notes |\n"
    "|---|---|---|---|---|---|---|\n"
)


@dataclass(frozen=True)
class StateCoverage:
    state: str
    hospitals: int
    published: int
    held: int
    none: int

    @property
    def share(self) -> float:
        return self.published / self.hospitals if self.hospitals else 0.0


@dataclass
class Metrics:
    today: date
    states: list[StateCoverage]
    hospitals: int
    published: int
    held: int
    none: int
    median_sheet_age_days: int | None
    documented_share: float | None
    accuracy: float | None
    scored_outcomes: int
    review_open: int
    credits_by_day: list[tuple[str, Decimal]]
    credits_today: Decimal
    daily_cap: int


def _latest_sheets(session: Session) -> dict[str, tuple[str, dict[str, Any]]]:
    """ccn -> (status, body) of each hospital's newest version, in one query."""
    newest = (
        select(SheetRow.ccn, func.max(SheetRow.version).label("version"))
        .group_by(SheetRow.ccn)
        .subquery()
    )
    query = select(SheetRow.ccn, SheetRow.status, SheetRow.body).join(
        newest, (SheetRow.ccn == newest.c.ccn) & (SheetRow.version == newest.c.version)
    )
    return {ccn: (status, body) for ccn, status, body in session.execute(query)}


def atlas_metrics(
    session: Session,
    today: date,
    governor: Governor | None = None,
    daily_cap: int = 0,
    days: int = 7,
) -> Metrics:
    hospitals = [row for row in repo.list_hospitals(session) if not repo.is_demo(row.ccn)]
    latest = _latest_sheets(session)
    newest = newest_fetch_by_ccn(session)
    per_state: dict[str, list[int]] = {}
    ages: list[int] = []
    completeness: list[float] = []
    for row in hospitals:
        tally = per_state.setdefault(row.state, [0, 0, 0, 0])
        tally[0] += 1
        found = latest.get(row.ccn)
        if found is None:
            tally[3] += 1
            continue
        status, body = found
        if status == "published":
            tally[1] += 1
            completeness.append(ProcedureSheet.model_validate(body).completeness())
        else:
            tally[2] += 1
        if (fetched := newest.get(row.ccn)) is not None:
            ages.append((today - fetched).days)
    states = [StateCoverage(state, *per_state[state]) for state in sorted(per_state)]
    scores = scoreboard(session)
    outcomes = sum(score.outcomes for score in scores)
    matched = sum(score.matched for score in scores)
    if governor is not None:
        by_day = governor.tavily_by_day(today - timedelta(days=days - 1), today)
        credits_today = governor.tavily_used_today(today)
    else:
        by_day, credits_today = [], Decimal("0")
    return Metrics(
        today=today,
        states=states,
        hospitals=len(hospitals),
        published=sum(s.published for s in states),
        held=sum(s.held for s in states),
        none=sum(s.none for s in states),
        median_sheet_age_days=int(statistics.median(ages)) if ages else None,
        documented_share=round(sum(completeness) / len(completeness), 2) if completeness else None,
        accuracy=round(matched / outcomes, 2) if outcomes else None,
        scored_outcomes=outcomes,
        review_open=len(repo.open_review_items(session)),
        credits_by_day=by_day,
        credits_today=credits_today,
        daily_cap=daily_cap,
    )


def metrics_json(metrics: Metrics) -> dict[str, Any]:
    return {
        "today": metrics.today.isoformat(),
        "hospitals": metrics.hospitals,
        "published": metrics.published,
        "held": metrics.held,
        "none": metrics.none,
        "median_sheet_age_days": metrics.median_sheet_age_days,
        "documented_share": metrics.documented_share,
        "accuracy": metrics.accuracy,
        "scored_outcomes": metrics.scored_outcomes,
        "review_open": metrics.review_open,
        "states": [
            {
                "state": s.state,
                "hospitals": s.hospitals,
                "published": s.published,
                "held": s.held,
                "none": s.none,
                "share": round(s.share, 3),
            }
            for s in metrics.states
        ],
        "credits_by_day": [{"day": day, "credits": str(c)} for day, c in metrics.credits_by_day],
        "credits_today": str(metrics.credits_today),
        "daily_cap": metrics.daily_cap,
        "license": "CC BY 4.0",
    }


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def national_report(metrics: Metrics) -> str:
    share = metrics.published / metrics.hospitals if metrics.hospitals else 0.0
    lines = [
        "# Atlas coverage — national",
        "",
        f"Generated {metrics.today.isoformat()} by `waive atlas report --national`. "
        "Counts only; open data, CC BY 4.0.",
        "",
        f"Hospitals in registry: {metrics.hospitals}",
        f"Published sheets: {metrics.published} ({share:.0%})",
        f"Held sheets: {metrics.held}; without a sheet: {metrics.none}",
        "Median sheet age: "
        + (
            "—"
            if metrics.median_sheet_age_days is None
            else f"{metrics.median_sheet_age_days} days"
        ),
        f"Core fields documented (published sheets): {_pct(metrics.documented_share)}",
        f"Prediction accuracy: {_pct(metrics.accuracy)} over {metrics.scored_outcomes} outcomes",
        f"Open review items: {metrics.review_open}",
        "",
        "| State | Hospitals | Published | Held | None | Coverage |",
        "|---|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {s.state} | {s.hospitals} | {s.published} | {s.held} | {s.none} | {s.share:.0%} |"
        for s in metrics.states
    )
    if metrics.credits_by_day:
        lines += ["", "| Day (UTC) | Tavily credits |", "|---|---|"]
        lines.extend(f"| {day} | {credits} |" for day, credits in metrics.credits_by_day)
    return "\n".join(lines) + "\n"


def write_national_report(path: Path, generated: str) -> None:
    """Write the generated part and keep the run log the orchestrator appends by hand: everything
    from RUN_LOG_HEADING to the end of the existing file survives a regeneration."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    index = existing.find(RUN_LOG_HEADING)
    tail = existing[index:] if index >= 0 else RUN_LOG_HEADER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generated.rstrip("\n") + "\n\n" + tail, encoding="utf-8")
