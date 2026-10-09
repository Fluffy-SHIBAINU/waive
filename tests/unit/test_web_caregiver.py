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
