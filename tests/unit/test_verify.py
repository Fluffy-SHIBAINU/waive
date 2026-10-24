from datetime import date
from decimal import Decimal

import pytest

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


# The Tufts Medicine table quoted for MelroseWakefield (220070) and Lowell General (220063).
TUFTS_TABLE = (
    "Family Size 100% FPL Up to 150% FPL Up to 300% FPL Greater than 301% FPL 1 $13,596 $20,388 "
    "$40,776 $40,777 TM Lowell General Hospital Inpatient Discount 100% 63.28% 30% TM Lowell "
    "General Hospital Outpatient Discount 70.54% TM Melrose Wakefield Hospital Inpatient Discount "
    "100% 36.84% 30% TM Melrose Wakefield Hospital Outpatient Discount 65.91%"
)


def test_a_number_never_matches_the_integer_part_of_a_decimal():
    from waive.atlas.verify import anchored_span, widened_span

    # Task 7.15a: MelroseWakefield (220070) published a 65% inpatient tier read from the
    # outpatient "65.91%", Lowell General (220063) 63 from "63.28%", Brigham and Women's (220110) a
    # free-care limit of 150 from a Chinese table row that starts at 150.1.
    assert not value_in_quote(Decimal(65), TUFTS_TABLE)
    assert not value_in_quote(Decimal(63), TUFTS_TABLE)
    assert not value_in_quote(Decimal(150), "150.1 至 250% 85%")
    # A whole number still matches before a sign, a space or the end of a sentence, and a decimal
    # matches itself.
    assert value_in_quote(Decimal(65), "Patients in this band receive a 65% discount.")
    assert value_in_quote(Decimal(250), "household income of up to 250.")
    assert value_in_quote(Decimal("65.91"), TUFTS_TABLE)
    assert value_in_quote(Decimal("63.28"), TUFTS_TABLE)
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + TUFTS_TABLE + "\n"}
    melrose = with_tiers_quote(TUFTS_TABLE, [(100, 150, 65), (150, 300, 30)])
    assert ("eligibility.discount_tiers", "value not in quote") in verify_sheet(
        melrose, docs
    ).rejected
    # The anchoring helpers read "137.5%" whole: it is not a limit of 137.
    passage = "Free care is available to patients at or below 137.5% of the Federal Poverty Level."
    assert anchored_span(passage, Decimal(137)) is None
    assert anchored_span(passage, Decimal("137.5")) == passage
    lead = "Our counselors will help you apply."
    document = f"{lead} {passage}"
    assert widened_span(lead, document, Decimal(137)) is None
    assert widened_span(lead, document, Decimal("137.5")) == document


def test_a_decimal_discount_verifies_against_its_quote_and_trailing_zeros_match():
    # Chenango (330033): the table states 83.5%; the tier keeps it and verifies.
    chenango = (
        "Annual Family Income Amount of Discount Less than 200% of FPG 100% 201% to 250% of FPG "
        "83.5% 251% to 300% of FPG 67%"
    )
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + chenango + "\n"}
    sheet = with_tiers_quote(chenango, [(200, 250, Decimal("83.5")), (250, 300, 67)])
    assert verify_sheet(sheet, docs).ok, verify_sheet(sheet, docs).rejected
    # A whole value matches with trailing zeros after it ("$25.00", "200.00%"), never a decimal.
    from waive.atlas.verify import states_income_limit

    assert value_in_quote(Decimal(25), "Balances of $25.00 or more qualify.")
    assert not value_in_quote(Decimal(25), "Balances of $25.50 or more qualify.")
    assert value_in_quote(Decimal("63.2"), "Inpatient Discount 63.20%")
    assert states_income_limit(
        "Patients at or below 200.00% of the FPL receive free care.", Decimal(200)
    )
    assert not states_income_limit(
        "Patients at or below 200.05% of the FPL receive free care.", Decimal(200)
    )


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


def tiers_of(bands):
    return [
        DiscountTier(
            min_fpl_exclusive=Decimal(low), max_fpl_inclusive=Decimal(high), discount_percent=pct
        )
        for low, high, pct in bands
    ]


def with_tiers_quote(quote, bands):
    sheet = st_example_sheet()
    cited = sheet.eligibility.discount_tiers.model_copy(
        update={"value": tiers_of(bands), "quote": quote}
    )
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


def with_bool(path, value, quote):
    section_name, field_name = path.split(".")
    sheet = st_example_sheet()
    cited = Cited(
        value=value, quote=quote, source_id=SAMPLE_SOURCE_ID, checked_on=date(2026, 10, 2)
    )
    section = getattr(sheet, section_name).model_copy(update={field_name: cited})
    return sheet.model_copy(update={section_name: section})


def verdict(path, value, quote):
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + quote + "\n"}
    report = verify_sheet(with_bool(path, value, quote), docs)
    return next((reason for p, reason in report.rejected if p == path), "accepted")


ADVENTIST_ASSETS = (
    "Furthermore, the first ten thousand dollars ($10,000) of a patient's monetary assets shall "
    "not be counted in determining eligibility, nor shall 50 percent of patient's monetary assets "
    "over the first ten thousand dollars ($10,000) be counted in determining eligibility."
)


def test_an_asset_test_value_must_be_supported_by_its_quote():
    from waive.atlas.verify import ASSET_REASON, quote_supports_asset_test

    path = "eligibility.asset_test"
    # 7.9: value_in_quote accepted any boolean, so 21 sheets carried asset_test values whose
    # quotes said the opposite or nothing at all.
    assert verdict(path, False, ADVENTIST_ASSETS) == ASSET_REASON  # a partial exemption
    assert (
        verdict(path, False, "an asset means test may also be applied to Medicare recipients only.")
        == ASSET_REASON
    )
    assert (
        verdict(
            path, False, "Proof of Assets does not apply to applicants at or below 200% of the FPL."
        )
        == ASSET_REASON
    )
    assert (
        verdict(path, False, "Eligibility is determined on the patient's family household income.")
        == ASSET_REASON
    )
    assert (
        verdict(
            path,
            False,
            "will never charge patients eligible for financial assistance more than AGB.",
        )
        == ASSET_REASON
    )
    assert (
        verdict(
            path,
            False,
            "Asset determinations will never include the primary residence or the primary automobile.",
        )
        == ASSET_REASON
    )
    assert (
        verdict(
            path,
            True,
            "Patients enrolled in MassHealth or SNAP are presumptively eligible for free care",
        )
        == ASSET_REASON
    )
    # Quotes that say what the value says pass.
    assert (
        verdict(
            path,
            False,
            "Assets and employment status are not considered in qualifying for this program.",
        )
        == "accepted"
    )
    assert (
        verdict(
            path,
            False,
            "AHMC does NOT consider your monetary assets in determining your eligibility for Charity Care.",
        )
        == "accepted"
    )
    assert (
        verdict(
            path,
            False,
            "Asset testing is not required for financial assistance for NHSC facilities.",
        )
        == "accepted"
    )
    assert (
        verdict(
            path,
            False,
            "There are no income or asset criteria requirements associated with this discount.",
        )
        == "accepted"
    )
    assert (
        verdict(path, True, "an asset means test may also be applied to Medicare recipients only.")
        == "accepted"
    )
    assert (
        verdict(
            path,
            True,
            "Asset limits for eligibility may not exceed $3,000 for applicant and $3,000 for spouse.",
        )
        == "accepted"
    )
    assert (
        verdict(
            path,
            True,
            "such individual is not eligible for assistance under this policy until such assets are exhausted.",
        )
        == "accepted"
    )
    assert quote_supports_asset_test(
        "Assets will also be used when: residency is outside the U.S.", True
    )
    assert not quote_supports_asset_test(
        "Assets will also be used when: residency is outside the U.S.", False
    )


def test_an_insured_patients_value_needs_a_quote_about_insured_patients():
    from waive.atlas.verify import INSURED_LEFT_OUT_REASON, INSURED_REASON

    path = "eligibility.insured_patients_covered"
    # 7.9: eight sheets took the value from sentences about uninsured patients only, EMTALA or
    # physician fees; the AGB-cap rule caught just one shape of that.
    assert (
        verdict(
            path,
            True,
            "This Policy also provides guidelines for discounted amounts that may be charged to all uninsured patients.",
        )
        == INSURED_REASON
    )
    assert (
        verdict(
            path,
            True,
            "Financial assistance is available for emergency care or medically necessary care.",
        )
        == INSURED_REASON
    )
    assert (
        verdict(
            path,
            False,
            "Financial assistance and discounts are available only for necessary hospital care.",
        )
        == INSURED_LEFT_OUT_REASON
    )
    assert (
        verdict(
            path,
            False,
            "In order to receive CCP financial assistance, the patient must apply for Medicaid and be denied.",
        )
        == INSURED_LEFT_OUT_REASON
    )
    # A false value needs the quote to exclude insured patients, not merely to mention them.
    assert (
        verdict(
            path,
            False,
            "Financial Assistance is offered to patients who are uninsured and underinsured.",
        )
        == INSURED_LEFT_OUT_REASON
    )
    # Holyoke (220024, held): an applicant's requirement that reads as a definition of the
    # uninsured patient, not a rule that leaves insured patients out.
    assert (
        verdict(
            path,
            False,
            "Does not have any form of insurance to cover services rendered that are medically necessary",
        )
        == INSURED_LEFT_OUT_REASON
    )
    # Leaving out co-pays and deductibles leaves insured patients in for the rest (Tufts, 220116).
    assert (
        verdict(
            path,
            False,
            "It is also not intended to provide discounts on insurance co-payments, co-insurance, or deductibles.",
        )
        == INSURED_LEFT_OUT_REASON
    )
    assert (
        verdict(
            path, True, "Both insured and uninsured patients are eligible for financial assistance."
        )
        == "accepted"
    )
    assert (
        verdict(
            path,
            True,
            "Financial assistance for insured patients is available once a patient receives a bill.",
        )
        == "accepted"
    )
    assert (
        verdict(
            path,
            True,
            "Whether patients are uninsured or underinsured, they can apply for financial assistance.",
        )
        == "accepted"
    )
    assert (
        verdict(
            path,
            True,
            "For patients that have insurance coverage, assistance is limited to deductible, coinsurance, co-pay.",
        )
        == "accepted"
    )


MIZELL_INSURED = (
    "If an individual has sufficient insurance coverage or assets available to pay for care, "
    "he/she may be deemed ineligible for financial assistance."
)
WRMC_STATEMENT = (
    "Insured patients will receive a statement for their applicable out-of-pocket responsibility "
    "as determined by their insurance company."
)
# (quote, stored value, the reason insured_field_problem gives: None to accept, or a key below)
_INSURED_CASES = [
    # The 2026-10-24 audit. False stored where the policy helps underinsured patients or only
    # says insured patients "may be deemed ineligible" (Mizell, 010007; Caldwell, 181322) or
    # where the quote is the uninsured self-pay discount (Mackinac, 231306).
    (MIZELL_INSURED, False, "left out"),
    ("Patients shall be able to get financial assistance if uninsured, underinsured, not eligible "
     "for a government program, or not qualify for governmental assistance (for example, Medicare "
     "or Medicaid).", False, "left out"),
    ("Uninsured patients who do not qualify for Mackinac Straits Health System's financial "
     "assistance program (e.g., due to income and family size) will receive a discount of 30% on "
     "gross charges for medically necessary services to ensure they do not pay more for care than "
     "insured individuals.", False, "price cap"),
    # True cited to a billing statement (Washington Regional, 040004), a payment plan (Western
    # Maryland, 210027), the AGB cap (Hutchinson Health, 240187) or a bare heading (Madelia,
    # 241323).
    (WRMC_STATEMENT, True, "payment"),
    ("Payment plans are available to patients with family income between 200 and 500 percent of "
     "the Federal Poverty Level for those patients who request assistance, regardless of their "
     "insurance status.", True, "payment"),
    ("After a patient’s eligibility for financial assistance under this policy is determined, a "
     "financial assistance-eligible patient will not be charged more than AGB to insured patients "
     "by the HealthPartners care delivery location for emergency or other medically necessary "
     "care.", True, "price cap"),
    ("Underinsured:", True, "label"),
    # A genuine exclusion still supports false; insured patients named as helped support true.
    ("Patients with insurance are not eligible for financial assistance", False, None),
    ("Insured patients who do not have the ability to pay for emergent or medically necessary "
     "portions not covered by insurance who meet the eligibility criteria outlined in this "
     "policy.", True, None),
    # Not a rule about financial assistance: a self-pay rate, a conditional, a criterion that
    # admits inadequate coverage, public insurance programs.
    ("Insured patients are not eligible for uninsured/true self pay rates.", False, "left out"),
    ("If you have insurance or assets to pay for your care, you may not be eligible for financial "
     "assistance.", False, "left out"),
    ("Have no insurance or inadequate insurance coverage (high deductible plan)", False,
     "left out"),
    ("You may qualify if: * You do not qualify for public insurance programs in your home state, "
     "and", False, "left out"),
    ("If you qualify for fnancial assistance, you will not be charged more for emergency or other "
     "medically necessary care than patients who are covered by Medicare or other insurance.",
     True, "price cap"),
    ("Montefiore Medical Center also provides payment arrangements to patients that have "
     "insurance coverage but have an out-of-pocket expense that they cannot afford or deem a "
     "hardship.", True, "payment"),
    # Exclusions of insured patients, including the "only uninsured" wording that has no insured
    # word of its own.
    ("Financial assistance is available only to uninsured patients.", False, None),
    ("This policy applies only to uninsured patients who meet the income criteria.", False, None),
    ("Insured patients are not eligible for financial assistance under this policy.", False, None),
    ("Patients with health insurance are not eligible for the charity care program.", False, None),
    ("The program is not available to patients with insurance.", False, None),
    ("Applicants must be uninsured.", False, None),
    ("Applicant cannot be covered by private insurance", False, None),  # Bacon County, 111327
    ("*Must be self-pay", False, None),  # Rockcastle County, 180115
    # The amounts-generally-billed cap, whichever value it was read as (UMass Memorial, 220163).
    (AGB_SENTENCE, True, "agb"),
    (AGB_SENTENCE, False, "agb"),
    # Insured patients named as helped, with or beside an AGB clause.
    ("Uninsured and underinsured patients who qualify will not be charged more than the amounts "
     "generally billed to insured patients.", True, None),
    ("Eligible patients with insurance coverage are not personally responsible to pay more than "
     "AGB after all payments by the health insurer have been applied.", True, None),
    ("The Financial Assistance Adjustment based on the AGB percentage will also be applied to the "
     "outstanding balance, after payments by insurance, if any, of patients with Catastrophic "
     "Medical Expenses, as defined above, who are not eligible for financial assistance based on "
     "the other provisions of this policy.", True, None),
    ("Insured patients with family income less than 450% of current Federal Poverty Level and "
     "excessive medical costs, may qualify for partially discounted care and extended payment "
     "plans.", True, None),
    ("Underinsured: The patient has some level of insurance or third-party assistance but still "
     "has out-of-pocket expenses that exceed his/her financial abilities.", True, None),
    ("Insured Patient Free And Discounted Care (set forth on Appendix B)", True, None),
    ("Insured patients will not have the AGB applied 2. The FA discount will be applied directly "
     "to the balance", True, None),
    ("Underinsured patients will not be charged more than amounts generally billed to patients "
     "with insurance.", True, None),
    ("Only patients with active insurance coverage are eligible for financial assistance.", True,
     None),
    # Leaving insured patients out of a self-pay, uninsured, prompt-pay or cash price says nothing
    # about financial assistance (231306, 141332, 310041); "not only" is no exclusion.
    ("Self-pay discounts are offered only to uninsured patients.", False, "left out"),
    ("The uninsured discount is available only to uninsured patients.", False, "left out"),
    ("Patients with insurance are not eligible for the 40% self-pay discount.", False, "left out"),
    ("Insured patients are not eligible for the prompt pay discount.", False, "left out"),
    ("The Discounted Cash Price is NOT available to governmentally covered or commercially "
     "insured patients.", False, "left out"),
    ("Financial assistance is offered not only to the uninsured but also to insured patients with "
     "high deductibles.", False, "left out"),
    # The price-cap clause stops at ", and ...".
    ("Patients who qualify will pay no more than 10% of charges, and insured patients may also "
     "qualify.", True, None),
    # Insurance named only as absent, or as something to buy (Geisinger, 390006; Houston
    # Methodist, 450358), against coverage that pays only part of the bill (Maine).
    ("Patient does not have insurance coverage and was denied Medicaid or any other Medical "
     "Assistance benefits.", True, "absent"),
    ("Individuals with the financial capacity to purchase health insurance will be encouraged to "
     "do so, as a means of providing access to health care services.", True, "absent"),
    ("Have no health insurance coverage or have coverage that pays only part of the bill; and",
     True, None),
    ("Have limited or no health insurance", True, None),
    ("We encourage patients to apply for financial assistance and patients with or without "
     "insurance are eligible.", True, None),
    ("If you do not have insurance and/or will have difficulty in paying your portion of the bill, "
     "we will review your eligibility for financial assistance", True, None),
    ("If you are not insured or insured for only part of your expenses, please talk with one of "
     "our financial counselors. They can help you establish a payment plan or apply for financial "
     "assistance.", True, None),
    ("Patients without insurance or with Medicare may qualify for financial assistance.", True,
     None),
    ("Payment in full is required at the time of service from all patients who owe co-pays, "
     "coinsurance, have not met their deductible, or do not have insurance coverage.", True,
     "payment"),  # 250094
    ("Financial assistance is available regardless of insurance status.", True, None),
    ("care for all uninsured patients", True, "insured"),
    ("even if you have some insurance", True, None),
    # Plain exclusions in other words.
    ("Patients with insurance coverage are not eligible for financial assistance.", False, None),
    ("Patients who are insured are not eligible for financial assistance.", False, None),
    ("This policy does not apply to insured patients.", False, None),
    # Cabell Huntington (510055, published v2, and its v1 draft): anyone whose bill any payor,
    # health insurance included, pays a portion of is not eligible. McKenzie (351302, held)
    # leaves out only patients whose insurer lists it as out of network, so it stays rejected.
    ("Patients are not eligible for financial assis tance, as provided for in this Policy, if the "
     "patient is eligible to receive reimbursement for any portion of a patient’s bill from any "
     "first or third party payor, including health insurance, workers’ compensation or liability "
     "payors,", False, None),
    ("Patients are not eligible for financial assistance, as provided for in this Policy, if the "
     "patient is eligible to receive reimbursement for any portion of a patient’s bill from any "
     "first or third party payor, including health insurance", False, None),
    ("Patients are not eligible for financial assistance if the applicable health insurance lists "
     "McKenzie Health as out-of-network or if the patient’s health insurance covers services on a "
     "non-contracted basis unless the services are emergency medical care provided in an "
     "emergency department or are pre-determined to be unique to McKenzie Health and such "
     "treatment is not available at another healthcare provider;", False, "left out"),
]  # fmt: skip


