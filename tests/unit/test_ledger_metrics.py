"""The ledger's computed metrics (Phase 10): percentile ranks, the gap and exposure labels, and
the two computations that fill `ledger_metrics` from the loaders' tables."""

from datetime import date
from decimal import Decimal

from waive.atlas import repo as atlas_repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.db import init_db, make_engine, session_scope
from waive.ledger import repo
from waive.ledger.metrics import (
    GAP_THRESHOLD,
    compute_behaviour,
    compute_exposure_metrics,
    exposure_label,
    gap_label,
    percentile_ranks,
)
from waive.ledger.models import ExposureLabel, GapLabel

TODAY = date(2026, 10, 16)
EARLIER = date(2026, 9, 1)
BASE = {
    "city": "BOSTON",
    "state": "MA",
    "zip": "02118",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
}
# The fictional demo hospital: seeded, published, and skipped by every computation.
DEMO = {**BASE, "ccn": "229999", "name": "ST. EXAMPLE MEDICAL CENTER"}
SHEETED = {**BASE, "ccn": "220031", "name": "REAL GENERAL HOSPITAL", "city": "WORCESTER"}
FILED = {**BASE, "ccn": "220032", "name": "FILED MEMORIAL", "city": "LOWELL"}
COSTED = {**BASE, "ccn": "220033", "name": "COSTED COMMUNITY", "city": "SALEM"}
BARE = {**BASE, "ccn": "220034", "name": "BARE HOSPITAL", "city": "LYNN"}


def real_sheet(fpl: str = "400"):
    sample = st_example_sheet()
    hospital = sample.hospital.model_copy(
        update={"ccn": "220031", "name": "Real General Hospital", "city": "Worcester"}
    )
    limit = sample.eligibility.free_care_max_fpl.model_copy(update={"value": Decimal(fpl)})
    eligibility = sample.eligibility.model_copy(update={"free_care_max_fpl": limit})
    return sample.model_copy(update={"hospital": hospital, "eligibility": eligibility})


def engine_with(*hospitals):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        for hospital in hospitals:
            atlas_repo.upsert_hospital(session, hospital)
    return engine


def seed_behaviour(engine):
    """One hospital per input shape: a published sheet with a cost report, a Schedule H alone,
    a cost report with a flag-free Schedule H, nothing at all, and the demo hospital."""
    with session_scope(engine) as session:
        publish_sheet(session, st_example_sheet())
        publish_sheet(session, real_sheet("400"))
        repo.upsert_cost_report(
            session,
            "220031",
            2024,
            fy_end=date(2024, 9, 30),
            charity_care_cost=10,
            total_costs=1000,
            bad_debt=36,
            net_patient_revenue=1000,
            vintage="HCRIS 2024",
        )
        repo.upsert_cost_report(
            session, "220031", 2023, charity_care_cost=500, total_costs=1000, bad_debt=0
        )
        repo.upsert_schedule_h(
            session,
            "220032",
            2024,
            ein="043145034",
            free_care_fpl_pct=300.0,
            charity_pct_of_expense=1.1,
            legal_action=True,
            credit_reporting=True,
            sells_debt=False,
            flag_line="engaged",
            permitted_actions="credit_reporting,legal_action",
            vintage="IRS e-file index 2025",
        )
        repo.upsert_cost_report(
            session,
            "220033",
            2024,
            fy_end=date(2024, 12, 31),
            charity_care_cost=50,
            total_costs=1000,
            bad_debt=10,
            net_patient_revenue=1000,
        )
        repo.upsert_schedule_h(
            session,
            "220033",
            2023,
            lawsuits=False,
            liens=False,
            credit_reporting=False,
            sells_debt=False,
            legal_action=False,
            flag_line="engaged",
            permitted_actions="none",
        )
        # The demo hospital has every input and must still get no row.
        repo.upsert_cost_report(session, "229999", 2024, charity_care_cost=1, total_costs=10)
        # Exposure columns written before the behaviour run must survive it.
        repo.upsert_metrics(session, "220031", exposure_rank=0.9, exposure_computed_on=EARLIER)


def test_percentile_ranks_share_ties_and_need_two_hospitals():
    assert percentile_ranks({}) == {}
    assert percentile_ranks({"a": 5.0}) == {}
    assert percentile_ranks({"a": 1.0, "b": 2.0}) == {"a": 0.0, "b": 1.0}
    ranks = percentile_ranks({"a": 1.0, "b": 2.0, "c": 2.0, "d": 3.0, "e": 0.5})
    assert ranks == {"e": 0.0, "a": 0.25, "b": 0.5, "c": 0.5, "d": 1.0}


def test_labels_turn_at_the_thresholds():
    assert GAP_THRESHOLD == 0.25
    assert gap_label(0.25) is GapLabel.PROMISES_MORE
    assert gap_label(0.249) is GapLabel.DELIVERS
    assert gap_label(0.0) is GapLabel.DELIVERS
    assert gap_label(-0.249) is GapLabel.DELIVERS
    assert gap_label(-0.25) is GapLabel.PROMISES_LESS
    assert exposure_label(1.0) is ExposureLabel.HIGH
    assert exposure_label(2 / 3) is ExposureLabel.HIGH
    assert exposure_label(0.66) is ExposureLabel.MEDIUM
    assert exposure_label(1 / 3) is ExposureLabel.MEDIUM
    assert exposure_label(0.33) is ExposureLabel.LOW
    assert exposure_label(0.0) is ExposureLabel.LOW


