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
