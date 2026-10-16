import hashlib
import json
from datetime import date

import httpx
import pytest
import respx
from pydantic import SecretStr

from waive.ai.client import AIClient, AIOutputError, AIRequestRejected
from waive.atlas import repo
from waive.atlas.discover import MIN_CONFIDENCE
from waive.atlas.pipeline import build_hospital, build_state, clear_wrong_domain, coverage_report
from waive.atlas.samples import SAMPLE_POLICY_TEXT, SAMPLE_SOURCE_ID
from waive.atlas.schema import ProcedureSheet, SheetStatus, SourceDoc, SourceKind
from waive.atlas.scout import source_id_for
from waive.atlas.structure import DraftField, SheetDraft
from waive.atlas.tavily_gateway import ExtractedPage, SearchHit
from waive.atlas.verify import PATIENT_SHARE_REASON, verify_sheet
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
    # When every stored document is another state's there is nothing to prefer: all stay.
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        repo.get_hospital(session, "229999").website_domain = "example.org"
        repo.save_source(session, hawaii, hawaii_text, "229999")
        ai = GroundedAI()
        build_hospital(session, FakeGateway(), ai, "229999", TODAY, reuse_sources=True)
        assert ai.prompts and all("fap-hawaii" in p for p in ai.prompts)
        assert "fap-hawaii" in {s.id for s, _ in repo.sources_for(session, "229999")}


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
        assert result.notes == [
            DENTAL_NOTE,
            NOTHING_LEFT_NOTE,
            f"{len(quoted)} fields quoted from them dropped and the sheet held: "
            + ", ".join(quoted),
        ]
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
            f"{len(quoted)} fields quoted from them dropped and the sheet held: "
            + ", ".join(quoted),
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
