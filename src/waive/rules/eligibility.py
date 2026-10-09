"""Deterministic eligibility check against a procedure sheet (spec section 9, step 6)."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from waive.atlas.schema import Cited, ProcedureSheet
from waive.rules.fpl import fpl_percent

ASSET_WARNING = "This hospital also looks at savings and other assets."


class Tier(StrEnum):
    FREE = "free"
    DISCOUNT = "discount"
    NOT_ELIGIBLE = "not_eligible"
    NEEDS_INFO = "needs_info"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Household:
    size: int
    annual_income: Decimal | None
    state: str
    programs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Reason:
    text: str
    field_path: str
    quote: str | None
    source_id: str | None


@dataclass(frozen=True)
class EligibilityResult:
    tier: Tier
    fpl_percent: Decimal | None = None
    discount_percent: int | None = None
    reasons: tuple[Reason, ...] = ()
    missing: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _reason(text: str, path: str, cited: Cited[Any]) -> Reason:
    return Reason(text=text, field_path=path, quote=cited.quote, source_id=cited.source_id)


def evaluate_eligibility(sheet: ProcedureSheet, household: Household) -> EligibilityResult:
    eligibility = sheet.eligibility
    warnings: tuple[str, ...] = ()
    if eligibility.asset_test is not None and eligibility.asset_test.value:
        warnings = (ASSET_WARNING,)

    residency = eligibility.residency
    if residency is not None:
        allowed = {state.upper() for state in residency.value}
        if household.state.upper() not in allowed:
            text = f"The policy covers residents of {', '.join(residency.value)}."
            return EligibilityResult(
                Tier.NOT_ELIGIBLE,
                reasons=(_reason(text, "eligibility.residency", residency),),
                warnings=warnings,
            )

    presumptive = sheet.programs.presumptive
    if presumptive is not None:
        listed = {program.lower(): program for program in presumptive.value}
        matches = [listed[p.lower()] for p in household.programs if p.lower() in listed]
        if matches:
            text = f"People enrolled in {matches[0]} qualify for free care without an income check."
            return EligibilityResult(
                Tier.FREE,
                discount_percent=100,
                reasons=(_reason(text, "programs.presumptive", presumptive),),
                warnings=warnings,
            )

    free_limit = eligibility.free_care_max_fpl
    tiers = eligibility.discount_tiers
    has_tiers = tiers is not None and bool(tiers.value)
    if free_limit is None and not has_tiers:
        return EligibilityResult(
            Tier.UNKNOWN, missing=("hospital income limits",), warnings=warnings
        )
    if household.annual_income is None:
        return EligibilityResult(Tier.NEEDS_INFO, missing=("household income",), warnings=warnings)

    percent = fpl_percent(household.annual_income, household.size, household.state)
    # Paid bands first: a documented band is the specific answer for the incomes it names, so a
    # free-care limit misread from an eligibility ceiling can never turn "85% off" into "free".
    if tiers is not None and has_tiers:
        for tier in tiers.value:
            lower = tier.min_fpl_exclusive
            # A band written "0 – 400%" starts at no income at all; "0" is inclusive there.
            above_lower = percent >= lower if lower == 0 else percent > lower
            if above_lower and percent <= tier.max_fpl_inclusive:
                text = (
                    f"Income is {percent:.0f}% of the poverty line; the policy gives a "
                    f"{tier.discount_percent}% discount between {tier.min_fpl_exclusive:.0f}% "
                    f"and {tier.max_fpl_inclusive:.0f}%."
                )
                return EligibilityResult(
                    Tier.DISCOUNT,
                    percent,
                    tier.discount_percent,
                    (_reason(text, "eligibility.discount_tiers", tiers),),
                    warnings=warnings,
                )

    if free_limit is not None and percent <= free_limit.value:
        text = (
            f"Income is {percent:.0f}% of the poverty line; "
            f"free care covers up to {free_limit.value:.0f}%."
        )
        return EligibilityResult(
            Tier.FREE,
            percent,
            100,
            (_reason(text, "eligibility.free_care_max_fpl", free_limit),),
            warnings=warnings,
        )

    if tiers is not None and has_tiers:
        top = max(tier.max_fpl_inclusive for tier in tiers.value)
        cited: Cited[Any] = tiers
        path = "eligibility.discount_tiers"
    else:
        assert free_limit is not None
        top = free_limit.value
        cited = free_limit
        path = "eligibility.free_care_max_fpl"
    if percent <= top:
        # Below the lowest documented band, or in a gap between bands: the sheet is silent here
        # (often an extraction that kept only the paid bands), so the answer is "not yet known",
        # never a confident denial for the poorest households.
        text = (
            f"Income is {percent:.0f}% of the poverty line; the policy text we have does not "
            f"say what happens at {percent:.0f}%."
        )
        return EligibilityResult(
            Tier.UNKNOWN,
            percent,
            None,
            (_reason(text, path, cited),),
            missing=(f"hospital rules for incomes around {percent:.0f}% of the poverty line",),
            warnings=warnings,
        )
    text = f"Income is {percent:.0f}% of the poverty line; this policy helps up to {top:.0f}%."
    return EligibilityResult(
        Tier.NOT_ELIGIBLE, percent, None, (_reason(text, path, cited),), warnings=warnings
    )
