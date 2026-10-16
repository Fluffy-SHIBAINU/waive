"""The ledger's public surfaces (Phase 10): the "What to expect here" block on the atlas page
and the under-review page, the `ledger` key of `/atlas/{ccn}.json`, the ranked list at /ledger
and the short lines on the result screen under the full link."""

from datetime import date
from decimal import Decimal

from waive.atlas import repo as atlas_repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.atlas.schema import SheetStatus
from waive.db import session_scope
from waive.ledger import repo
from waive.ledger.explain import MISSING
from waive.ledger.view import COST_REPORT_SOURCE, INPATIENT_SOURCE, SCHEDULE_H_SOURCE

from tests.unit.test_web_app import make_client
from tests.unit.test_web_senior import HOSPITAL, photo, seeded_client

TODAY = date(2026, 10, 16)
BASE = {
    "city": "BOSTON",
    "state": "MA",
    "zip": "02118",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
}
# REAL has every input and a published sheet; JERSEY has a filing, a cost report and a held sheet;
# HALF has a filing and inpatient counts and no sheet; LOW has a cost report and a sheet; BARE has
# an identity row and a sheet; PLAIN has a sheet and nothing in the ledger.
REAL = {**BASE, "ccn": "220031", "name": "REAL GENERAL HOSPITAL", "city": "WORCESTER"}
HALF = {**BASE, "ccn": "220032", "name": "HALF MEMORIAL", "city": "LOWELL"}
LOW = {**BASE, "ccn": "220033", "name": "LOW COMMUNITY", "city": "SALEM"}
BARE = {**BASE, "ccn": "220034", "name": "BARE HOSPITAL", "city": "LYNN"}
PLAIN = {**BASE, "ccn": "220035", "name": "PLAIN HOSPITAL", "city": "QUINCY"}
JERSEY = {**BASE, "ccn": "310001", "name": "JERSEY GENERAL", "city": "NEWARK", "state": "NJ"}
INPATIENT_VINTAGE = "2024-01-01 to 2024-12-31, file of 2026-10-05"
FILING_VINTAGE = "IRS e-file index 2025"


def sheet_for(hospital: dict, fpl: str, status: SheetStatus = SheetStatus.PUBLISHED):
    sample = st_example_sheet()
    ref = sample.hospital.model_copy(
        update={
            "ccn": hospital["ccn"],
            "name": hospital["name"].title(),
            "city": hospital["city"].title(),
            "state": hospital["state"],
        }
    )
    limit = sample.eligibility.free_care_max_fpl.model_copy(update={"value": Decimal(fpl)})
    eligibility = sample.eligibility.model_copy(update={"free_care_max_fpl": limit})
    return sample.model_copy(update={"hospital": ref, "eligibility": eligibility, "status": status})


