"""The ledger's words (Phase 10): every sentence a senior reads on the page or the result screen,
asserted verbatim, with no Python reprs, brackets or enum names in any of them."""

import re
from datetime import date

import pytest

from waive.ledger import explain
from waive.ledger.models import ExposureLabel, GapLabel

CLEAN = re.compile(r"[\[\]{}<>']|None|True|False|_[a-z]")


def assert_plain(text: str) -> None:
    assert text == "" or (text[0].isupper() and text.endswith(".")), text
    assert not CLEAN.search(text), text


@pytest.mark.parametrize(
    ("rank", "words"),
    [
        (None, ""),
        (1.0, "above 9 in 10 nonprofit hospitals"),
        (0.9, "above 9 in 10 nonprofit hospitals"),
        (0.85, "above 4 in 5 nonprofit hospitals"),
        (0.75, "above 3 in 4 nonprofit hospitals"),
        (0.7, "above 2 in 3 nonprofit hospitals"),
        (0.55, "above half of nonprofit hospitals"),
        (0.5, "about the middle of nonprofit hospitals"),
        (0.45, "below half of nonprofit hospitals"),
        (0.33, "below half of nonprofit hospitals"),
        (0.3, "below 2 in 3 nonprofit hospitals"),
        (0.2, "below 3 in 4 nonprofit hospitals"),
        (0.1, "below 4 in 5 nonprofit hospitals"),
        (0.0, "below 9 in 10 nonprofit hospitals"),
    ],
)
def test_rank_phrase(rank, words):
    assert explain.rank_phrase(rank) == words


def test_rank_phrase_takes_another_noun():
    assert explain.rank_phrase(0.95, "hospitals") == "above 9 in 10 hospitals"


def test_number_words():
    assert explain.percent(0.9) == "0.9%"
    assert explain.percent(2.61) == "2.6%"
    assert explain.percent(3.0) == "3%"
    assert explain.percent(12.04) == "12%"
    assert explain.fpl(300.0) == "300% of the poverty level"
    assert explain.fpl(137.5) == "137.5% of the poverty level"
    assert explain.month_year(date(2024, 9, 30)) == "September 2024"
    assert explain.count(770.4) == "770"
    assert explain.count(999) == "999"
    assert explain.count(1850) == "1,850"
    assert explain.count(1853) == "1,850"
    assert explain.count(8397.0) == "8,400"
    assert explain.count(12345) == "12,300"
    assert explain.count(0) == "0"


def test_gap_headlines():
    assert explain.gap_headline(GapLabel.PROMISES_MORE, 300.0, 0.9) == (
        "Promises more than it delivers"
    )
    assert explain.gap_headline(GapLabel.DELIVERS, 300.0, 2.0) == "Delivers what it promises"
    assert explain.gap_headline(GapLabel.PROMISES_LESS, 200.0, 5.0) == (
        "Promises less, delivers more"
    )
    assert explain.gap_headline(None, 300.0, None) == (
        "Free care promised up to 300% of the poverty level"
    )
    assert explain.gap_headline(None, None, 0.9) == "Spent 0.9% of its costs on free care"
    assert explain.gap_headline(None, None, None) == "Not enough data yet"


def test_gap_sentence_says_only_the_parts_that_exist():
    full = explain.gap_sentence(300.0, 0.85, 0.9, 0.2, date(2024, 9, 30), 1.1)
    assert full == (
        "Says free care goes up to 300% of the poverty level, above 4 in 5 nonprofit "
        "hospitals. Spent 0.9% of its costs on free care in the year to September 2024, "
        "below 3 in 4 nonprofit hospitals. Its tax filing puts financial assistance at 1.1% "
        "of expenses."
    )
    assert_plain(full)
    assert explain.gap_sentence(300.0, None, None, None, None, None) == (
        "Says free care goes up to 300% of the poverty level."
    )
    assert explain.gap_sentence(None, None, 0.9, None, None, None) == (
        "Spent 0.9% of its costs on free care."
    )
    assert explain.gap_sentence(None, None, 2.0, 0.5, None, 1.1) == (
        "Spent 2% of its costs on free care, about the middle of nonprofit hospitals. "
        "Its tax filing puts financial assistance at 1.1% of expenses."
    )
    assert explain.gap_sentence(None, None, None, None, None, None) == ""


