import hashlib
import json
import logging
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import SecretStr

from waive.ai.client import AIClient, AIOutputError, AIRequestRejected
from waive.atlas import repo
from waive.atlas.discover import MIN_CONFIDENCE
from waive.atlas.eve_import import record_owner_rulings
from waive.atlas.pipeline import build_hospital, build_state, clear_wrong_domain, coverage_report
from waive.atlas.samples import SAMPLE_POLICY_TEXT, SAMPLE_SOURCE_ID
from waive.atlas.schema import ProcedureSheet, SheetStatus, SourceDoc, SourceKind
from waive.atlas.scout import source_id_for
from waive.atlas.skiplist import SkipEntry
from waive.atlas.structure import DraftField, SheetDraft
from waive.atlas.tavily_gateway import ExtractedPage, SearchHit
from waive.atlas.verify import (
    CEILING_FREE_BAND_REASON,
    CEILING_NOT_ASSISTANCE_REASON,
    OTHER_STATE_FACILITY_REASON,
    OTHER_STATE_REASON,
    PATIENT_SHARE_REASON,
    verify_sheet,
)
from waive.config import Settings
from waive.db import HospitalRow, init_db, make_engine, session_scope
from waive.governor import Governor, Ledger

TODAY = date(2026, 10, 2)
HOSPITAL = {
    "ccn": "229999",
    "name": "ST. EXAMPLE MEDICAL CENTER",
    "city": "BOSTON",
    "state": "MA",
    "zip": "02118",
    "phone": "617-555-0100",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
}


class FakeGateway:
    def search(self, query, **kwargs):
        if kwargs.get("include_domains"):
            return [
                SearchHit(
                    "https://www.example.org/financial-assistance-policy.pdf",
                    "Financial Assistance Policy",
                    "",
                    0.9,
                )
            ]
        return [
            SearchHit(
                "https://www.example.org/",
                "St. Example Medical Center",
                "Call 617-555-0100",
                0.8,
            )
        ]

    def extract(self, urls, **kwargs):
        return [ExtractedPage(url, POLICY_TEXT) for url in urls]


OTHER_SENTENCE = (
    "Patients with household income at or below 300% of the Federal Poverty Guidelines may "
    "receive help from the Health Safety Net."
)
POLICY_TEXT = SAMPLE_POLICY_TEXT + "\n" + OTHER_SENTENCE + "\n"
FREE_CARE_QUOTE = (
    "household income at or below 250% of the Federal Poverty Guidelines are eligible for free care"
)
DISCOUNT_QUOTE = (
    "household income above 250% and at or below 400% of the Federal Poverty Guidelines "
    "receive a 60% discount"
)


class FakeAI:
    """The same draft for every model role, except `free_care_max_fpl`, which is set per role:
    a value (cited with the free-care sentence), a (value, quote) pair, None for "not stated",
    or an exception the call raises."""

    def __init__(self, free_limit_for_fast="250", free_limit_for_tiebreak="250"):
        self.free_limits = {
            "reason": "250",
            "fast": free_limit_for_fast,
            "tiebreak": free_limit_for_tiebreak,
        }

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        source_id = messages[1]["content"].split("=== SOURCE id=")[1].split(" ")[0]
        limit = self.free_limits[role]
        if isinstance(limit, Exception):
            raise limit
        free_care = None
        if limit is not None:
            if isinstance(limit, tuple):
                value, quote = limit
            else:
                # "300" is grounded in the document's extra sentence; anything else quotes the
                # free-care sentence (which only verifies for 250).
                value, quote = limit, (OTHER_SENTENCE if limit == "300" else FREE_CARE_QUOTE)
            free_care = DraftField(value=value, quote=quote, source_id=source_id)
        return SheetDraft(
            free_care_max_fpl=free_care,
            phone=DraftField(
                value="617-555-0100", quote="Questions: call 617-555-0100", source_id=source_id
            ),
            hours=DraftField(
                value="9-5", quote="this quote is not in the document", source_id=source_id
            ),
        )


# In the sample policy's town: a sheet is held when its documents never name the hospital's place.
REAL_HOSPITAL = {**HOSPITAL, "ccn": "220031", "name": "REAL GENERAL HOSPITAL", "city": "BOSTON"}


def make_engine_with_hospital(*hospitals):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        for hospital in hospitals or (HOSPITAL,):
            repo.upsert_hospital(session, hospital)
    return engine


def test_build_hospital_publishes_verified_sheet_and_logs_rejections():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY)
        assert (result.outcome, result.version) == ("published", 1)
        sheet, _row = repo.latest_sheet(session, "229999")
        assert sheet.status is SheetStatus.PUBLISHED
        assert sheet.contacts.hours is None
        assert sheet.eligibility.free_care_max_fpl.value == 250
        assert sheet.sources[0].id != SAMPLE_SOURCE_ID
        kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
        assert kinds == ["verification"]
        assert repo.get_hospital(session, "229999").website_domain == "example.org"


def test_ungrounded_cross_check_disagreement_cannot_veto_a_verified_primary():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        # The fast model says 999 but quotes the free-care sentence, which does not contain 999.
        ai = FakeAI(free_limit_for_fast=("999", FREE_CARE_QUOTE))
        result = build_hospital(session, FakeGateway(), ai, "229999", TODAY)
        assert result.outcome == "published"
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.free_care_max_fpl.value == 250
        assert any(note.startswith("cross-check values ignored") for note in result.notes)
        kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
        assert "conflict" not in kinds


def review_detail(session, ccn, kind):
    return next(item.detail for item in repo.open_review_items(session, ccn) if item.kind == kind)


def test_build_hospital_holds_sheet_on_critical_conflict():
    """The tie-break model agrees with neither model: the conflict stands and the sheet is held."""
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        ai = FakeAI(free_limit_for_fast="300", free_limit_for_tiebreak="999")
        result = build_hospital(session, FakeGateway(), ai, "229999", TODAY)
        assert result.outcome == "held"
        assert "critical fields disagree: eligibility.free_care_max_fpl" in result.notes
        kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
        assert kinds == ["conflict", "tiebreak", "verification"]
        assert review_detail(session, "229999", "tiebreak")["eligibility.free_care_max_fpl"] == {
            "primary": "250",
            "secondary": "300",
            "tiebreak": "999",
            "verdict": "unsettled",
        }
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.status is SheetStatus.HELD
        assert sheet.eligibility.free_care_max_fpl.value == 250  # the held draft keeps the primary


def test_tiebreak_siding_with_the_primary_publishes_without_a_conflict():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        ai = FakeAI(free_limit_for_fast="300", free_limit_for_tiebreak="250")
        result = build_hospital(session, FakeGateway(), ai, "229999", TODAY)
        assert result.outcome == "published"
        assert "tie-break settled eligibility.free_care_max_fpl (primary)" in result.notes
        assert not any(note.startswith("critical fields disagree") for note in result.notes)
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.free_care_max_fpl.value == 250
        assert sheet.eligibility.free_care_max_fpl.quote == FREE_CARE_QUOTE
        kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
        assert kinds == ["conflict", "tiebreak", "verification"]
        assert review_detail(session, "229999", "tiebreak")["eligibility.free_care_max_fpl"] == {
            "primary": "250",
            "secondary": "300",
            "tiebreak": "250",
            "verdict": "primary",
        }


def test_tiebreak_siding_with_the_secondary_publishes_its_value_and_quote():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        ai = FakeAI(free_limit_for_fast=("400", DISCOUNT_QUOTE), free_limit_for_tiebreak="400")
        result = build_hospital(session, FakeGateway(), ai, "229999", TODAY)
        assert result.outcome == "published"
        assert "tie-break settled eligibility.free_care_max_fpl (secondary)" in result.notes
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.free_care_max_fpl.value == 400
        assert sheet.eligibility.free_care_max_fpl.quote == DISCOUNT_QUOTE
        assert sheet.contacts.phone.value == "617-555-0100"  # the rest is the primary's
        detail = review_detail(session, "229999", "tiebreak")["eligibility.free_care_max_fpl"]
        assert detail == {
            "primary": "250",
            "secondary": "400",
            "tiebreak": "400",
            "verdict": "secondary",
        }


def test_unverifiable_cross_check_value_never_reaches_the_tiebreak():
    """The fast model's quote does not contain its value: the disagreement is dropped before any
    tie-break runs, the primary is published, and no conflict is recorded."""
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        ai = FakeAI(free_limit_for_fast=("300", FREE_CARE_QUOTE), free_limit_for_tiebreak="300")
        result = build_hospital(session, FakeGateway(), ai, "229999", TODAY)
        assert result.outcome == "published"
        assert not any(note.startswith("critical fields disagree") for note in result.notes)
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.free_care_max_fpl.value == 250
        kinds = {item.kind for item in repo.open_review_items(session, "229999")}
        assert "conflict" not in kinds and "tiebreak" not in kinds


def test_failed_tiebreak_call_keeps_the_hold_and_is_recorded():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        ai = FakeAI(
            free_limit_for_fast="300",
            free_limit_for_tiebreak=AIOutputError("output was cut off at 6000 tokens"),
        )
        result = build_hospital(session, FakeGateway(), ai, "229999", TODAY)
        assert result.outcome == "held"
        assert "tie-break model gave no usable output" in result.notes
        assert "critical fields disagree: eligibility.free_care_max_fpl" in result.notes
        kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
        assert kinds == ["conflict", "tiebreak_failed", "verification"]
        assert "cut off" in review_detail(session, "229999", "tiebreak_failed")["error"]


def test_presumptive_only_conflict_publishes_without_that_field():
    class PresumptiveAI(FakeAI):
        def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
            draft = super().complete_json(role, messages, schema, phi=phi, purpose=purpose)
            source_id = draft.free_care_max_fpl.source_id
            programs = ["MassHealth"] if role == "reason" else ["MassHealth", "SNAP"]
            draft.presumptive = DraftField(
                value=programs,
                quote="Patients enrolled in MassHealth or SNAP are presumptively eligible for free care",
                source_id=source_id,
            )
            return draft

    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(session, FakeGateway(), PresumptiveAI(), "229999", TODAY)
        assert result.outcome == "published"
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.programs.presumptive is None
        assert sheet.eligibility.free_care_max_fpl is not None
        assert any(item.kind == "conflict" for item in repo.open_review_items(session, "229999"))
        # The tolerated conflict is noted as such, not as a critical disagreement (2.8f).
        assert "presumptive programs disagree; published without them" in result.notes
        assert not any(note.startswith("critical fields disagree") for note in result.notes)


TABLE_SENTENCE = (
    "Household income from 150% to 250% of the Federal Poverty Guidelines receives an 85% "
    "discount and from 250% to 300% a 70% discount."
)


class TableGateway(FakeGateway):
    def extract(self, urls, **kwargs):
        return [ExtractedPage(url, POLICY_TEXT + TABLE_SENTENCE + "\n") for url in urls]


class CeilingAI(FakeAI):
    """Both models read the policy's 300% eligibility ceiling as the free-care limit, while the
    table's paid bands end at 300% too: every band would be dead and 225% would read as free."""

    def __init__(self):
        super().__init__(free_limit_for_fast="300")
        self.free_limits["reason"] = "300"

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        draft = super().complete_json(role, messages, schema, phi=phi, purpose=purpose)
        draft.discount_tiers = DraftField(
            value=[
                {"min_fpl_exclusive": 150, "max_fpl_inclusive": 250, "discount_percent": 85},
                {"min_fpl_exclusive": 250, "max_fpl_inclusive": 300, "discount_percent": 70},
            ],
            quote=TABLE_SENTENCE,
            source_id=draft.free_care_max_fpl.source_id,
        )
        return draft


def test_a_free_limit_above_the_discount_table_is_held_with_a_review_item():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(session, TableGateway(), CeilingAI(), "229999", TODAY)
        assert result.outcome == "held"
        assert any(note.startswith("eligibility.free_care_max_fpl: 300%") for note in result.notes)
        kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
        assert kinds == ["inconsistent", "verification"]
        assert "150%" in review_detail(session, "229999", "inconsistent")["problems"][0]
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.status is SheetStatus.HELD
        assert sheet.eligibility.free_care_max_fpl.value == 300  # the held draft keeps both
        assert len(sheet.eligibility.discount_tiers.value) == 2


UMASS_CEILING = (
    "Financial assistance is available to patients and their family members with household "
    "income, less than 600% of the federal poverty guidelines."
)


# The summary names no free-care band and no table, only the ceiling on all assistance.
UMASS_SUMMARY = (
    "St. Example Medical Center, Boston, MA 02118. Financial Assistance Summary.\n"
    f"{UMASS_CEILING}\n"
    "Patients who qualify are not charged more than the amounts generally billed to patients "
    "with insurance.\n"
    "Questions: call 617-555-0100.\n"
)


class UMassGateway(FakeGateway):
    def extract(self, urls, **kwargs):
        return [ExtractedPage(url, UMASS_SUMMARY) for url in urls]


class UMassAI(FakeAI):
    """Both models read the summary's 600% ceiling on all assistance as free care, and both also
    report it as the ceiling; the summary names no free-care band and no table."""

    def __init__(self):
        super().__init__(free_limit_for_fast=("600", UMASS_CEILING))
        self.free_limits["reason"] = ("600", UMASS_CEILING)

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        draft = super().complete_json(role, messages, schema, phi=phi, purpose=purpose)
        draft.assistance_ceiling_fpl = DraftField(
            value="600%", quote=UMASS_CEILING, source_id=draft.free_care_max_fpl.source_id
        )
        return draft


def test_a_ceiling_on_all_assistance_is_not_published_as_free_care():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(session, UMassGateway(), UMassAI(), "229999", TODAY)
        assert (
            "eligibility.free_care_max_fpl: stated 600 is at or above the overall assistance "
            "ceiling (600); not free care" in result.notes
        )
        # Decision 7.9a-2 (task 7.14): it publishes as what it is, the ceiling on any help.
        assert (result.outcome, result.version) == ("published", 1)
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.status is SheetStatus.PUBLISHED
        assert sheet.eligibility.free_care_max_fpl is None
        assert sheet.eligibility.discount_tiers is None
        ceiling = sheet.eligibility.assistance_ceiling_fpl
        assert (ceiling.value, ceiling.quote) == (600, UMASS_CEILING)
        assert sheet.contacts.phone.value == "617-555-0100"  # the rest of the sheet survives
        kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
        assert kinds == ["verification"]  # no misread reaches the inconsistency check


MERCY_FREE = "Patients with household income less than 100% FPL have no patient responsibility."
MERCY_TABLE = (
    "Qualifying Criterion Less than 100% FPL 101 - 200% FPL 201 - 250% FPL "
    "Patient Responsibility None Co-Pay Co-Pay + 15% of total charges"
)


class MercyGateway(FakeGateway):
    def extract(self, urls, **kwargs):
        return [
            ExtractedPage(url, POLICY_TEXT + MERCY_FREE + "\n" + MERCY_TABLE + "\n") for url in urls
        ]


class PatientShareAI(FakeAI):
    """Both models turn a table of what the patient pays into a 15% "discount" band."""

    def __init__(self):
        super().__init__(free_limit_for_fast=("100", MERCY_FREE))
        self.free_limits["reason"] = ("100", MERCY_FREE)

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        draft = super().complete_json(role, messages, schema, phi=phi, purpose=purpose)
        draft.discount_tiers = DraftField(
            value=[{"min_fpl_exclusive": "201 - 250% FPL", "discount_percent": 15}],
            quote=MERCY_TABLE,
            source_id=draft.free_care_max_fpl.source_id,
        )
        return draft


def test_a_patient_share_table_holds_the_sheet_instead_of_publishing_a_15_percent_discount():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(session, MercyGateway(), PatientShareAI(), "229999", TODAY)
        assert result.outcome == "held"
        assert f"eligibility.discount_tiers: {PATIENT_SHARE_REASON}" in result.notes
        kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
        assert kinds == ["patient_share_table", "verification"]
        assert (
            "15% of total charges"
            in review_detail(session, "229999", "patient_share_table")["quote"]
        )
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.status is SheetStatus.HELD
        assert sheet.eligibility.discount_tiers is None  # never published as a discount
        assert sheet.eligibility.free_care_max_fpl.value == 100  # the co-pay band stays unexpressed


# Russell Regional (171350) published an assistance ceiling of 70 read from its free-care line.
RUSSELL_FREE_LINE = (
    "Patient/guarantors that fall below Level 1, 70% of FPL, will receive a 100% discount."
)


RUSSELL_TABLE = (
    "Patients with household income above 70% and at or below 200% of the Federal Poverty "
    "Guidelines receive a 60% discount."
)


class RussellGateway(FakeGateway):
    def extract(self, urls, **kwargs):
        text = POLICY_TEXT + RUSSELL_FREE_LINE + "\n" + RUSSELL_TABLE + "\n"
        return [ExtractedPage(url, text) for url in urls]


class FreeLineCeilingAI(FakeAI):
    """Both models read no band and no table, only an assistance ceiling of 70 quoted from the
    free-care line."""

    def __init__(self):
        super().__init__(free_limit_for_fast=None)
        self.free_limits["reason"] = None

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        draft = super().complete_json(role, messages, schema, phi=phi, purpose=purpose)
        draft.assistance_ceiling_fpl = DraftField(
            value="70", quote=RUSSELL_FREE_LINE, source_id=draft.phone.source_id
        )
        return draft


def test_a_ceiling_quoted_from_the_free_care_band_holds_the_sheet():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(session, RussellGateway(), FreeLineCeilingAI(), "229999", TODAY)
        assert result.outcome == "held"
        assert f"eligibility.assistance_ceiling_fpl: {CEILING_FREE_BAND_REASON}" in result.notes
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.status is SheetStatus.HELD
        assert sheet.eligibility.assistance_ceiling_fpl is None  # never published as a ceiling
        # Dropping the ceiling left no income rule here, which holds a sheet anyway; the
        # rejection's own hold is what the primary pass reports.
        from waive.atlas.pipeline import BuildResult, _structure_and_verify

        row = repo.get_hospital(session, "229999")
        _sheet, holds = _structure_and_verify(
            session,
            FreeLineCeilingAI(),
            repo.hospital_ref(row),
            repo.sources_for(session, "229999"),
            TODAY,
            BuildResult("229999", row.name, "skipped"),
        )
        assert holds == [CEILING_FREE_BAND_REASON]


# UnityPoint's Illinois-only Schedule B (fap-ec276fb294e1a7f3), quoted for Trinity Bettendorf
# (160104) in Iowa; here the hospital is St. Example, in Massachusetts.
UNITYPOINT_IL_SCHEDULE = (
    "B.  An IL UPH Hospital shall provide a charitable discount of 100% of its charges for all "
    "medically necessary health care services to any Uninsured Patient who applies for a "
    "discount and has family income of not more than 200% of the federal poverty income "
    "guidelines."
)
UPH_TABLE = (
    "Patients with household income above 200% and at or below 400% of the Federal Poverty "
    "Guidelines receive a 60% discount."
)


class UnityPointGateway(FakeGateway):
    def extract(self, urls, **kwargs):
        text = POLICY_TEXT + UNITYPOINT_IL_SCHEDULE + "\n" + UPH_TABLE + "\n"
        return [ExtractedPage(url, text) for url in urls]


class OtherStateScheduleAI(FakeAI):
    """Both models read free care to 200% from the Illinois hospitals' schedule, and the table
    above it from the hospital's own."""

    def __init__(self):
        super().__init__(free_limit_for_fast=("200", UNITYPOINT_IL_SCHEDULE))
        self.free_limits["reason"] = ("200", UNITYPOINT_IL_SCHEDULE)

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        draft = super().complete_json(role, messages, schema, phi=phi, purpose=purpose)
        draft.discount_tiers = DraftField(
            value=[{"min_fpl_exclusive": 200, "max_fpl_inclusive": 400, "discount_percent": 60}],
            quote=UPH_TABLE,
            source_id=draft.free_care_max_fpl.source_id,
        )
        return draft


def test_a_band_from_another_states_facilities_holds_the_sheet_with_its_table():
    # Dropping the free-care limit alone would publish the table from 200% with nothing below it.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(
            session, UnityPointGateway(), OtherStateScheduleAI(), "229999", TODAY
        )
        assert result.outcome == "held"
        assert f"eligibility.free_care_max_fpl: {OTHER_STATE_FACILITY_REASON}" in result.notes
        kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
        assert kinds == ["verification"]
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.status is SheetStatus.HELD
        assert sheet.eligibility.free_care_max_fpl is None
        assert [tier.discount_percent for tier in sheet.eligibility.discount_tiers.value] == [60]


def test_build_hospital_survives_a_failed_cross_check():
    class FlakyAI(FakeAI):
        def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
            if role == "fast":
                raise AIOutputError("model output did not match SheetDraft")
            return super().complete_json(role, messages, schema, phi=phi, purpose=purpose)

    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(session, FakeGateway(), FlakyAI(), "229999", TODAY)
        assert result.outcome == "published"
        assert "cross-check model gave no usable output" in result.notes
        kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
        assert kinds == ["crosscheck_failed", "verification"]


TOO_LONG = (
    "nvidia/nemotron-3-super-120b-a12b: HTTP 400 (This model's maximum context length is "
    "131072 tokens. However, you requested 150000 tokens.)"
)
# The longest server message the client keeps (MAX_SERVER_MESSAGE_CHARS), behind the longest
# default model name: the review detail and the note must still carry every character of it.
LONG_REASON = ("The request is not valid for this model because " + "x" * 300)[:300]
LONG_REJECTION = f"nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B: HTTP 400 ({LONG_REASON})"


@pytest.mark.parametrize(
    ("error", "calls"),
    [
        # One short document: a smaller prompt would be identical, so there is no retry (the
        # respx test below shows the retry on a long one).
        (AIRequestRejected(TOO_LONG), 1),
        (AIRequestRejected(LONG_REJECTION), 1),
        (AIOutputError("model output did not match SheetDraft (free_care_max_fpl: dict_type)"), 1),
    ],
    ids=["rejected-request", "rejected-long-message", "unusable-output"],
)
def test_a_primary_structurer_failure_is_filed_for_review_not_raised(error, calls):
    """7.8: the hospital ends "failed" with the reason, its scouted documents stay stored, and
    nothing is published; the batch does not see an exception (which would roll them back)."""

    class FailingPrimary(FakeAI):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
            self.calls += 1
            assert role == "reason"  # no cross-check without a primary sheet
            raise error

    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        ai = FailingPrimary()
        result = build_hospital(session, FakeGateway(), ai, "229999", TODAY)
        assert (result.outcome, result.version) == ("failed", None)
        assert ai.calls == calls
        assert result.notes == [f"structurer gave no usable sheet: {error}"]
        assert repo.latest_sheet(session, "229999") is None
        kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
        assert kinds == ["structure_failed"]
        assert review_detail(session, "229999", "structure_failed") == {"error": str(error)}
        assert len(repo.sources_for(session, "229999")) == 1
        assert repo.get_hospital(session, "229999").website_domain == "example.org"
        # The batch sees an ordinary outcome, and a hospital without a sheet stays in line.
        batch = build_state(session, FakeGateway(), ai, "MA", TODAY)
        assert [r.outcome for r in batch] == ["failed"] and batch[0].notes == result.notes


BASE = "https://api.tokenfactory.nebius.com/v1"
# One long policy: the income rules at the head, then pages that mention discounts, so the
# structurer's passage budget (not the document length) decides the prompt size.
LONG_POLICY = (
    POLICY_TEXT
    + ("The hospital provides care to the community in many ways. A discount may apply. " * 2_000)
)[:50_000]
LONG_SOURCE_ID = source_id_for("fap", hashlib.sha256(LONG_POLICY.encode("utf-8")).hexdigest())
TOO_LONG_BODY = {
    "object": "error",
    "message": (
        "This model's maximum context length is 131072 tokens. However, you requested 150000 "
        "tokens."
    ),
    "type": "BadRequestError",
    "code": 400,
}


class LongGateway(FakeGateway):
    def extract(self, urls, **kwargs):
        return [ExtractedPage(url, LONG_POLICY) for url in urls]


def make_ai(tmp_path):
    settings = Settings(
        _env_file=None,
        nebius_api_key=SecretStr("test-key"),
        ledger_path=tmp_path / "usage.jsonl",
    )
    governor = Governor(
        Ledger(settings.ledger_path), settings.tavily_credit_cap, settings.token_factory_usd_cap
    )
    return AIClient(settings, governor)


def chat_payload(content):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


def prompt_sizes(route):
    return [len(json.loads(call.request.content)["messages"][1]["content"]) for call in route.calls]


@respx.mock
def test_a_token_factory_400_is_retried_smaller_then_filed_for_review(tmp_path):
    """The 140117 shape end to end: Token Factory answers 400 to the full prompt and to the
    smaller one; the hospital is reported failed with the server's reason and a review item."""
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(400, json=TOO_LONG_BODY)
    )
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(session, LongGateway(), make_ai(tmp_path), "229999", TODAY)
        assert result.outcome == "failed"
        assert route.call_count == 2
        first, second = prompt_sizes(route)
        assert second < first
        assert result.notes == [
            "structurer gave no usable sheet: nvidia/nemotron-3-super-120b-a12b: HTTP 400 "
            "(This model's maximum context length is 131072 tokens. However, you requested "
            "150000 tokens.)"
        ]
        kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
        assert kinds == ["structure_failed"]
        assert "131072" in review_detail(session, "229999", "structure_failed")["error"]
        assert repo.latest_sheet(session, "229999") is None
        assert [s.id for s, _ in repo.sources_for(session, "229999")] == [LONG_SOURCE_ID]


@respx.mock
def test_a_token_factory_400_recovers_on_the_smaller_prompt(tmp_path):
    draft = {
        "free_care_max_fpl": {
            "value": "250",
            "quote": FREE_CARE_QUOTE,
            "source_id": LONG_SOURCE_ID,
        },
        "phone": {
            "value": "617-555-0100",
            "quote": "Questions: call 617-555-0100",
            "source_id": LONG_SOURCE_ID,
        },
    }
    route = respx.post(f"{BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(400, json=TOO_LONG_BODY),
            httpx.Response(200, json=chat_payload(json.dumps(draft))),
        ]
    )
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(
            session, LongGateway(), make_ai(tmp_path), "229999", TODAY, dual=False
        )
        assert (result.outcome, result.version) == ("published", 1)
        assert route.call_count == 2
        first, second = prompt_sizes(route)
        assert second < first
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.free_care_max_fpl.value == 250
        assert sheet.contacts.phone.value == "617-555-0100"
        kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
        assert kinds == ["verification"]
        note = review_detail(session, "229999", "verification")["skipped"][0]
        assert note.startswith("structurer: retried with a smaller passage budget")
        assert "131072" in note and note in result.notes


def test_reuse_sources_leaves_state_overlay_documents_out_of_the_structurer():
    class RecordingAI(FakeAI):
        prompts: list[str] = []

        def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
            self.prompts.append(messages[1]["content"])
            return super().complete_json(role, messages, schema, phi=phi, purpose=purpose)

    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY)
        overlay = SourceDoc(
            id="state-mass-abc123",
            kind=SourceKind.STATE_REPOSITORY,
            url="https://www.mass.gov/doc/senior-guide/download",
            title="download",
            fetched_on=TODAY,
            sha256="0" * 64,
        )
        repo.save_source(session, overlay, "Senior guide to MassHealth. " * 5000, "229999")
        result = build_hospital(
            session, FakeGateway(), RecordingAI(), "229999", TODAY, reuse_sources=True
        )
        assert result.outcome == "published"
        assert RecordingAI.prompts and all("state-mass" not in p for p in RecordingAI.prompts)
        sheet, _ = repo.latest_sheet(session, "229999")
        assert [s.kind for s in sheet.sources] == [SourceKind.HOSPITAL_WEB]


