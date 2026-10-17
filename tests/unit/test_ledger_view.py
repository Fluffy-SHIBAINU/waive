"""The ledger view (Phase 10): the LedgerView a page, the JSON key and the result screen carry,
built from the metrics row and the loaders' tables in the known, partial and unknown states."""

import re
from datetime import date
from decimal import Decimal

from waive.atlas import repo as atlas_repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.db import init_db, make_engine, session_scope
from waive.ledger import explain, repo
from waive.ledger.explain import MISSING
from waive.ledger.metrics import compute_behaviour, compute_exposure_metrics
from waive.ledger.models import ExposureLabel, ExposureMetric, GapMetric, LedgerView, MetricStatus
from waive.ledger.view import (
    ACS_SOURCE,
    COST_REPORT_SOURCE,
    INPATIENT_SOURCE,
    SCHEDULE_H_SOURCE,
    SERVICE_AREA_SOURCE,
    SHEET_SOURCE,
    expect_lines,
    ledger_json,
    ledger_view,
)

TODAY = date(2026, 10, 16)
BASE = {
    "city": "BOSTON",
    "state": "MA",
    "zip": "02118",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
}
FULL = {**BASE, "ccn": "220031", "name": "REAL GENERAL HOSPITAL", "city": "WORCESTER"}
HALF = {**BASE, "ccn": "220032", "name": "HALF MEMORIAL", "city": "LOWELL"}
LOW = {**BASE, "ccn": "220033", "name": "LOW COMMUNITY", "city": "SALEM"}
BARE = {**BASE, "ccn": "220034", "name": "BARE HOSPITAL", "city": "LYNN"}
UGLY = re.compile(r"[\[\]{}<>]|None|True|False|GapLabel|ExposureLabel|MetricStatus|_[a-z]")


def real_sheet():
    sample = st_example_sheet()
    hospital = sample.hospital.model_copy(
        update={"ccn": "220031", "name": "Real General Hospital", "city": "Worcester"}
    )
    limit = sample.eligibility.free_care_max_fpl.model_copy(update={"value": Decimal("300")})
    eligibility = sample.eligibility.model_copy(update={"free_care_max_fpl": limit})
    return sample.model_copy(update={"hospital": hospital, "eligibility": eligibility})


def seeded_engine():
    """FULL has every input and ranks at the top; HALF has a filing and inpatient counts only;
    LOW has a cost report only; BARE has an identity row and nothing else."""
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        for hospital in (FULL, HALF, LOW, BARE):
            atlas_repo.upsert_hospital(session, hospital)
        publish_sheet(session, real_sheet())
        repo.upsert_hospital_id(
            session, "220031", ein="043145034", npi="1013995133", match_method="cbi",
            match_confidence=1.0,
        )  # fmt: skip
        repo.upsert_hospital_id(session, "220034", npi="1999999999", checked_on=TODAY)
        repo.upsert_cost_report(
            session, "220031", 2024, fy_end=date(2024, 9, 30), charity_care_cost=9,
            total_costs=1000, bad_debt=36, net_patient_revenue=1000, vintage="HCRIS 2024",
        )  # fmt: skip
        repo.upsert_cost_report(
            session, "220033", 2024, fy_end=date(2024, 12, 31), charity_care_cost=50,
            total_costs=1000, bad_debt=10, net_patient_revenue=1000, vintage="HCRIS 2024",
        )  # fmt: skip
        repo.upsert_schedule_h(
            session, "220031", 2024, ein="043145034", charity_pct_of_expense=1.1,
            credit_reporting=True, sells_debt=False, legal_action=True, flag_line="engaged",
            permitted_actions="credit_reporting,legal_action", vintage="IRS e-file index 2025",
        )  # fmt: skip
        repo.upsert_schedule_h(
            session, "220032", 2023, ein="043999999", free_care_fpl_pct=200.0,
            credit_reporting=False, sells_debt=False, legal_action=False, flag_line="engaged",
            permitted_actions="none", vintage="IRS e-file index 2025",
        )  # fmt: skip
        repo.upsert_exposure(
            session, "220031", discharges=1850, benes=1200, dual_benes=480, dual_discharges=770.4,
            poor_seniors_served=8397.0, inpatient_vintage="2024-01-01 to 2024-12-31, file of 2026-10-05",
            service_area_vintage="2023, file of 2026-10-05", acs_vintage="ACS 5-year 2024",
        )  # fmt: skip
        repo.upsert_exposure(
            session, "220032", discharges=400, benes=300, dual_benes=30, dual_discharges=40.0,
            inpatient_vintage="2024-01-01 to 2024-12-31, file of 2026-10-05",
        )  # fmt: skip
        repo.upsert_exposure(
            session, "220033", poor_seniors_served=3000.0, acs_vintage="ACS 5-year 2024"
        )
        compute_behaviour(session, date(2026, 10, 10))
        compute_exposure_metrics(session, TODAY)
    return engine