def test_gap_short_lines():
    assert explain.gap_short(GapLabel.PROMISES_MORE, 300.0, 0.9, 0.2) == (
        "Free care is promised up to 300% of the poverty level, but given less often than "
        "at 3 in 4 hospitals."
    )
    assert explain.gap_short(GapLabel.PROMISES_MORE, 300.0, 1.5, 0.4) == (
        "Free care is promised up to 300% of the poverty level, but given less often than "
        "at half of hospitals."
    )
    # A high promise rank with a middling charity rank is still a gap, but not "less than half".
    assert explain.gap_short(GapLabel.PROMISES_MORE, 300.0, 2.0, 0.6) == (
        "Free care is promised up to 300% of the poverty level, but given less often than "
        "that promise suggests."
    )
    assert explain.gap_short(GapLabel.DELIVERS, 300.0, 2.0, 0.5) == (
        "Free care is promised up to 300% of the poverty level and given about as often as "
        "elsewhere."
    )
    assert explain.gap_short(GapLabel.PROMISES_LESS, 200.0, 5.0, 0.8) == (
        "Free care is promised up to 200% of the poverty level and given more often than at "
        "most hospitals."
    )
    assert explain.gap_short(None, 300.0, None, None) == (
        "Free care is promised up to 300% of the poverty level."
    )
    assert explain.gap_short(None, None, 5.0, None) == (
        "This hospital spent 5% of its costs on free care."
    )
    assert explain.gap_short(None, None, None, None) == ""


def test_gap_advice():
    assert explain.gap_advice(GapLabel.PROMISES_MORE) == (
        "Apply in writing, keep a copy, and send every document the list asks for the first time."
    )
    assert explain.gap_advice(GapLabel.DELIVERS) == (
        "Apply as the policy says; most people who qualify get it."
    )
    assert explain.gap_advice(GapLabel.PROMISES_LESS) == (
        "Ask even if the policy looks narrow; this hospital gives more free care than it "
        "advertises."
    )
    assert explain.gap_advice(None) == "Apply in writing and keep a copy."


def test_exposure_words():
    assert explain.exposure_headline(ExposureLabel.HIGH) == "High"
    assert explain.exposure_headline(ExposureLabel.MEDIUM) == "Medium"
    assert explain.exposure_headline(ExposureLabel.LOW) == "Low"
    assert explain.exposure_headline(None) == "Not enough data yet"
    full = explain.exposure_sentence(1850, 770.4, 1200, 480, 8397.0, "2024")
    assert full == (
        "About 770 of its 1,850 Medicare inpatients in 2024 were also on Medicaid. About "
        "8,400 people 65 or over living under twice the poverty line are in the ZIP codes it "
        "serves."
    )
    assert_plain(full)
    assert explain.exposure_sentence(1850, 770.4, None, None, None, None) == (
        "About 770 of its 1,850 Medicare inpatients were also on Medicaid."
    )
    assert explain.exposure_sentence(None, None, None, None, 8397.0, "2024") == (
        "About 8,400 people 65 or over living under twice the poverty line are in the ZIP "
        "codes it serves."
    )
    assert explain.exposure_sentence(1850, None, None, None, None, "2024") == (
        "It had about 1,850 Medicare inpatients in 2024."
    )
    assert explain.exposure_sentence(None, None, None, None, None, None) == ""
    assert explain.exposure_short(480, 1200) == (
        "About 4 in 10 of its Medicare patients are also on Medicaid."
    )
    assert explain.exposure_short(40, 1200) == (
        "Fewer than 1 in 10 of its Medicare patients are also on Medicaid."
    )
    assert explain.exposure_short(1150, 1200) == (
        "Nearly all of its Medicare patients are also on Medicaid."
    )
    assert explain.exposure_short(None, 1200) == ""
    assert explain.exposure_short(10, 0) == ""
    assert explain.exposure_advice() == (
        "If you have both Medicare and Medicaid, you cannot be billed for this visit. Waive "
        "writes that letter instead of an application."
    )


def test_action_words_name_patients_once():
    assert explain.action_words(True, True, True, None, None) == [
        "takes legal action against patients",
        "reports them to credit agencies",
        "sells their debt",
    ]
    assert explain.action_words(True, False, False, None, None) == [
        "reports patients to credit agencies"
    ]
    assert explain.action_words(False, False, None, True, True) == [
        "sues patients",
        "puts liens on their homes",
    ]
    assert explain.action_words(False, False, False, False, False) == []
    assert explain.action_words(None, None, None, None, None) == []


def test_collections_headline():
    assert explain.collections_headline(None, None, True, False, True, 3.6) == (
        "Takes legal action, reports to credit agencies"
    )
    assert explain.collections_headline(True, True, False, True, None, None) == (
        "Sues patients, places liens, sells debt"
    )
    assert explain.collections_headline(False, False, False, False, False, None) == (
        "No lawsuits, liens, credit reports or debt sales reported"
    )
    assert explain.collections_headline(None, None, None, None, None, None) == (
        "Practices not reported"
    )
    assert explain.collections_headline(None, None, None, None, None, 3.6) == (
        "Practices not reported; bad debt was 3.6% of revenue"
    )