@pytest.mark.parametrize(("quote", "value", "expected"), _INSURED_CASES)
def test_insured_field_problem(quote, value, expected):
    from waive.atlas.verify import (
        ABSENT_REASON,
        BARE_LABEL_REASON,
        INSURED_LEFT_OUT_REASON,
        INSURED_REASON,
        PAYMENT_PLAN_REASON,
        PRICE_CAP_REASON,
        insured_field_problem,
    )

    reasons = {
        None: None,
        "agb": AGB_CAP_REASON,
        "insured": INSURED_REASON,
        "left out": INSURED_LEFT_OUT_REASON,
        "price cap": PRICE_CAP_REASON,
        "label": BARE_LABEL_REASON,
        "payment": PAYMENT_PLAN_REASON,
        "absent": ABSENT_REASON,
    }
    assert insured_field_problem(quote, value) == reasons[expected]
    # verify_sheet rejects the field with the same reason.
    assert verdict("eligibility.insured_patients_covered", value, quote) == (
        reasons[expected] or "accepted"
    )


def test_a_discount_given_as_a_range_or_a_floor_is_not_one_tier():
    from waive.atlas.verify import RANGE_REASON, discount_range_in_quote

    # Alliance (360131) and ACMH (390163), 7.9: "a reduction of 55% to 75%" became a 55% tier and
    # "a discount of at least 25%" a 25% tier. The caster rejects a range in the value; the quote
    # had the range all along.
    alliance = (
        "Patients that have a household income between 100% and 400% of the Federal Poverty "
        "Guidelines may qualify for a reduction of 55% to 75% of total charges."
    )
    acmh = (
        "Generally, patients with family income of 200% of the Federal Poverty Level or less may "
        "be eligible for a discount of 100%. Patients with family income up to 400% of the Federal "
        "Poverty Level may be eligible for a discount of at least 25%."
    )
    assert discount_range_in_quote(alliance) == {Decimal(55), Decimal(75)}
    assert discount_range_in_quote(acmh) == {Decimal(25)}
    assert discount_range_in_quote("receive a 60% discount") == set()
    assert (
        discount_range_in_quote("Up to 200% of the Federal Poverty Guidelines 100% (free care)")
        == set()
    )
    assert discount_range_in_quote("a 30% to 50% discount off charges") == {
        Decimal(30),
        Decimal(50),
    }
    for quote, bands in ((alliance, [(100, 400, 55)]), (acmh, [(200, 400, 25)])):
        docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + quote + "\n"}
        report = verify_sheet(with_tiers_quote(quote, bands), docs)
        assert ("eligibility.discount_tiers", RANGE_REASON) in report.rejected
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + acmh + "\n"}
    report = verify_sheet(with_tiers_quote(acmh, [(200, 400, 40)]), docs)
    assert ("eligibility.discount_tiers", RANGE_REASON) not in report.rejected


def test_a_discount_ranging_from_one_percentage_to_another_is_a_range():
    from waive.atlas.verify import RANGE_REASON, discount_range_in_quote

    # Mercyhealth Crystal Lake (140308), 7.15: "a sliding scale discount ranging from 20% to 80%"
    # was published as a 20% band, because only "discount of 20% to 80%" read as a range.
    mercyhealth = (
        "Patients with household gross income above 250% but at or below 350% of the FPL will "
        "get a sliding scale discount ranging from 20% to 80%."
    )
    assert discount_range_in_quote(mercyhealth) == {Decimal(20), Decimal(80)}
    assert discount_range_in_quote("discounts from 25% to 100% of charges") == {
        Decimal(25),
        Decimal(100),
    }
    assert discount_range_in_quote("a discount that ranges from 10% to 90%") == {
        Decimal(10),
        Decimal(90),
    }
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + mercyhealth + "\n"}
    report = verify_sheet(with_tiers_quote(mercyhealth, [(250, 350, 20)]), docs)
    assert ("eligibility.discount_tiers", RANGE_REASON) in report.rejected


# (quote, stored tier percents, the stored percents read as a range endpoint). The tiers field is
# rejected exactly when the last is not empty.
_ONE_SIGN_AND_SCALE_RANGES = [
    # Hartford HealthCare (070011, 070017, 070021, 070035): one percent sign, after the second
    # number. The sheets stored a flat 75, or 15 for MidState (070017).
    (
        "(ii) If family income is verified between 250% and 550% of the Federal\nPoverty Level "
        "Guidelines, the patient will qualify for a 15-75%\ndiscount against the patient’s "
        "account balance.",
        (75,),
        (75,),
    ),
    (
        "(ii) If family income is verified between 250% and 550% of the Federal Poverty Level "
        "Guidelines, the patient will qualify for a 15-75% discount against the patient’s account "
        "balance.",
        (15,),
        (15,),
    ),
    # Holton (171319): a scale phrase between the discount word and the range.
    (
        "Patients with a household income of 100% – 200% of the FPL may qualify for a discount "
        "from a scale of 10% to 75%.",
        (75,),
        (75,),
    ),
    (
        "Patients with a household income of 100% – 200% of the FPL may qualify for a discount "
        "from a scale of 10% to 75%.",
        (10,),
        (10,),
    ),
    ("will qualify for a 15-75% discount", (40,), ()),  # not an endpoint
    ("a discount of 15-75% off the balance", (15,), (15,)),
    ("a discount on a sliding scale from 25% to 75%", (25,), (25,)),
    ("a discount based on a sliding scale of 20%-80%", (80,), (80,)),
    # Income bands and fixed discounts, which are no discount range.
    ("Patients 201-300% FPL receive a 75% discount", (75,), ()),
    ("201-300% discount 75%", (75,), ()),
    ("0-100% of federal poverty level 100% discount", (100,), ()),
    (
        "0-100% of federal poverty level 100% discount ii. 101% - 200% of federal poverty level "
        "75% discount",
        (75,),
        (),
    ),  # Day Kimball (070003)
    (
        "- 100% financial assistance for income levels at or below 250% - 95% financial "
        "assistance for income levels between 251% and 275%",
        (95,),
        (),
    ),  # HealthSource Saginaw (230275)
    ("receive a 60% discount", (60,), ()),
    ("FPL 0-100% 100% 101-140% 90% 141-160% 80% 161-180% 75%", (90, 80, 75), ()),
    ("a discount of 2023-24% applies", (24,), ()),
    # The ranges the existing patterns already read.
    ("may qualify for a reduction of 55% to 75% of total charges.", (55,), (55,)),  # Alliance
    ("may be eligible for a discount of at least 25%.", (25,), (25,)),  # ACMH
    ("a 30% to 50% discount off charges", (30,), (30,)),
    # Range wordings found in stored hospital documents.
    (
        "Patients ... between 201% and 500% of the federal poverty guidelines. The discount will "
        "be between 25% and 75%.",
        (75,),
        (75,),
    ),
    (
        "reduced-cost care (35%-75%) adjustment to the initial gross charges",
        (35,),
        (35,),
    ),  # Johns Hopkins Medicine
    (
        "other discounts ranging from 40 to 90 percent apply if the patient's total household "
        "income is over 200 percent",
        (90,),
        (90,),
    ),
    ("discounts off charges from 60 percent to 96 percent : any eligible patient", (60,), (60,)),
    (
        "receive adjustments ranging from 35-100% of the patient portion of the bill",
        (35,),
        (35,),
    ),  # Freeman
    (
        "the financial assistance adjustment can be from 20% to 100% of your patient balance",
        (20,),
        (20,),
    ),
    (
        "the amount of the discount varies between 25 and 100 percent of the patient's portion",
        (25,),
        (25,),
    ),
    (
        "generally receive a sliding scale discount ranging from 100 % to 60 % for emergency",
        (60,),
        (60,),
    ),
    ("will receive discounts ranging between 95% and 5%.", (95,), (95,)),
    ("discounts ranging from 100 percent to 60 percent", (60,), (60,)),
    ("a discount of 75 to 25 percent", (25,), ()),  # runs downward without "ranging"
    ("a 15 to 75 percent discount", (15,), (15,)),
    ("a 15 to 75 per cent discount", (15,), (15,)),
    # Lists, patient shares, tables and dollar amounts, which are no discount range.
    ("when 90% and 80% discounts apply: for uninsured patients", (90, 80), ()),
    (
        "a. up to $100 - 5% discount b. $101 to $999 - 10% discount c. $1,000 - 20% discount",
        (5, 10, 20),
        (),
    ),
    (
        "if patients on the 20% to 80% sliding fee scale are responsible for qualified "
        "out-of-pocket",
        (80, 60, 40, 20),
        (),
    ),
    (
        "medical financial hardship adjustment medical bill as % of annual family income "
        "adjustment 20 - 29% 15% 30 - 39% 20%",
        (15, 20),
        (),
    ),
    (
        "a. patients that qualify for 0-20% will receive a 20% adjustment b. patients that "
        "qualify for 21-50% will receive a 50% adjustment.",
        (20, 50),
        (),
    ),
    (
        "total household income discount from gross charges less than 100% of fpl 100% discount "
        "between 101% - 150% of fpl 90% discount between 151% - 200% of fpl 75% discount",
        (90, 75),
        (),
    ),
    (
        "annual family income maximum discount 200% or less fpl 100% 201% - 250% 75% 251% - 300% "
        "50% 301% - 350% 24%",
        (75, 50, 24),
        (),
    ),
    ("fpl 0-50% 51-100% discount 100% 90%", (90,), ()),
    (
        "income (of fpg) % of poverty level uninsured charity discount insured charity discount "
        "0 - 100% 100% 100% 100% - 200% 100% 100% 200% - 300% 80%/agb 80%/agb",
        (80,),
        (),
    ),  # Atrium (360076)
    ("a 50% discount from 201-300% fpl", (50,), ()),
    # A numbered label, or an income band, before a dash and one discount.
    ("tier 2 - 75% discount", (75,), ()),
    ("level 2 - 75% discount", (75,), ()),
    ("category 3 - 40% discount", (40,), ()),
    ("section 4.2 - 50% discount", (50,), ()),
    ("0-50% fpl 51-75% discount", (75,), ()),
    ("0-50% 51-75% discount", (75,), ()),
    ("will qualify for a 15–75% discount", (75,), (75,)),  # an en dash, as Hartford's may be
]


@pytest.mark.parametrize(("quote", "stored", "expected"), _ONE_SIGN_AND_SCALE_RANGES)
def test_discount_range_in_quote_reads_one_sign_ranges_and_scale_phrases(quote, stored, expected):
    from waive.atlas.verify import discount_range_in_quote

    # Hartford HealthCare's "a 15-75% discount" and Holton's "a discount from a scale of 10% to
    # 75%" (task 7.17) were each published as one flat percent: the range was written with one
    # percent sign, or behind a scale phrase, and read as nothing.
    stored_percents = {Decimal(percent) for percent in stored}
    assert discount_range_in_quote(quote) & stored_percents == {Decimal(p) for p in expected}


def test_a_true_asset_test_is_not_supported_by_a_quote_that_rules_assets_out():
    from waive.atlas.verify import ASSET_REASON

    # Review of 7.9: the True branch only looked for a "tested" word, and "considered" is one, so
    # "Assets are not considered" verified asset_test=True as well as False.
    path = "eligibility.asset_test"
    assert (
        verdict(path, True, "Assets are not considered in determining eligibility.") == ASSET_REASON
    )
    assert (
        verdict(path, True, "The hospital does not consider assets when deciding eligibility.")
        == ASSET_REASON
    )
    assert (
        verdict(path, False, "Assets are not considered in determining eligibility.") == "accepted"
    )
    # A partial exemption still means the rest of the assets are looked at.
    assert verdict(path, True, ADVENTIST_ASSETS) == "accepted"


WAUCHULA_TABLE = (
    "Patients with household incomes that exceed two hundred fifty percent (250%) of the current "
    "Federal Poverty Guidelines but are less than four hundred one percent (401%) shall be granted "
    "the below discounts: Uninsured patients with household incomes between two hundred fifty-one "
    "percent (251%) and four hundred percent (400%) of Federal Poverty Guidelines would be granted "
    "a ninety-eight percent (98%) discount on applicable balances. Patients with household incomes "
    "that exceed four hundred one percent (401%) of the Federal Poverty Guidelines shall be granted "
    "the below discounts: For Illinois facilities - Uninsured patients with household incomes "
    "between four hundred one percent (401%) and six hundred percent (600%) of Federal Poverty "
    "Guidelines would be granted an eighty-five percent (85%) discount on applicable balances."
)


