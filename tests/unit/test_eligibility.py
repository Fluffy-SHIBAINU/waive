from datetime import date
from decimal import Decimal

from waive.atlas.samples import SAMPLE_SOURCE_ID, st_example_sheet
from waive.atlas.schema import Cited, DiscountTier, Eligibility, Programs
from waive.rules.eligibility import Household, Tier, evaluate_eligibility

GUIDELINE_1 = Decimal("15960")
TODAY = date(2026, 10, 2)


def household(income, size=1, state="MA", programs=()):
    return Household(size=size, annual_income=income, state=state, programs=programs)


def with_eligibility(**changes):
    sheet = st_example_sheet()
    return sheet.model_copy(update={"eligibility": sheet.eligibility.model_copy(update=changes)})


def test_rosa_gets_free_care_with_citation():
    result = evaluate_eligibility(st_example_sheet(), household(Decimal("22800")))
    assert result.tier is Tier.FREE
    assert result.fpl_percent == Decimal("142.86")
    assert result.discount_percent == 100
    assert result.reasons[0].field_path == "eligibility.free_care_max_fpl"
    assert "250%" in result.reasons[0].quote


def test_exactly_at_free_limit_is_free():
    result = evaluate_eligibility(st_example_sheet(), household(GUIDELINE_1 * Decimal("2.5")))
    assert result.tier is Tier.FREE


def test_discount_tier():
    result = evaluate_eligibility(st_example_sheet(), household(GUIDELINE_1 * 3))
    assert (result.tier, result.discount_percent) == (Tier.DISCOUNT, 60)
    assert result.reasons[0].field_path == "eligibility.discount_tiers"


def test_above_all_limits_is_not_eligible():
    result = evaluate_eligibility(st_example_sheet(), household(GUIDELINE_1 * 5))
    assert result.tier is Tier.NOT_ELIGIBLE
    assert result.fpl_percent == Decimal("500.00")


def test_missing_income_needs_info():
    result = evaluate_eligibility(st_example_sheet(), household(None))
    assert result.tier is Tier.NEEDS_INFO
    assert result.missing == ("household income",)


def test_presumptive_program_wins_over_income():
    result = evaluate_eligibility(
        st_example_sheet(), household(GUIDELINE_1 * 5, programs=("snap",))
    )
    assert result.tier is Tier.FREE
    assert result.reasons[0].field_path == "programs.presumptive"


def test_residency_mismatch_is_not_eligible():
    residency = Cited[list[str]](
        value=["MA"],
        quote="Massachusetts residents only",
        source_id=SAMPLE_SOURCE_ID,
        checked_on=TODAY,
    )
    sheet = with_eligibility(residency=residency)
    result = evaluate_eligibility(sheet, household(Decimal("1000"), state="NH"))
    assert result.tier is Tier.NOT_ELIGIBLE
    assert result.reasons[0].field_path == "eligibility.residency"


def test_sheet_without_limits_is_unknown():
    sheet = with_eligibility(free_care_max_fpl=None, discount_tiers=None)
    result = evaluate_eligibility(sheet, household(Decimal("1000")))
    assert result.tier is Tier.UNKNOWN
    assert result.missing == ("hospital income limits",)


def test_asset_test_adds_warning():
    assets = Cited[bool](
        value=True, quote="assets are considered", source_id=SAMPLE_SOURCE_ID, checked_on=TODAY
    )
    result = evaluate_eligibility(with_eligibility(asset_test=assets), household(Decimal("22800")))
    assert result.tier is Tier.FREE
    assert result.warnings == ("This hospital also looks at savings and other assets.",)


def free_limit(value):
    return Cited[Decimal](
        value=Decimal(value), quote="free care text", source_id=SAMPLE_SOURCE_ID, checked_on=TODAY
    )


def tiers(*bands):
    return Cited[list[DiscountTier]](
        value=[
            DiscountTier(
                min_fpl_exclusive=Decimal(low),
                max_fpl_inclusive=Decimal(high),
                discount_percent=pct,
            )
            for low, high, pct in bands
        ],
        quote="income bands text",
        source_id=SAMPLE_SOURCE_ID,
        checked_on=TODAY,
    )


