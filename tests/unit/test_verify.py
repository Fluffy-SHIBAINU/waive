from decimal import Decimal

from waive.atlas.samples import SAMPLE_POLICY_TEXT, SAMPLE_SOURCE_ID, st_example_sheet
from waive.atlas.verify import normalize, quote_found, value_in_quote, verify_sheet

DOCS = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT}


def test_sample_sheet_verifies_completely():
    sheet = st_example_sheet()
    report = verify_sheet(sheet, DOCS)
    assert report.ok, report.rejected
    assert len(report.accepted) == len(sheet.field_paths())


def test_tampered_value_is_rejected():
    sheet = st_example_sheet()
    cited = sheet.eligibility.free_care_max_fpl.model_copy(update={"value": Decimal("300")})
    sheet = sheet.model_copy(
        update={"eligibility": sheet.eligibility.model_copy(update={"free_care_max_fpl": cited})}
    )
    report = verify_sheet(sheet, DOCS)
    assert ("eligibility.free_care_max_fpl", "value not in quote") in report.rejected


def test_invented_quote_is_rejected():
    sheet = st_example_sheet()
    cited = sheet.contacts.phone.model_copy(
        update={"quote": "Call our billing team any time at 617-555-0100"}
    )
    sheet = sheet.model_copy(
        update={"contacts": sheet.contacts.model_copy(update={"phone": cited})}
    )
    report = verify_sheet(sheet, DOCS)
    assert ("contacts.phone", "quote not found in source") in report.rejected


def test_missing_source_text_is_rejected():
    report = verify_sheet(st_example_sheet(), {})
    assert not report.ok
    assert all(reason == "source text missing" for _, reason in report.rejected)


def test_typography_and_line_breaks_do_not_break_matching():
    document = (
        "Patients enrolled in MassHealth or SNAP are presump-\ntively eligible for “free care”."
    )
    quote = 'enrolled in MassHealth or SNAP are presumptively eligible for "free care"'
    assert quote_found(quote, document)


def test_short_quotes_are_not_trusted():
    assert not quote_found("250%", SAMPLE_POLICY_TEXT)


def test_numbers_must_match_whole_tokens():
    assert value_in_quote(Decimal("250"), "at or below 250% of the guidelines")
    assert not value_in_quote(Decimal("250"), "at or below 2500 dollars")
    assert value_in_quote(1000, "balances over $1,000 qualify")


def test_trim_quotes_keeps_the_sentence_that_is_really_in_the_source():
    from waive.atlas.verify import matched_span, trim_quotes

    stitched = (
        "Patients with household income at or below 250% of the Federal Poverty Guidelines are "
        "eligible for free care. This sentence was invented by the model."
    )
    assert matched_span(stitched, SAMPLE_POLICY_TEXT).startswith("Patients with household income")
    assert matched_span("Entirely invented words here.", SAMPLE_POLICY_TEXT) is None
    sheet = st_example_sheet()
    cited = sheet.eligibility.free_care_max_fpl.model_copy(update={"quote": stitched})
    sheet = sheet.model_copy(
        update={"eligibility": sheet.eligibility.model_copy(update={"free_care_max_fpl": cited})}
    )
    assert not verify_sheet(sheet, DOCS).ok
    trimmed = trim_quotes(sheet, DOCS)
    assert verify_sheet(trimmed, DOCS).ok
    assert "invented" not in trimmed.eligibility.free_care_max_fpl.quote


def test_normalize_collapses_whitespace_and_case():
    assert normalize("  Free CARE \n here ") == "free care here"
