from datetime import date

import pytest

from waive.atlas.samples import SAMPLE_SOURCE_ID, st_example_sheet
from waive.atlas.schema import Cited
from waive.rules.deadlines import compute_deadlines, deadlines_for

FIRST = date(2026, 9, 3)
TODAY = date(2026, 10, 2)


def with_window(days):
    sheet = st_example_sheet()
    window = Cited[int](
        value=days,
        quote=f"accepted up to {days} days",
        source_id=SAMPLE_SOURCE_ID,
        checked_on=TODAY,
    )
    return sheet.model_copy(
        update={"apply": sheet.apply.model_copy(update={"window_days_from_first_bill": window})}
    )


def test_standard_501r_windows():
    deadlines = compute_deadlines(FIRST, TODAY)
    assert deadlines.application_deadline == date(2027, 5, 1)
    assert deadlines.collections_allowed_from == date(2027, 1, 1)
    assert deadlines.days_left_to_apply == 211
    assert deadlines.collection_notice_too_early is None


def test_flags_collection_notice_before_day_120():
    early = compute_deadlines(FIRST, TODAY, collection_notice=date(2026, 11, 15))
    on_time = compute_deadlines(FIRST, TODAY, collection_notice=date(2027, 1, 2))
    assert early.collection_notice_too_early is True
    assert on_time.collection_notice_too_early is False


def test_rejects_windows_shorter_than_the_law():
    with pytest.raises(ValueError):
        compute_deadlines(FIRST, TODAY, window_days=200)
    with pytest.raises(ValueError):
        compute_deadlines(FIRST, TODAY, eca_wait_days=90)


def test_uses_a_more_generous_policy_window():
    assert deadlines_for(with_window(365), FIRST, TODAY).application_deadline == date(2027, 9, 3)


def test_never_shorter_than_the_law_even_if_the_sheet_says_so():
    assert deadlines_for(with_window(90), FIRST, TODAY).application_deadline == date(2027, 5, 1)
