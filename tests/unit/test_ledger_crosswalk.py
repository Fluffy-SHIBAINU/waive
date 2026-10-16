"""CCN to EIN and NPI crosswalk (Phase 10): Community Benefit Insight by CCN, PECOS siblings,
name and ZIP, fuzzy name in the same city, and hand-set rows. No sockets: respx."""

import csv
import json
import logging
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from waive.atlas import repo as atlas_repo
from waive.db import init_db, make_engine, session_scope
from waive.ledger import crosswalk
from waive.ledger import repo as ledger_repo
from waive.ledger.tables import HospitalIdRow

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ledger"
TODAY = date(2026, 10, 17)
ENROLLMENTS_VINTAGE = "2026-07-31, file of 2026-08-01"
# What download.vintage_of returns for a real Hospital Enrollments entry: with the brief's prefix
# it is 66 characters, more than hospital_ids.npi_vintage holds.
REAL_VINTAGE = "2026-08-01 to 2026-08-31, file of 2026-08-17"
COLUMNS = HospitalIdRow.__table__.c
COMMON = {
    "state": "MA",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
}
HOSPITALS = [
    {**COMMON, "ccn": "220001", "name": "HEALTHALLIANCE-CLINTON", "city": "LEOMINSTER", "zip": "01453"},
    {**COMMON, "ccn": "220002", "name": "BAYSTATE WING HOSPITAL", "city": "PALMER", "zip": "01069"},
    {**COMMON, "ccn": "220003", "name": "MOUNT AUBURN HOSPITAL", "city": "CAMBRIDGE", "zip": "02138"},
    {**COMMON, "ccn": "220004", "name": "STURDY MEMORIAL HOSPITAL", "city": "Attleboro", "zip": "02703"},
    {**COMMON, "ccn": "220005", "name": "ANNA JAQUES HOSPITAL", "city": "NEWBURYPORT", "zip": "01950"},
    {**COMMON, "ccn": "220006", "name": "SOUTHCOAST HOSPITAL", "city": "FALL RIVER", "zip": "02720"},
    {**COMMON, "ccn": "220007", "name": "BID HOSPITAL - NEEDHAM", "city": "NEEDHAM", "zip": "02492"},
    {**COMMON, "ccn": "229999", "name": "DEMO HOSPITAL", "city": "BOSTON", "zip": "02110"},
    {**COMMON, "ccn": "310001", "name": "JERSEY GENERAL", "city": "NEWARK", "zip": "07101", "state": "NJ"},
]  # fmt: skip
CATALOG = {
    "dataset": [
        {
            "title": "Hospital Enrollments : 2026-04-30",
            "modified": "2026-05-02",
            "temporal": [{"startDate": "2026-04-30", "endDate": "2026-04-30"}],
            "distribution": [
                {"accessURL": "https://data.cms.gov/data-api/v1/dataset/old/data"},
                {
                    "mediaType": "text/csv",
                    "downloadURL": "https://data.cms.gov/f/Hospital_Enrollments_2026.04.30.csv",
                },
            ],
        },
        {
            "title": "Hospital Enrollments : 2026-07-31",
            "modified": "2026-08-01",
            "temporal": [{"startDate": "2026-07-31", "endDate": "2026-07-31"}],
            "distribution": [
                {"accessURL": "https://data.cms.gov/data-api/v1/dataset/new/data"},
                {
                    "mediaType": "text/csv",
                    "downloadURL": "https://data.cms.gov/f/Hospital_Enrollments_2026.07.31.csv",
                },
            ],
        },
        {"title": "Something else", "modified": "2026-10-06", "distribution": []},
    ]
}


def cbi_rows():
    return json.loads((FIXTURES / "cbi_MA.json").read_text(encoding="utf-8"))