SHELL_TEXT = (
    "![Logo](/logo.png)\n\n## All Locations\n\n[View all Locations](/locations)\n\n"
    "Find a location by type, service, treatment or distance to you.\n\n"
    "## Services and Treatments\n\nYour trusted resource for all types of health conditions.\n\n"
    "### Emergency Care\n\nWe provide emergency care for adults and children.\n\n"
    "[Pay a bill](/patients-visitors/pay-a-bill)\n\n## Footer Menu\n\n## Legal Menu\n"
)
SHELL_URLS = {
    "fap": "https://www.example.org/patient-financial-services/financial-assistance-policy",
    "application": "https://www.example.org/patient-financial-services/financial-assistance-apply",
    "summary": "https://www.example.org/patient-financial-services/financial-assistance-summary",
}


def store_shells(session, ccn):
    """What the scout stored for Milford Regional (220090) on 2026-10-09: the site had moved, and
    its policy, application and summary URLs all answered with the same 10,760-character menu."""
    sha = hashlib.sha256(SHELL_TEXT.encode()).hexdigest()
    for doc_class, url in SHELL_URLS.items():
        source = SourceDoc(
            id=source_id_for(doc_class, sha),
            kind=SourceKind.HOSPITAL_WEB,
            url=url,
            title=f"Financial Assistance {doc_class.title()} - Example Regional",
            fetched_on=TODAY,
            sha256=sha,
        )
        repo.save_source(session, source, SHELL_TEXT, ccn)


class RecordingAI(FakeAI):
    def __init__(self):
        super().__init__()
        self.prompts: list[str] = []

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        self.prompts.append(messages[1]["content"])
        return super().complete_json(role, messages, schema, phi=phi, purpose=purpose)


def test_reuse_sources_detaches_stored_navigation_shells_and_says_so():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY)
        store_shells(session, "229999")
        assert len(repo.sources_for(session, "229999")) == 4
        ai = RecordingAI()
        result = build_hospital(session, FakeGateway(), ai, "229999", TODAY, reuse_sources=True)
        assert result.outcome == "published"
        assert result.notes[0] == (
            "3 stored pages are the site's navigation shell, not policy documents (the same "
            f"{len(SHELL_TEXT)}-character menu page at 3 URLs); detached and left out of the "
            "structurer"
        )
        assert ai.prompts and all("patient-financial-services" not in p for p in ai.prompts)
        assert all("Find a location" not in p for p in ai.prompts)
        sheet, _ = repo.latest_sheet(session, "229999")
        assert [s.url for s in sheet.sources] == [
            "https://www.example.org/financial-assistance-policy.pdf"
        ]
        assert [s.url for s, _ in repo.sources_for(session, "229999")] == [
            "https://www.example.org/financial-assistance-policy.pdf"
        ]
        assert review_detail(session, "229999", "navigation_shell") == {
            "urls": sorted(SHELL_URLS.values()),
            "chars": len(SHELL_TEXT),
        }


def test_reuse_sources_with_only_navigation_shells_stored_spends_nothing():
    class NoSpend:
        def search(self, *args, **kwargs):
            raise AssertionError("Tavily search would spend credits")

        def extract(self, *args, **kwargs):
            raise AssertionError("Tavily extract would spend credits")

    class NoModel(FakeAI):
        def complete_json(self, *args, **kwargs):
            raise AssertionError("the structurer would be shown a menu")

    engine = make_engine_with_hospital({**HOSPITAL, "website_domain": "example.org"})
    with session_scope(engine) as session:
        store_shells(session, "229999")
        result = build_hospital(session, NoSpend(), NoModel(), "229999", TODAY, reuse_sources=True)
        assert result.outcome == "skipped"
        assert result.notes[1] == (
            "no financial assistance documents left once the navigation shell is set aside; "
            "the hospital's policy is not in the stored documents"
        )
        assert repo.latest_sheet(session, "229999") is None
        assert repo.sources_for(session, "229999") == []
        kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
        assert kinds == ["navigation_shell", "no_documents"]
        assert review_detail(session, "229999", "no_documents") == {
            "domain": "example.org",
            "shells_only": True,
        }


def test_rebuild_keeps_the_state_overlay_from_the_previous_version():
    from waive.atlas.overlays import STATE_OVERLAYS, apply_overlay
    from waive.atlas.publish import publish_sheet
    from waive.atlas.schema import SourceDoc, SourceKind

    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY)
        sheet, _ = repo.latest_sheet(session, "229999")
        source = SourceDoc(
            id="state-ma-test",
            kind=SourceKind.STATE_REPOSITORY,
            url="https://www.mass.gov/hsn",
            title="HSN",
            fetched_on=TODAY,
            sha256="0" * 64,
        )
        text = "The Health Safety Net pays hospitals for care to low-income residents."
        overlaid = apply_overlay(sheet, source, text, STATE_OVERLAYS["MA"], TODAY)
        repo.save_source(session, source, text, "229999")
        assert publish_sheet(session, overlaid).version == 2

        result = build_hospital(
            session, FakeGateway(), FakeAI(), "229999", TODAY, reuse_sources=True
        )
        latest, _ = repo.latest_sheet(session, "229999")
        assert result.outcome == "published"
        assert latest.programs.state_programs is not None
        assert latest.programs.state_programs.source_id == "state-ma-test"
        assert any(s.id == "state-ma-test" for s in latest.sources)


def test_clearing_a_wrong_domain_detaches_its_documents_and_holds_the_sheet():
    # Harrington Hospital (220019): discovery picked billfairly.com, a billing directory, and
    # the sheet was published from that site's pages about hospitals in Georgia and Texas.
    from waive.atlas.overlays import STATE_OVERLAYS, apply_overlay
    from waive.atlas.publish import publish_sheet

    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY)
        sheet, _ = repo.latest_sheet(session, "229999")
        source = SourceDoc(
            id="state-ma-test",
            kind=SourceKind.STATE_REPOSITORY,
            url="https://www.mass.gov/hsn",
            title="HSN",
            fetched_on=TODAY,
            sha256="0" * 64,
        )
        text = "The Health Safety Net pays hospitals for care to low-income residents."
        repo.save_source(session, source, text, "229999")
        publish_sheet(session, apply_overlay(sheet, source, text, STATE_OVERLAYS["MA"], TODAY))
        policy_id = sheet.eligibility.free_care_max_fpl.source_id

        result = clear_wrong_domain(session, "229999", "billing directory, not the hospital")
        assert (result.outcome, result.version) == ("held", 3)
        assert result.notes == [
            "domain example.org cleared (billing directory, not the hospital); 1 documents "
            "detached",
            "2 fields quoted from them dropped: eligibility.free_care_max_fpl, contacts.phone",
        ]
        held, row = repo.latest_sheet(session, "229999")
        assert held.status is SheetStatus.HELD
        assert held.hospital.website_domain is None
        assert held.eligibility.free_care_max_fpl is None and held.contacts.phone is None
        # The state overlay is not the hospital's claim about itself: it and its source stay.
        assert held.programs.state_programs.source_id == "state-ma-test"
        assert [s.id for s in held.sources] == ["state-ma-test"]
        assert row.diff["status"] == {"old": "published", "new": "held"}
        assert repo.get_hospital(session, "229999").website_domain is None
        assert [s.id for s, _ in repo.sources_for(session, "229999")] == ["state-ma-test"]
        assert review_detail(session, "229999", "domain") == {
            "found": None,
            "cleared": "example.org",
            "reason": "billing directory, not the hospital",
            "sources": [policy_id],
        }
        # Nothing to do twice: no domain, no second version, no second item.
        again = clear_wrong_domain(session, "229999", "again")
        assert (again.outcome, again.notes) == ("skipped", ["no domain set"])
        assert repo.latest_sheet(session, "229999")[0].version == 3


def test_build_state_skips_hospitals_with_sheets_and_reports():
    engine = make_engine_with_hospital(REAL_HOSPITAL)
    with session_scope(engine) as session:
        first = build_state(session, FakeGateway(), FakeAI(), "MA", TODAY)
        second = build_state(session, FakeGateway(), FakeAI(), "MA", TODAY)
        assert [r.outcome for r in first] == ["published"]
        assert second == []
        report = coverage_report(session, "MA")
        assert "| 220031 |" in report and "published" in report


def test_build_state_leaves_the_hospitals_on_the_skip_list_out(caplog):
    # 7.12b: Eve could not build this hospital; a state batch never tries it again and adds no
    # result for it.
    second = {**REAL_HOSPITAL, "ccn": "220010", "name": "NEW HOSPITAL"}
    listed = {**REAL_HOSPITAL, "ccn": "220045", "name": "THIRD COMMUNITY HOSPITAL"}
    engine = make_engine_with_hospital(REAL_HOSPITAL, second, listed)
    skip = {
        "220045": SkipEntry(
            "220045", "MA", "THIRD COMMUNITY HOSPITAL", "scout found no official policy"
        ),
        "400001": SkipEntry("400001", "PR", "HOSPITAL SAN JUAN", "scout found no official policy"),
    }
    with session_scope(engine) as session, caplog.at_level(logging.INFO, "waive.atlas.pipeline"):
        results = build_state(session, FakeGateway(), FakeAI(), "MA", TODAY, skip=skip)
        assert sorted((r.ccn, r.outcome) for r in results) == [
            ("220010", "published"),
            ("220031", "published"),
        ]
        assert repo.latest_sheet(session, "220045") is None
        assert "skip list: 1 hospitals in MA left out" in caplog.text
        # Without the list it is built like the others, and nothing is said about a skip list.
        caplog.clear()
        later = build_state(session, FakeGateway(), FakeAI(), "MA", TODAY)
        assert [r.ccn for r in later] == ["220045"]
        assert "skip list" not in caplog.text


def test_a_hospital_left_out_by_the_skip_list_does_not_use_up_the_limit(caplog):
    # The listed hospital sorts first by name; `limit` counts hospitals built, so the next one
    # still gets its turn.
    listed = {**REAL_HOSPITAL, "ccn": "220010", "name": "ABBOTT LISTED HOSPITAL"}
    engine = make_engine_with_hospital(listed, REAL_HOSPITAL)
    skip = {
        "220010": SkipEntry(
            "220010", "MA", "ABBOTT LISTED HOSPITAL", "scout found no official policy"
        )
    }
    with session_scope(engine) as session, caplog.at_level(logging.INFO, "waive.atlas.pipeline"):
        results = build_state(session, FakeGateway(), FakeAI(), "ma", TODAY, limit=1, skip=skip)
        assert [r.ccn for r in results] == ["220031"]
        assert "skip list: 1 hospitals in MA left out" in caplog.text


def test_coverage_report_leaves_the_fictional_demo_hospital_out():
    engine = make_engine_with_hospital(HOSPITAL, REAL_HOSPITAL)
    with session_scope(engine) as session:
        # Building still works for the demo hospital; it just stays out of the public report.
        assert (
            build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY).outcome == "published"
        )
        assert (
            build_hospital(session, FakeGateway(), FakeAI(), "220031", TODAY).outcome == "published"
        )
        report = coverage_report(session, "MA")
        assert "Hospitals in registry: 1" in report
        assert "Published sheets: 1 (100%)" in report
        assert "| 220031 |" in report
        assert "229999" not in report and "ST. EXAMPLE" not in report


class GroundedAI(FakeAI):
    """Cites the free-care sentence from whichever source really contains it (none: no income
    rule), the way a model with the right document in front of it would."""

    def __init__(self):
        super().__init__()
        self.prompts: list[str] = []

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        content = messages[1]["content"]
        self.prompts.append(content)
        blocks = content.split("=== SOURCE id=")[1:]
        grounded = next((b.split(" ")[0] for b in blocks if FREE_CARE_QUOTE in b), None)
        first = blocks[0].split(" ")[0]
        free = (
            DraftField(value="250", quote=FREE_CARE_QUOTE, source_id=grounded) if grounded else None
        )
        return SheetDraft(
            free_care_max_fpl=free,
            phone=DraftField(
                value="617-555-0100",
                quote="Questions: call 617-555-0100",
                source_id=grounded or first,
            ),
        )


SIBLING = {
    **HOSPITAL,
    "ccn": "220031",
    "name": "REAL GENERAL HOSPITAL",
    "city": "BOSTON",
    "website_domain": "example.org",
    "domain_confidence": 0.9,  # discovery was sure of the site
}
THIN_PAGE = (
    "Financial assistance at Real General Hospital. We help patients who cannot pay. "
    "Call us for more information about our programs and how to apply. "
) * 3


def store_thin_page(session, ccn):
    sha = hashlib.sha256(THIN_PAGE.encode()).hexdigest()
    source = SourceDoc(
        id=source_id_for("fap", sha),
        kind=SourceKind.HOSPITAL_WEB,
        url="https://www.example.org/real/financial-assistance",
        title="Financial Assistance | Real General",
        fetched_on=TODAY,
        sha256=sha,
    )
    repo.save_source(session, source, THIN_PAGE, ccn)
    return source


def test_a_hospital_whose_own_pages_state_no_income_rules_borrows_a_published_siblings_policy():
    # Adventist Health White Memorial (050350), AdventHealth Riverview (100364), Advocate Christ
    # and Condell (140208, 140202), 7.9: scouting by hospital name on a system site found one
    # thin page, while the system's policy was already stored for a sibling on the same domain.
    engine = make_engine_with_hospital(HOSPITAL, SIBLING)
    with session_scope(engine) as session:
        assert (
            build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY).outcome == "published"
        )
        policy_id = repo.latest_sheet(session, "229999")[0].eligibility.free_care_max_fpl.source_id
        thin = store_thin_page(session, "220031")
        ai = GroundedAI()
        result = build_hospital(session, FakeGateway(), ai, "220031", TODAY, reuse_sources=True)
        assert result.outcome == "published"
        assert (
            "1 policy document shared from ST. EXAMPLE MEDICAL CENTER (229999) on example.org; "
            "the hospital's own documents state no income rules"
        ) in result.notes
        # Structured twice: once from its own page (no income rule), once with the shared policy.
        assert (
            len(ai.prompts) >= 2 and policy_id not in ai.prompts[0] and policy_id in ai.prompts[-1]
        )
        sheet, _ = repo.latest_sheet(session, "220031")
        assert sheet.eligibility.free_care_max_fpl.value == 250
        assert sheet.eligibility.free_care_max_fpl.source_id == policy_id
        assert {s.id for s in sheet.sources} == {thin.id, policy_id}
        assert {s.id for s, _ in repo.sources_for(session, "220031")} == {thin.id, policy_id}
        # The borrowed policy's phone is the sibling's desk (Condell's sheet sent patients to
        # Lutheran General's business office, review of 7.9): dropped and recorded.
        assert sheet.contacts.phone is None
        assert review_detail(session, "220031", "shared_sources") == {
            "from_ccn": "229999",
            "domain": "example.org",
            "sources": [policy_id],
            "dropped_fields": ["contacts.phone"],
        }
        assert (
            "dropped from the shared documents (a sibling's desk, not this hospital's): "
            "contacts.phone"
        ) in result.notes
        # A later --reuse-sources rebuild loads the shared documents as if they were the
        # hospital's own; the shared_sources item says which they were, and the drop holds.
        again = GroundedAI()
        result = build_hospital(session, FakeGateway(), again, "220031", TODAY, reuse_sources=True)
        assert result.outcome == "published" and len(again.prompts) == 2
        sheet, _ = repo.latest_sheet(session, "220031")
        assert sheet.contacts.phone is None and sheet.eligibility.free_care_max_fpl.value == 250
        assert any("contacts.phone" in note for note in result.notes)
        assert [i.kind for i in repo.open_review_items(session, "220031")].count(
            "shared_sources"
        ) == 1


def test_reuse_sources_with_nothing_stored_borrows_a_siblings_documents_before_scouting():
    # AdventHealth Carrollwood and Zephyrhills (100319, 100363): no documents of their own, seven
    # siblings on adventhealth.com with the system policy stored. No credits are spent.
    class NoSpend:
        def search(self, *args, **kwargs):
            raise AssertionError("Tavily search would spend credits")

        def extract(self, *args, **kwargs):
            raise AssertionError("Tavily extract would spend credits")

        def map(self, *args, **kwargs):
            raise AssertionError("Tavily map would spend credits")

    engine = make_engine_with_hospital(HOSPITAL, SIBLING)
    with session_scope(engine) as session:
        build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY)
        ai = GroundedAI()
        result = build_hospital(session, NoSpend(), ai, "220031", TODAY, reuse_sources=True)
        assert result.outcome == "published"
        assert len(ai.prompts) == 2  # primary and cross-check, no first pass without documents
        assert review_detail(session, "220031", "shared_sources")["from_ccn"] == "229999"
        assert repo.latest_sheet(session, "220031")[0].eligibility.free_care_max_fpl.value == 250


def test_a_scout_that_finds_nothing_borrows_a_siblings_documents():
    class EmptySite(FakeGateway):
        def search(self, query, **kwargs):
            return [] if kwargs.get("include_domains") else super().search(query, **kwargs)

        def map(self, url, **kwargs):
            return []

    engine = make_engine_with_hospital(HOSPITAL, SIBLING)
    with session_scope(engine) as session:
        build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY)
        result = build_hospital(session, EmptySite(), GroundedAI(), "220031", TODAY)
        assert result.outcome == "published"
        assert review_detail(session, "220031", "shared_sources")["from_ccn"] == "229999"
    # Without a published sibling the outcome is what it was: no documents, a review item.
    engine = make_engine_with_hospital(HOSPITAL, SIBLING)
    with session_scope(engine) as session:
        result = build_hospital(session, EmptySite(), GroundedAI(), "220031", TODAY)
        assert (result.outcome, result.notes) == (
            "skipped",
            ["no financial assistance documents found"],
        )


def test_other_state_versions_of_a_system_policy_are_set_aside():
    # Adventist Health (050013 and siblings, 7.9): the Hawaii and Oregon versions of the system
    # policy were stored next to the California one, and the structurer cited the Hawaii table.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY)
        hawaii_text = POLICY_TEXT.replace("250%", "300%")
        hawaii = SourceDoc(
            id="fap-hawaii",
            kind=SourceKind.HOSPITAL_WEB,
            url="https://www.example.org/documents/financial-assistance-policy-hawaii.pdf",
            title="[PDF] Financial Assistance Policy for Hawaii-Based Hospitals",
            fetched_on=TODAY,
            sha256="1" * 64,
        )
        repo.save_source(session, hawaii, hawaii_text, "229999")
        own_state = SourceDoc(
            id="fap-massachusetts",
            kind=SourceKind.HOSPITAL_WEB,
            url="https://www.example.org/documents/financial-assistance-policy-massachusetts.pdf",
            title="Financial Assistance Policy",
            fetched_on=TODAY,
            sha256="2" * 64,
        )
        repo.save_source(session, own_state, POLICY_TEXT, "229999")
        ai = GroundedAI()
        result = build_hospital(session, FakeGateway(), ai, "229999", TODAY, reuse_sources=True)
        assert result.outcome == "published"
        assert (
            "1 stored document is another state's version of the system policy (fap-hawaii: HI); "
            "detached and left out of the structurer"
        ) in result.notes
        assert ai.prompts and all("hawaii" not in p.lower() for p in ai.prompts)
        assert all("fap-massachusetts" in p for p in ai.prompts)
        assert "fap-hawaii" not in {s.id for s, _ in repo.sources_for(session, "229999")}
        assert review_detail(session, "229999", "other_state_documents") == {
            "state": "MA",
            "sources": [{"id": "fap-hawaii", "states": ["HI"]}],
        }
    # Beside documents that name no state, another state's version is set aside too (AdventHealth
    # Wauchula, Florida, took its free-care limit from /legal/financial-assistance-illinois while
    # the system policy named no state; review of 7.9).
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY)
        repo.save_source(session, hawaii, hawaii_text, "229999")
        ai = GroundedAI()
        result = build_hospital(session, FakeGateway(), ai, "229999", TODAY, reuse_sources=True)
        assert ai.prompts and all("fap-hawaii" not in p for p in ai.prompts)
        assert any("fap-hawaii: HI" in note for note in result.notes)
    # When every stored document is another state's, one whose address or title names only other
    # states is set aside all the same (the 2026-10-24 ruling; Ascension Providence, 450042):
    # nothing is left to structure.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        repo.get_hospital(session, "229999").website_domain = "example.org"
        repo.save_source(session, hawaii, hawaii_text, "229999")
        result = build_hospital(session, NoSpend(), NoModel(), "229999", TODAY, reuse_sources=True)
        assert result.notes == [
            "1 stored document is another state's version of the system policy (fap-hawaii: HI); "
            "detached and left out of the structurer",
            NOTHING_LEFT_NOTE,
        ]
        assert "fap-hawaii" not in {s.id for s, _ in repo.sources_for(session, "229999")}
    # A state named only by a code in parentheses is weaker evidence: when such documents are all
    # there is, there is nothing to prefer, and they stay.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        repo.get_hospital(session, "229999").website_domain = "example.org"
        coded = hawaii.model_copy(
            update={
                "id": "fap-coded",
                "url": "https://www.example.org/documents/billing-and-collection-policy.pdf",
                "title": "Billing and Collection Policy (HI)",
            }
        )
        repo.save_source(session, coded, hawaii_text, "229999")
        ai = GroundedAI()
        build_hospital(session, FakeGateway(), ai, "229999", TODAY, reuse_sources=True)
        assert ai.prompts and all("fap-coded" in p for p in ai.prompts)
        assert "fap-coded" in {s.id for s, _ in repo.sources_for(session, "229999")}


def test_a_state_inside_the_hospitals_own_name_does_not_make_its_policy_another_states():
    # Sierra Nevada Memorial (050150, California), Washington Hospital (390042, Pennsylvania):
    # 26 registry hospitals carry another state's name. Their own policy, named after them next
    # to a California fair-pricing notice, must not be detached as "Nevada's version".
    sierra = {
        **HOSPITAL,
        "ccn": "050150",
        "name": "SIERRA NEVADA MEMORIAL HOSPITAL",
        "city": "GRASS VALLEY",
        "state": "CA",
        "website_domain": "example.org",
        "domain_confidence": 0.9,
    }
    engine = make_engine_with_hospital(sierra)
    own = SourceDoc(
        id="fap-sierra",
        kind=SourceKind.HOSPITAL_WEB,
        url="https://www.example.org/sierra-nevada-memorial-hospital/financial-assistance-policy.pdf",
        title="Sierra Nevada Memorial Hospital Financial Assistance Policy",
        fetched_on=TODAY,
        sha256="3" * 64,
    )
    notice = SourceDoc(
        id="fap-california-notice",
        kind=SourceKind.HOSPITAL_WEB,
        url="https://www.example.org/sierra-nevada-memorial-hospital/california-fair-pricing-notice.pdf",
        title="Notice",
        fetched_on=TODAY,
        sha256="4" * 64,
    )
    with session_scope(engine) as session:
        repo.save_source(session, own, POLICY_TEXT, "050150")
        repo.save_source(
            session,
            notice,
            "California Fair Pricing Notice, Sierra Nevada Memorial Hospital. Ask a counselor. "
            * 3,
            "050150",
        )
        ai = GroundedAI()
        result = build_hospital(session, FakeGateway(), ai, "050150", TODAY, reuse_sources=True)
        assert result.outcome == "published"
        assert ai.prompts and all("fap-sierra" in p for p in ai.prompts)
        assert {s.id for s, _ in repo.sources_for(session, "050150")} == {own.id, notice.id}
        assert all(item.kind != "other_state_documents" for item in repo.open_review_items(session))


# Task 7.16: nine published Kaiser California sheets, Los Angeles (050138) among them, cited Kaiser
# Northwest's dental policy, stored next to Kaiser California's own; an admin had confirmed
# kaiserpermanente.org for Los Angeles (7.15c).
KAISER_LOS_ANGELES = {
    **HOSPITAL,
    "ccn": "050138",
    "name": "KAISER FOUNDATION HOSPITAL - LOS ANGELES",
    "city": "LOS ANGELES",
    "state": "CA",
    "zip": "90027",
    "phone": "323-783-4011",
    "website_domain": "kaiserpermanente.org",
    "domain_confidence": 1.0,
}
KAISER_VALLEJO = {
    **KAISER_LOS_ANGELES,
    "ccn": "050073",
    "name": "KAISER FOUNDATION HOSPITAL AND REHAB CENTER",
    "city": "VALLEJO",
    "zip": "94589",
    "phone": "707-651-1000",
}
KAISER_DOCS = "https://healthy.kaiserpermanente.org/content/dam/kporg/final/documents/"
DENTAL_PATH = "dfa/policy/dfa-policy-english-ada.pdf"
SCAL_PATH = "mfa/southern-california/english/mfa-policy-and-addendum-scal-english-ada.pdf"
MFA_TITLE = "POLICY TITLE Medical Financial Assistance"
# The search titled the dental policy after the medical program; its opening names the dental one.
DENTAL_TITLE = "POLICY TITLE: Medical Financial Assistance"
DENTAL_TEXT = (
    "Community Benefits POLICY TITLE Dental Financial Assistance POLICY NUMBER NW.CB.002 "
    "ACCOUNTABLE DEPARTMENT Charity Care, Community Benefits. Kaiser Foundation Health Plan of "
    "Northwest (KFHPNW) is committed to providing programs that support access to dental care for "
    "people with low incomes.\nFederal Poverty Guidelines Percentages Financial Assistance "
    "Discount From To 0% -200% 100% Discount (Full) 201% -300% 75% Discount (Partial) 301% -350% "
    "50% Discount (Partial) 351% -400% 25% Discount (Partial)\n"
)
# Kaiser California's policy opening, then the sample policy's sentences, which GroundedAI quotes.
SCAL_TEXT = (
    "Community Health POLICY TITLE Medical Financial Assistance POLICY NUMBER NATL.CB.307 "
    "ACCOUNTABLE DEPARTMENT National Community Health EFFECTIVE DATE January 1st, 2025\n"
    + POLICY_TEXT
)
DENTAL_NOTE = (
    "1 stored document is about a dental assistance program, not the hospital's financial "
    "assistance (fap-dental); detached and left out of the structurer"
)
NOTHING_LEFT_NOTE = "no financial assistance documents left to structure once those are set aside"


def held_note(quoted):
    return (
        f"{len(quoted)} fields quoted from the documents set aside dropped and the sheet held: "
        + ", ".join(quoted)
    )


