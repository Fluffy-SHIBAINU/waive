import re
from decimal import Decimal

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.cases.extract import BillExtract
from waive.cases.service import start_case
from waive.db import session_scope

from tests.unit.test_web_app import make_client
from tests.unit.test_web_senior import HOSPITAL, photo, seeded_client


def test_create_case_shows_both_links_and_review_works():
    client, _ = seeded_client()
    created = client.post("/cases", data={"state": "MA"})
    assert created.status_code == 200
    senior = re.search(r'href="(/s/[^"]+)"', created.text).group(1)
    caregiver = re.search(r'href="(/c/[^"]+)"', created.text).group(1)
    assert "Share this link" in created.text

    client.post(f"{senior}/bill", files=photo())
    review = client.get(caregiver)
    assert review.status_code == 200
    assert "ST. EXAMPLE MEDICAL CENTER" in review.text and "$1,850.00" in review.text

    corrected = client.post(
        f"{caregiver}/correct",
        data={
            "hospital_ccn": "229999",
            "amount_due": "1850.00",
            "statement_date": "2026-09-03",
            "size": "1",
            "annual_income": "22800",
            "programs": "",
        },
        follow_redirects=True,
    )
    assert "Likely free care" in corrected.text
    assert "Policy says" in corrected.text and "250%" in corrected.text
    assert "May 1, 2027" in corrected.text
    # The hospital's site must not learn the capability link from the Referer header.
    assert re.search(
        r'<a href="https://www\.example\.org[^"]*" rel="noopener noreferrer"', corrected.text
    )

    approved = client.post(f"{caregiver}/approve", follow_redirects=True)
    assert "Approved" in approved.text and "Download the packet" in approved.text

    pdf = client.get(f"{caregiver}/packet.pdf")
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"

    senior_token = senior.rsplit("/", 1)[1]
    assert client.post(f"/c/{senior_token}/approve").status_code == 403

    gone = client.post(f"{caregiver}/delete", follow_redirects=True)
    assert "deleted" in gone.text.lower()
    assert client.get(caregiver).status_code in (403, 404)


def test_caregiver_sees_provisional_dates_and_can_enter_the_first_bill():
    client, links = seeded_client()
    caregiver = f"/c/{links.caregiver_token}"
    client.post(f"/s/{links.senior_token}/bill", files=photo())
    page = client.post(
        f"{caregiver}/correct",
        data={"annual_income": "22800", "size": "1"},
        follow_redirects=True,
    ).text
    assert "May 1, 2027" in page and "first bill" in page and 'name="first_statement_date"' in page
    assert "came too early" not in page
    page = client.post(
        f"{caregiver}/correct",
        data={"annual_income": "22800", "size": "1", "first_statement_date": "2026-07-05"},
        follow_redirects=True,
    ).text
    assert "March 2, 2027" in page and "November 2, 2026" in page
    assert "may be earlier" not in page and 'value="2026-07-05"' in page
    assert "September 3, 2026" in page  # the statement date itself is untouched
    marked = client.post(
        f"{caregiver}/correct",
        data={
            "annual_income": "22800",
            "size": "1",
            "statement_date": "2026-08-01",
            "is_first_statement": "yes",
        },
        follow_redirects=True,
    ).text
    assert "March 29, 2027" in marked and "may be earlier" not in marked


def test_caregiver_can_make_new_links_and_the_old_ones_stop_working():
    client, links = seeded_client()
    senior, caregiver = f"/s/{links.senior_token}", f"/c/{links.caregiver_token}"
    client.post(f"{senior}/bill", files=photo())
    review = client.get(caregiver).text
    assert "Make new links" in review and f"{caregiver}/relink" in review
    assert client.post(f"/c/{links.senior_token}/relink").status_code == 403  # senior scope
    page = client.post(f"{caregiver}/relink")
    assert page.status_code == 200
    new_senior = re.search(r'href="(/s/[^"]+)"', page.text).group(1)
    new_caregiver = re.search(r'href="(/c/[^"]+)"', page.text).group(1)
    assert new_senior != senior and new_caregiver != caregiver
    assert "old links" in page.text.lower()
    assert client.get(senior).status_code == 403 and client.get(caregiver).status_code == 403
    assert client.get(new_senior).status_code == 200
    fresh = client.get(new_caregiver)
    assert fresh.status_code == 200 and "$1,850.00" in fresh.text  # the case itself survived


class UnknownHospitalAI:
    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        return BillExtract(
            hospital_name="Rosa Alvarez Memorial Clinic",  # a misread: the patient's name
            fap_url="https://pay.unknownhospital.org/acct/ACCT-20260903",
            amount_due=Decimal("100"),
        )


def test_one_tap_delete_also_removes_the_scout_request_the_bill_raised():
    client, engine = make_client(ai=UnknownHospitalAI())
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, st_example_sheet())
        links = start_case(client.app.state.deps.context(session), "MA")
    client.post(f"/s/{links.senior_token}/bill", files=photo())
    with session_scope(engine) as session:
        [request] = repo.open_review_items(session)
        assert request.kind == "scout_request" and request.ccn is None
        assert "ACCT" not in str(request.detail) and request.detail["cases"]
    gone = client.post(f"/c/{links.caregiver_token}/delete", follow_redirects=True)
    assert "deleted" in gone.text.lower()
    with session_scope(engine) as session:
        assert repo.open_review_items(session) == []


def test_correction_rejects_a_hospital_that_is_not_in_the_registry():
    client, links = seeded_client()
    caregiver = f"/c/{links.caregiver_token}"
    bad = client.post(
        f"{caregiver}/correct",
        data={"hospital_ccn": "999999", "annual_income": "22800", "size": "1"},
        follow_redirects=True,
    )
    assert bad.status_code == 404
    review = client.get(caregiver)
    assert review.status_code == 200 and "999999" not in review.text
