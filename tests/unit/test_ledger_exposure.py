"""Who each hospital serves (Phase 10, exposure lane): Medicare inpatient counts, the service
area's ZIP codes and the poor seniors living in them. No sockets: respx and tiny fixtures."""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from waive.atlas import repo as atlas_repo
from waive.db import init_db, make_engine, session_scope
from waive.ledger import repo
from waive.ledger.exposure import (
    ACS_MISSING_KEY,
    ACS_SUMMARY_URL,
    ACS_VARIABLES,
    InpatientFacts,
    ServiceArea,
    compute_exposure,
    fetch_acs_b17024,
    load_exposure,
    parse_inpatient_csv,
    parse_service_area_csv,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ledger"
INPATIENT = FIXTURES / "inpatient.csv"
SERVICE_AREA = FIXTURES / "service_area.csv"
ACS = FIXTURES / "acs_b17024.json"
TODAY = date(2026, 10, 17)
WANTED = {"220001", "310001", "330001"}

MA = {
    "ccn": "220001",
    "name": "HEALTHALLIANCE-CLINTON",
    "city": "LEOMINSTER",
    "state": "MA",
    "zip": "01453",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
}
NJ = {**MA, "ccn": "310001", "name": "JERSEY GENERAL", "city": "NEWARK", "state": "NJ"}
NY = {**MA, "ccn": "330001", "name": "EMPIRE GENERAL", "city": "ALBANY", "state": "NY"}
DEMO = {**MA, "ccn": "229999", "name": "DEMO HOSPITAL"}

INPATIENT_CSV_URL = "https://data.cms.gov/files/2026-04/abc/mup_inp_ry26_p04_v10_dy24_prv.csv"
SERVICE_AREA_CSV_URL = "https://data.cms.gov/files/2026-06/def/Hospital_Service_Area_2025.csv"
ACS_ENDPOINT = "https://api.census.gov/data/2024/acs/acs5"


def dataset(title: str, start: str, end: str, modified: str, csv_url: str) -> dict:
    return {
        "title": f"{title} : {start}",
        "modified": modified,
        "temporal": [{"startDate": start, "endDate": end}],
        "distribution": [{"mediaType": "text/csv", "downloadURL": csv_url}],
    }


# The real catalogue lists "Medicare Inpatient Hospitals - by Provider and Service" next to
# "... - by Provider", same period and same day: a prefix match must not pick the wrong one.
CATALOG = [
    dataset(
        "Medicare Inpatient Hospitals - by Provider",
        "2023-01-01",
        "2023-12-31",
        "2025-05-22",
        "https://data.cms.gov/files/old/MUP_INP_RY25_P04_V10_DY23_Prv.CSV",
    ),
    dataset(
        "Medicare Inpatient Hospitals - by Provider and Service",
        "2024-01-01",
        "2024-12-31",
        "2026-04-23",
        "https://data.cms.gov/files/wrong/MUP_INP_RY26_P03_V10_DY24_PrvSvc.CSV",
    ),
    dataset(
        "Medicare Inpatient Hospitals - by Provider",
        "2024-01-01",
        "2024-12-31",
        "2026-04-23",
        INPATIENT_CSV_URL,
    ),
    dataset("Hospital Service Area", "2024-01-01", "2024-12-31", "2025-09-10", "https://x/old.csv"),
    dataset(
        "Hospital Service Area", "2025-01-01", "2025-12-31", "2026-07-06", SERVICE_AREA_CSV_URL
    ),
]


def engine_with(*hospitals):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        for hospital in hospitals:
            atlas_repo.upsert_hospital(session, hospital)
    return engine


def test_parse_inpatient_csv_reads_the_counts_and_a_blank_count_as_none():
    facts = parse_inpatient_csv(INPATIENT)
    assert set(facts) == {"220001", "310001", "990001"}
    # The BOM never leaks into the first column's name.
    assert facts["220001"] == InpatientFacts("220001", discharges=2775, benes=1852, dual_benes=774)
    assert facts["310001"] == InpatientFacts("310001", discharges=600, benes=400, dual_benes=None)


def test_parse_service_area_csv_keeps_wanted_providers_and_counts_suppressed_rows():
    area = parse_service_area_csv(SERVICE_AREA, WANTED)
    assert isinstance(area, ServiceArea)
    # Only the wanted CCNs, only the unsuppressed rows; ZIPs keep their leading zero.
    assert area.cases == {
        "220001": {"01453": 100, "01420": 60},
        "310001": {"07101": 80, "01420": 20},
    }
    # A ZIP's total counts every provider in the file, wanted or not, hospital or not.
    assert area.zip_totals == {"01453": 120, "01420": 100, "07101": 80}
    # "*" rows are counted per wanted CCN and never add to a ZIP's total.
    assert area.suppressed == {"220001": 2, "310001": 1}


def test_compute_exposure_weights_each_zip_by_the_hospitals_share_of_its_cases():
    inpatient = parse_inpatient_csv(INPATIENT)
    area = parse_service_area_csv(SERVICE_AREA, WANTED)
    poor = {"01453": 240, "01420": 100, "07101": 400}
    facts = compute_exposure(inpatient, area, poor, WANTED)
    assert set(facts) == WANTED
    ma = facts["220001"]
    assert (ma.discharges, ma.benes, ma.dual_benes) == (2775, 1852, 774)
    assert ma.dual_discharges == pytest.approx(2775 * 774 / 1852)
    # 01453: 240 poor seniors × 100 of the ZIP's 120 cases = 200;
    # 01420: 100 × 60 of 100 = 60; the two "*" ZIPs add nothing.
    assert ma.poor_seniors_served == pytest.approx(260.0)
    assert (ma.zips_counted, ma.zips_suppressed) == (2, 2)
    nj = facts["310001"]
    # 07101: 400 × 80 of 80 = 400; 01420: 100 × 20 of 100 = 20.
    assert nj.poor_seniors_served == pytest.approx(420.0)
    assert (nj.zips_counted, nj.zips_suppressed) == (2, 1)
    # A ZIP the ACS does not know contributes nothing but still counts as served.
    facts = compute_exposure(inpatient, area, {"01453": 240}, WANTED)
    assert facts["220001"].poor_seniors_served == pytest.approx(200.0)
    assert facts["220001"].zips_counted == 2


def test_compute_exposure_gives_none_where_an_input_is_missing():
    inpatient = parse_inpatient_csv(INPATIENT)
    area = parse_service_area_csv(SERVICE_AREA, WANTED)
    facts = compute_exposure(inpatient, area, {}, WANTED)
    # A blank dual count: the counts stay, the share does not exist.
    nj = facts["310001"]
    assert (nj.discharges, nj.benes, nj.dual_benes, nj.dual_discharges) == (600, 400, None, None)
    # Absent from both files: a row of Nones and zero ZIPs.
    ny = facts["330001"]
    assert (ny.discharges, ny.benes, ny.dual_benes, ny.dual_discharges) == (
        None,
        None,
        None,
        None,
    )
    assert (ny.zips_counted, ny.zips_suppressed, ny.poor_seniors_served) == (0, 0, None)
    # Only suppressed ZIPs: counted as suppressed, no estimate.
    only_stars = ServiceArea(cases={}, zip_totals={}, suppressed={"220001": 3})
    ma = compute_exposure(inpatient, only_stars, {"01453": 240}, {"220001"})["220001"]
    assert (ma.zips_counted, ma.zips_suppressed, ma.poor_seniors_served) == (0, 3, None)
    # Zero beneficiaries never divides.
    zero = {"220001": InpatientFacts("220001", discharges=0, benes=0, dual_benes=0)}
    assert compute_exposure(zero, only_stars, {}, {"220001"})["220001"].dual_discharges is None


@respx.mock
def test_fetch_acs_b17024_sums_the_sixteen_bands_and_caches_the_answer(tmp_path):
    def answer(request):
        assert request.url.params["get"] == "NAME," + ",".join(ACS_VARIABLES)
        assert request.url.params["for"] == "zip code tabulation area:*"
        return httpx.Response(200, content=ACS.read_bytes())

    route = respx.get(ACS_ENDPOINT).mock(side_effect=answer)
    with httpx.Client() as http:
        poor = fetch_acs_b17024(http, tmp_path)
        # Negative codes ("not available") and nulls count as zero.
        assert poor == {"01453": 240, "01420": 100, "07101": 400}
        assert (tmp_path / "acs" / "b17024_2024.json").exists()
        assert fetch_acs_b17024(http, tmp_path) == poor
        assert route.call_count == 1
        fetch_acs_b17024(http, tmp_path, refresh=True)
        assert route.call_count == 2
    assert len(ACS_VARIABLES) == 16
    assert ACS_VARIABLES[0] == "B17024_107E" and ACS_VARIABLES[-1] == "B17024_127E"


# The table-based summary file names the same cells B17024_E107 and keys rows by GEO_ID;
# ZCTA rows start 860Z200US, the rest (tracts, places, ...) are skipped.
SUMMARY_DAT = (
    "GEO_ID|B17024_E001|"
    + "|".join(f"B17024_E{n:03d}" for n in (*range(107, 115), *range(120, 128)))
    + "\n"
    "1400000US25027715100|900|1|1|1|1|1|1|1|1|1|1|1|1|1|1|1|1\n"
    "860Z200US01453|9000|10|20|30|40|20|10|5|5|20|20|20|20|10|5|3|2\n"
    "860Z200US01420|9000|50|0|0|0|-666666666|0|0|0|30|20|0|0|0|0|0|0\n"
    "860Z200US07101|9000|100|100|50|50|0|0|0|0|100|0|0|0|0|0|0|\n"
    "0400000US25|9000|1|1|1|1|1|1|1|1|1|1|1|1|1|1|1|1\n"
).encode()


@respx.mock
def test_fetch_acs_b17024_falls_back_to_the_summary_file_when_the_api_wants_a_key(tmp_path):
    # api.census.gov answers a keyless request with a 302 to a "Missing Key" page.
    api = respx.get(ACS_ENDPOINT).mock(
        return_value=httpx.Response(302, headers={"Location": ACS_MISSING_KEY})
    )
    page = respx.get(ACS_MISSING_KEY).mock(
        return_value=httpx.Response(200, html="<html><title>Missing Key</title></html>")
    )
    summary = respx.get(ACS_SUMMARY_URL.format(year=2024)).mock(
        return_value=httpx.Response(200, content=SUMMARY_DAT)
    )
    with httpx.Client() as http:
        poor = fetch_acs_b17024(http, tmp_path)
        assert poor == {"01453": 240, "01420": 100, "07101": 400}
        assert (api.call_count, page.call_count, summary.call_count) == (1, 1, 1)
        # The summary file stays cached; the page never does; the JSON cache holds the answer
        # in the API's shape so the next run reads neither.
        assert (tmp_path / "acs" / "acsdt5y2024-b17024.dat").exists()
        cached = json.loads((tmp_path / "acs" / "b17024_2024.json").read_text())
        assert cached[0] == ["NAME", *ACS_VARIABLES, "zip code tabulation area"]
        assert cached[1][0] == "ZCTA5 01453" and cached[1][-1] == "01453"
        assert fetch_acs_b17024(http, tmp_path) == poor
        assert (api.call_count, summary.call_count) == (1, 1)


@respx.mock
def test_fetch_acs_b17024_replaces_a_refusal_page_left_under_the_cache_name(tmp_path):
    (tmp_path / "acs").mkdir()
    (tmp_path / "acs" / "b17024_2024.json").write_text("<html>Missing Key</html>")
    api = respx.get(ACS_ENDPOINT).mock(return_value=httpx.Response(200, content=ACS.read_bytes()))
    with httpx.Client() as http:
        assert fetch_acs_b17024(http, tmp_path) == {"01453": 240, "01420": 100, "07101": 400}
    assert api.call_count == 1
    assert json.loads((tmp_path / "acs" / "b17024_2024.json").read_text())[0][0] == "NAME"


@respx.mock
def test_load_exposure_end_to_end_then_a_rerun_updates_in_place(tmp_path):
    inpatient = respx.get(INPATIENT_CSV_URL).mock(
        return_value=httpx.Response(200, content=INPATIENT.read_bytes())
    )
    area = respx.get(SERVICE_AREA_CSV_URL).mock(
        return_value=httpx.Response(200, content=SERVICE_AREA.read_bytes())
    )
    acs = respx.get(ACS_ENDPOINT).mock(return_value=httpx.Response(200, content=ACS.read_bytes()))
    engine = engine_with(MA, NJ, NY, DEMO)
    with httpx.Client() as http, session_scope(engine) as session:
        report = load_exposure(session, http, tmp_path, CATALOG, today=TODAY)
    assert (report.hospitals, report.with_inpatient, report.with_service_area) == (3, 2, 2)
    assert report.with_both == 2
    assert (report.zips_matched_to_acs, report.acs_zctas) == (3, 3)
    assert (inpatient.call_count, area.call_count, acs.call_count) == (1, 1, 1)
    assert (tmp_path / "inpatient" / "mup_inp_ry26_p04_v10_dy24_prv.csv").exists()
    assert (tmp_path / "service_area" / "Hospital_Service_Area_2025.csv").exists()
    with session_scope(engine) as session:
        rows = repo.all_exposure(session)
        # The demo hospital gets no row; every real registry hospital does.
        assert set(rows) == {"220001", "310001", "330001"}
        ma = rows["220001"]
        assert (ma.discharges, ma.benes, ma.dual_benes) == (2775, 1852, 774)
        assert ma.dual_discharges == pytest.approx(2775 * 774 / 1852)
        assert ma.poor_seniors_served == pytest.approx(260.0)
        assert (ma.zips_counted, ma.zips_suppressed) == (2, 2)
        assert ma.inpatient_vintage == "2024-01-01 to 2024-12-31, file of 2026-04-23"
        assert ma.service_area_vintage == "2025-01-01 to 2025-12-31, file of 2026-07-06"
        assert ma.acs_vintage == "ACS 5-year 2020-2024, table B17024"
        assert ma.loaded_on == TODAY
        ny = rows["330001"]
        assert (ny.discharges, ny.dual_discharges, ny.poor_seniors_served) == (None, None, None)
        assert (ny.zips_counted, ny.zips_suppressed, ny.loaded_on) == (0, 0, TODAY)
    # A second run reads the cache and rewrites the same rows.
    later = date(2026, 11, 1)
    with httpx.Client() as http, session_scope(engine) as session:
        report = load_exposure(session, http, tmp_path, CATALOG, today=later, ccns={"220001"})
    assert (report.hospitals, report.with_both) == (1, 1)
    assert (inpatient.call_count, area.call_count, acs.call_count) == (1, 1, 1)
    with session_scope(engine) as session:
        rows = repo.all_exposure(session)
        assert set(rows) == {"220001", "310001", "330001"}
        assert rows["220001"].loaded_on == later
        assert rows["310001"].loaded_on == TODAY


def test_load_exposure_needs_both_datasets_in_the_catalogue(tmp_path):
    engine = engine_with(MA)
    with httpx.Client() as http, session_scope(engine) as session:
        with pytest.raises(ValueError, match="Hospital Service Area"):
            load_exposure(session, http, tmp_path, CATALOG[:3], today=TODAY)


def test_a_service_area_row_without_a_zip_is_neither_served_nor_suppressed(tmp_path):
    path = tmp_path / "area.csv"
    path.write_text(
        "MEDICARE_PROV_NUM,ZIP_CD_OF_RESIDENCE,TOTAL_DAYS_OF_CARE,TOTAL_CHARGES,TOTAL_CASES\n"
        "220001,,100,1000,40\n"
        "220001,01453,100,1000,30\n"
        "220001,01454,*,*,*\n",
        encoding="utf-8",
    )
    area = parse_service_area_csv(path, {"220001"})
    assert area.cases["220001"] == {"01453": 30}
    assert area.zip_totals == {"01453": 30}
    assert area.suppressed == {"220001": 1}
