import json
from decimal import Decimal

from waive.atlas import repo
from waive.atlas.publish import (
    critical_conflicts,
    decide_status,
    diff_sheets,
    drop_fields,
    export_state,
    publish_sheet,
    resolve_conflicts,
    sheet_inconsistencies,
)
from waive.atlas.samples import st_example_sheet
from waive.atlas.schema import DiscountTier, SheetStatus
from waive.db import init_db, make_engine, session_scope

SAMPLE = st_example_sheet()
HOSPITAL = {
    "ccn": "229999",
    "name": "ST. EXAMPLE MEDICAL CENTER",
    "city": "BOSTON",
    "state": "MA",
    "zip": "02118",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
}
REAL_HOSPITAL = {**HOSPITAL, "ccn": "220031", "name": "REAL GENERAL HOSPITAL", "city": "WORCESTER"}
REAL = SAMPLE.model_copy(
    update={
        "hospital": SAMPLE.hospital.model_copy(
            update={"ccn": "220031", "name": "Real General Hospital", "city": "Worcester"}
        )
    }
)


FREE_CARE = "eligibility.free_care_max_fpl"
PRESUMPTIVE = "programs.presumptive"


def with_free_limit(sheet, value, quote=None):
    update = {"value": Decimal(value)}
    if quote is not None:
        update["quote"] = quote
    cited = sheet.eligibility.free_care_max_fpl.model_copy(update=update)
    return sheet.model_copy(
        update={"eligibility": sheet.eligibility.model_copy(update={"free_care_max_fpl": cited})}
    )


def with_presumptive(sheet, programs):
    cited = sheet.programs.presumptive.model_copy(update={"value": programs})
    return sheet.model_copy(
        update={"programs": sheet.programs.model_copy(update={"presumptive": cited})}
    )


def test_resolve_conflicts_keeps_primary_when_the_tiebreak_agrees_with_it():
    secondary = with_free_limit(SAMPLE, 300)
    merged, remaining, detail = resolve_conflicts(SAMPLE, secondary, SAMPLE, [FREE_CARE])
    assert merged == SAMPLE
    assert remaining == []
    assert detail == {
        FREE_CARE: {"primary": "250", "secondary": "300", "tiebreak": "250", "verdict": "primary"}
    }


def test_resolve_conflicts_takes_the_secondary_cited_when_the_tiebreak_sides_with_it():
    secondary = with_free_limit(SAMPLE, 400, quote="above 250% and at or below 400%")
    tiebreak = with_free_limit(SAMPLE, 400, quote="a different quote from the third model")
    merged, remaining, detail = resolve_conflicts(SAMPLE, secondary, tiebreak, [FREE_CARE])
    assert remaining == []
    assert detail[FREE_CARE]["verdict"] == "secondary"
    assert merged.eligibility.free_care_max_fpl == secondary.eligibility.free_care_max_fpl
    assert merged.eligibility.free_care_max_fpl.quote == "above 250% and at or below 400%"
    # Everything else is still the primary's.
    assert merged.eligibility.discount_tiers == SAMPLE.eligibility.discount_tiers
    assert merged.programs == SAMPLE.programs and merged.apply == SAMPLE.apply


def test_resolve_conflicts_keeps_the_conflict_when_the_tiebreak_settles_nothing():
    secondary = with_free_limit(SAMPLE, 300)
    disagreeing = with_free_limit(SAMPLE, 999)
    missing = drop_fields(SAMPLE, [FREE_CARE])
    for tiebreak, third in ((disagreeing, "999"), (missing, None)):
        merged, remaining, detail = resolve_conflicts(SAMPLE, secondary, tiebreak, [FREE_CARE])
        assert merged == SAMPLE
        assert remaining == [FREE_CARE]
        assert detail[FREE_CARE] == {
            "primary": "250",
            "secondary": "300",
            "tiebreak": third,
            "verdict": "unsettled",
        }


def test_resolve_conflicts_settles_each_path_on_its_own():
    secondary = with_presumptive(with_free_limit(SAMPLE, 300), ["MassHealth", "SNAP", "WIC"])
    tiebreak = with_presumptive(
        SAMPLE, ["Medicare"]
    )  # free-care limit as primary, programs as neither
    merged, remaining, detail = resolve_conflicts(
        SAMPLE, secondary, tiebreak, [FREE_CARE, PRESUMPTIVE]
    )
    assert merged == SAMPLE
    assert remaining == [PRESUMPTIVE]
    assert detail[FREE_CARE]["verdict"] == "primary"
    assert detail[PRESUMPTIVE]["verdict"] == "unsettled"
    assert detail[PRESUMPTIVE]["tiebreak"] == ["Medicare"]


def test_drop_fields_removes_only_named_paths():
    sheet = drop_fields(SAMPLE, ["contacts.phone", "eligibility.discount_tiers"])
    paths = [path for path, _ in sheet.field_paths()]
    assert "contacts.phone" not in paths and "eligibility.discount_tiers" not in paths
    assert "eligibility.free_care_max_fpl" in paths


def test_critical_conflicts_only_on_disagreement():
    assert critical_conflicts(SAMPLE, SAMPLE) == []
    assert critical_conflicts(SAMPLE, with_free_limit(SAMPLE, 300)) == [
        "eligibility.free_care_max_fpl"
    ]
    missing = drop_fields(SAMPLE, ["eligibility.free_care_max_fpl"])
    assert critical_conflicts(SAMPLE, missing) == []