def test_income_below_the_lowest_documented_band_is_unknown_not_a_denial():
    # Tufts (220116): the extraction kept only the paid (150, 300] band; the sheet is silent on
    # lower incomes, so the engine must not turn silence into "does not qualify".
    sheet = with_eligibility(free_care_max_fpl=None, discount_tiers=tiers((150, 300, 30)))
    low = evaluate_eligibility(sheet, household(GUIDELINE_1))
    assert low.tier is Tier.UNKNOWN and low.fpl_percent == Decimal("100.00")
    assert low.missing and "100%" in low.missing[0]
    assert "helps up to" not in low.reasons[0].text
    assert low.reasons[0].field_path == "eligibility.discount_tiers"
    assert evaluate_eligibility(sheet, household(Decimal("0"))).tier is Tier.UNKNOWN
    inside = evaluate_eligibility(sheet, household(GUIDELINE_1 * Decimal("1.5") + 1))
    assert (inside.tier, inside.discount_percent) == (Tier.DISCOUNT, 30)
    above = evaluate_eligibility(sheet, household(GUIDELINE_1 * 3 + 1))
    assert above.tier is Tier.NOT_ELIGIBLE and "helps up to 300%" in above.reasons[0].text


def test_income_in_a_gap_between_bands_is_unknown():
    # Mercy (220066): free care to 100%, a paid band from 200%; the 101-200% band was dropped.
    sheet = with_eligibility(
        free_care_max_fpl=free_limit(100), discount_tiers=tiers((200, 250, 15))
    )
    gap = evaluate_eligibility(sheet, household(GUIDELINE_1 * Decimal("1.5")))
    assert gap.tier is Tier.UNKNOWN and gap.fpl_percent == Decimal("150.00")
    assert "150%" in gap.missing[0]
    assert evaluate_eligibility(sheet, household(GUIDELINE_1)).tier is Tier.FREE
    assert evaluate_eligibility(sheet, household(GUIDELINE_1 * Decimal("2.6"))).tier is (
        Tier.NOT_ELIGIBLE
    )


def test_a_band_starting_at_zero_includes_a_household_with_no_income():
    # Berkshire (220046): "0 – 400 46.07%" reads as a band from zero income.
    sheet = with_eligibility(free_care_max_fpl=None, discount_tiers=tiers((0, 400, 46)))
    none = evaluate_eligibility(sheet, household(Decimal("0")))
    assert (none.tier, none.discount_percent, none.fpl_percent) == (
        Tier.DISCOUNT,
        46,
        Decimal("0.00"),
    )
    assert evaluate_eligibility(sheet, household(GUIDELINE_1 * 4 + 1)).tier is Tier.NOT_ELIGIBLE


def test_a_paid_band_is_never_overridden_by_a_free_limit_that_swallows_it():
    # Brigham (220110): free=300 was a misread eligibility ceiling; the table's paid bands end at
    # 300 too. Inside a paid band the answer is the band's discount, never "free".
    sheet = with_eligibility(
        free_care_max_fpl=free_limit(300), discount_tiers=tiers((150, 250, 85), (250, 300, 70))
    )
    result = evaluate_eligibility(sheet, household(GUIDELINE_1 * Decimal("2.25")))
    assert (result.tier, result.discount_percent) == (Tier.DISCOUNT, 85)
    assert evaluate_eligibility(sheet, household(GUIDELINE_1)).tier is Tier.FREE


# Decision 7.9a-2 (task 7.14): a policy that states only the income up to which any help is given.
CEILING_QUOTE = (
    "Patients with household income up to 400% of the Federal Poverty Guidelines may qualify for "
    "financial assistance."
)