def test_a_band_stated_for_another_states_facilities_is_not_the_hospitals():
    from waive.atlas.verify import OTHER_STATE_REASON, bands_under_another_state

    # AdventHealth Wauchula, Florida (101300, review of 7.9): the 400-600% / 85% tier is granted
    # to Illinois facilities only, inside the system-wide policy.
    both = [
        DiscountTier(min_fpl_exclusive=250, max_fpl_inclusive=400, discount_percent=98),
        DiscountTier(min_fpl_exclusive=400, max_fpl_inclusive=600, discount_percent=85),
    ]
    assert bands_under_another_state(WAUCHULA_TABLE, "FL", both)
    assert not bands_under_another_state(WAUCHULA_TABLE, "IL", both)
    assert not bands_under_another_state(WAUCHULA_TABLE, "FL", both[:1])
    # The free-care limit too, and a quote without such a heading never trips.
    illinois_free = (
        "For Illinois facilities, patients at or below 300% of the Federal Poverty Guidelines "
        "receive a 100% reduction."
    )
    assert bands_under_another_state(illinois_free, "FL", Decimal(300))
    assert not bands_under_another_state(illinois_free, "IL", Decimal(300))
    assert not bands_under_another_state(
        "household income at or below 250% of the Federal Poverty Guidelines are eligible for free care",
        "FL",
        Decimal(250),
    )
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + WAUCHULA_TABLE + "\n"}
    sheet = st_example_sheet()  # a Massachusetts hospital
    cited = sheet.eligibility.discount_tiers.model_copy(
        update={"value": both, "quote": WAUCHULA_TABLE}
    )
    sheet = sheet.model_copy(
        update={"eligibility": sheet.eligibility.model_copy(update={"discount_tiers": cited})}
    )
    assert ("eligibility.discount_tiers", OTHER_STATE_REASON) in verify_sheet(sheet, docs).rejected


UMMS_FREE_CARE = (
    "Those with income up to 200% of the income eligibility limits established by the Maryland "
    "Department of Health are eligible for free care."
)
# (quote, value, whether the value is a percentage of a benchmark other than the poverty level)
_OTHER_BASIS_CASES = [
    # University of Maryland Medical System (task 7.17): Maryland's Medicaid limits, not the FPL.
    ("200% of the income eligibility limits established by the Maryland Department of Health", 200, True),
    (UMMS_FREE_CARE, 200, True),
    ("Free Care - " + UMMS_FREE_CARE, 200, True),
    ("Reduced Cost Care - Those between 200% and 300% of the income eligibility limits established "
     "by the Maryland Department of Health are eligible for discounts on a sliding scale", 300, True),
    ("Those between 200% and 300% of the income eligibility limits established by the Maryland "
     "Department of Health are eligible for discounts", 200, False),  # the next number comes first
    ("Those with income up to two hundred percent (200%) of the income eligibility limits "
     "established by the Maryland Department of Health", 200, True),
    ("Those with income up to 200% of the limits established by the Maryland Department of Health",
     200, True),
    # Other benchmarks, after "of" or right after the percent sign.
    ("Free care is available to households with income at or below 80% of the Area Median Income "
     "(AMI).", 80, True),
    ("Patients with income up to 250% of the State Median Income qualify for a full write-off.",
     250, True),
    ("Free care for income at or below 150% of the Medicaid income limit.", 150, True),
    ("Free care for incomes up to 200% of MDH limits.", 200, True),
    ("Free care: income at or below 200% of the Maryland Medicaid limit", 200, True),
    ("Free care: income at or below 200% MDH limits", 200, True),
    ("Free care for incomes up to 80% AMI", 80, True),
    # The benchmark must start within ten words after "of".
    ("Free care up to 200% of the levels the hospital sets each year by its income eligibility "
     "standards", 200, True),
    ("Free care up to 200% of the levels the hospital sets each year by its own income eligibility "
     "standards", 200, False),
    # The poverty level, whoever publishes it, and however far its words come after the number.
    ("200% of the Federal Poverty Level", 200, False),
    ("Patients with income up to 200% of the Federal Poverty Guidelines published by the "
     "Department of Health and Human Services receive free care.", 200, False),
    ("Household income must be at or below one-hundred percent (100%) of the poverty income "
     "guidelines established by the Department of Health and Human Services.", 100, False),
    ('Household income is less than or equal to 200% of the Federal Poverty Guidelines ("FPG") '
     "established by the U.S. Department of Health and Human Services.", 200, False),
    ("at or below 200 percent of the current Federal Poverty Guidelines as established by the "
     "United States Department of Health and Human Services (USDHHS).", 200, False),
    ("Patients with income at or below 200% of the guidelines set by the Department of Health and "
     "Human Services receive free care.", 200, False),
    ("Free care for income up to 200% of the guidelines issued by the U.S. Department of Health & "
     "Human Services.", 200, False),
    ("we may be able to write off as much as 100% of your bill, based on the U.S. Department of "
     "Health and Human Services poverty guidelines.", 100, False),  # UK King's Daughters (180009)
    ("free care up to 200% of the income eligibility limits (Federal Poverty Guidelines)", 200, False),
    # An abbreviation's period ends no sentence before the poverty words.
    ("Free care up to 200% of the income eligibility levels set by the U.S. Department of Health "
     "and Human Services poverty guidelines.", 200, False),
    ("Free care up to 200% of the income eligibility limits, i.e. the Federal Poverty Guidelines.",
     200, False),
    ("A 100% discount is available to individuals and families with annual incomes at or below "
     "200% of the FPG.", 200, False),
    ("the Medicaid income limit of 133% of the federal poverty guidelines", 133, False),
    ("free care at or below 250% of the State's poverty guidelines", 250, False),
    ("at or below 400% of the FPL; patients must first apply for Medicaid", 400, False),
    ("income up to 200% of the poverty guidelines; patients eligible under the Maryland "
     "Department of Health programs", 200, False),
    # Agencies that are not an income measure of their own: California's HCAI, New Jersey's
    # Health Care for the Uninsured.
    ("Free care for income up to 400% of the guidelines published by the California Department of "
     "Health Care Access and Information", 400, False),
    ("income up to 200% of the New Jersey Department of Health Care for the Uninsured Income "
     "Criteria", 200, False),
    # No benchmark named, or one only after the sentence ends.
    ("| less than or equal to 200% | 0%", 200, False),
    ("Up to 200% Eligible for Medicaid Rates", 200, False),
    ("Patients may be eligible for free care with income up to 100% of the income limits shown in "
     "Attachment A.", 100, False),
    ("Uninsured patients pay 100% of the Medicaid rate.", 100, False),
    ("Medical debt that exceeds 25% of that family's annual income. MDH Limits refers to the "
     "income eligibility limits established by the Maryland Department of Health.", 25, False),
]  # fmt: skip


@pytest.mark.parametrize(("quote", "value", "expected"), _OTHER_BASIS_CASES)
def test_percent_of_other_basis(quote, value, expected):
    from waive.atlas.verify import percent_of_other_basis

    assert percent_of_other_basis(quote, Decimal(value)) is expected


def test_verify_sheet_rejects_an_income_limit_on_another_benchmark():
    from waive.atlas.verify import OTHER_BASIS_REASON

    # UM Capital Region (210003), UM Baltimore Washington (210043), UM Upper Chesapeake (210049)
    # and UM Rehabilitation & Orthopaedic Institute (210058) published free care up to 200% FPL
    # from this sentence (task 7.17).
    free, tiers = "eligibility.free_care_max_fpl", "eligibility.discount_tiers"
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + UMMS_FREE_CARE + "\n"}
    assert (free, OTHER_BASIS_REASON) in verify_sheet(
        with_free_limit(200, UMMS_FREE_CARE), docs
    ).rejected
    # A tier's top or its bottom on such a benchmark is rejected the same way.
    for quote, band in (
        (
            "Those with income between 100% and 200% of the income eligibility limits established "
            "by the Maryland Department of Health receive a 75% discount.",
            (100, 200, 75),
        ),
        (
            "Those above 200% of the income eligibility limits established by the Maryland "
            "Department of Health and up to 300% of the Federal Poverty Level receive a 50% "
            "discount.",
            (200, 300, 50),
        ),
    ):
        docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + quote + "\n"}
        assert (tiers, OTHER_BASIS_REASON) in verify_sheet(
            with_tiers_quote(quote, [band]), docs
        ).rejected, quote
    # The poverty level as its publisher names it stays a free-care limit.
    fpl = (
        "Patients with income up to 200% of the Federal Poverty Guidelines published by the "
        "Department of Health and Human Services receive free care."
    )
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + fpl + "\n"}
    assert not any(p == free for p, _ in verify_sheet(with_free_limit(200, fpl), docs).rejected)


MEDSTAR_RESIDENCY = (
    "All patients must provide proof of residency within Maryland, northern Virginia, or within "
    "the District of Columbia."
)
MEDSTAR_PROOF = (
    "Proof of residency documentation would include gas and electric bills, pay stubs, bank "
    "statements, rent statements, etc. Patient must first apply for Medical Assistance, Medical "
    "Assistance Emergency Services, and other coverage program(s) eligibility."
)
# (stored states, quote, None when the quote names each one as a whole state, "missing" when it
# does not, "both" when it says residents of other states qualify too)
_RESIDENCY_CASES = [
    # MedStar §3.1.1 (210015, 210018, 210024, 210034, 210062, task 7.17): only northern Virginia.
    (["MD", "VA", "DC"], MEDSTAR_RESIDENCY, "missing"),
    (["MD", "DC"], MEDSTAR_RESIDENCY, None),
    (["MD", "DC", "VA"], "for patients who reside within Maryland, the District of \nColumbia, or "
     "northern Virginia.", "missing"),
    (["MD", "VA", "DC"], MEDSTAR_PROOF, "missing"),  # names no state at all
    (["MD"], "Patients must provide proof of residency to qualify.", "missing"),
    # A region word before the name: a part of the state.
    (["ND", "MT"], "residing in North Dakota or Northeastern Montana and have received care at a "
     "Trinity Health facility", "missing"),
    (["WV"], "Patients who reside in southern West Virginia are eligible.", "missing"),
    (["NC"], "residents of western North Carolina", "missing"),
    (["NY"], "residents of Western New York", "missing"),
    (["TX"], "residents of Central Texas counties", "missing"),
    (["TN"], "“Community” means the twelve (13) Counties of Middle Tennessee which include: "
     "Cannon, Davidson", "missing"),
    # Whole states, longest name first: West Virginia is not Virginia.
    (["NC", "SC", "GA", "AL"], "Uninsured patients who reside in North Carolina, South Carolina, "
     "Georgia, or Alabama", None),
    (["WV"], "Patients who reside in West Virginia are eligible.", None),
    (["VA"], "Patients who reside in West Virginia are eligible.", "missing"),
    (["VA", "WV"], "Financial assistance is open to residents of Virginia or West Virginia.", None),
    (["NY"], "residents of New York State", None),
    (["TX"], "residents of the State of Texas", None),
    (["CA"], "Was the patient a resident of California at the time of service?", None),
    (["MN"], "who are residents of Minnesota", None),
    (["MO", "KS"], "residents of Missouri and Kansas", None),
    (["OR", "WA"], "Financial assistance is offered to eligible residents of Oregon or Washington.",
     None),
    (["IL"], "Illinois Resident means a Patient who lives in Illinois and intends to continue "
     "living in Illinois indefinitely.", None),
    (["IL"], "Illinois residents. Hospital charges", None),
    (["NJ"], "Proof of New Jersey residence as of the date of service", None),
    (["MI", "OH"], "Having a personal residence within the state of Michigan or Ohio, within "
     "approximately 25 miles of a McLaren HealthCare facility.", None),
    (["KY"], "Service area includes the State of Kentucky. CCMH reserves the right to add to or "
     "subtract from the list in its service area.", None),
    (["MD"], "All residents of Meritus’ service area, and all Maryland residents, will be "
     "considered for financial assistance", None),
    # "non-New Jersey" still names New Jersey (RWJBarnabas, 310075, 310041).
    (["NJ"], "Charity Care may be available to non-New Jersey residents, requiring immediate "
     "medical attention", None),
    (["NJ"], "Residency Criteria Charity Care may be available to non-New Jersey residents, "
     "requiring immediate medical attention for an emergency medical condition.", None),
    # A footnote mark glued to the name does not hide it (fap-d9c5de9c9fa6dd89).
    (["IL", "WI", "NC", "SC", "GA", "AL"], "Uninsured patients who reside in Illinois, Wisconsin1, "
     "North Carolina, South Carolina, Georgia or Alabama.", None),
    # Paired North and South name both.
    (["NC", "SC"], "residents of North and South Carolina", None),
    (["ND", "SD"], "residents of North or South Dakota", None),
    # A place word after the name: a county, a city, a beach, a region, a campus, a facility.
    (["WA"], "Patients who live in Washington County are eligible for assistance.", "missing"),
    (["KS"], "Patients who live in Kansas City are eligible for assistance.", "missing"),
    (["VA"], "residents of the City of Virginia Beach", "missing"),
    (["LA"], "Verification of Louisiana Region 5 Residency is required for any financial "
     "assistance request.", "missing"),
    (["CA"], "have established residency within the CommonSpirit Entity Service Area as defined "
     "by the most recent Hospital Facility CHNA, unless the visit is urgent or emergent or occurs "
     "within a California Hospital Facility", "missing"),
    (["WI"], "“Community” means any county in Wisconsin that has an Ascension WI facility or "
     "clinic", "missing"),
    # ... and a place word before it.
    (["MO"], "You live in Kansas City, Missouri, or Jackson County, Missouri", "missing"),
    (["MO"], "The applicant must currently reside, with the intent to stay, in the City of Kansas "
     "City, Missouri or Jackson County, Missouri.", "missing"),
    (["GA"], "reside in Grady, Mitchell, Brooks or Thomas County, Georgia, or reside in a county "
     "in Georgia which does not include a hospital", "missing"),
    (["NE"], "for patients residing in Richardson County, Nebraska and for patients with "
     "established long term primary care", "missing"),
    (["WI"], "Services provided at all Vernon Health, Inc. Hospital and Clinics located in Vernon "
     "and Crawford Counties, Wisconsin.", "missing"),
    (["OR", "WA"], "You must be a resident of the state of Oregon or bordering county in the state "
     "of Washington (Benton, Clark)", "missing"),
    (["OR"], "You must be a resident of the state of Oregon or bordering county in the state of "
     "Washington (Benton, Clark)", None),
    (["VT", "NH"], "the patient/guarantor must be a resident of the State of Vermont, or Sullivan "
     "or Cheshire Counties in New Hampshire.", "missing"),
    (["DE", "PA", "MD", "NJ"], "The Financial Assistance Policy applies to residents living "
     "within ChristianaCare’s primary service areas in Delaware, Pennsylvania, Maryland and New "
     "Jersey", "missing"),
    # A beach, a region or a panhandle before the name locates a town or a part of the state.
    (["VA"], "residents of the City of Virginia Beach, Virginia", "missing"),
    (["FL"], "Patients who live in Palm Beach, Florida are eligible.", "missing"),
    (["CA"], "Patients who live in Long Beach, California are eligible.", "missing"),
    (["WV"], "residents of the northern panhandle of West Virginia", "missing"),
    (["OH"], "patients who live in our service region in Ohio", "missing"),
    (["FL"], "Florida residents, including those in Palm Beach, Florida, qualify.", None),
    (["NC"], "Care at a facility in North Carolina is open to North Carolina residents.", None),
    # An empty list would turn every household away.
    ([], "Patients who reside in Maryland are eligible.", "missing"),
    # One whole mention is enough, whatever place names follow (Phoebe Putney's "which county").
    (["GA"], "Georgia residents who are existing patients of PPG physicians will be deemed to have "
     "met the residency requirement regardless of which county in Georgia they currently reside.",
     None),
    (["KS"], "Residents of Kansas, including Kansas City, qualify for assistance.", None),
    (["VA"], "Virginia residents, including residents of Virginia Beach, qualify.", None),
    (["WA"], "Washington residents, including those in Washington County, qualify.", None),
    # Washington, D.C. is the District, not Washington; a county's own state in parentheses.
    (["DC"], "residents of Washington, D.C. and its suburbs", None),
    (["WA"], "residents of Washington, D.C. and its suburbs", "missing"),
    (["DC"], "residents of Washington, DC and its suburbs", None),
    (["MD", "DC", "VA"], "Residents of Maryland, DC or Virginia qualify for assistance.", None),
    (["VT", "WA", "NY", "MA"], "be a Vermont resident as defined in the \"Definitions\" section of "
     "this policy or a resident of the following counties located outside of Vermont Rensselaer "
     "(NY), Washington (NY) and Berkshire (MA).", "missing"),
    (["VT"], "be a Vermont resident as defined in the \"Definitions\" section of this policy or a "
     "resident of the following counties located outside of Vermont Rensselaer (NY), Washington "
     "(NY) and Berkshire (MA).", None),
    (["KS", "MO"], "residents of Kansas (KS) and Missouri (MO)", None),
    (["VT"], "Vermont residents must sign the Declaration of Residency (VT)", None),
    # Postal codes count only as upper-case tokens in mixed-case text, with the same guards.
    (["NJ"], "Must be a resident of the State of NJ or the crime had to occur in this State.", None),
    (["IN"], "Patients must live IN the service area.", "missing"),
    (["MD"], "patient must reside in Cecil County, MD or one of the following surrounding "
     "counties to be eligible for charity:", "missing"),
    (["SC"], "Horry County, Lancaster County, and York County, SC.", "missing"),
    (["NH"], "Charity care is only available to residents of Southern NH Health’s primary and "
     "secondary service areas.", "missing"),
    (["NH", "VT"], "Live within UCVH’s catchment area which includes all of NH and VT and Oxford "
     "County in ME.", None),
    (["ME", "NH", "VT"], "The patient lives within our catchment area which includes all of NH "
     "and VT and Oxford County in Maine.", "missing"),
    # Residents of other states qualify too, so a list of states would turn them away (ECU, Novant).
    (["MN"], "Minnesota residents and non-Minnesota residents are eligible to apply.", "both"),
    (["ME"], "Financial assistance for Emergency Care is available to Maine Residents and "
     "non-Maine residents", "both"),
    (["NC"], "Additionally, this policy applies to North Carolina and non-North Carolina residents "
     "in accordance with their state's FPL", "both"),
    (["NC"], "All North Carolina residents and non-North Carolina residents within a Novant "
     "Health Service Area (see attached) are eligible to apply", "both"),
    (["NC"], "The applicant must be a North Carolina resident (the criteria for determining "
     "residency is attached as Appendix A to this policy);", None),
]  # fmt: skip


