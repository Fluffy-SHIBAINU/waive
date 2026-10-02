from datetime import date

from waive.ai.client import AIOutputError
from waive.atlas import repo
from waive.atlas.pipeline import build_hospital, build_state, coverage_report
from waive.atlas.samples import SAMPLE_POLICY_TEXT, SAMPLE_SOURCE_ID
from waive.atlas.schema import SheetStatus
from waive.atlas.structure import DraftField, SheetDraft
from waive.atlas.tavily_gateway import ExtractedPage, SearchHit
from waive.db import init_db, make_engine, session_scope

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
        return [ExtractedPage(url, SAMPLE_POLICY_TEXT) for url in urls]


class FakeAI:
    def __init__(self, free_limit_for_fast="250"):
        self.free_limit_for_fast = free_limit_for_fast

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        source_id = messages[1]["content"].split("=== SOURCE id=")[1].split(" ")[0]
        value = "250" if role == "reason" else self.free_limit_for_fast
        return SheetDraft(
            free_care_max_fpl=DraftField(
                value=value,
                quote="household income at or below 250% of the Federal Poverty Guidelines are eligible for free care",
                source_id=source_id,
            ),
            phone=DraftField(
                value="617-555-0100", quote="Questions: call 617-555-0100", source_id=source_id
            ),
            hours=DraftField(
                value="9-5", quote="this quote is not in the document", source_id=source_id
            ),
        )


def make_engine_with_hospital():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
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


def test_build_hospital_holds_sheet_on_critical_conflict():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(
            session, FakeGateway(), FakeAI(free_limit_for_fast="300"), "229999", TODAY
        )
        assert result.outcome == "held"
        kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
        assert kinds == ["conflict", "verification"]


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


def test_build_state_skips_hospitals_with_sheets_and_reports():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        first = build_state(session, FakeGateway(), FakeAI(), "MA", TODAY)
        second = build_state(session, FakeGateway(), FakeAI(), "MA", TODAY)
        assert [r.outcome for r in first] == ["published"]
        assert second == []
        report = coverage_report(session, "MA")
        assert "| 229999 |" in report and "published" in report
