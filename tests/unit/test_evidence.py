import base64
from datetime import date
from decimal import Decimal

import pytest

from waive.atlas import repo
from waive.atlas.pipeline import build_hospital
from waive.atlas.publish import carry_over_reported, publish_sheet
from waive.atlas.samples import SAMPLE_POLICY_TEXT, st_example_sheet
from waive.atlas.schema import Cited, DocType, Layer
from waive.cases.service import CaseContext, confirm_bill, set_household, start_case
from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.db import ReportedEvidenceRow, init_db, make_engine, session_scope
from waive.learning.evidence import (
    DOCUMENTS_PATH,
    FLAG_INTERNAL,
    FLAG_PUBLIC,
    PUBLISH_THRESHOLD,
    SLIP_PATH,
    add_evidence,
    audit_evidence,
    confirm_flag,
    flag_confirmed,
    public_flag_level,
    publish_reported,
    raise_flags,
    slip_flag_level,
    withdraw_slips,
)
from waive.learning.outcomes import Decision, DenialReason, OutcomeExtract
from waive.learning.triage import record_outcome

from tests.unit.test_classify import FakeAI
from tests.unit.test_pipeline import FakeAI as StructurerAI
from tests.unit.test_pipeline import FakeGateway
from tests.unit.test_web_app import make_client
from tests.unit.test_web_senior import HOSPITAL

TODAY = date(2026, 10, 2)


@pytest.fixture
def session():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        sheet = st_example_sheet()
        publish_sheet(session, sheet)
        repo.save_source(session, sheet.sources[0], SAMPLE_POLICY_TEXT, "229999")
        yield session


def report(session, value, cases, path=DOCUMENTS_PATH):
    for index in range(cases):
        add_evidence(session, "229999", path, value, f"case-{index}", TODAY)


def test_reported_documents_publish_only_after_five_distinct_cases(session):
    assert PUBLISH_THRESHOLD == 5
    report(session, "proof_of_residency", 4)
    assert publish_reported(session, "229999", TODAY) is None
    report(session, "proof_of_residency", 5)  # cases 0-3 again plus case-4
    report(session, "photo_id", 5)  # already documented: never reported on top
    published = publish_reported(session, "229999", TODAY)
    assert published is not None and published.version == 2
    assert published.diff == {
        "apply.documents_reported": {"old": None, "new": ["proof_of_residency"]}
    }
    sheet, _ = repo.latest_sheet(session, "229999")
    reported = sheet.apply.documents_reported
    assert reported.value == [DocType.PROOF_OF_RESIDENCY]
    assert (reported.layer, reported.support_count, reported.quote, reported.source_id) == (
        Layer.REPORTED,
        5,
        None,
        None,
    )
    assert sheet.apply.documents_required.value == [DocType.PHOTO_ID, DocType.PROOF_OF_INCOME]
    assert publish_reported(session, "229999", TODAY) is None  # nothing new: no version churn


def test_carry_over_reported_is_pure_and_never_overwrites():
    sample = st_example_sheet()
    reported = Cited[list[DocType]](
        value=[DocType.PROOF_OF_RESIDENCY], layer=Layer.REPORTED, checked_on=TODAY, support_count=5
    )
    previous = sample.model_copy(
        update={"apply": sample.apply.model_copy(update={"documents_reported": reported})}
    )
    carried = carry_over_reported(sample, previous)
    assert carried.apply.documents_reported == reported
    assert sample.apply.documents_reported is None
    assert carry_over_reported(previous, previous) == previous


def test_rebuild_from_documents_keeps_reported_fields(session):
    report(session, "proof_of_residency", 5)
    assert publish_reported(session, "229999", TODAY).version == 2
    result = build_hospital(
        session, FakeGateway(), StructurerAI(), "229999", TODAY, reuse_sources=True
    )
    assert (result.outcome, result.version) == ("published", 3)
    sheet, _ = repo.latest_sheet(session, "229999")
    assert sheet.apply.documents_reported.value == [DocType.PROOF_OF_RESIDENCY]
    assert sheet.eligibility.free_care_max_fpl.value == 250