def seed(engine):
    """Six real hospitals with ledger rows in every state the pages show. Ranks are stored as
    the metrics runs would leave them: REAL on top of every list, JERSEY in the middle."""
    with session_scope(engine) as session:
        for hospital in (REAL, HALF, LOW, BARE, PLAIN, JERSEY):
            atlas_repo.upsert_hospital(session, hospital)
        publish_sheet(session, sheet_for(REAL, "300"))
        publish_sheet(session, sheet_for(LOW, "200"))
        publish_sheet(session, sheet_for(BARE, "250"))
        publish_sheet(session, sheet_for(PLAIN, "250"))
        publish_sheet(session, sheet_for(JERSEY, "250", SheetStatus.HELD))
        repo.upsert_hospital_id(
            session, "220031", ein="043145034", npi="1013995133", match_method="cbi",
            match_confidence=1.0, checked_on=TODAY,
        )  # fmt: skip
        repo.upsert_hospital_id(
            session, "310001", ein="221234567", match_method="enrollments", match_confidence=0.9
        )
        repo.upsert_hospital_id(session, "220034", npi="1999999999", checked_on=TODAY)
        repo.upsert_cost_report(
            session, "220031", 2024, fy_end=date(2024, 9, 30), charity_care_cost=9,
            total_costs=1000, bad_debt=36, net_patient_revenue=1000, vintage="HCRIS 2024",
        )  # fmt: skip
        repo.upsert_cost_report(
            session, "220033", 2024, fy_end=date(2024, 12, 31), charity_care_cost=50,
            total_costs=1000, bad_debt=10, net_patient_revenue=1000, vintage="HCRIS 2024",
        )  # fmt: skip
        repo.upsert_cost_report(
            session, "310001", 2024, fy_end=date(2024, 6, 30), charity_care_cost=30,
            total_costs=1000, bad_debt=20, net_patient_revenue=1000, vintage="HCRIS 2024",
        )  # fmt: skip
        repo.upsert_schedule_h(
            session, "220031", 2024, ein="043145034", charity_pct_of_expense=1.1,
            credit_reporting=True, sells_debt=False, legal_action=True, flag_line="engaged",
            permitted_actions="credit_reporting,legal_action", vintage=FILING_VINTAGE,
        )  # fmt: skip
        repo.upsert_schedule_h(
            session, "220032", 2023, ein="043999999", free_care_fpl_pct=200.0,
            credit_reporting=False, sells_debt=False, legal_action=False, flag_line="engaged",
            permitted_actions="none", vintage=FILING_VINTAGE,
        )  # fmt: skip
        repo.upsert_schedule_h(
            session, "310001", 2024, ein="221234567", free_care_fpl_pct=250.0,
            lawsuits=False, liens=False, credit_reporting=False, sells_debt=False,
            legal_action=False, flag_line="engaged", permitted_actions="none",
            vintage=FILING_VINTAGE,
        )  # fmt: skip
        repo.upsert_exposure(
            session, "220031", discharges=1850, benes=1200, dual_benes=480, dual_discharges=770.4,
            poor_seniors_served=8397.0, inpatient_vintage=INPATIENT_VINTAGE,
            service_area_vintage="2023, file of 2026-10-05", acs_vintage="ACS 5-year 2024",
        )  # fmt: skip
        repo.upsert_exposure(
            session, "220032", discharges=400, benes=300, dual_benes=30, dual_discharges=40.0,
            inpatient_vintage=INPATIENT_VINTAGE,
        )  # fmt: skip
        repo.upsert_exposure(
            session, "310001", discharges=900, benes=700, dual_benes=210, dual_discharges=300.0,
            poor_seniors_served=2000.0, inpatient_vintage=INPATIENT_VINTAGE,
            service_area_vintage="2023, file of 2026-10-05", acs_vintage="ACS 5-year 2024",
        )  # fmt: skip
        repo.upsert_metrics(
            session, "220031", stated_fpl=300.0, stated_source="sheet", stated_rank=1.0,
            stated_peers=4, charity_cost_pct=0.9, charity_rank=0.0, charity_peers=3,
            charity_pct_990=1.1, cost_report_year=2024, gap=1.0,
            gap_label="promises_more_delivers_less", exposure_score=9167.4, exposure_rank=1.0,
            exposure_peers=3, exposure_label="high", credit_reporting=True, sells_debt=False,
            legal_action=True, flag_line="engaged", permitted_actions="credit_reporting,legal_action",
            schedule_h_year=2024, bad_debt_pct=3.6, bad_debt_rank=1.0, bad_debt_peers=3,
            behaviour_computed_on=TODAY, exposure_computed_on=TODAY,
        )  # fmt: skip
        repo.upsert_metrics(
            session, "220032", stated_fpl=200.0, stated_source="irs990", stated_rank=0.0,
            stated_peers=4, exposure_score=40.0, exposure_rank=0.0, exposure_peers=3,
            exposure_label="low", credit_reporting=False, sells_debt=False, legal_action=False,
            flag_line="engaged", permitted_actions="none", schedule_h_year=2023,
            behaviour_computed_on=TODAY, exposure_computed_on=TODAY,
        )  # fmt: skip
        repo.upsert_metrics(
            session, "220033", stated_fpl=200.0, stated_source="sheet", stated_rank=0.0,
            stated_peers=4, charity_cost_pct=5.0, charity_rank=1.0, charity_peers=3,
            cost_report_year=2024, gap=-1.0, gap_label="promises_less_delivers_more",
            bad_debt_pct=1.0, bad_debt_rank=0.0, bad_debt_peers=3, behaviour_computed_on=TODAY,
        )  # fmt: skip
        repo.upsert_metrics(
            session, "310001", stated_fpl=250.0, stated_source="irs990", stated_rank=0.5,
            stated_peers=4, charity_cost_pct=3.0, charity_rank=0.5, charity_peers=3,
            cost_report_year=2024, gap=0.0, gap_label="delivers_what_it_promises",
            exposure_score=2300.0, exposure_rank=0.5, exposure_peers=3, exposure_label="medium",
            lawsuits=False, liens=False, credit_reporting=False, sells_debt=False,
            legal_action=False, flag_line="engaged", permitted_actions="none",
            schedule_h_year=2024, bad_debt_pct=2.0, bad_debt_rank=0.5, bad_debt_peers=3,
            behaviour_computed_on=TODAY, exposure_computed_on=TODAY,
        )  # fmt: skip