@pytest.mark.parametrize(("value", "quote", "expected"), _RESIDENCY_CASES)
def test_residency_problem(value, quote, expected):
    from waive.atlas.verify import RESIDENCY_BOTH_REASON, RESIDENCY_REASON, residency_problem

    reasons = {None: None, "missing": RESIDENCY_REASON, "both": RESIDENCY_BOTH_REASON}
    assert residency_problem(value, quote) == reasons[expected]


def test_residency_problem_reads_state_codes_in_any_case():
    from waive.atlas.verify import RESIDENCY_REASON, residency_problem

    assert residency_problem(["md", "dc"], MEDSTAR_RESIDENCY) is None
    assert residency_problem(["md", "va", "dc"], MEDSTAR_RESIDENCY) == RESIDENCY_REASON


def test_verify_sheet_rejects_residency_the_quote_does_not_name_as_whole_states():
    from waive.atlas.verify import RESIDENCY_BOTH_REASON, RESIDENCY_REASON, prune_lists

    path = "eligibility.residency"
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + MEDSTAR_RESIDENCY + "\n"}
    # MedStar Southern Maryland (210062) published [MD, VA, DC]; Richmond and Norfolk residents
    # passed the residency step under the bare VA.
    sheet = with_list(path, ["MD", "VA", "DC"], MEDSTAR_RESIDENCY)
    report = verify_sheet(sheet, docs)
    assert report.rejected == [(path, RESIDENCY_REASON)]
    # The field is rejected, never pruned to [MD, DC]: that would turn northern Virginia away.
    pruned, notes = prune_lists(sheet, docs)
    assert pruned.eligibility.residency.value == ["MD", "VA", "DC"] and notes == []
    assert verify_sheet(with_list(path, ["MD", "DC"], MEDSTAR_RESIDENCY), docs).ok
    both = "North Carolina residents and non-North Carolina residents are eligible to apply."
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + both + "\n"}
    assert verify_sheet(with_list(path, ["NC"], both), docs).rejected == [
        (path, RESIDENCY_BOTH_REASON)
    ]


def with_list(path, value, quote):
    section_name, field_name = path.split(".")
    sheet = st_example_sheet()
    cited = Cited(
        value=value, quote=quote, source_id=SAMPLE_SOURCE_ID, checked_on=date(2026, 10, 2)
    )
    section = getattr(sheet, section_name).model_copy(update={field_name: cited})
    return sheet.model_copy(update={section_name: section})


def test_a_list_field_must_be_named_in_full_by_its_stored_quote():
    from waive.atlas.schema import DocType, SubmitMethod
    from waive.atlas.verify import LIST_REASON, prune_lists

    # Review of 7.9: the structurer's list filters ran on the model's untrimmed quote; after
    # trim_quotes the stored sentence named fewer items and nothing checked again (140202 kept
    # photo_id and bank_statements under a sentence about W-2s and pay stubs).
    sentence = "Applicants must provide a photo ID and one proof of income."
    sheet = with_list(
        "apply.documents_required",
        [DocType.PHOTO_ID, DocType.PROOF_OF_INCOME, DocType.BANK_STATEMENTS],
        sentence,
    )
    assert ("apply.documents_required", LIST_REASON) in verify_sheet(sheet, DOCS).rejected
    pruned, notes = prune_lists(sheet, DOCS)
    assert [k.value for k in pruned.apply.documents_required.value] == [
        "photo_id",
        "proof_of_income",
    ]
    assert notes == ["apply.documents_required: 1 item not named in the trimmed quote"]
    assert verify_sheet(pruned, DOCS).ok
    # A list the quote names nothing of goes, with the filter's own words.
    sheet = with_list(
        "apply.submit_methods",
        [SubmitMethod(kind="mail", detail=""), SubmitMethod(kind="fax", detail="")],
        "Questions: call 617-555-0100.",
    )
    assert (
        "apply.submit_methods",
        "none of the listed channels is named in the quote",
    ) in verify_sheet(sheet, DOCS).rejected
    pruned, notes = prune_lists(sheet, DOCS)
    assert pruned.apply.submit_methods is None
    assert notes == [
        "apply.submit_methods: none of the listed channels is named in the quote (after trimming)"
    ]
    sheet = with_list(
        "programs.presumptive",
        ["Medicaid"],
        "Applicants must provide a photo ID and one proof of income.",
    )
    assert (
        "programs.presumptive",
        "quote does not describe presumptive (automatic) eligibility",
    ) in verify_sheet(sheet, DOCS).rejected
    # The sample sheet's own lists stand on their quotes.
    assert verify_sheet(st_example_sheet(), DOCS).ok


def test_free_care_wording_next_to_partial_is_not_a_ceiling():
    from waive.atlas.verify import quotes_assistance_ceiling

    # Review of 7.9: a bare "partial" or "eligible for assistance" classed these as ceilings and
    # held sheets whose free limit was right.
    assert not quotes_assistance_ceiling(
        "Households at or below 200% FPL qualify for full charity care; 201-400% receive partial "
        "assistance"
    )
    assert not quotes_assistance_ceiling(
        "Patients at or below 250% FPL are eligible for financial assistance covering the full "
        "cost of care"
    )
    assert not quotes_assistance_ceiling(
        "Free hospital care is available to patients at or below 200 percent of the poverty level"
    )
    # The Arnot wording is still a ceiling, and so is a bare promise of some help.
    assert quotes_assistance_ceiling(ARNOT_SENTENCE)
    assert quotes_assistance_ceiling(
        "Patients up to 400% FPL are eligible for financial assistance under this policy."
    )
    assert quotes_assistance_ceiling(
        "Patients up to 400% FPL receive partial financial assistance."
    )


def test_free_care_limits_a_document_states():
    from waive.atlas.verify import free_care_limits_stated

    # AHMC Anaheim (050226, review of 7.9): the web page and the newer PDF disagree.
    assert free_care_limits_stated(
        "Patients whose family income is at or below 200 percent of the Federal Poverty Level "
        "will be eligible for a 100 percent write-off. Patients between 201 and 400 percent "
        "receive a discount."
    ) == {Decimal(200)}
    assert free_care_limits_stated(
        "No-Cost (charity care): Uninsured and underinsured patients whose family gross income "
        "is between 0% and 250% of FPL are eligible for charity care (i.e., free care)."
    ) == {Decimal(250)}
    assert free_care_limits_stated(SAMPLE_POLICY_TEXT) == {Decimal(250)}
    # Sentences about discounts, caps or programs name no free-care limit.
    assert (
        free_care_limits_stated(
            "Patients at or below 400% FPL will not be charged more than 100% of the amounts "
            "generally billed. Households up to 300% of FPL may receive help from the Health Safety Net."
        )
        == set()
    )
    assert free_care_limits_stated("Free care is available; ask a financial counselor.") == set()


# CHRISTUS Spohn (450046, national batch 2): the limit sits in the sentence before the one that
# grants the discount, and the model quoted only the second.
SPOHN = (
    "Charity care is for patients who do not qualify for state or federal assistance. In most "
    "cases, this will apply to patients who fall between 0 - 200% of the Federal Poverty Level. "
    "Federal Poverty Levels based on total household income, with sufficient supporting "
    "documentation provided by the patient, will have a 100% Charity discount processed. "
    "Our billing office is at 300 Main Street."
)
SPOHN_GRANT = (
    "Federal Poverty Levels based on total household income, with sufficient supporting "
    "documentation provided by the patient, will have a 100% Charity discount processed."
)


def with_free_limit(value, quote):
    sheet = st_example_sheet()
    cited = sheet.eligibility.free_care_max_fpl.model_copy(
        update={"value": Decimal(value), "quote": quote}
    )
    return sheet.model_copy(
        update={"eligibility": sheet.eligibility.model_copy(update={"free_care_max_fpl": cited})}
    )


def test_a_free_care_quote_is_widened_to_the_neighbouring_sentence_that_states_the_limit():
    from waive.atlas.verify import trim_quotes, widened_span

    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + SPOHN}
    free = "eligibility.free_care_max_fpl"
    sheet = with_free_limit(200, SPOHN_GRANT)
    assert (free, "value not in quote") in verify_sheet(sheet, docs).rejected
    trimmed = trim_quotes(sheet, docs)
    quote = trimmed.eligibility.free_care_max_fpl.quote
    assert quote.startswith("In most cases, this will apply to patients who fall between 0 - 200%")
    assert quote.endswith("will have a 100% Charity discount processed.")
    assert quote_found(quote, docs[SAMPLE_SOURCE_ID])  # verbatim source text
    assert all(path != free for path, _ in verify_sheet(trimmed, docs).rejected)
    # A bare number next door is not the limit: 300 is a street address here.
    assert widened_span(SPOHN_GRANT, docs[SAMPLE_SOURCE_ID], Decimal(300)) is None
    held = trim_quotes(with_free_limit(300, SPOHN_GRANT), docs)
    assert held.eligibility.free_care_max_fpl.quote == SPOHN_GRANT
    assert (free, "value not in quote") in verify_sheet(held, docs).rejected
    # Nothing to widen when the quote is not the source's.
    assert (
        widened_span("Entirely invented words here.", docs[SAMPLE_SOURCE_ID], Decimal(200)) is None
    )


def test_a_stitched_quote_keeps_the_sentence_that_carries_the_value():
    # Blessing Hospital (140015): the model stitched two real sentences in the wrong order; the
    # longer one is about prescriptions, the shorter one states the 275% limit.
    limit = (
        "Full financial assistance is granted when the patient has reported income below 275% "
        "of the Federal Poverty Income Guidelines."
    )
    pharmacy = (
        "Patients may receive 340b medication assistance resulting in no cost prescriptions at "
        "owned retail pharmacies after presenting an approved card."
    )
    source = f"{pharmacy} {limit}"
    stitched = f"{limit} {pharmacy}"
    assert not quote_found(stitched, source)
    assert matched_span(stitched, source) == pharmacy  # the longest, as before
    assert matched_span(stitched, source, Decimal(275)) == limit
    # No sentence carries the value: the longest real one is kept, and verification rejects it.
    assert matched_span(stitched, source, Decimal(400)) == pharmacy


# Blessing Hospital (140015): the model gave the right limit and quoted the definition of
# "financially indigent"; the limit stands two paragraphs on, under a heading.
BLESSING = (
    "Financially indigent patients are eligible for a 100% discount unless they qualify for "
    "catastrophic assistance. Payment plans are available.\n"
    "1. Full Financial Assistance a. Patient has reported income below 275% of the Federal "
    "Poverty Income Guidelines at any Blessing location. b. Patients may receive medication "
    "assistance.\n"
    "2. Catastrophic assistance: income greater than 275% of the Federal Poverty Guidelines and "
    "medical bills above 20% of income."
)
INDIGENT = (
    "Financially indigent patients are eligible for a 100% discount unless they qualify for "
    "catastrophic assistance."
)


def test_a_free_care_limit_is_re_anchored_on_the_one_passage_that_states_it():
    from waive.atlas.verify import anchored_span, trim_quotes

    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + BLESSING}
    free = "eligibility.free_care_max_fpl"
    sheet = with_free_limit(275, INDIGENT)
    assert (free, "value not in quote") in verify_sheet(sheet, docs).rejected
    trimmed = trim_quotes(sheet, docs)
    assert trimmed.eligibility.free_care_max_fpl.quote == (
        "Full Financial Assistance a. Patient has reported income below 275% of the Federal "
        "Poverty Income Guidelines at any Blessing location."
    )
    assert all(path != free for path, _ in verify_sheet(trimmed, docs).rejected)
    # A limit the document does not state is not anchored anywhere.
    assert anchored_span(docs[SAMPLE_SOURCE_ID], Decimal(300)) is None
    unchanged = trim_quotes(with_free_limit(300, INDIGENT), docs)
    assert unchanged.eligibility.free_care_max_fpl.quote == INDIGENT
    # A flattened table row names several percentages: which one is free care is not ours to say.
    row = (
        "CFAP Program Guidelines Federal Poverty Level 200% 201 - 300% 400% Carle Financial "
        "Assistance Program 100% Discount 50% Discount."
    )
    assert anchored_span(row, Decimal(200)) is None
    # A paid band is not free care, even next to the words "100% and".
    band = "Patients between 100% and 275% of the Federal Poverty Level receive a 60% discount."
    assert anchored_span(band, Decimal(275)) is None
    # An invented quote is replaced too when the document states exactly this limit.
    invented = trim_quotes(with_free_limit(275, "Entirely invented words here."), docs)
    assert invented.eligibility.free_care_max_fpl.quote.startswith("Full Financial Assistance a.")


def test_free_care_at_or_below_the_poverty_guidelines_is_a_limit_of_100_percent():
    # Alliance Community Hospital (360131): Ohio's HCAP band, written without its number.
    from waive.atlas.verify import states_the_poverty_line

    quote = (
        "Individuals are eligible for medically necessary health care at no cost if their family "
        "does not exceed the Federal Poverty Income Guidelines."
    )
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + quote}
    free = "eligibility.free_care_max_fpl"
    assert states_the_poverty_line(quote)
    assert all(path != free for path, _ in verify_sheet(with_free_limit(100, quote), docs).rejected)
    assert (free, "value not in quote") in verify_sheet(with_free_limit(200, quote), docs).rejected
    # A percentage of the guidelines is not the poverty line itself, and "reduced cost" is not free.
    assert not states_the_poverty_line(
        "Care is provided at no cost if income does not exceed 250% of the Federal Poverty Level."
    )
    assert not states_the_poverty_line(
        "Care is provided at a reduced cost if income does not exceed the Federal Poverty Level."
    )


