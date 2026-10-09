import random
import re
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from waive.ai.client import AIOutputError
from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.cases.extract import BillExtract, IncomeExtract
from waive.cases.service import start_case
from waive.cases.synth import make_truth, render_bill
from waive.db import session_scope

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