def test_collections_sentence():
    engaged = explain.collections_sentence(
        2024, None, None, True, False, True, ["credit_reporting", "legal_action"], 3.6, 0.7
    )
    assert engaged == (
        "In its 2024 tax filing the hospital reported that, before checking whether patients "
        "qualified for free care, it took legal action against them and reported them to "
        "credit agencies. Bad debt was 3.6% of patient revenue, above 2 in 3 nonprofit "
        "hospitals."
    )
    assert_plain(engaged)
    three = explain.collections_sentence(2023, True, True, False, True, None, [], None, None)
    assert three == (
        "In its 2023 tax filing the hospital reported that, before checking whether patients "
        "qualified for free care, it sued them, put liens on their homes and sold their debt."
    )
    clean = explain.collections_sentence(
        2024, False, False, False, False, False, ["credit_reporting", "legal_action"], 3.6, None
    )
    assert clean == (
        "In its 2024 tax filing the hospital reported no lawsuits, liens, credit reporting or "
        "debt sales before checking who qualified for free care. Its policy permits credit "
        "reporting and legal action. Bad debt was 3.6% of patient revenue."
    )
    assert_plain(clean)
    assert explain.collections_sentence(
        2024, False, False, False, False, False, ["none"], None, None
    ) == (
        "In its 2024 tax filing the hospital reported no lawsuits, liens, credit reporting or "
        "debt sales before checking who qualified for free care."
    )
    assert explain.collections_sentence(
        2024, False, False, False, False, False, ["selling_debt", "other"], None, None
    ).endswith("Its policy permits selling debt and other actions.")
    unknown = explain.collections_sentence(None, None, None, None, None, None, [], 3.6, 0.7)
    assert unknown == (
        "Waive could not match this hospital to a tax filing, so whether it sues patients, "
        "places liens, reports to credit agencies or sells debt is unknown. Bad debt was 3.6% "
        "of patient revenue, above 2 in 3 nonprofit hospitals."
    )
    assert_plain(unknown)
    assert explain.collections_sentence(None, None, None, None, None, None, [], None, None) == (
        "Waive could not match this hospital to a tax filing, so whether it sues patients, "
        "places liens, reports to credit agencies or sells debt is unknown."
    )
    # A matched filing that left the collection lines blank.
    assert explain.collections_sentence(2022, None, None, None, None, None, [], None, None) == (
        "Its 2022 tax filing does not say whether it sues patients, places liens, reports to "
        "credit agencies or sells debt."
    )


def test_collections_short_and_advice():
    assert explain.collections_short(None, None, True, False, True) == (
        "This hospital takes legal action and reports patients to credit agencies before "
        "checking who qualifies."
    )
    assert explain.collections_short(True, False, False, True, None) == (
        "This hospital sues patients and sells patient debt before checking who qualifies."
    )
    assert explain.collections_short(False, False, False, False, False) == (
        "This hospital reported no lawsuits, liens or credit reporting before checking who "
        "qualifies."
    )
    assert explain.collections_short(None, None, None, None, None) == (
        "Whether this hospital sues or reports patients before checking who qualifies is not "
        "on file."
    )
    assert explain.collections_advice(True) == (
        "Apply within 120 days of the first bill. Waive sets the reminders and writes the "
        "letter if a collector calls."
    )
    assert explain.collections_advice(False) == (
        "Apply before the first bill is 120 days old; this hospital reported no collection "
        "actions before checking eligibility."
    )
    assert explain.collections_advice(None) == (
        "Apply within 120 days of the first bill and keep a copy of everything."
    )


def test_missing_words():
    assert set(explain.MISSING) == {
        "stated",
        "cost_report",
        "schedule_h",
        "inpatient",
        "service_area",
        "charity_cell",
        "bad_debt_cell",
    }
    assert explain.MISSING["charity_cell"] == "its cost report leaves the charity-care line blank"
    assert explain.MISSING["stated"] == (
        "no free-care limit on file (no published policy sheet and no matched tax filing)"
    )
    assert explain.MISSING["cost_report"] == "no cost report on file"
    assert explain.MISSING["schedule_h"] == "no tax filing matched to this hospital"
    assert explain.MISSING["inpatient"] == (
        "CMS does not publish this hospital's Medicare inpatient counts (fewer than 11 cases)"
    )
    assert explain.MISSING["service_area"] == "no ZIP code with 11 or more of its Medicare cases"


def test_a_five_percent_dual_share_reads_one_in_ten_not_zero():
    assert explain.exposure_short(60, 1200) == (
        "About 1 in 10 of its Medicare patients are also on Medicaid."
    )


def test_an_older_return_names_a_lawsuit_once():
    # The loader derives legal_action from the separate lawsuit and lien answers of a pre-2016
    # return; the words must not say "takes legal action" and "sues" for the same act.
    words = explain.action_words(
        credit_reporting=False, sells_debt=False, legal_action=True, lawsuits=True, liens=False
    )
    assert len(words) == 1 and "sues" in words[0]
