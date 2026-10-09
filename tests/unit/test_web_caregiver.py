import re

from tests.unit.test_web_senior import photo, seeded_client


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