def test_slip_flags_internal_at_three_and_public_at_five(session):
    assert (FLAG_INTERNAL, FLAG_PUBLIC) == (3, 5)
    report(session, "denied_despite_policy", 2, path=SLIP_PATH)
    assert slip_flag_level(session, "229999") == "none"
    assert raise_flags(session, "229999") is None
    report(session, "denied_despite_policy", 3, path=SLIP_PATH)
    assert slip_flag_level(session, "229999") == "internal"
    item = raise_flags(session, "229999")
    assert item.kind == "accountability_flag" and item.detail == {"level": "internal", "cases": 3}
    report(session, "partial_despite_free", 5, path=SLIP_PATH)  # five cases across both values
    assert slip_flag_level(session, "229999") == "public"
    assert raise_flags(session, "229999").id == item.id and item.detail["level"] == "public"
    flags = [
        i for i in repo.open_review_items(session, "229999") if i.kind == "accountability_flag"
    ]
    assert len(flags) == 1
    assert withdraw_slips(session, "229999") == 8  # 3 denied rows + 5 partial rows
    assert slip_flag_level(session, "229999") == "none"
    assert repo.open_review_items(session, "229999") == []


def test_public_atlas_page_shows_only_admin_confirmed_public_flags():
    """Five distinct case hashes are cheap to mint (POST /cases needs no login), so the public
    sentence about a real hospital waits for an admin's confirmation (spec §10)."""
    client, engine = make_client()
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, st_example_sheet())
        report(session, "denied_despite_policy", 3, path=SLIP_PATH)
    assert "report being denied" not in client.get("/atlas/229999").text
    with session_scope(engine) as session:
        report(session, "denied_despite_policy", 5, path=SLIP_PATH)
        raise_flags(session, "229999")
        assert slip_flag_level(session, "229999") == "public"
        assert public_flag_level(session, "229999") == "internal"
    assert "report being denied" not in client.get("/atlas/229999").text
    with session_scope(engine) as session:
        assert confirm_flag(session, "229999").status == "confirmed"
        assert public_flag_level(session, "229999") == "public"
    assert "5 patients report being denied" in client.get("/atlas/229999").text


def test_confirmed_flag_is_kept_by_raise_flags_and_withdrawn_with_the_slips(session):
    report(session, "denied_despite_policy", 5, path=SLIP_PATH)
    item = confirm_flag(session, "229999")
    assert item.kind == "accountability_flag" and item.status == "confirmed"
    assert flag_confirmed(session, "229999")
    assert repo.open_review_items(session, "229999") == []  # confirmed: out of the queue
    report(session, "partial_despite_free", 6, path=SLIP_PATH)
    assert raise_flags(session, "229999").id == item.id  # no duplicate open flag
    assert item.detail == {"level": "public", "cases": 6}
    assert repo.open_review_items(session, "229999") == []
    assert withdraw_slips(session, "229999") == 11
    assert item.status == "withdrawn"
    assert not flag_confirmed(session, "229999")
    assert public_flag_level(session, "229999") == "none"
    assert confirm_flag(session, "229999") is None  # nothing left to confirm


def test_audit_evidence_is_clean_after_real_outcomes_and_catches_bad_rows(session):
    ctx = CaseContext(
        session=session,
        ai=FakeAI({}),
        cipher=FieldCipher(base64.b64decode(new_key())),
        signer=TokenSigner("x" * 40),
        today=TODAY,
    )
    for _ in range(3):
        links = start_case(ctx, "MA")
        confirm_bill(
            ctx,
            links.case_id,
            {"statement_date": "2026-09-03", "amount_due": "1850.00"},
            ccn="229999",
        )
        set_household(ctx, links.case_id, 1, Decimal("22800"), ())
        denial = OutcomeExtract(decision=Decision.DENIED, reasons=[DenialReason.OTHER])
        record_outcome(ctx, links.case_id, denial)
    assert slip_flag_level(session, "229999") == "internal"
    assert any(i.kind == "accountability_flag" for i in repo.open_review_items(session, "229999"))
    assert audit_evidence(session) == []
    session.add(
        ReportedEvidenceRow(
            ccn="229999",
            field_path=DOCUMENTS_PATH,
            value="Rosa's tax return",
            case_hash="case-1",
            created_on=TODAY,
        )
    )
    session.flush()
    problems = audit_evidence(session)
    assert len(problems) == 2
    assert "not an allowed" in problems[0] and "hash" in problems[1]


def test_audit_flags_scout_requests_that_outlive_their_case_or_keep_a_full_url(session):
    repo.add_review_item(
        session,
        None,
        "scout_request",
        {"hospital_name": "Fine Hospital", "fap_url": "fine.org", "state": "MA", "cases": ["a"]},
    )
    assert audit_evidence(session) == []
    repo.add_review_item(
        session,
        None,
        "scout_request",
        {"hospital_name": "Legacy", "fap_url": "https://pay.x.org/acct/ACCT-1", "state": "MA"},
    )
    problems = audit_evidence(session)
    assert len(problems) == 2
    assert "case hash" in problems[0] and "path or query" in problems[1]