def seed_demo(engine):
    """The fictional demo hospital with a metrics row: it must stay off the ranked list."""
    with session_scope(engine) as session:
        atlas_repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, st_example_sheet())
        repo.upsert_metrics(
            session, "229999", exposure_rank=0.9, exposure_label="high", exposure_computed_on=TODAY
        )


def seeded():
    client, engine = make_client()
    seed(engine)
    seed_demo(engine)
    return client


def block_of(text: str) -> str:
    """The page from the block's heading on, so an assertion cannot match the sheet above it."""
    assert "<h2>What to expect here</h2>" in text
    return text[text.index("<h2>What to expect here</h2>") :]


def test_the_block_in_the_known_state_has_three_cards_with_advice_and_sources():
    client = seeded()
    page = client.get("/atlas/220031")
    assert page.status_code == 200
    block = block_of(page.text)
    gap = block.index("Free care, promised vs delivered")
    exposure = block.index("Seniors at risk here")
    collections = block.index("<span>Collections</span>")
    assert gap < exposure < collections
    # The gap card: the warn colour for a hospital that promises more than it delivers.
    assert '<div class="card status-unknown">' in block
    assert "<strong>Promises more than it delivers</strong>" in block
    assert "Says free care goes up to 300% of the poverty level, above 9 in 10" in block
    assert "<p>What to do: Apply in writing, keep a copy, and send every document" in block
    assert '<a href="/atlas/220031">Waive policy sheet</a>, version 1 · ' in block
    assert (
        f'<a href="{COST_REPORT_SOURCE[1]}">{COST_REPORT_SOURCE[0]}</a>, '
        "year to September 2024, HCRIS 2024"
    ) in block
    assert (
        '<a href="https://projects.propublica.org/nonprofits/organizations/043145034">'
        f"{SCHEDULE_H_SOURCE[0]}</a>, tax year 2024, {FILING_VINTAGE}"
    ) in block
    # The exposure card: blue, the three federal files under it.
    assert '<div class="card status-discount">' in block
    assert "<strong>High</strong>" in block
    assert "About 770 of its 1,850 Medicare inpatients in 2024 were also on Medicaid." in block
    assert "<p>What to do: If you have both Medicare and Medicaid" in block
    assert (
        f'<a href="{INPATIENT_SOURCE[1]}">{INPATIENT_SOURCE[0]}</a>, {INPATIENT_VINTAGE}' in block
    )
    assert "Census ACS 5-year, table B17024</a>, ACS 5-year 2024" in block
    # The collections card: red, because the filing reported legal action and credit reporting.
    assert '<div class="card status-bad">' in block
    assert "<strong>Takes legal action, reports to credit agencies</strong>" in block
    assert "took legal action against them and reported them to credit agencies" in block
    assert (
        "<p>What to do: Apply within 120 days of the first bill. Waive sets the reminders" in block
    )
    assert block.count("<p>What to do: ") == 3
    assert "Not enough data yet" not in block
    # Plain words only: no enum names, no Python values, and the sheet above is still there.
    for ugly in ("promises_more", "MetricStatus", "None", "True", "[", "]"):
        assert ugly not in block, ugly
    assert "free_care_max_fpl" in page.text and "household income at or below 250%" in page.text


