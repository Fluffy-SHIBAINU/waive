from datetime import date

from waive.ai.client import AIOutputError
from waive.atlas import repo
from waive.atlas.pipeline import build_hospital, build_state, coverage_report
from waive.atlas.samples import SAMPLE_POLICY_TEXT, SAMPLE_SOURCE_ID
from waive.atlas.schema import SheetStatus, SourceDoc, SourceKind
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


def test_build_hospital_holds_sheet_on_critical_conflict():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = build_hospital(
            session, FakeGateway(), FakeAI(free_limit_for_fast="300"), "229999", TODAY
        )
        assert result.outcome == "held"
        assert "critical fields disagree: eligibility.free_care_max_fpl" in result.notes
        kinds = sorted(item.kind for item in repo.open_review_items(session, "229999"))
        assert kinds == ["conflict", "verification"]


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
