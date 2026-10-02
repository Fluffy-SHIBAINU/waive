from datetime import date

import pytest

from waive.atlas import repo
from waive.atlas.samples import SAMPLE_POLICY_TEXT, st_example_sheet
from waive.atlas.schema import SheetStatus
from waive.db import init_db, make_engine, session_scope

HOSPITAL = {
    "ccn": "229999",
    "name": "ST. EXAMPLE MEDICAL CENTER",
    "address": "1 EXAMPLE WAY",
    "city": "BOSTON",
    "state": "MA",
    "zip": "02118",
    "phone": "617-555-0100",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
}


@pytest.fixture
def engine():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return engine


def test_upsert_hospital_is_idempotent(engine):
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        repo.upsert_hospital(session, {**HOSPITAL, "name": "ST. EXAMPLE MEDICAL CENTER INC"})
    with session_scope(engine) as session:
        rows = repo.list_hospitals(session, state="MA")
        assert [row.name for row in rows] == ["ST. EXAMPLE MEDICAL CENTER INC"]
        assert repo.list_hospitals(session, state="MA", missing_domain=True) == rows
        ref = repo.hospital_ref(rows[0])
        assert (ref.ccn, ref.state, ref.phone) == ("229999", "MA", "617-555-0100")


def test_sources_dedupe_by_id_and_link_to_hospitals(engine):
    sheet = st_example_sheet()
    source = sheet.sources[0]
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        repo.upsert_hospital(session, {**HOSPITAL, "ccn": "229998", "name": "ST. EXAMPLE NORTH"})
        repo.save_source(session, source, SAMPLE_POLICY_TEXT, "229999")
        repo.save_source(session, source, SAMPLE_POLICY_TEXT, "229998")
    with session_scope(engine) as session:
        for ccn in ("229999", "229998"):
            [(doc, text)] = repo.sources_for(session, ccn)
            assert doc == source
            assert text == SAMPLE_POLICY_TEXT


def test_sheet_versions_round_trip(engine):
    sheet = st_example_sheet()
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        assert repo.latest_sheet(session, "229999") is None
        repo.add_sheet_version(session, sheet, None)
        second = sheet.model_copy(update={"version": 2, "status": SheetStatus.HELD})
        repo.add_sheet_version(session, second, {"status": {"old": "published", "new": "held"}})
    with session_scope(engine) as session:
        latest, row = repo.latest_sheet(session, "229999")
        assert (latest.version, latest.status) == (2, SheetStatus.HELD)
        assert latest.eligibility == sheet.eligibility
        assert row.diff == {"status": {"old": "published", "new": "held"}}
        assert [s.version for s in repo.list_latest_sheets(session, "MA")] == [2]


def test_review_items(engine):
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        repo.add_review_item(session, "229999", "domain", {"candidates": ["example.org"]})
    with session_scope(engine) as session:
        [item] = repo.open_review_items(session)
        assert (item.ccn, item.kind, item.status) == ("229999", "domain", "open")
        assert item.detail == {"candidates": ["example.org"]}
        assert item.created_at.date() >= date(2026, 10, 2)
