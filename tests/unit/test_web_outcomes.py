import base64
from datetime import date, timedelta

from fastapi.testclient import TestClient
from pydantic import SecretStr

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.cases.service import start_case
from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.config import Settings
from waive.db import init_db, make_engine, session_scope
from waive.learning.classify import PhotoClass
from waive.web.app import create_app

from tests.unit.test_web_paper import PaperAI, to_result
from tests.unit.test_web_senior import HOSPITAL

TODAY = date(2026, 10, 2)


def client_at(today, ai=None):
    """A seeded app whose clock the test can move (`clock["today"] = ...`)."""
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    clock = {"today": today}
    app = create_app(
        Settings(_env_file=None, nebius_api_key=SecretStr("k")),
        engine=engine,
        ai=ai or PaperAI(PhotoClass.DECISION_LETTER),
        cipher=FieldCipher(base64.b64decode(new_key())),
        signer=TokenSigner("x" * 40),
        today_fn=lambda: clock["today"],
    )
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, st_example_sheet())
        links = start_case(app.state.deps.context(session), "MA")
    return TestClient(app), clock, links


def test_caregiver_records_a_decision_by_hand_and_sees_it():
    client, _, links = client_at(TODAY)
    to_result(client, links)
    caregiver = f"/c/{links.caregiver_token}"
    assert "Enter the hospital" in client.get(caregiver).text
    after = client.post(f"{caregiver}/outcome", data={"decision": "denied"}, follow_redirects=True)
    assert "The hospital said: denied" in after.text and "Enter the hospital" not in after.text
    assert client.post(f"{caregiver}/outcome", data={"decision": "maybe"}).status_code == 404


def test_check_in_prompt_appears_after_14_days_and_can_be_answered():
    client, clock, links = client_at(TODAY)
    to_result(client, links)
    caregiver = f"/c/{links.caregiver_token}"
    client.post(f"{caregiver}/approve")
    assert "Has the hospital answered" not in client.get(caregiver).text
    clock["today"] = TODAY + timedelta(days=20)
    page = client.get(caregiver)
    assert "Has the hospital answered" in page.text and 'name="day" value="14"' in page.text
    answered = client.post(f"{caregiver}/check-in", data={"day": "14"}, follow_redirects=True)
    assert "Has the hospital answered" not in answered.text
