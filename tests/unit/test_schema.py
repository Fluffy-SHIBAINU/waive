from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from waive.atlas.samples import st_example_sheet
from waive.atlas.schema import Cited, DiscountTier, Eligibility, Layer, ProcedureSheet

TODAY = date(2026, 10, 2)


def test_documented_field_requires_quote_and_source():
    with pytest.raises(ValidationError):
        Cited[int](value=240, checked_on=TODAY)


def test_reported_field_requires_support():
    with pytest.raises(ValidationError):
        Cited[int](value=41, layer=Layer.REPORTED, checked_on=TODAY)
    reported = Cited[int](value=41, layer=Layer.REPORTED, support_count=5, checked_on=TODAY)
    assert reported.support_count == 5


def test_discount_tiers_must_not_overlap():
    tiers = [
        DiscountTier(
            min_fpl_exclusive=Decimal("200"), max_fpl_inclusive=Decimal("300"), discount_percent=50
        ),
        DiscountTier(
            min_fpl_exclusive=Decimal("250"), max_fpl_inclusive=Decimal("400"), discount_percent=25
        ),
    ]
    with pytest.raises(ValidationError):
        Eligibility(
            discount_tiers=Cited[list[DiscountTier]](
                value=tiers, quote="discount tiers text", source_id="s", checked_on=TODAY
            )
        )


def test_sheet_rejects_unknown_source():
    data = st_example_sheet().model_dump()
    data["contacts"]["phone"]["source_id"] = "missing-source"
    with pytest.raises(ValidationError):
        ProcedureSheet.model_validate(data)


def test_field_paths_and_completeness():
    sheet = st_example_sheet()
    paths = [path for path, _ in sheet.field_paths()]
    assert "eligibility.free_care_max_fpl" in paths
    assert "collections.eca_wait_days" in paths
    assert len(paths) == 8
    assert sheet.completeness() == pytest.approx(5 / 6)


def test_sheet_round_trips_through_json():
    sheet = st_example_sheet()
    assert ProcedureSheet.model_validate_json(sheet.model_dump_json()) == sheet