# Task 7.15a: published free-care limits of 100 that were discounts, not incomes.
ONEIDA = "Sliding Scale 100% 100% 89% 89% 79% 79% 69% 69% 59% 59% 49% 49% 0%"
HOLZER = (
    "> Number of Household Members Account Balance Adjusted 100% Account Balance becomes 25% of "
    "total charges."
)
GRADY = (
    "% Federal Poverty Guidelines Discount of OP Charges 150% 100% 151 - 199% 90% 200% 75% "
    "201 – 300% 55% 301 – 400% 35%"
)
# Memorial Hospital, Carthage (141305): "100% Assistance" is the column of free care, whose
# incomes ($22,590 for one person) are 150% of the guidelines.
CARTHAGE = (
    "The household income must be at or below the guidelines listed below: Size of Family FPL "
    "Guidelines 100% Assistance *No Patient Share Tier One *Patient Share is 25% Tier Two *Patient "
    "Share is 50% 1 $15,060 $22,590 $22,591 $30,120 $30,121 $45,180"
)


def test_a_free_care_limit_must_be_quoted_as_an_income_limit():
    from waive.atlas.verify import FREE_LIMIT_UNTIED_REASON, states_income_limit

    free = "eligibility.free_care_max_fpl"
    # Oneida (330115), Holzer (360054) and Grady (360210) published free care up to 100% of the
    # poverty level from a discount column; Carthage's 100 names the help, not the income.
    for quote in (ONEIDA, HOLZER, GRADY, CARTHAGE):
        assert not states_income_limit(quote, Decimal(100)), quote
        docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + quote + "\n"}
        assert (free, FREE_LIMIT_UNTIED_REASON) in verify_sheet(
            with_free_limit(100, quote), docs
        ).rejected
    # Adirondack Health (330079): "from 80% to 100% discounting" is a discount, not an income.
    assert not states_income_limit(
        "This program is based on Federal Poverty Guidelines, and allows from 80% to 100% "
        "discounting on services billed by Adirondack Health.",
        Decimal(100),
    )
    # Published limits that are incomes stay, however the policy writes them (7.15a: a rule that
    # wanted "% of the poverty level" spelled out would have dropped 69 sheets, most of them right).
    for quote, value in (
        ("Federal Poverty Guidelines Percentages Financial Assistance Discount From To 0% -200% "
         "100% Discount (Full)", 200),  # Kaiser
        ("Family Income % FPG Discount 0 to 150% 100%", 150),  # Massachusetts General
        ("| less than or equal to 200% | 0% |", 200),  # Hackensack
        ("Free Care: Insured Illinois Residents with Household Income of 250% or less of the then "
         "current Federal Poverty Guideline", 250),  # Northwestern
        ("Generally, patients with family income of 200% of the current year's federal poverty "
         "guidelines or less may be eligible for a discount of 100%", 200),  # Chan Soon-Shiong
        ("Federal Poverty Level ≤ 200%", 200),  # Carle BroMenn
        ("FPL LEVEL 200% 300% 400% 1 $30,120 $45,180 $60,240 Discount on Charges 100% 90% 80%",
         200),  # Cuba Memorial
        ("Patients with income less than or equal to 250 1% of the Federal Poverty Level income "
         '("FPL"), will be eligible for 100% charity care', 250),  # Ascension, a footnote 1
        ("100% – 200% 100% 100%", 200),  # Atrium
        ("0% - 350% 100% (Charity Care)", 350),  # Lower Bucks
        ("Less than 200%", 200),  # Lehigh Valley Hazleton
        ("Whose family incomes are at or below 200 percent of the most recent federal poverty "
         "level guidelines and", 200),  # Gateway
        ("Patients with an income up to 200% of the local poverty line - which equates to an "
         "annual income of $60,000 for a family of four - may qualify for free care", 200),  # Marietta
        ("Patients whose income does not exceed two hundred percent (200%) of the FPL receive "
         "free care.", 200),
    ):  # fmt: skip
        assert states_income_limit(quote, Decimal(value)), quote
    # A range names its end as the limit, never its start; a dollar figure is no percentage.
    assert states_income_limit("201 - 300% of the FPL: a 50% discount", Decimal(300))
    assert not states_income_limit("201 - 300% of the FPL: a 50% discount", Decimal(201))
    assert not states_income_limit(
        "Patients between 100% and 200% of the FPL receive a 50% discount", Decimal(100)
    )
    assert not states_income_limit("income less than $200 a week", Decimal(200))
    # Free care up to the poverty line itself, written without its number, still verifies.
    alliance = (
        "Individuals are eligible for medically necessary health care at no cost if their family "
        "does not exceed the Federal Poverty Income Guidelines."
    )
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + alliance + "\n"}
    assert all(
        path != free for path, _ in verify_sheet(with_free_limit(100, alliance), docs).rejected
    )


def test_a_discount_of_100_beside_an_income_limit_is_no_free_care_limit_of_100():
    from waive.atlas.verify import states_income_limit

    # Review of 7.15b: in 173 passages of stored documents a 100 that is the discount verified as
    # a free-care limit of 100, beside the real limit or after poverty words that head a column.
    for quote in (
        "Patients at or below 200% of the FPL receive 100% charity care.",
        "Patients with income at or below 300% of the FPG will receive 100% financial assistance.",
        "Household income below 190% of the FPG qualifies for a 100% charity discount.",
        "Income Level (% of Federal Poverty Level) Discount ≤200% 100% 201-300% 75%",
        "0–250% FPL 100% 251–400% FPL 98%",
        "Up to 200% FPL = 100% Charity Write Off",
        "We cover 100% at 200% FPL.",
    ):
        assert not states_income_limit(quote, Decimal(100)), quote
    # The band's top, followed by the next band's start, is its limit: Grady's own table states
    # free care to 150% (360210), and "of poverty" names the poverty level.
    assert states_income_limit(GRADY, Decimal(150))
    assert not states_income_limit(GRADY, Decimal(100))
    assert states_income_limit(
        "Patients with household income at 200% of poverty qualify for free care.", Decimal(200)
    )
    # A range's end still reads as its limit, help word and all ("0-200% Free Care").
    assert states_income_limit("FPL 0-200% Free Care 201-300% 50% discount", Decimal(200))
    # Poverty words that head a column still count; after a verb or a benefit they do not.
    assert states_income_limit("FPL LEVEL 200% 300% 400%", Decimal(200))
    assert not states_income_limit("Federal Poverty Level Discount 100% 75% 50%", Decimal(100))


def test_trimming_never_widens_a_free_care_quote_to_a_passage_the_verifier_rejects():
    from waive.atlas.verify import FREE_LIMIT_UNTIED_REASON, trim_quotes

    free = "eligibility.free_care_max_fpl"
    # The neighbouring line names the poverty level, but not the 100 as an income: widening onto
    # it would publish a span the income-limit rule rejects.
    document = f"{ONEIDA}\nDiscounts follow the Federal Poverty Guidelines.\n"
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + document}
    trimmed = trim_quotes(with_free_limit(100, ONEIDA), docs)
    assert trimmed.eligibility.free_care_max_fpl.quote == ONEIDA
    assert (free, FREE_LIMIT_UNTIED_REASON) in verify_sheet(trimmed, docs).rejected
    # A neighbour that states the limit as an income is the span to keep.
    document = (
        f"{ONEIDA}\nPatients at or below 100% of the Federal Poverty Level receive free care.\n"
    )
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + document}
    trimmed = trim_quotes(with_free_limit(100, ONEIDA), docs)
    assert trimmed.eligibility.free_care_max_fpl.quote.endswith("receive free care.")
    assert all(path != free for path, _ in verify_sheet(trimmed, docs).rejected)


def test_a_field_quoted_from_a_document_the_caller_sets_aside_is_rejected_with_its_reason():
    from waive.atlas.verify import DENTAL_REASON, OTHER_REGION_REASON, TRANSLATION_REASON

    # Review of 7.15b: the pipeline names the documents that are translations while an English
    # document states the income rules; every field quoted from one is rejected. Review of 7.16:
    # so is every field quoted from a dental program's document or another Kaiser region's.
    sheet = st_example_sheet()
    for reason in (TRANSLATION_REASON, DENTAL_REASON, OTHER_REGION_REASON):
        report = verify_sheet(sheet, DOCS, {SAMPLE_SOURCE_ID: reason})
        assert {rejected for _, rejected in report.rejected} == {reason}
        assert len(report.rejected) == len(sheet.field_paths())
    assert verify_sheet(sheet, DOCS).ok
    assert verify_sheet(sheet, DOCS, {"fap-elsewhere": DENTAL_REASON}).ok
    # A quote its document does not contain is reported as that first.
    report = verify_sheet(
        sheet, {SAMPLE_SOURCE_ID: "Nothing here."}, {SAMPLE_SOURCE_ID: DENTAL_REASON}
    )
    assert {rejected for _, rejected in report.rejected} == {"quote not found in source"}


def test_a_stitched_free_care_quote_keeps_the_sentence_that_states_the_limit():
    from waive.atlas.verify import trim_quotes

    # Review of 7.15b: of a stitched quote's real sentences, the longest carrying the value was
    # kept even when only a shorter one states it as an income limit.
    limit = "Patients with income below 275% of the Federal Poverty Guidelines pay nothing."
    dollars = (
        "Balances of more than 275 dollars may be paid in monthly installments through our "
        "business office."
    )
    document = f"{dollars}\nOur office is open on weekdays.\nParking is free.\n{limit}\n"
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + document}
    trimmed = trim_quotes(with_free_limit(275, f"{limit} {dollars}"), docs)
    assert trimmed.eligibility.free_care_max_fpl.quote == limit
    free = "eligibility.free_care_max_fpl"
    assert all(path != free for path, _ in verify_sheet(trimmed, docs).rejected)


def test_a_discount_percentage_is_not_a_free_care_limit_of_the_poverty_level():
    # Adirondack Health (330079): its FAQ held the sheet as a "conflict" with the real 200% limit.
    from waive.atlas.verify import free_care_limits_stated

    faq = (
        "This program is based on Federal Poverty Guidelines, and allows from 80% to 100% "
        "discounting on services billed by Adirondack Health."
    )
    assert free_care_limits_stated(faq) == set()
    assert free_care_limits_stated(
        "Patients at or below 200 percent of the Federal Poverty Level receive free care."
    ) == {Decimal(200)}
    assert free_care_limits_stated(
        "Free care for household income between 0 - 250% FPL; a 100% discount applies."
    ) == {Decimal(250)}


# Decision 7.9a-2 (task 7.14): a policy that states only the income up to which any help is given.
CEILING_SENTENCE = (
    "Patients with household income up to 300% of the Federal Poverty Guidelines may qualify for "
    "financial assistance."
)


def with_ceiling(value, quote):
    """St. Example as a hospital whose documents state only the ceiling on any help."""
    sheet = st_example_sheet()
    cited = Cited[Decimal](
        value=Decimal(value), quote=quote, source_id=SAMPLE_SOURCE_ID, checked_on=date(2026, 10, 2)
    )
    eligibility = sheet.eligibility.model_copy(
        update={"free_care_max_fpl": None, "discount_tiers": None, "assistance_ceiling_fpl": cited}
    )
    return sheet.model_copy(update={"eligibility": eligibility})


def test_an_assistance_ceiling_is_a_percentage_the_quote_ties_to_the_poverty_level():
    from waive.atlas.verify import names_percent_of_poverty_level

    assert names_percent_of_poverty_level(CEILING_SENTENCE, Decimal(300))
    assert names_percent_of_poverty_level(ARNOT_SENTENCE, Decimal(400))
    assert names_percent_of_poverty_level(
        "household income, less than 600% of the federal poverty guidelines.", Decimal(600)
    )
    assert names_percent_of_poverty_level("Discounts are limited to 300% FPG.", Decimal(300))
    assert names_percent_of_poverty_level(
        "help is available up to 250 percent of the current poverty level", Decimal(250)
    )
    assert names_percent_of_poverty_level("eligible up to 137.5% of the FPL", Decimal("137.5"))
    # Policies write the number out with the figure in brackets, and date the guidelines.
    assert names_percent_of_poverty_level(
        "incomes up to four hundred percent (400%) of the Federal Poverty Guidelines", Decimal(400)
    )
    assert names_percent_of_poverty_level(
        "income at or below 300% of the 2026 HHS Poverty Guidelines may qualify", Decimal(300)
    )
    assert names_percent_of_poverty_level(
        "up to 250% of the U.S. Federal Poverty Level", Decimal(250)
    )
    assert names_percent_of_poverty_level(
        "up to 250% of the 2026 U.S. HHS Poverty Guidelines", Decimal(250)
    )
    # The number alone, another number of the sentence, or a discount is not the ceiling.
    assert not names_percent_of_poverty_level(
        "Financial assistance is available for incomes up to 400%.", Decimal(400)
    )
    assert not names_percent_of_poverty_level(
        "Patients between 200% and 400% of the Federal Poverty Level receive help.", Decimal(200)
    )
    assert names_percent_of_poverty_level(
        "Patients between 200% and 400% of the Federal Poverty Level receive help.", Decimal(400)
    )
    assert not names_percent_of_poverty_level(
        "This program follows the Federal Poverty Guidelines and gives a 40% discount.",
        Decimal(40),
    )
    assert not names_percent_of_poverty_level(
        "Help is available up to four times the federal poverty level.", Decimal(400)
    )


def test_a_ceiling_verifies_against_its_source_like_any_documented_value():
    from waive.atlas.verify import CEILING_UNTIED_REASON, OTHER_STATE_REASON

    ceiling = "eligibility.assistance_ceiling_fpl"
    untied = "Financial assistance is available for household incomes up to 300%."
    illinois = (
        "For Illinois facilities, patients up to 300% of the Federal Poverty Guidelines may "
        "qualify for financial assistance."
    )
    docs = {SAMPLE_SOURCE_ID: "\n".join([SAMPLE_POLICY_TEXT, CEILING_SENTENCE, untied, illinois])}
    report = verify_sheet(with_ceiling(300, CEILING_SENTENCE), docs)
    assert report.ok, report.rejected
    assert ceiling in report.accepted
    # A ceiling whose quote does not name the poverty level is dropped (verify_sheet rejects it).
    assert (ceiling, CEILING_UNTIED_REASON) in verify_sheet(
        with_ceiling(300, untied), docs
    ).rejected
    # The exact-quote and value rules apply to it as to every documented field.
    assert (ceiling, "value not in quote") in verify_sheet(
        with_ceiling(400, CEILING_SENTENCE), docs
    ).rejected
    invented = "Patients up to 300% of the Federal Poverty Guidelines always get help from us."
    assert (ceiling, "quote not found in source") in verify_sheet(
        with_ceiling(300, invented), docs
    ).rejected
    # Another state's clause of a system policy is not this Massachusetts hospital's ceiling.
    assert (ceiling, OTHER_STATE_REASON) in verify_sheet(with_ceiling(300, illinois), docs).rejected