def store_kaiser(session, ccn, source_id, path, text, title=MFA_TITLE, docs=KAISER_DOCS):
    source = SourceDoc(
        id=source_id,
        kind=SourceKind.HOSPITAL_WEB,
        url=docs + path,
        title=title,
        fetched_on=TODAY,
        sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    repo.save_source(session, source, text, ccn)
    return source


def publish_quoting(session, ccn, source):
    """A published sheet whose every field quotes `source`, as the nine Kaiser California sheets
    quoted Kaiser Northwest's dental policy before 7.16."""
    from waive.atlas.publish import publish_sheet
    from waive.atlas.samples import st_example_sheet

    body = st_example_sheet().model_dump_json().replace(SAMPLE_SOURCE_ID, source.id)
    sheet = ProcedureSheet.model_validate_json(body).model_copy(
        update={"hospital": repo.hospital_ref(repo.get_hospital(session, ccn)), "sources": [source]}
    )
    return publish_sheet(session, sheet)


class NoSpend:
    def search(self, *args, **kwargs):
        raise AssertionError("Tavily search would spend credits")

    def extract(self, *args, **kwargs):
        raise AssertionError("Tavily extract would spend credits")

    def map(self, *args, **kwargs):
        raise AssertionError("Tavily map would spend credits")


class NoModel(FakeAI):
    def complete_json(self, *args, **kwargs):
        raise AssertionError("the structurer has nothing of the hospital's to read")


def test_a_stored_dental_policy_is_detached_and_never_reaches_the_structurer():
    engine = make_engine_with_hospital(KAISER_LOS_ANGELES)
    with session_scope(engine) as session:
        dental = store_kaiser(
            session, "050138", "fap-dental", DENTAL_PATH, DENTAL_TEXT, DENTAL_TITLE
        )
        store_kaiser(session, "050138", "fap-scal", SCAL_PATH, SCAL_TEXT)
        ai = GroundedAI()
        result = build_hospital(session, NoSpend(), ai, "050138", TODAY, reuse_sources=True)
        assert result.outcome == "published"
        assert DENTAL_NOTE in result.notes
        assert ai.prompts and all(
            "fap-dental" not in p and "NW.CB.002" not in p for p in ai.prompts
        )
        sheet, _ = repo.latest_sheet(session, "050138")
        assert sheet.eligibility.free_care_max_fpl.source_id == "fap-scal"
        assert [s.id for s in sheet.sources] == ["fap-scal"]
        assert [s.id for s, _ in repo.sources_for(session, "050138")] == ["fap-scal"]
        assert review_detail(session, "050138", "dental_documents") == {
            "sources": [{"id": "fap-dental", "url": dental.url}]
        }


def test_a_sheet_that_quoted_a_dental_policy_it_had_alone_is_held_without_those_fields():
    # Kaiser Foundation Hospital and Rehab Center (050073) had stored nothing but the dental
    # policy. Nothing is left to structure: no model is asked, no credit is spent, and a sheet
    # quoting the dental bands cannot stay published.
    engine = make_engine_with_hospital(KAISER_VALLEJO)
    with session_scope(engine) as session:
        dental = store_kaiser(
            session, "050073", "fap-dental", DENTAL_PATH, DENTAL_TEXT, DENTAL_TITLE
        )
        assert publish_quoting(session, "050073", dental).version == 1
        quoted = [path for path, _ in repo.latest_sheet(session, "050073")[0].field_paths()]
        result = build_hospital(session, NoSpend(), NoModel(), "050073", TODAY, reuse_sources=True)
        assert (result.outcome, result.version) == ("held", 2)
        assert result.notes == [DENTAL_NOTE, NOTHING_LEFT_NOTE, held_note(quoted)]
        held, row = repo.latest_sheet(session, "050073")
        assert held.status is SheetStatus.HELD
        assert held.field_paths() == [] and held.sources == []
        assert row.diff["status"] == {"old": "published", "new": "held"}
        assert repo.sources_for(session, "050073") == []
        kinds = sorted(item.kind for item in repo.open_review_items(session, "050073"))
        assert kinds == ["dental_documents", "no_documents"]
        assert review_detail(session, "050073", "no_documents") == {
            "domain": "kaiserpermanente.org",
            "set_aside": ["fap-dental"],
        }
    # With no sheet yet there is nothing to hold: the build stops at the review item.
    engine = make_engine_with_hospital(KAISER_VALLEJO)
    with session_scope(engine) as session:
        store_kaiser(session, "050073", "fap-dental", DENTAL_PATH, DENTAL_TEXT, DENTAL_TITLE)
        result = build_hospital(session, NoSpend(), NoModel(), "050073", TODAY, reuse_sources=True)
        assert (result.outcome, result.version, result.notes) == (
            "skipped",
            None,
            [DENTAL_NOTE, NOTHING_LEFT_NOTE],
        )
        assert repo.latest_sheet(session, "050073") is None


NW_PATH = "mfa/nw/english/mfa-policy-and-addendum-nw-english-ada.pdf"
# Kaiser Northwest's policy, with a free-care limit of its own so a model reading it would show.
NW_TEXT = (
    "Community Health POLICY TITLE Medical Financial Assistance POLICY NUMBER NATL.CB.307 "
    "Kaiser Foundation Health Plan of the Northwest\n" + POLICY_TEXT.replace("250%", "300%")
)


def test_another_kaiser_regions_policy_is_set_aside_beside_californias():
    # California Kaiser hospitals stored Kaiser Northwest's policy next to Southern California's
    # and the dental one (San Rafael, 050510, was given Northwest's by its re-scout in 7.15c).
    # Only the address tells the regions apart: "/mfa/nw/" is Oregon and Washington.
    engine = make_engine_with_hospital(KAISER_LOS_ANGELES)
    with session_scope(engine) as session:
        store_kaiser(session, "050138", "fap-dental", DENTAL_PATH, DENTAL_TEXT, DENTAL_TITLE)
        store_kaiser(session, "050138", "fap-nw", NW_PATH, NW_TEXT)
        store_kaiser(session, "050138", "fap-scal", SCAL_PATH, SCAL_TEXT)
        ai = GroundedAI()
        result = build_hospital(session, NoSpend(), ai, "050138", TODAY, reuse_sources=True)
        assert result.outcome == "published"
        assert result.notes[:2] == [
            DENTAL_NOTE,
            "1 stored document is another state's version of the system policy (fap-nw: OR/WA); "
            "detached and left out of the structurer",
        ]
        assert ai.prompts and all("fap-nw" not in p and "fap-dental" not in p for p in ai.prompts)
        assert all("=== SOURCE id=fap-scal" in p and "names_state=CA" in p for p in ai.prompts)
        sheet, _ = repo.latest_sheet(session, "050138")
        assert sheet.eligibility.free_care_max_fpl.source_id == "fap-scal"
        assert [s.id for s, _ in repo.sources_for(session, "050138")] == ["fap-scal"]
        assert review_detail(session, "050138", "other_state_documents") == {
            "state": "CA",
            "sources": [{"id": "fap-nw", "states": ["OR", "WA"]}],
        }


KAISER_SAN_FRANCISCO = {
    **KAISER_LOS_ANGELES,
    "ccn": "050076",
    "name": "KAISER FOUNDATION HOSPITAL - SAN FRANCISCO",
    "city": "SAN FRANCISCO",
    "zip": "94115",
    "phone": "415-833-2646",
}
WASHINGTON_SITE = "https://wa.kaiserpermanente.org/"
WASHINGTON_PATH = "static/pdf/public/customer-service/charity-care-policy.pdf"
WASHINGTON_TEXT = (
    "Kaiser Foundation Health Plan of Washington. Charity Care Policy.\n" + POLICY_TEXT
)


def test_kaiser_documents_all_of_another_region_are_set_aside_and_the_sheet_held():
    # Kaiser Foundation Hospital - San Francisco (050076), and Redwood City, San Leandro and
    # Moreno Valley with it, had stored nothing but Kaiser Washington's charity care policy and the
    # dental one. Anywhere else, when every document is another state's, all stay: there is
    # nothing to prefer (test_other_state_versions_of_a_system_policy_are_set_aside). On Kaiser's
    # sites the address says whose policy it is, and a California sheet must not publish
    # Washington's: with no sibling to borrow from, it is held, and the hospital waits to be
    # scouted again.
    engine = make_engine_with_hospital(KAISER_SAN_FRANCISCO)
    with session_scope(engine) as session:
        store_kaiser(session, "050076", "fap-dental", DENTAL_PATH, DENTAL_TEXT, DENTAL_TITLE)
        washington = store_kaiser(
            session,
            "050076",
            "fap-washington",
            WASHINGTON_PATH,
            WASHINGTON_TEXT,
            "Charity Care Policy",
            docs=WASHINGTON_SITE,
        )
        assert publish_quoting(session, "050076", washington).version == 1
        quoted = [path for path, _ in repo.latest_sheet(session, "050076")[0].field_paths()]
        result = build_hospital(session, NoSpend(), NoModel(), "050076", TODAY, reuse_sources=True)
        assert (result.outcome, result.version) == ("held", 2)
        assert result.notes == [
            DENTAL_NOTE,
            "1 stored document is another state's version of the system policy "
            "(fap-washington: WA); detached and left out of the structurer",
            NOTHING_LEFT_NOTE,
            held_note(quoted),
        ]
        held, _ = repo.latest_sheet(session, "050076")
        assert held.status is SheetStatus.HELD
        assert held.field_paths() == [] and held.sources == []
        assert repo.sources_for(session, "050076") == []
        assert review_detail(session, "050076", "other_state_documents") == {
            "state": "CA",
            "sources": [{"id": "fap-washington", "states": ["WA"]}],
        }
        assert review_detail(session, "050076", "no_documents") == {
            "domain": "kaiserpermanente.org",
            "set_aside": ["fap-dental", "fap-washington"],
        }


LOS_ANGELES_SHARE = (
    "1 policy document shared from KAISER FOUNDATION HOSPITAL - LOS ANGELES (050138) on "
    "kaiserpermanente.org; "
)


def test_a_hospital_whose_documents_are_all_set_aside_borrows_its_siblings_policy_in_one_run():
    # Review of 7.16: San Francisco (050076) had stored only Kaiser Washington's policy and the
    # dental one. The first build set both aside and held the sheet with a no_documents item;
    # the same command run again found nothing stored and borrowed Los Angeles's California
    # policy. It borrows in the first run now, as a build with nothing stored does.
    engine = make_engine_with_hospital(KAISER_LOS_ANGELES, KAISER_SAN_FRANCISCO)
    with session_scope(engine) as session:
        scal = store_kaiser(session, "050138", "fap-scal", SCAL_PATH, SCAL_TEXT)
        publish_quoting(session, "050138", scal)
        store_kaiser(session, "050076", "fap-dental", DENTAL_PATH, DENTAL_TEXT, DENTAL_TITLE)
        washington = store_kaiser(
            session,
            "050076",
            "fap-washington",
            WASHINGTON_PATH,
            WASHINGTON_TEXT,
            "Charity Care Policy",
            docs=WASHINGTON_SITE,
        )
        publish_quoting(session, "050076", washington)
        ai = GroundedAI()
        result = build_hospital(session, NoSpend(), ai, "050076", TODAY, reuse_sources=True)
        assert (result.outcome, result.version) == ("published", 2)
        assert result.notes[:3] == [
            DENTAL_NOTE,
            "1 stored document is another state's version of the system policy "
            "(fap-washington: WA); detached and left out of the structurer",
            LOS_ANGELES_SHARE + "none of the hospital's own documents is left once those are set "
            "aside",
        ]
        assert ai.prompts and all(
            "fap-washington" not in p and "fap-dental" not in p for p in ai.prompts
        )
        sheet, _ = repo.latest_sheet(session, "050076")
        assert sheet.eligibility.free_care_max_fpl.source_id == "fap-scal"
        assert [s.id for s in sheet.sources] == ["fap-scal"]
        assert [s.id for s, _ in repo.sources_for(session, "050076")] == ["fap-scal"]
        assert review_detail(session, "050076", "shared_sources")["sources"] == ["fap-scal"]
        assert all(item.kind != "no_documents" for item in repo.open_review_items(session))
        # The same command again finds what the first run found.
        again = build_hospital(
            session, NoSpend(), GroundedAI(), "050076", TODAY, reuse_sources=True
        )
        assert (again.outcome, again.version) == ("published", 2)


def test_a_sibling_lends_neither_a_dental_policy_nor_another_kaisers_regions():
    # Review of 7.16: Los Angeles (050138) still held the dental policy and Kaiser Northwest's
    # beside California's when San Francisco (050076), with nothing stored, borrowed from it. All
    # three were linked and listed as shared, and two were detached at once. Vallejo (050073),
    # earlier in line, holds only Northwest's: it has nothing to lend, and the search goes on.
    engine = make_engine_with_hospital(KAISER_VALLEJO, KAISER_LOS_ANGELES, KAISER_SAN_FRANCISCO)
    with session_scope(engine) as session:
        publish_quoting(
            session, "050073", store_kaiser(session, "050073", "fap-nw", NW_PATH, NW_TEXT)
        )
        store_kaiser(session, "050138", "fap-dental", DENTAL_PATH, DENTAL_TEXT, DENTAL_TITLE)
        store_kaiser(session, "050138", "fap-nw", NW_PATH, NW_TEXT)
        publish_quoting(
            session, "050138", store_kaiser(session, "050138", "fap-scal", SCAL_PATH, SCAL_TEXT)
        )
        ai = GroundedAI()
        result = build_hospital(session, NoSpend(), ai, "050076", TODAY, reuse_sources=True)
        assert result.outcome == "published"
        assert result.notes[0] == (
            LOS_ANGELES_SHARE + "the hospital's own documents state no income rules"
        )
        assert ai.prompts and all("fap-dental" not in p and "fap-nw" not in p for p in ai.prompts)
        assert review_detail(session, "050076", "shared_sources")["sources"] == ["fap-scal"]
        assert [s.id for s, _ in repo.sources_for(session, "050076")] == ["fap-scal"]
        assert all(
            item.kind not in ("dental_documents", "other_state_documents")
            for item in repo.open_review_items(session, "050076")
        )
        # The lender keeps everything it stored: what it may cite is its own build's business.
        assert {s.id for s, _ in repo.sources_for(session, "050138")} == {
            "fap-dental",
            "fap-nw",
            "fap-scal",
        }


def test_recheck_withdraw_and_confirmation_reject_fields_quoted_from_a_linked_set_aside_document():
    # Review of 7.16: recheck flagged a field only once its document was no longer linked, so the
    # nine published Kaiser California sheets quoting Kaiser Northwest's dental policy, still
    # linked to them on the local database, passed it. What a build sets aside before structuring
    # is now held to stored sheets too, with a reason each. The dental policy's and Washington's
    # own sentences are stood in for by the sample policy's, which the sheets quote.
    from waive.atlas.pipeline import confirm_domain, recheck_sheets, withdraw_rejected
    from waive.atlas.verify import DENTAL_REASON, OTHER_REGION_REASON

    engine = make_engine_with_hospital(KAISER_LOS_ANGELES, KAISER_SAN_FRANCISCO)
    with session_scope(engine) as session:
        dental = store_kaiser(
            session, "050138", "fap-dental", DENTAL_PATH, DENTAL_TEXT + POLICY_TEXT, DENTAL_TITLE
        )
        publish_quoting(session, "050138", dental)
        washington = store_kaiser(
            session,
            "050076",
            "fap-washington",
            WASHINGTON_PATH,
            WASHINGTON_TEXT,
            "Charity Care Policy",
            docs=WASHINGTON_SITE,
        )
        publish_quoting(session, "050076", washington)
        quoted = [path for path, _ in repo.latest_sheet(session, "050138")[0].field_paths()]
        assert {entry.ccn: entry.rejected for entry in recheck_sheets(session, "CA")} == {
            "050138": [(path, DENTAL_REASON) for path in quoted],
            "050076": [(path, OTHER_REGION_REASON) for path in quoted],
        }
        withdrawn = withdraw_rejected(session, "CA")
        assert sorted((r.ccn, r.outcome, r.version) for r in withdrawn) == [
            ("050076", "held", 2),
            ("050138", "held", 2),
        ]
        for ccn in ("050076", "050138"):
            assert repo.latest_sheet(session, ccn)[0].field_paths() == []
        assert recheck_sheets(session, "CA") == []
    # Confirming the website re-verifies the sheet the same way: San Rafael (050510) was matched
    # at 0.6 and given Kaiser Northwest's policy by its re-scout in 7.15c.
    san_rafael = {
        **KAISER_LOS_ANGELES,
        "ccn": "050510",
        "name": "KAISER FOUNDATION HOSPITAL",
        "city": "SAN RAFAEL",
        "zip": "94903",
        "domain_confidence": 0.6,
    }
    engine = make_engine_with_hospital(san_rafael)
    with session_scope(engine) as session:
        northwest = store_kaiser(session, "050510", "fap-nw", NW_PATH, NW_TEXT + POLICY_TEXT)
        publish_quoting(session, "050510", northwest)
        result = confirm_domain(session, "050510", "kaiserpermanente.org")
        assert (result.outcome, result.version) == ("held", 2)
        assert f"eligibility.free_care_max_fpl: {OTHER_REGION_REASON}" in result.notes
        assert repo.latest_sheet(session, "050510")[0].field_paths() == []


class FailingModel(FakeAI):
    def complete_json(self, *args, **kwargs):
        raise AIOutputError("model output did not match SheetDraft (free_care_max_fpl: dict_type)")


def test_a_failed_structurer_still_holds_the_fields_quoted_from_what_the_build_detached():
    # Review of 7.16: Los Angeles's sheet quoted the dental policy; the rebuild detached it and
    # the structurer then gave nothing usable, which left the dental bands published.
    engine = make_engine_with_hospital(KAISER_LOS_ANGELES)
    with session_scope(engine) as session:
        dental = store_kaiser(
            session, "050138", "fap-dental", DENTAL_PATH, DENTAL_TEXT, DENTAL_TITLE
        )
        store_kaiser(session, "050138", "fap-scal", SCAL_PATH, SCAL_TEXT)
        publish_quoting(session, "050138", dental)
        quoted = [path for path, _ in repo.latest_sheet(session, "050138")[0].field_paths()]
        result = build_hospital(
            session, NoSpend(), FailingModel(), "050138", TODAY, reuse_sources=True
        )
        assert (result.outcome, result.version) == ("failed", 2)
        assert result.notes[0] == DENTAL_NOTE
        assert result.notes[-1] == held_note(quoted)
        held, _ = repo.latest_sheet(session, "050138")
        assert held.status is SheetStatus.HELD and held.field_paths() == []
        kinds = sorted(item.kind for item in repo.open_review_items(session, "050138"))
        assert kinds == ["dental_documents", "structure_failed"]
    # A sheet that quotes none of what was detached is left as it is.
    engine = make_engine_with_hospital(KAISER_LOS_ANGELES)
    with session_scope(engine) as session:
        store_kaiser(session, "050138", "fap-dental", DENTAL_PATH, DENTAL_TEXT, DENTAL_TITLE)
        scal = store_kaiser(session, "050138", "fap-scal", SCAL_PATH, SCAL_TEXT)
        publish_quoting(session, "050138", scal)
        result = build_hospital(
            session, NoSpend(), FailingModel(), "050138", TODAY, reuse_sources=True
        )
        assert (result.outcome, result.version) == ("failed", None)
        assert repo.latest_sheet(session, "050138")[0].status is SheetStatus.PUBLISHED


def store_kaiser_region_documents(session, ccn):
    """Kaiser Northwest's policy, quoting the sample's sentences, with California's and Kaiser
    Washington's beside it."""
    northwest_text = "Kaiser Foundation Health Plan of the Northwest.\n" + POLICY_TEXT
    store_kaiser(session, ccn, "fap-nw", NW_PATH, northwest_text)
    store_kaiser(session, ccn, "fap-scal", SCAL_PATH, SCAL_TEXT.replace("250%", "200%"))
    store_kaiser(
        session,
        ccn,
        "fap-washington",
        WASHINGTON_PATH,
        WASHINGTON_TEXT.replace("250%", "300%"),
        "Charity Care Policy",
        docs=WASHINGTON_SITE,
    )


def test_a_kaiser_hospital_outside_california_keeps_its_own_regions_documents():
    # Review of 7.16: Kaiser Sunnyside (380091, Oregon) is in Kaiser Northwest's region, which
    # serves Oregon and Washington: Northwest's policy is its own, while California's and Kaiser
    # Washington's, which serves Washington alone, are other regions'. Kaiser Permanente Central
    # Hospital (500052, Seattle) keeps both Northwest's and Washington's.
    sunnyside = {
        **KAISER_LOS_ANGELES,
        "ccn": "380091",
        "name": "KAISER SUNNYSIDE MEDICAL CENTER",
        "city": "CLACKAMAS",
        "state": "OR",
        "zip": "97015",
    }
    engine = make_engine_with_hospital(sunnyside)
    with session_scope(engine) as session:
        store_kaiser_region_documents(session, "380091")
        ai = GroundedAI()
        result = build_hospital(session, NoSpend(), ai, "380091", TODAY, reuse_sources=True)
        assert result.outcome == "published"
        detail = review_detail(session, "380091", "other_state_documents")
        assert detail["state"] == "OR"
        assert sorted((entry["id"], entry["states"]) for entry in detail["sources"]) == [
            ("fap-scal", ["CA"]),
            ("fap-washington", ["WA"]),
        ]
        assert ai.prompts and all(
            "names_state=OR,WA" in p and "fap-scal" not in p and "fap-washington" not in p
            for p in ai.prompts
        )
        assert repo.latest_sheet(session, "380091")[0].eligibility.free_care_max_fpl.source_id == (
            "fap-nw"
        )
    seattle = {**sunnyside, "ccn": "500052", "name": "KAISER PERMANENTE CENTRAL HOSPITAL"}
    seattle.update(city="SEATTLE", state="WA", zip="98112")
    engine = make_engine_with_hospital(seattle)
    with session_scope(engine) as session:
        store_kaiser_region_documents(session, "500052")
        ai = GroundedAI()
        build_hospital(session, NoSpend(), ai, "500052", TODAY, reuse_sources=True)
        assert ai.prompts and all("fap-nw" in p and "fap-washington" in p for p in ai.prompts)
        assert all("fap-scal" not in p for p in ai.prompts)
        assert {s.id for s, _ in repo.sources_for(session, "500052")} == {
            "fap-nw",
            "fap-washington",
        }


# Baptist Health's Tagalog policy, which Baptist Beaches and Nassau (100117, 100140) quoted on the
# public atlas although the English one was stored (task 7.15a).
TAGALOG_POLICY = (
    "BAPTIST HEALTH MANWAL NG MGA POLISIYA AT PROSESO Paksa: POLISIYA NG TULONG PINANSYAL NG "
    "OSPITAL. Ayon sa pilosopiya, misyon, at pangunahing mga halaga ng Baptist Health, ang aming "
    "polisiya ay magbigay ng Emergency at Medically Necessary Care para sa lahat ng mga pasyente. "
    "Buong Pinansyal na Tulong: Kabuuang kita ng pamilya ay nasa 200% o mas mababa ng Pederal na "
    "Antas ng Kahirapan (FPL).\nTumawag sa 617-555-0100 mula Lunes hanggang Biyernes.\n"
)
TRANSLATION_NOTE = (
    "1 stored document is not in English (fap-tagalog); left out of the structurer while an "
    "English one states the income rules, and still linked"
)
KEPT_TRANSLATION_NOTE = (
    "1 stored document is not in English (fap-tagalog) and no English one states the income "
    "rules: kept for the structurer, and an english_policy_missing item asks for the English policy"
)


def store_translation(session, ccn):
    source = SourceDoc(
        id="fap-tagalog",
        kind=SourceKind.HOSPITAL_WEB,
        url="https://www.example.org/documents/financial-assistance-policy-tagalog.pdf",
        title="Patakaran",
        fetched_on=TODAY,
        sha256="5" * 64,
    )
    repo.save_source(session, source, TAGALOG_POLICY, ccn)
    return source


def test_a_translation_is_kept_from_the_structurer_while_an_english_policy_is_stored():
    # Task 7.15a: BID Needham, Hoag, Baptist Beaches and Nassau, Brigham and Women's and Hardin
    # Memorial published quotes from a translation of their policy.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY)
        store_translation(session, "229999")
        ai = GroundedAI()
        result = build_hospital(session, FakeGateway(), ai, "229999", TODAY, reuse_sources=True)
        assert result.outcome == "published"
        assert TRANSLATION_NOTE in result.notes
        assert ai.prompts and all("fap-tagalog" not in p for p in ai.prompts)
        sheet, _ = repo.latest_sheet(session, "229999")
        assert "fap-tagalog" not in {source.id for source in sheet.sources}
        # Nothing is detached: the translation stays the hospital's.
        assert "fap-tagalog" in {s.id for s, _ in repo.sources_for(session, "229999")}
        assert all(i.kind != "english_policy_missing" for i in repo.open_review_items(session))
    # When every stored document is a translation there is nothing to prefer: it is read, and an
    # admin is asked for the English policy.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        repo.get_hospital(session, "229999").website_domain = "example.org"
        store_translation(session, "229999")
        ai = GroundedAI()
        result = build_hospital(session, FakeGateway(), ai, "229999", TODAY, reuse_sources=True)
        assert ai.prompts and all("fap-tagalog" in p for p in ai.prompts)
        assert KEPT_TRANSLATION_NOTE in result.notes
        assert review_detail(session, "229999", "english_policy_missing")["translations"] == [
            "fap-tagalog"
        ]


def test_a_translation_stays_while_no_english_document_states_the_income_rules():
    # Review of 7.15b: BID Needham (220083), Baptist Beaches and Nassau (100117, 100140) and Hoag
    # (050224) store English billing pages and a job listing, none naming the poverty level.
    # Setting their translations aside would leave a rebuild with no income rule at all.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        repo.get_hospital(session, "229999").website_domain = "example.org"
        store_thin_page(session, "229999")
        store_translation(session, "229999")
        for _ in range(2):
            ai = GroundedAI()
            result = build_hospital(session, FakeGateway(), ai, "229999", TODAY, reuse_sources=True)
            assert ai.prompts and all("fap-tagalog" in p for p in ai.prompts)
            assert KEPT_TRANSLATION_NOTE in result.notes
        # One open item however often it is rebuilt.
        items = [
            i
            for i in repo.open_review_items(session, "229999")
            if i.kind == "english_policy_missing"
        ]
        assert len(items) == 1 and items[0].detail["translations"] == ["fap-tagalog"]


def test_a_hospitals_own_set_aside_translation_is_never_recorded_as_shared_from_a_sibling():
    # Review of 7.15b: Baptist Beaches and Nassau (100117, 100140) share their Tagalog policy. When
    # the second pass borrows from the sibling, the hospital's own translation, set aside on the
    # first pass, must not come back as the sibling's document.
    engine = make_engine_with_hospital(HOSPITAL, SIBLING)
    with session_scope(engine) as session:
        build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY)
        store_translation(session, "229999")
        repo.link_source(session, "220031", "fap-tagalog")
        english = "Financial assistance is based on the Federal Poverty Guidelines. Call us. " * 3
        page = SourceDoc(
            id="billing-real",
            kind=SourceKind.HOSPITAL_WEB,
            url="https://www.example.org/real/billing",
            title="Billing | Real General",
            fetched_on=TODAY,
            sha256="6" * 64,
        )
        repo.save_source(session, page, english, "220031")
        result = build_hospital(
            session, FakeGateway(), GroundedAI(), "220031", TODAY, reuse_sources=True
        )
        assert result.outcome == "published"
        shared = review_detail(session, "220031", "shared_sources")["sources"]
        assert shared and "fap-tagalog" not in shared