def test_compute_behaviour_ranks_the_gap_and_collections_and_leaves_exposure_alone():
    engine = engine_with(DEMO, SHEETED, FILED, COSTED, BARE)
    seed_behaviour(engine)
    with session_scope(engine) as session:
        report = compute_behaviour(session, TODAY)
    assert (report.hospitals, report.with_stated, report.stated_from_sheet) == (4, 2, 1)
    assert (report.stated_from_990, report.with_cost_report, report.with_schedule_h) == (1, 2, 2)
    assert (report.gap_known, report.gap_partial) == (1, 2)
    assert (report.collections_known, report.collections_partial) == (1, 2)
    with session_scope(engine) as session:
        sheeted = repo.get_metrics(session, "220031")
        filed = repo.get_metrics(session, "220032")
        costed = repo.get_metrics(session, "220033")
        assert repo.get_metrics(session, "220034") is None
        assert repo.get_metrics(session, "229999") is None
        # The sheet's limit beats the filing's; the newest cost report (2024) is the one used.
        assert (sheeted.stated_fpl, sheeted.stated_source) == (400.0, "sheet")
        assert (filed.stated_fpl, filed.stated_source) == (300.0, "irs990")
        assert (costed.stated_fpl, costed.stated_source) == (None, None)
        assert (sheeted.stated_rank, sheeted.stated_peers) == (1.0, 2)
        assert (filed.stated_rank, filed.stated_peers) == (0.0, 2)
        assert (sheeted.charity_cost_pct, sheeted.cost_report_year) == (1.0, 2024)
        assert (costed.charity_cost_pct, costed.cost_report_year) == (5.0, 2024)
        assert (sheeted.charity_rank, costed.charity_rank, sheeted.charity_peers) == (0.0, 1.0, 2)
        assert filed.charity_pct_990 == 1.1 and filed.charity_cost_pct is None
        assert (sheeted.gap, sheeted.gap_label) == (1.0, "promises_more_delivers_less")
        assert (filed.gap, filed.gap_label, costed.gap) == (None, None, None)
        # Collections: the flags come from the newest filing, bad debt from the newest report.
        assert (filed.legal_action, filed.credit_reporting, filed.sells_debt) == (
            True,
            True,
            False,
        )
        assert (filed.flag_line, filed.permitted_actions, filed.schedule_h_year) == (
            "engaged",
            "credit_reporting,legal_action",
            2024,
        )
        assert (filed.bad_debt_pct, filed.bad_debt_rank) == (None, None)
        assert (sheeted.bad_debt_pct, sheeted.bad_debt_rank, sheeted.bad_debt_peers) == (
            3.6,
            1.0,
            2,
        )
        assert (costed.bad_debt_pct, costed.bad_debt_rank) == (1.0, 0.0)
        assert costed.lawsuits is False and costed.flag_line == "engaged"
        assert sheeted.flag_line is None and sheeted.schedule_h_year is None
        assert all(row.behaviour_computed_on == TODAY for row in (sheeted, filed, costed))
        # The exposure columns written earlier are untouched.
        assert (sheeted.exposure_rank, sheeted.exposure_computed_on) == (0.9, EARLIER)
        assert filed.exposure_rank is None and filed.exposure_computed_on is None


def test_compute_behaviour_alone_in_the_country_has_a_value_but_no_rank():
    engine = engine_with(SHEETED)
    with session_scope(engine) as session:
        publish_sheet(session, real_sheet("250"))
        compute_behaviour(session, TODAY)
    with session_scope(engine) as session:
        row = repo.get_metrics(session, "220031")
        assert (row.stated_fpl, row.stated_rank, row.stated_peers) == (250.0, None, None)
        assert (row.gap, row.gap_label) == (None, None)


def test_compute_exposure_metrics_scores_ranks_and_labels_without_touching_behaviour():
    engine = engine_with(DEMO, SHEETED, FILED, COSTED, BARE)
    with session_scope(engine) as session:
        repo.upsert_exposure(
            session, "220031", discharges=1850, dual_discharges=770.4, poor_seniors_served=8397.0
        )
        repo.upsert_exposure(session, "220032", discharges=400, dual_discharges=100.0)
        repo.upsert_exposure(session, "220033", poor_seniors_served=20000.0)
        repo.upsert_exposure(session, "220034", discharges=50, zips_suppressed=3)
        repo.upsert_exposure(
            session, "229999", discharges=9, dual_discharges=9.0, poor_seniors_served=99999.0
        )
        repo.upsert_metrics(session, "220031", gap=0.5, behaviour_computed_on=EARLIER)
        report = compute_exposure_metrics(session, TODAY)
    assert (report.hospitals, report.scored, report.with_dual, report.with_poor_seniors) == (
        4,
        3,
        2,
        2,
    )
    with session_scope(engine) as session:
        sheeted = repo.get_metrics(session, "220031")
        filed = repo.get_metrics(session, "220032")
        costed = repo.get_metrics(session, "220033")
        assert repo.get_metrics(session, "220034") is None
        assert repo.get_metrics(session, "229999") is None
        assert sheeted.exposure_score == 770.4 + 8397.0
        assert (filed.exposure_score, costed.exposure_score) == (100.0, 20000.0)
        assert (filed.exposure_rank, sheeted.exposure_rank, costed.exposure_rank) == (
            0.0,
            0.5,
            1.0,
        )
        assert (filed.exposure_label, sheeted.exposure_label, costed.exposure_label) == (
            "low",
            "medium",
            "high",
        )
        assert sheeted.exposure_peers == 3 and sheeted.exposure_computed_on == TODAY
        assert (sheeted.gap, sheeted.behaviour_computed_on) == (0.5, EARLIER)
        assert filed.gap is None and filed.behaviour_computed_on is None
