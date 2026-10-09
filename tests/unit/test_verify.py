from decimal import Decimal

from waive.atlas.samples import SAMPLE_POLICY_TEXT, SAMPLE_SOURCE_ID, st_example_sheet
from waive.atlas.schema import DiscountTier
from waive.atlas.verify import (
    PATIENT_SHARE_REASON,
    looks_serialised,
    normalize,
    quote_found,
    quotes_patient_share,
    value_in_quote,
    verify_sheet,
)

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


def test_values_written_out_as_a_list_or_object_are_rejected():
    # Anna Jaques (CCN 220029): the model returned phone as a list of objects, the structurer
    # stringified it and the quote check let it through, since strings were never inspected.
    sheet = st_example_sheet()
    serialised = "[{'kind': 'main', 'number': '617-555-0100'}, {'kind': 'main', 'number': '0101'}]"
    cited = sheet.contacts.phone.model_copy(update={"value": serialised})
    sheet = sheet.model_copy(
        update={"contacts": sheet.contacts.model_copy(update={"phone": cited})}
    )
    report = verify_sheet(sheet, DOCS)
    assert ("contacts.phone", "value is a list or object written as text") in report.rejected
    assert looks_serialised(serialised) and looks_serialised(" {'name': 'MassHealth'}")
    # One serialised item spoils a list of strings too.
    assert looks_serialised(["MassHealth", "{'name': 'SNAP'}"])
    assert not looks_serialised("617-555-0100") and not looks_serialised(["MassHealth", "SNAP"])
    assert not looks_serialised(Decimal("250")) and not looks_serialised(True)


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


MERCY_TABLE = (
    "Level 1 Level 2 Level 3 Qualifying Criterion Less than 100% FPL 101 - 200% FPL "
    "201 - 250% FPL Patient Responsibility None Co-Pay Co-Pay + 15% of total charges"
)


def with_tier_quote(quote, low=201, high=250, percent=15):
    sheet = st_example_sheet()
    tier = DiscountTier(
        min_fpl_exclusive=Decimal(low), max_fpl_inclusive=Decimal(high), discount_percent=percent
    )
    cited = sheet.eligibility.discount_tiers.model_copy(update={"value": [tier], "quote": quote})
    return sheet.model_copy(
        update={"eligibility": sheet.eligibility.model_copy(update={"discount_tiers": cited})}
    )


def test_a_table_of_the_patients_share_is_not_a_discount_table():
    # Mercy Medical Center (220066): "Co-Pay + 15% of total charges" is what the patient pays,
    # yet 15 passed as a discount because every number appears in the quote.
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + MERCY_TABLE + "\n"}
    report = verify_sheet(with_tier_quote(MERCY_TABLE), docs)
    assert ("eligibility.discount_tiers", PATIENT_SHARE_REASON) in report.rejected
    assert quotes_patient_share(MERCY_TABLE)
    assert quotes_patient_share("patients pay 15% of charges between 201 and 250")
    # Real discount wording passes, even when the patient's share is mentioned alongside it.
    assert not quotes_patient_share("between 201% and 250% patients receive a 15% discount")
    assert not quotes_patient_share("patient responsibility is 15% after an 85% discount")
    assert not quotes_patient_share("Lowell General: Inpatient Discount 100% 63.28% 30%")
    discount = (
        "between 201% and 250% of the Federal Poverty Guidelines patients receive a 15% discount"
    )
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + discount + "\n"}
    assert verify_sheet(with_tier_quote(discount), docs).ok


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


def test_markdown_links_and_bold_do_not_break_quotes():
    document = (
        "income is below 301% of the**[Federal Poverty Guidelines](https://x.gov/fpl)** per year."
    )
    assert quote_found("income is below 301% of the Federal Poverty Guidelines", document)


def test_normalize_collapses_whitespace_and_case():
    assert normalize("  Free CARE \n here ") == "free care here"
