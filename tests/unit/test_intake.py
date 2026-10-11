import base64
from datetime import date

import pytest

from waive.ai.client import AIOutputError
from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.cases.service import (
    READ_LIMITS,
    CaseContext,
    TooManyReads,
    get_row,
    load_sealed,
    start_case,
)
from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.db import init_db, make_engine, session_scope
from waive.learning.classify import PhotoClass, PhotoClassification
from waive.learning.intake import MESSAGES, ingest_paper
from waive.learning.outcomes import Decision, DenialReason, OutcomeExtract, load_outcome

from tests.unit.test_web_senior import HOSPITAL, photo

TODAY = date(2026, 10, 2)


class ClassifyAI:
    def __init__(self, photo_class, fail=False, fail_outcome=False):
        self.photo_class = photo_class
        self.fail = fail
        self.fail_outcome = fail_outcome
        self.calls = []

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        self.calls.append(purpose)
        if self.fail:
            raise AIOutputError("model output did not match PhotoClassification")
        if schema is OutcomeExtract:
            if self.fail_outcome:
                raise AIOutputError("model output did not match OutcomeExtract")
            return OutcomeExtract(decision=Decision.DENIED, reasons=[DenialReason.OTHER])
        return PhotoClassification(photo_class=self.photo_class, confidence=0.9)


@pytest.fixture
def ctx():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, st_example_sheet())
        yield CaseContext(
            session=session,
            ai=ClassifyAI(PhotoClass.DECISION_LETTER),
            cipher=FieldCipher(base64.b64decode(new_key())),
            signer=TokenSigner("x" * 40),
            today=TODAY,
        )


def test_ingest_logs_the_class_in_the_sealed_blob(ctx):
    links = start_case(ctx, "MA")
    result = ingest_paper(ctx, links.case_id, photo()["photo"][1])
    assert (result.photo_class, result.route) == (PhotoClass.DECISION_LETTER, "outcome")
    assert "Thank you" in result.message
    assert ctx.ai.calls == ["learn.classify", "case.outcome"]
    assert load_outcome(ctx, links.case_id).decision is Decision.DENIED
    row = get_row(ctx, links.case_id)
    assert load_sealed(ctx, row)["papers"] == [
        {"photo_class": "decision_letter", "route": "outcome", "on": "2026-10-02"}
    ]
    assert "decision_letter" not in (row.sealed or "")


def test_papers_count_against_the_daily_read_limit(ctx):
    links = start_case(ctx, "MA")
    for _ in range(READ_LIMITS["paper"]):
        ingest_paper(ctx, links.case_id, photo()["photo"][1])
    calls = len(ctx.ai.calls)
    with pytest.raises(TooManyReads):
        ingest_paper(ctx, links.case_id, photo()["photo"][1])
    assert len(ctx.ai.calls) == calls


def test_unreadable_classification_falls_back_to_other(ctx):
    ctx.ai = ClassifyAI(PhotoClass.FAP, fail=True)
    links = start_case(ctx, "MA")
    result = ingest_paper(ctx, links.case_id, photo()["photo"][1])
    assert (result.photo_class, result.route) == (PhotoClass.OTHER, "ignore")
    assert "could not tell" in result.message


def test_the_outcome_route_returns_what_was_read(ctx):
    # The first user test (2026-10-11): after a letter was photographed the page answered "We
    # will read the hospital's answer" although it had just been read. The result now carries it.
    links = start_case(ctx, "MA")
    result = ingest_paper(ctx, links.case_id, photo()["photo"][1])
    assert result.route == "outcome"
    assert result.outcome == OutcomeExtract(decision=Decision.DENIED, reasons=[DenialReason.OTHER])
    assert result.outcome == load_outcome(ctx, links.case_id)
    assert result.triage is not None and result.triage.kind.value == "no_prediction"
    assert all("We will read" not in message for message in MESSAGES.values())
    assert "what we read" in result.message


def test_papers_on_the_other_routes_carry_no_outcome_and_keep_their_messages(ctx):
    links = start_case(ctx, "MA")
    expected = {
        PhotoClass.BILL: "Use the bill step",
        PhotoClass.SOCIAL_SECURITY_LETTER: "Use the income step",
        PhotoClass.FAP: "a reviewer checks it first",
        PhotoClass.OTHER: "could not tell what this paper is",
    }
    for photo_class, words in expected.items():
        ctx.ai = ClassifyAI(photo_class)
        result = ingest_paper(ctx, links.case_id, photo()["photo"][1])
        assert words in result.message, photo_class
        assert result.outcome is None and result.triage is None, photo_class
    assert load_outcome(ctx, links.case_id) is None


def test_an_answer_that_cannot_be_read_says_so_and_returns_nothing(ctx):
    ctx.ai = ClassifyAI(PhotoClass.DECISION_LETTER, fail_outcome=True)
    links = start_case(ctx, "MA")
    result = ingest_paper(ctx, links.case_id, photo()["photo"][1])
    assert result.route == "outcome" and result.outcome is None and result.triage is None
    assert "could not read the hospital's answer" in result.message
    assert load_outcome(ctx, links.case_id) is None
