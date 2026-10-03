import hashlib
from datetime import timedelta

import httpx
import respx

from waive.atlas import repo
from waive.atlas.pipeline import build_hospital
from waive.atlas.refresh import doc_class_of, fetch_texts, is_asset_host, refresh_hospital
from waive.atlas.schema import SourceDoc, SourceKind
from waive.atlas.tavily_gateway import ExtractedPage
from waive.db import SourceDocRow, session_scope

from tests.unit.test_fetch import pdf_response, sample_pdf
from tests.unit.test_pipeline import (
    OTHER_SENTENCE,
    POLICY_TEXT,
    TODAY,
    FakeAI,
    FakeGateway,
    make_engine_with_hospital,
)

LATER = TODAY + timedelta(days=20)
POLICY_URL = "https://www.example.org/financial-assistance-policy.pdf"
REVISED_TEXT = POLICY_TEXT + "Revised October 2026.\n"
CANTO_URL = "https://h.canto.com/direct/document/abc/def/original?content-type=application%2Fpdf"


class RefreshGateway(FakeGateway):
    """Extract answers from a table of current texts and records what it was asked for."""

    def __init__(self, texts):
        self.texts = texts
        self.extracted = []

    def extract(self, urls, **kwargs):
        self.extracted.append(list(urls))
        return [ExtractedPage(url, self.texts.get(url, "")) for url in urls]


class RevisedAI(FakeAI):
    """Reads 300% once the revised document is in the prompt (quoting the sentence that holds it)."""

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        limit = "300" if "Revised October 2026" in messages[1]["content"] else "250"
        self.free_limits = {"reason": limit, "fast": limit, "tiebreak": limit}
        return super().complete_json(role, messages, schema, phi=phi, purpose=purpose)


def seeded_engine():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        assert build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY).version == 1
    return engine


def test_doc_class_of_and_asset_hosts():
    assert doc_class_of("fap-0123456789abcdef") == "fap"
    assert doc_class_of("application-0123") == "application"
    assert doc_class_of("no-such-class") == "fap"
    assert is_asset_host("https://baystatehealth.canto.com/direct/document/x")
    assert not is_asset_host("https://www.example.org/fap.pdf")


def test_unchanged_documents_touch_the_source_and_create_no_version():
    engine = seeded_engine()
    gateway = RefreshGateway({POLICY_URL: POLICY_TEXT})
    with session_scope(engine) as session, httpx.Client() as http:
        result = refresh_hospital(session, gateway, FakeAI(), "229999", LATER, http)
        assert (result.outcome, result.checked, result.changed, result.unreachable) == (
            "unchanged",
            1,
            [],
            [],
        )
        assert gateway.extracted == [[POLICY_URL]]
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.version == 1
        [(source, _text)] = repo.sources_for(session, "229999")
        assert source.fetched_on == LATER  # staleness resets without a spurious version


def test_changed_document_is_stored_relinked_and_restructured():
    engine = seeded_engine()
    gateway = RefreshGateway({POLICY_URL: REVISED_TEXT})
    with session_scope(engine) as session, httpx.Client() as http:
        [(old, _)] = repo.sources_for(session, "229999")
        result = refresh_hospital(session, gateway, RevisedAI(), "229999", LATER, http)
        assert result.outcome == "restructured"
        assert result.changed == [old.id]
        assert result.build is not None and result.build.version == 2
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.free_care_max_fpl.value == 300
        assert sheet.eligibility.free_care_max_fpl.quote == OTHER_SENTENCE
        [(current, text)] = repo.sources_for(session, "229999")
        assert current.id != old.id and current.id.startswith("fap-")
        assert current.sha256 == hashlib.sha256(REVISED_TEXT.encode("utf-8")).hexdigest()
        assert text == REVISED_TEXT and current.fetched_on == LATER
        assert session.get(SourceDocRow, old.id) is not None  # version 1 still cites it
        assert [s.id for s in sheet.sources] == [current.id]


@respx.mock
def test_asset_host_documents_are_downloaded_without_tavily():
    engine = make_engine_with_hospital()
    pdf = sample_pdf()
    respx.get(CANTO_URL).mock(return_value=pdf_response(pdf, "application/octet-stream"))
    gateway = RefreshGateway({})
    with session_scope(engine) as session, httpx.Client() as http:
        stored = SourceDoc(
            id="fap-" + "c" * 16,
            kind=SourceKind.HOSPITAL_WEB,
            url=CANTO_URL,
            title="FAP",
            fetched_on=TODAY,
            sha256="c" * 64,
        )
        repo.save_source(session, stored, "old text from a previous download", "229999")
        texts = fetch_texts(gateway, [CANTO_URL], http)
        assert "250% of the Federal Poverty" in texts[CANTO_URL]
        assert gateway.extracted == []  # Tavily cannot fetch asset hosts; nothing was spent
        result = refresh_hospital(session, gateway, FakeAI(), "229999", LATER, http)
        assert result.checked == 1 and result.changed == ["fap-" + "c" * 16]


@respx.mock
def test_empty_extract_falls_back_to_a_direct_download():
    respx.get(POLICY_URL).mock(return_value=pdf_response(sample_pdf()))
    gateway = RefreshGateway({POLICY_URL: ""})
    with httpx.Client() as http:
        texts = fetch_texts(gateway, [POLICY_URL], http)
    assert gateway.extracted == [[POLICY_URL]]
    assert "250% of the Federal Poverty" in texts[POLICY_URL]


@respx.mock
def test_unreachable_documents_are_reported_and_leave_the_sheet_alone():
    engine = seeded_engine()
    respx.get(POLICY_URL).mock(return_value=httpx.Response(404))
    gateway = RefreshGateway({POLICY_URL: ""})
    with session_scope(engine) as session, httpx.Client() as http:
        [(stored, _)] = repo.sources_for(session, "229999")
        result = refresh_hospital(session, gateway, FakeAI(), "229999", LATER, http)
        assert (result.outcome, result.unreachable, result.changed) == (
            "unchanged",
            [stored.id],
            [],
        )
        [(source, _)] = repo.sources_for(session, "229999")
        assert source.fetched_on == TODAY  # not touched: we learned nothing about it


def test_hospital_without_web_documents_is_skipped():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = refresh_hospital(session, RefreshGateway({}), FakeAI(), "229999", LATER)
        assert (result.outcome, result.checked) == ("skipped", 0)
        missing = refresh_hospital(session, RefreshGateway({}), FakeAI(), "000000", LATER)
        assert missing.outcome == "failed"
