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
    from waive.atlas.verify import INSURED_REASON, quote_addresses_insured

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
        == INSURED_REASON
    )
    assert (
        verdict(
            path,
            False,
            "In order to receive CCP financial assistance, the patient must apply for Medicaid and be denied.",
        )
        == INSURED_REASON
    )
    # A false value needs the quote to exclude insured patients, not merely to mention them.
    assert (
        verdict(
            path,
            False,
            "Financial Assistance is offered to patients who are uninsured and underinsured.",
        )
        == INSURED_REASON
    )
    assert (
        verdict(
            path,
            False,
            "Does not have any form of insurance to cover services rendered that are medically necessary",
        )
        == "accepted"
    )
    assert (
        verdict(
            path,
            False,
            "It is also not intended to provide discounts on insurance co-payments, co-insurance, or deductibles.",
        )
        == "accepted"
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
    assert not quote_addresses_insured("care for all uninsured patients", True)
    assert quote_addresses_insured("even if you have some insurance", True)


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