def test_a_field_quoted_from_a_translation_is_withdrawn_while_an_english_policy_is_stored():
    # Review of 7.15b: 16 published sheets cite a translation while an English document stating
    # the income rules is stored (Anna Jaques 220029 in Italian, four fields; Edward 140231 in
    # Japanese; Jefferson Northeast 390115 in Vietnamese). recheck lists such a field and
    # withdraw drops it at no cost; a rebuild then quotes the English document.
    from waive.atlas.pipeline import recheck_sheets, withdraw_rejected
    from waive.atlas.schema import Cited
    from waive.atlas.verify import TRANSLATION_REASON

    engine = make_engine_with_hospital(REAL_HOSPITAL)
    with session_scope(engine) as session:
        build_hospital(session, FakeGateway(), FakeAI(), "220031", TODAY)
        translation = store_translation(session, "220031")
        sheet, _ = repo.latest_sheet(session, "220031")
        phone = Cited(
            value="617-555-0100",
            quote="Tumawag sa 617-555-0100 mula Lunes hanggang Biyernes.",
            source_id=translation.id,
            checked_on=TODAY,
        )
        quoted = sheet.model_copy(
            update={
                "contacts": sheet.contacts.model_copy(update={"phone": phone}),
                "sources": [*sheet.sources, translation],
                "version": 2,
            }
        )
        repo.add_sheet_version(session, quoted, {"contacts.phone": {"old": None}})
        [entry] = recheck_sheets(session, "MA")
        assert entry.rejected == [("contacts.phone", TRANSLATION_REASON)]
        [result] = withdraw_rejected(session, "MA")
        assert result.outcome == "published"
        latest, _ = repo.latest_sheet(session, "220031")
        assert latest.contacts.phone is None
        assert latest.eligibility.free_care_max_fpl.value == 250
        # Without an English document that states the income rules, the translation's field
        # stands (the build files an english_policy_missing item instead).
        for source, _ in repo.sources_for(session, "220031"):
            if source.id != translation.id:
                repo.unlink_source(session, "220031", source.id)
        store_thin_page(session, "220031")
        repo.add_sheet_version(
            session, quoted.model_copy(update={"version": 4}), {"contacts.phone": {"old": None}}
        )
        assert all(
            reason != TRANSLATION_REASON
            for entry in recheck_sheets(session, "MA")
            for _, reason in entry.rejected
        )


def test_a_siblings_translation_is_kept_from_the_structurer_on_both_sharing_paths():
    # The sibling's stored translation names the poverty level, so it is shared with its policy.
    engine = make_engine_with_hospital(HOSPITAL, SIBLING)
    with session_scope(engine) as session:
        build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY)
        store_translation(session, "229999")
        # The second pass: the hospital's own thin page states no income rule.
        store_thin_page(session, "220031")
        ai = GroundedAI()
        result = build_hospital(session, FakeGateway(), ai, "220031", TODAY, reuse_sources=True)
        assert result.outcome == "published"
        assert len(ai.prompts) >= 2 and all("fap-tagalog" not in p for p in ai.prompts)
        assert TRANSLATION_NOTE in result.notes
    engine = make_engine_with_hospital(HOSPITAL, SIBLING)
    with session_scope(engine) as session:
        build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY)
        store_translation(session, "229999")
        # Nothing stored of its own: the sibling's documents are shared before the first pass.
        ai = GroundedAI()
        result = build_hospital(session, FakeGateway(), ai, "220031", TODAY, reuse_sources=True)
        assert result.outcome == "published"
        assert ai.prompts and all("fap-tagalog" not in p for p in ai.prompts)
        assert TRANSLATION_NOTE in result.notes


class TwoSentenceAI(FakeAI):
    """Quotes a list under two sentences, only the second of which the source contains: the
    model stitched a passage, trim_quotes keeps the real sentence, and the list must follow."""

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        draft = super().complete_json(role, messages, schema, phi=phi, purpose=purpose)
        draft.documents_required = DraftField(
            value=["photo_id", "bank_statements", "proof_of_income"],
            quote=(
                "Bring your passport and a recent bank statement. "
                "Applicants must provide a photo ID and one proof of income."
            ),
            source_id=draft.phone.source_id,
        )
        return draft


def test_lists_are_pruned_again_against_the_trimmed_quote():
    # Review of 7.9: the structurer filtered documents_required against the model's whole quote,
    # then trim_quotes kept the one sentence the source contains, and 140202 published photo_id
    # and bank_statements under a sentence naming only W-2s, tax returns and pay stubs.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(session, FakeGateway(), TwoSentenceAI(), "229999", TODAY)
        assert result.outcome == "published"
        sheet, _ = repo.latest_sheet(session, "229999")
        cited = sheet.apply.documents_required
        assert [kind.value for kind in cited.value] == ["photo_id", "proof_of_income"]
        assert cited.quote == "Applicants must provide a photo ID and one proof of income."
        assert "apply.documents_required: 1 item not named in the trimmed quote" in result.notes
        texts = {s.id: t for s, t in repo.sources_for(session, "229999")}
        assert verify_sheet(sheet, texts).ok


NEWER_POLICY = SourceDoc(
    id="fap-newer-pdf",
    kind=SourceKind.HOSPITAL_WEB,
    url="https://www.example.org/charity-care-policy-revised.pdf",
    title="Charity Care and Discount Payment Policy",
    fetched_on=TODAY,
    sha256="5" * 64,
)
NEWER_POLICY_TEXT = (
    "Charity Care and Discount Payment Policy. Revised April 20, 2026.\n"
    "Patients whose family income is at or below 200 percent of the Federal Poverty Level "
    "will be eligible for a 100 percent write-off of their balance.\n"
)


def test_two_documents_naming_different_free_care_limits_hold_the_sheet():
    # AHMC Anaheim (050226, review of 7.9): the web page said 0-250% of FPL is no-cost care, the
    # newer PDF said 200%, and the sheet published 250 with nothing to trip on.
    engine = make_engine_with_hospital()
    newer, newer_text = NEWER_POLICY, NEWER_POLICY_TEXT
    with session_scope(engine) as session:
        build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY)
        repo.save_source(session, newer, newer_text, "229999")
        result = build_hospital(
            session, FakeGateway(), GroundedAI(), "229999", TODAY, reuse_sources=True
        )
        assert (result.outcome, result.version) == ("held", 2)
        assert (
            "the hospital's documents state different free-care limits (fap-newer-pdf: 200%)"
        ) in result.notes
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.status is SheetStatus.HELD
        assert sheet.eligibility.free_care_max_fpl.value == 250  # kept for the admin to compare
        detail = review_detail(session, "229999", "document_conflict")
        assert detail["published"] == "250" and detail["path"] == "eligibility.free_care_max_fpl"
        assert detail["documents"] == [{"id": "fap-newer-pdf", "limits": ["200"]}]
    # A patient's photo of a newer policy was approved by an admin: it supersedes, not conflicts.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY)
        photo = newer.model_copy(update={"id": "photo-newer", "kind": SourceKind.PATIENT_PHOTO})
        repo.save_source(session, photo, newer_text, "229999")
        result = build_hospital(
            session, FakeGateway(), GroundedAI(), "229999", TODAY, reuse_sources=True
        )
        assert result.outcome == "published"


def test_recheck_lists_stored_fields_the_current_rules_reject_and_withdraw_republishes_without_them():
    # Review of 7.9: four national sheets and sixteen Massachusetts fields kept values the new
    # rules had been written for because nobody re-ran verification over stored sheets.
    from waive.atlas.pipeline import recheck_sheets, withdraw_rejected
    from waive.atlas.schema import Cited, DocType

    engine = make_engine_with_hospital(REAL_HOSPITAL)
    with session_scope(engine) as session:
        build_hospital(session, FakeGateway(), FakeAI(), "220031", TODAY)
        sheet, _ = repo.latest_sheet(session, "220031")
        source_id = sheet.eligibility.free_care_max_fpl.source_id
        # An older rule set let these through: a boolean under a quote that never mentions
        # assets, and a document list the quote names only part of.
        stale = sheet.model_copy(
            update={
                "eligibility": sheet.eligibility.model_copy(
                    update={
                        "asset_test": Cited(
                            value=False,
                            quote=FREE_CARE_QUOTE,
                            source_id=source_id,
                            checked_on=TODAY,
                        )
                    }
                ),
                "apply": sheet.apply.model_copy(
                    update={
                        "documents_required": Cited(
                            value=[DocType.PHOTO_ID, DocType.BANK_STATEMENTS],
                            quote="Applicants must provide a photo ID and one proof of income.",
                            source_id=source_id,
                            checked_on=TODAY,
                        )
                    }
                ),
                "version": 2,
            }
        )
        repo.add_sheet_version(session, stale, {"eligibility.asset_test": {"old": None}})
        assert recheck_sheets(session, "XX") == []
        [entry] = recheck_sheets(session, "MA")
        assert (entry.ccn, entry.status) == ("220031", SheetStatus.PUBLISHED)
        assert entry.rejected == [
            ("eligibility.asset_test", "quote does not support the asset-test value"),
            ("apply.documents_required", "quote does not name every listed item"),
        ]
        [result] = withdraw_rejected(session, "MA")
        assert (result.outcome, result.version) == ("published", 3)
        assert result.notes == [
            "apply.documents_required: 1 item not named in the trimmed quote",
            "eligibility.asset_test: quote does not support the asset-test value",
        ]
        latest, _ = repo.latest_sheet(session, "220031")
        assert latest.status is SheetStatus.PUBLISHED and latest.version == 3
        assert latest.eligibility.asset_test is None
        assert [kind.value for kind in latest.apply.documents_required.value] == ["photo_id"]
        assert latest.eligibility.free_care_max_fpl.value == 250
        assert review_detail(session, "220031", "recheck")["rejected"] == [
            ["eligibility.asset_test", "quote does not support the asset-test value"]
        ]
        assert recheck_sheets(session, "MA") == []
        assert withdraw_rejected(session, "MA") == []  # nothing left to withdraw


def test_withdraw_holds_a_published_sheet_whose_ceiling_is_quoted_from_the_free_care_band():
    # Russell Regional (171350): dropping the ceiling alone would leave the sheet published.
    from waive.atlas.pipeline import recheck_sheets, withdraw_rejected
    from waive.atlas.schema import Cited

    engine = make_engine_with_hospital(REAL_HOSPITAL)
    with session_scope(engine) as session:
        build_hospital(session, RussellGateway(), FakeAI(), "220031", TODAY)
        sheet, _ = repo.latest_sheet(session, "220031")
        assert sheet.status is SheetStatus.PUBLISHED
        ceiling = Cited(
            value=Decimal(70),
            quote=RUSSELL_FREE_LINE,
            source_id=sheet.eligibility.free_care_max_fpl.source_id,
            checked_on=TODAY,
        )
        stale = sheet.model_copy(
            update={
                "eligibility": sheet.eligibility.model_copy(
                    update={"assistance_ceiling_fpl": ceiling}
                ),
                "version": 2,
            }
        )
        repo.add_sheet_version(
            session, stale, {"eligibility.assistance_ceiling_fpl": {"old": None}}
        )
        [entry] = recheck_sheets(session, "MA")
        assert entry.rejected == [("eligibility.assistance_ceiling_fpl", CEILING_FREE_BAND_REASON)]
        [result] = withdraw_rejected(session, "MA")
        assert (result.outcome, result.version) == ("held", 3)
        latest, _ = repo.latest_sheet(session, "220031")
        assert latest.status is SheetStatus.HELD
        assert latest.eligibility.assistance_ceiling_fpl is None
        assert latest.eligibility.free_care_max_fpl.value == 250  # kept for the admin to compare


def test_documents_that_never_name_the_hospitals_state_hold_the_sheet_until_the_domain_is_set():
    # National batch 2: Bellevue Hospital (Ohio) was published from Overlake Medical Center's
    # policy (Bellevue, Washington). The scouted policy here is St. Example's, of Boston, MA.
    ohio = {**HOSPITAL, "ccn": "360107", "name": "BELLEVUE HOSPITAL", "city": "BELLEVUE"}
    ohio.update(state="OH", zip="44811")
    engine = make_engine_with_hospital(ohio)
    with session_scope(engine) as session:
        result = build_hospital(session, FakeGateway(), FakeAI(), "360107", TODAY)
        assert result.outcome == "held"
        assert (
            "the documents cannot be tied to this hospital's place: the documents never name Ohio"
        ) in result.notes
        sheet, _ = repo.latest_sheet(session, "360107")
        assert sheet.status is SheetStatus.HELD
        assert review_detail(session, "360107", "place_unconfirmed") == {
            "domain": "example.org",
            "reason": "the documents never name Ohio",
        }
        # An admin confirms the site by setting the domain by hand: the check stands down.
        row = repo.get_hospital(session, "360107")
        row.domain_confidence = 1.0
        session.flush()
        result = build_hospital(
            session, FakeGateway(), FakeAI(), "360107", TODAY, reuse_sources=True
        )
        assert result.outcome == "published"


def test_recheck_holds_a_published_sheet_whose_documents_cannot_be_tied_to_its_place():
    # MetroWest Medical Center (220175) stood published from Mass General Brigham's policy for a
    # week before the place check existed; `recheck --withdraw` must reach such sheets without a
    # model call.
    from waive.atlas.pipeline import recheck_sheets, withdraw_rejected

    engine = make_engine_with_hospital(REAL_HOSPITAL)
    with session_scope(engine) as session:
        assert build_hospital(session, FakeGateway(), FakeAI(), "220031", TODAY).outcome == (
            "published"
        )
        assert recheck_sheets(session, "MA") == []
        # The same sheet, had the hospital been in Ohio: its Boston policy never names Ohio.
        row = repo.get_hospital(session, "220031")
        row.state = "OH"
        session.flush()
        [entry] = recheck_sheets(session, "OH")
        assert entry.rejected == [
            (
                "sheet",
                "the documents cannot be tied to this hospital's place: "
                "the documents never name Ohio",
            )
        ]
        [result] = withdraw_rejected(session, "OH")
        assert result.outcome == "held"
        sheet, _ = repo.latest_sheet(session, "220031")
        assert sheet.status is SheetStatus.HELD
        assert sheet.eligibility.free_care_max_fpl is not None  # kept for the admin to compare
        assert review_detail(session, "220031", "place_unconfirmed")["reason"] == (
            "the documents never name Ohio"
        )
        assert recheck_sheets(session, "OH") == []  # held sheets are not listed again


class GuessGateway(FakeGateway):
    """Discovery's only hit names neither the hospital nor its phone: a guess (confidence 0.6)."""

    def search(self, query, **kwargs):
        if kwargs.get("include_domains"):
            return super().search(query, **kwargs)
        return [SearchHit("https://www.example.org/", "Paying your bill", "Questions?", 0.8)]


UNCONFIRMED_NOTE = (
    "the hospital's website was matched with low confidence and is not confirmed yet: "
    "example.org (confidence 0.6)"
)
REBUILD_NOTE = "re-checking that needs model calls (Rebuild from stored documents)"
# Real General with a website discovery matched with low confidence and never confirmed.
UNSURE_HOSPITAL = {**REAL_HOSPITAL, "website_domain": "example.org", "domain_confidence": 0.6}


def test_a_sheet_from_a_website_matched_with_low_confidence_is_held_until_an_admin_confirms_it():
    # Decision 7.9a-1 (task 7.13). 7.11a: of about 20 sheets published from a 0.6 match, three
    # were another hospital's site on day one (Bellevue Hospital OH from Overlake Medical
    # Center's, in Bellevue, Washington).
    engine = make_engine_with_hospital(REAL_HOSPITAL)
    with session_scope(engine) as session:
        result = build_hospital(session, GuessGateway(), FakeAI(), "220031", TODAY)
        assert (result.outcome, result.version) == ("held", 1)
        assert "domain example.org needs review" in result.notes
        assert UNCONFIRMED_NOTE in result.notes
        sheet, _ = repo.latest_sheet(session, "220031")
        assert sheet.status is SheetStatus.HELD
        assert sheet.eligibility.free_care_max_fpl.value == 250  # kept for the admin to compare
        assert review_detail(session, "220031", "domain_unconfirmed") == {
            "domain": "example.org",
            "confidence": 0.6,
        }
        # The same documents publish once an admin has confirmed the website (confidence 1.0).
        repo.get_hospital(session, "220031").domain_confidence = 1.0
        result = build_hospital(
            session, FakeGateway(), FakeAI(), "220031", TODAY, reuse_sources=True
        )
        assert (result.outcome, result.version) == ("published", 2)
        assert UNCONFIRMED_NOTE not in result.notes


@pytest.mark.parametrize(
    ("confidence", "outcome"),
    [
        (None, "held"),  # a domain set with no confidence recorded
        (0.6, "held"),
        (0.79, "held"),
        (MIN_CONFIDENCE, "published"),
        (0.95, "published"),  # the loop's hand-entered domains (7.11b): still place-checked
        (1.0, "published"),
    ],
)
def test_only_a_website_matched_with_enough_confidence_or_confirmed_publishes(confidence, outcome):
    hospital = {**REAL_HOSPITAL, "website_domain": "example.org", "domain_confidence": confidence}
    engine = make_engine_with_hospital(hospital)
    with session_scope(engine) as session:
        assert build_hospital(session, FakeGateway(), FakeAI(), "220031", TODAY).outcome == outcome
        kinds = {item.kind for item in repo.open_review_items(session, "220031")}
        assert ("domain_unconfirmed" in kinds) is (outcome == "held")


def test_recheck_holds_a_published_sheet_whose_website_is_not_confirmed():
    # About 20 sheets stood published from a 0.6 match before decision 7.9a-1; `recheck
    # --withdraw` must reach them without a model call.
    from waive.atlas.pipeline import recheck_sheets, withdraw_rejected

    engine = make_engine_with_hospital(REAL_HOSPITAL)
    with session_scope(engine) as session:
        assert build_hospital(session, FakeGateway(), FakeAI(), "220031", TODAY).outcome == (
            "published"
        )
        assert recheck_sheets(session, "MA") == []
        # As discovery left the sheets it published before 7.13: a guess nobody confirmed.
        repo.get_hospital(session, "220031").domain_confidence = 0.6
        session.flush()
        [entry] = recheck_sheets(session, "MA")
        assert (entry.ccn, entry.status) == ("220031", SheetStatus.PUBLISHED)
        assert entry.rejected == [("sheet", UNCONFIRMED_NOTE)]
        [result] = withdraw_rejected(session, "MA")
        assert (result.outcome, result.version, result.notes) == ("held", 2, [UNCONFIRMED_NOTE])
        sheet, row = repo.latest_sheet(session, "220031")
        assert sheet.status is SheetStatus.HELD
        assert sheet.eligibility.free_care_max_fpl.value == 250  # kept for the admin to compare
        assert row.diff == {"status": {"old": "published", "new": "held"}}
        assert review_detail(session, "220031", "domain_unconfirmed") == {
            "domain": "example.org",
            "confidence": 0.6,
        }
        # Nothing was pruned or rejected, so there is no empty `recheck` card for the queue.
        assert repo.review_items_of_kind(session, "220031", "recheck") == []
        assert recheck_sheets(session, "MA") == []  # held sheets are not listed again
        assert withdraw_rejected(session, "MA") == []


def test_an_admin_confirming_the_website_publishes_a_sheet_held_only_for_it():
    from waive.atlas.pipeline import confirm_domain

    engine = make_engine_with_hospital(REAL_HOSPITAL)
    with session_scope(engine) as session:
        assert build_hospital(session, GuessGateway(), FakeAI(), "220031", TODAY).outcome == "held"
        # A page left open while the website changed confirms nothing.
        stale = confirm_domain(session, "220031", "other.org")
        assert (stale.outcome, stale.version) == ("skipped", None)
        assert stale.notes == ["the website is example.org now, not other.org; nothing confirmed"]
        assert repo.get_hospital(session, "220031").domain_confidence == 0.6

        result = confirm_domain(session, "220031", "example.org")
        assert (result.outcome, result.version) == ("published", 2)
        assert result.notes == ["website example.org confirmed"]
        assert repo.get_hospital(session, "220031").domain_confidence == 1.0
        sheet, row = repo.latest_sheet(session, "220031")
        assert sheet.status is SheetStatus.PUBLISHED
        assert row.diff == {"status": {"old": "held", "new": "published"}}
        # Answered: the website hold and discovery's "needs review" item are resolved.
        kinds = {item.kind for item in repo.open_review_items(session, "220031")}
        assert not kinds & {"domain_unconfirmed", "domain"}
        items = repo.review_items_of_kind(session, "220031", "domain_unconfirmed")
        assert [item.status for item in items] == ["resolved"]
        # The confirmation is on record, so a confirmed guess can be told from a hand-set 1.0.
        [record] = repo.review_items_of_kind(session, "220031", "website_confirmed")
        assert (record.status, record.detail) == (
            "resolved",
            {"domain": "example.org", "previous_confidence": 0.6},
        )
        # Nothing to do twice, and nothing recorded twice.
        again = confirm_domain(session, "220031", "example.org")
        assert (again.outcome, again.version) == ("published", 2)
        assert len(repo.review_items_of_kind(session, "220031", "website_confirmed")) == 1


def test_confirming_the_website_keeps_the_sheet_held_while_its_documents_disagree():
    from waive.atlas.pipeline import confirm_domain

    engine = make_engine_with_hospital(REAL_HOSPITAL)
    with session_scope(engine) as session:
        assert build_hospital(session, FakeGateway(), FakeAI(), "220031", TODAY).outcome == (
            "published"
        )
        repo.save_source(session, NEWER_POLICY, NEWER_POLICY_TEXT, "220031")
        repo.get_hospital(session, "220031").domain_confidence = 0.6
        result = build_hospital(
            session, FakeGateway(), GroundedAI(), "220031", TODAY, reuse_sources=True
        )
        assert (result.outcome, result.version) == ("held", 2)
        assert UNCONFIRMED_NOTE in result.notes
        result = confirm_domain(session, "220031", "example.org")
        assert (result.outcome, result.version) == ("held", 2)
        assert result.notes == [
            "website example.org confirmed",
            "the hospital's documents state different free-care limits (fap-newer-pdf: 200%)",
        ]
        assert repo.latest_sheet(session, "220031")[0].status is SheetStatus.HELD
        assert repo.get_hospital(session, "220031").domain_confidence == 1.0
        kinds = {item.kind for item in repo.open_review_items(session, "220031")}
        assert "domain_unconfirmed" not in kinds and "document_conflict" in kinds


def test_confirming_the_website_leaves_a_model_disagreement_to_a_rebuild():
    # A stored sheet keeps neither model's draft, so a disagreement that held it cannot be
    # re-checked without model calls: the sheet stays held and the note says what it takes.
    from waive.atlas.pipeline import confirm_domain, withdraw_rejected

    engine = make_engine_with_hospital(UNSURE_HOSPITAL)
    with session_scope(engine) as session:
        ai = FakeAI(free_limit_for_fast="300", free_limit_for_tiebreak="999")
        assert build_hospital(session, FakeGateway(), ai, "220031", TODAY).outcome == "held"
        result = confirm_domain(session, "220031", "example.org")
        assert (result.outcome, result.version) == ("held", 1)
        assert result.notes == [
            "website example.org confirmed",
            f"critical fields disagree: eligibility.free_care_max_fpl; {REBUILD_NOTE}",
        ]
        # A rebuild in which the models agree publishes it.
        result = build_hospital(
            session, FakeGateway(), FakeAI(), "220031", TODAY, reuse_sources=True
        )
        assert result.outcome == "published"
    # A disagreement the tie-break settled in the same build never held the sheet.
    engine = make_engine_with_hospital(UNSURE_HOSPITAL)
    with session_scope(engine) as session:
        ai = FakeAI(free_limit_for_fast="300", free_limit_for_tiebreak="250")
        assert build_hospital(session, FakeGateway(), ai, "220031", TODAY).outcome == "held"
        assert confirm_domain(session, "220031", "example.org").outcome == "published"
    # Nor does one from a build before the sheet was last published: that build settled it.
    engine = make_engine_with_hospital(REAL_HOSPITAL)
    with session_scope(engine) as session:
        ai = FakeAI(free_limit_for_fast="300", free_limit_for_tiebreak="999")
        assert build_hospital(session, FakeGateway(), ai, "220031", TODAY).outcome == "held"
        result = build_hospital(
            session, FakeGateway(), FakeAI(), "220031", TODAY, reuse_sources=True
        )
        assert (result.outcome, result.version) == ("published", 2)
        repo.get_hospital(session, "220031").domain_confidence = 0.6
        assert [r.outcome for r in withdraw_rejected(session, "MA")] == ["held"]
    with session_scope(engine) as session:  # a later request: timestamps read back from storage
        result = confirm_domain(session, "220031", "example.org")
        assert (result.outcome, result.version) == ("published", 4)
    # One from a rebuild after the sheet was published still holds it.
    engine = make_engine_with_hospital(REAL_HOSPITAL)
    with session_scope(engine) as session:
        assert build_hospital(session, FakeGateway(), FakeAI(), "220031", TODAY).outcome == (
            "published"
        )
        repo.get_hospital(session, "220031").domain_confidence = 0.6
        ai = FakeAI(free_limit_for_fast="300", free_limit_for_tiebreak="999")
        result = build_hospital(session, FakeGateway(), ai, "220031", TODAY, reuse_sources=True)
        assert (result.outcome, result.version) == ("held", 2)
    with session_scope(engine) as session:
        result = confirm_domain(session, "220031", "example.org")
        assert (result.outcome, result.version) == ("held", 2)
        assert f"critical fields disagree: eligibility.free_care_max_fpl; {REBUILD_NOTE}" in (
            result.notes
        )


def test_confirming_the_website_keeps_a_patient_share_table_held():
    # Verification dropped the table, so the stored sheet alone would look publishable; the
    # build's review item is the only record of why it was held.
    from waive.atlas.pipeline import confirm_domain

    engine = make_engine_with_hospital(UNSURE_HOSPITAL)
    with session_scope(engine) as session:
        result = build_hospital(session, MercyGateway(), PatientShareAI(), "220031", TODAY)
        assert result.outcome == "held"
        result = confirm_domain(session, "220031", "example.org")
        assert (result.outcome, result.version) == ("held", 1)
        assert f"eligibility.discount_tiers: {PATIENT_SHARE_REASON}; {REBUILD_NOTE}" in (
            result.notes
        )
        sheet, _ = repo.latest_sheet(session, "220031")
        assert sheet.status is SheetStatus.HELD and sheet.eligibility.discount_tiers is None


def test_confirming_the_website_keeps_a_band_from_another_states_facilities_held():
    # Verification dropped the free-care limit, so the stored table alone would look publishable;
    # the build's verification item is the record of why it was held.
    from waive.atlas.pipeline import confirm_domain

    engine = make_engine_with_hospital(UNSURE_HOSPITAL)
    with session_scope(engine) as session:
        result = build_hospital(
            session, UnityPointGateway(), OtherStateScheduleAI(), "220031", TODAY
        )
        assert result.outcome == "held"
        result = confirm_domain(session, "220031", "example.org")
        assert (result.outcome, result.version) == ("held", 1)
        assert (
            f"eligibility.free_care_max_fpl: {OTHER_STATE_FACILITY_REASON}; {REBUILD_NOTE}"
        ) in result.notes
        sheet, _ = repo.latest_sheet(session, "220031")
        assert sheet.status is SheetStatus.HELD and sheet.eligibility.free_care_max_fpl is None