def test_decide_status():
    assert decide_status(SAMPLE, []) is SheetStatus.PUBLISHED
    assert decide_status(SAMPLE, ["eligibility.free_care_max_fpl"]) is SheetStatus.HELD
    no_limits = drop_fields(SAMPLE, ["eligibility.free_care_max_fpl", "eligibility.discount_tiers"])
    assert decide_status(no_limits, []) is SheetStatus.HELD


def with_tiers(sheet, bands):
    tiers = [
        DiscountTier(
            min_fpl_exclusive=Decimal(low), max_fpl_inclusive=Decimal(high), discount_percent=pct
        )
        for low, high, pct in bands
    ]
    cited = sheet.eligibility.discount_tiers.model_copy(update={"value": tiers})
    return sheet.model_copy(
        update={"eligibility": sheet.eligibility.model_copy(update={"discount_tiers": cited})}
    )


def test_a_free_limit_that_swallows_the_discount_table_holds_the_sheet():
    # Brigham (220110): "discounts are limited to incomes up to 300% FPG" was read as free care
    # while the same document's table gives 100% only to 150%, then 85% and 70% up to 300%.
    swallowed = with_tiers(with_free_limit(SAMPLE, 300), [(150, 250, 85), (250, 300, 70)])
    problems = sheet_inconsistencies(swallowed)
    assert len(problems) == 1 and problems[0].startswith("eligibility.free_care_max_fpl: 300%")
    assert "150%" in problems[0]
    assert decide_status(swallowed, []) is SheetStatus.HELD
    assert (
        sheet_inconsistencies(SAMPLE) == []
        and sheet_inconsistencies(with_free_limit(SAMPLE, 250)) == []
    )
    # A limit at the first band's lower bound is the normal shape (free to 250, then 60% off).
    assert decide_status(with_tiers(with_free_limit(SAMPLE, 250), [(250, 400, 60)]), []) is (
        SheetStatus.PUBLISHED
    )


def test_a_very_high_free_limit_with_no_table_is_held_for_review():
    # UMass Memorial (220163): "less than 600% of the federal poverty guidelines" is the program's
    # ceiling (with an AGB cap), not a free-care band.
    tall = drop_fields(with_free_limit(SAMPLE, 600), ["eligibility.discount_tiers"])
    assert decide_status(tall, []) is SheetStatus.HELD
    assert "600%" in sheet_inconsistencies(tall)[0]
    usual = drop_fields(with_free_limit(SAMPLE, 400), ["eligibility.discount_tiers"])
    assert decide_status(usual, []) is SheetStatus.PUBLISHED


def test_diff_sheets():
    assert diff_sheets(SAMPLE, SAMPLE) == {}
    changed = with_free_limit(SAMPLE, 300)
    assert diff_sheets(SAMPLE, changed) == {
        "eligibility.free_care_max_fpl": {"old": "250", "new": "300"}
    }
    assert diff_sheets(None, SAMPLE)["eligibility.free_care_max_fpl"] == {"old": None, "new": "250"}


def test_publish_bumps_version_only_on_change(tmp_path):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        repo.upsert_hospital(session, REAL_HOSPITAL)
        first = publish_sheet(session, REAL)
        again = publish_sheet(session, REAL)
        second = publish_sheet(session, with_free_limit(REAL, 300))
        assert (first.version, again, second.version) == (1, None, 2)
        assert second.diff == {"eligibility.free_care_max_fpl": {"old": "250", "new": "300"}}
        out = tmp_path / "ma.json"
        assert export_state(session, "MA", out) == 1
    data = json.loads(out.read_text())
    assert data["license"] == "CC BY 4.0"
    assert data["sheets"][0]["version"] == 2


def test_export_leaves_the_fictional_demo_hospital_out(tmp_path):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        repo.upsert_hospital(session, REAL_HOSPITAL)
        assert publish_sheet(session, SAMPLE).version == 1
        assert publish_sheet(session, REAL).version == 1
        # The demo sheet is still in the database for the phone demo ...
        assert repo.latest_sheet(session, "229999") is not None
        out = tmp_path / "ma.json"
        # ... but the public export only carries real hospitals.
        assert export_state(session, "MA", out) == 1
    data = json.loads(out.read_text())
    assert [sheet["hospital"]["ccn"] for sheet in data["sheets"]] == ["220031"]
    assert "229999" not in out.read_text()


def test_a_discount_that_rises_with_income_holds_the_sheet():
    # Adventist Health (050013, 7.9): tiers 50/75/75 published from a patient-responsibility
    # table. The publish-time check is the safety net behind verify's patient-share rule.
    upside_down = with_tiers(SAMPLE, [(250, 300, 50), (300, 400, 75)])
    problems = sheet_inconsistencies(upside_down)
    assert len(problems) == 1 and problems[0].startswith("eligibility.discount_tiers:")
    assert "75%" in problems[0] and "50%" in problems[0]
    assert decide_status(upside_down, []) is SheetStatus.HELD
    assert sheet_inconsistencies(with_tiers(SAMPLE, [(250, 300, 75), (300, 400, 50)])) == []
