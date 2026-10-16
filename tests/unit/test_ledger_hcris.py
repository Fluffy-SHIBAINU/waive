"""HCRIS cost reports into ledger_cost_reports (Phase 10). No sockets: respx serves the fixture."""

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import respx
from sqlalchemy import func, select

from waive.atlas import repo as atlas_repo
from waive.db import init_db, make_engine, session_scope
from waive.ledger import repo
from waive.ledger.download import datasets_named, vintage_of
from waive.ledger.hcris import (
    COST_REPORT_LANDING,
    COST_REPORT_TITLE,
    CostReportFacts,
    bad_debt_share,
    charity_share,
    load_cost_reports,
    parse_cost_report_csv,
    pick_one_per_year,
)
from waive.ledger.tables import CostReportRow

FIXTURE = Path(__file__).parent.parent / "fixtures" / "ledger" / "cost_report.csv"
TODAY = date(2026, 10, 17)
HOSPITAL = {
    "name": "X",
    "city": "LEOMINSTER",
    "state": "MA",
    "zip": "01453",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
}
REGISTRY = ("220001", "220002", "310001", "229999")
CATALOG = [
    {
        "title": "Hospital Provider Cost Report : 2023-01-01",
        "modified": "2026-09-01",
        "temporal": [{"startDate": "2023-01-01", "endDate": "2023-12-31"}],
        "distribution": [
            {"accessURL": "https://data.cms.gov/data-api/v1/dataset/aaa/data"},
            {"mediaType": "text/csv", "downloadURL": "https://data.cms.gov/f/2023.csv"},
        ],
    },
    {
        "title": "Hospital Provider Cost Report : 2024-01-02",
        "modified": "2026-10-05",
        "temporal": [{"startDate": "2024-01-01", "endDate": "2024-12-31"}],
        "distribution": [
            {"accessURL": "https://data.cms.gov/data-api/v1/dataset/bbb/data"},
            {"mediaType": "text/csv", "downloadURL": "https://data.cms.gov/f/2024.csv"},
        ],
    },
    {
        "title": "Hospital Provider Cost Report : 2022-01-01",
        "modified": "2026-09-01",
        "temporal": [{"startDate": "2022-01-01", "endDate": "2022-12-31"}],
        "distribution": [
            {"mediaType": "text/csv", "downloadURL": "https://data.cms.gov/f/2022.csv"},
        ],
    },
    {"title": "Something else", "modified": "2026-10-06", "distribution": []},
]


def facts(ccn="220002", begin=date(2023, 7, 1), end=date(2024, 6, 30), **over):
    fields = dict(
        rpt_rec_num="1",
        ccn=ccn,
        hospital_name="H",
        state="MA",
        type_of_control=2,
        fy_begin=begin,
        fy_end=end,
        charity_care_cost=1,
        bad_debt=1,
        uncompensated_care_cost=1,
        total_costs=1,
        net_patient_revenue=1,
    )
    return CostReportFacts(**{**fields, **over})


def engine_with(*ccns):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        for ccn in ccns:
            atlas_repo.upsert_hospital(session, {**HOSPITAL, "ccn": ccn})
    return engine


def serve(content: bytes, *urls: str):
    for url in urls:
        respx.get(url).mock(return_value=httpx.Response(200, content=content))


def rows_of(session):
    return list(session.scalars(select(CostReportRow).order_by(CostReportRow.ccn)))


def test_constants_name_the_dataset_family_and_its_landing_page():
    assert COST_REPORT_TITLE == "Hospital Provider Cost Report"
    assert COST_REPORT_LANDING == (
        "https://data.cms.gov/provider-compliance/cost-report/hospital-provider-cost-report"
    )


def test_parse_reads_money_as_whole_dollars_and_blanks_as_none():
    parsed = list(parse_cost_report_csv(FIXTURE))
    assert [f.ccn for f in parsed] == ["220001", "310001", "990001", "220002", "220002"]
    first = parsed[0]
    assert first == CostReportFacts(
        rpt_rec_num="773939",
        ccn="220001",
        hospital_name="HEALTHALLIANCE-CLINTON",
        state="MA",
        type_of_control=2,
        fy_begin=date(2023, 10, 1),
        fy_end=date(2024, 9, 30),
        charity_care_cost=2060112,
        bad_debt=513000,
        uncompensated_care_cost=2573112,
        total_costs=91300000,
        net_patient_revenue=100500000,
    )
    # Blank money is None, a decimal string is truncated to whole dollars, US dates parse too.
    jersey = parsed[1]
    assert (jersey.charity_care_cost, jersey.uncompensated_care_cost) == (None, None)
    assert (jersey.total_costs, jersey.bad_debt, jersey.net_patient_revenue) == (1234, 20, 0)
    assert (jersey.fy_begin, jersey.fy_end) == (date(2022, 10, 1), date(2023, 9, 30))
    assert jersey.type_of_control == 1


