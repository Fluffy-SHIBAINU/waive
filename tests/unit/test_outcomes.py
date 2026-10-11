import base64
from datetime import date
from decimal import Decimal

import pytest

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.atlas.schema import DocType
from waive.cases.service import (
    CaseContext,
    approve,
    confirm_bill,
    get_row,
    load_sealed,
    set_household,
    start_case,
)
from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.db import init_db, make_engine, session_scope
from waive.learning.outcomes import (
    CHECK_IN_DAYS,
    OUTCOME_PROMPT,
    Decision,
    DenialReason,
    OutcomeExtract,
    due_check_in,
    extract_outcome,
    load_outcome,
    outcome_text,
    pending_check_in,
    record_check_in,
    save_outcome,
)

from tests.unit.test_classify import JPEG, FakeAI
from tests.unit.test_web_senior import HOSPITAL

TODAY = date(2026, 10, 2)


def test_extract_outcome_uses_vision_with_phi_and_fixed_enums():
    ai = FakeAI(
        {
            "decision": "denied",
            "reasons": ["income_too_high", "other"],
            "documents_requested": ["proof_of_residency"],
            "decision_date": "2026-09-20",
            "discount_percent": None,
        }
    )
    result = extract_outcome(ai, JPEG)
    assert result.decision is Decision.DENIED
    assert result.reasons == [DenialReason.INCOME_TOO_HIGH, DenialReason.OTHER]
    assert result.documents_requested == [DocType.PROOF_OF_RESIDENCY]
    assert result.decision_date == date(2026, 9, 20)
    call = ai.calls[0]
    assert (call["role"], call["phi"], call["purpose"]) == ("vision", True, "case.outcome")
    assert "Never copy names" in call["messages"][0]["content"]
    with pytest.raises(ValueError):
        OutcomeExtract(decision="denied", reasons=["the patient earns too much"])


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


def test_save_outcome_keeps_details_encrypted_and_only_the_decision_in_clear(ctx):
    case_id = evaluated_case(ctx)
    assert load_outcome(ctx, case_id) is None
    outcome = OutcomeExtract(
        decision=Decision.PARTIAL,
        discount_percent=60,
        reasons=[DenialReason.INCOME_TOO_HIGH],
        decision_date=date(2026, 9, 20),
    )
    save_outcome(ctx, case_id, outcome)
    row = get_row(ctx, case_id)
    assert row.outcome == {"decision": "partial", "recorded_on": "2026-10-02"}
    assert load_sealed(ctx, row)["outcome"]["decision_date"] == "2026-09-20"
    assert "2026-09-20" not in (row.sealed or "")
    assert "discount_percent" not in row.outcome and "reasons" not in row.outcome
    assert load_outcome(ctx, case_id) == outcome
    assert row.status == "evaluated"  # outcomes never ride on the case status


def test_check_in_schedule():
    since = date(2026, 9, 1)
    assert CHECK_IN_DAYS == (14, 30, 45)
    assert due_check_in(date(2026, 9, 10), since, ()) is None
    assert due_check_in(date(2026, 9, 20), since, ()).day == 14
    assert due_check_in(date(2026, 10, 5), since, (14,)).day == 30
    assert due_check_in(date(2026, 10, 5), since, (14, 30)) is None
    assert due_check_in(date(2026, 11, 1), since, (14, 30)).day == 45
    assert "Has the hospital answered" in due_check_in(date(2026, 9, 20), since, ()).text


def test_pending_check_in_needs_an_approved_case_without_an_outcome(ctx):
    case_id = evaluated_case(ctx)
    ctx.today = date(2026, 10, 20)  # 18 days after the prediction made on 2026-10-02
    assert pending_check_in(ctx, case_id) is None  # not approved yet
    approve(ctx, case_id)
    check_in = pending_check_in(ctx, case_id)
    assert check_in is not None and check_in.day == 14
    record_check_in(ctx, case_id, 14, "no_answer")
    assert pending_check_in(ctx, case_id) is None
    assert load_sealed(ctx, get_row(ctx, case_id))["check_ins"] == {
        "14": {"answer": "no_answer", "on": "2026-10-20"}
    }
    ctx.today = date(2026, 11, 5)
    assert pending_check_in(ctx, case_id).day == 30
    save_outcome(ctx, case_id, OutcomeExtract(decision=Decision.APPROVED, discount_percent=100))
    assert pending_check_in(ctx, case_id) is None