def ceiling_only_sheet(limit="400", quote=CEILING_QUOTE):
    """St. Example as a hospital whose documents state only the ceiling on any help: no
    free-care band, no table, and no presumptive programs (the sample's promise free care)."""
    sheet = st_example_sheet()
    ceiling = Cited[Decimal](
        value=Decimal(limit), quote=quote, source_id=SAMPLE_SOURCE_ID, checked_on=TODAY
    )
    return sheet.model_copy(
        update={
            "eligibility": Eligibility(assistance_ceiling_fpl=ceiling),
            "programs": Programs(),
        }
    )


def test_income_at_or_below_the_ceiling_gets_help_whose_amount_the_hospital_decides():
    result = evaluate_eligibility(ceiling_only_sheet(), household(GUIDELINE_1 * 3))
    assert result.tier is Tier.ASSISTANCE
    assert (result.fpl_percent, result.discount_percent) == (Decimal("300.00"), None)
    [reason] = result.reasons
    assert reason.field_path == "eligibility.assistance_ceiling_fpl"
    assert (reason.quote, reason.source_id) == (CEILING_QUOTE, SAMPLE_SOURCE_ID)
    assert reason.text == (
        "Income is 300% of the poverty line; assistance exists up to 400% of the poverty level, "
        "and the hospital decides the amount."
    )
    assert "free" not in reason.text.lower()
    # No income: the household's own figure is still needed, as for any sheet.
    needs = evaluate_eligibility(ceiling_only_sheet(), household(None))
    assert (needs.tier, needs.missing) == (Tier.NEEDS_INFO, ("household income",))


def test_the_ceiling_itself_is_inside_and_one_dollar_more_is_not():
    at = evaluate_eligibility(ceiling_only_sheet(), household(GUIDELINE_1 * 4))
    assert (at.tier, at.fpl_percent) == (Tier.ASSISTANCE, Decimal("400.00"))
    above = evaluate_eligibility(ceiling_only_sheet(), household(GUIDELINE_1 * 4 + 1))
    assert above.tier is Tier.NOT_ELIGIBLE and above.fpl_percent == Decimal("400.01")
    # Above it: the same "likely not eligible under this policy" answer as any other limit.
    [reason] = above.reasons
    assert reason.text == "Income is 400% of the poverty line; this policy helps up to 400%."
    assert reason.field_path == "eligibility.assistance_ceiling_fpl"
    assert reason.quote == CEILING_QUOTE


def test_a_presumptive_program_on_a_ceiling_only_sheet_never_promises_free_care():
    presumptive = Cited[list[str]](
        value=["SNAP"],
        quote="Patients enrolled in SNAP are presumed eligible for financial assistance",
        source_id=SAMPLE_SOURCE_ID,
        checked_on=TODAY,
    )
    sheet = ceiling_only_sheet().model_copy(update={"programs": Programs(presumptive=presumptive)})
    enrolled = household(GUIDELINE_1 * 5, programs=("snap",))
    result = evaluate_eligibility(sheet, enrolled)
    assert (result.tier, result.discount_percent) == (Tier.ASSISTANCE, None)
    assert result.reasons[0].field_path == "programs.presumptive"
    assert result.reasons[0].text == (
        "People enrolled in SNAP qualify for assistance without an income check; the hospital "
        "decides the amount."
    )
    # With a free-care band or a table the presumptive answer is free care, as before.
    assert evaluate_eligibility(st_example_sheet(), enrolled).tier is Tier.FREE


def test_a_ceiling_beside_a_band_or_a_table_changes_no_answer():
    # The structurer keeps the ceiling off such sheets; were one there, the band and the table
    # still decide, and an income above them is not eligible, not "assistance".
    ceiling = ceiling_only_sheet("600").eligibility.assistance_ceiling_fpl
    sheet = with_eligibility(assistance_ceiling_fpl=ceiling)
    for income, tier in (
        (Decimal("22800"), Tier.FREE),
        (GUIDELINE_1 * 3, Tier.DISCOUNT),
        (GUIDELINE_1 * 5, Tier.NOT_ELIGIBLE),
    ):
        assert evaluate_eligibility(sheet, household(income)).tier is tier
        assert evaluate_eligibility(sheet, household(income)) == evaluate_eligibility(
            st_example_sheet(), household(income)
        )
