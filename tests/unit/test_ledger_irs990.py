"""IRS 990 lane of the ledger (Phase 10): the e-file index, bulk ZIP members and Schedule H.
No sockets: the index and the ZIP are served through respx; the ZIP is built here."""

import zipfile
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from waive.atlas import repo as atlas_repo
from waive.db import init_db, make_engine, session_scope
from waive.ledger import repo
from waive.ledger.irs990 import (
    BATCH_URL,
    INDEX_URL,
    FacilityFacts,
    IndexEntry,
    extract_members,
    fetch_index,
    fetch_return_xml,
    load_schedule_h,
    match_facility,
    parse_schedule_h,
    select_returns,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ledger"
INDEX = FIXTURES / "irs_index.csv"
SCHEDULE_H = (FIXTURES / "schedule_h.xml").read_bytes()
TODAY = date(2026, 10, 17)
UMASS_OBJECT = "202502269349301705"
MOUNT_AUBURN_OBJECT = "202522279349302182"

HOSPITAL = {
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
}
CLINTON = {
    **HOSPITAL,
    "ccn": "220001",
    "name": "HEALTHALLIANCE-CLINTON",
    "city": "LEOMINSTER",
    "state": "MA",
    "zip": "01453",
}
WORCESTER = {
    **HOSPITAL,
    "ccn": "220002",
    "name": "UMASS MEMORIAL MEDICAL CENTER",
    "city": "WORCESTER",
    "state": "MA",
    "zip": "01605",
}
RIVERBEND = {
    **HOSPITAL,
    "ccn": "220003",
    "name": "RIVERBEND CLINIC",
    "city": "SPRINGFIELD",
    "state": "MA",
    "zip": "01101",
}
MOUNT_AUBURN = {
    **HOSPITAL,
    "ccn": "220004",
    "name": "MOUNT AUBURN HOSPITAL",
    "city": "CAMBRIDGE",
    "state": "MA",
    "zip": "02138",
}
DEMO = {**HOSPITAL, "ccn": "229999", "name": "ST. EXAMPLE", "city": "X", "state": "MA", "zip": ""}
JERSEY = {**HOSPITAL, "ccn": "310001", "name": "JERSEY GENERAL", "city": "NEWARK", "state": "NJ"}


def return_xml(version: str, header: str, schedule_h: str | None) -> bytes:
    """A small return in the IRS namespace: `schedule_h` is the body of IRS990ScheduleH, or
    None for a return that carries none."""
    data = f"<IRS990ScheduleH>{schedule_h}</IRS990ScheduleH>" if schedule_h is not None else ""
    return (
        f'<?xml version="1.0"?><Return xmlns="http://www.irs.gov/efile" returnVersion="{version}">'
        f"<ReturnHeader>{header}</ReturnHeader><ReturnData><IRS990/>{data}</ReturnData></Return>"
    ).encode()


HEADER = (
    "<TaxPeriodEndDt>2024-09-30</TaxPeriodEndDt><Filer><EIN>912155626</EIN><BusinessName>"
    "<BusinessNameLine1Txt>UMass Memorial Health Care Inc &amp; Affiliates</BusinessNameLine1Txt>"
    "</BusinessName></Filer><TaxYr>2023</TaxYr>"
)
MOUNT_AUBURN_HEADER = (
    "<TaxPeriodEndDt>2024-09-30</TaxPeriodEndDt><Filer><EIN>042103606</EIN><BusinessName>"
    "<BusinessNameLine1Txt>MOUNT AUBURN HOSPITAL</BusinessNameLine1Txt></BusinessName></Filer>"
    "<TaxYr>2023</TaxYr>"
)


def section_a(
    num: int, name: str, city: str, zip_code: str = "01605", code: str | None = None
) -> str:
    group = f"<FacilityReportingGroupCd>{code}</FacilityReportingGroupCd>" if code else ""
    return (
        f"<HospitalFacilitiesGrp><FacilityNum>{num}</FacilityNum><BusinessName>"
        f"<BusinessNameLine1Txt>{name}</BusinessNameLine1Txt></BusinessName><USAddress>"
        f"<CityNm>{city}</CityNm><StateAbbreviationCd>MA</StateAbbreviationCd>"
        f"<ZIPCd>{zip_code}</ZIPCd></USAddress>{group}</HospitalFacilitiesGrp>"
    )


def section_b(num: int | None, free: str, *flags: str, name: str | None = None) -> str:
    """A Section B group; `num` None leaves FacilityNum out, `name` is HospitalFacilityName."""
    line = f"<FacilityNum>{num}</FacilityNum>" if num is not None else ""
    named = (
        f"<HospitalFacilityName><BusinessNameLine1Txt>{name}</BusinessNameLine1Txt>"
        "</HospitalFacilityName>"
        if name
        else ""
    )
    checked = "".join(f"<{flag}>X</{flag}>" for flag in flags)
    return (
        f"<HospitalFcltyPoliciesPrctcGrp>{line}{named}"
        f"<FPGFamilyIncmLmtFreeCarePct>{free}</FPGFamilyIncmLmtFreeCarePct>{checked}"
        "</HospitalFcltyPoliciesPrctcGrp>"
    )


TWO_FACILITIES = section_a(1, "UMASS MEMORIAL MEDICAL CENTER INC", "WORCESTER") + section_a(
    2, "UMASS MEMORIAL HEALTHALLIANCE-CLINTON HOSPITAL INC", "LEOMINSTER", "01453"
)


def build_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for object_id, payload in members.items():
            archive.writestr(f"{object_id}_public.xml", payload)
    return path


def entry(object_id: str, ein: str = "912155626", **overrides) -> IndexEntry:
    fields = {
        "ein": ein,
        "tax_period": "202409",
        "return_type": "990",
        "object_id": object_id,
        "batch_id": "2025_TEOS_XML_08A",
        "index_year": 2025,
        "sub_date": "2025",
        "taxpayer_name": "UMass Memorial Health Care Inc & Affiliates",
    }
    fields.update(overrides)
    return IndexEntry(**fields)


# --- the index -----------------------------------------------------------------------------


def test_select_returns_keeps_only_990s_and_the_newest_period_per_wanted_ein():
    chosen = select_returns([INDEX], {"912155626", "042103606"})
    assert set(chosen) == {"912155626", "042103606"}
    umass = chosen["912155626"]
    # Two tax periods: the newer wins; two filings of that period: the larger object id wins.
    assert (umass.tax_period, umass.object_id, umass.batch_id, umass.index_year) == (
        "202409",
        UMASS_OBJECT,
        "2025_TEOS_XML_08A",
        2025,
    )
    assert umass.sub_date == "2025" and umass.taxpayer_name.startswith("UMass Memorial")
    # The 990-T is skipped, and the file's leading-zero-stripped EIN is normalised.
    auburn = chosen["042103606"]
    assert (auburn.ein, auburn.return_type, auburn.object_id) == (
        "042103606",
        "990",
        MOUNT_AUBURN_OBJECT,
    )
    assert select_returns([INDEX], {"000000000"}) == {}


def test_select_returns_reads_several_index_files_and_keeps_the_newest_across_them(tmp_path):
    older = tmp_path / "index_2024.csv"
    rows = INDEX.read_text().splitlines()
    older.write_text("\n".join([rows[0], rows[1]]) + "\n")  # only the 202309 UMass row
    assert select_returns([older], {"912155626"})["912155626"].index_year == 2024
    assert select_returns([older, INDEX], {"912155626"})["912155626"].object_id == UMASS_OBJECT
    assert select_returns([INDEX, older], {"912155626"})["912155626"].object_id == UMASS_OBJECT


@respx.mock
def test_fetch_index_caches_the_csv_under_irs990(tmp_path):
    route = respx.get(INDEX_URL.format(year=2025)).mock(
        return_value=httpx.Response(200, content=INDEX.read_bytes())
    )
    with httpx.Client() as http:
        path = fetch_index(http, tmp_path, 2025)
        assert path == tmp_path / "irs990" / "index_2025.csv"
        assert fetch_index(http, tmp_path, 2025) == path and route.call_count == 1
        fetch_index(http, tmp_path, 2025, refresh=True)
        assert route.call_count == 2


# --- the bulk ZIPs -------------------------------------------------------------------------


@respx.mock
def test_fetch_return_xml_extracts_one_member_and_then_serves_the_cache(tmp_path):
    archive = build_zip(tmp_path / "batch.zip", {UMASS_OBJECT: SCHEDULE_H})
    route = respx.get(BATCH_URL.format(year=2025, batch_id="2025_TEOS_XML_08A")).mock(
        return_value=httpx.Response(200, content=archive.read_bytes())
    )
    cache = tmp_path / "cache"
    with httpx.Client() as http:
        path = fetch_return_xml(http, cache, entry(UMASS_OBJECT))
        assert path == cache / "irs990" / "xml" / f"{UMASS_OBJECT}_public.xml"
        assert path.read_bytes() == SCHEDULE_H
        assert (cache / "irs990" / "zips" / "2025_TEOS_XML_08A.zip").exists()
        assert fetch_return_xml(http, cache, entry(UMASS_OBJECT)) == path
        assert route.call_count == 1
        with pytest.raises(KeyError):
            fetch_return_xml(http, cache, entry("999999999999999999"))
        assert route.call_count == 1  # the cached ZIP answered
    assert not list((cache / "irs990" / "xml").glob("*.part"))


def test_extract_members_pulls_many_members_in_one_pass_and_skips_the_present(tmp_path):
    archive = build_zip(
        tmp_path / "batch.zip", {UMASS_OBJECT: SCHEDULE_H, MOUNT_AUBURN_OBJECT: b"<x/>"}
    )
    xml_dir = tmp_path / "xml"
    xml_dir.mkdir()
    (xml_dir / f"{MOUNT_AUBURN_OBJECT}_public.xml").write_bytes(b"kept")
    found = extract_members(archive, {UMASS_OBJECT, MOUNT_AUBURN_OBJECT, "1"}, xml_dir)
    assert set(found) == {UMASS_OBJECT, MOUNT_AUBURN_OBJECT}
    assert found[UMASS_OBJECT].read_bytes() == SCHEDULE_H
    # A member already on disk is not extracted again.
    assert found[MOUNT_AUBURN_OBJECT].read_bytes() == b"kept"
    assert extract_members(archive, set(), xml_dir) == {}


# --- Schedule H ----------------------------------------------------------------------------


def test_parse_schedule_h_reads_the_header_part_i_and_both_part_v_sections():
    facts = parse_schedule_h(SCHEDULE_H)
    assert (facts.ein, facts.tax_period, facts.tax_year, facts.period_end) == (
        "912155626",
        "202409",
        2023,
        date(2024, 9, 30),
    )
    assert facts.filer_name == "UMass Memorial Health Care Inc & Affiliates"
    assert facts.return_version == "2023v6.0"
    # Line 7a is a fraction of total expense in the file and a percent in the ledger.
    assert facts.charity_pct_of_expense == pytest.approx(2.61)
    assert len(facts.facilities) == 2
    first, second = facts.facilities
    assert first == FacilityFacts(
        line=1,
        name="UMASS MEMORIAL MEDICAL CENTER INC",
        city="WORCESTER",
        state="MA",
        zip5="01605",
        license="V111",
        free_care_fpl_pct=400.0,
        discount_care_fpl_pct=600.0,
        asset_test=True,
        credit_reporting=False,
        sells_debt=False,
        legal_action=False,
        lawsuits=None,
        liens=None,
        other_actions=False,
        flag_line="engaged",
        permitted_actions=("none",),
    )
    assert (second.line, second.name, second.city, second.zip5, second.license) == (
        2,
        "UMASS MEMORIAL HEALTHALLIANCE-CLINTON HOSPITAL INC",
        "LEOMINSTER",
        "01453",
        "VWPE",
    )
    assert (second.free_care_fpl_pct, second.discount_care_fpl_pct, second.asset_test) == (
        200.0,
        400.0,
        False,
    )
    # Absent checkboxes inside an existing group are False, not unknown.
    assert (second.credit_reporting, second.legal_action, second.sells_debt) == (
        True,
        True,
        False,
    )
    assert second.permitted_actions == ("credit_reporting", "legal_action")
    assert second.flag_line == "engaged"


def test_parse_schedule_h_without_the_schedule_keeps_the_header_only():
    facts = parse_schedule_h(return_xml("2023v6.0", HEADER, None))
    assert (facts.ein, facts.tax_year, facts.filer_name) == (
        "912155626",
        2023,
        "UMass Memorial Health Care Inc & Affiliates",
    )
    assert facts.charity_pct_of_expense is None and facts.facilities == ()


def test_parse_schedule_h_applies_identical_groups_to_every_facility():
    # UMass's real return: four Section A facilities, four Section B groups all numbered 1.
    body = (
        TWO_FACILITIES
        + section_b(1, "200.0", "PermitNoActionsInd")
        + section_b(1, "200.0", "PermitNoActionsInd")
    )
    facts = parse_schedule_h(return_xml("2023v6.0", HEADER, body))
    assert [
        (f.line, f.city, f.free_care_fpl_pct, f.permitted_actions) for f in facts.facilities
    ] == [
        (1, "WORCESTER", 200.0, ("none",)),
        (2, "LEOMINSTER", 200.0, ("none",)),
    ]


def test_parse_schedule_h_maps_same_numbered_differing_groups_positionally():
    body = (
        TWO_FACILITIES
        + section_b(1, "400.0", "PermitNoActionsInd")
        + section_b(1, "250.0", "PermitSellingDebtInd", "EngagedSellingDebtInd")
    )
    facts = parse_schedule_h(return_xml("2023v6.0", HEADER, body))
    assert [(f.line, f.free_care_fpl_pct, f.sells_debt) for f in facts.facilities] == [
        (1, 400.0, False),
        (2, 250.0, True),
    ]
    assert facts.facilities[1].permitted_actions == ("selling_debt",)


def test_parse_schedule_h_stores_the_first_group_unattributed_when_nothing_lines_up():
    body = (
        TWO_FACILITIES
        + section_b(1, "300.0", "PermitOtherActionsInd", "OtherActionsTakenInd")
        + section_b(1, "250.0")
        + section_b(1, "150.0")
    )
    facts = parse_schedule_h(return_xml("2023v6.0", HEADER, body))
    assert len(facts.facilities) == 1
    only = facts.facilities[0]
    assert (only.line, only.name, only.state, only.free_care_fpl_pct) == (None, None, None, 300.0)
    assert only.other_actions is True and only.permitted_actions == ("other",)


def test_parse_schedule_h_applies_a_reporting_group_to_every_facility_in_it():
    # WakeMed's real return: two facilities in reporting group A, one Section B group that
    # carries FacilityNum 2 and the name "A".
    body = (
        section_a(1, "WAKEMED RALEIGH CAMPUS", "RALEIGH", "27610", code="A")
        + section_a(2, "WAKEMED CARY HOSPITAL", "CARY", "27518", code="A")
        + section_b(2, "250.0", "PermitNoActionsInd", name="A")
    )
    facts = parse_schedule_h(return_xml("2023v6.0", HEADER, body))
    assert [
        (f.line, f.name, f.free_care_fpl_pct, f.flag_line, f.permitted_actions)
        for f in facts.facilities
    ] == [
        (1, "WAKEMED RALEIGH CAMPUS", 250.0, "engaged", ("none",)),
        (2, "WAKEMED CARY HOSPITAL", 250.0, "engaged", ("none",)),
    ]
    # Baptist Memorial's shape: the lone group has no FacilityNum and spells the group out.
    body = (
        section_a(1, "BAPTIST MEMORIAL HOSPITAL-MEMPHIS", "MEMPHIS", code="A")
        + section_a(2, "BAPTIST MEMORIAL HOSPITAL-COLLIERVILLE", "COLLIERVILLE", code="A")
        + section_a(3, "BAPTIST MEMORIAL HOSPITAL FOR WOMEN", "MEMPHIS", code="A")
        + section_b(None, "200.0", name="FACILITY REPORTING GROUP - A")
    )
    facts = parse_schedule_h(return_xml("2023v6.0", HEADER, body))
    assert [(f.line, f.free_care_fpl_pct, f.flag_line) for f in facts.facilities] == [
        (1, 200.0, "engaged"),
        (2, 200.0, "engaged"),
        (3, 200.0, "engaged"),
    ]


def test_parse_schedule_h_applies_each_reporting_group_to_its_own_members():
    # Scripps' real return: four facilities in group A, one in group B; the group A answers sit
    # on FacilityNum 4 and the group B answers on FacilityNum 1.
    body = (
        section_a(2, "SCRIPPS MERCY HOSPITAL", "SAN DIEGO", code="A")
        + section_a(1, "SCRIPPS MEMORIAL HOSPITAL LA JOLLA", "LA JOLLA", code="A")
        + section_a(3, "SCRIPPS GREEN HOSPITAL", "LA JOLLA", code="A")
        + section_a(5, "SELECT SPECIALTY HOSPITAL - SAN DIEGO", "SAN DIEGO", code="B")
        + section_b(4, "200.0", "PermitReportToCreditAgencyInd", name="A")
        + section_b(1, "300.0", "PermitNoActionsInd", name="B")
    )
    facts = parse_schedule_h(return_xml("2023v6.0", HEADER, body))
    assert [(f.line, f.free_care_fpl_pct, f.permitted_actions) for f in facts.facilities] == [
        (2, 200.0, ("credit_reporting",)),
        (1, 200.0, ("credit_reporting",)),
        (3, 200.0, ("credit_reporting",)),
        (5, 300.0, ("none",)),
    ]


def test_parse_schedule_h_joins_a_group_to_its_facility_by_name():
    # Maria Parham's real return: Section A line 1, Section B FacilityNum 5 with the same name.
    body = section_a(1, "MARIA PARHAM MEDICAL CENTER", "HENDERSON", "27536") + section_b(
        5, "0.0", "PermitSellingDebtInd", name="MARIA PARHAM MEDICAL CENTER"
    )
    facts = parse_schedule_h(return_xml("2023v6.0", HEADER, body))
    assert [(f.line, f.free_care_fpl_pct, f.permitted_actions) for f in facts.facilities] == [
        (1, 0.0, ("selling_debt",))
    ]
    # Connecticut Children's shape: every facility in group 1, one group named after the
    # hospital itself: the satellites in that group share its answers.
    body = (
        section_a(1, "CONNECTICUT CHILDREN'S MEDICAL CENTER", "HARTFORD", code="1")
        + section_a(2, "CCMC - NICU HARTFORD HOSPITAL", "HARTFORD", code="1")
        + section_b(1, "250.", name="CONNECTICUT CHILDREN'S MEDICAL CENTER")
    )
    facts = parse_schedule_h(return_xml("2023v6.0", HEADER, body))
    assert [(f.line, f.free_care_fpl_pct, f.flag_line) for f in facts.facilities] == [
        (1, 250.0, "engaged"),
        (2, 250.0, "engaged"),
    ]
    # Without reporting groups a group named for one facility answers for that facility only.
    body = (
        section_a(1, "NORTHERN MAINE MEDICAL CENTER", "FORT KENT")
        + section_a(2, "FOREST HILL", "FORT KENT")
        + section_b(1, "150.0", name="NORTHERN MAINE MEDICAL CENTER")
    )
    facts = parse_schedule_h(return_xml("2023v6.0", HEADER, body))
    assert [(f.line, f.free_care_fpl_pct, f.flag_line) for f in facts.facilities] == [
        (1, 150.0, "engaged"),
        (2, None, None),
    ]


def test_parse_schedule_h_reads_the_2014_and_2015_schemas():
    # Those schemas ask about selling debt and credit reporting but nothing about lawsuits,
    # liens or a legal process, so the legal flags are unknown rather than "no".
    body = section_a(1, "MOUNT AUBURN HOSPITAL", "CAMBRIDGE", "02138") + section_b(
        1, "200", "AssetLevelCriteriaInd", "PermitReportToCreditAgencyInd", "PermitSellingDebtInd"
    )
    facts = parse_schedule_h(return_xml("2014v5.0", MOUNT_AUBURN_HEADER, body))
    only = facts.facilities[0]
    assert (only.free_care_fpl_pct, only.asset_test, only.flag_line) == (200.0, True, "engaged")
    assert only.permitted_actions == ("credit_reporting", "selling_debt")
    assert (only.sells_debt, only.credit_reporting) == (False, False)
    assert (only.legal_action, only.lawsuits, only.liens) == (None, None, None)
    engaged = section_a(1, "MOUNT AUBURN HOSPITAL", "CAMBRIDGE", "02138") + section_b(
        1, "200", "EngagedSellingDebtInd"
    )
    facts = parse_schedule_h(return_xml("2015v3.0", MOUNT_AUBURN_HEADER, engaged))
    assert facts.facilities[0].sells_debt is True


def test_parse_schedule_h_reads_the_older_schema_names():
    header = (
        "<TaxPeriodEndDt>2014-12-31</TaxPeriodEndDt><Filer><EIN>42103606</EIN><BusinessName>"
        "<BusinessNameLine1>MOUNT AUBURN HOSPITAL</BusinessNameLine1></BusinessName></Filer>"
    )
    body = (
        "<CharityCareAtCostGrp><TotalExpensePct>0.031</TotalExpensePct></CharityCareAtCostGrp>"
        "<HospitalFacilitiesGrp><FacilityNum>1</FacilityNum><BusinessName>"
        "<BusinessNameLine1>MOUNT AUBURN HOSPITAL</BusinessNameLine1></BusinessName>"
        "<USAddress><CityNm>CAMBRIDGE</CityNm><StateAbbreviationCd>MA</StateAbbreviationCd>"
        "<ZIPCd>02138</ZIPCd></USAddress></HospitalFacilitiesGrp>"
        "<HospitalFcltyPoliciesPrctcGrp><FacilityNum>1</FacilityNum>"
        "<FPGFamilyIncmLmtFreeCarePct>200</FPGFamilyIncmLmtFreeCarePct>"
        "<AssetLevelInd>X</AssetLevelInd><PermitLawsuitInd>X</PermitLawsuitInd>"
        "<PermitLienOnResidenceInd>X</PermitLienOnResidenceInd><LawsuitInd>X</LawsuitInd>"
        "</HospitalFcltyPoliciesPrctcGrp>"
    )
    facts = parse_schedule_h(return_xml("2013v4.0", header, body))
    # No TaxYr element: a calendar-year period ending in December is that year.
    assert (facts.ein, facts.tax_year, facts.tax_period, facts.filer_name) == (
        "042103606",
        2014,
        "201412",
        "MOUNT AUBURN HOSPITAL",
    )
    assert facts.charity_pct_of_expense == pytest.approx(3.1)
    only = facts.facilities[0]
    assert (only.name, only.free_care_fpl_pct, only.asset_test) == (
        "MOUNT AUBURN HOSPITAL",
        200.0,
        True,
    )
    # Older schemas ask about lawsuits and liens on their own lines; the fused flag follows.
    assert (only.lawsuits, only.liens, only.legal_action, only.credit_reporting) == (
        True,
        False,
        True,
        False,
    )
    assert only.permitted_actions == ("legal_action",)
    # Selling debt was not asked before 2014v5.0: unknown, not "no".
    assert only.sells_debt is None


# --- matching ------------------------------------------------------------------------------


def test_match_facility_prefers_the_closest_name_in_state_with_a_zip_or_city_bonus():
    facts = parse_schedule_h(SCHEDULE_H)
    clinton = match_facility(facts, "HEALTHALLIANCE-CLINTON", "LEOMINSTER", "MA", "01453")
    assert clinton is not None and clinton.line == 2
    worcester = match_facility(facts, "UMass Memorial Medical Center", "WORCESTER", "MA", "01605")
    assert worcester is not None and worcester.line == 1
    # Two facilities in the state, none close enough: nothing is guessed.
    assert match_facility(facts, "RIVERBEND CLINIC", "SPRINGFIELD", "MA", "01101") is None
    # Another state: nothing to pick from.
    assert match_facility(facts, "HEALTHALLIANCE-CLINTON", "LEOMINSTER", "NJ", "01453") is None
    # The same generic words at two facilities: the ZIP decides.
    assert match_facility(facts, "UMASS MEMORIAL HOSPITAL", "LEOMINSTER", "MA", "01453").line == 2


def test_match_facility_takes_the_only_facility_in_the_state_whatever_its_name():
    body = section_a(1, "THE CAMBRIDGE HOSPITAL CORPORATION", "CAMBRIDGE", "02138") + section_b(
        1, "300.0"
    )
    facts = parse_schedule_h(return_xml("2023v6.0", MOUNT_AUBURN_HEADER, body))
    only = match_facility(facts, "MOUNT AUBURN HOSPITAL", "CAMBRIDGE", "MA", "02138")
    assert only is not None and only.line == 1
    assert (
        match_facility(parse_schedule_h(return_xml("2023v6.0", HEADER, None)), "X", "Y", "MA", "")
        is None
    )


# --- the loader ----------------------------------------------------------------------------


def engine_with(*hospitals):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        for hospital in hospitals:
            atlas_repo.upsert_hospital(session, hospital)
    return engine


@respx.mock
def test_load_schedule_h_end_to_end_with_hospitals_sharing_one_ein(tmp_path):
    engine = engine_with(CLINTON, WORCESTER, RIVERBEND, MOUNT_AUBURN, DEMO, JERSEY)
    with session_scope(engine) as session:
        for ccn in ("220001", "220002", "220003", "229999"):
            repo.upsert_hospital_id(session, ccn, ein="912155626", match_method="cbi")
        repo.upsert_hospital_id(session, "220004", ein="042103606", match_method="cbi")
        repo.upsert_hospital_id(session, "310001", npi="1013995133")
    archive = build_zip(
        tmp_path / "batch.zip",
        {
            UMASS_OBJECT: SCHEDULE_H,
            MOUNT_AUBURN_OBJECT: return_xml("2023v6.0", MOUNT_AUBURN_HEADER, None),
        },
    )
    index_route = respx.get(INDEX_URL.format(year=2025)).mock(
        return_value=httpx.Response(200, content=INDEX.read_bytes())
    )
    zip_route = respx.get(BATCH_URL.format(year=2025, batch_id="2025_TEOS_XML_08A")).mock(
        return_value=httpx.Response(200, content=archive.read_bytes())
    )
    cache = tmp_path / "cache"
    with httpx.Client() as http, session_scope(engine) as session:
        report = load_schedule_h(session, http, cache, today=TODAY, index_years=(2025,))
    assert (
        report.eins_wanted,
        report.returns_found,
        report.batches_downloaded,
        report.xml_fetched,
        report.hospitals_loaded,
    ) == (2, 2, 1, 2, 4)
    assert [(u["ccn"], u["ein"]) for u in report.facilities_unmatched] == [
        ("220003", "912155626"),
        ("220004", "042103606"),
    ]
    assert index_route.call_count == 1 and zip_route.call_count == 1

    with session_scope(engine) as session:
        clinton = repo.latest_schedule_h(session, "220001")
        assert (clinton.tax_year, clinton.tax_period, clinton.ein, clinton.object_id) == (
            2023,
            "202409",
            "912155626",
            UMASS_OBJECT,
        )
        assert clinton.filer_name == "UMass Memorial Health Care Inc & Affiliates"
        assert clinton.facility_name == "UMASS MEMORIAL HEALTHALLIANCE-CLINTON HOSPITAL INC"
        assert (
            clinton.facility_line,
            clinton.free_care_fpl_pct,
            clinton.discount_care_fpl_pct,
        ) == (
            2,
            200.0,
            400.0,
        )
        assert (clinton.asset_test, clinton.credit_reporting, clinton.legal_action) == (
            False,
            True,
            True,
        )
        assert (clinton.sells_debt, clinton.lawsuits, clinton.liens) == (False, None, None)
        assert clinton.flag_line == "engaged"
        assert clinton.permitted_actions == "credit_reporting,legal_action"
        assert clinton.charity_pct_of_expense == pytest.approx(2.61)
        assert clinton.vintage == "IRS e-file index 2025, filed 2025"
        assert clinton.loaded_on == TODAY

        worcester = repo.latest_schedule_h(session, "220002")
        assert (worcester.facility_line, worcester.free_care_fpl_pct, worcester.asset_test) == (
            1,
            400.0,
            True,
        )
        assert worcester.permitted_actions == "none" and worcester.credit_reporting is False

        # Unmatched: the return-level numbers and no facility.
        riverbend = repo.latest_schedule_h(session, "220003")
        assert riverbend.charity_pct_of_expense == pytest.approx(2.61)
        assert riverbend.filer_name.startswith("UMass") and riverbend.facility_name is None
        assert (riverbend.free_care_fpl_pct, riverbend.flag_line, riverbend.permitted_actions) == (
            None,
            None,
            None,
        )
        # A 990 without Schedule H: the filing is recorded, every fact stays unknown.
        auburn = repo.latest_schedule_h(session, "220004")
        assert (auburn.object_id, auburn.charity_pct_of_expense, auburn.facility_name) == (
            MOUNT_AUBURN_OBJECT,
            None,
            None,
        )
        # The demo hospital and the hospital without an EIN get nothing.
        assert repo.latest_schedule_h(session, "229999") is None
        assert repo.latest_schedule_h(session, "310001") is None

    # A second run reads everything from the cache.
    with httpx.Client() as http, session_scope(engine) as session:
        again = load_schedule_h(session, http, cache, today=TODAY, index_years=(2025,))
    assert (again.batches_downloaded, again.xml_fetched, again.hospitals_loaded) == (0, 0, 4)
    assert index_route.call_count == 1 and zip_route.call_count == 1


@respx.mock
def test_load_schedule_h_honours_the_state_filter_and_a_member_missing_from_the_zip(tmp_path):
    engine = engine_with(CLINTON, JERSEY)
    with session_scope(engine) as session:
        repo.upsert_hospital_id(session, "220001", ein="912155626")
        repo.upsert_hospital_id(session, "310001", ein="042103606")
    archive = build_zip(tmp_path / "batch.zip", {UMASS_OBJECT: SCHEDULE_H})
    respx.get(INDEX_URL.format(year=2025)).mock(
        return_value=httpx.Response(200, content=INDEX.read_bytes())
    )
    respx.get(BATCH_URL.format(year=2025, batch_id="2025_TEOS_XML_08A")).mock(
        return_value=httpx.Response(200, content=archive.read_bytes())
    )
    with httpx.Client() as http, session_scope(engine) as session:
        nj = load_schedule_h(
            session, http, tmp_path / "c", today=TODAY, index_years=(2025,), state="nj"
        )
        assert (nj.eins_wanted, nj.returns_found, nj.xml_fetched, nj.hospitals_loaded) == (
            1,
            1,
            0,
            0,
        )
        # The return the ZIP lacks is reported, not silently dropped.
        assert nj.returns_missing == [
            {"ein": "042103606", "object_id": MOUNT_AUBURN_OBJECT, "batch_id": "2025_TEOS_XML_08A"}
        ]
        everyone = load_schedule_h(session, http, tmp_path / "c", today=TODAY, index_years=(2025,))
        assert (everyone.eins_wanted, everyone.returns_found, everyone.hospitals_loaded) == (
            2,
            2,
            1,
        )
        assert [m["object_id"] for m in everyone.returns_missing] == [MOUNT_AUBURN_OBJECT]
    with session_scope(engine) as session:
        assert repo.latest_schedule_h(session, "220001").facility_line == 2
        assert repo.latest_schedule_h(session, "310001") is None


@respx.mock
def test_load_schedule_h_drops_the_former_filers_rows_when_a_hospital_changes_ein(tmp_path):
    engine = engine_with(CLINTON)
    with session_scope(engine) as session:
        repo.upsert_hospital_id(session, "220001", ein="912155626", match_method="cbi")
    # Mount Auburn's newest 990 is a year older than UMass's.
    auburn_header = MOUNT_AUBURN_HEADER.replace("2024-09-30", "2023-09-30").replace(
        "<TaxYr>2023</TaxYr>", "<TaxYr>2022</TaxYr>"
    )
    auburn_body = section_a(1, "MOUNT AUBURN HOSPITAL", "CAMBRIDGE", "02138") + section_b(
        1, "400.0", "PermitNoActionsInd"
    )
    archive = build_zip(
        tmp_path / "batch.zip",
        {
            UMASS_OBJECT: SCHEDULE_H,
            MOUNT_AUBURN_OBJECT: return_xml("2023v6.0", auburn_header, auburn_body),
        },
    )
    respx.get(INDEX_URL.format(year=2025)).mock(
        return_value=httpx.Response(200, content=INDEX.read_bytes())
    )
    respx.get(BATCH_URL.format(year=2025, batch_id="2025_TEOS_XML_08A")).mock(
        return_value=httpx.Response(200, content=archive.read_bytes())
    )
    with httpx.Client() as http, session_scope(engine) as session:
        load_schedule_h(session, http, tmp_path / "c", today=TODAY, index_years=(2025,))
        assert repo.latest_schedule_h(session, "220001").ein == "912155626"
        # The crosswalk re-matches the hospital to Mount Auburn's EIN.
        repo.upsert_hospital_id(session, "220001", ein="042103606", match_method="manual")
        load_schedule_h(session, http, tmp_path / "c", today=TODAY, index_years=(2025,))
    with session_scope(engine) as session:
        latest = repo.latest_schedule_h(session, "220001")
        assert (latest.ein, latest.tax_year, latest.object_id, latest.free_care_fpl_pct) == (
            "042103606",
            2022,
            MOUNT_AUBURN_OBJECT,
            400.0,
        )


@respx.mock
def test_load_schedule_h_reports_a_matched_facility_the_return_never_answered_for(tmp_path):
    engine = engine_with(CLINTON, WORCESTER)
    with session_scope(engine) as session:
        for ccn in ("220001", "220002"):
            repo.upsert_hospital_id(session, ccn, ein="912155626", match_method="cbi")
    # Section B answers for Worcester only; Clinton is listed in Section A and nowhere else.
    body = TWO_FACILITIES + section_b(1, "400.0", "PermitNoActionsInd")
    archive = build_zip(
        tmp_path / "batch.zip", {UMASS_OBJECT: return_xml("2023v6.0", HEADER, body)}
    )
    respx.get(INDEX_URL.format(year=2025)).mock(
        return_value=httpx.Response(200, content=INDEX.read_bytes())
    )
    respx.get(BATCH_URL.format(year=2025, batch_id="2025_TEOS_XML_08A")).mock(
        return_value=httpx.Response(200, content=archive.read_bytes())
    )
    with httpx.Client() as http, session_scope(engine) as session:
        report = load_schedule_h(session, http, tmp_path / "c", today=TODAY, index_years=(2025,))
        assert report.hospitals_loaded == 2 and report.facilities_unmatched == []
        assert [(u["ccn"], u["facility"]) for u in report.facilities_unanswered] == [
            ("220001", "UMASS MEMORIAL HEALTHALLIANCE-CLINTON HOSPITAL INC")
        ]
        clinton = repo.latest_schedule_h(session, "220001")
        assert (clinton.facility_line, clinton.free_care_fpl_pct, clinton.flag_line) == (
            2,
            None,
            None,
        )
        assert repo.latest_schedule_h(session, "220002").free_care_fpl_pct == 400.0


def test_a_deflate64_member_is_read_through_the_system_unzip(tmp_path, monkeypatch):
    """2026_TEOS_XML_05A is written with Deflate64 (method 9): zipfile raises
    NotImplementedError, and the member is pulled with `unzip -p` instead."""
    import subprocess
    import zipfile

    from waive.ledger import irs990

    zip_path = tmp_path / "2026_TEOS_XML_05A.zip"
    member = "202601149339300000_public.xml"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(member, "<Return/>")
    calls = []

    def refuse(self, name, *args, **kwargs):
        raise NotImplementedError("That compression method is not supported")

    def fake_unzip(command, stdout, stderr, check, timeout):
        calls.append(command)
        stdout.write(b"<Return>from unzip</Return>")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(zipfile.ZipFile, "open", refuse)
    monkeypatch.setattr(irs990.subprocess, "run", fake_unzip)
    found = irs990.extract_members(zip_path, ["202601149339300000"], tmp_path / "xml")
    assert (tmp_path / "xml" / member).read_bytes() == b"<Return>from unzip</Return>"
    assert calls == [["unzip", "-p", str(zip_path), member]]
    assert not list((tmp_path / "xml").glob("*.part"))
    # A member name that is not a return's never reaches a subprocess.
    with pytest.raises(ValueError):
        irs990._extract_with_unzip(str(zip_path), "../etc/passwd", None)
    assert found == {"202601149339300000": tmp_path / "xml" / member}


def test_without_unzip_the_deflate64_batch_fails_with_a_clear_message(tmp_path, monkeypatch):
    from waive.ledger import irs990

    def missing(*args, **kwargs):
        raise FileNotFoundError("unzip")

    monkeypatch.setattr(irs990.subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="unzip"):
        irs990._extract_with_unzip(str(tmp_path / "x.zip"), "202601149339300000_public.xml", None)
