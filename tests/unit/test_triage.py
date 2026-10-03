import base64
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from waive.atlas import repo
from waive.atlas.publish import drop_fields, publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.atlas.schema import DocType
from waive.cases.service import CaseContext, confirm_bill, get_row, set_household, start_case
from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.db import ReportedEvidenceRow, init_db, make_engine, session_scope
from waive.learning.evidence import DOCUMENTS_PATH, SLIP_PATH, add_evidence, support
from waive.learning.hashing import case_hash
from waive.learning.outcomes import Decision, DenialReason, OutcomeExtract
from waive.learning.triage import Triage, record_outcome, triage, triage_for
from waive.rules.eligibility import Tier

from tests.unit.test_classify import FakeAI
from tests.unit.test_web_senior import HOSPITAL

TODAY = date(2026, 10, 2)
PREDICTION = {
    "sheet_version": 1,
    "tier": "free",
    "fpl_band": "101-200",
    "predicted_documents": ["photo_id", "proof_of_income"],
    "created_on": "2026-10-02",
}


def outcome(decision, reasons=(), documents=(), discount=None):
    return OutcomeExtract(
        decision=decision,
        reasons=list(reasons),
        documents_requested=list(documents),
        discount_percent=discount,
    )


def test_triage_table():
    free = PREDICTION
    discount = {**PREDICTION, "tier": "discount"}
    not_eligible = {**PREDICTION, "tier": "not_eligible"}
    # matches
    assert triage(free, outcome(Decision.APPROVED, discount=100), 1).kind is Triage.MATCHED
    assert triage(discount, outcome(Decision.PARTIAL, discount=60), 1).matched is True
    denied_income = outcome(Decision.DENIED, [DenialReason.INCOME_TOO_HIGH])
    assert triage(not_eligible, denied_income, 1).matched is True
    # the hospital asks for a document the sheet does not list
    missing = triage(
        free,
        outcome(Decision.MORE_INFO, documents=[DocType.PROOF_OF_RESIDENCY, DocType.PHOTO_ID]),
        1,
    )
    assert missing.kind is Triage.SHEET_MISSING and missing.matched is None
    assert missing.new_documents == (DocType.PROOF_OF_RESIDENCY,)
    known = outcome(Decision.MORE_INFO, documents=[DocType.PHOTO_ID])
    assert triage(free, known, 1).kind is Triage.MATCHED and triage(free, known, 1).matched is None
    # the person's own problem: help, no atlas change
    issue = triage(free, outcome(Decision.DENIED, [DenialReason.UNSIGNED, DenialReason.LATE]), 1)
    assert issue.kind is Triage.CASE_ISSUE and issue.matched is None
    assert "Sign it" in issue.help_text and "too late" in issue.help_text
    assert issue.slip_value is None and not issue.rescout
    # a denial that contradicts an unchanged sheet is a provisional hospital slip
    slip = triage(free, outcome(Decision.DENIED, [DenialReason.OTHER]), 1)
    assert slip.kind is Triage.HOSPITAL_SLIP and slip.matched is False
    assert slip.slip_value == "denied_despite_policy" and slip.rescout
    partial = triage(free, outcome(Decision.PARTIAL, discount=60), 1)
    assert partial.kind is Triage.HOSPITAL_SLIP and partial.slip_value == "partial_despite_free"
    assert triage(free, outcome(Decision.DENIED), 1).kind is Triage.HOSPITAL_SLIP  # no reason given
    # the same denial after the sheet changed: the old sheet was wrong, nothing to re-scout
    later = triage(free, outcome(Decision.DENIED, [DenialReason.OTHER]), 2)
    assert later.kind is Triage.SHEET_WRONG and later.matched is False and not later.rescout
    # the sheet said no but the hospital helped: the sheet is too strict
    strict = triage(not_eligible, outcome(Decision.APPROVED, discount=100), 1)
    assert strict.kind is Triage.SHEET_WRONG and strict.matched is False and strict.rescout


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


def evaluated_case(ctx, income="22800", tier=Tier.FREE):
    links = start_case(ctx, "MA")
    confirm_bill(
        ctx, links.case_id, {"statement_date": "2026-09-03", "amount_due": "1850.00"}, ccn="229999"
    )
    shown = set_household(ctx, links.case_id, 1, Decimal(income), ())
    assert shown.tier is tier
    return links.case_id