def test_a_ceiling_quote_must_state_the_hospitals_own_top_limit_on_assistance():
    from waive.atlas.verify import (
        CEILING_FLOOR_REASON,
        CEILING_NOT_ASSISTANCE_REASON,
        CEILING_NOT_TOP_REASON,
        CEILING_OTHER_PROGRAM_REASON,
        CEILING_UNTIED_REASON,
        ceiling_problem,
    )

    # Review of 7.14. Accepted: the hospital's own help, up to its top limit.
    for quote, value in (
        (CEILING_SENTENCE, 300),
        (ARNOT_SENTENCE, 400),
        ("Financial assistance is available to household incomes less than 600% of the FPG.", 600),
        ("Discounts are limited to incomes up to 300% FPG.", 300),
        ("Charity care is available if income does not exceed 400% of the FPL.", 400),
        ("Income that is not more than 250% of the FPL qualifies for a discount.", 250),
        ("Patients up to 400% of the FPL who are not eligible for Medicaid may qualify for "
         "financial assistance.", 400),
        ("Insured patients up to 200% FPL and uninsured patients up to 400% FPL are eligible.", 400),
    ):  # fmt: skip
        assert ceiling_problem(quote, Decimal(value)) is None, quote
    # The top of the quote, not a band inside it.
    bands = (
        "Patients up to 200% of the Federal Poverty Level get an 80% discount and patients up to "
        "300% of the Federal Poverty Level get a 50% discount."
    )
    assert ceiling_problem(bands, Decimal(200)) == CEILING_NOT_TOP_REASON
    assert ceiling_problem(bands, Decimal(300)) is None
    # A floor is not a ceiling.
    for quote in (
        "Patients with income above 400% of the Federal Poverty Guidelines may apply for "
        "catastrophic assistance.",
        "Patients with income of at least 400% of the Federal Poverty Level are not eligible "
        "for assistance.",
        "Households whose income exceeds 400% of the FPL may qualify for a discount.",
        "Patients earning more than four hundred percent (400%) of the FPL are eligible for a "
        "discount.",
    ):
        assert ceiling_problem(quote, Decimal(400)) == CEILING_FLOOR_REASON, quote
    # Another program's limit, a payment plan, or no word of assistance at all.
    assert ceiling_problem(
        "Households up to 300% of the Federal Poverty Guidelines may also apply to the state's "
        "Health Safety Net.",
        Decimal(300),
    ) in (CEILING_NOT_ASSISTANCE_REASON, CEILING_OTHER_PROGRAM_REASON)
    assert (
        ceiling_problem(
            "Medicaid expansion adults up to 138% of the Federal Poverty Level are screened for "
            "Medicaid eligibility.",
            Decimal(138),
        )
        == CEILING_OTHER_PROGRAM_REASON
    )
    assert (
        ceiling_problem(
            "Patients earning less than 600% of the Federal Poverty Guidelines are eligible for "
            "payment plans only.",
            Decimal(600),
        )
        == CEILING_OTHER_PROGRAM_REASON
    )
    assert ceiling_problem(
        "A payment plan is available to patients up to 400% of the Federal Poverty Level.",
        Decimal(400),
    ) in (CEILING_NOT_ASSISTANCE_REASON, CEILING_OTHER_PROGRAM_REASON)
    assert (
        ceiling_problem(
            "Households up to 300% of the Federal Poverty Guidelines may apply to the state's "
            "Health Safety Net for financial assistance.",
            Decimal(300),
        )
        == CEILING_OTHER_PROGRAM_REASON
    )
    assert (
        ceiling_problem("Patients up to 400% of the FPL are welcome at every campus.", Decimal(400))
        == CEILING_NOT_ASSISTANCE_REASON
    )
    assert ceiling_problem("Assistance up to 400% of the cost.", Decimal(400)) == (
        CEILING_UNTIED_REASON
    )


def test_verify_sheet_rejects_a_ceiling_that_is_not_the_hospitals_own_limit():
    from waive.atlas.verify import CEILING_FLOOR_REASON, CEILING_OTHER_PROGRAM_REASON

    ceiling = "eligibility.assistance_ceiling_fpl"
    floor = (
        "Patients with income above 300% of the Federal Poverty Guidelines may apply for "
        "catastrophic assistance."
    )
    program = (
        "Households up to 300% of the Federal Poverty Guidelines may apply to the state's Health "
        "Safety Net for financial assistance."
    )
    docs = {SAMPLE_SOURCE_ID: "\n".join([SAMPLE_POLICY_TEXT, floor, program])}
    assert (ceiling, CEILING_FLOOR_REASON) in verify_sheet(with_ceiling(300, floor), docs).rejected
    assert (ceiling, CEILING_OTHER_PROGRAM_REASON) in verify_sheet(
        with_ceiling(300, program), docs
    ).rejected


def test_free_care_in_plain_words_names_a_free_care_limit():
    from waive.atlas.verify import free_care_limits_stated

    # Review of 7.14: free bands the document-conflict net did not recognise.
    for sentence, limit in (
        ("Patients with household income at or below 200% of the Federal Poverty Guidelines are "
         "not billed for services.", 200),
        ("Patients at or below 200% FPL receive Charity Care at 100%.", 200),
        ("Level 1 (up to 200% of the Federal Poverty Level): 100% of charges forgiven.", 200),
        ("Patients up to 200% of the Federal Poverty Level will not be charged.", 200),
        ("Income up to 200% of the federal poverty level qualifies for completely free services.",
         200),
        ("Patients at or below 100% of the Federal Poverty Level receive free care.", 100),
    ):  # fmt: skip
        assert free_care_limits_stated(sentence) == {Decimal(limit)}, sentence
    # The amounts-generally-billed cap and a payment plan's terms are not free care.
    assert (
        free_care_limits_stated(
            "Patients up to 400% of the FPL will not be charged more than the amounts generally "
            "billed. Patients up to 300% FPL are not charged interest on a payment plan."
        )
        == set()
    )


def test_assistance_limits_a_document_states():
    from waive.atlas.verify import assistance_limits_stated

    # Review of 7.14: what a ceiling-only sheet is checked against (publish.document_conflicts).
    assert assistance_limits_stated(
        "Patients at or below 200% of the Federal Poverty Level are eligible for financial "
        "assistance. Patients between 201% and 400% of the Federal Poverty Level receive a sliding "
        "scale discount. Call 617-555-0100 about payment plans up to 500% of the FPL."
    ) == {Decimal(200), Decimal(400)}
    assert (
        assistance_limits_stated(
            "Households up to 300% of the Federal Poverty Guidelines may also apply to the state's "
            "Health Safety Net for financial assistance. Our office is open to all."
        )
        == set()
    )


def test_reading_poverty_percentages_stays_fast_on_a_long_run_of_years():
    import time

    from waive.atlas.verify import (
        assistance_limits_stated,
        names_percent_of_poverty_level,
        states_income_limit,
    )

    # A flattened table can hold thousands of year-like numbers in one "sentence"; the reading
    # must not backtrack through them (a repeated qualifier group took 8 s for 16,000).
    run = "Financial assistance up to 400% " + "2026 " * 20_000 + "of the year"
    started = time.perf_counter()
    assert not names_percent_of_poverty_level(run, Decimal(400))
    assert assistance_limits_stated(run + ". Federal poverty level.") == set()
    # The income-limit rule reads a few words around each occurrence, never the whole table.
    assert not states_income_limit("Sliding Scale " + "100% 89% " * 2_000, Decimal(100))
    assert time.perf_counter() - started < 2


def test_the_verifier_rejects_a_covered_provider_the_quote_does_not_name():
    from waive.atlas.schema import Cited, Coverage

    sheet = st_example_sheet()
    cited = Cited[list[str]](
        value=["Example Emergency Physicians", "Harbor Hospitalists"],
        quote="providers covered by this policy: Example Emergency Physicians",
        source_id=SAMPLE_SOURCE_ID,
        checked_on=sheet.eligibility.free_care_max_fpl.checked_on,
    )
    listed = sheet.model_copy(update={"coverage": Coverage(covered_providers=cited)})
    report = verify_sheet(listed, {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT})
    assert not report.ok
    assert any(path == "coverage.covered_providers" for path, _ in report.rejected)


def test_a_free_care_limit_that_differs_for_inpatient_and_outpatient_care_is_not_one_limit():
    from waive.atlas.verify import FREE_LIMIT_SPLIT_REASON

    # OhioHealth Doctors Hospital (360152), website check 2: "up to 100% of the FPG for inpatient
    # services or 150% of the FPG for outpatient services" was published as free care to 150%,
    # which promises free inpatient care the policy does not give between 100% and 150%.
    split = (
        "If your annual family income is up to 100% of the FPG for inpatient services or 150% of "
        "the FPG for outpatient services, you will receive free care."
    )
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + split + "\n"}
    for value in (150, 100):
        report = verify_sheet(with_free_limit(value, split), docs)
        assert ("eligibility.free_care_max_fpl", FREE_LIMIT_SPLIT_REASON) in report.rejected
    # One limit for both kinds of care stays a limit.
    both = (
        "Patients with family income at or below 200% of the Federal Poverty Level receive free "
        "care for inpatient and outpatient services."
    )
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + both + "\n"}
    report = verify_sheet(with_free_limit(200, both), docs)
    assert not any(path == "eligibility.free_care_max_fpl" for path, _ in report.rejected)


def test_a_sliding_scale_or_another_states_program_is_no_free_care_limit():
    from waive.atlas.verify import FREE_LIMIT_NOT_FREE_REASON, OTHER_STATE_REASON

    free = "eligibility.free_care_max_fpl"
    # Website check 3 (2026-10-19): UF Health Jacksonville (100001), Summa Western Reserve
    # (360150) and UF Health Shands (100113) published free care to 200% from a sliding scale,
    # a prorated scale and a denial sentence; none of the three promises free care.
    for quote in (
        "Financial Assistance shall be granted on a sliding scale for qualified applicants for "
        "patients with income up to 200% of the poverty guidelines.",
        "For insured patients, HFA eligibility is based upon expanded income levels of up to "
        "200% of FPG and is prorated on a sliding scale.",
        "Individuals who are denied Financial Assistance, due to being over the 200% of federal "
        "poverty limit, can be reviewed for Catastrophic Reduction.",
    ):
        docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + quote + "\n"}
        assert (free, FREE_LIMIT_NOT_FREE_REASON) in verify_sheet(
            with_free_limit(200, quote), docs
        ).rejected, quote
    # Thomas Jefferson University Hospital (390174, Philadelphia) quoted Jefferson's New Jersey
    # section: "100% NJ Charity Care (free care)" is New Jersey's program, not Pennsylvania's.
    nj = (
        "Patients with family gross income less than or equal to 200% of Federal Poverty Level "
        '("FPL ") are eligible for 100% NJ Charity Care (free care).'
    )
    sheet = with_free_limit(200, nj)
    pa = sheet.model_copy(update={"hospital": sheet.hospital.model_copy(update={"state": "PA"})})
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + nj + "\n"}
    assert (free, OTHER_STATE_REASON) in verify_sheet(pa, docs).rejected
    nj_sheet = sheet.model_copy(
        update={"hospital": sheet.hospital.model_copy(update={"state": "NJ"})}
    )
    assert not any(p == free for p, _ in verify_sheet(nj_sheet, docs).rejected)
    # Free care said plainly, or as a sliding scale that starts with free care, stays.
    for quote in (
        "Patients with family income at or below 200% of the Federal Poverty Level receive free "
        "care, and a sliding scale applies above it.",
        "Charity Care: The hospital bill amount that the patient is responsible for will be "
        "forgiven when an income level is 200% percent or less of the FPL.",
    ):
        docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + quote + "\n"}
        assert not any(
            p == free for p, _ in verify_sheet(with_free_limit(200, quote), docs).rejected
        ), quote


def test_the_top_of_a_later_band_is_no_free_care_limit():
    from waive.atlas.verify import FREE_LIMIT_LATER_BAND_REASON

    # AHN Saint Vincent (390009), website check 4: the table "100% Discount 75% Discount Household
    # Members Federal Poverty Level - 100% 200% 201% - 300%" gives free care to 200% and 75% off
    # from 201% to 300%; it was published as free care to 300%.
    quote = "Household Members Federal Poverty Level - 100% 200% 201% - 300%"
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + quote + "\n"}
    report = verify_sheet(with_free_limit(300, quote), docs)
    assert ("eligibility.free_care_max_fpl", FREE_LIMIT_LATER_BAND_REASON) in report.rejected
    # The first band's top stays a limit.
    first = (
        "Federal Poverty Guidelines Financial Assistance Discount 0% – 200% 100% 201% - 250% 80%"
    )
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + first + "\n"}
    report = verify_sheet(with_free_limit(200, first), docs)
    assert not any(p == "eligibility.free_care_max_fpl" for p, _ in report.rejected)


def test_a_tier_lower_bound_accepts_the_written_inclusive_start() -> None:
    from waive.atlas.schema import DiscountTier
    from waive.atlas.verify import _lower_bound_in, value_in_quote

    assert _lower_bound_in(Decimal(200), "above 200% of FPL")
    assert _lower_bound_in(Decimal(200), "201% to 400% of FPL: 50% discount")
    assert _lower_bound_in(Decimal(200), "200.1 to 400% 50%")
    assert _lower_bound_in(Decimal(200), "200.01% - 400% 50%")
    assert not _lower_bound_in(Decimal(200), "250% to 400% of FPL: 50% discount")
    tier = DiscountTier(
        min_fpl_exclusive=Decimal(200), max_fpl_inclusive=Decimal(400), discount_percent=Decimal(50)
    )
    assert value_in_quote([tier], "201% to 400% of FPL: 50% discount")
    assert not value_in_quote([tier], "201% to 400% of FPL: 60% discount")


def test_a_multi_span_quote_is_found_when_every_span_is() -> None:
    from waive.atlas.verify import quote_found

    document = "Income 0 to 200% of FPL: free care. Income 201% to 400% of FPL: 50% discount."
    assert quote_found("0 to 200% of FPL: free care || 201% to 400% of FPL: 50% discount", document)
    assert not quote_found(
        "0 to 200% of FPL: free care || 401% to 500% of FPL: 25% discount", document
    )
    assert not quote_found(" || ", document)


def test_assistance_that_may_reach_up_to_100_percent_is_a_limit_not_free_care():
    from waive.atlas.verify import CEILING_REASON, quotes_assistance_ceiling

    free = "eligibility.free_care_max_fpl"
    # Decision 7.12c-1 (owner, 2026-10-23, option B): each of these promises at most full help,
    # and only maybe, so its income figure is a limit on assistance, not a free-care band.
    for value, quote in (
        (
            400,
            "If your gross (total) family income is no higher than 400 percent of the "
            "[Federal Poverty Level (FPL)](https://syfphr.oshpd.ca.gov/), you may receive a "
            "discount of up to 100 percent on the services you received.",
        ),
        (
            200,
            "If your family adjusted income is less than 200% of federal poverty guidelines, you "
            "may be eligible for up to a 100% discount.",
        ),
        (
            240,
            "If the patient's income is between 200%-240% of the Federal Poverty Income "
            "Guidelines they may qualify for Financial Assistance up to 100% of their hospital "
            "and clinic bills.",
        ),
    ):
        assert quotes_assistance_ceiling(quote), quote
        docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + quote + "\n"}
        assert (free, CEILING_REASON) in verify_sheet(with_free_limit(value, quote), docs).rejected
    # No "may", or free care said in so many words: still free care.
    for value, quote in (
        (
            200,
            "Any patient whose Family Income is at or below 200% of the FPL, including, without "
            "limitation, any Uninsured or Underinsured patient, is eligible to receive Financial "
            "Assistance up to a 100% discount from his or her account balance for eligible "
            "services provided to the patient after payment, if any, by any third-party(ies).",
        ),
        (
            150,
            "Patients who are uninsured or underinsured and have a family income at or below 150% "
            "of the Federal Poverty Guidelines (FPG) may receive free care (a 100% discount).",
        ),
        (
            200,
            "Patients at or below 200% of the poverty guidelines may receive free care, a discount "
            "of up to 100% of charges.",
        ),
    ):
        assert not quotes_assistance_ceiling(quote), quote
        docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + quote + "\n"}
        assert not any(
            p == free for p, _ in verify_sheet(with_free_limit(value, quote), docs).rejected
        ), quote