def test_the_block_in_the_unknown_state_says_what_is_missing_and_gives_no_advice():
    client = seeded()
    page = client.get("/atlas/220034")
    assert page.status_code == 200
    block = block_of(page.text)
    assert block.count('<strong class="muted">Not enough data yet</strong>') == 3
    assert "Free care, promised vs delivered" in block and "Collections" in block
    stated, cost = MISSING["stated"], MISSING["cost_report"]
    assert f'<p class="muted">{stated[0].upper()}{stated[1:]}; {cost}.</p>' in block
    assert MISSING["service_area"] in block
    assert "What to do" not in block
    # An unknown card carries no colour: nothing is known to colour it with.
    assert "status-bad" not in block and "status-free" not in block
    assert "status-discount" not in block and "status-unknown" not in block


def test_a_hospital_without_ledger_rows_shows_no_block_and_no_json_key():
    client = seeded()
    page = client.get("/atlas/220035")
    assert page.status_code == 200
    assert "What to expect here" not in page.text and "Not enough data" not in page.text
    assert "free_care_max_fpl" in page.text and "<h2>Sources</h2>" in page.text
    data = client.get("/atlas/220035.json").json()
    assert data["version"] == 1 and data["ledger"] is None


def test_the_under_review_page_and_its_json_carry_the_block():
    client = seeded()
    page = client.get("/atlas/310001")
    assert page.status_code == 200
    assert "Under review" in page.text and "free_care_max_fpl" not in page.text
    block = block_of(page.text)
    assert "<strong>Delivers what it promises</strong>" in block
    assert '<div class="card status-free">' in block
    assert "<strong>Medium</strong>" in block
    assert "<strong>No lawsuits, liens, credit reports or debt sales reported</strong>" in block
    assert "What to do: Apply as the policy says; most people who qualify get it." in block
    # The "Under review" card comes first, then the block, then the way back to the list.
    assert page.text.index("Under review") < page.text.index("What to expect here")
    assert page.text.index("What to expect here") < page.text.index('href="/atlas"')
    data = client.get("/atlas/310001.json").json()
    assert data["status"] == "under_review" and data["name"] == "JERSEY GENERAL"
    assert data["ledger"]["gap"]["status"] == "known"
    assert data["ledger"]["gap"]["label"] == "delivers_what_it_promises"
    assert data["ledger"]["exposure"]["label"] == "medium"


def test_the_json_key_carries_every_metric_with_its_status():
    client = seeded()
    data = client.get("/atlas/220031.json").json()
    assert data["version"] == 1 and data["eligibility"]["free_care_max_fpl"]["value"] == "300"
    ledger = data["ledger"]
    assert ledger["ccn"] == "220031" and ledger["ids"]["ein"] == "043145034"
    assert ledger["gap"]["status"] == "known"
    assert ledger["gap"]["label"] == "promises_more_delivers_less"
    assert ledger["exposure"]["status"] == "known" and ledger["exposure"]["label"] == "high"
    assert ledger["collections"]["status"] == "known"
    assert ledger["collections"]["permitted_actions"] == ["credit_reporting", "legal_action"]
    assert ledger["computed_on"] == "2026-10-16"
    bare = client.get("/atlas/220034.json").json()["ledger"]
    assert bare["ids"]["npi"] == "1999999999"
    assert {bare[m]["status"] for m in ("gap", "exposure", "collections")} == {"unknown"}
    assert bare["gap"]["missing"] == [MISSING["stated"], MISSING["cost_report"]]
    assert client.get("/atlas/000000.json").status_code == 404


def names_in_order(text: str) -> list[str]:
    names = (REAL["name"], HALF["name"], LOW["name"], JERSEY["name"])
    return sorted((n for n in names if n in text), key=text.index)