def test_confirming_the_website_holds_a_stored_ceiling_quoted_from_the_free_care_band():
    # The re-verification itself finds the rejection: it holds the sheet, and nothing about it
    # needs a rebuild to re-check.
    from waive.atlas.pipeline import confirm_domain
    from waive.atlas.schema import Cited

    engine = make_engine_with_hospital(UNSURE_HOSPITAL)
    with session_scope(engine) as session:
        assert build_hospital(session, RussellGateway(), FakeAI(), "220031", TODAY).outcome == (
            "held"
        )
        sheet, _ = repo.latest_sheet(session, "220031")
        ceiling = Cited(
            value=Decimal(70),
            quote=RUSSELL_FREE_LINE,
            source_id=sheet.eligibility.free_care_max_fpl.source_id,
            checked_on=TODAY,
        )
        stale = sheet.model_copy(
            update={
                "eligibility": sheet.eligibility.model_copy(
                    update={"assistance_ceiling_fpl": ceiling}
                ),
                "version": 2,
            }
        )
        repo.add_sheet_version(
            session, stale, {"eligibility.assistance_ceiling_fpl": {"old": None}}
        )
        result = confirm_domain(session, "220031", "example.org")
        assert (result.outcome, result.version) == ("held", 3)
        assert f"eligibility.assistance_ceiling_fpl: {CEILING_FREE_BAND_REASON}" in result.notes
        assert not any(REBUILD_NOTE in note for note in result.notes)
        latest, _ = repo.latest_sheet(session, "220031")
        assert latest.eligibility.assistance_ceiling_fpl is None
        assert latest.eligibility.free_care_max_fpl.value == 250


def test_an_admin_marking_the_website_wrong_clears_it_and_keeps_the_sheet_held():
    from waive.atlas.pipeline import reject_domain

    engine = make_engine_with_hospital(REAL_HOSPITAL)
    with session_scope(engine) as session:
        assert build_hospital(session, GuessGateway(), FakeAI(), "220031", TODAY).outcome == "held"
        stale = reject_domain(session, "220031", "other.org", "not the hospital")
        assert (stale.outcome, stale.notes) == (
            "skipped",
            ["the website is example.org now, not other.org; nothing cleared"],
        )
        result = reject_domain(session, "220031", "example.org", "another hospital's site")
        assert (result.outcome, result.version) == ("held", 2)
        assert result.notes[0] == (
            "domain example.org cleared (another hospital's site); 1 documents detached"
        )
        row = repo.get_hospital(session, "220031")
        assert (row.website_domain, row.domain_confidence) == (None, None)
        kinds = [item.kind for item in repo.open_review_items(session, "220031")]
        assert "domain_unconfirmed" not in kinds
        # The item clear_wrong_domain files keeps the scheduler off meanwhile (FAILURE_KINDS);
        # discovery's own item about example.org is answered.
        assert review_detail(session, "220031", "domain")["cleared"] == "example.org"
        assert kinds.count("domain") == 1


# Policy PDFs a hospital's site links to often sit on an asset host (fetch.DOCUMENT_HOSTS): Sutter
# on sitecorecloud.io, Adirondack on website-files.com (task 7.9).
ASSET_POLICY = SourceDoc(
    id="fap-asset-host",
    kind=SourceKind.HOSPITAL_WEB,
    url="https://d111111abcdef8.cloudfront.net/policies/financial-assistance.pdf",
    title="Financial Assistance Policy",
    fetched_on=TODAY,
    sha256="6" * 64,
)


class NothingThereGateway(FakeGateway):
    """Discovery guesses newsite.org (0.6); searching and mapping that site finds nothing."""

    def search(self, query, **kwargs):
        if kwargs.get("include_domains"):
            return []
        return [SearchHit("https://www.newsite.org/", "Paying your bill", "Questions?", 0.8)]

    def map(self, url, **kwargs):
        return []


def test_a_rejected_websites_policy_on_an_asset_host_goes_with_it_and_cannot_come_back():
    # Review of 7.13: "Wrong website" left the asset-host PDF linked and its fields on the held
    # sheet, and confirming the next site discovery guessed published the rejected site's limits.
    from waive.atlas.pipeline import confirm_domain, reject_domain, withdraw_rejected

    sure = {**REAL_HOSPITAL, "website_domain": "example.org", "domain_confidence": 0.9}
    engine = make_engine_with_hospital(sure)
    with session_scope(engine) as session:
        repo.save_source(session, ASSET_POLICY, POLICY_TEXT, "220031")
        result = build_hospital(
            session, FakeGateway(), GroundedAI(), "220031", TODAY, reuse_sources=True
        )
        assert result.outcome == "published"
        sheet, _ = repo.latest_sheet(session, "220031")
        assert sheet.eligibility.free_care_max_fpl.source_id == ASSET_POLICY.id
        repo.get_hospital(session, "220031").domain_confidence = 0.6
        assert [r.outcome for r in withdraw_rejected(session, "MA")] == ["held"]

        result = reject_domain(session, "220031", "example.org", "another hospital's site")
        assert result.notes[0] == (
            "domain example.org cleared (another hospital's site); 1 documents detached"
        )
        held, _ = repo.latest_sheet(session, "220031")
        assert held.eligibility.free_care_max_fpl is None
        assert ASSET_POLICY.id not in {source.id for source in held.sources}
        assert repo.sources_for(session, "220031") == []

        # The next build guesses another site and finds nothing there: no new version.
        result = build_hospital(session, NothingThereGateway(), FakeAI(), "220031", TODAY)
        assert result.notes == [
            "domain newsite.org needs review",
            "no financial assistance documents found",
        ]
        result = confirm_domain(session, "220031", "newsite.org")
        assert (result.outcome, result.version) == ("held", 3)
        assert result.notes == [
            "website newsite.org confirmed",
            "the sheet was not built from newsite.org; it stays held until a build scouts it: "
            "uv run waive atlas build --state MA --ccn 220031",
        ]
        assert repo.get_hospital(session, "220031").domain_confidence == 1.0
        assert repo.latest_sheet(session, "220031")[0].status is SheetStatus.HELD


def test_confirming_a_website_the_held_sheet_was_not_built_from_keeps_it_held():
    # The domain moved after the build (set by hand, or rediscovered after a clear): the held
    # sheet's fields still come from example.org and must not publish under newsite.org.
    from waive.atlas.pipeline import confirm_domain

    engine = make_engine_with_hospital(UNSURE_HOSPITAL)
    with session_scope(engine) as session:
        assert build_hospital(session, FakeGateway(), FakeAI(), "220031", TODAY).outcome == "held"
        row = repo.get_hospital(session, "220031")
        row.website_domain, row.domain_confidence = "newsite.org", 0.6
        result = confirm_domain(session, "220031", "newsite.org")
        assert (result.outcome, result.version) == ("held", 1)
        assert result.notes == [
            "website newsite.org confirmed",
            "the sheet was built from example.org, not newsite.org; it stays held until a build "
            "scouts it: uv run waive atlas build --state MA --ccn 220031",
        ]
        sheet, _ = repo.latest_sheet(session, "220031")
        assert sheet.status is SheetStatus.HELD and sheet.eligibility.free_care_max_fpl.value == 250
        assert repo.get_hospital(session, "220031").domain_confidence == 1.0
        kinds = {item.kind for item in repo.open_review_items(session, "220031")}
        assert "domain_unconfirmed" not in kinds


def test_a_second_confirmation_read_before_the_first_was_saved_changes_nothing(tmp_path):
    # A double click: both requests read the hospital before either saved. The row is locked and
    # read again first (FOR UPDATE on PostgreSQL, where a second "version 2" would break the
    # (ccn, version) constraint; SQLite has one writer at a time).
    from waive.atlas.pipeline import confirm_domain

    engine = make_engine(f"sqlite:///{tmp_path / 'waive.db'}")
    init_db(engine)
    with session_scope(engine) as session:
        repo.upsert_hospital(session, UNSURE_HOSPITAL)
        assert build_hospital(session, FakeGateway(), FakeAI(), "220031", TODAY).outcome == "held"
    with session_scope(engine) as second:
        assert repo.get_hospital(second, "220031").domain_confidence == 0.6
        with session_scope(engine) as first:
            assert confirm_domain(first, "220031", "example.org").version == 2
        result = confirm_domain(second, "220031", "example.org")
        assert (result.outcome, result.version) == ("published", 2)
    with session_scope(engine) as session:
        assert [row.version for row in repo.sheet_versions(session, "220031")] == [2, 1]
        assert len(repo.review_items_of_kind(session, "220031", "website_confirmed")) == 1
    engine.dispose()


def test_discovery_never_brings_back_a_website_an_admin_rejected():
    # 7.11a left three hospitals paused because rediscovery would likely find the same wrong site.
    from waive.atlas.pipeline import reject_domain

    engine = make_engine_with_hospital(REAL_HOSPITAL)
    with session_scope(engine) as session:
        assert build_hospital(session, GuessGateway(), FakeAI(), "220031", TODAY).outcome == "held"
        reject_domain(session, "220031", "example.org", "another hospital's site")
        # The same search again: its only hit is the rejected site.
        result = build_hospital(session, GuessGateway(), FakeAI(), "220031", TODAY)
        assert (result.outcome, result.notes) == ("skipped", ["no official domain found"])
        assert repo.get_hospital(session, "220031").website_domain is None


# Decision 7.9a-2 (task 7.14): documents that state only the income up to which any help is
# given (Baptist Hospital FL and two AHN hospitals, 7.11d), not which share is free or discounted.
CEILING_SENTENCE = (
    "Patients with household income up to 400% of the Federal Poverty Guidelines may qualify for "
    "financial assistance."
)
# Help under the ceiling, read as a lower ceiling by a careless model.
UNINSURED_SENTENCE = (
    "Uninsured patients with household income up to 300% of the Federal Poverty Guidelines "
    "qualify for a discount."
)
# Another program's limit: never this hospital's ceiling (review of 7.14).
STATE_SENTENCE = (
    "Households up to 300% of the Federal Poverty Guidelines may also apply to the state's Health "
    "Safety Net."
)
CEILING_TEXT = (
    "St. Example Medical Center, Boston, MA 02118. Financial Assistance Summary.\n"
    f"{CEILING_SENTENCE}\n"
    "The hospital decides the amount of assistance after it reviews each application.\n"
    f"{UNINSURED_SENTENCE}\n"
    f"{STATE_SENTENCE}\n"
    "Questions: call 617-555-0100.\n"
)
CEILING_PATH = "eligibility.assistance_ceiling_fpl"
CEILING_NOTE = (
    "eligibility.free_care_max_fpl: not stated; the documents give only an overall assistance "
    "ceiling (400% FPL)"
)


class CeilingGateway(FakeGateway):
    def extract(self, urls, **kwargs):
        return [ExtractedPage(url, CEILING_TEXT) for url in urls]


class CeilingOnlyAI(FakeAI):
    """No model finds a free-care band or a table; each fills assistance_ceiling_fpl, set per
    role like FakeAI's free-care limit: a value (cited with the ceiling sentence), a (value,
    quote) pair, or None. The quote is cited from the document that contains it."""

    def __init__(self, reason="400", fast="400", tiebreak="400"):
        super().__init__(free_limit_for_fast=None, free_limit_for_tiebreak=None)
        self.free_limits["reason"] = None
        self.ceilings = {"reason": reason, "fast": fast, "tiebreak": tiebreak}

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        draft = super().complete_json(role, messages, schema, phi=phi, purpose=purpose)
        ceiling = self.ceilings[role]
        if ceiling is not None:
            value, quote = ceiling if isinstance(ceiling, tuple) else (ceiling, CEILING_SENTENCE)
            blocks = messages[1]["content"].split("=== SOURCE id=")[1:]
            cited = next((b for b in blocks if quote in b), blocks[0]).split(" ")[0]
            draft.assistance_ceiling_fpl = DraftField(value=value, quote=quote, source_id=cited)
        return draft


def test_a_ceiling_with_no_band_or_table_publishes_and_the_rules_answer_with_it():
    from decimal import Decimal

    from waive.rules.eligibility import Household, Tier, evaluate_eligibility

    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(session, CeilingGateway(), CeilingOnlyAI(), "229999", TODAY)
        assert (result.outcome, result.version) == ("published", 1)
        assert CEILING_NOTE in result.notes
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.status is SheetStatus.PUBLISHED
        ceiling = sheet.eligibility.assistance_ceiling_fpl
        assert (ceiling.value, ceiling.quote) == (400, CEILING_SENTENCE)
        assert sheet.eligibility.free_care_max_fpl is None
        assert sheet.eligibility.discount_tiers is None
        texts = {source.id: text for source, text in repo.sources_for(session, "229999")}
        assert verify_sheet(sheet, texts).ok
        kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
        assert kinds == ["verification"]  # the note above and the unverifiable hours
        # 300% of the poverty line for one person: help exists, the amount is the hospital's.
        answer = evaluate_eligibility(sheet, Household(1, Decimal("47880"), "MA"))
        assert (answer.tier, answer.discount_percent) == (Tier.ASSISTANCE, None)
        assert answer.reasons[0].field_path == CEILING_PATH


def test_a_ceiling_whose_quote_never_names_the_poverty_level_is_dropped_and_the_sheet_held():
    untied = "Financial assistance is available for household incomes up to 400%."

    class UntiedGateway(FakeGateway):
        def extract(self, urls, **kwargs):
            text = CEILING_TEXT.replace(CEILING_SENTENCE, untied)
            return [ExtractedPage(url, text) for url in urls]

    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        ai = CeilingOnlyAI(reason=("400", untied), fast=("400", untied))
        result = build_hospital(session, UntiedGateway(), ai, "229999", TODAY)
        assert result.outcome == "held"
        assert (
            f"{CEILING_PATH}: quote does not state the ceiling as a percentage of the poverty level"
        ) in result.notes
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.status is SheetStatus.HELD
        assert sheet.eligibility.assistance_ceiling_fpl is None
        assert sheet.eligibility.free_care_max_fpl is None


def test_a_ceiling_only_sheet_waits_on_the_website_as_before_and_publishes_once_confirmed():
    from waive.atlas.pipeline import confirm_domain

    engine = make_engine_with_hospital(UNSURE_HOSPITAL)
    with session_scope(engine) as session:
        result = build_hospital(session, CeilingGateway(), CeilingOnlyAI(), "220031", TODAY)
        assert (result.outcome, result.version) == ("held", 1)
        assert UNCONFIRMED_NOTE in result.notes
        sheet, _ = repo.latest_sheet(session, "220031")
        assert sheet.status is SheetStatus.HELD
        assert sheet.eligibility.assistance_ceiling_fpl.value == 400  # kept for the admin
        result = confirm_domain(session, "220031", "example.org")
        assert (result.outcome, result.version) == ("published", 2)
        assert result.notes == ["website example.org confirmed"]
        assert repo.latest_sheet(session, "220031")[0].status is SheetStatus.PUBLISHED


def test_a_ceiling_only_sheet_is_held_while_a_document_states_a_free_care_limit():
    # The sheet would say the documents name no free-care band; the newer policy names one.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        first = build_hospital(session, CeilingGateway(), CeilingOnlyAI(), "229999", TODAY)
        assert first.outcome == "published"
        repo.save_source(session, NEWER_POLICY, NEWER_POLICY_TEXT, "229999")
        result = build_hospital(
            session, CeilingGateway(), CeilingOnlyAI(), "229999", TODAY, reuse_sources=True
        )
        assert (result.outcome, result.version) == ("held", 2)
        assert (
            "the hospital's documents state a free-care band or a higher limit than the "
            "assistance ceiling (fap-newer-pdf: 200%)"
        ) in result.notes
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.status is SheetStatus.HELD
        ceiling = sheet.eligibility.assistance_ceiling_fpl
        assert ceiling.value == 400  # kept for the admin
        detail = review_detail(session, "229999", "document_conflict")
        assert (detail["path"], detail["published"]) == (CEILING_PATH, "400")
        assert detail["source_id"] == ceiling.source_id
        assert detail["documents"] == [{"id": "fap-newer-pdf", "limits": ["200"]}]


def test_two_models_disagreeing_on_the_ceiling_hold_the_sheet_like_any_critical_field():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        ai = CeilingOnlyAI(fast=("300", UNINSURED_SENTENCE), tiebreak="999")
        result = build_hospital(session, CeilingGateway(), ai, "229999", TODAY)
        assert result.outcome == "held"
        assert f"critical fields disagree: {CEILING_PATH}" in result.notes
        assert review_detail(session, "229999", "tiebreak")[CEILING_PATH] == {
            "primary": "400",
            "secondary": "300",
            "tiebreak": "999",
            "verdict": "unsettled",
        }
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.status is SheetStatus.HELD
        assert sheet.eligibility.assistance_ceiling_fpl.value == 400  # the primary's, for review
    # The third model siding with the primary settles it, and the sheet publishes.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        ai = CeilingOnlyAI(fast=("300", UNINSURED_SENTENCE), tiebreak="400")
        result = build_hospital(session, CeilingGateway(), ai, "229999", TODAY)
        assert result.outcome == "published"
        assert f"tie-break settled {CEILING_PATH} (primary)" in result.notes


def test_another_programs_limit_is_never_the_hospitals_ceiling():
    # Review of 7.14: both models cite the state program's 300% as the ceiling.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        ai = CeilingOnlyAI(reason=("300", STATE_SENTENCE), fast=("300", STATE_SENTENCE))
        result = build_hospital(session, CeilingGateway(), ai, "229999", TODAY)
        assert result.outcome == "held"
        assert any(note.startswith(f"{CEILING_PATH}: quote ") for note in result.notes)
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.assistance_ceiling_fpl is None


# Review of 7.14: the first band of a sliding scale, worded like a ceiling.
FIRST_BAND = (
    "Patients with household income at or below 200% of the Federal Poverty Level are eligible "
    "for financial assistance."
)
SLIDING_BAND = (
    "Patients with household income between 201% and 400% of the Federal Poverty Level receive a "
    "sliding scale discount."
)
BANDS_TEXT = (
    "St. Example Medical Center, Boston, MA 02118. Financial Assistance Summary.\n"
    f"{FIRST_BAND}\n{SLIDING_BAND}\nQuestions: call 617-555-0100.\n"
)


class BandsGateway(FakeGateway):
    def extract(self, urls, **kwargs):
        return [ExtractedPage(url, BANDS_TEXT) for url in urls]


def test_the_first_band_of_a_sliding_scale_never_publishes_as_the_ceiling():
    # Filed as free care by every model: set aside, and not made the ceiling on its own.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        ai = FakeAI(free_limit_for_fast=("200", FIRST_BAND), free_limit_for_tiebreak=None)
        ai.free_limits["reason"] = ("200", FIRST_BAND)
        result = build_hospital(session, BandsGateway(), ai, "229999", TODAY)
        assert result.outcome == "held"
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.assistance_ceiling_fpl is None
        assert sheet.eligibility.free_care_max_fpl is None
    # Filed as the ceiling: the sliding scale above it holds the sheet for an admin.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        ai = CeilingOnlyAI(reason=("200", FIRST_BAND), fast=("200", FIRST_BAND))
        result = build_hospital(session, BandsGateway(), ai, "229999", TODAY)
        assert result.outcome == "held"
        assert (
            "the hospital's documents state a free-care band or a higher limit than the "
            "assistance ceiling"
        ) in " ".join(result.notes)
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.assistance_ceiling_fpl.value == 200  # kept for the admin


FREE_PLAIN = (
    "Patients with household income at or below 200% of the Federal Poverty Guidelines are not "
    "billed for services."
)


class FreePlainGateway(FakeGateway):
    def extract(self, urls, **kwargs):
        return [ExtractedPage(url, CEILING_TEXT + FREE_PLAIN + "\n") for url in urls]


class FreeBandFastAI(CeilingOnlyAI):
    """The reason and tie-break models read only the ceiling; the fast model reads the free band
    written in plain words instead."""

    def __init__(self, tiebreak="400"):
        super().__init__(fast=None, tiebreak=tiebreak)

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        draft = super().complete_json(role, messages, schema, phi=phi, purpose=purpose)
        if role == "fast":
            source_id = messages[1]["content"].split("=== SOURCE id=")[1].split(" ")[0]
            draft.free_care_max_fpl = DraftField(value="200", quote=FREE_PLAIN, source_id=source_id)
        return draft


def test_a_ceiling_only_reading_against_a_verified_free_band_holds_the_sheet():
    # Review of 7.14: the cross-check found a free band; ceiling-only must not publish past it.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(session, FreePlainGateway(), FreeBandFastAI("200"), "229999", TODAY)
        assert result.outcome == "held"
        assert f"critical fields disagree: {CEILING_PATH}" in result.notes
        detail = review_detail(session, "229999", "tiebreak")[CEILING_PATH]
        assert (detail["primary"], detail["secondary"], detail["verdict"]) == (
            "400",
            None,
            "unsettled",
        )
    # A third model siding with the ceiling settles the dispute, and the free band in plain words
    # still holds the sheet as a document conflict.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(session, FreePlainGateway(), FreeBandFastAI(), "229999", TODAY)
        assert result.outcome == "held"
        assert f"tie-break settled {CEILING_PATH} (primary)" in result.notes
        assert any(
            note.startswith("the hospital's documents state a free-care band or a higher limit")
            for note in result.notes
        )


class TieredAI(FakeAI):
    """The sample policy read whole: the free-care band, the 60% table, and with `ceiling` the
    400% top of that table filed as the overall ceiling too."""

    def __init__(self, ceiling):
        super().__init__()
        self.ceiling = ceiling

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        draft = super().complete_json(role, messages, schema, phi=phi, purpose=purpose)
        source_id = draft.free_care_max_fpl.source_id
        draft.discount_tiers = DraftField(
            value=[{"min_fpl_exclusive": 250, "max_fpl_inclusive": 400, "discount_percent": 60}],
            quote=DISCOUNT_QUOTE,
            source_id=source_id,
        )
        if self.ceiling:
            draft.assistance_ceiling_fpl = DraftField(
                value="400", quote=DISCOUNT_QUOTE, source_id=source_id
            )
        return draft


def test_a_sheet_with_a_band_and_a_table_is_unchanged_by_a_ceiling():
    built = {}
    for ceiling in (False, True):
        engine = make_engine_with_hospital()
        with session_scope(engine) as session:
            result = build_hospital(session, FakeGateway(), TieredAI(ceiling), "229999", TODAY)
            sheet, _ = repo.latest_sheet(session, "229999")
            kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
            built[ceiling] = (result.outcome, result.version, result.notes, kinds, sheet)
    assert built[True] == built[False]
    outcome, _, notes, _, sheet = built[True]
    assert outcome == "published" and sheet.status is SheetStatus.PUBLISHED
    assert sheet.eligibility.assistance_ceiling_fpl is None
    assert sheet.eligibility.free_care_max_fpl.value == 250
    assert len(sheet.eligibility.discount_tiers.value) == 1
    assert not any("ceiling" in note for note in notes)


# --- the owner's rulings kept on a rebuild (task 7.12c) ------------------------------------

RULING_NOTE = (
    "eligibility.free_care_max_fpl: owner review says 250% is a discount or assistance limit, "
    "not free care; recorded as assistance_ceiling_fpl"
)
RULED_AT = "2026-10-23T17:31:54.394Z"
RULED_LATER_AT = "2026-10-24T09:00:00.000Z"


def owner_ruling(decision, value="250", ccn="229999", at=RULED_AT):
    """One entry of data/atlas-eve/_decisions.json, as the review page writes it."""
    return {
        "ccn": ccn,
        "decision": decision,
        "value": value,
        "note": "",
        "at": at,
        "hospital": "St. Example Medical Center",
        "state": "MA",
        "bucket": "hedged",
    }


def test_a_discount_ruling_keeps_the_ruled_out_free_care_value_off_the_rebuilt_sheet():
    # FakeAI reads 250 as free care; the owner ruled that 250 is a discount limit, not free care.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        ruling = owner_ruling("discount")
        result = build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY, decision=ruling)
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.free_care_max_fpl is None
        # The reading is kept, as the cited ceiling it is, and verified as one: the only field the
        # verifier rejects is the unrelated unverifiable "hours". (The sample policy does say
        # "free care" at 250%, so a document conflict holds this ceiling-only sheet for an admin.)
        ceiling = sheet.eligibility.assistance_ceiling_fpl
        assert (ceiling.value, ceiling.quote) == (250, FREE_CARE_QUOTE)
        rejected = review_detail(session, "229999", "verification")["rejected"]
        assert rejected == [["contacts.hours", "quote not found in source"]]
        assert RULING_NOTE in result.notes
        assert review_detail(session, "229999", "owner_decision") == {
            "decision": "discount",
            "value": "250",
            "kept_as": "assistance_ceiling_fpl",
            "decided_at": RULED_AT,
        }
        assert [item.kind for item in repo.open_review_items(session, "229999")].count(
            "owner_decision"
        ) == 1
        # What the model read besides it is judged as before.
        assert sheet.contacts.phone.value == "617-555-0100" and sheet.contacts.hours is None


@pytest.mark.parametrize(
    "ruling",
    [None, owner_ruling("accept"), owner_ruling("discount", "300")],
    ids=["no ruling", "accepted as free care", "ruled on another value"],
)
def test_a_build_the_ruling_does_not_touch_keeps_the_free_care_value(ruling):
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY, decision=ruling)
        assert (result.outcome, result.version) == ("published", 1)
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.free_care_max_fpl.value == 250
        assert sheet.eligibility.assistance_ceiling_fpl is None
        assert not any("owner review" in note for note in result.notes)
        kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
        assert kinds == ["verification"]


def test_an_unsure_ruling_drops_the_value_and_files_it_as_dropped():
    # The owner could not tell whether 250 is free care: nothing is recorded in its place, and
    # with no income rule left the sheet waits for an admin.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(
            session, FakeGateway(), FakeAI(), "229999", TODAY, decision=owner_ruling("unsure")
        )
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.free_care_max_fpl is None
        assert sheet.eligibility.assistance_ceiling_fpl is None
        assert result.outcome == "held"
        assert (
            "eligibility.free_care_max_fpl: owner review could not tell whether 250% is free "
            "care; not recorded"
        ) in result.notes
        assert review_detail(session, "229999", "owner_decision") == {
            "decision": "unsure",
            "value": "250",
            "kept_as": "dropped",
            "decided_at": RULED_AT,
        }


def test_the_cross_check_read_is_held_to_the_ruling_too():
    # Both models read the ruled-out 250. Held to the ruling, both give the same ceiling and do
    # not dispute it; the fast model's free-care reading would otherwise contradict the ceiling
    # and send the build to the tie-break model for nothing.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(
            session, FakeGateway(), FakeAI(), "229999", TODAY, decision=owner_ruling("discount")
        )
        kinds = {item.kind for item in repo.open_review_items(session, "229999")}
        assert "conflict" not in kinds and "tiebreak" not in kinds
        assert not any(note.startswith("critical fields disagree") for note in result.notes)


def test_the_tie_break_read_is_held_to_the_ruling_too():
    # The primary read is the ruled-out 250 (kept as the ceiling), the cross-check reads a free
    # band of 400 and disputes it, and the tie-break model reads 250 again: held to the ruling,
    # that is the primary's ceiling, so the primary stands.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        ai = FakeAI(free_limit_for_fast=("400", DISCOUNT_QUOTE), free_limit_for_tiebreak="250")
        result = build_hospital(
            session, FakeGateway(), ai, "229999", TODAY, decision=owner_ruling("discount")
        )
        detail = review_detail(session, "229999", "tiebreak")["eligibility.assistance_ceiling_fpl"]
        assert detail == {
            "primary": "250",
            "secondary": None,
            "tiebreak": "250",
            "verdict": "primary",
        }
        assert "tie-break settled eligibility.assistance_ceiling_fpl (primary)" in result.notes
        assert not any(note.startswith("critical fields disagree") for note in result.notes)
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.free_care_max_fpl is None
        assert sheet.eligibility.assistance_ceiling_fpl.value == 250


