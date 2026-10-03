"""Scripted outcomes drive the learning loop end to end with fakes (spec §13 simulation;
master plan Phase 5 exit checks)."""

import base64
import re
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import SAMPLE_POLICY_TEXT, st_example_sheet
from waive.atlas.schema import DocType, Layer, SourceKind
from waive.cases.service import CaseContext, confirm_bill, set_household, start_case, view
from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.db import (
    CaseRow,
    ContributionRow,
    ReportedEvidenceRow,
    init_db,
    make_engine,
    session_scope,
)
from waive.learning.classify import PhotoClass
from waive.learning.contributions import (
    approve_contribution,
    rebuild_from_sources,
    submit_contribution,
)
from waive.learning.evidence import (
    OUTCOME_KEYS,
    audit_evidence,
    publish_reported,
    slip_flag_level,
    withdraw_slips,
)
from waive.learning.outcomes import Decision, DenialReason, OutcomeExtract
from waive.learning.scoreboard import queue_prechecks, scoreboard
from waive.learning.triage import Triage, record_outcome
from waive.logging_setup import SENSITIVE
from waive.rules.eligibility import Tier

from tests.unit.test_contributions import NEW_POLICY_TEXT, PhotoAI
from tests.unit.test_web_senior import HOSPITAL

TODAY = date(2026, 10, 2)
REAL_HOSPITAL = {**HOSPITAL, "ccn": "220031", "name": "REAL GENERAL HOSPITAL", "city": "WORCESTER"}
DENIED = OutcomeExtract(
    decision=Decision.DENIED, reasons=[DenialReason.OTHER], decision_date=date(2026, 9, 20)
)
MORE_INFO = OutcomeExtract(
    decision=Decision.MORE_INFO, documents_requested=[DocType.PROOF_OF_RESIDENCY]
)
APPROVED = OutcomeExtract(decision=Decision.APPROVED, discount_percent=100)


def real_sheet():
    sample = st_example_sheet()
    hospital = sample.hospital.model_copy(
        update={"ccn": "220031", "name": "Real General Hospital", "city": "Worcester"}
    )
    return sample.model_copy(update={"hospital": hospital})


@pytest.fixture
def ctx():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        repo.upsert_hospital(session, REAL_HOSPITAL)
        sample = st_example_sheet()
        publish_sheet(session, sample)
        publish_sheet(session, real_sheet())
        repo.save_source(session, sample.sources[0], SAMPLE_POLICY_TEXT, "229999")
        yield CaseContext(
            session=session,
            ai=PhotoAI(),
            cipher=FieldCipher(base64.b64decode(new_key())),
            signer=TokenSigner("x" * 40),
            today=TODAY,
        )


def evaluated_case(ctx, ccn, income="28000"):
    """A case predicted FREE: $28,000 for one person is about 175% of the 2026 poverty line,
    under the sample policy's 250% free-care limit."""
    links = start_case(ctx, "MA")
    confirm_bill(
        ctx, links.case_id, {"statement_date": "2026-09-03", "amount_due": "1850.00"}, ccn=ccn
    )
    shown = set_household(ctx, links.case_id, 1, Decimal(income), ())
    assert shown.tier is Tier.FREE
    return links.case_id


def open_kinds(session, ccn):
    return sorted(item.kind for item in repo.open_review_items(session, ccn))


def test_three_denials_request_a_rescout_and_an_approved_photo_fixes_the_sheet(ctx):
    cases = [evaluated_case(ctx, "229999") for _ in range(4)]

    # 1. Three denials that contradict the sheet: one re-scout request, a slip flag at three.
    kinds = [record_outcome(ctx, case_id, DENIED).kind for case_id in cases[:3]]
    assert kinds == [Triage.HOSPITAL_SLIP] * 3
    assert open_kinds(ctx.session, "229999") == ["accountability_flag", "rescout_request"]
    rescout = next(
        i for i in repo.open_review_items(ctx.session, "229999") if i.kind == "rescout_request"
    )
    assert rescout.detail["count"] == 3 and len(set(rescout.detail["cases"])) == 3
    assert slip_flag_level(ctx.session, "229999") == "internal"

    # 2. A patient photographs the hospital's current policy; it passes the personal-information
    #    check, an admin approves it, and the sheet is rebuilt from stored documents (no Tavily).
    contribution = submit_contribution(
        ctx.session,
        ccn="229999",
        case_id=cases[0],
        photo_class=PhotoClass.FAP,
        text=NEW_POLICY_TEXT,
        vision_flag=False,
        today=TODAY,
    )
    assert contribution.status == "open"
    source = approve_contribution(ctx.session, contribution.id, TODAY)
    assert source.kind is SourceKind.PATIENT_PHOTO
    result = rebuild_from_sources(ctx.session, ctx.ai, "229999", TODAY)
    assert (result.outcome, result.version) == ("published", 2)
    latest, _ = repo.latest_sheet(ctx.session, "229999")
    assert latest.eligibility.free_care_max_fpl.value == 150
    assert latest.eligibility.free_care_max_fpl.source_id == source.id

    # 3. Open cases are re-evaluated against the new version; predictions stay as they were made.
    shown = view(ctx, cases[0])
    assert shown.tier is Tier.DISCOUNT and shown.result.discount_percent == 60
    assert ctx.session.get(CaseRow, cases[0]).prediction["sheet_version"] == 1

    # 4. A denial recorded now, against a prediction from the old version, is "sheet wrong".
    assert record_outcome(ctx, cases[3], DENIED).kind is Triage.SHEET_WRONG

    # 5. The admin settles the re-scout: the sheet was wrong, so the denials were not slips.
    withdraw_slips(ctx.session, "229999")
    repo.set_review_status(ctx.session, rescout.id, "resolved")
    assert slip_flag_level(ctx.session, "229999") == "none"
    assert open_kinds(ctx.session, "229999") == []