def test_the_ledger_list_sorts_by_exposure_gap_and_name_with_unknowns_last():
    client = seeded()
    page = client.get("/ledger")
    assert page.status_code == 200
    assert "<title>Hospitals by how they behave</title>" in page.text
    assert "<h1>Hospitals by how they behave</h1>" in page.text
    # Exposure is the default: REAL (rank 1), JERSEY (0.5), HALF (0), LOW (no exposure row).
    by_exposure = [REAL["name"], JERSEY["name"], HALF["name"], LOW["name"]]
    assert names_in_order(page.text) == by_exposure
    assert names_in_order(client.get("/ledger?sort=exposure").text) == by_exposure
    assert names_in_order(client.get("/ledger?sort=nonsense").text) == by_exposure
    # The gap: REAL (1.0), JERSEY (0.0), LOW (-1.0), HALF (no cost report, so no gap).
    assert names_in_order(client.get("/ledger?sort=gap").text) == [
        REAL["name"],
        JERSEY["name"],
        LOW["name"],
        HALF["name"],
    ]
    assert names_in_order(client.get("/ledger?sort=name").text) == [
        HALF["name"],
        JERSEY["name"],
        LOW["name"],
        REAL["name"],
    ]
    # The demo hospital has a metrics row and still never appears.
    assert "ST. EXAMPLE" not in page.text and "229999" not in page.text
    assert "No hospitals have ledger data yet" not in page.text


def test_the_ledger_list_filters_by_state_and_links_only_hospitals_with_a_sheet():
    client = seeded()
    page = client.get("/ledger")
    text = page.text
    assert '<form method="get" action="/ledger" class="stack">' in text
    assert '<label for="state">State</label>' in text and '<select id="state" name="state">' in text
    assert '<option value="" selected>All states</option>' in text
    assert '<option value="MA">MA</option>' in text and '<option value="NJ">NJ</option>' in text
    assert '<option value="WY"' not in text
    assert '<label for="sort">Sort by</label>' in text and '<select id="sort" name="sort">' in text
    assert '<option value="exposure" selected>Seniors at risk</option>' in text
    assert '<option value="gap">Free-care gap</option>' in text
    assert '<option value="name">Name</option>' in text
    assert '<button class="btn" type="submit">Show</button>' in text
    # Published and under review both link; no sheet, no link.
    assert '<a href="/atlas/220031"><strong>REAL GENERAL HOSPITAL</strong></a>' in text
    assert "<br>WORCESTER, MA — published" in text
    assert '<a href="/atlas/310001"><strong>JERSEY GENERAL</strong></a>' in text
    assert "<br>NEWARK, NJ — under review" in text
    assert 'href="/atlas/220032"' not in text and "<strong>HALF MEMORIAL</strong>" in text
    assert "<br>LOWELL, MA — no policy sheet yet" in text
    assert (
        '<p class="muted">Seniors at risk: High · Free care: promises more than it delivers · '
        "Collections: Takes legal action, reports to credit agencies</p>"
    ) in text
    assert (
        '<p class="muted">Seniors at risk: Low · Free care: free care promised up to 200% of '
        "the poverty level · Collections: No lawsuits, liens, credit reports or debt sales "
        "reported</p>"
    ) in text
    assert (
        '<p class="muted">Seniors at risk: not enough data · Free care: promises less, '
        "delivers more · Collections: Practices not reported; bad debt was 1% of revenue</p>"
    ) in text
    assert (
        '<p class="muted">Open data, CC BY 4.0. Every figure names its federal file and year. '
        "The hospital decides each application.</p>"
    ) in text
    # One state, lower-cased in the address, upper-cased on the page and kept in the select.
    nj = client.get("/ledger?state=nj&sort=gap").text
    assert names_in_order(nj) == [JERSEY["name"]]
    assert '<option value="NJ" selected>NJ</option>' in nj
    assert '<option value="gap" selected>Free-care gap</option>' in nj
    assert '<option value="MA">MA</option>' in nj  # every state stays offered
    empty = client.get("/ledger?state=WY").text
    assert "No hospitals have ledger data yet." in empty and names_in_order(empty) == []
    assert names_in_order(client.get("/ledger?state=mass").text) == names_in_order(text)
    # The atlas list points here.
    assert '<a href="/ledger" class="btn">Rank hospitals by how they behave</a>' in (
        client.get("/atlas").text
    )
    assert ".status-bad { border-color: var(--bad); }" in client.get("/static/waive.css").text


