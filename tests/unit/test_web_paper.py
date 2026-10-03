import re
from datetime import date
from decimal import Decimal

from waive.atlas import repo
from waive.atlas.publish import drop_fields, publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.cases.extract import BillExtract, IncomeExtract
from waive.cases.service import start_case
from waive.db import session_scope
from waive.learning.classify import PhotoClass, PhotoClassification

from tests.unit.test_web_app import make_client
from tests.unit.test_web_senior import HOSPITAL, photo


class PaperAI:
    """Reads bills and benefit letters like the Phase 4 fake, and classifies every other photo
    as the class given."""

    def __init__(self, photo_class):
        self.photo_class = photo_class

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        if schema is BillExtract:
            return BillExtract(
                hospital_name="St. Example Medical Center",
                hospital_phone="617-555-0100",
                statement_date=date(2026, 9, 3),
                amount_due=Decimal("1850.00"),
                confidence=0.9,
            )
        if schema is IncomeExtract:
            return IncomeExtract(monthly_benefit=Decimal("1900"))
        return PhotoClassification(photo_class=self.photo_class, confidence=0.9)


def paper_client(photo_class, sheet=None):
    client, engine = make_client(ai=PaperAI(photo_class))
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, sheet or st_example_sheet())
        links = start_case(client.app.state.deps.context(session), "MA")
    return client, engine, links


def to_result(client, links):
    base = f"/s/{links.senior_token}"
    client.post(f"{base}/bill", files=photo())
    client.post(f"{base}/confirm", data={"answer": "yes"})
    client.post(f"{base}/household", data={"size": "1", "programs": "none"})
    return client.post(f"{base}/income", files=photo(), follow_redirects=True)


def test_senior_result_offers_a_paper_photo_and_routes_it():
    client, _, links = paper_client(PhotoClass.DECISION_LETTER)
    result = to_result(client, links)
    assert "letter or give you papers" in result.text
    assert re.search(r'action="/s/[^"]+/paper"', result.text)
    thanks = client.post(f"/s/{links.senior_token}/paper", files=photo())
    assert thanks.status_code == 200
    assert "We will read the hospital" in thanks.text and "Back to what we found" in thanks.text


def test_caregiver_paper_upload_shows_the_routing_note():
    client, _, links = paper_client(PhotoClass.FAP)
    review = client.get(f"/c/{links.caregiver_token}")
    assert "Add a letter or paper" in review.text
    after = client.post(f"/c/{links.caregiver_token}/paper", files=photo(), follow_redirects=True)
    assert after.status_code == 200
    assert "reviewer checks it first" in after.text
    assert client.post(f"/c/{links.senior_token}/paper", files=photo()).status_code == 403


def test_senior_sees_one_targeted_question_and_can_skip_it():
    sheet = drop_fields(st_example_sheet(), ["apply.documents_required"])
    client, _, links = paper_client(PhotoClass.APPLICATION_FORM, sheet=sheet)
    result = to_result(client, links)
    assert "form to apply for help" in result.text and "Skip this" in result.text
    skipped = client.post(f"/s/{links.senior_token}/paper/skip", follow_redirects=True)
    assert "form to apply" not in skipped.text and "letter or give you papers" in skipped.text
    caregiver = client.get(f"/c/{links.caregiver_token}")
    assert "We asked" not in caregiver.text  # answered: nothing left to relay