def test_ledger_view_is_none_without_an_identity_or_metrics():
    engine = seeded_engine()
    with session_scope(engine) as session:
        assert ledger_view(session, "000000") is None
        assert ledger_json(session, "000000") is None
        assert expect_lines(session, "000000") == []


def test_ledger_view_known_state_has_numbers_words_and_sources():
    engine = seeded_engine()
    with session_scope(engine) as session:
        view = ledger_view(session, "220031")
    assert view.ccn == "220031" and view.computed_on == TODAY
    assert (view.ids.ein, view.ids.npi, view.ids.match_method, view.ids.match_confidence) == (
        "043145034",
        "1013995133",
        "cbi",
        1.0,
    )
    gap = view.gap
    assert gap.status is MetricStatus.KNOWN and gap.missing == []
    assert (gap.stated_free_care_fpl, gap.stated_source, gap.stated_rank, gap.stated_peers) == (
        300.0,
        "sheet",
        1.0,
        2,
    )
    assert (gap.charity_cost_pct, gap.charity_rank, gap.charity_peers) == (0.9, 0.0, 2)
    assert (gap.charity_pct_990, gap.cost_report_year, gap.gap) == (1.1, 2024, 1.0)
    assert gap.label.value == "promises_more_delivers_less"
    assert gap.headline == "Promises a lot, spends little on free care"
    assert gap.sentence == (
        "Says free care goes up to 300% of the poverty level, above 9 in 10 nonprofit "
        "hospitals. Spent 0.9% of its costs on free care in the year to September 2024, "
        "below 9 in 10 nonprofit hospitals. Its tax filing puts financial assistance at 1.1% "
        "of expenses."
    )
    assert gap.short == (
        "Free care is promised up to 300% of the poverty level, but its spending on free care "
        "is below 9 in 10 nonprofit hospitals."
    )
    assert gap.advice.startswith("Apply in writing, keep a copy")
    assert [(s.name, s.vintage, s.url) for s in gap.sources] == [
        (SHEET_SOURCE[0], "version 1", "/atlas/220031"),
        (COST_REPORT_SOURCE[0], "year to September 2024, HCRIS 2024", COST_REPORT_SOURCE[1]),
        (
            SCHEDULE_H_SOURCE[0],
            "tax year 2024, IRS e-file index 2025",
            "https://projects.propublica.org/nonprofits/organizations/043145034",
        ),
    ]
    exposure = view.exposure
    assert exposure.status is MetricStatus.KNOWN and exposure.missing == []
    assert (exposure.discharges, exposure.dual_discharges, exposure.poor_seniors_served) == (
        1850,
        770,
        8397,
    )
    assert (exposure.score, exposure.rank, exposure.peers) == (770.4 + 8397.0, 1.0, 3)
    assert exposure.label.value == "high" and exposure.headline == "High"
    assert exposure.sentence == (
        "About 770 of its 1,850 Medicare inpatients in 2024 were also on Medicaid. Going by "
        "where its Medicare patients live, about 8,400 people 65 or over living under twice "
        "the poverty line look to this hospital."
    )
    assert exposure.short == "About 4 in 10 of its Medicare patients are also on Medicaid."
    assert exposure.advice.startswith("If you have both Medicare and Medicaid")
    assert [(s.name, s.vintage, s.url) for s in exposure.sources] == [
        (INPATIENT_SOURCE[0], "2024-01-01 to 2024-12-31, file of 2026-10-05", INPATIENT_SOURCE[1]),
        (SERVICE_AREA_SOURCE[0], "2023, file of 2026-10-05", SERVICE_AREA_SOURCE[1]),
        (ACS_SOURCE[0], "ACS 5-year 2024", ACS_SOURCE[1]),
    ]
    collections = view.collections
    assert collections.status is MetricStatus.KNOWN and collections.missing == []
    assert (collections.legal_action, collections.credit_reporting, collections.sells_debt) == (
        True,
        True,
        False,
    )
    assert (collections.lawsuits, collections.liens, collections.flag_line) == (
        None,
        None,
        "engaged",
    )
    assert collections.permitted_actions == ["credit_reporting", "legal_action"]
    assert (collections.schedule_h_year, collections.bad_debt_pct) == (2024, 3.6)
    assert (collections.bad_debt_rank, collections.bad_debt_peers) == (1.0, 2)
    assert collections.headline == "Takes legal action, reports to credit agencies"
    assert collections.sentence == (
        "In its 2024 tax filing the hospital reported that, before checking whether patients "
        "qualified for free care, it took legal action against them and reported them to "
        "credit agencies. Bad debt was 3.6% of patient revenue, above 9 in 10 nonprofit "
        "hospitals."
    )
    assert collections.short == (
        "This hospital takes legal action and reports patients to credit agencies before "
        "checking who qualifies."
    )
    assert collections.advice.startswith("Apply within 120 days of the first bill. Waive")
    assert [(s.name, s.vintage) for s in collections.sources] == [
        (SCHEDULE_H_SOURCE[0], "tax year 2024, IRS e-file index 2025"),
        (COST_REPORT_SOURCE[0], "year to September 2024, HCRIS 2024"),
    ]
    for metric in (gap, exposure, collections):
        for text in (metric.headline, metric.sentence, metric.short, metric.advice):
            assert not UGLY.search(text), text


