import hashlib
import json
from datetime import date

import httpx
import pytest
import respx
from pydantic import SecretStr

from waive.ai.client import AIClient, AIOutputError, AIRequestRejected
from waive.atlas import repo
from waive.atlas.pipeline import build_hospital, build_state, coverage_report
from waive.atlas.samples import SAMPLE_POLICY_TEXT, SAMPLE_SOURCE_ID
from waive.atlas.schema import SheetStatus, SourceDoc, SourceKind
from waive.atlas.scout import source_id_for
from waive.atlas.structure import DraftField, SheetDraft
from waive.atlas.tavily_gateway import ExtractedPage, SearchHit
from waive.atlas.verify import PATIENT_SHARE_REASON
from waive.config import Settings
from waive.db import init_db, make_engine, session_scope
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


REAL_HOSPITAL = {**HOSPITAL, "ccn": "220031", "name": "REAL GENERAL HOSPITAL", "city": "WORCESTER"}


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


@pytest.mark.parametrize(
    ("error", "calls"),
    [
        (AIRequestRejected(TOO_LONG), 2),  # the full prompt, then the smaller one
        (AIOutputError("model output did not match SheetDraft (free_care_max_fpl: dict_type)"), 1),
    ],
    ids=["rejected-request", "unusable-output"],
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


def test_build_state_skips_hospitals_with_sheets_and_reports():
    engine = make_engine_with_hospital(REAL_HOSPITAL)
    with session_scope(engine) as session:
        first = build_state(session, FakeGateway(), FakeAI(), "MA", TODAY)
        second = build_state(session, FakeGateway(), FakeAI(), "MA", TODAY)
        assert [r.outcome for r in first] == ["published"]
        assert second == []
        report = coverage_report(session, "MA")
        assert "| 220031 |" in report and "published" in report


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
