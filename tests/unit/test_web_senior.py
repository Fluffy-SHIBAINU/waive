import random
import re
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from waive.ai.client import AIOutputError, AIUnavailable
from waive.atlas import repo
from waive.atlas.pipeline import build_hospital
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.atlas.schema import SheetStatus
from waive.cases.extract import BillExtract, IncomeExtract
from waive.cases.service import confirm_bill, set_household, start_case
from waive.cases.synth import make_truth, render_bill
from waive.db import CaseRow, session_scope
from waive.governor import BudgetExceeded

from tests.unit.test_pipeline import UNSURE_HOSPITAL, FakeGateway
from tests.unit.test_pipeline import FakeAI as DraftAI
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
    "domain_confidence": 1.0,  # set by hand, as `waive demo seed` does
}


class FakeAI:
    def __init__(self, **bill):
        self.bill = bill

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        if schema is BillExtract:
            return BillExtract(
                hospital_name="St. Example Medical Center",
                hospital_phone="617-555-0100",
                statement_date=date(2026, 9, 3),
                amount_due=Decimal("1850.00"),
                confidence=0.9,
                **self.bill,
            )
        return IncomeExtract(monthly_benefit=Decimal("1900"))


def seeded_client(ai=None):
    client, engine = make_client(ai=ai or FakeAI())
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


NO_CHECKED_POLICY = "Waive does not have a checked policy for this hospital yet"


def test_a_sheet_held_for_its_website_never_tells_the_senior_good_news():
    # Review of 7.13 (exp4) and audit rules-correctness-9: Real General's sheet, held until an
    # admin confirms its website, still answered "Good news. You likely do not have to pay this
    # bill".
    client, links = seeded_client()
    engine = client.app.state.deps.engine
    with session_scope(engine) as session:
        repo.upsert_hospital(session, UNSURE_HOSPITAL)
        built = build_hospital(session, FakeGateway(), DraftAI(), "220031", date(2026, 10, 2))
        assert built.outcome == "held"
        ctx = client.app.state.deps.context(session)
        bill = {"statement_date": "2026-09-03", "amount_due": "1850.00"}
        confirm_bill(ctx, links.case_id, bill, ccn="220031")
        assert set_household(ctx, links.case_id, 1, Decimal("28000"), ()).under_review
    for page in (
        client.get(f"/s/{links.senior_token}"),  # where the senior's link lands
        client.get(f"/s/{links.senior_token}/result"),
        client.get(f"/c/{links.caregiver_token}/result"),
    ):
        assert page.status_code == 200
        assert "Good news" not in page.text and "likely do not have to pay" not in page.text
        assert NO_CHECKED_POLICY in page.text
    review = client.get(f"/c/{links.caregiver_token}").text
    assert NO_CHECKED_POLICY in review and "make my form" not in review
    assert client.get(f"/c/{links.caregiver_token}/packet.pdf").status_code == 404


def test_an_approved_case_offers_no_form_while_its_sheet_is_held():
    client, links, caregiver = approved_case()
    with session_scope(client.app.state.deps.engine) as session:
        sheet, _ = repo.latest_sheet(session, "229999")
        publish_sheet(session, sheet.model_copy(update={"status": SheetStatus.HELD}))
    assert client.get(f"{caregiver}/packet.pdf").status_code == 404
    assert client.get(f"{caregiver}/form", follow_redirects=False).status_code == 303
    review = client.get(caregiver).text
    assert NO_CHECKED_POLICY in review and "Your form and reminders" not in review


