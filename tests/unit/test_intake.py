import base64
from datetime import date

import pytest

from waive.ai.client import AIOutputError
from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.cases.service import CaseContext, get_row, load_sealed, start_case
from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.db import init_db, make_engine, session_scope
from waive.learning.classify import PhotoClass, PhotoClassification
from waive.learning.intake import ingest_paper
from waive.learning.outcomes import Decision, DenialReason, OutcomeExtract, load_outcome

from tests.unit.test_web_senior import HOSPITAL, photo

TODAY = date(2026, 10, 2)


class ClassifyAI:
    def __init__(self, photo_class, fail=False):
        self.photo_class = photo_class
        self.fail = fail
        self.calls = []

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        self.calls.append(purpose)
        if self.fail:
            raise AIOutputError("model output did not match PhotoClassification")
        if schema is OutcomeExtract:
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


def test_unreadable_classification_falls_back_to_other(ctx):
    ctx.ai = ClassifyAI(PhotoClass.FAP, fail=True)
    links = start_case(ctx, "MA")
    result = ingest_paper(ctx, links.case_id, photo()["photo"][1])
    assert (result.photo_class, result.route) == (PhotoClass.OTHER, "ignore")
    assert "could not tell" in result.message
