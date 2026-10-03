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
)
from waive.atlas.samples import st_example_sheet
from waive.atlas.schema import SheetStatus
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


def with_free_limit(sheet, value):
    cited = sheet.eligibility.free_care_max_fpl.model_copy(update={"value": Decimal(value)})
    return sheet.model_copy(
        update={"eligibility": sheet.eligibility.model_copy(update={"free_care_max_fpl": cited})}
    )


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