# Discount tables the publication audit (2026-10-24) found misread: rules R1', R2, R9, R10 and
# R11. A quote named for a hospital is the one it published, or its policy's own text.
CECIL_HARDSHIP = (
    "Patients with household income between 401-450% of FPL and with a financial hardship will "
    "receive a 40% adjustment. Patients with household income between 451-500% will receive a 35% "
    "adjustment."
)
MASS_HEALTH_PARTIAL = (
    "Patients whose MAGI Household Income or Medical Hardship Family Countable Income is between "
    "150.1% and 300% of the FPL receive a 20% discount."
)
UNITYPOINT_AGB_TABLE = (
    "2.3 Hospital bills will be further reduced by the following amounts for patients in \n"
    "each FPIG category below:   \n0-200% of FPIG :  100% discount off AGB \n"
    "201-225% of FPIG :  65% discount off AGB \n226-250% of FPIG :  45% discount off AGB \n"
    "251-300% of FPIG :  25% discount off AGB \n301-400% of FPIG :  5% discount off AGB"
)
LAKEWOOD_TIERS = (
    "B. 0-100% FPL – patient service fee\n1. Outpatient office visit $5\n"
    "2. Emergency Department $10\n3. Inpatient 100% financial assistance\n"
    "C. 101-300% FPL – patient service fee\n1. Outpatient office visit $25\n"
    "2. Emergency Department $50\n3. Inpatient $100 per inpatient day\n"
    "D. 301-400% FPL: Services will be discounted by the current Uninsured Discount amount, with "
    "an additional 25% discount on the remaining balance"
)
HENRY_FORD_INSURED_TABLE = (
    "Household Income Discount Off of Balance Due\n351% - 400% of the FPL 25%\n"
    "301% - 350% of the FPL 50%\n251% - 300% of the FPL 75%\n<250% of the FPL 100%"
)
HENRY_FORD_TABLES = (
    "Sliding Scale Discount Tables: \nUninsured Patients: \nFor qualifying uninsured patients "
    "with incomes less than 400% of FPL, patient responsibility is adjusted to \nthe percentage of "
    "the Medicare allowable amount based on the table below: \n  Household Income % of Medicare "
    "Rate \n351% - 400% of the FPL 91% \n301% - 350% of the FPL 61% \n251% - 300% of the FPL 30% "
    "\n<250% of the FPL 0% \nQualifying uninsured patients with incomes below 250% are given a "
    "100% discount \nInsured Patients: \nFor qualifying insured patients with incomes less than "
    "400% of FPL, patient responsibility is discounted \nbased on the table below \n"
    + HENRY_FORD_INSURED_TABLE
)
HENRY_FORD_BANDS = [(250, 300, 75), (300, 350, 50), (350, 400, 25)]
HEART_OF_TEXAS_TABLE = (
    "Family Income as % of FPG Discount Patient LIability Up to 300% of FPG 100% 0% Over 300% & "
    "up to 350% 75% 25%"
)
HEART_OF_TEXAS_TERMS = (
    "2. Patients whose family income is above 300% but not more than 350% of the FPL are eligible "
    "to receive services at amounts no greater than the amounts generally billed to (received by "
    "the hospital for) commercially insured [or Medicare] patients; Current levels of discount and "
    "patient liability are: " + HEART_OF_TEXAS_TABLE
)
ALEXIAN_TABLE = (
    "(FPL – Federal Poverty Level Income) Uninsured Patient Sliding Scale % Insured Patient "
    "Sliding Scale % 0% - 250% FPL 100% 0% - 250% FPL 100% 251% - 300% FPL 95% 251% - 300% FPL "
    "95% 301% - 400% FPL 90% 301% - 400% FPL 90% 401% - 600% FPL 85%"
)
MEDSTAR_REDUCED_COST_TABLE = (
    "Financial Assistance Level  \nFree / Reduced-Cost Care  \n\nAdjusted Percentage of  \n"
    "Poverty Level  \n\nHSCRC-Regulated  \nServices  \nWashington Hospitals,  \n"
    "Hospital-Based Physician  \nPractices, and non-HSCRC  \nRegulated Services  \n"
    "0% to 200% 100% 100%  \n201% to 250% 75% % 80%  \n251% to 300% 60% 60%  \n"
    "301% to 350%   40%  \n351% to 400%    20%"
)
MEDSTAR_HSCRC_COLUMN = [(200, 250, 75), (250, 300, 60), (300, 350, 40), (350, 400, 20)]
MEDSTAR_WASHINGTON_COLUMN = [(200, 250, 80), (250, 300, 60), (300, 350, 40), (350, 400, 20)]
TUFTS_SERVICE_TABLE = (
    "Family Size\n\n100% FPL\nUp to\n150% FPL\nUp to\n300% FPL\nGreater than\n301% FPL\n"
    "1 $13,596 $20,388 $40,776 $40,777\n2 $18,312 $27,468 $54,936 $54,937\n"
    "3 $23,040 $34,548 $69,096 $69,097\n4 $27,756 $41,628 $83,256 $83,257\n"
    "5 $32,472 $48,708 $97,416 $97,417\n6 $37,200 $55,788 $111,576 $111,577\n"
    "7 $41,916 $62,868 $125,736 $125,737\n8 $46,632 $69,948 $139,896 $139,897\n"
    "Each Person > 8 $4,728 $7,080 $14,160 $14,161\n\nTM Lowell General Hospital\n"
    "Inpatient Discount\n100% 63.28% 30%\nTM Lowell General Hospital\nOutpatient Discount \n"
    "70.54%\n\nTM Melrose Wakefield Hospital\nInpatient Discount\n100% 36.84% 30%\n"
    "TM Melrose Wakefield Hospital\nOutpatient Discount\n65.91%\n\n"
    "TM Tufts Medical Center Hospital\nInpatient Discount\n100% 49.50% 30%\n"
    "TM Tufts Medical Center Hospital\nOutpatient Discount\n69.50%"
)
BERKSHIRE_SERVICE_TABLE = (
    "Family Income as % of FPG\nBerkshire Medical Center\nFairview Hospital\nInpatient\n"
    "Outpatient\nInpatient\nOutpatient\n0 – 400\n46.07%\n63.14%\n48.58%\n53.17%"
)
ONE_TABLE_FOR_BOTH = (
    "Inpatient and outpatient services will be discounted by the following percentages: "
    "201-250% 80%"
)
GRANDVIEW_SPANS = (
    "Grandview Hospital 16% 95% 89% 84% 84% || Federal Poverty Level 251-300% 301-350% 351-400% "
    "Catastrophic"
)
UVM_UNINSURED = (
    "Eligible uninsured patients with modified adjusted gross household income between 250% and "
    "400% of the FPLG shall receive an additional 40% discount from the amount generally billed "
    "for the services received."
)
UVM_AWARDS = (
    UVM_UNINSURED + "\n• Eligible patients with insurance or other coverage for the services "
    "delivered and with household income between 250% and 400% of the FPLG shall receive a n "
    "additional 40% discount on the patient’s out-of-pocket costs, after insurance \npayments or "
    "payments from other coverage."
)
DARTMOUTH_INSURED_TIERS = (
    "For insured patients whose household income is: •  251%-300% of FPL, the discount will be 75% "
    "•  301%-350% of FPL, the discount will be 50% •  351%-400% of FPL, the discount will be 40%"
)
DARTMOUTH_AWARDS = (
    "If an individual is approved for financial assistance, the amount of such assistance to be "
    "\nprovided for applicable care will be as follows: \nInsured patients: Insured patients shall "
    "have a discount applied to their out-of-pocket \nexpenses. \n \nFor insured patients whose "
    "household income is at or below 250% of FPL, we will \nwaive (i.e., 100% discount) all "
    "out-of-pocket costs for emergency and medically \nnecessary services rendered (i.e., the care "
    "is free). For insured patients whose household \nincome is: \n•  251%-300% of FPL, the "
    "discount will be 75% \n•  301%-350% of FPL, the discount will be 50% \n•  351%-400% of FPL, "
    "the discount will be 40% \n \nUninsured patients: Uninsured patients who are eligible for "
    "financial assistance shall \nhave a discount applied to the amount generally billed for the "
    "emergency and medically \nnecessary services rendered. \n \nFor uninsured patients whose "
    "household income is at or below 250% of FPL, we will \nwaive (i.e., 100% discount) the amount "
    "generally billed for emergency and medically \nnecessary services rendered (i.e., the care is "
    "free). For uninsured patients whose household \nincome is: \n•  251%-300% of FPL, the "
    "discount will be 75% \n•   301%-350% of FPL, the discount will be 50% \n• 351%-400% of FPL, "
    "the discount will be 40% \n \nIn catastrophic situations, for uninsured or insured patients: "
    "If the total amount owed for emergency \nand medically necessary services rendered exceeds "
    "20% of the household income, then the amount \nowed shall be reduced so that it is equal to "
    "20% of the patient’s household’s \nincome."
)
DARTMOUTH_BANDS = [(250, 300, 75), (300, 350, 50), (350, 400, 40)]
# The same policy with the uninsured 351-400% band at another discount, or left out.
_DARTMOUTH_LAST_UNINSURED = "• 351%-400% of FPL, the discount will be 40% \n \nIn catastrophic"
DARTMOUTH_UNINSURED_30 = DARTMOUTH_AWARDS.replace(
    _DARTMOUTH_LAST_UNINSURED, _DARTMOUTH_LAST_UNINSURED.replace("40%", "30%")
)
DARTMOUTH_UNINSURED_SHORT = DARTMOUTH_AWARDS.replace(
    _DARTMOUTH_LAST_UNINSURED, "\n \nIn catastrophic"
)


def with_tiers_in(quote, bands, state):
    sheet = with_tiers_quote(quote, bands)
    return sheet.model_copy(update={"hospital": sheet.hospital.model_copy(update={"state": state})})


_HARDSHIP_TIER_CASES = [
    # Union Hospital of Cecil County (210032): the 401-450% band needs a financial hardship
    # (Maryland's reduced-cost care for medical debt); both bands were published as income bands.
    (CECIL_HARDSHIP, [(400, 450, 40), (450, 500, 35)], True),
    ("Patients with a medical hardship and income between 301% and 500% of the FPL receive a 50% "
     "discount.", [(300, 500, 50)], True),
    ("Patients with medical debt above 25% of family income and income between 401% and 500% of "
     "the FPL receive a 50% discount.", [(400, 500, 50)], True),
    ("Patients with catastrophic medical expenses and income between 401% and 500% of the FPL "
     "receive a 30% discount.", [(400, 500, 30)], True),
    # MassHealth's "Medical Hardship Family Countable Income" is an income measure, no condition.
    (MASS_HEALTH_PARTIAL, [(150, 300, 20)], False),
    (UNITYPOINT_AGB_TABLE, [(200, 225, 65), (225, 250, 45), (250, 300, 25), (300, 400, 5)], False),
    # The hardship sentence states none of the tiers' discounts.
    ("Patients with a financial hardship may apply for further help. Patients between 201% and "
     "300% of the FPL receive a 50% discount.", [(200, 300, 50)], False),
    # UCHealth Grandview (060130): the spans are two rows of one table, so its "Catastrophic"
    # column header conditions none of the income tiers.
    (GRANDVIEW_SPANS, [(250, 300, 95), (300, 350, 89), (350, 400, 84)], False),
]  # fmt: skip


@pytest.mark.parametrize(("quote", "bands", "expected"), _HARDSHIP_TIER_CASES)
def test_tiers_need_hardship(quote, bands, expected):
    from waive.atlas.verify import tiers_need_hardship

    assert tiers_need_hardship(quote, tiers_of(bands)) is expected


_ADDITIONAL_TIER_CASES = [
    # University Health Lakewood (260102): the 301-400% band gets the uninsured discount first and
    # 25% more on what is left, so 25 is not that band's discount.
    (LAKEWOOD_TIERS, [(300, 400, 25)], True),
    ("Patients between 301% and 400% of the FPL receive an additional 20 percent reduction after "
     "the self-pay discount.", [(300, 400, 20)], True),
    ("Patients between 201% and 300% of FPL receive a 50% discount. Add $5,680 for each "
     "additional person.", [(200, 300, 50)], False),
    # The added discount is not a stored tier's.
    ("Patients between 201% and 300% of the FPL receive a 75% discount, and an additional 10% "
     "discount for prompt payment.", [(200, 300, 75)], False),
    # UVM Health (470001, 470003) bases every uninsured award on the amount generally billed and
    # every insured one on the patient's out-of-pocket costs, so 40 is the band's own discount,
    # read like UnityPoint's "65% discount off AGB".
    (UVM_UNINSURED, [(250, 400, 40)], False),
    (UVM_AWARDS, [(250, 400, 40)], False),
    ("Patients between 201% and 300% of the FPL receive an additional 20% off AGB.",
     [(200, 300, 20)], False),
    ("Patients between 201% and 300% of the FPL receive an additional 20% discount off the "
     "self-pay rate.", [(200, 300, 20)], True),
]  # fmt: skip


@pytest.mark.parametrize(("quote", "bands", "expected"), _ADDITIONAL_TIER_CASES)
def test_tier_is_additional(quote, bands, expected):
    from waive.atlas.verify import tier_is_additional

    assert tier_is_additional(quote, tiers_of(bands)) is expected


_INSURED_TABLE_CASES = [
    # Henry Ford (230047, 230053, 230092, 230302): the "Discount Off of Balance Due" table follows
    # "Insured Patients: For qualifying insured patients"; uninsured patients have their own table.
    (HENRY_FORD_INSURED_TABLE, HENRY_FORD_TABLES, True),
    (HENRY_FORD_INSURED_TABLE, "Insured Patients:\n" + HENRY_FORD_INSURED_TABLE, True),
    (HENRY_FORD_INSURED_TABLE, "Financial assistance is available to both uninsured and insured "
     "patients. Discounts are based on the table below: \n" + HENRY_FORD_INSURED_TABLE, False),
    # Heart of Texas (451348): the AGB clause's "commercially insured patients" introduces no table.
    (HEART_OF_TEXAS_TABLE, HEART_OF_TEXAS_TERMS, False),
    # Alexian Brothers (140258): the quote has an uninsured column of its own.
    (ALEXIAN_TABLE, "The sliding scale discount is as follows: " + ALEXIAN_TABLE, False),
    # The quote is not in the document: nothing comes before it.
    (HENRY_FORD_INSURED_TABLE, "Insured Patients: For qualifying insured patients the table "
     "below applies.", False),
    # Dartmouth Health (301304, 301305, 471302): the quote is the insured paragraph.
    (DARTMOUTH_INSURED_TIERS, DARTMOUTH_AWARDS, True),
]  # fmt: skip


@pytest.mark.parametrize(("quote", "document", "expected"), _INSURED_TABLE_CASES)
def test_insured_only_table(quote, document, expected):
    from waive.atlas.verify import insured_only_table

    assert insured_only_table(quote, document) is expected


_SAME_TIERS_CASES = [
    # Dartmouth Health's policy gives uninsured patients the insured patients' 75/50/40 for the
    # same bands, so the insured paragraph's tiers are everyone's.
    (DARTMOUTH_AWARDS, DARTMOUTH_BANDS, True),
    # Henry Ford's uninsured patients pay a share of the Medicare rate instead.
    (HENRY_FORD_TABLES, HENRY_FORD_BANDS, False),
    (DARTMOUTH_UNINSURED_30, DARTMOUTH_BANDS, False),
    (DARTMOUTH_UNINSURED_SHORT, DARTMOUTH_BANDS, False),
    (DARTMOUTH_AWARDS, [], False),
]


@pytest.mark.parametrize(("document", "bands", "expected"), _SAME_TIERS_CASES)
def test_uninsured_get_same_tiers(document, bands, expected):
    from waive.atlas.verify import uninsured_get_same_tiers

    assert DARTMOUTH_UNINSURED_30 != DARTMOUTH_AWARDS != DARTMOUTH_UNINSURED_SHORT
    assert uninsured_get_same_tiers(document, tiers_of(bands)) is expected


