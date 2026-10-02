from datetime import date
from decimal import Decimal

from waive.atlas.samples import SAMPLE_SOURCE_ID, st_example_sheet
from waive.atlas.schema import Cited
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