def test_ledger_view_partial_state_names_what_is_missing():
    engine = seeded_engine()
    with session_scope(engine) as session:
        view = ledger_view(session, "220032")
    # No identity row: the metrics row alone makes the view, and the ids are empty.
    assert view.ids.ein is None and view.computed_on == TODAY
    gap = view.gap
    assert gap.status is MetricStatus.PARTIAL
    assert (gap.stated_free_care_fpl, gap.stated_source, gap.stated_rank) == (200.0, "irs990", 0.0)
    assert gap.charity_cost_pct is None and gap.label is None
    assert gap.missing == [MISSING["cost_report"]]
    assert gap.headline == "Free care promised up to 200% of the poverty level"
    assert gap.sentence == (
        "Says free care goes up to 200% of the poverty level, below 9 in 10 nonprofit hospitals."
    )
    assert gap.short == "Free care is promised up to 200% of the poverty level."
    assert gap.advice == "Apply in writing and keep a copy."
    assert [(s.name, s.vintage, s.url) for s in gap.sources] == [
        (
            SCHEDULE_H_SOURCE[0],
            "tax year 2023, IRS e-file index 2025",
            "https://projects.propublica.org/nonprofits/organizations/043999999",
        )
    ]
    exposure = view.exposure
    assert exposure.status is MetricStatus.PARTIAL
    assert exposure.missing == [MISSING["service_area"]]
    assert (exposure.dual_discharges, exposure.poor_seniors_served, exposure.rank) == (
        40,
        None,
        0.0,
    )
    assert exposure.headline == "Low"
    assert (
        exposure.sentence
        == "About 40 of its 400 Medicare inpatients in 2024 were also on Medicaid."
    )
    assert exposure.short == "About 1 in 10 of its Medicare patients are also on Medicaid."
    assert [s.name for s in exposure.sources] == [INPATIENT_SOURCE[0]]
    collections = view.collections
    assert collections.status is MetricStatus.PARTIAL
    assert collections.missing == [MISSING["cost_report"]]
    assert collections.permitted_actions == ["none"]
    assert collections.headline == "No lawsuits, liens, credit reports or debt sales reported"
    assert collections.sentence == (
        "In its 2023 tax filing the hospital reported no lawsuits, liens, credit reporting or "
        "debt sales before checking who qualified for free care."
    )
    assert collections.advice.startswith("Apply before the first bill is 120 days old")
    assert [s.name for s in collections.sources] == [SCHEDULE_H_SOURCE[0]]