def test_parse_tolerates_a_bom_and_skips_rows_without_a_ccn_or_both_dates(tmp_path):
    header = (
        '"rpt_rec_num","Provider CCN","Hospital Name","State Code","Type of Control",'
        '"Fiscal Year Begin Date","Fiscal Year End Date","Cost of Charity Care",'
        '"Total Bad Debt Expense","Cost of Uncompensated Care","Total Costs","Net Patient Revenue"'
    )
    lines = [
        header,
        '"1","","NO CCN","MA","2","2023-10-01","2024-09-30","1","1","1","1","1"',
        '"2","220001","NO END","MA","2","2023-10-01","","1","1","1","1","1"',
        '"3","220001","NO BEGIN","MA","2","","2024-09-30","1","1","1","1","1"',
        '"4","220001","BAD DATE","MA","2","2023-13-45","2024-09-30","1","1","1","1","1"',
        '"5","220001","KEPT","MA","","2023-10-01","2024-09-30","x","-5","1e3","2.9","n/a"',
    ]
    path = tmp_path / "bom.csv"
    path.write_bytes(b"\xef\xbb\xbf" + "\n".join(lines).encode("utf-8") + b"\n")
    parsed = list(parse_cost_report_csv(path))
    assert [f.hospital_name for f in parsed] == ["KEPT"]
    kept = parsed[0]
    assert kept.type_of_control is None
    assert (kept.charity_care_cost, kept.bad_debt, kept.uncompensated_care_cost) == (None, -5, 1000)
    assert (kept.total_costs, kept.net_patient_revenue) == (2, None)


def test_pick_one_per_year_keeps_the_longer_period_then_the_later_end():
    long_report = facts(rpt_rec_num="A", begin=date(2023, 7, 1), end=date(2024, 6, 30))
    short_later = facts(rpt_rec_num="B", begin=date(2024, 7, 1), end=date(2024, 12, 31))
    other_year = facts(rpt_rec_num="C", begin=date(2022, 7, 1), end=date(2023, 6, 30))
    picked = pick_one_per_year([short_later, long_report, other_year])
    assert set(picked) == {("220002", 2024), ("220002", 2023)}
    assert picked[("220002", 2024)].rpt_rec_num == "A"
    assert picked[("220002", 2023)].rpt_rec_num == "C"
    # Equal lengths: the later end wins, whichever came first.
    early = facts(rpt_rec_num="D", begin=date(2023, 1, 1), end=date(2024, 1, 1))
    late = facts(rpt_rec_num="E", begin=date(2023, 6, 30), end=date(2024, 6, 29))
    assert pick_one_per_year([late, early])[("220002", 2024)].rpt_rec_num == "E"
    assert pick_one_per_year([early, late])[("220002", 2024)].rpt_rec_num == "E"
    # Two CCNs never collide.
    other = facts(ccn="220001", rpt_rec_num="F")
    assert len(pick_one_per_year([long_report, other])) == 2
    assert pick_one_per_year([]) == {}


@respx.mock
def test_load_cost_reports_keeps_registry_ccns_one_per_year_with_vintage_and_file(tmp_path):
    serve(
        FIXTURE.read_bytes(), "https://data.cms.gov/f/2024.csv", "https://data.cms.gov/f/2023.csv"
    )
    engine = engine_with(*REGISTRY)
    newest = datasets_named(CATALOG, COST_REPORT_TITLE)[0]
    with session_scope(engine) as session, httpx.Client() as http:
        report = load_cost_reports(session, http, tmp_path, CATALOG, today=TODAY, years=2)
    assert report.files == ["2024.csv", "2023.csv"]
    assert report.rows_read == 10
    assert report.hospitals_loaded == 3
    assert report.by_year == {2023: 1, 2024: 2}
    assert sorted(p.name for p in (tmp_path / "hcris").iterdir()) == ["2023.csv", "2024.csv"]
    with session_scope(engine) as session:
        rows = rows_of(session)
        assert [(r.ccn, r.fiscal_year) for r in rows] == [
            ("220001", 2024),
            ("220002", 2024),
            ("310001", 2023),
        ]
        clinton = repo.latest_cost_report(session, "220001")
        assert (clinton.rpt_rec_num, clinton.hospital_name) == ("773939", "HEALTHALLIANCE-CLINTON")
        assert (clinton.fy_begin, clinton.fy_end) == (date(2023, 10, 1), date(2024, 9, 30))
        assert (clinton.charity_care_cost, clinton.bad_debt) == (2060112, 513000)
        assert (clinton.uncompensated_care_cost, clinton.total_costs) == (2573112, 91300000)
        assert (clinton.net_patient_revenue, clinton.type_of_control) == (100500000, 2)
        # The same facts in two files: the newest file's vintage and name are the ones stored.
        assert (
            clinton.vintage == vintage_of(newest) == "2024-01-01 to 2024-12-31, file of 2026-10-05"
        )
        assert clinton.source_file == "2024.csv"
        assert clinton.loaded_on == TODAY
        twins = repo.latest_cost_report(session, "220002")
        assert (twins.rpt_rec_num, twins.fy_begin) == ("781000", date(2023, 7, 1))
        jersey = repo.latest_cost_report(session, "310001")
        assert (jersey.charity_care_cost, jersey.total_costs, jersey.vintage) == (
            None,
            1234,
            clinton.vintage,
        )
        assert repo.latest_cost_report(session, "990001") is None


