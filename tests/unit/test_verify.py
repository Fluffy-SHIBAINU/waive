from datetime import date
from decimal import Decimal

from waive.atlas.samples import SAMPLE_POLICY_TEXT, SAMPLE_SOURCE_ID, st_example_sheet
from waive.atlas.schema import Cited, DiscountTier
from waive.atlas.verify import (
    AGB_CAP_REASON,
    PATIENT_SHARE_REASON,
    looks_serialised,
    matched_span,
    normalize,
    quote_found,
    quotes_only_the_agb_cap,
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


def test_a_quote_must_start_at_a_word_boundary():
    # UMass Memorial (220163): "Phone: [508-334-9300](tel:...)" verified against a page that says
    # "Telephone:" because the normalized quote is a mid-word substring of the normalized page.
    document = (
        "UMass Memorial Health patient financial counseling contact information:\n\n"
        "* Telephone:\xa0[508-334-9300](tel:508-334-9300)\n* Email: needinsurance@example.org"
    )
    assert quote_found("Telephone: 508-334-9300", document)
    assert not quote_found("Phone: [508-334-9300](tel:508-334-9300)", document)
    # The hyphen- and space-insensitive fallback applies the same rule.
    assert quote_found("telephone: 508 334 9300", document)
    assert not quote_found("phone: 508 334 9300", document)
    # trim_quotes then falls back to the span the page really has.
    assert matched_span("Phone: [508-334-9300](tel:508-334-9300)", document) == (
        "[508-334-9300](tel:508-334-9300)"
    )


AGB_SENTENCE = (
    "If you qualify, you will not be billed more than the amount generally billed to patients "
    "with insurance coverage."
)


def with_insured(quote, value=True):
    sheet = st_example_sheet()
    cited = Cited(
        value=value, quote=quote, source_id=SAMPLE_SOURCE_ID, checked_on=date(2026, 10, 2)
    )
    return sheet.model_copy(
        update={
            "eligibility": sheet.eligibility.model_copy(update={"insured_patients_covered": cited})
        }
    )


def test_the_amounts_generally_billed_cap_does_not_say_who_may_apply():
    # UMass Memorial (220163): the 501(r) cap prices care for whoever qualifies; one run read it
    # as insured patients covered, the next as not covered. Only that clause mentions insurance.
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + AGB_SENTENCE + "\n"}
    report = verify_sheet(with_insured(AGB_SENTENCE), docs)
    assert ("eligibility.insured_patients_covered", AGB_CAP_REASON) in report.rejected
    assert quotes_only_the_agb_cap(AGB_SENTENCE)
    # A sentence that says who qualifies passes, even with the cap in the same breath.
    both = (
        "Uninsured and underinsured patients who qualify will not be charged more than the "
        "amounts generally billed to insured patients."
    )
    assert not quotes_only_the_agb_cap(both)
    assert not quotes_only_the_agb_cap(
        "Whether patients are uninsured or underinsured, they can apply for financial assistance."
    )
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + both + "\n"}
    assert verify_sheet(with_insured(both, value=True), docs).ok


ADVENTIST_TABLE = (
    "> 200% to 300% of the Federal Poverty Level 50% of the Amount Generally Billed > 300% to 350% "
    "of the Federal Poverty Level 75% of the Amount Generally Billed > 350% to 400% of the Federal "
    "Poverty Level 75% of the Amount Generally Billed"
)


def with_tiers_quote(quote, bands):
    sheet = st_example_sheet()
    tiers = [
        DiscountTier(
            min_fpl_exclusive=Decimal(low), max_fpl_inclusive=Decimal(high), discount_percent=pct
        )
        for low, high, pct in bands
    ]
    cited = sheet.eligibility.discount_tiers.model_copy(update={"value": tiers, "quote": quote})
    return sheet.model_copy(
        update={"eligibility": sheet.eligibility.model_copy(update={"discount_tiers": cited})}
    )


