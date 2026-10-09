import random
import re
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from waive.ai.client import AIOutputError, AIUnavailable
from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.cases.extract import BillExtract, IncomeExtract
from waive.cases.service import start_case
from waive.cases.synth import make_truth, render_bill
from waive.db import CaseRow, session_scope
from waive.governor import BudgetExceeded

from tests.unit.test_web_app import make_client

HOSPITAL = {
    "ccn": "229999",
    "name": "ST. EXAMPLE MEDICAL CENTER",
    "city": "BOSTON",
    "state": "MA",
    "zip": "02118",
    "phone": "617-555-0100",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
    "website_domain": "example.org",
}


class FakeAI:
    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        if schema is BillExtract:
            return BillExtract(
                hospital_name="St. Example Medical Center",
                hospital_phone="617-555-0100",
                statement_date=date(2026, 9, 3),
                amount_due=Decimal("1850.00"),
                confidence=0.9,
            )
        return IncomeExtract(monthly_benefit=Decimal("1900"))


def seeded_client():
    client, engine = make_client(ai=FakeAI())
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, st_example_sheet())
        deps = client.app.state.deps
        links = start_case(deps.context(session), "MA")
    return client, links


def photo():
    jpeg = render_bill(
        make_truth(random.Random(1)), random.Random(1), layout=0, rotate_deg=0.0, blur=0.0
    )
    return {"photo": ("bill.jpg", jpeg, "image/jpeg")}


def test_senior_flow_from_photo_to_result():
    client, links = seeded_client()
    base = f"/s/{links.senior_token}"
    start = client.get(base)
    assert start.status_code == 200
    assert 'capture="environment"' in start.text and "Take a photo" in start.text

    readback = client.post(f"{base}/bill", files=photo())
    assert readback.status_code == 200
    assert "ST. EXAMPLE MEDICAL CENTER" in readback.text
    assert "$1,850.00" in readback.text and "September 3, 2026" in readback.text
    assert re.search(r"Yes, that.?s right", readback.text)

    household = client.post(f"{base}/confirm", data={"answer": "yes"}, follow_redirects=True)
    assert "How many people" in household.text

    income = client.post(
        f"{base}/household", data={"size": "1", "programs": "none"}, follow_redirects=True
    )
    assert "Social Security" in income.text

    result = client.post(f"{base}/income", files=photo(), follow_redirects=True)
    assert result.status_code == 200
    assert "likely do not have to pay" in result.text
    assert "Read this to me" in result.text
    assert "1850" not in str(result.url)


def test_senior_says_not_right_and_waits_for_helper():
    client, links = seeded_client()
    base = f"/s/{links.senior_token}"
    client.post(f"{base}/bill", files=photo())
    wait = client.post(f"{base}/confirm", data={"answer": "no"}, follow_redirects=True)
    assert "helper" in wait.text.lower()


def test_tampered_token_is_rejected():
    client, links = seeded_client()
    assert client.get("/s/" + links.senior_token[:-3] + "abc").status_code == 403


def approved_case():
    """A case the caregiver corrected and approved; the packet is ready."""
    client, links = seeded_client()
    caregiver = f"/c/{links.caregiver_token}"
    client.post(f"/s/{links.senior_token}/bill", files=photo())
    client.post(
        f"{caregiver}/correct",
        data={"hospital_ccn": "229999", "annual_income": "22800", "size": "1"},
    )
    client.post(f"{caregiver}/approve")
    assert client.get(f"{caregiver}/packet.pdf").status_code == 200
    return client, links, caregiver


def test_the_senior_link_cannot_change_an_approved_case():
    """The senior scope is "add photos and see the result" (case_created.html, spec §11); a
    forwarded link, or a cached form auto-submitting, must not un-approve or alter the packet."""
    client, links, caregiver = approved_case()
    senior = f"/s/{links.senior_token}"
    deps = client.app.state.deps
    attempts = (
        lambda: client.post(f"{senior}/bill", files=photo()),
        lambda: client.post(f"{senior}/confirm", data={"answer": "yes"}),
        lambda: client.post(f"{senior}/household", data={"size": "4", "programs": "none"}),
        lambda: client.post(f"{senior}/income", files=photo()),
        lambda: client.get(f"{senior}/household"),
        lambda: client.get(f"{senior}/income"),
    )
    for attempt in attempts:
        response = attempt()
        assert response.status_code == 200 and str(response.url).endswith("/result")
        assert "likely do not have to pay" in response.text
        with session_scope(deps.engine) as session:
            row = session.get(CaseRow, links.case_id)
            assert row.status == "approved"
            sealed = deps.cipher.decrypt(row.sealed)
            assert sealed["household"] == {"size": 1, "annual_income": "22800", "programs": []}
        review = client.get(caregiver).text
        assert "Approved" in review and "Download the packet" in review
    assert client.get(f"{caregiver}/packet.pdf").status_code == 200


class FailingAI:
    """Every paid read raises `error`: the governor's cap was reached, or Token Factory is down."""

    def __init__(self, error):
        self.error = error

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        BudgetExceeded("Token Factory cap of $15 reached (used $15.0)"),
        AIUnavailable("openbmb/MiniCPM-V-4_5: APIConnectionError"),
    ],
)
def test_an_unavailable_reader_is_a_kind_503_page_on_every_photo_route_not_a_500(error):
    client, engine = make_client(ai=FailingAI(error))
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, st_example_sheet())
        links = start_case(client.app.state.deps.context(session), "MA")
    client = TestClient(client.app, raise_server_exceptions=False)
    senior, caregiver = f"/s/{links.senior_token}", f"/c/{links.caregiver_token}"
    for path in (f"{senior}/bill", f"{senior}/income", f"{senior}/paper", f"{caregiver}/paper"):
        response = client.post(path, files=photo())
        assert response.status_code == 503, path
        assert "paused" in response.text.lower() and "helper" in response.text.lower()
    assert client.get("/healthz").status_code == 200


class UnreadableAI:
    """The vision model answered twice with JSON that does not fit the schema."""

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        raise AIOutputError(f"model output did not match {schema.__name__}")


def test_unreadable_model_answer_gives_the_senior_a_kind_page_not_a_500():
    client, engine = make_client(ai=UnreadableAI())
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, st_example_sheet())
        links = start_case(client.app.state.deps.context(session), "MA")
    client = TestClient(client.app, raise_server_exceptions=False)
    base = f"/s/{links.senior_token}"
    bill = client.post(f"{base}/bill", files=photo())
    assert bill.status_code == 200 and "helper" in bill.text.lower()
    letter = client.post(f"{base}/income", files=photo())
    assert letter.status_code == 200 and "skip this step" in letter.text