def test_ledger_view_cost_report_only_and_unknown_states():
    engine = seeded_engine()
    with session_scope(engine) as session:
        low = ledger_view(session, "220033")
        bare = ledger_view(session, "220034")
    assert low.gap.status is MetricStatus.PARTIAL and low.gap.missing == [MISSING["stated"]]
    assert low.gap.headline == "Spent 5% of its costs on free care"
    assert low.gap.sentence == (
        "Spent 5% of its costs on free care in the year to December 2024, above 9 in 10 "
        "nonprofit hospitals."
    )
    assert low.gap.short == "This hospital spent 5% of its costs on free care."
    assert [s.name for s in low.gap.sources] == [COST_REPORT_SOURCE[0]]
    assert low.collections.status is MetricStatus.PARTIAL
    assert low.collections.missing == [MISSING["schedule_h"]]
    assert low.collections.headline == "Practices not reported; bad debt was 1% of revenue"
    assert low.collections.sentence == (
        "Waive could not match this hospital to a tax filing, so whether it sues patients, "
        "places liens, reports to credit agencies or sells debt is unknown. Bad debt was 1% "
        "of patient revenue, below 9 in 10 nonprofit hospitals."
    )
    assert low.collections.advice == (
        "Apply within 120 days of the first bill and keep a copy of everything."
    )
    assert [s.name for s in low.collections.sources] == [COST_REPORT_SOURCE[0]]
    assert low.exposure.status is MetricStatus.PARTIAL
    assert low.exposure.missing == [MISSING["inpatient"]]
    assert low.exposure.headline == "Medium" and low.exposure.short == ""
    assert [s.name for s in low.exposure.sources] == [SERVICE_AREA_SOURCE[0], ACS_SOURCE[0]]
    # An identity row alone: every metric unknown, every missing word listed, no sources.
    assert bare.ids.npi == "1999999999" and bare.computed_on is None
    for metric in (bare.gap, bare.exposure, bare.collections):
        assert metric.status is MetricStatus.UNKNOWN and metric.sources == []
    assert bare.gap.missing == [MISSING["stated"], MISSING["cost_report"]]
    assert bare.exposure.missing == [MISSING["inpatient"], MISSING["service_area"]]
    assert bare.collections.missing == [MISSING["schedule_h"], MISSING["cost_report"]]
    assert bare.gap.headline == "Not enough data yet"
    assert bare.exposure.headline == "Not enough data yet"
    assert bare.collections.headline == "Practices not reported"


def test_ledger_json_shape():
    engine = seeded_engine()
    with session_scope(engine) as session:
        data = ledger_json(session, "220031")
        partial = ledger_json(session, "220032")
    assert list(data) == ["ccn", "ids", "gap", "exposure", "collections", "computed_on"]
    assert data["computed_on"] == "2026-10-16"
    assert data["ids"]["ein"] == "043145034"
    assert (
        data["gap"]["status"] == "known" and data["gap"]["label"] == "promises_more_delivers_less"
    )
    assert data["exposure"]["status"] == "known" and data["exposure"]["label"] == "high"
    assert data["collections"]["status"] == "known"
    assert data["collections"]["permitted_actions"] == ["credit_reporting", "legal_action"]
    assert data["gap"]["sources"][0] == {
        "name": "Waive policy sheet",
        "vintage": "version 1",
        "url": "/atlas/220031",
    }
    assert partial["gap"]["status"] == "partial" and partial["gap"]["label"] is None
    assert partial["gap"]["missing"] == ["no cost report on file"]


def test_expect_lines_in_order_without_unknown_metrics():
    engine = seeded_engine()
    with session_scope(engine) as session:
        full = expect_lines(session, "220031")
        low = expect_lines(session, "220033")
        bare = expect_lines(session, "220034")
    assert [line for line, _ in full] == [
        "Free care is promised up to 300% of the poverty level, but its spending on free care "
        "is below 9 in 10 nonprofit hospitals.",
        "This hospital takes legal action and reports patients to credit agencies before "
        "checking who qualifies.",
        "About 4 in 10 of its Medicare patients are also on Medicaid.",
    ]
    assert full[0][1].startswith("Apply in writing, keep a copy")
    assert full[1][1].startswith("Apply within 120 days of the first bill. Waive")
    assert full[2][1].startswith("If you have both Medicare and Medicaid")
    # A cost-report-only hospital still has a gap line and a collections line. It has
    # service-area data but no inpatient counts, so its exposure short line is empty and the
    # metric is left out: the screen never shows the bare word "Medium".
    assert [line for line, _ in low] == [
        "This hospital spent 5% of its costs on free care.",
        "Whether this hospital sues or reports patients before checking who qualifies is not "
        "on file.",
    ]
    assert bare == []


def test_expect_lines_use_the_short_line_only_and_skip_a_metric_without_one():
    # Service-area data ranks the hospital (headline "Medium") but without inpatient counts
    # there is no short line; the headline alone must not stand in for it.
    exposure = ExposureMetric(
        status=MetricStatus.PARTIAL,
        headline="Medium",
        short="",
        advice=explain.exposure_advice(),
        poor_seniors_served=3000,
        label=ExposureLabel.MEDIUM,
    )
    gap = GapMetric(
        status=MetricStatus.PARTIAL,
        headline="Spent 5% of its costs on free care",
        short="This hospital spent 5% of its costs on free care.",
        advice="Apply in writing and keep a copy.",
    )
    view = LedgerView(ccn="220033", gap=gap, exposure=exposure)
    assert view.expect_lines() == [
        ("This hospital spent 5% of its costs on free care.", "Apply in writing and keep a copy.")
    ]
    # With inpatient counts the exposure line is the share, never the headline.
    with_counts = exposure.model_copy(
        update={"short": "About 4 in 10 of its Medicare patients are also on Medicaid."}
    )
    lines = LedgerView(ccn="220033", gap=gap, exposure=with_counts).expect_lines()
    assert [line for line, _ in lines][-1] == (
        "About 4 in 10 of its Medicare patients are also on Medicaid."
    )
    assert "Medium" not in [line for line, _ in lines]
    # A known metric whose short line is empty is skipped too; an unknown one always is.
    assert LedgerView(ccn="220034", gap=gap.model_copy(update={"short": ""})).expect_lines() == []
    unknown = gap.model_copy(update={"status": MetricStatus.UNKNOWN})
    assert LedgerView(ccn="220034", gap=unknown).expect_lines() == []


