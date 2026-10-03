import base64
from datetime import date
from decimal import Decimal

import pytest

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.atlas.schema import DocType
from waive.cases.service import CaseContext, confirm_bill, set_household, start_case
from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.db import init_db, make_engine, session_scope
from waive.learning.outcomes import Decision, DenialReason, OutcomeExtract
from waive.learning.scoreboard import ACCURACY_FLOOR, MIN_OUTCOMES, queue_prechecks, scoreboard
from waive.learning.triage import record_outcome

from tests.unit.test_classify import FakeAI
from tests.unit.test_web_senior import HOSPITAL

TODAY = date(2026, 10, 2)
APPROVED = OutcomeExtract(decision=Decision.APPROVED, discount_percent=100)
DENIED = OutcomeExtract(decision=Decision.DENIED, reasons=[DenialReason.OTHER])


@pytest.fixture
def ctx():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, st_example_sheet())
        yield CaseContext(
            session=session,
            ai=FakeAI({}),
            cipher=FieldCipher(base64.b64decode(new_key())),
            signer=TokenSigner("x" * 40),
            today=TODAY,
        )


def evaluated_case(ctx):
    links = start_case(ctx, "MA")
    confirm_bill(
        ctx, links.case_id, {"statement_date": "2026-09-03", "amount_due": "1850.00"}, ccn="229999"
    )
    set_household(ctx, links.case_id, 1, Decimal("22800"), ())
    return links.case_id


def test_scoreboard_counts_only_final_outcomes_and_flags_low_accuracy(ctx):
    assert (MIN_OUTCOMES, ACCURACY_FLOOR) == (3, 0.8)
    assert scoreboard(ctx.session) == []
    for _ in range(2):
        record_outcome(ctx, evaluated_case(ctx), DENIED)
    more_info = OutcomeExtract(
        decision=Decision.MORE_INFO, documents_requested=[DocType.PROOF_OF_RESIDENCY]
    )
    record_outcome(ctx, evaluated_case(ctx), more_info)  # not a final outcome: not scored
    incomplete = OutcomeExtract(decision=Decision.DENIED, reasons=[DenialReason.INCOMPLETE])
    record_outcome(ctx, evaluated_case(ctx), incomplete)  # the case's own fault: not scored
    [score] = scoreboard(ctx.session, "MA")
    assert (score.ccn, score.outcomes, score.matched, score.accuracy) == ("229999", 2, 0, 0.0)
    assert not score.needs_recheck  # fewer than MIN_OUTCOMES scored outcomes
    assert queue_prechecks(ctx.session) == []

    record_outcome(ctx, evaluated_case(ctx), APPROVED)
    [score] = scoreboard(ctx.session, "MA")
    assert (score.outcomes, score.matched) == (3, 1)
    assert score.accuracy == pytest.approx(1 / 3) and score.needs_recheck
    assert (score.sheet_version, score.flag_level) == (1, "none")
    assert queue_prechecks(ctx.session) == ["229999"]
    assert queue_prechecks(ctx.session) == []  # one open item per hospital
    item = next(
        i for i in repo.open_review_items(ctx.session, "229999") if i.kind == "priority_recheck"
    )
    assert item.detail == {"outcomes": 3, "accuracy": 0.33}
    assert scoreboard(ctx.session, "NY") == []


def test_accurate_hospitals_are_not_queued(ctx):
    for _ in range(4):
        record_outcome(ctx, evaluated_case(ctx), APPROVED)
    record_outcome(ctx, evaluated_case(ctx), DENIED)
    [score] = scoreboard(ctx.session)
    assert score.accuracy == 0.8 and not score.needs_recheck
    assert queue_prechecks(ctx.session) == []
