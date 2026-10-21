"""`waive atlas export --tender`: the per-state snapshot Tender imports, one row per registry
hospital (Kindred connect, spec 5.3)."""

import csv
import json
from decimal import Decimal

from typer.testing import CliRunner

from waive.atlas import repo
from waive.atlas.publish import (
    TENDER_COLUMNS,
    drop_fields,
    export_state,
    export_tender,
    publish_sheet,
)
from waive.atlas.samples import st_example_sheet
from waive.atlas.schema import Eligibility, SheetStatus, SourceKind
from waive.cli import app
from waive.db import init_db, make_engine, session_scope

SAMPLE = st_example_sheet()
SOURCE = SAMPLE.sources[0]
HEADER = (
    "ccn,name,status,website,fap_url,source_kind,source_title,free_pct,quote,discount_pct,"
    "discount_quote,retrieved_at,system_name,system_size,sheet_version,source_sha256"
)
FIRST = {
    "ccn": "330101",
    "name": "FIRST GENERAL HOSPITAL",
    "city": "ALBANY",
    "state": "NY",
    "zip": "12208",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
    "website_domain": "first.example.org",
    "domain_confidence": 0.95,
    "system": "Example Health",
}
SECOND = {
    **FIRST,
    "ccn": "330102",
    "name": "SECOND COMMUNITY HOSPITAL",
    "city": "BUFFALO",
    "website_domain": "second.example.org",
    "system": None,
}
# Every column after `status` but the registry's system name, which travels on every row.
EMPTY_POLICY = {column: "" for column in TENDER_COLUMNS[3:] if column != "system_name"}


def sheet_for(hospital, **update):
    """The sample sheet re-homed at `hospital`."""
    ref = SAMPLE.hospital.model_copy(
        update={
            "ccn": hospital["ccn"],
            "name": hospital["name"],
            "city": hospital["city"],
            "state": hospital["state"],
            "website_domain": hospital["website_domain"],
            "system": hospital["system"],
        }
    )
    return SAMPLE.model_copy(update={"hospital": ref, **update})


def with_free_limit(sheet, value):
    cited = sheet.eligibility.free_care_max_fpl.model_copy(update={"value": Decimal(value)})
    return sheet.model_copy(
        update={"eligibility": sheet.eligibility.model_copy(update={"free_care_max_fpl": cited})}
    )


def engine_with(*hospitals):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        for hospital in hospitals:
            repo.upsert_hospital(session, hospital)
    return engine