@respx.mock
def test_load_cost_reports_takes_only_the_newest_years_and_the_ccns_asked_for(tmp_path):
    routes = {
        year: respx.get(f"https://data.cms.gov/f/{year}.csv").mock(
            return_value=httpx.Response(200, content=FIXTURE.read_bytes())
        )
        for year in (2022, 2023, 2024)
    }
    engine = engine_with(*REGISTRY)
    with session_scope(engine) as session, httpx.Client() as http:
        report = load_cost_reports(
            session, http, tmp_path, CATALOG, today=TODAY, years=1, ccns={"310001", "990001"}
        )
    assert report.files == ["2024.csv"]
    assert (routes[2024].call_count, routes[2023].call_count, routes[2022].call_count) == (1, 0, 0)
    assert (report.rows_read, report.hospitals_loaded, report.by_year) == (5, 1, {2023: 1})
    with session_scope(engine) as session:
        assert [r.ccn for r in rows_of(session)] == ["310001"]


@respx.mock
def test_load_cost_reports_rerun_updates_in_place_and_serves_the_cache(tmp_path):
    route = respx.get("https://data.cms.gov/f/2024.csv").mock(
        return_value=httpx.Response(200, content=FIXTURE.read_bytes())
    )
    engine = engine_with(*REGISTRY)
    later = date(2026, 11, 1)
    with session_scope(engine) as session, httpx.Client() as http:
        load_cost_reports(session, http, tmp_path, CATALOG, today=TODAY, years=1)
        again = load_cost_reports(session, http, tmp_path, CATALOG, today=later, years=1)
        load_cost_reports(session, http, tmp_path, CATALOG, today=later, years=1, refresh=True)
    assert route.call_count == 2
    assert again.hospitals_loaded == 3
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(CostReportRow)) == 3
        assert {r.loaded_on for r in rows_of(session)} == {later}


@respx.mock
def test_load_cost_reports_leaves_the_demo_hospital_out(tmp_path):
    header = FIXTURE.read_text(encoding="utf-8").splitlines()[0]
    demo = '"9","229999","ST. EXAMPLE","MA","2","2023-10-01","2024-09-30","1","1","1","1","1"'
    content = (header + "\n" + demo + "\n").encode("utf-8")
    serve(content, "https://data.cms.gov/f/2024.csv")
    engine = engine_with(*REGISTRY)
    with session_scope(engine) as session, httpx.Client() as http:
        report = load_cost_reports(session, http, tmp_path, CATALOG, today=TODAY, years=1)
        explicit = load_cost_reports(
            session, http, tmp_path, CATALOG, today=TODAY, years=1, ccns={"229999"}
        )
    assert (report.rows_read, report.hospitals_loaded, report.by_year) == (1, 0, {})
    assert explicit.hospitals_loaded == 0
    with session_scope(engine) as session:
        assert rows_of(session) == []


def test_shares_are_percentages_and_none_without_a_positive_denominator():
    row = SimpleNamespace(
        charity_care_cost=2060112,
        total_costs=91300000,
        bad_debt=513000,
        net_patient_revenue=100500000,
    )
    assert round(charity_share(row), 4) == 2.2564
    assert round(bad_debt_share(row), 4) == 0.5104
    assert charity_share(facts(charity_care_cost=5, total_costs=200)) == 2.5
    assert bad_debt_share(facts(bad_debt=1, net_patient_revenue=400)) == 0.25
    assert charity_share(facts(charity_care_cost=None, total_costs=200)) is None
    assert charity_share(facts(charity_care_cost=5, total_costs=None)) is None
    assert charity_share(facts(charity_care_cost=5, total_costs=0)) is None
    assert charity_share(facts(charity_care_cost=5, total_costs=-10)) is None
    assert charity_share(facts(charity_care_cost=0, total_costs=10)) == 0.0
    assert bad_debt_share(facts(bad_debt=None, net_patient_revenue=400)) is None
    assert bad_debt_share(facts(bad_debt=1, net_patient_revenue=None)) is None
    assert bad_debt_share(facts(bad_debt=1, net_patient_revenue=0)) is None
    assert bad_debt_share(CostReportRow(ccn="220001", fiscal_year=2024)) is None
    assert charity_share(CostReportRow(ccn="220001", fiscal_year=2024)) is None