_REGULATED_COLUMN_CASES = [
    # MedStar Montgomery (210018) and MedStar Southern Maryland (210062) published 80 for the
    # 201-250% band, read from the Washington hospitals' column; a Maryland hospital's is 75.
    (MEDSTAR_REDUCED_COST_TABLE, "MD", MEDSTAR_WASHINGTON_COLUMN, True),
    (MEDSTAR_REDUCED_COST_TABLE, "MD", MEDSTAR_HSCRC_COLUMN, False),
    # A Washington, D.C. hospital (MedStar Washington, 090011) reads the last column.
    (MEDSTAR_REDUCED_COST_TABLE, "DC", MEDSTAR_WASHINGTON_COLUMN, False),
    (MEDSTAR_REDUCED_COST_TABLE, "DC", MEDSTAR_HSCRC_COLUMN, True),
    # Two columns without the HSCRC headers: no rule says which one is the hospital's.
    ("201% to 250% 75% 80%", "MD", [(200, 250, 80)], False),
]


@pytest.mark.parametrize(("quote", "state", "bands", "expected"), _REGULATED_COLUMN_CASES)
def test_wrong_regulated_column(quote, state, bands, expected):
    from waive.atlas.verify import wrong_regulated_column

    assert wrong_regulated_column(quote, state, tiers_of(bands)) is expected


_SPLIT_BY_SERVICE_CASES = [
    # Tufts Medical Center (220116) published 30% for 150-300%, one hospital's inpatient column;
    # its outpatients get 69.50%. Berkshire Medical Center (220046) published 46.07%, its
    # inpatient column; outpatients get 63.14%.
    (TUFTS_SERVICE_TABLE, True),
    (BERKSHIRE_SERVICE_TABLE, True),
    ("Income In-patient: Out-patient 201-300% 50% 60%", True),
    (ONE_TABLE_FOR_BOTH, False),
    (LAKEWOOD_TIERS, False),
]


@pytest.mark.parametrize(("quote", "expected"), _SPLIT_BY_SERVICE_CASES)
def test_tiers_split_by_service(quote, expected):
    from waive.atlas.verify import tiers_split_by_service

    assert tiers_split_by_service(quote) is expected


_MISREAD_TIER_SHEETS = [
    ("HARDSHIP_TIER_REASON", CECIL_HARDSHIP, [(400, 450, 40), (450, 500, 35)], "MD",
     CECIL_HARDSHIP),
    ("ADDITIONAL_TIER_REASON", LAKEWOOD_TIERS, [(300, 400, 25)], "MO", LAKEWOOD_TIERS),
    ("INSURED_TABLE_REASON", HENRY_FORD_INSURED_TABLE, HENRY_FORD_BANDS, "MI", HENRY_FORD_TABLES),
    ("INSURED_TABLE_REASON", DARTMOUTH_INSURED_TIERS, DARTMOUTH_BANDS, "NH",
     DARTMOUTH_UNINSURED_30),
    ("REGULATED_COLUMN_REASON", MEDSTAR_REDUCED_COST_TABLE, MEDSTAR_WASHINGTON_COLUMN, "MD",
     MEDSTAR_REDUCED_COST_TABLE),
    ("TIERS_SPLIT_REASON", TUFTS_SERVICE_TABLE, [(150, 300, 30)], "MA", TUFTS_SERVICE_TABLE),
    ("TIERS_SPLIT_REASON", BERKSHIRE_SERVICE_TABLE, [(0, 400, "46.07")], "MA",
     BERKSHIRE_SERVICE_TABLE),
]  # fmt: skip


@pytest.mark.parametrize(("reason", "quote", "bands", "state", "document"), _MISREAD_TIER_SHEETS)
def test_verify_sheet_rejects_a_misread_discount_table(reason, quote, bands, state, document):
    from waive.atlas import verify

    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + document + "\n"}
    report = verify_sheet(with_tiers_in(quote, bands, state), docs)
    assert ("eligibility.discount_tiers", getattr(verify, reason)) in report.rejected


_TIER_SHEETS_READ_RIGHT = [
    (MEDSTAR_REDUCED_COST_TABLE, MEDSTAR_HSCRC_COLUMN, "MD", MEDSTAR_REDUCED_COST_TABLE),
    (MEDSTAR_REDUCED_COST_TABLE, MEDSTAR_WASHINGTON_COLUMN, "DC", MEDSTAR_REDUCED_COST_TABLE),
    (MASS_HEALTH_PARTIAL, [(150, 300, 20)], "MA", MASS_HEALTH_PARTIAL),
    (ONE_TABLE_FOR_BOTH, [(200, 250, 80)], "ID", ONE_TABLE_FOR_BOTH),
    (HENRY_FORD_INSURED_TABLE, HENRY_FORD_BANDS, "MI", "Financial assistance is available to both "
     "uninsured and insured patients. Discounts are based on the table below: \n"
     + HENRY_FORD_INSURED_TABLE),
    (DARTMOUTH_INSURED_TIERS, DARTMOUTH_BANDS, "NH", DARTMOUTH_AWARDS),
    (UVM_AWARDS, [(250, 400, 40)], "VT", UVM_AWARDS),
    (GRANDVIEW_SPANS, [(250, 300, 95), (300, 350, 89), (350, 400, 84)], "CO", GRANDVIEW_SPANS),
]  # fmt: skip


@pytest.mark.parametrize(("quote", "bands", "state", "document"), _TIER_SHEETS_READ_RIGHT)
def test_verify_sheet_keeps_a_discount_table_read_right(quote, bands, state, document):
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + document + "\n"}
    report = verify_sheet(with_tiers_in(quote, bands, state), docs)
    assert not [r for r in report.rejected if r[0] == "eligibility.discount_tiers"]


# Free-care limits the publication audit (2026-10-24) found misread: rules R3' and R8'. A quote
# named for a hospital is the one it published, or its policy's own text.
LAKEWOOD_FREE_BAND = (
    "0-100% FPL – patient service fee\n1. Outpatient office visit $5\n"
    "2. Emergency Department $10\n3. Inpatient 100% financial assistance"
)
CAYUGA_NOMINAL = (
    "A patient whose household income, as determined by the application income worksheet, is "
    "equal to or less than 100% of the most recent Federal Poverty Guidelines qualifies for a "
    "nominal payment limit as designated by major service category, the rest will be written off."
)
EMORY_FREE_LIMIT = (
    "If Family Income of a patient not enrolled in Medicaid is less than \nor equal to 200% of the "
    "Federal Poverty Guidelines, the patient will \nreceive 100% charity care adjustment for "
    "Eligible Health Care \nServices (consistent with Indigent Care Trust Fund requirements)."
)
EMORY_AWARDS = (
    "b. " + EMORY_FREE_LIMIT + "  \nc. If Family Income of a patient who is not enrolled in "
    "Medicaid and \nis uninsured is at least 201 % but less than or equal to 350% of the \nFederal "
    "Poverty Guidelines, the patient will receive 100% financial \nassistance for the patient’s "
    "personal responsibility for Eligible \nHealth Care Services. \n3. Family Income must be "
    "verified through either the Application Process or \nthe External Screening Process, as "
    "further described below. \n6. NOTE: Uninsured patients whose Family Income is greater than "
    "350% of \nthe U.S. Federal Poverty Guidelines may qualify for a 65% self-pay \ndiscount on "
    "Gross Charges when payment arrangements are established \nwith Emory Healthcare."
)
ENLOE_FREE_LIMIT = "Families with income less than 150% of the Federal Poverty Level may be eligible for free care."
ENLOE_HOSPITAL_CHARGES = (
    "## Hospital Charges\n\nFinancially qualified families:\n\n*   " + ENLOE_FREE_LIMIT + "\n*   "
    "Families with incomes between 151% and 300% of the Federal Poverty Level are eligible for "
    "free care after a $100 per visit co-pay.\n*   Families with incomes between 301% and 400% of "
    "the Federal Poverty Level are eligible for Medicare pricing or the self-pay discount of 55%, "
    "whichever is less.\n"
)
LIMA_FREE_LIMIT = (
    "offers emergency and other medically necessary hospital-level services free of charge if the "
    "patient is a resident of Ohio and the patient’s income is at or below 100% of the Federal "
    "Poverty Guidelines (the FPG)"
)
LIMA_AWARDS = (
    "As a participant in the HCAP Program, LMHS " + LIMA_FREE_LIMIT + ". • General Financial "
    "Assistance Policy In addition to HCAP, under LMHS’s Financial Assistance Policy, LMHS "
    "provides financial assistance for emergency and other medically necessary care as a discount "
    "from our gross hospital charges if the patient is a resident in the Hospital Service Area "
    "and meets one of the following criteria as determined by the patient’s insurance status: "
    "Insured vs. Uninsured o Insured. For those patients who have insurance and do not qualify "
    "for HCAP but fall within 101-200% of the FPG, they will be eligible for a 100% discount on "
    "any co-pay, co-insurance, and deductible after insurance. o Uninsured. For patients without "
    "insurance, a discount of up to 100% will be extended to those whose family income is at or "
    "below 200% of the FPG. Patients whose family income falls between 200% and 400% of the FPG "
    "will receive a standard discount of 95%"
)


_FREE_BAND_FEE_CASES = [
    # University Health Lakewood (260102) published free care to 100% from its 0-100% band, which
    # still charges $5 an office visit and $10 in the emergency department.
    (LAKEWOOD_FREE_BAND, True),
    ("Patients at or below 100% of the FPL pay $5 per outpatient visit and nothing for inpatient "
     "care.", True),
    # Cayuga Medical Center (330307): at or below 100% the patient still owes a nominal payment.
    (CAYUGA_NOMINAL, True),
    ("Patients at or below 200% FPL receive free care; patient fees are waived.", False),
    ("Patients at or below 100% of the FPL receive free care, but patient fees are not waived.",
     True),
    ("Patients at or below 200% of the FPL receive free care with no patient fees.", False),
    ("For incomes up to 200% of the FPL, service fees will not be charged and care is free.",
     False),
    ("For patients with incomes below 200% of the FPL, BronxCare will waive all charges and no "
     "nominal payment will be collected.", False),
    ("For eligible individuals with incomes up to 200% of the FPL: charges waived, no nominal "
     "payments due.", False),
    ("Family gross income at 0 to 200% of FPL Full Financial Assistance; $0 is billable to the "
     "patient.", False),
    ("Patients at or below 200% of the FPL receive a 100% discount. Add $5,680 per person for "
     "families larger than 8 people.", False),
    # Advocate Lutheran General (140223) and Good Samaritan (140288): the dollar figure is an income.
    ("Patients with household income at or below 300% FPL (~$47,880/yr single) qualify for full "
     "financial assistance, meaning zero out-of-pocket cost", False),
    ("Patients at or below 200% of the FPL, about $31,920 per year for one person, receive free "
     "care.", False),
    ("Patients at or below 200% of the FPL, $62,400 per calendar year for four, receive free care.",
     False),
    # A fee priced per period still charges: Exeter (300023) bills "$15.00 per 30 day period".
    ("Patients at or below 100% of the FPL pay a $10 per month fee.", True),
    ("Recurring (such as $15.00 per 30 day \nperiod Oncology/Radiation, Rehab, Sleep, Wound)", True),
    # Lake Charles Memorial (190060): free care "subject to a minimal fee".
    ("Patients whose gross family income does not exceed 201% of the current Federal Poverty "
     "Guidelines (FPG) will qualify for full coverage of any hospital and/or physician charges, "
     "subject to a minimal fee based on a sliding scale.", True),
    # North Shore University (330106): "at no charge or at the nominal payment level" may charge.
    ("Patients whose family income is at or below 100% of the FPG are eligible to receive "
     "emergency or medically necessary services at no charge or at the nominal payment level "
     "defined by the NYSDOH;", True),
    # Linton Regional (351328): "Patients will not be required a nominal fee."
    ("Patients will not be required a nominal fee.", False),
    ("Patients at or below 200% of the FPL will not be charged a nominal fee.", False),
    ("Patients at or below 200% of the FPL will not be charged any patient fees.", False),
    # A clinic's copay with no amount is not caught (Mizell, 010007: hospital care is free).
    ("If the patient's income is under 200% of the FPG, the patient is approved for financial "
     "assistance at 100% with no patient financial responsibility except for any applicable "
     "clinic visit copays.", False),
]  # fmt: skip


@pytest.mark.parametrize(("quote", "expected"), _FREE_BAND_FEE_CASES)
def test_free_band_charges_patient(quote, expected):
    from waive.atlas.verify import free_band_charges_patient

    assert free_band_charges_patient(quote) is expected


_FULL_HELP_ABOVE_CASES = [
    # Emory (110069, and St Francis-Emory, 110129) published free care to 200% from clause b,
    # while clause c of the same policy gives uninsured patients at 201%-350% 100% financial
    # assistance.
    (200, EMORY_AWARDS, True),
    (250, EMORY_AWARDS, False),
    (200, "Patients between 201% and 300% of the FPL receive a 100% discount.", True),
    (100, "Uninsured patients between 101% and 200% of the FPL receive a 100% discount.", True),
    # Enloe (050039): the 151%-300% band is free only after a $100 co-pay a visit.
    (150, ENLOE_HOSPITAL_CHARGES, False),
    # Lima Memorial (360009): the 101%-200% band waives an insured patient's co-pays only.
    (100, LIMA_AWARDS, False),
    # Decision B: "may ... up to 100%" is no free band.
    (200, "Patients whose income is between 201% and 300% of the FPL may be eligible for up to a "
     "100% discount.", False),
    (200, "Patients between 201% and 300% of the FPL may receive up to 100% financial assistance.",
     False),
    (200, "Insured patients between 201% and 300% of the FPL receive a 100% discount.", False),
    (200, "Non-insured patients between 201% and 300% of the FPL receive free care.", True),
    (200, "Patients between 201% and 300% of the FPL receive a 100% discount on deductibles.",
     False),
    (200, "Patients between 201% and 300% of the FPL receive a 100% discount after a $25 per "
     "visit fee.", False),
    (200, "Income 0-200% of FPL: 100% discount. Income 201-300% of FPL: 75% discount. Income "
     "301-400% of FPL: 50% discount.", False),
]  # fmt: skip


@pytest.mark.parametrize(("value", "document", "expected"), _FULL_HELP_ABOVE_CASES)
def test_full_help_band_above(value, document, expected):
    from waive.atlas.verify import full_help_band_above

    assert full_help_band_above(Decimal(value), document) is expected


def with_free_limit_in(value, quote, state):
    sheet = with_free_limit(value, quote)
    return sheet.model_copy(update={"hospital": sheet.hospital.model_copy(update={"state": state})})


_MISREAD_FREE_LIMITS = [
    ("FREE_BAND_FEE_REASON", 100, LAKEWOOD_FREE_BAND, "MO", LAKEWOOD_TIERS),
    ("FREE_BAND_FEE_REASON", 100, CAYUGA_NOMINAL, "NY", CAYUGA_NOMINAL),
    ("FREE_LIMIT_BELOW_FULL_HELP_REASON", 200, EMORY_FREE_LIMIT, "GA", EMORY_AWARDS),
]


@pytest.mark.parametrize(("reason", "value", "quote", "state", "document"), _MISREAD_FREE_LIMITS)
def test_verify_sheet_rejects_a_misread_free_care_limit(reason, value, quote, state, document):
    from waive.atlas import verify

    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + document + "\n"}
    report = verify_sheet(with_free_limit_in(value, quote, state), docs)
    assert ("eligibility.free_care_max_fpl", getattr(verify, reason)) in report.rejected


_FREE_LIMITS_READ_RIGHT = [
    (150, ENLOE_FREE_LIMIT, "CA", ENLOE_HOSPITAL_CHARGES),
    (100, LIMA_FREE_LIMIT, "OH", LIMA_AWARDS),
    (200, "Patients at or below 200% of the FPL receive free care. Patients between 201% and 300% "
     "of the FPL may receive up to 100% financial assistance.", "MA", ""),
]  # fmt: skip


@pytest.mark.parametrize(("value", "quote", "state", "document"), _FREE_LIMITS_READ_RIGHT)
def test_verify_sheet_keeps_a_free_care_limit_read_right(value, quote, state, document):
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT + "\n" + document + "\n" + quote + "\n"}
    report = verify_sheet(with_free_limit_in(value, quote, state), docs)
    assert not [r for r in report.rejected if r[0] == "eligibility.free_care_max_fpl"]