def read_rows(path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_the_snapshot_carries_one_row_per_registry_hospital(tmp_path):
    engine = engine_with(FIRST, SECOND)
    with session_scope(engine) as session:
        assert publish_sheet(session, sheet_for(FIRST)).version == 1
        assert publish_sheet(session, sheet_for(SECOND)).version == 1
        # A later hold withdraws the second hospital's published version.
        assert publish_sheet(session, sheet_for(SECOND, status=SheetStatus.HELD)).version == 2
        out = tmp_path / "tender-ny.csv"
        assert export_tender(session, "NY", out) == 2
    assert out.read_text(encoding="utf-8").splitlines()[0] == HEADER
    assert HEADER.split(",") == list(TENDER_COLUMNS)
    rows = read_rows(out)
    assert [(row["ccn"], row["status"]) for row in rows] == [
        ("330101", "published"),
        ("330102", "held"),
    ]
    first, second = rows
    assert first == {
        "ccn": "330101",
        "name": "FIRST GENERAL HOSPITAL",
        "status": "published",
        "website": "first.example.org",
        "fap_url": SOURCE.url,
        "source_kind": "hospital_web",
        "source_title": "Financial Assistance Policy",
        "free_pct": "250",
        "quote": SAMPLE.eligibility.free_care_max_fpl.quote,
        "discount_pct": "400",
        "discount_quote": SAMPLE.eligibility.discount_tiers.quote,
        "retrieved_at": "2026-10-02",
        "system_name": "Example Health",
        "system_size": "",
        "sheet_version": "1",
        "source_sha256": SOURCE.sha256,
    }
    # The held row names the hospital and none of what it once published: no policy, no
    # website (Tender keeps whatever website it holds), only the registry's system name.
    assert second == {
        "ccn": "330102",
        "name": "SECOND COMMUNITY HOSPITAL",
        "status": "held",
        "system_name": "",
        **EMPTY_POLICY,
    }


def test_unscouted_and_draft_hospitals_appear_with_empty_policy_columns(tmp_path):
    never_scouted = {**FIRST, "ccn": "330103", "name": "THIRD HOSPITAL"}
    elsewhere = {**FIRST, "ccn": "220031", "name": "A MASSACHUSETTS HOSPITAL", "state": "MA"}
    demo = {**FIRST, "ccn": "229999", "name": "ST. EXAMPLE MEDICAL CENTER"}
    engine = engine_with(FIRST, never_scouted, elsewhere, demo)
    with session_scope(engine) as session:
        repo.add_sheet_version(session, sheet_for(FIRST, status=SheetStatus.DRAFT), None)
        publish_sheet(session, sheet_for(elsewhere))
        publish_sheet(session, sheet_for(demo))
        out = tmp_path / "tender-ny.csv"
        # The other state's hospital and the fictional demo hospital stay out.
        assert export_tender(session, "NY", out) == 2
    rows = read_rows(out)
    assert [(row["ccn"], row["status"], row["system_name"]) for row in rows] == [
        ("330101", "draft", "Example Health"),
        ("330103", "unscouted", "Example Health"),
    ]
    for row in rows:
        assert {column: row[column] for column in EMPTY_POLICY} == EMPTY_POLICY


def test_the_website_needs_a_domain_matched_with_confidence_of_at_least_ninety_percent(tmp_path):
    sure = {**FIRST, "domain_confidence": 0.9}
    unsure = {**SECOND, "domain_confidence": 0.85}
    unknown = {**FIRST, "ccn": "330103", "name": "THIRD HOSPITAL", "domain_confidence": None}
    engine = engine_with(sure, unsure, unknown)
    with session_scope(engine) as session:
        for hospital in (sure, unsure, unknown):
            publish_sheet(session, sheet_for(hospital))
        out = tmp_path / "tender-ny.csv"
        assert export_tender(session, "NY", out) == 3
    rows = read_rows(out)
    assert [(row["ccn"], row["status"], row["website"]) for row in rows] == [
        ("330101", "published", "first.example.org"),
        ("330102", "published", ""),
        ("330103", "published", ""),
    ]
    # The policy columns do not depend on the website.
    assert all(row["free_pct"] == "250" and row["fap_url"] == SOURCE.url for row in rows)


def test_a_published_sheet_whose_cited_source_has_no_url_is_exported_as_held(tmp_path):
    # Tender shows a quote only with the URL it was read from; a patient's photo has none.
    photo = SOURCE.model_copy(update={"url": None, "kind": SourceKind.PATIENT_PHOTO})
    engine = engine_with(FIRST)
    with session_scope(engine) as session:
        publish_sheet(session, sheet_for(FIRST, sources=[photo]))
        assert repo.latest_sheet(session, "330101")[0].status is SheetStatus.PUBLISHED
        out = tmp_path / "tender-ny.csv"
        assert export_tender(session, "NY", out) == 1
    (row,) = read_rows(out)
    assert row == {
        "ccn": "330101",
        "name": FIRST["name"],
        "status": "held",
        "system_name": "Example Health",
        **EMPTY_POLICY,
    }


def test_discount_tiers_alone_publish_with_the_discount_columns_and_a_ceiling_alone_is_held(
    tmp_path,
):
    # Spec 5.3: of the sheets with no free-care field, those with a table import the table;
    # those with neither (a ceiling only) are exported held.
    tiers_only = drop_fields(sheet_for(FIRST), ["eligibility.free_care_max_fpl"])
    free = SAMPLE.eligibility.free_care_max_fpl
    ceiling = free.model_copy(update={"value": Decimal(400), "quote": "up to 400% may qualify"})
    ceiling_only = sheet_for(SECOND, eligibility=Eligibility(assistance_ceiling_fpl=ceiling))
    engine = engine_with(FIRST, SECOND)
    with session_scope(engine) as session:
        publish_sheet(session, tiers_only)
        publish_sheet(session, ceiling_only)
        out = tmp_path / "tender-ny.csv"
        assert export_tender(session, "NY", out) == 2
    table, held = read_rows(out)
    assert table == {
        "ccn": "330101",
        "name": "FIRST GENERAL HOSPITAL",
        "status": "published",
        "website": "first.example.org",
        "fap_url": SOURCE.url,
        "source_kind": "hospital_web",
        "source_title": "Financial Assistance Policy",
        "free_pct": "",
        "quote": "",
        "discount_pct": "400",
        "discount_quote": SAMPLE.eligibility.discount_tiers.quote,
        "retrieved_at": "2026-10-02",
        "system_name": "Example Health",
        "system_size": "",
        "sheet_version": "1",
        "source_sha256": SOURCE.sha256,
    }
    assert held == {
        "ccn": "330102",
        "name": SECOND["name"],
        "status": "held",
        "system_name": "",
        **EMPTY_POLICY,
    }


def test_the_json_export_carries_each_sheet_version_diff(tmp_path):
    engine = engine_with(FIRST)
    with session_scope(engine) as session:
        publish_sheet(session, sheet_for(FIRST))
        assert publish_sheet(session, with_free_limit(sheet_for(FIRST), 300)).version == 2
        out = tmp_path / "ny.json"
        assert export_state(session, "NY", out) == 1
    (sheet,) = json.loads(out.read_text(encoding="utf-8"))["sheets"]
    assert sheet["version"] == 2
    assert sheet["diff"] == {"eligibility.free_care_max_fpl": {"old": "250", "new": "300"}}


def test_cli_export_writes_the_json_and_with_tender_the_snapshot(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    url = f"sqlite:///{tmp_path / 'local.db'}"
    monkeypatch.setenv("WAIVE_DATABASE_URL", url)
    engine = make_engine(url)
    init_db(engine)
    with session_scope(engine) as session:
        repo.upsert_hospital(session, FIRST)
        repo.upsert_hospital(session, SECOND)
        publish_sheet(session, sheet_for(FIRST))
    engine.dispose()
    out, tender = tmp_path / "ny.json", tmp_path / "tender-ny.csv"
    result = CliRunner().invoke(
        app, ["atlas", "export", "--state", "NY", "--out", str(out), "--tender", str(tender)]
    )
    assert result.exit_code == 0, result.output
    # Rich wraps the output at 80 columns and may break the long temporary paths mid-word, so
    # the phrases are checked in the text and the paths through the files they name.
    text = " ".join(result.output.split())
    assert "Exported 1 sheets to" in text
    assert "Exported 2 hospitals for Tender to" in text
    assert json.loads(out.read_text(encoding="utf-8"))["sheets"][0]["hospital"]["ccn"] == "330101"
    assert tender.read_text(encoding="utf-8").splitlines()[0] == HEADER
    assert [(row["ccn"], row["status"]) for row in read_rows(tender)] == [
        ("330101", "published"),
        ("330102", "unscouted"),
    ]
    # Without --tender the command writes the JSON alone, as before.
    tender.unlink()
    result = CliRunner().invoke(app, ["atlas", "export", "--state", "NY", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "Tender" not in result.output and not tender.exists()