def test_a_stub_period_cost_report_is_named_and_never_called_a_year():
    # The hospital changed its fiscal year and its only report covers July to September: the
    # metrics skip its shares, and the card says why rather than "the year to September".
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        atlas_repo.upsert_hospital(session, LOW)
        atlas_repo.upsert_hospital(session, FULL)
        repo.upsert_cost_report(
            session, "220033", 2024, fy_begin=date(2024, 7, 1), fy_end=date(2024, 9, 30),
            charity_care_cost=50, total_costs=100, bad_debt=10, net_patient_revenue=100,
            vintage="HCRIS 2024",
        )  # fmt: skip
        repo.upsert_cost_report(
            session, "220031", 2024, fy_begin=date(2023, 10, 1), fy_end=date(2024, 9, 30),
            charity_care_cost=9, total_costs=1000, bad_debt=36, net_patient_revenue=1000,
        )  # fmt: skip
        compute_behaviour(session, TODAY)
        view = ledger_view(session, "220033")
        full = ledger_view(session, "220031")
    assert view.gap.status is MetricStatus.UNKNOWN
    assert view.gap.charity_cost_pct is None and view.gap.cost_report_year == 2024
    assert view.gap.missing == [MISSING["stated"], MISSING["short_period"]]
    assert view.collections.missing == [MISSING["schedule_h"], MISSING["short_period"]]
    assert view.gap.sources == [] and view.collections.sources == []
    for text in (view.gap.headline, view.gap.sentence, view.collections.sentence):
        assert "year to" not in text, text
    # A full year's report is still read, ranked and dated as before.
    assert full.gap.charity_cost_pct == 0.9 and full.collections.bad_debt_pct == 3.6
    assert full.gap.missing == [MISSING["stated"]]
    assert "in the year to September 2024" in full.gap.sentence


def test_a_blank_charity_cell_is_named_as_such_and_vintages_never_print_none():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        atlas_repo.upsert_hospital(session, HALF)
        # The 2024 report is on file but its charity-care and bad-debt lines are blank, and the
        # loader left the vintage empty: the words must say "blank line", not "no report".
        repo.upsert_cost_report(
            session,
            "220032",
            2024,
            fy_begin=date(2023, 10, 1),
            fy_end=date(2024, 9, 30),
            total_costs=1000,
        )
        repo.upsert_metrics(session, "220032", stated_fpl=300.0, stated_source="irs990")
        repo.upsert_schedule_h(
            session,
            "220032",
            2024,
            ein="042103606",
            free_care_fpl_pct=300.0,
            flag_line="engaged",
            credit_reporting=True,
            legal_action=False,
            sells_debt=False,
        )
        repo.upsert_metrics(
            session,
            "220032",
            flag_line="engaged",
            credit_reporting=True,
            legal_action=False,
            sells_debt=False,
            schedule_h_year=2024,
        )
        view = ledger_view(session, "220032")
    assert view.gap.status is MetricStatus.PARTIAL
    assert view.gap.missing == [MISSING["charity_cell"]]
    assert view.collections.missing == [MISSING["bad_debt_cell"]]
    assert [s.vintage for s in view.collections.sources] == ["tax year 2024"]
    assert not any(
        "None" in s.vintage for metric in (view.gap, view.collections) for s in metric.sources
    )


def test_the_demo_hospital_has_no_ledger_view_even_with_rows():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        atlas_repo.upsert_hospital(session, {**BASE, "ccn": "229999", "name": "ST. EXAMPLE"})
        repo.upsert_hospital_id(session, "229999", ein="049999999", match_method="manual")
        repo.upsert_metrics(session, "229999", exposure_rank=0.9)
        assert ledger_view(session, "229999") is None
        assert ledger_json(session, "229999") is None and expect_lines(session, "229999") == []
