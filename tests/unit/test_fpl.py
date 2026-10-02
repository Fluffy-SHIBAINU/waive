from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from waive.rules.fpl import fpl_percent, poverty_guideline


@pytest.mark.parametrize(
    ("size", "state", "expected"),
    [
        (1, "MA", 15960),
        (2, "MA", 21640),
        (4, "MA", 33000),
        (8, "MA", 55720),
        (9, "MA", 61400),
        (1, "AK", 19950),
        (8, "AK", 69650),
        (1, "HI", 18360),
        (8, "HI", 64070),
    ],
)
def test_matches_2026_table(size, state, expected):
    assert poverty_guideline(size, state) == Decimal(expected)


def test_rosa_is_about_143_percent():
    assert fpl_percent(Decimal("22800"), 1, "MA") == Decimal("142.86")


def test_state_code_is_case_insensitive():
    assert poverty_guideline(1, "ak") == Decimal("19950")


def test_rejects_bad_inputs():
    with pytest.raises(ValueError):
        poverty_guideline(0, "MA")
    with pytest.raises(ValueError):
        fpl_percent(Decimal("-1"), 1, "MA")


@given(st.integers(min_value=0, max_value=500_000), st.integers(min_value=1, max_value=12))
def test_more_income_never_lowers_percent(income, size):
    lower = fpl_percent(Decimal(income), size, "MA")
    higher = fpl_percent(Decimal(income + 1000), size, "MA")
    assert higher >= lower