def test_the_ledger_list_is_empty_without_metrics_rows():
    client, engine = make_client()
    with session_scope(engine) as session:
        atlas_repo.upsert_hospital(session, REAL)
        repo.upsert_hospital_id(session, "220031", npi="1013995133", checked_on=TODAY)
    page = client.get("/ledger")
    assert page.status_code == 200
    assert "No hospitals have ledger data yet." in page.text
    assert "REAL GENERAL" not in page.text
    assert '<option value="" selected>All states</option>' in page.text


EXPECT_LINES = (
    "Free care is promised up to 300% of the poverty level, but given less often than at 9 in "
    "10 hospitals.",
    "This hospital takes legal action and reports patients to credit agencies before checking "
    "who qualifies.",
    "About 4 in 10 of its Medicare patients are also on Medicaid.",
)


def test_the_result_screen_shows_the_expect_lines_under_the_full_link_only():
    client, links = seeded_client()
    seed(client.app.state.deps.engine)
    full = f"/c/{links.caregiver_token}"
    assert client.post(f"{full}/bill", files=photo()).status_code == 200
    # The full link corrects the hospital to Real General, which has ledger rows.
    corrected = client.post(
        f"{full}/correct",
        data={"hospital_ccn": "220031", "annual_income": "22800", "size": "1"},
        follow_redirects=False,
    )
    assert corrected.status_code == 303
    result = client.get(f"{full}/result")
    assert result.status_code == 200
    assert "likely do not have to pay" in result.text
    assert "<h2>What to expect here</h2>" in result.text
    for line in EXPECT_LINES:
        assert f'<p class="big">{line}</p>' in result.text, line
    assert "<p>What to do: Apply in writing, keep a copy, and send every document" in result.text
    assert "<p>What to do: If you have both Medicare and Medicaid" in result.text
    # The lines sit between the answer and "What happens next".
    assert result.text.index("What to expect here") < result.text.index("What happens next")
    assert result.text.index("likely do not have to pay") < result.text.index("What to expect here")
    # The limited link sees the same answer and none of the ledger.
    limited = client.get(f"/s/{links.senior_token}/result")
    assert limited.status_code == 200 and "likely do not have to pay" in limited.text
    assert "What to expect here" not in limited.text
    for line in EXPECT_LINES:
        assert line not in limited.text, line


def test_the_result_screen_has_no_block_for_a_hospital_without_ledger_rows():
    # The demo hospital: the photo matches it, and it has nothing in the ledger.
    client, links = seeded_client()
    full = f"/c/{links.caregiver_token}"
    client.post(f"{full}/bill", files=photo())
    client.post(f"{full}/correct", data={"annual_income": "22800", "size": "1"})
    result = client.get(f"{full}/result")
    assert result.status_code == 200 and "likely do not have to pay" in result.text
    assert "What to expect here" not in result.text and "What to do" not in result.text


def test_the_template_global_returns_nothing_for_an_unknown_ccn_and_never_raises():
    client = seeded()
    ledger_expect = client.app.state.deps.templates.env.globals["ledger_expect"]
    assert ledger_expect("000000") == []
    assert ledger_expect(None) == [] and ledger_expect("") == []
    assert ledger_expect("220034") == []  # an identity row alone: every metric unknown
    assert [line for line, _ in ledger_expect("220031")] == list(EXPECT_LINES)
    assert ledger_expect("220033") == [
        (
            "Free care is promised up to 200% of the poverty level and given more often than "
            "at most hospitals.",
            "Ask even if the policy looks narrow; this hospital gives more free care than it "
            "advertises.",
        ),
        (
            "Whether this hospital sues or reports patients before checking who qualifies is "
            "not on file.",
            "Apply within 120 days of the first bill and keep a copy of everything.",
        ),
    ]
