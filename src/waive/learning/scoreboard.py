"""Per-hospital prediction accuracy from recorded outcomes (spec §10 scoreboard, §16)."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.db import CaseRow
from waive.learning.evidence import FlagLevel, slip_flag_level

MIN_OUTCOMES = 3
ACCURACY_FLOOR = 0.8


@dataclass(frozen=True)
class HospitalScore:
    ccn: str
    name: str
    outcomes: int
    matched: int
    accuracy: float
    sheet_version: int | None
    flag_level: FlagLevel

    @property
    def needs_recheck(self) -> bool:
        return self.outcomes >= MIN_OUTCOMES and self.accuracy < ACCURACY_FLOOR


def scoreboard(session: Session, state: str | None = None) -> list[HospitalScore]:
    """Rolling accuracy = matched outcomes / scored outcomes, per hospital, lowest first.
    Outcomes with `matched` None (more information asked, case issues) do not count."""
    tallies: dict[str, list[int]] = {}
    rows = session.scalars(
        select(CaseRow).where(CaseRow.outcome.is_not(None), CaseRow.ccn.is_not(None))
    )
    for row in rows:
        matched = (row.outcome or {}).get("matched")
        if matched is None:
            continue
        tally = tallies.setdefault(row.ccn, [0, 0])
        tally[0] += 1
        tally[1] += int(bool(matched))
    scores: list[HospitalScore] = []
    for hospital in repo.list_hospitals(session, state=state):
        if hospital.ccn not in tallies:
            continue
        outcomes, matched = tallies[hospital.ccn]
        found = repo.latest_sheet(session, hospital.ccn)
        scores.append(
            HospitalScore(
                ccn=hospital.ccn,
                name=hospital.name,
                outcomes=outcomes,
                matched=matched,
                accuracy=matched / outcomes,
                sheet_version=found[0].version if found else None,
                flag_level=slip_flag_level(session, hospital.ccn),
            )
        )
    return sorted(scores, key=lambda score: (score.accuracy, -score.outcomes))


def queue_prechecks(session: Session) -> list[str]:
    """Open one `priority_recheck` review item per hospital below the accuracy floor."""
    queued: list[str] = []
    for score in scoreboard(session):
        if not score.needs_recheck:
            continue
        if any(i.kind == "priority_recheck" for i in repo.open_review_items(session, score.ccn)):
            continue
        detail = {"outcomes": score.outcomes, "accuracy": round(score.accuracy, 2)}
        repo.add_review_item(session, score.ccn, "priority_recheck", detail)
        queued.append(score.ccn)
    return queued