def test_a_document_hospitals_ask_for_is_reported_after_five_distinct_cases(ctx):
    cases = [evaluated_case(ctx, "220031") for _ in range(5)]
    for case_id in cases[:4]:
        assert record_outcome(ctx, case_id, MORE_INFO).kind is Triage.SHEET_MISSING
    record_outcome(ctx, cases[0], MORE_INFO)  # the same case again does not count twice
    assert publish_reported(ctx.session, "220031", TODAY) is None
    record_outcome(ctx, cases[4], MORE_INFO)
    published = publish_reported(ctx.session, "220031", TODAY)
    assert published is not None and published.version == 2
    sheet, _ = repo.latest_sheet(ctx.session, "220031")
    reported = sheet.apply.documents_reported
    assert reported.value == [DocType.PROOF_OF_RESIDENCY] and reported.layer is Layer.REPORTED
    assert reported.support_count == 5 and reported.quote is None and reported.source_id is None
    assert sheet.apply.documents_required.value == [DocType.PHOTO_ID, DocType.PROOF_OF_INCOME]
    assert open_kinds(ctx.session, "220031") == []  # reports open no review items


def test_a_hospital_slip_is_flagged_internally_at_three_and_publicly_at_five(ctx):
    cases = [evaluated_case(ctx, "220031") for _ in range(5)]
    for case_id in cases[:2]:
        record_outcome(ctx, case_id, DENIED)
    assert slip_flag_level(ctx.session, "220031") == "none"
    record_outcome(ctx, cases[2], DENIED)
    assert slip_flag_level(ctx.session, "220031") == "internal"
    flag = next(
        i for i in repo.open_review_items(ctx.session, "220031") if i.kind == "accountability_flag"
    )
    assert flag.detail == {"level": "internal", "cases": 3}
    for case_id in cases[3:]:
        record_outcome(ctx, case_id, DENIED)
    assert slip_flag_level(ctx.session, "220031") == "public"
    assert flag.detail == {"level": "public", "cases": 5}
    flags = [
        i for i in repo.open_review_items(ctx.session, "220031") if i.kind == "accountability_flag"
    ]
    assert len(flags) == 1


def test_scoreboard_queues_a_priority_recheck_for_a_bad_sheet(ctx):
    for _ in range(3):
        record_outcome(ctx, evaluated_case(ctx, "229999"), DENIED)
    record_outcome(ctx, evaluated_case(ctx, "220031"), APPROVED)
    scores = {score.ccn: score for score in scoreboard(ctx.session, "MA")}
    assert scores["229999"].accuracy == 0.0 and scores["229999"].needs_recheck
    assert scores["220031"].accuracy == 1.0 and not scores["220031"].needs_recheck
    assert queue_prechecks(ctx.session) == ["229999"]
    assert "priority_recheck" in open_kinds(ctx.session, "229999")


def test_evidence_tables_hold_no_personal_data(ctx):
    for case_id in [evaluated_case(ctx, "229999") for _ in range(3)]:
        record_outcome(ctx, case_id, DENIED)
    for case_id in [evaluated_case(ctx, "220031") for _ in range(2)]:
        record_outcome(ctx, case_id, MORE_INFO)
    submit_contribution(
        ctx.session,
        ccn="229999",
        case_id="whatever",
        photo_class=PhotoClass.FAP,
        text=NEW_POLICY_TEXT + "Patient: Rosa Alvarez, DOB 01/02/1950",
        vision_flag=False,
        today=TODAY,
    )
    assert audit_evidence(ctx.session) == []
    for row in ctx.session.scalars(select(ReportedEvidenceRow)):
        assert SENSITIVE.search(f"{row.ccn} {row.field_path} {row.value}") is None
        assert re.fullmatch(r"[0-9a-f]{16}", row.case_hash)
    for row in ctx.session.scalars(select(CaseRow).where(CaseRow.outcome.is_not(None))):
        assert set(row.outcome) <= OUTCOME_KEYS
        assert "1850" not in str(row.outcome) and "28000" not in str(row.outcome)
    for row in ctx.session.scalars(select(ContributionRow)):
        assert "Rosa" not in row.text and "1950" not in row.text
        assert row.status == "rejected"