def enrollment_rows():
    with (FIXTURES / "enrollments.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def engine_with_registry():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        for hospital in HOSPITALS:
            atlas_repo.upsert_hospital(session, hospital)
    return engine


def run_ma(engine):
    with session_scope(engine) as session:
        crosswalk.set_manual(session, "220006", "04-2104345", TODAY)
        return crosswalk.crosswalk_state(
            session,
            "MA",
            cbi_rows(),
            enrollment_rows(),
            TODAY,
            enrollments_vintage=ENROLLMENTS_VINTAGE,
        )


def ids(engine, ccn):
    with session_scope(engine) as session:
        row = ledger_repo.get_hospital_id(session, ccn)
        session.expunge(row)
        return row


def test_normalise_ein_pads_to_nine_digits_and_rejects_the_rest():
    assert crosswalk.normalise_ein("912155626") == "912155626"
    assert crosswalk.normalise_ein("04-2103606") == "042103606"
    assert crosswalk.normalise_ein("42103606") == "042103606"
    assert crosswalk.normalise_ein(" 4 2103606 ") == "042103606"
    assert crosswalk.normalise_ein("1234567890") is None
    # Seven digits is a typo, not a lost zero: no EIN starts with "00".
    assert crosswalk.normalise_ein("1234567") is None
    assert crosswalk.normalise_ein("") is None
    assert crosswalk.normalise_ein(None) is None


def test_normalise_name_drops_punctuation_and_common_words():
    assert crosswalk.normalise_name("Mount Auburn Hospital, Inc.") == "MOUNT AUBURN"
    assert crosswalk.normalise_name("The  Sturdy Memorial Hospital & Medical Center") == (
        "STURDY MEMORIAL"
    )
    assert crosswalk.normalise_name("HealthAlliance-Clinton") == "HEALTHALLIANCE CLINTON"
    assert crosswalk.normalise_name("UMass Memorial Health System LLC") == "UMASS MEMORIAL"
    assert crosswalk.normalise_name("") == ""


def test_cbi_match_by_ccn_takes_the_main_location_npi():
    engine = engine_with_registry()
    run_ma(engine)
    row = ids(engine, "220001")
    assert (row.ein, row.match_method, row.match_confidence) == ("042103606", "cbi", 1.0)
    assert row.matched_name == "HealthAlliance-Clinton Hospital"
    assert row.ein_vintage == "Community Benefit Insight, updated 2024-12-06"
    # Two practice locations; the MAIN one carries the hospital's NPI.
    assert (row.npi, row.associate_id, row.nonprofit_flag) == ("1013995133", "8224084090", "N")
    assert row.npi_vintage == "Hospital Enrollments, " + ENROLLMENTS_VINTAGE
    assert row.checked_on == TODAY


def test_sibling_with_the_same_associate_id_inherits_the_cbi_ein():
    engine = engine_with_registry()
    run_ma(engine)
    row = ids(engine, "220002")
    assert (row.ein, row.match_method, row.match_confidence) == ("042103606", "enrollments", 0.9)
    assert row.matched_name == "HealthAlliance-Clinton Hospital"
    assert row.ein_vintage == "Community Benefit Insight, updated 2024-12-06"
    assert row.npi == "1234567893"


def test_name_and_zip_match_keeps_the_raw_update_text_when_it_does_not_parse():
    engine = engine_with_registry()
    run_ma(engine)
    row = ids(engine, "220003")
    assert (row.ein, row.match_method, row.match_confidence) == ("042104785", "name_zip", 0.8)
    assert row.matched_name == "Mount Auburn Hospital, Inc."
    assert row.ein_vintage == "Community Benefit Insight, updated 2024-12"


def test_fuzzy_match_in_the_same_city_pads_a_short_ein():
    engine = engine_with_registry()
    run_ma(engine)
    row = ids(engine, "220004")
    assert (row.ein, row.match_method, row.match_confidence) == ("041234567", "fuzzy", 0.7)
    assert row.matched_name == "Sturdy Memorial Hospital Association"


def test_unmatched_hospital_still_gets_its_npi_and_names_its_best_candidate():
    engine = engine_with_registry()
    report = run_ma(engine)
    row = ids(engine, "220005")
    assert (row.ein, row.match_method, row.match_confidence, row.matched_name) == (
        None,
        None,
        None,
        None,
    )
    assert (row.npi, row.associate_id, row.nonprofit_flag) == ("1567890123", "8300000003", "N")
    assert row.checked_on == TODAY
    assert report.unmatched_rows == [
        {
            "ccn": "220005",
            "name": "ANNA JAQUES HOSPITAL",
            "city": "NEWBURYPORT",
            "zip": "01950",
            "best_name": "Nantucket Cottage Hospital",
            "best_ein": "043456789",
            "best_score": 35.7,
        }
    ]


def test_a_hospital_enrolled_only_under_a_unit_ccn_takes_that_row():
    engine = engine_with_registry()
    run_ma(engine)
    row = ids(engine, "220007")
    assert (row.ein, row.match_method) == ("042103571", "cbi")
    assert (row.npi, row.associate_id, row.nonprofit_flag) == ("1205893450", "9133037120", "N")


def test_manual_row_keeps_its_ein_and_gains_the_npi():
    engine = engine_with_registry()
    run_ma(engine)
    row = ids(engine, "220006")
    assert (row.ein, row.match_method, row.match_confidence) == ("042104345", "manual", 1.0)
    assert (row.npi, row.nonprofit_flag) == ("1678901234", "P")
    assert row.checked_on == TODAY


def test_report_counts_every_method_and_skips_the_demo_hospital():
    engine = engine_with_registry()
    report = run_ma(engine)
    assert (report.state, report.hospitals, report.matched, report.unmatched) == ("MA", 7, 6, 1)
    assert report.by_method == {
        "cbi": 2,
        "enrollments": 1,
        "name_zip": 1,
        "fuzzy": 1,
        "manual": 1,
    }
    with session_scope(engine) as session:
        assert ledger_repo.get_hospital_id(session, "229999") is None
        assert ledger_repo.get_hospital_id(session, "310001") is None


def test_a_later_state_inherits_from_a_cbi_ein_already_stored():
    engine = engine_with_registry()
    run_ma(engine)
    with session_scope(engine) as session:
        report = crosswalk.crosswalk_state(
            session, "nj", [], enrollment_rows(), TODAY, enrollments_vintage=ENROLLMENTS_VINTAGE
        )
    assert (report.state, report.hospitals, report.matched) == ("NJ", 1, 1)
    assert report.by_method == {"enrollments": 1}
    row = ids(engine, "310001")
    assert (row.ein, row.match_method, row.npi) == ("042103606", "enrollments", "1789012345")


def test_a_cbi_row_with_a_bad_ein_does_not_match():
    engine = engine_with_registry()
    rows = cbi_rows()
    for row in rows:
        if row["medicare_provider_number"] == "220001":
            row["ein"] = "12"
    with session_scope(engine) as session:
        report = crosswalk.crosswalk_state(session, "MA", rows, enrollment_rows(), TODAY)
    assert report.by_method.get("cbi") == 1  # 220007 only; 220001's row is unusable
    assert ids(engine, "220001").ein is None
    # Nothing to inherit either: the sibling is unmatched too.
    assert ids(engine, "220002").ein is None
    assert ids(engine, "220001").npi_vintage == "Hospital Enrollments, "


def test_vintages_never_exceed_their_hospital_ids_columns():
    """SQLite stores a 66-character value in a String(60) column; PostgreSQL refuses the whole
    upsert. The crosswalk fits what it stores to the column, so the suite catches an overflow
    on SQLite and production does not stop on it."""
    engine = engine_with_registry()
    rows = cbi_rows()
    rows[0]["updated_dt"] = "some time in the winter of twenty twenty-four, they said"
    with session_scope(engine) as session:
        crosswalk.crosswalk_state(
            session, "MA", rows, enrollment_rows(), TODAY, enrollments_vintage=REAL_VINTAGE
        )
    row = ids(engine, "220001")
    assert len(row.npi_vintage) <= COLUMNS.npi_vintage.type.length
    assert row.npi_vintage.startswith("Hospital Enrollments, 2026-08-01 to 2026-08-31")
    assert len(row.ein_vintage) <= COLUMNS.ein_vintage.type.length
    assert row.ein_vintage.startswith("Community Benefit Insight, updated some time")


def test_a_rerun_follows_this_runs_cbi_match_not_the_stored_one():
    engine = engine_with_registry()
    run_ma(engine)
    rows = cbi_rows()
    rows[0]["ein"] = "048888888"
    with session_scope(engine) as session:
        crosswalk.crosswalk_state(session, "MA", rows, enrollment_rows(), TODAY)
    assert ids(engine, "220001").ein == "048888888"
    # The two CCNs of one legal entity agree: the sibling follows the fresh match.
    sibling = ids(engine, "220002")
    assert (sibling.ein, sibling.match_method) == ("048888888", "enrollments")


def test_a_hospital_whose_cbi_row_vanished_does_not_inherit_its_own_old_ein():
    engine = engine_with_registry()
    run_ma(engine)
    with session_scope(engine) as session:
        report = crosswalk.crosswalk_state(session, "MA", [], enrollment_rows(), TODAY)
    assert (report.matched, report.by_method) == (1, {"manual": 1})
    for ccn in ("220001", "220002"):
        row = ids(engine, ccn)
        assert (row.ein, row.match_method, row.match_confidence) == (None, None, None)
    assert sorted(r["ccn"] for r in report.unmatched_rows) == [
        "220001",
        "220002",
        "220003",
        "220004",
        "220005",
        "220007",
    ]


def test_fuzzy_needs_the_zip_too_so_a_short_name_does_not_take_a_neighbours_ein():
    # normalise_name("BOSTON MEDICAL CENTER") is "BOSTON", a subset of "BOSTON CHILDREN S", and
    # token_set_ratio scores a subset 100: the same city is not enough in a city of ten hospitals.
    engine = engine_with_registry()
    with session_scope(engine) as session:
        atlas_repo.upsert_hospital(
            session,
            {
                **COMMON,
                "ccn": "220500",
                "name": "BOSTON MEDICAL CENTER",
                "city": "Boston",
                "zip": "02118",
            },
        )
    report = run_ma(engine)
    row = ids(engine, "220500")
    assert (row.ein, row.match_method, row.match_confidence) == (None, None, None)
    assert sorted(r["ccn"] for r in report.unmatched_rows) == ["220005", "220500"]
    boston = next(r for r in report.unmatched_rows if r["ccn"] == "220500")
    assert (boston["best_name"], boston["best_ein"], boston["best_score"]) == (
        "Boston Children's Hospital",
        "042774441",
        100.0,
    )


def test_name_and_zip_do_not_match_on_two_blank_zips():
    engine = engine_with_registry()
    rows = cbi_rows()
    next(r for r in rows if r["name"].startswith("Mount Auburn"))["zip_code"] = ""
    with session_scope(engine) as session:
        atlas_repo.upsert_hospital(session, {"ccn": "220003", "zip": ""})
        crosswalk.crosswalk_state(session, "MA", rows, enrollment_rows(), TODAY)
    row = ids(engine, "220003")
    assert (row.ein, row.match_method, row.npi) == (None, None, "1345678901")


def test_the_first_cbi_row_for_a_ccn_wins_and_the_repeat_is_logged(caplog):
    engine = engine_with_registry()
    with caplog.at_level(logging.WARNING, logger="waive.ledger.crosswalk"):
        run_ma(engine)
    row = ids(engine, "220001")
    assert (row.ein, row.matched_name) == ("042103606", "HealthAlliance-Clinton Hospital")
    # The warning names the rows, never their EINs: the redacting filter would drop it.
    repeats = [r.getMessage() for r in caplog.records if "220001" in r.getMessage()]
    assert len(repeats) == 1 and "UMass Memorial HealthAlliance-Clinton Hospital" in repeats[0]


def test_write_unmatched_writes_the_founder_csv(tmp_path):
    engine = engine_with_registry()
    report = run_ma(engine)
    path = crosswalk.write_unmatched(report, tmp_path / "out" / "unmatched_MA.csv")
    assert path == tmp_path / "out" / "unmatched_MA.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [
        {
            "ccn": "220005",
            "name": "ANNA JAQUES HOSPITAL",
            "city": "NEWBURYPORT",
            "zip": "01950",
            "best_name": "Nantucket Cottage Hospital",
            "best_ein": "043456789",
            "best_score": "35.7",
        }
    ]


