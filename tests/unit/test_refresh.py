import hashlib
from datetime import timedelta

import httpx
import respx

from waive.atlas import repo
from waive.atlas.pipeline import build_hospital
from waive.atlas.refresh import doc_class_of, fetch_texts, is_asset_host, refresh_hospital
from waive.atlas.schema import SourceDoc, SourceKind
from waive.atlas.scout import source_id_for
from waive.atlas.tavily_gateway import ExtractedPage
from waive.db import SourceDocRow, session_scope

from tests.unit.test_fetch import HTML_PAGE, pdf_response, sample_pdf
from tests.unit.test_pipeline import (
    OTHER_SENTENCE,
    POLICY_TEXT,
    TODAY,
    FakeAI,
    FakeGateway,
    make_engine_with_hospital,
)
from tests.unit.test_scout import NAV_ONLY

LATER = TODAY + timedelta(days=20)
POLICY_URL = "https://www.example.org/financial-assistance-policy.pdf"
PAGE_URL = "https://www.example.org/patients/financial-assistance"
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
        texts = fetch_texts(gateway, [POLICY_URL], http, domain="example.org")
    assert gateway.extracted == [[POLICY_URL]]
    assert "250% of the Federal Poverty" in texts[POLICY_URL]


@respx.mock
def test_a_document_that_redirects_off_the_allowed_hosts_is_unreachable_not_stored():
    """A stored asset-host link (a bucket somebody else can claim) is re-fetched on every tick;
    wherever it redirects, nothing off the hospital's domain or the asset hosts is requested."""
    engine = make_engine_with_hospital()
    respx.get(CANTO_URL).mock(
        return_value=httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/report.pdf"}
        )
    )
    leaked = respx.get("http://169.254.169.254/latest/report.pdf").mock(
        return_value=pdf_response(sample_pdf())
    )
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
        result = refresh_hospital(session, gateway, FakeAI(), "229999", LATER, http)
        assert (result.outcome, result.unreachable, result.changed) == (
            "unchanged",
            [stored.id],
            [],
        )
        assert not leaked.called
        [(source, text)] = repo.sources_for(session, "229999")
        assert source.id == stored.id and text == "old text from a previous download"


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


class DepthRefreshGateway(RefreshGateway):
    """Basic extraction renders the policy page as navigation only; the advanced depth gets the
    page (task 2.8h). Records (urls, depth) for every call."""

    def __init__(self, texts, advanced):
        super().__init__(texts)
        self.advanced = advanced
        self.calls = []

    def extract(self, urls, **kwargs):
        depth = kwargs.get("depth", "basic")
        self.calls.append((list(urls), depth))
        table = self.advanced if depth == "advanced" else self.texts
        return [ExtractedPage(url, table.get(url, "")) for url in urls]


def test_thin_pages_are_re_extracted_at_advanced_depth_before_the_hash_is_compared():
    """A page the scout only got at the advanced depth must not read as "changed" (and be
    re-structured from its menu) every time the refresh extracts it at the basic depth."""
    engine = make_engine_with_hospital()
    page_text = POLICY_TEXT + "Financial counselors are available Monday through Friday.\n"
    stored = SourceDoc(
        id=source_id_for("fap", hashlib.sha256(page_text.encode("utf-8")).hexdigest()),
        kind=SourceKind.HOSPITAL_WEB,
        url=PAGE_URL,
        title="Financial Assistance",
        fetched_on=TODAY,
        sha256=hashlib.sha256(page_text.encode("utf-8")).hexdigest(),
    )
    gateway = DepthRefreshGateway({PAGE_URL: NAV_ONLY}, {PAGE_URL: page_text})
    with session_scope(engine) as session, httpx.Client() as http:
        repo.save_source(session, stored, page_text, "229999")
        result = refresh_hospital(session, gateway, FakeAI(), "229999", LATER, http)
        assert (result.outcome, result.checked, result.changed) == ("unchanged", 1, [])
        assert gateway.calls == [([PAGE_URL], "basic"), ([PAGE_URL], "advanced")]
        [(source, _)] = repo.sources_for(session, "229999")
        assert source.id == stored.id and source.fetched_on == LATER


@respx.mock
def test_the_refresh_re_fetches_at_the_configured_depth_with_no_second_pass():
    """7.10: with WAIVE_SCOUT_EXTRACT_DEPTH=advanced the refresh extracts at that depth once, like
    the scout did; a page that stays thin is downloaded, never re-extracted (no double spend)."""
    engine = make_engine_with_hospital()
    page_text = POLICY_TEXT + "Financial counselors are available Monday through Friday.\n"
    sha = hashlib.sha256(page_text.encode("utf-8")).hexdigest()
    stored = SourceDoc(
        id=source_id_for("fap", sha),
        kind=SourceKind.HOSPITAL_WEB,
        url=PAGE_URL,
        title="Financial Assistance",
        fetched_on=TODAY,
        sha256=sha,
    )
    gateway = DepthRefreshGateway({}, {PAGE_URL: page_text})
    with session_scope(engine) as session, httpx.Client() as http:
        repo.save_source(session, stored, page_text, "229999")
        result = refresh_hospital(
            session, gateway, FakeAI(), "229999", LATER, http, extract_depth="advanced"
        )
        assert (result.outcome, result.checked, result.changed) == ("unchanged", 1, [])
        assert gateway.calls == [([PAGE_URL], "advanced")]
        [(source, _)] = repo.sources_for(session, "229999")
        assert source.fetched_on == LATER
    page = respx.get(PAGE_URL).mock(
        return_value=httpx.Response(200, text=HTML_PAGE, headers={"content-type": "text/html"})
    )
    gateway = DepthRefreshGateway({}, {PAGE_URL: NAV_ONLY})
    with httpx.Client() as http:
        texts = fetch_texts(
            gateway, [PAGE_URL], http, domain="example.org", extract_depth="advanced"
        )
    assert page.called
    assert gateway.calls == [([PAGE_URL], "advanced")]
    assert "250% of the Federal Poverty" in texts[PAGE_URL]
