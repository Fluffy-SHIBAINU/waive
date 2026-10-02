"""501(r) timing: application window and collections protection (spec section 9, step 10)."""

from dataclasses import dataclass
from datetime import date, timedelta

from waive.atlas.schema import ProcedureSheet

MIN_WINDOW_DAYS = 240
MIN_ECA_WAIT_DAYS = 120


@dataclass(frozen=True)
class Deadlines:
    application_deadline: date
    collections_allowed_from: date
    days_left_to_apply: int
    collection_notice_too_early: bool | None


def compute_deadlines(
    first_statement: date,
    today: date,
    *,
    window_days: int = MIN_WINDOW_DAYS,
    eca_wait_days: int = MIN_ECA_WAIT_DAYS,
    collection_notice: date | None = None,
) -> Deadlines:
    if window_days < MIN_WINDOW_DAYS:
        raise ValueError("501(r) requires an application window of at least 240 days")
    if eca_wait_days < MIN_ECA_WAIT_DAYS:
        raise ValueError("501(r) requires at least 120 days before collection actions")
    deadline = first_statement + timedelta(days=window_days)
    collections_from = first_statement + timedelta(days=eca_wait_days)
    too_early = None if collection_notice is None else collection_notice < collections_from
    return Deadlines(deadline, collections_from, (deadline - today).days, too_early)


def deadlines_for(
    sheet: ProcedureSheet,
    first_statement: date,
    today: date,
    collection_notice: date | None = None,
) -> Deadlines:
    window = sheet.apply.window_days_from_first_bill
    wait = sheet.collections.eca_wait_days
    return compute_deadlines(
        first_statement,
        today,
        window_days=max(window.value if window else MIN_WINDOW_DAYS, MIN_WINDOW_DAYS),
        eca_wait_days=max(wait.value if wait else MIN_ECA_WAIT_DAYS, MIN_ECA_WAIT_DAYS),
        collection_notice=collection_notice,
    )