def test_a_ceiling_the_ruling_moved_is_still_verified_like_any_other_field():
    # 300 is quoted from a sentence about the Health Safety Net: accepted as a free-care reading
    # (no ruling, first build), never as the hospital's own assistance ceiling. The ruling moves
    # the value; it does not excuse it from the ceiling rules, so nothing unverified reaches the
    # sheet.
    def ai():
        reads_300 = FakeAI(free_limit_for_fast="300")
        reads_300.free_limits["reason"] = "300"
        return reads_300

    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        build_hospital(session, FakeGateway(), ai(), "229999", TODAY)
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.free_care_max_fpl.value == 300
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        ruling = owner_ruling("discount", "300")
        result = build_hospital(session, FakeGateway(), ai(), "229999", TODAY, decision=ruling)
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.free_care_max_fpl is None
        assert sheet.eligibility.assistance_ceiling_fpl is None
        assert result.outcome == "held"
        rejected = review_detail(session, "229999", "verification")["rejected"]
        assert [CEILING_PATH, CEILING_NOT_ASSISTANCE_REASON] in rejected
        assert review_detail(session, "229999", "owner_decision")["kept_as"] == (
            "assistance_ceiling_fpl"
        )


def test_build_state_hands_each_hospital_its_own_ruling():
    second = {**REAL_HOSPITAL, "ccn": "220010", "name": "NEW HOSPITAL"}
    engine = make_engine_with_hospital(REAL_HOSPITAL, second)
    decisions = {"220031": owner_ruling("discount", ccn="220031")}
    with session_scope(engine) as session:
        results = build_state(session, FakeGateway(), FakeAI(), "MA", TODAY, decisions=decisions)
        assert sorted(r.ccn for r in results) == ["220010", "220031"]
        ruled, _ = repo.latest_sheet(session, "220031")
        other, _ = repo.latest_sheet(session, "220010")
        assert ruled.eligibility.free_care_max_fpl is None
        assert ruled.eligibility.assistance_ceiling_fpl.value == 250
        assert other.eligibility.free_care_max_fpl.value == 250
        assert [item.ccn for item in repo.open_review_items(session, kind="owner_decision")] == [
            "220031"
        ]


# The rulings are also stored with the hospitals (`import-eve --db` records them), because the
# paths that rebuild a sheet other than `waive atlas build` pass no decision of their own: the
# admin console's Rebuild button, `waive learn rebuild`, the refresh and the scheduler, on a
# server that has no decisions file.


def test_a_stored_ruling_holds_a_build_that_is_given_no_decision():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        assert record_owner_rulings(session, {"229999": owner_ruling("discount")}) == 1
        result = build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY)
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.free_care_max_fpl is None
        ceiling = sheet.eligibility.assistance_ceiling_fpl
        assert (ceiling.value, ceiling.quote) == (250, FREE_CARE_QUOTE)
        assert RULING_NOTE in result.notes
        assert review_detail(session, "229999", "owner_decision") == {
            "decision": "discount",
            "value": "250",
            "kept_as": "assistance_ceiling_fpl",
            "decided_at": RULED_AT,
        }


def test_a_decision_passed_in_wins_over_the_stored_ruling_that_would_apply():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        record_owner_rulings(session, {"229999": owner_ruling("discount")})
        accepted = owner_ruling("accept", at=RULED_LATER_AT)
        result = build_hospital(
            session, FakeGateway(), FakeAI(), "229999", TODAY, decision=accepted
        )
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.free_care_max_fpl.value == 250
        assert sheet.eligibility.assistance_ceiling_fpl is None
        assert not any("owner review" in note for note in result.notes)
        assert "owner_decision" not in {i.kind for i in repo.open_review_items(session, "229999")}


def test_a_decision_passed_in_is_the_one_a_build_records_over_a_stored_one():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        record_owner_rulings(session, {"229999": owner_ruling("accept")})
        later = owner_ruling("discount", at=RULED_LATER_AT)
        build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY, decision=later)
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.free_care_max_fpl is None
        detail = review_detail(session, "229999", "owner_decision")
        assert (detail["decision"], detail["decided_at"]) == ("discount", RULED_LATER_AT)


def test_the_latest_stored_ruling_is_the_one_a_build_uses():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        # The owner ruled 250 a discount, then changed their mind and accepted it as free care.
        record_owner_rulings(session, {"229999": owner_ruling("discount")})
        record_owner_rulings(session, {"229999": owner_ruling("accept", at=RULED_LATER_AT)})
        result = build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY)
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.free_care_max_fpl.value == 250
        assert not any("owner review" in note for note in result.notes)


def test_build_state_gives_a_hospital_the_file_does_not_name_its_stored_ruling():
    second = {**REAL_HOSPITAL, "ccn": "220010", "name": "NEW HOSPITAL"}
    engine = make_engine_with_hospital(REAL_HOSPITAL, second)
    with session_scope(engine) as session:
        record_owner_rulings(session, {"220031": owner_ruling("discount", ccn="220031")})
        # The decisions file names the other hospital only; each one gets what is meant for it.
        file_says = {"220010": owner_ruling("accept", ccn="220010")}
        results = build_state(session, FakeGateway(), FakeAI(), "MA", TODAY, decisions=file_says)
        assert sorted(r.ccn for r in results) == ["220010", "220031"]
        ruled, _ = repo.latest_sheet(session, "220031")
        other, _ = repo.latest_sheet(session, "220010")
        assert ruled.eligibility.free_care_max_fpl is None
        assert ruled.eligibility.assistance_ceiling_fpl.value == 250
        assert other.eligibility.free_care_max_fpl.value == 250
        assert [i.ccn for i in repo.open_review_items(session, kind="owner_decision")] == ["220031"]


# Owner rulings win over the ceiling readings (review decision 2026-10-23): a ceiling a
# `discount` ruling made is neither rejected as the free-care band nor held below the sheet's own
# rules, and each conflict goes to the owner as an open `owner_ruling_conflict` item.
FREE_BAND_TOO_LOW = "eligibility.assistance_ceiling_fpl: 70% is below the sheet's own income rules"


class RussellAI(FakeAI):
    """Russell Regional (171350): free care read from the 100%-discount line at 70%, a table from
    70% to 200%."""

    def __init__(self):
        super().__init__(
            free_limit_for_fast=("70", RUSSELL_FREE_LINE),
            free_limit_for_tiebreak=("70", RUSSELL_FREE_LINE),
        )
        self.free_limits["reason"] = ("70", RUSSELL_FREE_LINE)

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        draft = super().complete_json(role, messages, schema, phi=phi, purpose=purpose)
        draft.discount_tiers = DraftField(
            value=[{"min_fpl_exclusive": 70, "max_fpl_inclusive": 200, "discount_percent": 60}],
            quote=RUSSELL_TABLE,
            source_id=draft.free_care_max_fpl.source_id,
        )
        return draft


def ruling_conflicts(session, ccn):
    return [item.detail for item in repo.open_review_items(session, ccn, "owner_ruling_conflict")]


def ruling_conflict(reason, value, quote):
    return {
        "path": CEILING_PATH,
        "reason": reason,
        "ruled_value": value,
        "quote": quote,
        "decided_at": RULED_AT,
    }


def test_an_owner_ruled_ceiling_from_the_free_care_line_publishes_and_the_conflict_goes_to_the_owner():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        ruling = owner_ruling("discount", "70")
        result = build_hospital(
            session, RussellGateway(), RussellAI(), "229999", TODAY, decision=ruling
        )
        assert result.outcome == "published"
        sheet, _ = repo.latest_sheet(session, "229999")
        ceiling = sheet.eligibility.assistance_ceiling_fpl
        assert (ceiling.value, ceiling.quote) == (70, RUSSELL_FREE_LINE)
        assert [tier.discount_percent for tier in sheet.eligibility.discount_tiers.value] == [60]
        assert ruling_conflicts(session, "229999") == [
            ruling_conflict(CEILING_FREE_BAND_REASON, "70", RUSSELL_FREE_LINE),
            ruling_conflict(FREE_BAND_TOO_LOW, "70", RUSSELL_FREE_LINE),
        ]
        # The rejection is not recorded as one, so nothing later reads it as a hold.
        rejected = review_detail(session, "229999", "verification")["rejected"]
        assert [CEILING_PATH, CEILING_FREE_BAND_REASON] not in rejected
    # The same reading with no ruling never makes a ceiling: the build keeps free care at 70.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        build_hospital(session, RussellGateway(), RussellAI(), "229999", TODAY)
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.free_care_max_fpl.value == 70
        assert sheet.eligibility.assistance_ceiling_fpl is None
        assert ruling_conflicts(session, "229999") == []


def test_withdraw_keeps_an_owner_ruled_ceiling_from_the_free_care_line_and_files_the_conflict_once():
    # The same stored sheet with no ruling is held (the withdraw test above).
    from waive.atlas.pipeline import recheck_sheets, withdraw_rejected
    from waive.atlas.schema import Cited

    engine = make_engine_with_hospital(REAL_HOSPITAL)
    with session_scope(engine) as session:
        build_hospital(session, RussellGateway(), FakeAI(), "220031", TODAY)
        sheet, _ = repo.latest_sheet(session, "220031")
        ceiling = Cited(
            value=Decimal(70),
            quote=RUSSELL_FREE_LINE,
            source_id=sheet.eligibility.free_care_max_fpl.source_id,
            checked_on=TODAY,
        )
        stale = sheet.model_copy(
            update={
                "eligibility": sheet.eligibility.model_copy(
                    update={"assistance_ceiling_fpl": ceiling}
                ),
                "version": 2,
            }
        )
        repo.add_sheet_version(
            session, stale, {"eligibility.assistance_ceiling_fpl": {"old": None}}
        )
        record_owner_rulings(session, {"220031": owner_ruling("discount", "70", ccn="220031")})
        assert recheck_sheets(session, "MA") == []
        # Nothing is withdrawn; what went to the owner is reported.
        [result] = withdraw_rejected(session, "MA")
        assert (result.outcome, result.version) == ("skipped", 2)
        assert sum("recorded for the owner" in note for note in result.notes) == 2
        latest, _ = repo.latest_sheet(session, "220031")
        assert (latest.version, latest.status) == (2, SheetStatus.PUBLISHED)
        assert latest.eligibility.assistance_ceiling_fpl.value == 70
        assert ruling_conflicts(session, "220031") == [
            ruling_conflict(CEILING_FREE_BAND_REASON, "70", RUSSELL_FREE_LINE),
            ruling_conflict(FREE_BAND_TOO_LOW, "70", RUSSELL_FREE_LINE),
        ]
        assert withdraw_rejected(session, "MA") == []
        assert len(ruling_conflicts(session, "220031")) == 2  # filed once, not on every recheck
        # Settled by the owner, a conflict is not filed again; a new ruling is a new conflict.
        for item in repo.open_review_items(session, "220031", "owner_ruling_conflict"):
            item.status = "resolved"
        session.flush()
        assert withdraw_rejected(session, "MA") == []
        assert ruling_conflicts(session, "220031") == []
        later = owner_ruling("discount", "70", ccn="220031", at=RULED_LATER_AT)
        record_owner_rulings(session, {"220031": later})
        [result] = withdraw_rejected(session, "MA")
        assert result.outcome == "skipped"
        assert {conflict["decided_at"] for conflict in ruling_conflicts(session, "220031")} == {
            RULED_LATER_AT
        }
        assert len(ruling_conflicts(session, "220031")) == 2


# Baptist Health (180104, 180130 and the others ruled "discount 300"): the ruling makes the 300%
# "full assistance" line the ceiling, next to the 301-400% partial-assistance band.
BAPTIST_FULL = (
    "If your family income is less than or equal to 300% of the federal poverty level, then you "
    "may be eligible for full assistance."
)
BAPTIST_PARTIAL = (
    "If your family income is between 301% and 400% of the federal poverty level, then you may "
    "be eligible for partial assistance (50%)."
)
CEILING_UNDER_THE_TABLE = (
    "eligibility.assistance_ceiling_fpl: 300% is below the sheet's own income rules"
)


class BaptistGateway(FakeGateway):
    def extract(self, urls, **kwargs):
        text = POLICY_TEXT + BAPTIST_FULL + "\n" + BAPTIST_PARTIAL + "\n"
        return [ExtractedPage(url, text) for url in urls]


class BaptistAI(FakeAI):
    def __init__(self):
        super().__init__(
            free_limit_for_fast=("300", BAPTIST_FULL),
            free_limit_for_tiebreak=("300", BAPTIST_FULL),
        )
        self.free_limits["reason"] = ("300", BAPTIST_FULL)

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        draft = super().complete_json(role, messages, schema, phi=phi, purpose=purpose)
        draft.discount_tiers = DraftField(
            value=[{"min_fpl_exclusive": 300, "max_fpl_inclusive": 400, "discount_percent": 50}],
            quote=BAPTIST_PARTIAL,
            source_id=draft.free_care_max_fpl.source_id,
        )
        return draft


def test_an_owner_ruled_ceiling_under_the_table_publishes_and_the_conflict_goes_to_the_owner():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        ruling = owner_ruling("discount", "300")
        result = build_hospital(
            session, BaptistGateway(), BaptistAI(), "229999", TODAY, decision=ruling
        )
        assert result.outcome == "published"
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.assistance_ceiling_fpl.value == 300
        assert sheet.eligibility.free_care_max_fpl is None
        assert ruling_conflicts(session, "229999") == [
            ruling_conflict(CEILING_UNDER_THE_TABLE, "300", BAPTIST_FULL)
        ]
        kinds = {item.kind for item in repo.open_review_items(session, "229999")}
        assert "inconsistent" not in kinds


def test_confirming_the_website_holds_a_ceiling_under_the_table_unless_the_owner_ruled_it():
    from waive.atlas.pipeline import confirm_domain

    def stored_with_ceiling(session):
        build_hospital(session, BaptistGateway(), BaptistAI(), "220031", TODAY)
        sheet, _ = repo.latest_sheet(session, "220031")
        eligibility = sheet.eligibility.model_copy(
            update={
                "free_care_max_fpl": None,
                "assistance_ceiling_fpl": sheet.eligibility.free_care_max_fpl,
            }
        )
        stale = sheet.model_copy(update={"eligibility": eligibility, "version": 2})
        repo.add_sheet_version(session, stale, {"eligibility.assistance_ceiling_fpl": {}})

    engine = make_engine_with_hospital(UNSURE_HOSPITAL)
    with session_scope(engine) as session:
        stored_with_ceiling(session)
        result = confirm_domain(session, "220031", "example.org")
        assert result.outcome == "held"
        assert CEILING_UNDER_THE_TABLE in result.notes
        # The reason is recorded, not only noted, as a build and a withdraw record it.
        assert review_detail(session, "220031", "inconsistent") == {
            "problems": [CEILING_UNDER_THE_TABLE]
        }
        assert ruling_conflicts(session, "220031") == []
    engine = make_engine_with_hospital(UNSURE_HOSPITAL)
    with session_scope(engine) as session:
        stored_with_ceiling(session)
        record_owner_rulings(session, {"220031": owner_ruling("discount", "300", ccn="220031")})
        result = confirm_domain(session, "220031", "example.org")
        assert (result.outcome, result.version) == ("published", 3)
        assert "inconsistent" not in {i.kind for i in repo.open_review_items(session, "220031")}
        latest, _ = repo.latest_sheet(session, "220031")
        assert latest.eligibility.assistance_ceiling_fpl.value == 300
        assert ruling_conflicts(session, "220031") == [
            ruling_conflict(CEILING_UNDER_THE_TABLE, "300", BAPTIST_FULL)
        ]


def store_with(session, ccn, **fields):
    """A new version of the latest sheet, its status kept, with these eligibility fields set."""
    sheet, _ = repo.latest_sheet(session, ccn)
    stale = sheet.model_copy(
        update={
            "eligibility": sheet.eligibility.model_copy(update=fields),
            "version": sheet.version + 1,
        }
    )
    repo.add_sheet_version(session, stale, {f"eligibility.{name}": {} for name in fields})
    return stale


def cited_from(sheet, value, quote):
    from waive.atlas.schema import Cited

    source_id = sheet.eligibility.free_care_max_fpl.source_id
    return Cited(value=value, quote=quote, source_id=source_id, checked_on=TODAY)


# Henry County (361309) published free care to 100% and bands from 200%, its 101-200% band
# missing. No field is rejected, so only the sheet-level check finds it.
HENRY_TABLE = (
    "Patients with household income above 300% and at or below 400% of the Federal Poverty "
    "Guidelines receive a 50% discount."
)


class HenryGateway(FakeGateway):
    def extract(self, urls, **kwargs):
        return [ExtractedPage(url, POLICY_TEXT + HENRY_TABLE + "\n") for url in urls]


def test_recheck_lists_and_withdraw_holds_a_published_sheet_with_a_gap_between_its_bands():
    from waive.atlas.pipeline import recheck_sheets, withdraw_rejected
    from waive.atlas.schema import DiscountTier

    engine = make_engine_with_hospital(REAL_HOSPITAL)
    with session_scope(engine) as session:
        assert build_hospital(session, HenryGateway(), FakeAI(), "220031", TODAY).outcome == (
            "published"
        )
        sheet, _ = repo.latest_sheet(session, "220031")
        tier = DiscountTier(min_fpl_exclusive=300, max_fpl_inclusive=400, discount_percent=50)
        store_with(session, "220031", discount_tiers=cited_from(sheet, [tier], HENRY_TABLE))
        gap = "eligibility.discount_tiers: no rule covers incomes from 250% to 300%"
        [entry] = recheck_sheets(session, "MA")
        assert entry.rejected == [("sheet", gap)]
        assert not entry.waits_on_website
        [result] = withdraw_rejected(session, "MA")
        assert (result.outcome, result.version) == ("held", 3)
        assert gap in result.notes
        assert review_detail(session, "220031", "inconsistent") == {"problems": [gap]}
        latest, _ = repo.latest_sheet(session, "220031")
        assert latest.status is SheetStatus.HELD
        assert latest.eligibility.discount_tiers is not None  # kept for the admin to compare
        assert recheck_sheets(session, "MA") == []  # held sheets are not listed again


# The ruling wins over the free-band reading only: the ruled ceiling quoted from UnityPoint's
# Illinois schedule, for a Massachusetts hospital, is still another state's band.
UPH_IL_FREE_LINE = (
    "An IL UPH Hospital shall provide a charitable discount of 100% of its charges to any "
    "Uninsured Patient with family income of not more than 70% of the federal poverty income "
    "guidelines."
)


class UnityPointLineGateway(FakeGateway):
    def extract(self, urls, **kwargs):
        return [ExtractedPage(url, POLICY_TEXT + UPH_IL_FREE_LINE + "\n") for url in urls]


def test_an_owner_ruled_ceiling_from_another_states_facilities_is_rejected_and_held():
    from waive.atlas.pipeline import BuildResult, _structure_and_verify, withdraw_rejected

    def reads_the_illinois_line():
        ai = FakeAI(
            free_limit_for_fast=("70", UPH_IL_FREE_LINE),
            free_limit_for_tiebreak=("70", UPH_IL_FREE_LINE),
        )
        ai.free_limits["reason"] = ("70", UPH_IL_FREE_LINE)
        return ai

    ruling = owner_ruling("discount", "70")
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(
            session, UnityPointLineGateway(), reads_the_illinois_line(), "229999", TODAY,
            decision=ruling,
        )  # fmt: skip
        assert result.outcome == "held"
        assert f"{CEILING_PATH}: {OTHER_STATE_FACILITY_REASON}" in result.notes
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.assistance_ceiling_fpl is None
        assert ruling_conflicts(session, "229999") == []  # the ruling kept nothing here
        row = repo.get_hospital(session, "229999")
        _sheet, holds = _structure_and_verify(
            session,
            reads_the_illinois_line(),
            repo.hospital_ref(row),
            repo.sources_for(session, "229999"),
            TODAY,
            BuildResult("229999", row.name, "skipped"),
            ruling,
        )
        assert holds == [CEILING_FREE_BAND_REASON, OTHER_STATE_FACILITY_REASON]
    engine = make_engine_with_hospital(REAL_HOSPITAL)
    with session_scope(engine) as session:
        build_hospital(session, UnityPointLineGateway(), FakeAI(), "220031", TODAY)
        sheet, _ = repo.latest_sheet(session, "220031")
        ceiling = cited_from(sheet, Decimal(70), UPH_IL_FREE_LINE)
        store_with(session, "220031", assistance_ceiling_fpl=ceiling)
        record_owner_rulings(session, {"220031": owner_ruling("discount", "70", ccn="220031")})
        [result] = withdraw_rejected(session, "MA")
        assert (result.outcome, result.version) == ("held", 3)
        assert f"{CEILING_PATH}: {OTHER_STATE_FACILITY_REASON}" in result.notes
        latest, _ = repo.latest_sheet(session, "220031")
        assert latest.eligibility.assistance_ceiling_fpl is None
        assert ruling_conflicts(session, "220031") == []


def test_withdraw_files_the_patient_share_table_it_holds_a_sheet_for():
    from waive.atlas.pipeline import withdraw_rejected
    from waive.atlas.schema import DiscountTier

    engine = make_engine_with_hospital(REAL_HOSPITAL)
    with session_scope(engine) as session:
        build_hospital(session, MercyGateway(), FakeAI(), "220031", TODAY)
        sheet, _ = repo.latest_sheet(session, "220031")
        tier = DiscountTier(min_fpl_exclusive=200, max_fpl_inclusive=250, discount_percent=15)
        store_with(session, "220031", discount_tiers=cited_from(sheet, [tier], MERCY_TABLE))
        [result] = withdraw_rejected(session, "MA")
        assert (result.outcome, result.version) == ("held", 3)
        assert f"eligibility.discount_tiers: {PATIENT_SHARE_REASON}" in result.notes
        assert review_detail(session, "220031", "patient_share_table") == {"quote": MERCY_TABLE}


# A ruled ceiling that another check overturns keeps nothing for the owner: it is held as the same
# reading with no ruling is (a Kansas hospital whose free line sits in a system policy's Illinois
# section).
ILLINOIS_FREE_LINE = (
    "For Illinois facilities: Patients with household income at or below 70% of the FPL will "
    "receive a 100% discount."
)
KANSAS_HOSPITAL = {
    **REAL_HOSPITAL,
    "state": "KS",
    "website_domain": "example.org",
    "domain_confidence": 1.0,  # confirmed: the Boston sample names no Kansas place
}


class IllinoisLineGateway(FakeGateway):
    def extract(self, urls, **kwargs):
        return [ExtractedPage(url, POLICY_TEXT + ILLINOIS_FREE_LINE + "\n") for url in urls]


def test_a_ruled_ceiling_another_check_overturns_is_held_as_without_the_ruling():
    from waive.atlas.pipeline import BuildResult, _structure_and_verify, withdraw_rejected

    for ruled in (False, True):
        engine = make_engine_with_hospital(KANSAS_HOSPITAL)
        with session_scope(engine) as session:
            assert (
                build_hospital(session, IllinoisLineGateway(), FakeAI(), "220031", TODAY).outcome
                == "published"
            )
            sheet, _ = repo.latest_sheet(session, "220031")
            ceiling = cited_from(sheet, Decimal(70), ILLINOIS_FREE_LINE)
            store_with(session, "220031", assistance_ceiling_fpl=ceiling)
            if ruled:
                ruling = owner_ruling("discount", "70", ccn="220031")
                record_owner_rulings(session, {"220031": ruling})
            [result] = withdraw_rejected(session, "KS")
            assert (result.outcome, result.version) == ("held", 3), ruled
            assert f"{CEILING_PATH}: {CEILING_FREE_BAND_REASON}" in result.notes
            assert (f"{CEILING_PATH}: {OTHER_STATE_REASON}" in result.notes) is ruled
            assert ruling_conflicts(session, "220031") == []
    # The build holds it for the free-band reading too, beside the other state's band.
    engine = make_engine_with_hospital(KANSAS_HOSPITAL)
    with session_scope(engine) as session:
        ai = FakeAI(free_limit_for_fast=("70", ILLINOIS_FREE_LINE))
        ai.free_limits["reason"] = ("70", ILLINOIS_FREE_LINE)
        ruling = owner_ruling("discount", "70", ccn="220031")
        build_hospital(session, IllinoisLineGateway(), ai, "220031", TODAY, decision=ruling)
        row = repo.get_hospital(session, "220031")
        _sheet, holds = _structure_and_verify(
            session,
            ai,
            repo.hospital_ref(row),
            repo.sources_for(session, "220031"),
            TODAY,
            BuildResult("220031", row.name, "skipped"),
            ruling,
        )
        assert holds == [CEILING_FREE_BAND_REASON]
        assert ruling_conflicts(session, "220031") == []


# Task 8 of the 2026-10-24 publication audit: documents that belong to another hospital or another
# state. The stand-ins below keep the shape of the stored documents the owner-scope diagnosis tested
# on (docs/superpowers/plans/2026-10-24-waive-audit-rules/owner-scope/): the covered-hospital list,
# the host, the links on the hospital's own pages and the words that name the hospital.


def hospital_entry(ccn, name, city, state, website_domain=None, **extra):
    """A registry hospital whose website an admin confirmed, so the place check stands down."""
    return {
        **HOSPITAL,
        "ccn": ccn,
        "name": name,
        "city": city,
        "state": state,
        "website_domain": website_domain,
        "domain_confidence": 1.0,
        **extra,
    }


def registry_rows(entries):
    """The state's hospitals as repo.list_hospitals returns them."""
    return [HospitalRow(**entry) for entry in entries]


def ref_of(entry, **changes):
    return repo.hospital_ref(HospitalRow(**entry)).model_copy(update=changes)


