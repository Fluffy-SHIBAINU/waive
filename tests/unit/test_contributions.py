import re
from datetime import date

import pytest

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import SAMPLE_POLICY_TEXT, st_example_sheet
from waive.atlas.schema import SourceKind
from waive.atlas.structure import DraftField, SheetDraft
from waive.db import ContributionRow, init_db, make_engine, session_scope
from waive.learning.classify import PhotoClass
from waive.learning.contributions import (
    NoTavily,
    approve_contribution,
    list_contributions,
    personal_info_hits,
    rebuild_from_sources,
    reject_contribution,
    submit_contribution,
)
from waive.learning.hashing import case_hash

from tests.unit.test_web_senior import HOSPITAL

TODAY = date(2026, 10, 2)
NEW_POLICY_TEXT = (
    "St. Example Medical Center Financial Assistance Policy. Effective September 1, 2026.\n"
    "Patients with household income at or below 150% of the Federal Poverty Guidelines "
    "are eligible for free care.\n"
    "Patients with household income above 150% and at or below 400% of the Federal Poverty "
    "Guidelines receive a 60% discount.\n"
    "Applicants must provide a photo ID and one proof of income.\n"
    "Questions: call 617-555-0100.\n"
)


class PhotoAI:
    """Structurer fake: cites whichever source came from a patient photo (id "photo-...")."""

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        ids = re.findall(r"=== SOURCE id=(\S+)", messages[1]["content"])
        source_id = next(i for i in ids if i.startswith("photo-"))
        return SheetDraft(
            free_care_max_fpl=DraftField(
                value="150",
                quote="household income at or below 150% of the Federal Poverty Guidelines are eligible for free care",
                source_id=source_id,
            ),
            discount_tiers=DraftField(
                value=[
                    {"min_fpl_exclusive": "150", "max_fpl_inclusive": "400", "discount_percent": 60}
                ],
                quote="above 150% and at or below 400% of the Federal Poverty Guidelines receive a 60% discount",
                source_id=source_id,
            ),
            documents_required=DraftField(
                value=["photo ID", "proof of income"],
                quote="Applicants must provide a photo ID and one proof of income",
                source_id=source_id,
            ),
            phone=DraftField(
                value="617-555-0100", quote="Questions: call 617-555-0100", source_id=source_id
            ),
        )


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


def test_case_hash_is_one_way_and_short():
    assert re.fullmatch(r"[0-9a-f]{16}", case_hash("abc123"))
    assert case_hash("abc123") == case_hash("abc123") != case_hash("abc124")
    assert "abc123" not in case_hash("abc123")


def test_personal_patterns_catch_identifiers_but_pass_a_public_policy():
    assert personal_info_hits(SAMPLE_POLICY_TEXT) == []
    assert personal_info_hits(NEW_POLICY_TEXT) == []
    assert "ssn" in personal_info_hits("SSN 123-45-6789 on file")
    assert "account_number" in personal_info_hits("Account number: 48213377")
    assert "long_number" in personal_info_hits("Guarantor 1234567890")
    assert "date_of_birth" in personal_info_hits("DOB: 01/02/1950")
    assert "patient_name" in personal_info_hits("Patient: Rosa Alvarez")
    assert "patient_name" in personal_info_hits("Dear Mr. Alvarez,")


def test_submit_holds_clean_text_and_rejects_personal_text_without_storing_it(session):
    held = submit_contribution(
        session,
        ccn="229999",
        case_id="case-a",
        photo_class=PhotoClass.FAP,
        text=NEW_POLICY_TEXT,
        vision_flag=False,
        today=TODAY,
    )
    assert (held.status, held.reject_reasons, held.case_hash) == ("open", [], case_hash("case-a"))
    assert held.text == NEW_POLICY_TEXT.strip() and "case-a" not in held.case_hash

    again = submit_contribution(
        session,
        ccn="229999",
        case_id="case-b",
        photo_class=PhotoClass.FAP,
        text=NEW_POLICY_TEXT,
        vision_flag=False,
        today=TODAY,
    )
    assert again.id == held.id  # same document twice is one contribution

    rejected = submit_contribution(
        session,
        ccn="229999",
        case_id="case-c",
        photo_class=PhotoClass.APPLICATION_FORM,
        text=NEW_POLICY_TEXT + "Patient: Rosa Alvarez",
        vision_flag=True,
        today=TODAY,
    )
    assert rejected.status == "rejected" and rejected.text == ""
    assert rejected.reject_reasons == ["vision", "patient_name"]
    short = submit_contribution(
        session,
        ccn="229999",
        case_id="case-d",
        photo_class=PhotoClass.FAP,
        text="Too short.",
        vision_flag=False,
        today=TODAY,
    )
    assert short.status == "rejected" and short.reject_reasons == ["too_short"]
    with pytest.raises(ValueError):
        submit_contribution(
            session,
            ccn="229999",
            case_id="case-e",
            photo_class=PhotoClass.BILL,
            text=NEW_POLICY_TEXT,
            vision_flag=False,
            today=TODAY,
        )
    assert [row.id for row in list_contributions(session)] == [held.id]
    assert "Rosa" not in "".join(row.text for row in session.query(ContributionRow))


def test_approved_photo_becomes_a_source_and_rebuild_publishes_a_new_version(session):
    row = submit_contribution(
        session,
        ccn="229999",
        case_id="case-a",
        photo_class=PhotoClass.FAP,
        text=NEW_POLICY_TEXT,
        vision_flag=False,
        today=TODAY,
    )
    source = approve_contribution(session, row.id, TODAY)
    assert source.kind is SourceKind.PATIENT_PHOTO and source.url is None
    assert source.id.startswith("photo-") and source.title == "Patient photo: fap"
    assert row.status == "approved"
    assert any(s.id == source.id for s, _ in repo.sources_for(session, "229999"))

    result = rebuild_from_sources(session, PhotoAI(), "229999", TODAY)
    assert (result.outcome, result.version) == ("published", 2)
    latest, _ = repo.latest_sheet(session, "229999")
    assert latest.eligibility.free_care_max_fpl.value == 150
    assert latest.eligibility.free_care_max_fpl.source_id == source.id
    assert {s.kind for s in latest.sources} == {SourceKind.HOSPITAL_WEB, SourceKind.PATIENT_PHOTO}
    with pytest.raises(KeyError):
        approve_contribution(session, row.id, TODAY)  # not open any more


def test_reject_clears_text_and_rebuild_never_calls_tavily(session):
    row = submit_contribution(
        session,
        ccn="229999",
        case_id="case-a",
        photo_class=PhotoClass.PLAIN_LANGUAGE_SUMMARY,
        text=NEW_POLICY_TEXT,
        vision_flag=False,
        today=TODAY,
    )
    reject_contribution(session, row.id, "blurry")
    assert (row.status, row.text, row.reject_reasons) == ("rejected", "", ["blurry"])
    with pytest.raises(RuntimeError):
        NoTavily().search("anything")
    repo.upsert_hospital(session, {**HOSPITAL, "ccn": "220031", "name": "REAL GENERAL HOSPITAL"})
    skipped = rebuild_from_sources(session, PhotoAI(), "220031", TODAY)
    assert skipped.outcome == "skipped" and "no stored documents" in skipped.notes[0]
