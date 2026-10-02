import re
from decimal import Decimal

from waive.atlas.samples import st_example_sheet
from waive.rules.eligibility import EligibilityResult, Household, Tier, evaluate_eligibility
from waive.rules.explain import caregiver_summary, senior_message

ALL_TIERS = [
    EligibilityResult(Tier.FREE, discount_percent=100),
    EligibilityResult(Tier.DISCOUNT, discount_percent=60),
    EligibilityResult(Tier.NOT_ELIGIBLE),
    EligibilityResult(Tier.NEEDS_INFO),
    EligibilityResult(Tier.UNKNOWN),
]


def test_senior_messages_are_short_and_hedged():
    for result in ALL_TIERS:
        message = senior_message(result, "St. Example")
        sentences = [part for part in re.split(r"[.!?]\s*", message) if part]
        assert all(len(sentence.split()) <= 14 for sentence in sentences), message
        assert "guarantee" not in message.lower()
    assert "likely" in senior_message(ALL_TIERS[0], "St. Example")
    assert "60%" in senior_message(ALL_TIERS[1], "St. Example")


def test_caregiver_summary_cites_the_policy():
    result = evaluate_eligibility(st_example_sheet(), Household(1, Decimal("22800"), "MA"))
    summary = caregiver_summary(result, "St. Example Medical Center")
    assert summary.startswith("Likely free care at St. Example Medical Center.")
    assert 'Policy says: "household income at or below 250%' in summary
    assert summary.endswith("This is an estimate. The hospital makes the final decision.")


def test_caregiver_summary_lists_missing_information():
    summary = caregiver_summary(
        EligibilityResult(Tier.NEEDS_INFO, missing=("household income",)), "St. Example"
    )
    assert "Still needed: household income." in summary