# What a careful reader takes from the three synthetic sample letters in var/demo/
# (hospital-letter-approved.jpg, -more-documents.jpg, -denied.jpg), all dated October 6, 2026.
# Tests use these extracts; the vision model is never asked.
SAMPLE_LETTERS = {
    "approved": OutcomeExtract(
        decision=Decision.APPROVED,
        discount_percent=100,
        amount_owed=Decimal("0.00"),
        decision_date=date(2026, 10, 6),
    ),
    "more_documents": OutcomeExtract(
        decision=Decision.MORE_INFO,
        documents_requested=[DocType.BANK_STATEMENTS, DocType.PROOF_OF_RESIDENCY],
        decision_date=date(2026, 10, 6),
        respond_within_days=30,
    ),
    "denied": OutcomeExtract(
        decision=Decision.DENIED,
        reasons=[DenialReason.MISSING_DOCUMENTS],
        amount_owed=Decimal("1850.00"),
        decision_date=date(2026, 10, 6),
        respond_within_days=30,
    ),
}


def test_the_three_sample_letters_in_plain_sentences():
    approved = outcome_text(SAMPLE_LETTERS["approved"])
    assert approved.kind == "approved"
    assert approved.headline == "The hospital approved your application."
    assert approved.lines == ("It forgave the whole bill.", "You owe $0.00 now.")
    assert approved.documents == () and approved.deadline == ""

    asked = outcome_text(SAMPLE_LETTERS["more_documents"])
    assert asked.kind == "more_info"
    assert asked.headline == "The hospital asks for more documents:"
    assert asked.documents == ("Bank statements", "Proof of address")
    assert asked.deadline == "Send them by November 5, 2026."

    denied = outcome_text(SAMPLE_LETTERS["denied"])
    assert denied.kind == "denied"
    assert denied.headline == "The hospital said no."
    assert denied.lines == ("Reason: documents were missing.", "The letter says you owe $1,850.00.")
    assert denied.deadline == "You can appeal until November 5, 2026."


def test_outcome_text_only_says_what_was_read():
    bare = outcome_text(OutcomeExtract(decision=Decision.APPROVED))
    assert (bare.lines, bare.documents, bare.deadline) == ((), (), "")
    partial = outcome_text(OutcomeExtract(decision=Decision.PARTIAL, discount_percent=60))
    assert partial.kind == "approved" and partial.headline == "The hospital gave you a discount."
    assert partial.lines == ("It took 60% off the bill.",)
    # A printed deadline wins over a count of days; a count without the letter's date is
    # repeated as the letter says it.
    printed = OutcomeExtract(
        decision=Decision.MORE_INFO,
        documents_requested=[DocType.OTHER],
        decision_date=date(2026, 10, 6),
        respond_by=date(2026, 10, 20),
        respond_within_days=30,
    )
    assert outcome_text(printed).deadline == "Send them by October 20, 2026."
    assert outcome_text(printed).documents == ("Something else named in the letter",)
    undated = OutcomeExtract(decision=Decision.DENIED, respond_within_days=30)
    assert outcome_text(undated).deadline == (
        "You can appeal within 30 days of the date on the letter."
    )
    assert outcome_text(undated).lines == ()
    nothing_listed = outcome_text(OutcomeExtract(decision=Decision.MORE_INFO))
    assert nothing_listed.headline == "The hospital asks for more information."
    assert nothing_listed.lines == ("The letter says what it needs.",)
    several = OutcomeExtract(
        decision=Decision.DENIED, reasons=[DenialReason.LATE, DenialReason.UNSIGNED]
    )
    assert outcome_text(several).lines == (
        "Reason: the application came too late; the application was not signed.",
    )


def test_the_new_letter_fields_are_optional_and_asked_for():
    # Outcomes sealed before Phase 9 have none of the new fields and must still load.
    old = OutcomeExtract.model_validate({"decision": "denied", "reasons": ["other"]})
    assert (old.amount_owed, old.respond_by, old.respond_within_days) == (None, None, None)
    assert old.respond_deadline() is None
    read = OutcomeExtract.model_validate(
        {"decision": "more_info", "decision_date": "2026-10-06", "respond_within_days": 30}
    )
    assert read.respond_deadline() == date(2026, 11, 5)
    round_trip = OutcomeExtract.model_validate(SAMPLE_LETTERS["approved"].model_dump(mode="json"))
    assert round_trip == SAMPLE_LETTERS["approved"]
    for field in ("amount_owed", "respond_by", "respond_within_days"):
        assert f'"{field}"' in OUTCOME_PROMPT, field
    with pytest.raises(ValueError):
        OutcomeExtract(decision="denied", respond_within_days=-1)
    with pytest.raises(ValueError):
        OutcomeExtract(decision="denied", amount_owed="-5")
