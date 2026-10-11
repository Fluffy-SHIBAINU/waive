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
from waive.learning.outcomes import Decision, DenialReason, OutcomeExtract

from tests.unit.test_outcomes import SAMPLE_LETTERS
from tests.unit.test_web_app import make_client
from tests.unit.test_web_senior import HOSPITAL, photo


class PaperAI:
    """Reads bills and benefit letters like the Phase 4 fake, and classifies every other photo
    as the class given."""

    def __init__(self, photo_class, outcome=None):
        self.photo_class = photo_class
        self.outcome = outcome or OutcomeExtract(
            decision=Decision.DENIED, reasons=[DenialReason.OTHER]
        )

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
        if schema is OutcomeExtract:
            return self.outcome
        return PhotoClassification(photo_class=self.photo_class, confidence=0.9)


def paper_client(photo_class, sheet=None, outcome=None):
    client, engine = make_client(ai=PaperAI(photo_class, outcome))
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
    # Phase 9.5: the page shows what was read instead of "We will read the hospital's answer".
    assert "We will read the hospital" not in thanks.text
    assert "The hospital said no." in thanks.text and "Back to what we found" in thanks.text
    assert "The hospital said: denied" in client.get(f"/c/{links.caregiver_token}").text


def test_papers_are_still_welcome_after_the_caregiver_approved():
    # Decision letters arrive after approval; the learning loop depends on them, so the lock on
    # approved cases leaves the paper step (and the gap question) open.
    client, _, links = paper_client(PhotoClass.DECISION_LETTER)
    to_result(client, links)
    caregiver = f"/c/{links.caregiver_token}"
    client.post(f"{caregiver}/approve")
    thanks = client.post(f"/s/{links.senior_token}/paper", files=photo())
    assert thanks.status_code == 200 and "The hospital said no." in thanks.text
    assert "We will read the hospital" not in thanks.text
    review = client.get(caregiver).text
    assert "The hospital said: denied" in review and "Download the packet" in review


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


def letter_client(name):
    """A case at its result whose reader takes `name`'s sample letter for what it is."""
    outcome = SAMPLE_LETTERS[name]
    photo_class = (
        PhotoClass.INFORMATION_REQUEST
        if outcome.decision is Decision.MORE_INFO
        else PhotoClass.DECISION_LETTER
    )
    client, _, links = paper_client(photo_class, outcome=outcome)
    to_result(client, links)
    return client, links


def both_links(links):
    return (f"/c/{links.caregiver_token}", True), (f"/s/{links.senior_token}", False)


def test_an_approval_letter_is_read_back_on_the_same_page_under_both_links():
    client, links = letter_client("approved")
    for base, _ in both_links(links):
        page = client.post(f"{base}/paper", files=photo())
        assert page.status_code == 200 and "<h1>Did the hospital answer?</h1>" in page.text
        assert "The hospital approved your application." in page.text
        assert "It forgave the whole bill." in page.text and "You owe $0.00 now." in page.text
        assert "We will read the hospital" not in page.text
        # The same step again, by its own address: the answer is still there.
        again = client.get(f"{base}/paper")
        assert again.status_code == 200 and "The hospital approved your application." in again.text
        assert f'action="{base}/paper"' in again.text and 'capture="environment"' in again.text
        assert again.headers["cache-control"] == "no-store"


def test_a_request_for_documents_lists_them_and_the_date_under_both_links():
    client, links = letter_client("more_documents")
    for base, _ in both_links(links):
        page = client.post(f"{base}/paper", files=photo())
        assert "The hospital asks for more documents:" in page.text
        assert "<li>Bank statements</li>" in page.text and "<li>Proof of address</li>" in page.text
        assert "Send them by November 5, 2026." in page.text
        assert "appeal" not in page.text.lower()


def test_a_denial_gives_the_reason_and_what_to_do_under_both_links():
    client, links = letter_client("denied")
    for base, _ in both_links(links):
        page = client.post(f"{base}/paper", files=photo())
        assert "The hospital said no." in page.text
        assert "Reason: documents were missing." in page.text
        assert "You can appeal until November 5, 2026." in page.text
        # Missing documents is something the person can fix: help, not an appeal draft.
        assert "Send the documents it listed." in page.text
        assert "appeal letter" not in page.text


def test_a_denial_against_the_policy_points_at_the_appeal_draft_on_the_check_page():
    # The fake reader's default answer: denied for "other" reasons although the policy says free
    # care. Triage already writes an appeal draft for that on the check page.
    client, _, links = paper_client(PhotoClass.DECISION_LETTER)
    to_result(client, links)
    full, limited = f"/c/{links.caregiver_token}", f"/s/{links.senior_token}"
    page = client.post(f"{full}/paper", files=photo())
    assert "The hospital said no." in page.text
    assert f'<a href="{full}#answer" class="btn primary">See the appeal letter</a>' in page.text
    check = client.get(full).text
    assert 'id="answer"' in check and "Appeal draft" in check
    # The limited link cannot open the check page; it is told who can.
    page = client.post(f"{limited}/paper", files=photo())
    assert "The hospital said no." in page.text and "appeal letter" in page.text
    assert "/c/" not in page.text and "Appeal draft" not in page.text
    assert "See the appeal letter" not in page.text


def test_the_letter_step_is_reached_from_the_form_and_the_check_page():
    client, links = letter_client("approved")
    base = f"/c/{links.caregiver_token}"
    assert f'href="{base}/paper"' in client.get(base).text
    form = client.post(f"{base}/approve", follow_redirects=True).text
    assert "Did the hospital answer?" in form and f'href="{base}/paper"' in form
    empty = client.get(f"{base}/paper")
    assert empty.status_code == 200 and "<h1>Did the hospital answer?</h1>" in empty.text
    assert "Take a photo of the letter" in empty.text
    assert "The hospital approved" not in empty.text  # nothing was photographed yet
    assert f'href="{base}/form"' in empty.text  # back to the form and reminders
    assert 'aria-label="Your case"' in empty.text
    assert client.get(f"/c/{links.senior_token}/paper").status_code == 403
    assert 'aria-label="Your case"' not in client.get(f"/s/{links.senior_token}/paper").text


def test_a_paper_that_is_not_an_answer_keeps_its_own_message_on_the_letter_step():
    client, _, links = paper_client(PhotoClass.BILL)
    to_result(client, links)
    for base, _ in both_links(links):
        page = client.post(f"{base}/paper", files=photo())
        assert page.status_code == 200 and "That looks like a bill" in page.text
        assert "The hospital said" not in page.text and "The hospital approved" not in page.text
