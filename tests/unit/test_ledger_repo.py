"""Ledger upserts and reads (Phase 10)."""

from datetime import date

from waive.atlas import repo as atlas_repo
from waive.db import init_db, make_engine, session_scope
from waive.ledger import repo

TODAY = date(2026, 10, 17)
MA = {
    "ccn": "220001",
    "name": "HEALTHALLIANCE-CLINTON",
    "city": "LEOMINSTER",
    "state": "MA",
    "zip": "01453",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
}
NJ = {
    **MA,
    "ccn": "310001",
    "name": "JERSEY GENERAL",
    "city": "NEWARK",
    "state": "NJ",
    "zip": "07101",
}


def engine_with(*hospitals):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        for hospital in hospitals:
            atlas_repo.upsert_hospital(session, hospital)
    return engine


def test_hospital_id_upsert_updates_in_place_and_lists_by_state():
    engine = engine_with(MA, NJ)
    with session_scope(engine) as session:
        repo.upsert_hospital_id(session, "220001", npi="1013995133", checked_on=TODAY)
        repo.upsert_hospital_id(
            session, "220001", ein="912155626", match_method="cbi", match_confidence=1.0
        )
        repo.upsert_hospital_id(session, "310001", checked_on=TODAY)
    with session_scope(engine) as session:
        row = repo.get_hospital_id(session, "220001")
        assert (row.ein, row.npi, row.match_method, row.match_confidence) == (
            "912155626",
            "1013995133",
            "cbi",
            1.0,
        )
        assert [r.ccn for r in repo.list_hospital_ids(session)] == ["220001", "310001"]
        assert [r.ccn for r in repo.list_hospital_ids(session, state="nj")] == ["310001"]
        # Only matched EINs, keyed by CCN.
        assert repo.matched_eins(session) == {"220001": "912155626"}
        assert repo.matched_eins(session, state="NJ") == {}


def test_cost_reports_and_schedule_h_keep_one_row_per_year_and_read_the_newest():
    engine = engine_with(MA)
    with session_scope(engine) as session:
        for year, costs in ((2023, 200), (2024, 300)):
            repo.upsert_cost_report(
                session,
                "220001",
                year,
                rpt_rec_num=str(year),
                fy_begin=date(year - 1, 10, 1),
                fy_end=date(year, 9, 30),
                total_costs=costs,
                charity_care_cost=3,
                hospital_name="X",
                source_file="f.csv",
                vintage="v",
                loaded_on=TODAY,
            )
        repo.upsert_cost_report(session, "220001", 2024, total_costs=301)
        for year in (2023, 2024):
            repo.upsert_schedule_h(
                session,
                "220001",
                year,
                ein="912155626",
                tax_period=f"{year}09",
                object_id=f"obj{year}",
                filer_name="UMass",
                lawsuits=year == 2024,
                vintage="IRS e-file index 2025",
                loaded_on=TODAY,
            )
    with session_scope(engine) as session:
        newest = repo.latest_cost_report(session, "220001")
        assert (newest.fiscal_year, newest.total_costs) == (2024, 301)
        assert repo.all_latest_cost_reports(session)["220001"].fiscal_year == 2024
        assert repo.latest_schedule_h(session, "220001").lawsuits is True
        assert repo.all_latest_schedule_h(session)["220001"].tax_year == 2024
        assert repo.latest_cost_report(session, "000000") is None
        assert repo.latest_schedule_h(session, "000000") is None


def test_exposure_and_metrics_upserts_and_the_state_filtered_listing():
    engine = engine_with(MA, NJ)
    with session_scope(engine) as session:
        repo.upsert_exposure(
            session, "220001", discharges=2775, zips_counted=10, zips_suppressed=2, loaded_on=TODAY
        )
        repo.upsert_exposure(session, "220001", dual_discharges=1160.0)
        repo.upsert_metrics(session, "220001", exposure_rank=0.9, exposure_computed_on=TODAY)
        repo.upsert_metrics(session, "220001", gap=0.5, behaviour_computed_on=TODAY)
        repo.upsert_metrics(session, "310001", exposure_rank=0.2)
    with session_scope(engine) as session:
        exposure = repo.get_exposure(session, "220001")
        assert (exposure.discharges, exposure.dual_discharges) == (2775, 1160.0)
        assert set(repo.all_exposure(session)) == {"220001"}
        metrics = repo.get_metrics(session, "220001")
        assert (metrics.exposure_rank, metrics.gap, metrics.exposure_computed_on) == (
            0.9,
            0.5,
            TODAY,
        )
        listed = repo.list_metrics(session)
        assert [(hospital.ccn, m.exposure_rank) for hospital, m in listed] == [
            ("220001", 0.9),
            ("310001", 0.2),
        ]
        assert [h.ccn for h, _ in repo.list_metrics(session, state="MA")] == ["220001"]
        assert repo.exposure_ranks(session) == {"220001": 0.9, "310001": 0.2}
        assert repo.get_metrics(session, "000000") is None
