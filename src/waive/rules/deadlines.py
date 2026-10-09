"""501(r) timing: application window and collections protection (spec section 9, step 10)."""

from dataclasses import dataclass
from datetime import date, timedelta

from waive.atlas.schema import ProcedureSheet

MIN_WINDOW_DAYS = 240
MIN_ECA_WAIT_DAYS = 120
# Shown wherever provisional dates appear (review page, packet, calendar). Both clocks run from
# the first post-discharge statement (26 CFR 1.501(r)-6); a later statement overstates them.
ANCHOR_CAVEAT = (
    "Counted from the statement that was photographed; the law counts from the first bill "
    "after discharge, so the real dates may be earlier."
)


@dataclass(frozen=True)
class Deadlines:
    application_deadline: date
    collections_allowed_from: date
    days_left_to_apply: int
    collection_notice_too_early: bool | None
    # False when the start date is a later statement's, not the first bill's: the dates are then
    # provisional and no 501(r) claim may rest on them.
    anchor_confirmed: bool = True


def compute_deadlines(
    first_statement: date,
    today: date,
    *,
    window_days: int = MIN_WINDOW_DAYS,
    eca_wait_days: int = MIN_ECA_WAIT_DAYS,
    collection_notice: date | None = None,
    anchor_confirmed: bool = True,
) -> Deadlines:
    if window_days < MIN_WINDOW_DAYS:
        raise ValueError("501(r) requires an application window of at least 240 days")
    if eca_wait_days < MIN_ECA_WAIT_DAYS:
        raise ValueError("501(r) requires at least 120 days before collection actions")
    deadline = first_statement + timedelta(days=window_days)
    collections_from = first_statement + timedelta(days=eca_wait_days)
    too_early = (
        None
        if collection_notice is None or not anchor_confirmed
        else collection_notice < collections_from
    )
    return Deadlines(
        deadline, collections_from, (deadline - today).days, too_early, anchor_confirmed
    )


def deadlines_for(
    sheet: ProcedureSheet,
    first_statement: date,
    today: date,
    collection_notice: date | None = None,
    *,
    anchor_confirmed: bool = True,
) -> Deadlines:
    window = sheet.apply.window_days_from_first_bill
    wait = sheet.collections.eca_wait_days
    return compute_deadlines(
        first_statement,
        today,
        window_days=max(window.value if window else MIN_WINDOW_DAYS, MIN_WINDOW_DAYS),
        eca_wait_days=max(wait.value if wait else MIN_ECA_WAIT_DAYS, MIN_ECA_WAIT_DAYS),
        collection_notice=collection_notice,
        anchor_confirmed=anchor_confirmed,
    )