def test_set_manual_validates_the_ein_and_the_ccn():
    engine = engine_with_registry()
    with session_scope(engine) as session:
        with pytest.raises(ValueError, match="EIN"):
            crosswalk.set_manual(session, "220001", "12345", TODAY)
        with pytest.raises(ValueError, match="000000"):
            crosswalk.set_manual(session, "000000", "912155626", TODAY)
        # The demo hospital stays out of every ledger table, hand-set rows included.
        with pytest.raises(ValueError, match="229999"):
            crosswalk.set_manual(session, "229999", "912155626", TODAY)
        assert ledger_repo.list_hospital_ids(session) == []
        row = crosswalk.set_manual(session, "220001", "91-2155626", TODAY)
        assert (row.ein, row.match_method, row.match_confidence, row.checked_on) == (
            "912155626",
            "manual",
            1.0,
            TODAY,
        )


@respx.mock
def test_fetch_cbi_downloads_once_and_then_reads_the_cache(tmp_path):
    route = respx.get(crosswalk.CBI_URL.format(state="MA")).mock(
        return_value=httpx.Response(200, json=cbi_rows())
    )
    with httpx.Client() as http:
        rows = crosswalk.fetch_cbi(http, "ma", tmp_path)
        assert [r["medicare_provider_number"] for r in rows[:2]] == ["220001", "220077"]
        assert (tmp_path / "cbi" / "MA.json").exists()
        assert route.calls.last.request.headers["User-Agent"].startswith("Waive/")
        assert crosswalk.fetch_cbi(http, "MA", tmp_path) == rows
        assert route.call_count == 1
        crosswalk.fetch_cbi(http, "MA", tmp_path, refresh=True)
        assert route.call_count == 2


