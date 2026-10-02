"""HHS poverty guidelines and percent-of-poverty math."""

import json
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from importlib import resources
from typing import Any


@cache
def _table(year: int) -> dict[str, Any]:
    path = resources.files("waive.rules") / "data" / f"fpl_{year}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def poverty_guideline(household_size: int, state: str, year: int = 2026) -> Decimal:
    if household_size < 1:
        raise ValueError("household_size must be at least 1")
    regions = _table(year)["regions"]
    region = regions.get(state.upper(), regions["contiguous"])
    return Decimal(region["base"] + (household_size - 1) * region["per_additional"])


def fpl_percent(
    annual_income: Decimal, household_size: int, state: str, year: int = 2026
) -> Decimal:
    if annual_income < 0:
        raise ValueError("annual_income cannot be negative")
    percent = annual_income / poverty_guideline(household_size, state, year) * 100
    return percent.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
