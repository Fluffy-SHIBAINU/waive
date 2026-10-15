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
    EligibilityResult(Tier.ASSISTANCE),
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


def test_messages_for_someone_working_alone_never_send_them_to_a_helper():
    # Phase 9: whoever holds the full link has no helper to be sent to; the next page is theirs.
    for result in ALL_TIERS:
        message = senior_message(result, "St. Example", alone=True)
        assert "helper" not in message.lower(), message
        sentences = [part for part in re.split(r"[.!?]\s*", message) if part]
        assert all(len(sentence.split()) <= 14 for sentence in sentences), message
    for result in ALL_TIERS[:2] + ALL_TIERS[4:]:
        assert senior_message(result, "St. Example", alone=True) == senior_message(
            result, "St. Example"
        )
    assert "next page" in senior_message(ALL_TIERS[2], "St. Example", alone=True)
    assert "next page" in senior_message(ALL_TIERS[3], "St. Example", alone=True)
    # The limited link's words are unchanged.
    assert "Your helper will ask you" in senior_message(ALL_TIERS[3], "St. Example")


def test_assistance_under_a_ceiling_says_the_hospital_decides_how_much_and_never_free():
    # Decision 7.9a-2 (task 7.14): the policy states only the income up to which help is given.
    from tests.unit.test_eligibility import CEILING_QUOTE, ceiling_only_sheet

    result = evaluate_eligibility(ceiling_only_sheet(), Household(1, Decimal("47880"), "MA"))
    assert result.tier is Tier.ASSISTANCE
    for alone in (False, True):
        message = senior_message(result, "St. Example", alone=alone)
        assert message == (
            "Good news. You can likely get help with this bill. St. Example decides how much "
            "help you get. We can fill in the form for you."
        )
    summary = caregiver_summary(result, "St. Example Medical Center")
    assert summary.split("\n") == [
        "Likely eligible for assistance at St. Example Medical Center.",
        "Income is 300% of the poverty line; assistance exists up to 400% of the poverty level, "
        "and the hospital decides the amount.",
        f'Policy says: "{CEILING_QUOTE}"',
        "This is an estimate. The hospital makes the final decision.",
    ]
    for text in (summary, senior_message(result, "St. Example")):
        assert "free" not in text.lower()