def test_evidence_rows_are_deduplicated_per_case_and_enum_checked(ctx):
    session = ctx.session
    assert add_evidence(session, "229999", DOCUMENTS_PATH, "proof_of_residency", "case-1", TODAY)
    assert (
        add_evidence(session, "229999", DOCUMENTS_PATH, "proof_of_residency", "case-1", TODAY)
        is None
    )
    assert add_evidence(session, "229999", DOCUMENTS_PATH, "proof_of_residency", "case-2", TODAY)
    assert support(session, "229999", DOCUMENTS_PATH, "proof_of_residency") == 2
    with pytest.raises(ValueError):
        add_evidence(session, "229999", DOCUMENTS_PATH, "my tax papers", "case-3", TODAY)
    with pytest.raises(ValueError):
        add_evidence(session, "229999", "eligibility.free_care_max_fpl", "300", "case-3", TODAY)
    rows = list(session.scalars(select(ReportedEvidenceRow)))
    assert {row.case_hash for row in rows} == {case_hash("case-1"), case_hash("case-2")}
    assert all("case-" not in row.case_hash for row in rows)


def test_denials_that_contradict_the_sheet_queue_one_rescout_and_slip_evidence(ctx):
    first, second = evaluated_case(ctx), evaluated_case(ctx)
    denial = OutcomeExtract(
        decision=Decision.DENIED, reasons=[DenialReason.OTHER], decision_date=date(2026, 9, 20)
    )
    result = record_outcome(ctx, first, denial)
    assert result.kind is Triage.HOSPITAL_SLIP
    assert "Appeal draft" in result.appeal_text and "250%" in result.appeal_text
    assert "2026-09-20" in result.appeal_text and "Rosa" not in result.appeal_text
    record_outcome(ctx, second, denial)
    record_outcome(ctx, second, denial)  # the same case again changes nothing
    items = [
        item
        for item in repo.open_review_items(ctx.session, "229999")
        if item.kind == "rescout_request"
    ]
    assert len(items) == 1 and items[0].detail["count"] == 2
    assert sorted(items[0].detail["cases"]) == sorted([case_hash(first), case_hash(second)])
    assert support(ctx.session, "229999", SLIP_PATH, "denied_despite_policy") == 2
    row = get_row(ctx, first)
    assert row.outcome == {
        "decision": "denied",
        "recorded_on": "2026-10-02",
        "triage": "hospital_slip",
        "matched": False,
    }
    assert triage_for(ctx, first).kind is Triage.HOSPITAL_SLIP
    assert "Appeal draft" in triage_for(ctx, first).appeal_text


def test_more_info_records_missing_documents_and_case_issues_only_help(ctx):
    case_id = evaluated_case(ctx)
    request = OutcomeExtract(
        decision=Decision.MORE_INFO, documents_requested=[DocType.PROOF_OF_RESIDENCY]
    )
    assert record_outcome(ctx, case_id, request).kind is Triage.SHEET_MISSING
    assert support(ctx.session, "229999", DOCUMENTS_PATH, "proof_of_residency") == 1
    assert get_row(ctx, case_id).outcome["matched"] is None

    other = evaluated_case(ctx)
    incomplete = OutcomeExtract(decision=Decision.DENIED, reasons=[DenialReason.INCOMPLETE])
    result = record_outcome(ctx, other, incomplete)
    assert result.kind is Triage.CASE_ISSUE and "Fill in every line" in result.help_text
    assert repo.open_review_items(ctx.session, "229999") == []
    assert support(ctx.session, "229999", SLIP_PATH, "denied_despite_policy") == 0


def test_a_denial_after_the_sheet_changed_is_sheet_wrong_without_a_rescout(ctx):
    case_id = evaluated_case(ctx)
    publish_sheet(ctx.session, drop_fields(st_example_sheet(), ["contacts.phone"]))  # version 2
    denial = OutcomeExtract(decision=Decision.DENIED, reasons=[DenialReason.OTHER])
    result = record_outcome(ctx, case_id, denial)
    assert result.kind is Triage.SHEET_WRONG and result.appeal_text is None
    assert repo.open_review_items(ctx.session, "229999") == []


def test_outcome_without_a_prediction_is_recorded_but_not_scored(ctx):
    links = start_case(ctx, "MA")
    result = record_outcome(ctx, links.case_id, OutcomeExtract(decision=Decision.APPROVED))
    assert result.kind is Triage.NO_PREDICTION and result.matched is None
    assert get_row(ctx, links.case_id).outcome["triage"] == "no_prediction"
    assert triage_for(ctx, links.case_id) is None