def test_a_fraction_of_the_amount_generally_billed_is_the_patients_share():
    # Adventist Health (050013 and five siblings, 7.9): the "Patient Responsibility" column of
    # the Hawaii policy's table was published as discount tiers 50/75/75, because the quote
    # dropped the header and "of the Amount Generally Billed" was not a patient-share phrase.
    assert quotes_patient_share(ADVENTIST_TABLE)
    assert quotes_patient_share("patients pay 50% of the AGB between 201% and 300%")
    assert not quotes_patient_share("a 50% discount off the amount generally billed")
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + ADVENTIST_TABLE + "\n"}
    sheet = with_tiers_quote(ADVENTIST_TABLE, [(200, 300, 50), (300, 350, 75), (350, 400, 75)])
    assert ("eligibility.discount_tiers", PATIENT_SHARE_REASON) in verify_sheet(
        sheet, docs
    ).rejected


def test_a_discount_that_rises_with_income_is_upside_down():
    from waive.atlas.verify import tiers_rise_with_income

    # No sliding scale gives a bigger discount to a richer household: a rising table is the
    # patient's share read as a discount, whatever words surround it.
    rising = "above 200% to 300% 50% discount; above 300% to 400% 75% discount"
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + rising + "\n"}
    sheet = with_tiers_quote(rising, [(200, 300, 50), (300, 400, 75)])
    assert tiers_rise_with_income(sheet.eligibility.discount_tiers.value)
    assert ("eligibility.discount_tiers", PATIENT_SHARE_REASON) in verify_sheet(
        sheet, docs
    ).rejected
    falling = "above 200% to 300% 75% discount; above 300% to 400% 50% discount"
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + falling + "\n"}
    sheet = with_tiers_quote(falling, [(200, 300, 75), (300, 400, 50)])
    assert not tiers_rise_with_income(sheet.eligibility.discount_tiers.value)
    assert verify_sheet(sheet, docs).ok
    flat = with_tiers_quote(falling, [(200, 300, 50), (300, 400, 50)])
    assert not tiers_rise_with_income(flat.eligibility.discount_tiers.value)


ARNOT_SENTENCE = (
    "Whether you are uninsured or insured, you may qualify for full or partial financial "
    "assistance if your household income is at or below 400% of the Federal Poverty Level for "
    "the 48 Contiguous States & D.C."
)


def test_full_or_partial_assistance_wording_is_a_ceiling_not_a_free_care_band():
    from waive.atlas.verify import CEILING_REASON, quotes_assistance_ceiling

    # Arnot Ogden (330090, 7.9): published free care up to 400% from a sentence that promises
    # "full or partial" help up to that income; sheet_inconsistencies only fires above 400%.
    assert quotes_assistance_ceiling(ARNOT_SENTENCE)
    assert quotes_assistance_ceiling(
        "Patients with income up to 300% FPL may qualify for financial assistance."
    )
    # Free-care wording passes, even next to the word "partial".
    assert not quotes_assistance_ceiling(
        "covers (i) 100% of charges for patients with family gross income less than or equal to "
        "200% of the federal poverty level; and (ii) a portion of charges above that"
    )
    assert not quotes_assistance_ceiling(
        "If your household income is 150% of the federal poverty guidelines or below, you may be "
        "eligible for free care"
    )
    assert not quotes_assistance_ceiling(
        "patients with family income of 200% of the Federal Poverty Level or less may be eligible "
        "for a discount of 100%"
    )
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + ARNOT_SENTENCE + "\n"}
    sheet = st_example_sheet()
    cited = sheet.eligibility.free_care_max_fpl.model_copy(
        update={"value": Decimal(400), "quote": ARNOT_SENTENCE}
    )
    sheet = sheet.model_copy(
        update={"eligibility": sheet.eligibility.model_copy(update={"free_care_max_fpl": cited})}
    )
    assert ("eligibility.free_care_max_fpl", CEILING_REASON) in verify_sheet(sheet, docs).rejected