def web_doc(source_id, url, text, title=""):
    source = SourceDoc(
        id=source_id,
        kind=SourceKind.HOSPITAL_WEB,
        url=url,
        title=title,
        fetched_on=TODAY,
        sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    return source, text


def with_text(doc, extra):
    return doc[0], doc[1] + extra


# Iowa. Clarke County Hospital (161348) quoted UnityPoint Health's 1.BR.34 from files.unitypoint.org,
# whose Schedule D lists other UnityPoint hospitals and not Clarke; its own page links only
# chart.myunitypoint.org.
CLARKE = hospital_entry(
    "161348", "CLARKE COUNTY HOSPITAL", "OSCEOLA", "IA", "clarkehosp.org", zip="50213"
)
BETTENDORF = hospital_entry("160104", "TRINITY - BETTENDORF", "BETTENDORF", "IA", "unitypoint.org")
IOWA = [
    CLARKE,
    BETTENDORF,
    hospital_entry("160069", "ALLEN HOSPITAL", "WATERLOO", "IA", "unitypoint.org"),
    hospital_entry("160117", "FINLEY HOSPITAL", "DUBUQUE", "IA", "unitypoint.org"),
]
UPH_FILES = "https://files.unitypoint.org/api/public/content/"
UPH_POLICY = web_doc(
    "fap-uph-policy",
    UPH_FILES + "4af34da08b024422b2259af05dd622ae?v=",
    "Title: Financial Assistance – Hospital Facilities 1.BR.34\n"
    "SCOPE: UnityPoint Health hospitals and hospital organizations (referred to collectively as "
    "“UPH Hospitals”) that are 501(c )(3) tax -exempt, in Illinois and Iowa, and included in "
    "attached Schedule D.\n"
    "0-200% of FPIG : 100% discount off AGB\n"
    "SCHEDULE D – Covered Services and Provider Practices by Hospital (Updated July 1, 2023) The "
    "following UnityPoint Health Hospitals and Hospital Organizations are covered under Policy "
    "1.BR.34, Financial Assistance – Hospital Facilities.\n"
    "Allen Hospital, Waterloo, Iowa\nFinley Hospital, Dubuque, Iowa\n"
    "UnityPoint Health – Trinity Bettendorf, Bettendorf, Iowa\n",
)
UPH_APPLICATION = web_doc(
    "fap-uph-application",
    UPH_FILES + "80a01a24ad2644c7a68fc69d210d5a5b?v=39acd7fe",
    "UnityPoint Health Financial Assistance Application for Iowa and Illinois hospitals. Return "
    "the completed application to: UnityPoint Health Financial Assistance PO Box 809330 Chicago, "
    "IL 60680-9330\n",
)
CLARKE_PAGE = web_doc(
    "web-clarke",
    "https://www.clarkehosp.org/patients/options-to-estimate-your-health-care-costs",
    "Clarke County Hospital, 800 S Fillmore St, Osceola, IA 50213. Estimate your costs in "
    "[MyUnityPoint](https://chart.myunitypoint.org/mychart/) before your visit.\n",
)
CLARKE_LINKS_UPH = web_doc(
    "web-clarke-fap",
    "https://www.clarkehosp.org/financial-assistance",
    "Clarke County Hospital follows the [Financial Assistance Policy]"
    "(https://files.unitypoint.org/api/public/content/4af34da08b024422.pdf).\n",
)

# Georgia. St. Francis Hospital-Emory Healthcare (110129, Columbus) cited Emory Healthcare's policy,
# whose "Hospitals include:" list leaves it out; mystfrancis.com never links Emory.
ST_FRANCIS = hospital_entry(
    "110129", "ST FRANCIS HOSPITAL- EMORY HEALTHCARE", "COLUMBUS", "GA", "mystfrancis.com"
)
PIEDMONT_ATHENS = hospital_entry(
    "110074", "PIEDMONT ATHENS REGIONAL MEDICAL CENTER", "ATHENS", "GA", "piedmont.org"
)
GEORGIA = [
    ST_FRANCIS,
    PIEDMONT_ATHENS,
    hospital_entry("110010", "EMORY UNIVERSITY HOSPITAL", "ATLANTA", "GA", "emoryhealthcare.org"),
    hospital_entry("110076", "EMORY DECATUR HOSPITAL", "DECATUR", "GA", "emoryhealthcare.org"),
    hospital_entry("110083", "PIEDMONT ATLANTA HOSPITAL", "ATLANTA", "GA", "piedmont.org"),
]
EMORY_POLICY = web_doc(
    "fap-emory",
    "https://www.emoryhealthcare.org/-/media/Project/EH/Emory/ui/pdfs/insurance/"
    "financial-assistance/emory-healthcare-financial-assistance-policy-final.pdf",
    "POLICY STATEMENT This Policy applies to Emergency and other Medically Necessary Care provided "
    "at Emory Healthcare Hospitals (listed below) and their outpatient departments.\n"
    "Emory Healthcare Hospitals include:\n• Emory University Hospital, Atlanta\n"
    "• Emory Decatur Hospital, Decatur\n• Houston Hospital - Perry\n"
    "Patients with family income at or below 200% of the Federal Poverty Level qualify for free "
    "care.\n",
)
ST_FRANCIS_PAGE = web_doc(
    "web-st-francis",
    "https://www.mystfrancis.com/pay-my-bill/billing-policies",
    "St. Francis Hospital, Columbus, Georgia. Our Financial Assistance Program helps patients who "
    "cannot pay. Call 706.596.4412. [Facebook](https://www.facebook.com/mystfrancis)\n",
    "Billing Policies | St. Francis - Emory Healthcare",
)
PIEDMONT_APPLICATION = web_doc(
    "fap-piedmont-application",
    "https://cd.piedmont-jss.production.merge-digital.com/-/media/files/patients-and-visitors/"
    "financial-assistance/2025-06-phc-financial-assistance-application.pdf",
    "Piedmont Healthcare Financial Assistance Application. Piedmont hospitals include: Piedmont "
    "Atlanta Hospital, Atlanta.\n",
)
PIEDMONT_PAGE = web_doc(
    "web-piedmont",
    "https://www.piedmont.org/patients-and-visitors/billing-and-financial-resources",
    "Billing and financial resources at Piedmont. Ask us about financial assistance.\n",
)

# Connecticut. Hartford HealthCare's policy names its hospitals in an appendix, not a list of its
# own; Charlotte Hungerford's page (070011) links hartfordhealthcare.org.
CHARLOTTE_HUNGERFORD = hospital_entry(
    "070011", "CHARLOTTE HUNGERFORD HOSPITAL", "TORRINGTON", "CT", "charlottehungerford.org"
)
CONNECTICUT = [
    CHARLOTTE_HUNGERFORD,
    hospital_entry("070025", "HARTFORD HOSPITAL", "HARTFORD", "CT", "hartfordhospital.org"),
]
HHC_POLICY = web_doc(
    "fap-hhc",
    "https://hartfordhealthcare.org/file%20library/fap/fap-policy-english.pdf",
    "Hartford HealthCare Financial Assistance Policy. This policy applies to each Hartford "
    "HealthCare entity as listed in Appendix D, Hartford Hospital in Hartford among them. Patients "
    "with household income at or below 250% of the Federal Poverty Level receive free care.\n",
)
CHARLOTTE_HUNGERFORD_PAGE = web_doc(
    "web-charlotte-hungerford",
    "https://www.charlottehungerford.org/patients-visitors/billing-insurance/financial-assistance",
    "Charlotte Hungerford Hospital, Torrington, CT. Read the [Financial Assistance Policy]"
    "(https://hartfordhealthcare.org/financial-assistance).\n",
)

# North Dakota. CHI Oakes (351315) is correct on CommonSpirit's G-003, which carries no list of
# covered hospitals; its own page says the policy applies but links only commonspirit.myfa.app.
CHI_OAKES = hospital_entry("351315", "CHI OAKES HOSPITAL", "OAKES", "ND", "oakeshospital.com")
NORTH_DAKOTA = [
    CHI_OAKES,
    hospital_entry(
        "350002", "CHI ST ALEXIUS HEALTH BISMARCK", "BISMARCK", "ND", "chistalexiushealth.org"
    ),
]
G003_POLICY = web_doc(
    "fap-g003",
    "https://www.commonspirit.org/content/dam/shared/en/pdfs/finance/assistance/"
    "g-003-financial-assistance-policy-en.pdf",
    "CommonSpirit Health Financial Assistance Policy G-003. Scope: CommonSpirit Health and each of "
    "its Hospital Organizations, CHI St. Alexius Health Bismarck in Bismarck among them. Patients "
    "with household income at or below 200% of the Federal Poverty Level receive a 100% "
    "discount.\n",
)
CHI_APPLICATION = web_doc(
    "fap-chi-application",
    "https://chilakewoodhealth.com/wp-content/uploads/2026/08/en_app_ndmn.pdf",
    "CHI Financial Assistance Application for North Dakota and Minnesota. Send it to CHI St. "
    "Alexius Health, Bismarck, ND.\n",
)
CHI_OAKES_PAGE = web_doc(
    "web-chi-oakes",
    "https://oakeshospital.com/financial/financial-assistance/",
    "CHI Oakes Hospital, Oakes, North Dakota. The CommonSpirit Health Financial Assistance Policy "
    "applies to CHI Oakes Hospital. [Apply online](https://commonspirit.myfa.app/)\n",
    "Financial Assistance - CHI Oakes Hospital",
)

# System documents on a CDN or the system's own domain, each with a covered-hospital list: the
# website's own name (Corewell, Piedmont, Sentara, UNC Health Appalachian) or "Applies to: All"
# (Franciscan) says they are the hospital's system's.
ROYAL_OAK = hospital_entry(
    "230130", "BEAUMONT HOSPITAL ROYAL OAK", "ROYAL OAK", "MI", "corewellhealth.org"
)
MICHIGAN = [
    ROYAL_OAK,
    hospital_entry(
        "230038",
        "COREWELL HEALTH BUTTERWORTH HOSPITAL",
        "GRAND RAPIDS",
        "MI",
        "corewellhealth.org",
    ),
]
COREWELL_APPLICATION = web_doc(
    "fap-corewell-application",
    "https://assets.contentstack.io/v3/assets/blt3055f692fe7bf193/blt96841f74da7cff06/"
    "confidential-financial-assistance-application-english.pdf",
    "Corewell Health Confidential Financial Assistance Application. The following Corewell Health "
    "hospitals are covered by this application: Corewell Health Butterworth Hospital, Grand "
    "Rapids.\n",
)
COREWELL_PAGE = web_doc(
    "web-corewell",
    "https://corewellhealth.org/billing/financial-assistance",
    "Financial assistance at Corewell Health. Ask about our programs.\n",
)
SENTARA_VIRGINIA_BEACH = hospital_entry(
    "490057", "SENTARA VIRGINIA BEACH GENERAL HOSPITAL", "VIRGINIA BEACH", "VA", "sentara.com"
)
VIRGINIA = [
    SENTARA_VIRGINIA_BEACH,
    hospital_entry("490007", "SENTARA NORFOLK GENERAL HOSPITAL", "NORFOLK", "VA", "sentara.com"),
]
SENTARA_FORM = web_doc(
    "fap-sentara-form",
    "https://shc-p-001.sitecorecontenthub.cloud/api/public/content/"
    "ce32340acc4c46e4bb42003f9e27546e?v=c7b9a7b6",
    "Sentara Health Financial Assistance Application. The following hospital facilities are "
    "covered under the Financial Assistance Policy: Sentara Norfolk General Hospital, Norfolk.\n",
)
SENTARA_PAGE = web_doc(
    "web-sentara",
    "https://www.sentara.com/en/health-care/billing/financial-assistance",
    "Sentara offers financial assistance to patients who qualify.\n",
)
RENSSELAER = hospital_entry(
    "151324", "FRANCISCAN HEALTH RENSSELAER, INC", "RENSSELAER", "IN", "franciscanhealth.org"
)
ANDERSON = hospital_entry(
    "150088", "ASCENSION ST VINCENT ANDERSON", "ANDERSON", "IN", "ascension.org"
)
INDIANA = [
    RENSSELAER,
    ANDERSON,
    hospital_entry(
        "150126", "FRANCISCAN HEALTH CROWN POINT", "CROWN POINT", "IN", "franciscanhealth.org"
    ),
]
FRANCISCAN_POLICY = web_doc(
    "fap-franciscan",
    "https://statements.franciscanalliance.org/images/PolicyENG.pdf",
    "Policy Number: 301.01 Applies to: All Franciscan Alliance Hospitals (See Entity Listing)\n"
    "The following Franciscan Alliance hospitals are covered under this policy: Franciscan Crown "
    "Point, Crown Point.\n0-200% of the Federal Poverty Guidelines: 100% discount\n",
)
CANNON = hospital_entry(
    "341323",
    "CHARLES A CANNON JR MEMORIAL HOSPITAL",
    "LINVILLE",
    "NC",
    "unchealthappalachian.org",
)
NORTH_CAROLINA = [
    CANNON,
    hospital_entry("340041", "CALDWELL MEMORIAL HOSPITAL", "LENOIR", "NC", "unchealth.org"),
]
UNC_POLICY = web_doc(
    "fap-unc",
    "https://www.unchealth.org/pdfs/finance/pdf-system-Patient_Financial_Assistance_and_Hospital_"
    "Medical_Debt_Mitigation_Policy.pdf",
    "Patient Financial Assistance and Hospital Medical Debt Mitigation Policy APPLICABILITY: This "
    'policy applies to the following entities (collectively referred to as "UNC Health" in this '
    "policy): ✓ Caldwell Memorial Hospital, Lenoir ✓ UNC Health Appalachian\n",
)
BARNES_JEWISH = hospital_entry(
    "260032", "BARNES JEWISH HOSPITAL", "SAINT LOUIS", "MO", "barnesjewish.org"
)
MISSOURI = [
    BARNES_JEWISH,
    hospital_entry("260091", "ST LUKES HOSPITAL", "CHESTERFIELD", "MO", "stlukes-stl.com"),
]
BJC_APPLICATION = web_doc(
    "fap-bjc-application",
    "https://www.saintlukeskc.org/sites/slkc/files/2026-05/"
    "3259250-Financial%20Assistance%20Application.pdf",
    "BJC HealthCare Financial Assistance Application. Mail the completed form to BJC Patient "
    "Financial Services.\n",
)
WEST_ALLIS = hospital_entry(
    "520139", "WEST ALLIS MEMORIAL HOSPITAL", "WEST ALLIS", "WI", "aurorahealthcare.org"
)
WISCONSIN = [
    WEST_ALLIS,
    hospital_entry(
        "520138", "AURORA ST LUKES MEDICAL CENTER", "MILWAUKEE", "WI", "aurorahealthcare.org"
    ),
]
ADVOCATE_POLICY = web_doc(
    "fap-advocate",
    "https://cdn.atriumhealth.org/-/media/chs/files/for-patients-visitors/financial-assistance/"
    "advocate-financial-assistance-policies/advocate-enterprise-fap.pdf",
    "Advocate Health Enterprise Financial Assistance Policy. It applies to Advocate Health "
    "hospitals, Aurora St. Luke's Medical Center in Milwaukee among them. Patients with household "
    "income at or below 300% of the Federal Poverty Guidelines receive free care.\n",
)

# Illinois. Carle Hoopeston (141316) quoted Carle Health West Region's 1.BR.34 on carle.org, its
# own domain: the policy covers the hospitals "included in attached Schedule D", which lists
# Methodist, Pekin and Proctor only.
HOOPESTON = hospital_entry(
    "141316", "CARLE HOOPESTON REGIONAL HEALTH CENTER", "HOOPESTON", "IL", "carle.org"
)
PROCTOR = hospital_entry("140013", "CARLE HEALTH PROCTOR HOSPITAL", "PEORIA", "IL", "carle.org")
ILLINOIS = [
    HOOPESTON,
    PROCTOR,
    hospital_entry("140209", "CARLE HEALTH METHODIST HOSPITAL", "PEORIA", "IL", "carle.org"),
    hospital_entry("140120", "CARLE HEALTH PEKIN HOSPITAL", "PEKIN", "IL", "carle.org"),
]
CARLE_WEST_POLICY = web_doc(
    "fap-carle-west",
    "https://carle.org/getmedia/9e26c950-72c2-4b1f-9c81-433c1c9144c6/"
    "PeoriaFinancialAssistancePolicyEnglish-FINAL.pdf",
    "SCOPE: Carle Health – West Region hospitals and hospital organizations (referred to "
    "collectively as “CHWR Hospitals”) that are 501(c)(3) tax-exempt and included in attached "
    "Schedule D.\n0-200% of FPIG : 100% discount off AGB\n"
    "SCHEDULE D – Covered Services and Provider Practices by Hospital. The following Carle Health "
    "– West Region f/k/a Methodist Health Services Corporation Hospitals and Hospital "
    "Organizations are covered under Policy 1.BR.34: Carle Health Methodist Hospital, Peoria; "
    "Carle Health Pekin Hospital, Pekin; Carle Health Proctor Hospital, Peoria.\n",
    "Title: Financial Assistance – Hospital Facilities 1.BR.34",
)
CARLE_PAGE = web_doc(
    "web-carle",
    "https://carle.org/patients-visitors/financial-assistance",
    "Financial assistance at Carle Health hospitals, Carle Hoopeston Regional Health Center among "
    "them.\n",
    "Financial Assistance - Apply Today | Carle Health",
)

# Kansas and New York: documents a hospital's own pages link, its own domain's, and an asset
# host's.
WAMEGO = hospital_entry("171337", "WAMEGO HEALTH CENTER", "WAMEGO", "KS", "wamegohealthcenter.org")
MARYSVILLE = hospital_entry(
    "171363", "COMMUNITY MEMORIAL HEALTHCARE, INC.", "MARYSVILLE", "KS", "cmhcare.org"
)
KANSAS = [
    WAMEGO,
    MARYSVILLE,
    hospital_entry("170122", "ASCENSION VIA CHRISTI ST FRANCIS", "WICHITA", "KS", "ascension.org"),
]
ASCENSION_FILES = "https://healthcare.ascension.org/-/media/healthcare/financial-assistance/"
KANSAS_POLICY = web_doc(
    "fap-ascension-kansas",
    ASCENSION_FILES + "kansas/fap/runningfile_kswic_english_financial-assistance-policy.pdf",
    "Ascension Via Christi Financial Assistance Policy. This policy applies to all emergency and "
    "other medically necessary care provided by the Organization, Wamego Health Center among its "
    "hospitals. Patients with household income at or below 200% of the Federal Poverty Guidelines "
    "receive free care.\n",
)
KANSAS_APPLICATION = web_doc(
    "fap-ascension-kansas-application",
    ASCENSION_FILES
    + "kansas/fap-application/runningfile_kswic_english_financial-assistance-application.pdf",
    "Ascension Financial Assistance Application. Hospitals include: Ascension Via Christi St. "
    "Francis, Wichita.\n",
)
WAMEGO_PAGE = web_doc(
    "web-wamego",
    "https://wamegohealthcenter.org/patients-visitors/billing-and-insurance-information/",
    "Wamego Health Center, Wamego, Kansas. Read the [Ascension financial assistance policy]"
    "(https://healthcare.ascension.org/financial-assistance).\n",
    "Billing and Insurance Information | Wamego Health Center",
)
MARYSVILLE_POLICY = web_doc(
    "fap-marysville",
    "https://www.cmhcare.org/docs/2022_Financial_Assistance_Policy.pdf",
    "Financial Assistance Policy. Hospitals include: Ascension Via Christi St. Francis, Wichita, "
    "for transfers. Patients with household income at or below 200% of the Federal Poverty "
    "Guidelines receive free care.\n",
)
CANTON_POTSDAM = hospital_entry(
    "330197", "CANTON-POTSDAM HOSPITAL", "POTSDAM", "NY", "rochesterregional.org"
)
ROCHESTER_GENERAL = hospital_entry("330125", "ROCHESTER GENERAL HOSPITAL", "ROCHESTER", "NY")
DELAWARE_VALLEY = hospital_entry(
    "331312", "DELAWARE VALLEY HOSPITAL, INC", "WALTON", "NY", "nyuhs.org"
)
NEW_YORK = [
    CANTON_POTSDAM,
    ROCHESTER_GENERAL,
    DELAWARE_VALLEY,
    hospital_entry("330011", "UHS BINGHAMTON GENERAL HOSPITAL", "BINGHAMTON", "NY", "nyuhs.org"),
]
NYUHS_FORM = web_doc(
    "application-nyuhs",
    "https://nyuhs.widen.net/s/m8mqhlqzkx/nyuhsuniformhospital_fa_app0226f",
    "UHS Financial Assistance Application. Hospitals include: UHS Binghamton General Hospital, "
    "Binghamton.\n",
    "**Financial Assistance Application (English)**",
)

# Canton-Potsdam (330197) quoted "Financial Assistance Program for Rochester General Hospital",
# while Rochester Regional's page also links a Canton-Potsdam Hospital policy that was never
# stored.
RRH_FILES = (
    "https://edge.sitecorecloud.io/rochesterred189-rrh4822-prodb67b-4284/media/project/rrh/rrh/"
    "files/billing-and-insurance/"
)
RGH_POLICY = web_doc(
    "billing-rgh-policy",
    RRH_FILES + "financial-assistance-policy-rgh.pdf",
    "Financial Assistance Program for Rochester General Hospital and all Rochester Regional "
    "Health Employed Physicians. Patients with household income at or below 400% of the Federal "
    "Poverty Guidelines qualify for free care.\n",
    "Rochester General Hospital Financial Assistance Policy",
)
RRH_PAGE = web_doc(
    "billing-rrh",
    "https://www.rochesterregional.org/billing-and-insurance/financial-assistance",
    "Financial Assistance at Rochester Regional Health.\n"
    f"- [Rochester General Hospital Financial Assistance Policy]({RRH_FILES}"
    "financial-assistance-policy-rgh.pdf)\n"
    f"- [Canton-Potsdam Hospital Financial Assistance Policy]({RRH_FILES}"
    "financial-assistance-policy-cph.pdf)\n"
    f"- [Download the Financial Assistance Application - English]({RRH_FILES}"
    "uniform-application-english.pdf)\n",
    "Financial Assistance | Rochester Regional Health",
)
RRH_APPLICATION = web_doc(
    "application-rrh",
    RRH_FILES + "uniform-application-english.pdf",
    "NYS Uniform Hospital Financial Assistance Application. Return it to the hospital's patient "
    "financial services office.\n",
    "Download the Financial Assistance Application - English",
)

# Pennsylvania. Penn Medicine's summary names The Chester County Hospital (390179), though a page
# also links it under Lancaster General's name.
CHESTER_COUNTY = hospital_entry(
    "390179", "CHESTER COUNTY HOSPITAL", "WEST CHESTER", "PA", "pennmedicine.org"
)
PENNSYLVANIA = [
    CHESTER_COUNTY,
    hospital_entry("390100", "LANCASTER GENERAL HOSPITAL", "LANCASTER", "PA", "pennmedicine.org"),
    hospital_entry("390203", "DOYLESTOWN HOSPITAL", "DOYLESTOWN", "PA", "doylestownhealth.org"),
]
PENN_POLICIES = "https://www.pennmedicine.org/patient-resources/policies/financial-assistance"
PENN_PAGE = web_doc(
    "fap-penn",
    PENN_POLICIES,
    "[Financial Assistance Policy - Penn Medicine, Chester County Hospital, and Doylestown Health]"
    f"({PENN_POLICIES}/financial-assistance-policy)\n"
    "[Lancaster General Health financial assistance policy summary]"
    f"({PENN_POLICIES}/financial-assistance-policy-plain-language)\n",
    "Financial assistance",
)
PENN_SUMMARY = web_doc(
    "summary-penn",
    PENN_POLICIES + "/financial-assistance-policy-plain-language",
    "Financial Assistance Plain Language Summary. Penn Medicine hospitals, The Chester County "
    "Hospital in West Chester among them, give free care at or below 400% of the Federal Poverty "
    "Guidelines.\n",
    "Financial Assistance Plain Language Summary",
)

# Another state's version of the system policy, named by the address or the title's first
# segment: Ascension Providence (450042, Waco) stored only other states' pages and quoted
# Maryland's free-care limit.
TEHACHAPI = hospital_entry(
    "051301", "ADVENTIST HEALTH TEHACHAPI VALLEY", "TEHACHAPI", "CA", "adventisthealth.org"
)
HAWAII_BILLING = web_doc(
    "billing-adventist-hawaii",
    "https://www.adventisthealth.org/documents/system/self-pay-billing-and-collection-policy-"
    "hawaii.pdf",
    "Adventist Health Self Pay Billing and Collection Policy for its Hawaii hospital.\n",
    "[PDF] Self Pay Billing and Collection Policy (HI) - Adventist Health",
)
PROVIDENCE = hospital_entry("450042", "ASCENSION PROVIDENCE", "WACO", "TX", "ascension.org")
MARYLAND_PAGE = web_doc(
    "fap-ascension-maryland",
    "https://healthcare.ascension.org/financial-assistance/"
    "maryland-ascension-saint-agnes-financial-assistance",
    "Financial assistance at Ascension Saint Agnes. Locations: Ascension Saint Agnes Hospital, "
    "Ascension Providence. Patients with household income at or below 250% of the Federal Poverty "
    "Guidelines receive free care.\n",
    "Financial assistance Ascension Saint Agnes in Maryland | Ascension",
)
FLORIDA_PAGE = web_doc(
    "fap-ascension-florida",
    "https://healthcare.ascension.org/financial-assistance/"
    "florida-ascension-st-vincents-financial-assistance",
    "Financial assistance at Ascension St. Vincent's. Bring a photo ID and proof of income.\n",
    "Financial assistance at Ascension St. Vincent’s in FL | Ascension",
)
MARION = hospital_entry("010044", "MARION REGIONAL MEDICAL CENTER", "HAMILTON", "AL", "nmhs.net")
NMHS_PAGE = web_doc(
    "fap-nmhs",
    "https://www.nmhs.net/patients-and-visitors/financial-assistance",
    "North Mississippi Health Services offers financial assistance at Marion Regional Medical "
    "Center, Hamilton, Alabama.\n",
    "Financial Assistance | North Mississippi Health Services",
)
INDIANA_POLICY = web_doc(
    "fap-ascension-indiana",
    ASCENSION_FILES
    + "indiana/fap/english/runningfile_inasc_anderson_english_financial-assistance-policy.pdf",
    "Ascension St. Vincent Anderson Financial Assistance Policy. Send payments to PO Box 4471, "
    "Chicago, Illinois 60680.\n",
)

A, B, C = "foreign_documents", "sibling_policies", "other_state_documents"
CLARKE_DOCS = [UPH_POLICY, UPH_APPLICATION, CLARKE_PAGE]
WAMEGO_DOCS = [KANSAS_POLICY, KANSAS_APPLICATION, WAMEGO_PAGE]
RRH_DOCS = [RGH_POLICY, RRH_PAGE, RRH_APPLICATION]
OWNER_SCOPE_CASES = [
    # Clause A2: off the hospital's site and the asset hosts, unlinked, unnamed, with a
    # covered-hospital list naming other hospitals of the state.
    (A, CLARKE, CLARKE_DOCS, IOWA, "fap-uph-policy", True, "161348-uph-policy"),
    (A, CLARKE, CLARKE_DOCS, IOWA, "fap-uph-application", True, "161348-uph-application"),
    (A, CLARKE, CLARKE_DOCS, IOWA, "web-clarke", False, "161348-own-page"),
    (A, CLARKE, [UPH_POLICY, CLARKE_LINKS_UPH], IOWA, "fap-uph-policy", False, "linked"),
    (
        A,
        CLARKE,
        [with_text(UPH_POLICY, "Clarke County Hospital, Osceola, Iowa\n"), CLARKE_PAGE],
        IOWA,
        "fap-uph-policy",
        False,
        "names-clarke",
    ),
    (A, ST_FRANCIS, [EMORY_POLICY, ST_FRANCIS_PAGE], GEORGIA, "fap-emory", True, "110129-emory"),
    (
        A,
        CHARLOTTE_HUNGERFORD,
        [HHC_POLICY, CHARLOTTE_HUNGERFORD_PAGE],
        CONNECTICUT,
        "fap-hhc",
        False,
        "070011-linked",
    ),
    (A, CHARLOTTE_HUNGERFORD, [HHC_POLICY], CONNECTICUT, "fap-hhc", False, "070011-no-list"),
    (
        A,
        CHI_OAKES,
        [G003_POLICY, CHI_APPLICATION, CHI_OAKES_PAGE],
        NORTH_DAKOTA,
        "fap-g003",
        False,
        "351315-g003",
    ),
    (
        A,
        CHI_OAKES,
        [G003_POLICY, CHI_APPLICATION, CHI_OAKES_PAGE],
        NORTH_DAKOTA,
        "fap-chi-application",
        False,
        "351315-application",
    ),
    (
        A,
        ROYAL_OAK,
        [COREWELL_APPLICATION, COREWELL_PAGE],
        MICHIGAN,
        "fap-corewell-application",
        False,
        "230130-contentstack",
    ),
    (
        A,
        PIEDMONT_ATHENS,
        [PIEDMONT_APPLICATION, PIEDMONT_PAGE],
        GEORGIA,
        "fap-piedmont-application",
        False,
        "110074-merge-digital",
    ),
    (
        A,
        SENTARA_VIRGINIA_BEACH,
        [SENTARA_FORM, SENTARA_PAGE],
        VIRGINIA,
        "fap-sentara-form",
        False,
        "490057-sitecorecontenthub",
    ),
    (A, RENSSELAER, [FRANCISCAN_POLICY], INDIANA, "fap-franciscan", False, "151324-applies-to-all"),
    (A, CANNON, [UNC_POLICY], NORTH_CAROLINA, "fap-unc", False, "341323-website-name"),
    (A, BARNES_JEWISH, [BJC_APPLICATION], MISSOURI, "fap-bjc-application", False, "260032-bjc"),
    (A, WEST_ALLIS, [ADVOCATE_POLICY], WISCONSIN, "fap-advocate", False, "520139-advocate"),
    # Clause S: the Schedule D template's own scope, on any host, the hospital's own included.
    (
        A,
        HOOPESTON,
        [CARLE_WEST_POLICY, CARLE_PAGE],
        ILLINOIS,
        "fap-carle-west",
        True,
        "141316-schedule-d",
    ),
    (A, HOOPESTON, [CARLE_WEST_POLICY, CARLE_PAGE], ILLINOIS, "web-carle", False, "141316-page"),
    (
        A,
        HOOPESTON,
        [with_text(CARLE_WEST_POLICY, "Carle Hoopeston Regional Health Center, Hoopeston\n")],
        ILLINOIS,
        "fap-carle-west",
        False,
        "names-hoopeston",
    ),
    (A, PROCTOR, [CARLE_WEST_POLICY], ILLINOIS, "fap-carle-west", False, "140013-in-schedule"),
    (A, BETTENDORF, CLARKE_DOCS, IOWA, "fap-uph-policy", False, "160104-in-schedule"),
    (A, WAMEGO, WAMEGO_DOCS, KANSAS, "fap-ascension-kansas", False, "171337-names-wamego"),
    (
        A,
        WAMEGO,
        WAMEGO_DOCS,
        KANSAS,
        "fap-ascension-kansas-application",
        False,
        "171337-linked",
    ),
    (A, DELAWARE_VALLEY, [NYUHS_FORM], NEW_YORK, "application-nyuhs", False, "331312-asset-host"),
    (A, MARYSVILLE, [MARYSVILLE_POLICY], KANSAS, "fap-marysville", False, "171363-own-domain"),
    (
        A,
        {**CLARKE, "website_domain": None},
        [UPH_POLICY, CLARKE_PAGE],
        IOWA,
        "fap-uph-policy",
        False,
        "no-website",
    ),
    # Clause C: the address or the title's first segment names states, none of them the
    # hospital's.
    (C, TEHACHAPI, [HAWAII_BILLING], [], "billing-adventist-hawaii", True, "051301-hawaii"),
    (C, PROVIDENCE, [MARYLAND_PAGE], [], "fap-ascension-maryland", True, "450042-maryland"),
    (C, PROVIDENCE, [FLORIDA_PAGE], [], "fap-ascension-florida", True, "450042-florida"),
    (C, MARION, [NMHS_PAGE], [], "fap-nmhs", False, "010044-site-name"),
    (C, WAMEGO, WAMEGO_DOCS, [], "fap-ascension-kansas", False, "171337-kansas"),
    (C, ANDERSON, [INDIANA_POLICY], [], "fap-ascension-indiana", False, "150088-indiana"),
    # Clause B: the hospital's site links its own policy, and links this one under another
    # hospital's name.
    (B, CANTON_POTSDAM, RRH_DOCS, NEW_YORK, "billing-rgh-policy", True, "330197-rgh-policy"),
    (B, CANTON_POTSDAM, RRH_DOCS, NEW_YORK, "application-rrh", False, "330197-application"),
    (
        B,
        ROCHESTER_GENERAL,
        [RGH_POLICY, RRH_PAGE],
        NEW_YORK,
        "billing-rgh-policy",
        False,
        "330125-own",
    ),
    (
        B,
        CANTON_POTSDAM,
        [with_text(RGH_POLICY, "Canton-Potsdam Hospital\n"), RRH_PAGE],
        NEW_YORK,
        "billing-rgh-policy",
        False,
        "names-canton-potsdam",
    ),
    (
        B,
        CANTON_POTSDAM,
        [
            RGH_POLICY,
            (
                RRH_PAGE[0],
                RRH_PAGE[1].replace("Canton-Potsdam Hospital Financial", "Other Financial"),
            ),
        ],
        NEW_YORK,
        "billing-rgh-policy",
        False,
        "no-own-link",
    ),
    (
        B,
        CHESTER_COUNTY,
        [PENN_PAGE, PENN_SUMMARY],
        PENNSYLVANIA,
        "summary-penn",
        False,
        "390179-names-chester-county",
    ),
]


@pytest.mark.parametrize(
    ("clause", "hospital", "documents", "registry", "source_id", "set_aside"),
    [pytest.param(*case[:-1], id=case[-1]) for case in OWNER_SCOPE_CASES],
)
def test_foreign_sibling_policies_and_other_state_documents_by_clause(
    clause, hospital, documents, registry, source_id, set_aside
):
    from waive.atlas import pipeline

    judge = getattr(pipeline, clause)
    args = (ref_of(hospital), documents) + (() if clause == C else (registry_rows(registry),))
    assert (source_id in judge(*args)) is set_aside


def test_documents_set_aside_puts_foreign_and_other_state_documents_after_the_existing_ones():
    from waive.atlas.pipeline import _documents_set_aside
    from waive.atlas.verify import (
        DENTAL_REASON,
        FOREIGN_OWNER_REASON,
        OTHER_REGION_REASON,
        OTHER_STATE_DOCUMENT_REASON,
        SCHEDULE_OWNER_REASON,
        SIBLING_POLICY_REASON,
    )

    # Clarke's UnityPoint policy meets clause S too, but it is off Clarke's website and unlinked:
    # A2's reason says so. Carle West's sits on carle.org itself: only the schedule clause holds.
    assert _documents_set_aside(ref_of(CLARKE), CLARKE_DOCS, registry_rows(IOWA), set()) == {
        "fap-uph-policy": FOREIGN_OWNER_REASON,
        "fap-uph-application": FOREIGN_OWNER_REASON,
    }
    hoopeston_docs = [CARLE_WEST_POLICY, CARLE_PAGE]
    assert _documents_set_aside(
        ref_of(HOOPESTON), hoopeston_docs, registry_rows(ILLINOIS), set()
    ) == {"fap-carle-west": SCHEDULE_OWNER_REASON}
    assert _documents_set_aside(
        ref_of(CANTON_POTSDAM), RRH_DOCS, registry_rows(NEW_YORK), set()
    ) == {"billing-rgh-policy": SIBLING_POLICY_REASON}
    # Every stored document another state's: set aside all the same (the plan's ruling), for no
    # reader should be shown Maryland's policy as a Texas hospital's.
    assert _documents_set_aside(ref_of(PROVIDENCE), [MARYLAND_PAGE, FLORIDA_PAGE], [], set()) == {
        "fap-ascension-maryland": OTHER_STATE_DOCUMENT_REASON,
        "fap-ascension-florida": OTHER_STATE_DOCUMENT_REASON,
    }
    # The existing reasons come first: another Kaiser region's policy, whose address names
    # California, and a dental program's whose address names Oregon.
    sunnyside = hospital_entry(
        "380091", "KAISER SUNNYSIDE MEDICAL CENTER", "CLACKAMAS", "OR", "kaiserpermanente.org"
    )
    scal = web_doc("fap-scal", KAISER_DOCS + SCAL_PATH, SCAL_TEXT, MFA_TITLE)
    assert _documents_set_aside(ref_of(sunnyside), [scal], [], {"fap-scal"}) == {
        "fap-scal": OTHER_REGION_REASON
    }
    dental = web_doc("fap-dental", KAISER_DOCS + "dfa/oregon/" + DENTAL_PATH, DENTAL_TEXT)
    assert _documents_set_aside(ref_of(KAISER_LOS_ANGELES), [dental], [], set()) == {
        "fap-dental": DENTAL_REASON
    }


def store_docs(session, ccn, *docs):
    for source, text in docs:
        repo.save_source(session, source, text, ccn)


def test_recheck_withdraw_and_confirmation_set_aside_foreign_sibling_and_other_state_documents():
    from waive.atlas.eve_import import stored_ruling
    from waive.atlas.pipeline import confirm_domain, recheck_sheets, withdraw_rejected
    from waive.atlas.verify import (
        FOREIGN_OWNER_REASON,
        OTHER_STATE_DOCUMENT_REASON,
        SCHEDULE_OWNER_REASON,
        SIBLING_POLICY_REASON,
    )

    quoted_uph = with_text(UPH_POLICY, POLICY_TEXT)
    quoted_maryland = with_text(MARYLAND_PAGE, POLICY_TEXT)
    quoted_rgh = with_text(RGH_POLICY, POLICY_TEXT)
    engine = make_engine_with_hospital(*IOWA, PROVIDENCE, *NEW_YORK)
    with session_scope(engine) as session:
        store_docs(session, "161348", quoted_uph, UPH_APPLICATION, CLARKE_PAGE)
        store_docs(session, "450042", quoted_maryland, FLORIDA_PAGE)
        store_docs(session, "330197", quoted_rgh, RRH_PAGE, RRH_APPLICATION)
        for ccn, (source, _) in (
            ("161348", quoted_uph),
            ("450042", quoted_maryland),
            ("330197", quoted_rgh),
        ):
            publish_quoting(session, ccn, source)
        ruling = owner_ruling("discount", ccn="161348")
        record_owner_rulings(session, {"161348": ruling})
        before = stored_ruling(session, "161348")
        quoted = [path for path, _ in repo.latest_sheet(session, "161348")[0].field_paths()]
        assert {entry.ccn: entry.rejected for entry in recheck_sheets(session)} == {
            "161348": [(path, FOREIGN_OWNER_REASON) for path in quoted],
            "450042": [(path, OTHER_STATE_DOCUMENT_REASON) for path in quoted],
            "330197": [(path, SIBLING_POLICY_REASON) for path in quoted],
        }
        withdrawn = withdraw_rejected(session)
        assert sorted((r.ccn, r.outcome, r.version) for r in withdrawn) == [
            ("161348", "held", 2),
            ("330197", "held", 2),
            ("450042", "held", 2),
        ]
        assert recheck_sheets(session) == []
        # A set-aside never changes the owner's ruling.
        assert stored_ruling(session, "161348") == before
    # Confirming the website re-verifies the sheet the same way.
    engine = make_engine_with_hospital({**HOOPESTON, "domain_confidence": 0.6}, *ILLINOIS[1:])
    with session_scope(engine) as session:
        carle_west = with_text(CARLE_WEST_POLICY, POLICY_TEXT)
        store_docs(session, "141316", carle_west, CARLE_PAGE)
        publish_quoting(session, "141316", carle_west[0])
        result = confirm_domain(session, "141316", "carle.org")
        assert (result.outcome, result.version) == ("held", 2)
        assert f"eligibility.free_care_max_fpl: {SCHEDULE_OWNER_REASON}" in result.notes
        assert repo.latest_sheet(session, "141316")[0].field_paths() == []


def test_foreign_documents_and_sibling_policies_are_detached_at_build_time():
    from waive.atlas.verify import (
        FOREIGN_OWNER_REASON,
        SCHEDULE_OWNER_REASON,
        SIBLING_POLICY_REASON,
    )

    engine = make_engine_with_hospital(*IOWA)
    with session_scope(engine) as session:
        store_docs(session, "161348", *CLARKE_DOCS)
        ai = GroundedAI()
        result = build_hospital(session, NoSpend(), ai, "161348", TODAY, reuse_sources=True)
        detail = review_detail(session, "161348", "foreign_documents")
        assert sorted(detail["sources"], key=lambda entry: entry["id"]) == [
            {
                "id": "fap-uph-application",
                "url": UPH_APPLICATION[0].url,
                "clause": "A2",
                "reason": FOREIGN_OWNER_REASON,
            },
            {
                "id": "fap-uph-policy",
                "url": UPH_POLICY[0].url,
                "clause": "A2",
                "reason": FOREIGN_OWNER_REASON,
            },
        ]
        listed = ", ".join(entry["id"] for entry in detail["sources"])
        assert result.notes[0] == (
            "2 stored documents are another organisation's or another hospital's policy that does "
            f"not cover the hospital ({listed}); detached and left out of the structurer"
        )
        assert ai.prompts and all("fap-uph" not in prompt for prompt in ai.prompts)
        assert [s.id for s, _ in repo.sources_for(session, "161348")] == ["web-clarke"]
    engine = make_engine_with_hospital(*NEW_YORK)
    with session_scope(engine) as session:
        store_docs(session, "330197", *RRH_DOCS)
        ai = GroundedAI()
        build_hospital(session, NoSpend(), ai, "330197", TODAY, reuse_sources=True)
        assert ai.prompts and all("billing-rgh-policy" not in prompt for prompt in ai.prompts)
        assert review_detail(session, "330197", "foreign_documents") == {
            "sources": [
                {
                    "id": "billing-rgh-policy",
                    "url": RGH_POLICY[0].url,
                    "clause": "B",
                    "reason": SIBLING_POLICY_REASON,
                }
            ]
        }
    engine = make_engine_with_hospital(*ILLINOIS)
    with session_scope(engine) as session:
        store_docs(session, "141316", CARLE_WEST_POLICY, CARLE_PAGE)
        build_hospital(session, NoSpend(), GroundedAI(), "141316", TODAY, reuse_sources=True)
        assert review_detail(session, "141316", "foreign_documents") == {
            "sources": [
                {
                    "id": "fap-carle-west",
                    "url": CARLE_WEST_POLICY[0].url,
                    "clause": "S",
                    "reason": SCHEDULE_OWNER_REASON,
                }
            ]
        }


def test_other_state_documents_are_detached_at_build_time_even_when_they_are_all_there_is():
    # Ascension Providence (450042, Waco) stored only Maryland's, Florida's and other states'
    # pages, and published Maryland's 250% free-care limit: the 7.9 "nothing to prefer" fallback
    # kept them. A document whose address or title names only other states is set aside now,
    # whatever else is stored, and the sheet is held without the fields it quoted.
    engine = make_engine_with_hospital(PROVIDENCE)
    with session_scope(engine) as session:
        quoted_maryland = with_text(MARYLAND_PAGE, POLICY_TEXT)
        store_docs(session, "450042", quoted_maryland, FLORIDA_PAGE)
        publish_quoting(session, "450042", quoted_maryland[0])
        quoted = [path for path, _ in repo.latest_sheet(session, "450042")[0].field_paths()]
        result = build_hospital(session, NoSpend(), NoModel(), "450042", TODAY, reuse_sources=True)
        assert (result.outcome, result.version) == ("held", 2)
        assert result.notes[1:] == [NOTHING_LEFT_NOTE, held_note(quoted)]
        assert result.notes[0].startswith("2 stored documents are another state's version")
        assert repo.sources_for(session, "450042") == []
        detail = review_detail(session, "450042", "other_state_documents")
        assert detail["state"] == "TX"
        assert sorted((entry["id"], entry["states"]) for entry in detail["sources"]) == [
            ("fap-ascension-florida", ["FL"]),
            ("fap-ascension-maryland", ["MD"]),
        ]


MERCY_MERCED = hospital_entry("050444", "MERCY MEDICAL CENTER", "MERCED", "CA", "commonspirit.org")
MERCY_SAN_JUAN = hospital_entry(
    "050516", "MERCY SAN JUAN MEDICAL CENTER", "CARMICHAEL", "CA", "commonspirit.org"
)
LONGMONT = hospital_entry(
    "060003", "LONGMONT UNITED HOSPITAL", "LONGMONT", "CO", "commonspirit.org"
)
ST_ANTHONY = hospital_entry(
    "060015", "CENTURA HEALTH-ST ANTHONY HOSPITAL", "LAKEWOOD", "CO", "commonspirit.org"
)
COMMONSPIRIT_DAM = "https://www.commonspirit.org/content/dam/"
MERCY_SAN_JUAN_APPLICATION = web_doc(
    "application-mercy-san-juan",
    COMMONSPIRIT_DAM + "commonspiritorg/en/dhsac/casa/finance/assistance/"
    "msjmc-financial-assistance-app-en.pdf",
    "Mercy San Juan Medical Center, Carmichael. Financial Assistance Application. " + POLICY_TEXT,
    "[PDF] Mercy San Juan Medical Center Financial Assistance Application ...",
)
COMMONSPIRIT_POLICY = web_doc(
    "fap-commonspirit",
    COMMONSPIRIT_DAM + "shared/en/pdfs/finance/assistance/g-003-financial-assistance-policy-en.pdf",
    "CommonSpirit Health Financial Assistance Policy G-003. " + POLICY_TEXT,
)
CALIFORNIA_POLICY = web_doc(
    "fap-commonspirit-california",
    COMMONSPIRIT_DAM + "shared/en/pdfs/finance/assistance/california/financial-assistance.pdf",
    "Dignity Health Financial Assistance Policy. " + POLICY_TEXT,
    "Financial Assistance Policy for California Hospitals",
)


def test_other_state_documents_are_never_lent_and_siblings_lend_only_within_the_state():
    # Longmont United (060003, Colorado) and two Colorado and Kansas siblings on commonspirit.org
    # borrowed Mercy Medical Center Merced's (050444, California) documents, Mercy San Juan's
    # application among them, and published its 30-day decision. repo.siblings_on_domain has no
    # state filter.
    from waive.atlas.pipeline import BuildResult, _share_sibling_sources

    engine = make_engine_with_hospital(MERCY_MERCED, LONGMONT, ST_ANTHONY)
    with session_scope(engine) as session:
        store_docs(session, "050444", MERCY_SAN_JUAN_APPLICATION)
        publish_quoting(session, "050444", MERCY_SAN_JUAN_APPLICATION[0])
        longmont = repo.get_hospital(session, "060003")
        result = BuildResult("060003", longmont.name, "skipped")
        assert _share_sibling_sources(session, longmont, [], result) is None
        # A sibling in the state lends its policy, but not another state's version beside it.
        store_docs(session, "060015", COMMONSPIRIT_POLICY, CALIFORNIA_POLICY)
        publish_quoting(session, "060015", COMMONSPIRIT_POLICY[0])
        borrowed = _share_sibling_sources(session, longmont, [], result)
        assert borrowed is not None and borrowed.shared_ids == {"fap-commonspirit"}
        assert [s.id for s, _ in repo.sources_for(session, "060003")] == ["fap-commonspirit"]
    # Nor does a sibling in the state pass on what it borrowed from another state's: St. Anthony
    # held Mercy San Juan's application from its own borrowing, and Longmont would have had it
    # from St. Anthony instead.
    engine = make_engine_with_hospital(MERCY_MERCED, MERCY_SAN_JUAN, LONGMONT, ST_ANTHONY)
    with session_scope(engine) as session:
        store_docs(session, "050444", MERCY_SAN_JUAN_APPLICATION)
        publish_quoting(session, "050444", MERCY_SAN_JUAN_APPLICATION[0])
        repo.link_source(session, "060015", "application-mercy-san-juan")
        repo.add_review_item(
            session,
            "060015",
            "shared_sources",
            {
                "from_ccn": "050444",
                "domain": "commonspirit.org",
                "sources": ["application-mercy-san-juan"],
            },
        )
        publish_quoting(session, "060015", MERCY_SAN_JUAN_APPLICATION[0])
        longmont = repo.get_hospital(session, "060003")
        result = BuildResult("060003", longmont.name, "skipped")
        assert _share_sibling_sources(session, longmont, [], result) is None
        store_docs(session, "060015", COMMONSPIRIT_POLICY)
        borrowed = _share_sibling_sources(session, longmont, [], result)
        assert borrowed is not None and borrowed.shared_ids == {"fap-commonspirit"}


def lend(session, borrower, lender, *source_ids):
    """What _share_sibling_sources records when a sibling lends documents."""
    for source_id in source_ids:
        repo.link_source(session, borrower, source_id)
    detail = {"from_ccn": lender, "domain": "commonspirit.org", "sources": list(source_ids)}
    return repo.add_review_item(session, borrower, "shared_sources", detail)


BRAZOSPORT = hospital_entry(
    "450072", "CHI ST LUKE'S HEALTH BRAZOSPORT", "LAKE JACKSON", "TX", "commonspirit.org"
)
MERCY_REDDING = hospital_entry(
    "051319", "MERCY MEDICAL CENTER", "REDDING", "CA", "commonspirit.org"
)
BRAZOSPORT_APPLICATION = web_doc(
    "application-brazosport",
    COMMONSPIRIT_DAM + "commonspiritorg/en/stluh/soho/finance/assistance/"
    "stlukes-health-brazosport-financial-assistance-app-en.pdf",
    "CHI St. Luke's Health Brazosport, Lake Jackson. Financial Assistance Application. "
    + POLICY_TEXT,
    "Brazosport Hospital Financial Assistance Application Form ...",
)
OKLAHOMA = [
    hospital_entry("260065", "MERCY HOSPITAL SPRINGFIELD", "SPRINGFIELD", "MO", "mercy.net"),
    hospital_entry("371304", "MERCY HOSPITAL TISHOMINGO INC", "TISHOMINGO", "OK", "mercy.net"),
]
MERCY_REGIONS_PAGE = web_doc(
    "web-mercy-regions",
    "https://www.mercy.net/patients-visitors/billing/financial-assistance/",
    "Financial assistance at Mercy Hospital Springfield, Springfield. " + POLICY_TEXT,
    "Financial Assistance AR, OK, Springfield & St. Louis | Mercy",
)
ST_LUKES_PAGE = web_doc(
    "fap-st-lukes",
    "https://www.commonspirit.org/patient-resources/stlukes-health-financial-assistance",
    "Financial assistance at CHI St. Luke's Health Brazosport, Lake Jackson, and Baylor St. "
    "Luke's Medical Center, Houston. " + POLICY_TEXT,
    "St. Luke's Health Financial Assistance | CommonSpirit Health",
)
BAYLOR_ST_LUKES = hospital_entry(
    "450193", "BAYLOR ST LUKES MEDICAL CENTER", "HOUSTON", "TX", "commonspirit.org"
)
LENT_NOTE = (
    "1 stored document is lent by another state's hospital (application-mercy-san-juan); left "
    "out of the structurer and still linked"
)


def test_documents_lent_across_states_are_set_aside_for_the_borrower_only():
    # The atlas of 2026-10-24 holds 25 loans from a sibling in another state, made before siblings
    # lent only within the state: Mercy Medical Center Merced (050444, California) lent Mercy San
    # Juan's application to Longmont United (060003, Colorado), whose published sheet quotes its
    # decision deadline. Nothing in a lent document's address names California. A document the
    # borrower held itself before the loan (a sheet of its own read it) is its own, and stays; a
    # loan from an in-state sibling that had the document from another state is traced back to
    # that state (Mercy Redding, 051319, from Merced, of Brazosport's Texas application). A
    # system-wide document lent across states stays too: CommonSpirit's policy names no hospital
    # of the state it came from, and Mercy's page names Oklahoma in its title.
    from waive.atlas.pipeline import _lent_from_another_state, recheck_sheets, withdraw_rejected
    from waive.atlas.verify import OTHER_STATE_DOCUMENT_REASON

    def lent(ccn):
        row = repo.get_hospital(session, ccn)
        registry = repo.list_hospitals(session, state=row.state)
        return _lent_from_another_state(
            session, repo.hospital_ref(row), repo.sources_for(session, ccn), registry
        )

    engine = make_engine_with_hospital(
        MERCY_MERCED,
        MERCY_SAN_JUAN,
        LONGMONT,
        ST_ANTHONY,
        BRAZOSPORT,
        BAYLOR_ST_LUKES,
        MERCY_REDDING,
        *OKLAHOMA,
    )
    with session_scope(engine) as session:
        # Merced holds the Texas St. Luke's hospitals' page itself (it names two of them), and
        # lent it with the rest.
        store_docs(session, "050444", MERCY_SAN_JUAN_APPLICATION, COMMONSPIRIT_POLICY)
        store_docs(session, "050444", ST_LUKES_PAGE)
        publish_quoting(session, "050444", MERCY_SAN_JUAN_APPLICATION[0])
        lend(
            session,
            "060003",
            "050444",
            "application-mercy-san-juan",
            "fap-commonspirit",
            "fap-st-lukes",
        )
        publish_quoting(session, "060003", MERCY_SAN_JUAN_APPLICATION[0])
        # St. Anthony scouted CommonSpirit's policy and published from it before the same loan.
        store_docs(session, "060015", COMMONSPIRIT_POLICY)
        own = publish_quoting(session, "060015", COMMONSPIRIT_POLICY[0])
        own.created_at = datetime(2026, 10, 1, tzinfo=UTC)
        session.flush()
        lend(session, "060015", "050444", "fap-commonspirit")
        # Merced borrowed Brazosport's application from Texas and passed it on to Redding.
        store_docs(session, "450072", BRAZOSPORT_APPLICATION)
        lend(session, "050444", "450072", "application-brazosport")
        lend(session, "051319", "050444", "application-brazosport")
        publish_quoting(session, "051319", BRAZOSPORT_APPLICATION[0])
        # Mercy Springfield (Missouri) lent its "AR, OK, Springfield & St. Louis" page to
        # Tishomingo (Oklahoma): it covers Oklahoma, and stays.
        store_docs(session, "260065", MERCY_REGIONS_PAGE)
        lend(session, "371304", "260065", "web-mercy-regions")
        assert lent("060003") == {"application-mercy-san-juan", "fap-st-lukes"}
        assert lent("060015") == set()
        assert lent("050444") == {"application-brazosport"}
        assert lent("051319") == {"application-brazosport"}
        assert lent("371304") == set()
        quoted = [path for path, _ in repo.latest_sheet(session, "060003")[0].field_paths()]
        assert {entry.ccn: entry.rejected for entry in recheck_sheets(session)} == {
            "060003": [(path, OTHER_STATE_DOCUMENT_REASON) for path in quoted],
            "051319": [(path, OTHER_STATE_DOCUMENT_REASON) for path in quoted],
        }
        withdrawn = withdraw_rejected(session)
        assert sorted((r.ccn, r.outcome) for r in withdrawn) == [
            ("051319", "held"),
            ("060003", "held"),
        ]
        # The links stay: a set-aside never deletes one.
        assert {s.id for s, _ in repo.sources_for(session, "060003")} == {
            "application-mercy-san-juan",
            "fap-commonspirit",
            "fap-st-lukes",
        }
    # A rebuild from stored documents leaves the loan out of the structurer as well, so it cannot
    # publish the fields again, and holds the sheet when nothing else is left.
    engine = make_engine_with_hospital(MERCY_MERCED, MERCY_SAN_JUAN, LONGMONT)
    with session_scope(engine) as session:
        store_docs(session, "050444", MERCY_SAN_JUAN_APPLICATION)
        publish_quoting(session, "050444", MERCY_SAN_JUAN_APPLICATION[0])
        lend(session, "060003", "050444", "application-mercy-san-juan")
        publish_quoting(session, "060003", MERCY_SAN_JUAN_APPLICATION[0])
        quoted = [path for path, _ in repo.latest_sheet(session, "060003")[0].field_paths()]
        result = build_hospital(session, NoSpend(), NoModel(), "060003", TODAY, reuse_sources=True)
        assert (result.outcome, result.version) == ("held", 2)
        assert result.notes == [LENT_NOTE, NOTHING_LEFT_NOTE, held_note(quoted)]
        assert [s.id for s, _ in repo.sources_for(session, "060003")] == [
            "application-mercy-san-juan"
        ]