@respx.mock
def test_fetch_enrollments_takes_the_newest_catalogue_entry(tmp_path):
    old = respx.get("https://data.cms.gov/f/Hospital_Enrollments_2026.04.30.csv").mock(
        return_value=httpx.Response(200, content=b"nope")
    )
    new = respx.get("https://data.cms.gov/f/Hospital_Enrollments_2026.07.31.csv").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "enrollments.csv").read_bytes())
    )
    with httpx.Client() as http:
        rows, vintage = crosswalk.fetch_enrollments(http, tmp_path, CATALOG["dataset"])
    assert vintage == "2026-07-31 to 2026-07-31, file of 2026-08-01"
    assert [r["CCN"] for r in rows][:3] == ["220001", "220001", "220002"]
    assert rows[1]["PRACTICE LOCATION TYPE"] == "MAIN/PRIMARY HOSPITAL LOCATION"
    assert (tmp_path / "enrollments" / "Hospital_Enrollments_2026.07.31.csv").exists()
    assert (new.call_count, old.call_count) == (1, 0)


@respx.mock
def test_fetch_enrollments_reads_the_cp1252_file_cms_ships(tmp_path):
    text = (FIXTURES / "enrollments.csv").read_text(encoding="utf-8")
    text = text.replace("ANNA JAQUES HOSPITAL", "ANNA JAQUES HOSPITAL ¿")
    respx.get("https://data.cms.gov/f/Hospital_Enrollments_2026.07.31.csv").mock(
        return_value=httpx.Response(200, content=text.encode("cp1252"))
    )
    with httpx.Client() as http:
        rows, _ = crosswalk.fetch_enrollments(http, tmp_path, CATALOG["dataset"])
    assert rows[5]["ORGANIZATION NAME"] == "ANNA JAQUES HOSPITAL ¿"


def test_fetch_enrollments_needs_the_dataset_in_the_catalogue(tmp_path):
    with httpx.Client() as http:
        with pytest.raises(ValueError, match="Hospital Enrollments"):
            crosswalk.fetch_enrollments(http, tmp_path, [])


@respx.mock
def test_run_crosswalk_fetches_both_files_and_matches(tmp_path):
    respx.get(crosswalk.CBI_URL.format(state="MA")).mock(
        return_value=httpx.Response(200, json=cbi_rows())
    )
    respx.get("https://data.cms.gov/f/Hospital_Enrollments_2026.07.31.csv").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "enrollments.csv").read_bytes())
    )
    engine = engine_with_registry()
    with session_scope(engine) as session, httpx.Client() as http:
        report = crosswalk.run_crosswalk(session, http, "MA", tmp_path, CATALOG["dataset"], TODAY)
    assert (report.hospitals, report.matched, report.unmatched) == (7, 5, 2)
    row = ids(engine, "220001")
    assert row.ein == "042103606"
    assert row.npi_vintage.startswith("Hospital Enrollments, 2026-07-31 to 2026-07-31, file of")
    assert len(row.npi_vintage) <= COLUMNS.npi_vintage.type.length
