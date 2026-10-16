"""The ledger's five tables (Phase 10): created by `init_db`, columns pinned, keys as designed."""

from datetime import date

from sqlalchemy import inspect

from waive.db import init_db, make_engine, session_scope, upgrade_schema
from waive.ledger.tables import (
    MATCH_METHODS,
    CostReportRow,
    ExposureRow,
    HospitalIdRow,
    LedgerMetricsRow,
    ScheduleHRow,
)

LEDGER_TABLES = (
    "hospital_ids",
    "ledger_cost_reports",
    "ledger_schedule_h",
    "ledger_exposure",
    "ledger_metrics",
)


def test_init_db_creates_the_ledger_tables_with_the_designed_columns():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    assert init_db(engine) == []
    inspector = inspect(engine)
    assert set(LEDGER_TABLES) <= set(inspector.get_table_names())
    columns = {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in LEDGER_TABLES
    }
    assert columns["hospital_ids"] == {
        "ccn", "ein", "npi", "associate_id", "match_method", "match_confidence",
        "matched_name", "ein_vintage", "npi_vintage", "nonprofit_flag", "checked_on",
    }  # fmt: skip
    assert columns["ledger_cost_reports"] == {
        "ccn", "fiscal_year", "rpt_rec_num", "fy_begin", "fy_end", "charity_care_cost",
        "bad_debt", "uncompensated_care_cost", "total_costs", "net_patient_revenue",
        "type_of_control", "hospital_name", "source_file", "vintage", "loaded_on",
    }  # fmt: skip
    assert columns["ledger_schedule_h"] == {
        "ccn", "tax_year", "ein", "tax_period", "object_id", "filer_name", "facility_name",
        "facility_line", "free_care_fpl_pct", "discount_care_fpl_pct", "asset_test",
        "lawsuits", "liens", "credit_reporting", "sells_debt", "legal_action", "flag_line",
        "permitted_actions", "charity_pct_of_expense", "vintage", "loaded_on",
    }  # fmt: skip
    assert columns["ledger_exposure"] == {
        "ccn", "discharges", "benes", "dual_benes", "dual_discharges", "zips_counted",
        "zips_suppressed", "poor_seniors_served", "inpatient_vintage",
        "service_area_vintage", "acs_vintage", "loaded_on",
    }  # fmt: skip
    assert columns["ledger_metrics"] == {
        "ccn", "stated_fpl", "stated_source", "stated_rank", "stated_peers",
        "charity_cost_pct", "charity_rank", "charity_peers", "charity_pct_990",
        "cost_report_year", "gap", "gap_label", "exposure_score", "exposure_rank",
        "exposure_peers", "exposure_label", "lawsuits", "liens", "credit_reporting",
        "sells_debt", "legal_action", "flag_line", "permitted_actions", "schedule_h_year",
        "bad_debt_pct", "bad_debt_rank",
        "bad_debt_peers", "behaviour_computed_on", "exposure_computed_on",
    }  # fmt: skip
    assert inspector.get_pk_constraint("ledger_cost_reports")["constrained_columns"] == [
        "ccn",
        "fiscal_year",
    ]
    assert inspector.get_pk_constraint("ledger_schedule_h")["constrained_columns"] == [
        "ccn",
        "tax_year",
    ]
    # Nothing to add on a second pass: the tables were created whole.
    assert upgrade_schema(engine) == []


def test_match_methods_are_the_five_designed_values():
    assert MATCH_METHODS == ("cbi", "enrollments", "name_zip", "fuzzy", "manual")


def test_rows_round_trip_through_sqlite():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        session.execute(
            __import__("sqlalchemy").text(
                "INSERT INTO hospitals (ccn, name, address, city, state, zip, hospital_type, "
                "ownership, updated_at) VALUES ('220001', 'A', '', 'B', 'MA', '01453', 'Acute', "
                "'Voluntary non-profit', '2026-10-01 00:00:00')"
            )
        )
        session.add(HospitalIdRow(ccn="220001", ein="912155626", checked_on=date(2026, 10, 17)))
        session.add(
            CostReportRow(
                ccn="220001",
                fiscal_year=2024,
                rpt_rec_num="837430",
                fy_begin=date(2023, 10, 1),
                fy_end=date(2024, 9, 30),
                total_costs=245_642_337,
                charity_care_cost=2_060_112,
                hospital_name="HEALTHALLIANCE-CLINTON",
                source_file="CostReport_2024_Final.csv",
                vintage="file of 2026-10-05",
                loaded_on=date(2026, 10, 17),
            )
        )
        session.add(
            ScheduleHRow(
                ccn="220001",
                tax_year=2024,
                ein="912155626",
                tax_period="202409",
                object_id="202502269349301705",
                filer_name="UMass Memorial Health Care Inc",
                lawsuits=True,
                flag_line="engaged",
                vintage="IRS e-file index 2025",
                loaded_on=date(2026, 10, 17),
            )
        )
        session.add(
            ExposureRow(
                ccn="220001", zips_counted=3, zips_suppressed=1, loaded_on=date(2026, 10, 17)
            )
        )
        session.add(
            LedgerMetricsRow(ccn="220001", gap=0.5, gap_label="promises_more_delivers_less")
        )
    with session_scope(engine) as session:
        assert session.get(CostReportRow, ("220001", 2024)).total_costs == 245_642_337
        assert session.get(ScheduleHRow, ("220001", 2024)).lawsuits is True
        assert session.get(LedgerMetricsRow, "220001").gap_label == "promises_more_delivers_less"