def test_the_senior_link_cannot_change_an_approved_case():
    """The senior scope is "add photos and see the result" (the case bar's words, spec §11); a
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


def test_a_database_that_stays_locked_is_a_kind_503_page_not_a_500(monkeypatch):
    # 2026-10-11: "Internal Server Error" after confirming a bill while a scouting batch held
    # SQLite's write lock.
    import sqlite3

    from sqlalchemy.exc import OperationalError

    from waive.web import routes_senior

    def locked(ctx, case_id, changes):
        raise OperationalError(
            "UPDATE cases SET status=?", {}, sqlite3.OperationalError("database is locked")
        )

    client, links = seeded_client()
    client = TestClient(client.app, raise_server_exceptions=False)
    senior = f"/s/{links.senior_token}"
    client.post(f"{senior}/bill", files=photo())
    monkeypatch.setattr(routes_senior, "confirm_bill", locked)
    response = client.post(f"{senior}/confirm", data={"answer": "yes"})
    assert response.status_code == 503
    assert "nothing was lost" in response.text.lower() and "try again" in response.text.lower()
    assert "Traceback" not in response.text and "UPDATE cases" not in response.text


def test_the_photo_button_opens_the_camera_and_the_page_says_it_is_reading():
    # 2026-10-11, the first test by the user: the page showed a bare file field and a submit
    # button labelled "Take a photo of the bill"; after choosing a photo nothing seemed to
    # happen while the server read it.
    client, links = seeded_client()
    start = client.get(f"/s/{links.senior_token}").text
    assert (
        '<label for="photo" class="btn primary" data-photo-button>Take a photo of the bill</label>'
    ) in start
    assert '<input id="photo" class="visually-hidden" type="file" name="photo"' in start
    assert 'capture="environment"' in start
    assert "data-photo-wait hidden>Reading your bill." in start
    assert '<script src="/static/photo.js"></script>' in start
    script = client.get("/static/photo.js").text
    assert "form[data-photo-form]" in script and "form.submit()" in script
    # No submit button that looks like the camera button, and no bare inline handler.
    assert '<button type="submit" class="btn primary">Take a photo' not in start
    assert "onchange=" not in start
    css = client.get("/static/waive.css").text
    assert "[hidden] { display: none !important; }" in css


def test_the_household_step_asks_about_a_medicaid_or_qmb_card_with_big_buttons():
    client, links = seeded_client()
    base = f"/s/{links.senior_token}"
    client.post(f"{base}/bill", files=photo())
    page = client.post(f"{base}/confirm", data={"answer": "yes"}, follow_redirects=True).text
    assert "Do you have MassHealth or a QMB card?" in page
    for label in ("Yes, MassHealth", "Yes, QMB", "SNAP only", "No, or not sure"):
        assert label in page
    assert page.count('name="programs"') == 4


def test_a_qmb_card_short_circuits_to_a_not_owed_result_and_a_complaint_letter():
    client, links = seeded_client()
    base = f"/s/{links.senior_token}"
    caregiver = f"/c/{links.caregiver_token}"
    client.post(f"{base}/bill", files=photo())
    client.post(f"{base}/confirm", data={"answer": "yes"})
    result = client.post(
        f"{base}/household", data={"size": "1", "programs": "QMB"}, follow_redirects=True
    )
    assert str(result.url).endswith("/result")  # no income step: the answer needs no income
    assert "not yours to pay" in result.text and "MassHealth or QMB card" in result.text
    assert "Good news" not in result.text and "likely do not have to pay" not in result.text
    own = client.get(f"{caregiver}/result").text
    assert "not yours to pay" in own and "we make the letter" in own.lower()
    review = client.get(caregiver).text
    assert "Likely not owed at all" in review and "make my letter" in review
    client.post(f"{caregiver}/approve")
    form = client.get(f"{caregiver}/form").text
    assert "Your letter is ready" in form and "Send it by" not in form
    assert "Copy of your MassHealth or QMB card" in form
    pdf = client.get(f"{caregiver}/packet.pdf")
    assert pdf.status_code == 200 and pdf.headers["content-disposition"].endswith('letter.pdf"')


def test_a_physician_group_bill_says_the_policy_does_not_cover_it_and_offers_a_request():
    ai = FakeAI(payee_kind="physician_group", payee_name="Example Emergency Physicians")
    client, links = seeded_client(ai)
    base = f"/s/{links.senior_token}"
    caregiver = f"/c/{links.caregiver_token}"
    client.post(f"{base}/bill", files=photo())
    client.post(f"{base}/confirm", data={"answer": "yes"})
    client.post(f"{base}/household", data={"size": "1", "programs": "none"})
    result = client.post(f"{base}/income", files=photo(), follow_redirects=True).text
    assert "policy likely does not cover this bill" in result
    assert "Example Emergency Physicians" in result and "their own financial assistance" in result
    assert "Good news" not in result
    client.post(f"{caregiver}/approve")
    form = client.get(f"{caregiver}/form").text
    assert "Example Emergency Physicians" in form and "address on the bill" in form
    assert client.get(f"{caregiver}/packet.pdf").status_code == 200


def test_an_ambulance_bill_points_to_ship_and_makes_no_packet():
    ai = FakeAI(payee_kind="ambulance", payee_name="Boston EMS")
    client, links = seeded_client(ai)
    base = f"/s/{links.senior_token}"
    caregiver = f"/c/{links.caregiver_token}"
    client.post(f"{base}/bill", files=photo())
    client.post(f"{base}/confirm", data={"answer": "yes"})
    client.post(f"{base}/household", data={"size": "1", "programs": "none"})
    result = client.post(f"{base}/income", files=photo(), follow_redirects=True).text
    assert "1-877-839-2675" in result and "Boston EMS" in result
    client.post(f"{caregiver}/approve")
    form = client.get(f"{caregiver}/form").text
    assert "1-877-839-2675" in form and "Download the packet" not in form
    assert client.get(f"{caregiver}/packet.pdf").status_code == 404


def test_the_household_step_keeps_only_the_offered_program_answers():
    client, links = seeded_client()
    base = f"/s/{links.senior_token}"
    deps = client.app.state.deps
    client.post(f"{base}/bill", files=photo())
    client.post(f"{base}/confirm", data={"answer": "yes"})
    # Untrusted form text never reaches the sealed case; a padded real answer still counts.
    client.post(f"{base}/household", data={"size": "1", "programs": "Medicare,<b>x</b>"})
    with session_scope(deps.engine) as session:
        sealed = deps.cipher.decrypt(session.get(CaseRow, links.case_id).sealed)
        assert sealed["household"]["programs"] == []
    result = client.post(
        f"{base}/household", data={"size": "1", "programs": " QMB "}, follow_redirects=True
    )
    assert "not yours to pay" in result.text


def test_the_review_page_labels_the_ambulance_approve_button():
    ai = FakeAI(payee_kind="ambulance", payee_name="Boston EMS")
    client, links = seeded_client(ai)
    base = f"/s/{links.senior_token}"
    client.post(f"{base}/bill", files=photo())
    client.post(f"{base}/confirm", data={"answer": "yes"})
    client.post(f"{base}/household", data={"size": "1", "programs": "none"})
    client.post(f"{base}/income", files=photo())
    review = client.get(f"/c/{links.caregiver_token}").text
    assert "Looks right, show me what to do" in review
