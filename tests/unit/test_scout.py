import hashlib
from datetime import date
from decimal import Decimal

import httpx
import respx

from waive.atlas import repo
from waive.atlas.fetch import html_text
from waive.atlas.samples import SAMPLE_POLICY_TEXT, st_example_sheet
from waive.atlas.schema import SourceKind
from waive.atlas.scout import (
    MIN_CHARS,
    THIN_CHARS,
    ScoutedDoc,
    classify_doc,
    policy_links,
    scout_hospital,
    select_urls,
    source_id_for,
    store_scouted,
)
from waive.atlas.tavily_gateway import ExtractedPage, SearchHit, TavilyGateway
from waive.db import init_db, make_engine, session_scope
from waive.governor import BudgetExceeded, Governor, Ledger

from tests.unit.test_fetch import CANTO_URL, HTML_PAGE, sample_pdf

HOSPITAL = st_example_sheet().hospital
# Tavily renders most hospital pages at well over THIN_CHARS characters; shorter pages are
# re-extracted at the advanced depth (task 2.8h). The fakes' web pages carry this paragraph so
# the tests of link following and de-duplication stay about links and duplicates.
BODY_TEXT = (
    "Financial counselors are available Monday through Friday from 8 a.m. to 5 p.m. in the "
    "admitting office on the first floor of the main building and by phone. They help patients "
    "apply for MassHealth, the Health Safety Net and the hospital's own financial assistance "
    "program, explain the documents an application needs (a recent tax return, two pay stubs or "
    "a letter from an employer, and proof of address), and arrange interest-free payment plans "
    "for balances that remain after assistance. Applications are accepted at any time, "
    "including after a bill has been sent to a collection agency, and a decision is mailed "
    "within thirty days of a complete application. Patients who disagree with a decision may "
    "ask for a review by the director of patient financial services. Assistance covers "
    "emergency and medically necessary care at every hospital location, but not cosmetic "
    "procedures or services a physician has not ordered.\n"
)
PAGE_TEXT = SAMPLE_POLICY_TEXT + "\n" + BODY_TEXT
assert len(PAGE_TEXT) >= 1_000 and len(BODY_TEXT) >= 800


def hit(url, title="", score=0.5):
    return SearchHit(url=url, title=title, content="", score=score)


def test_classify_doc():
    assert classify_doc("https://x.org/financial-assistance-policy.pdf", "") == "fap"
    assert (
        classify_doc("https://x.org/fa/application.pdf", "Financial Assistance Application")
        == "application"
    )
    assert classify_doc("https://x.org/plain-language-summary", "Financial assistance") == "summary"
    assert classify_doc("https://x.org/billing-and-collections-policy.pdf", "") == "billing"
    assert classify_doc("https://x.org/careers", "Jobs") is None


def test_classify_doc_ignores_the_pdf_mime_type_in_asset_host_urls():
    canto = "https://h.canto.com/direct/document/a/b/original?content-type=application%2Fpdf&name="
    assert (
        classify_doc(canto + "FAP+Policy.pdf", "Hospital financial assistance policy (pdf)")
        == "fap"
    )
    assert (
        classify_doc(
            canto + "Plain+Language+Summary.pdf", "Financial assistance plain language summary"
        )
        == "summary"
    )


def test_select_urls_one_per_class_then_extras_max_four():
    hits = [
        hit("https://x.org/fap-a.pdf", "Financial Assistance Policy", 0.9),
        hit("https://x.org/fap-b.pdf", "Charity Care Policy", 0.8),
        hit("https://x.org/application.pdf", "Financial assistance application", 0.7),
        hit("https://x.org/summary", "Plain language summary financial assistance", 0.6),
        hit("https://x.org/billing-policy", "Billing and collection policy", 0.5),
        hit("https://x.org/jobs", "Careers", 0.99),
    ]
    selected = select_urls(hits)
    assert selected == [
        ("https://x.org/fap-a.pdf", "fap"),
        ("https://x.org/application.pdf", "application"),
        ("https://x.org/summary", "summary"),
        ("https://x.org/billing-policy", "billing"),
    ]


class FakeGateway:
    def __init__(self):
        self.extracted = []

    def search(self, query, **kwargs):
        assert kwargs["include_domains"] == ["example.org"]
        return [
            hit(
                "https://www.example.org/financial-assistance-policy.pdf",
                "Financial Assistance Policy",
                0.9,
            ),
            hit("https://www.example.org/financial-assistance-policy.pdf", "duplicate", 0.8),
            hit("https://www.example.org/application.pdf", "Financial Assistance Application", 0.7),
        ]

    def extract(self, urls, **kwargs):
        self.extracted.append(urls)
        return [ExtractedPage(url=url, text=SAMPLE_POLICY_TEXT) for url in urls]


def test_scout_hospital_dedupes_and_extracts_selected_urls():
    gateway = FakeGateway()
    docs = scout_hospital(gateway, HOSPITAL)
    assert [doc.doc_class for doc in docs] == ["fap", "application"]
    assert gateway.extracted == [
        [
            "https://www.example.org/financial-assistance-policy.pdf",
            "https://www.example.org/application.pdf",
        ]
    ]
    assert docs[0].sha256 == hashlib.sha256(SAMPLE_POLICY_TEXT.encode()).hexdigest()


class MapGateway:
    def __init__(self):
        self.extracted = []
        self.mapped = []

    def search(self, query, **kwargs):
        return [
            hit(
                "https://www.example.org/patients/healthcare-prices-and-billing",
                "Healthcare Prices & Billing",
                0.6,
            )
        ]

    def map(self, url, **kwargs):
        self.mapped.append((url, kwargs.get("select_paths")))
        return [
            "https://www.example.org/patients/financial-assistance-policy.pdf",
            "https://www.example.org/careers",
        ]

    def extract(self, urls, **kwargs):
        self.extracted.append(urls)
        return [ExtractedPage(url=url, text=PAGE_TEXT) for url in urls]


def test_scout_falls_back_to_site_map_when_search_finds_no_policy():
    gateway = MapGateway()
    docs = scout_hospital(gateway, HOSPITAL)
    assert gateway.mapped[0][0] == "https://www.example.org"
    assert [doc.doc_class for doc in docs] == ["fap", "billing"]
    assert gateway.extracted == [
        [
            "https://www.example.org/patients/financial-assistance-policy.pdf",
            "https://www.example.org/patients/healthcare-prices-and-billing",
        ]
    ]


def test_scout_hospital_without_domain_returns_nothing():
    assert scout_hospital(FakeGateway(), HOSPITAL.model_copy(update={"website_domain": None})) == []


def test_policy_links_keeps_policy_links_on_the_hospital_domain():
    text = (
        "# Financial Assistance\n"
        "[Financial Assistance Policy (PDF)](https://www.example.org/docs/fap.pdf)\n"
        "[Application](/patients/financial-assistance-application.pdf)\n"
        "[Plain language summary](https://billing.example.org/summary)\n"
        "[Charity care policy](https://www.otherhospital.org/charity.pdf)\n"
        "[Careers](https://www.example.org/careers)\n"
        "[Email us](mailto:billing@example.org)\n"
        "Also https://www.example.org/docs/fap.pdf#page=2 and https://www.example.org/forms/fa.pdf.\n"
    )
    assert policy_links(text, "example.org") == [
        ("https://www.example.org/docs/fap.pdf", "Financial Assistance Policy (PDF)"),
        ("https://www.example.org/patients/financial-assistance-application.pdf", "Application"),
        ("https://billing.example.org/summary", "Plain language summary"),
        ("https://www.example.org/forms/fa.pdf", ""),
    ]


def test_policy_links_ignores_accordion_toggles_icons_and_the_site_root():
    text = (
        "[### Financial Assistance Information\n\n![](/assets/icons/plus.svg)](#)\n"
        "[### Ayuda económica (Financial Assistance Information in Spanish)](#)\n"
        "[Financial assistance policy](/)\n"
        "[Financial assistance policy (pdf)](/documents/fap.pdf)\n"
    )
    assert policy_links(text, "example.org") == [
        ("https://www.example.org/documents/fap.pdf", "Financial assistance policy (pdf)")
    ]


def test_policy_links_follows_clearly_labelled_documents_on_asset_hosts_only():
    """Baystate keeps its policies on baystatehealth.canto.com, a document host without .pdf
    URLs. Follow such links when the label names the policy; never follow another site's."""
    text = (
        "[Hospital financial assistance policy (pdf)](https://h.canto.com/direct/document/a/origin)\n"
        "[Hospital billing and collections policy (pdf)](https://h.canto.com/direct/document/b/origin)\n"
        "[Provider listing (pdf)](https://h.canto.com/direct/document/c/origin)\n"
        "[Individual and family application](https://www.mahealthconnector.org/)\n"
        "[Financial assistance policy](https://www.otherhospital.org/fap.pdf)\n"
    )
    assert policy_links(text, "example.org") == [
        (
            "https://h.canto.com/direct/document/a/origin",
            "Hospital financial assistance policy (pdf)",
        ),
        (
            "https://h.canto.com/direct/document/b/origin",
            "Hospital billing and collections policy (pdf)",
        ),
    ]


ENTRY_PAGE = (
    "# Financial Assistance\n"
    "St. Example helps patients who cannot afford their care. Read the full policy and the "
    "application form below, or call 617-555-0100 for help in any language. Interpreters are "
    "available at no cost. Our partner hospital publishes its own charity care policy.\n"
    + BODY_TEXT
    + "[Financial Assistance Policy (PDF)](https://www.example.org/docs/fap.pdf)\n"
    "[Partner charity care policy](https://www.otherhospital.org/charity-care-policy.pdf)\n"
)


class LinkGateway:
    """Search finds only the HTML entry page; its text links to the policy PDF."""

    def __init__(self, entry_text=ENTRY_PAGE):
        self.entry_text = entry_text
        self.extracted = []

    def search(self, query, **kwargs):
        return [
            hit(
                "https://www.example.org/patients/financial-assistance", "Financial Assistance", 0.9
            )
        ]

    def extract(self, urls, **kwargs):
        self.extracted.append(urls)
        if len(self.extracted) == 1:
            return [ExtractedPage(url=url, text=self.entry_text) for url in urls]
        return [ExtractedPage(url=url, text=SAMPLE_POLICY_TEXT) for url in urls]


def test_scout_follows_policy_links_from_entry_pages_on_the_same_domain():
    gateway = LinkGateway()
    docs = scout_hospital(gateway, HOSPITAL)
    assert gateway.extracted == [
        ["https://www.example.org/patients/financial-assistance"],
        ["https://www.example.org/docs/fap.pdf"],
    ]
    assert [(doc.url, doc.doc_class) for doc in docs] == [
        ("https://www.example.org/patients/financial-assistance", "fap"),
        ("https://www.example.org/docs/fap.pdf", "fap"),
    ]
    assert docs[1].title == "Financial Assistance Policy (PDF)"
    assert docs[1].sha256 == hashlib.sha256(SAMPLE_POLICY_TEXT.encode()).hexdigest()


def test_scout_follows_at_most_four_links_preferring_policy_then_application():
    entry = ENTRY_PAGE + (
        "[Billing and collections policy](/billing-policy)\n"
        "[Financial assistance plain language summary](/fa/summary)\n"
        "[Financial assistance application (English)](/fa/application-en.pdf)\n"
        "[Financial assistance application (Spanish)](/fa/application-es.pdf)\n"
        "[Charity care policy](/fa/charity-care-policy.pdf)\n"
        "[Financial Assistance](/patients/financial-assistance)\n"
    )
    gateway = LinkGateway(entry)
    scout_hospital(gateway, HOSPITAL)
    assert gateway.extracted[1] == [
        "https://www.example.org/docs/fap.pdf",
        "https://www.example.org/fa/charity-care-policy.pdf",
        "https://www.example.org/fa/application-en.pdf",
        "https://www.example.org/fa/application-es.pdf",
    ]


class SamePageGateway(LinkGateway):
    """The linked PDF turns out to be the very text the entry page already showed."""

    def extract(self, urls, **kwargs):
        self.extracted.append(urls)
        return [ExtractedPage(url=url, text=self.entry_text) for url in urls]


def test_scout_skips_followed_documents_identical_to_ones_already_fetched():
    gateway = SamePageGateway(PAGE_TEXT + "[Policy (PDF)](https://www.example.org/fap.pdf)\n")
    docs = scout_hospital(gateway, HOSPITAL)
    assert len(gateway.extracted) == 2
    assert [doc.url for doc in docs] == ["https://www.example.org/patients/financial-assistance"]


ASSET_ENTRY_PAGE = (
    "# Healthcare prices and billing\n"
    "Baystate-style entry page: the policy lives on a document host Tavily cannot fetch.\n"
    + BODY_TEXT
    + f"[Hospital financial assistance policy (pdf)]({CANTO_URL})\n"
)


class AssetHostGateway(LinkGateway):
    """Tavily Extract returns no result at all for the asset-host PDF ("Failed to fetch url")."""

    def __init__(self):
        super().__init__(ASSET_ENTRY_PAGE)

    def extract(self, urls, **kwargs):
        self.extracted.append(urls)
        if len(self.extracted) == 1:
            return [ExtractedPage(url=url, text=self.entry_text) for url in urls]
        return []


def pdf_route(url=CANTO_URL):
    return respx.get(url).mock(
        return_value=httpx.Response(
            200, content=sample_pdf(), headers={"content-type": "application/pdf"}
        )
    )


@respx.mock
def test_scout_downloads_linked_pdfs_tavily_cannot_fetch():
    route = pdf_route()
    gateway = AssetHostGateway()
    docs = scout_hospital(gateway, HOSPITAL)
    assert gateway.extracted[1] == [CANTO_URL]
    assert route.called
    assert [(doc.url, doc.doc_class) for doc in docs] == [
        ("https://www.example.org/patients/financial-assistance", "fap"),
        (CANTO_URL, "fap"),
    ]
    assert docs[1].title == "Hospital financial assistance policy (pdf)"
    assert "250% of the Federal Poverty" in docs[1].text
    assert docs[1].sha256 == hashlib.sha256(docs[1].text.encode()).hexdigest()


class EmptyExtractGateway(FakeGateway):
    """Search finds the PDFs directly, but Extract returns them without text."""

    def extract(self, urls, **kwargs):
        self.extracted.append(urls)
        return [ExtractedPage(url=url, text="") for url in urls]


@respx.mock
def test_scout_downloads_selected_documents_extracted_without_text():
    fap = pdf_route("https://www.example.org/financial-assistance-policy.pdf")
    respx.get("https://www.example.org/application.pdf").mock(return_value=httpx.Response(404))
    gateway = EmptyExtractGateway()
    with httpx.Client() as http:
        docs = scout_hospital(gateway, HOSPITAL, http=http)
    assert fap.called
    assert [(doc.url, doc.doc_class, doc.title) for doc in docs] == [
        (
            "https://www.example.org/financial-assistance-policy.pdf",
            "fap",
            "Financial Assistance Policy",
        )
    ]
    assert "250% of the Federal Poverty" in docs[0].text
    assert len(gateway.extracted) == 1


@respx.mock
def test_scout_does_not_download_when_tavily_already_has_the_text():
    route = pdf_route("https://www.example.org/financial-assistance-policy.pdf")
    scout_hospital(FakeGateway(), HOSPITAL)
    assert not route.called


def test_store_scouted_dedupes_shared_documents():
    sha = hashlib.sha256(SAMPLE_POLICY_TEXT.encode()).hexdigest()
    doc = ScoutedDoc(
        "https://www.example.org/fap.pdf",
        "fap",
        "Financial Assistance Policy",
        SAMPLE_POLICY_TEXT,
        sha,
    )
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    base = {
        "name": "X",
        "city": "BOSTON",
        "state": "MA",
        "zip": "02118",
        "hospital_type": "Acute Care Hospitals",
        "ownership": "Voluntary non-profit - Private",
    }
    with session_scope(engine) as session:
        repo.upsert_hospital(session, {**base, "ccn": "1"})
        repo.upsert_hospital(session, {**base, "ccn": "2"})
        [source] = store_scouted(session, "1", [doc], date(2026, 10, 2))
        store_scouted(session, "2", [doc], date(2026, 10, 2))
        assert source.id == source_id_for("fap", sha)
        assert source.kind is SourceKind.HOSPITAL_WEB
    with session_scope(engine) as session:
        assert len(repo.sources_for(session, "1")) == 1
        assert len(repo.sources_for(session, "2")) == 1


ENTRY_URL = "https://www.example.org/patients/financial-assistance"
FAP_PDF = "https://www.example.org/docs/fap.pdf"
# What Tavily's basic extraction returns for Cape Cod Hospital's financial-assistance page: the
# site menu, 643 characters, and not a word of the policy (task 2.8h).
NAV_ONLY = (
    "[Skip to Content](#Content)\n\n[Set Your Location](#SetLocation)\n\n"
    "Your location is...  [Change Your Location](#SetLocation)\n\n"
    "Set Your Location to See Relevant Information\n\n"
    "*Setting your location helps us to show you nearby providers and locations based on your "
    "healthcare needs.*\n\n[MyChart](https://mychart.example.org/MyChart/Authentication/Login)\n\n"
    "What can we help you find?\n\n* [Find a doctor](/our-providers/)\n"
    "* [Find a location](/locations-and-directions/)\n* [Browse our services](/medical-services/)\n"
    "* [Pay a bill](/patients-visitors/paying-for-care/)\n"
)
assert MIN_CHARS <= len(NAV_ONLY) < THIN_CHARS


class DepthGateway:
    """Extract answers from one table per depth and records (urls, depth) for every call."""

    def __init__(self, basic, advanced):
        self.basic, self.advanced = basic, advanced
        self.calls = []

    def search(self, query, **kwargs):
        return [hit(ENTRY_URL, "Financial Assistance", 0.9)]

    def extract(self, urls, **kwargs):
        depth = kwargs.get("depth", "basic")
        self.calls.append((list(urls), depth))
        table = self.advanced if depth == "advanced" else self.basic
        return [ExtractedPage(url=url, text=table[url]) for url in urls if url in table]


def html_route(url=ENTRY_URL, body=HTML_PAGE):
    return respx.get(url).mock(
        return_value=httpx.Response(200, text=body, headers={"content-type": "text/html"})
    )


@respx.mock
def test_thin_pages_are_re_extracted_once_at_advanced_depth():
    page = html_route()
    gateway = DepthGateway(
        basic={ENTRY_URL: NAV_ONLY, FAP_PDF: SAMPLE_POLICY_TEXT}, advanced={ENTRY_URL: ENTRY_PAGE}
    )
    docs = scout_hospital(gateway, HOSPITAL)
    # The advanced rendering is long enough, so no download; its links are the ones followed.
    assert gateway.calls == [
        ([ENTRY_URL], "basic"),
        ([ENTRY_URL], "advanced"),
        ([FAP_PDF], "basic"),
    ]
    assert not page.called
    assert [(doc.url, doc.doc_class, doc.text) for doc in docs] == [
        (ENTRY_URL, "fap", ENTRY_PAGE),
        (FAP_PDF, "fap", SAMPLE_POLICY_TEXT),
    ]


@respx.mock
def test_pages_still_thin_after_advanced_extraction_are_downloaded_and_tag_stripped():
    page = html_route()
    gateway = DepthGateway(
        basic={ENTRY_URL: NAV_ONLY, FAP_PDF: SAMPLE_POLICY_TEXT}, advanced={ENTRY_URL: NAV_ONLY}
    )
    with httpx.Client() as http:
        docs = scout_hospital(gateway, HOSPITAL, http=http)
    assert page.called
    assert gateway.calls == [
        ([ENTRY_URL], "basic"),
        ([ENTRY_URL], "advanced"),
        ([FAP_PDF], "basic"),  # the link to the PDF came from the downloaded page
    ]
    assert [(doc.url, doc.doc_class) for doc in docs] == [(ENTRY_URL, "fap"), (FAP_PDF, "fap")]
    assert "250% of the Federal Poverty" in docs[0].text
    assert f"[Financial Assistance Policy (PDF)]({FAP_PDF})" in docs[0].text
    assert "dataLayer" not in docs[0].text


@respx.mock
def test_thin_pages_keep_their_text_when_neither_fallback_improves_on_it():
    respx.get(ENTRY_URL).mock(return_value=httpx.Response(404))
    # Advanced extraction fails for the page (no result) ...
    gateway = DepthGateway(basic={ENTRY_URL: NAV_ONLY}, advanced={})
    docs = scout_hospital(gateway, HOSPITAL)
    assert gateway.calls == [([ENTRY_URL], "basic"), ([ENTRY_URL], "advanced")]
    assert [doc.text for doc in docs] == [NAV_ONLY]
    # ... or renders even less: whatever is longest stays, and it is tried only once.
    gateway = DepthGateway(
        basic={ENTRY_URL: NAV_ONLY}, advanced={ENTRY_URL: "Financial Assistance"}
    )
    assert [doc.text for doc in scout_hospital(gateway, HOSPITAL)] == [NAV_ONLY]
    assert len(gateway.calls) == 2


@respx.mock
def test_short_pdfs_and_pages_tavily_could_not_fetch_are_not_re_extracted():
    """The advanced depth renders web pages; a short PDF is just short, and a page Tavily could
    not fetch at all goes straight to the download (task 2.8c)."""
    gateway = DepthGateway(basic={FAP_PDF: SAMPLE_POLICY_TEXT}, advanced={FAP_PDF: PAGE_TEXT})
    gateway.search = lambda query, **kwargs: [hit(FAP_PDF, "Financial Assistance Policy", 0.9)]
    assert [doc.text for doc in scout_hospital(gateway, HOSPITAL)] == [SAMPLE_POLICY_TEXT]
    assert gateway.calls == [([FAP_PDF], "basic")]
    page = respx.get(ENTRY_URL).mock(return_value=httpx.Response(404))
    gateway = DepthGateway(basic={}, advanced={ENTRY_URL: ENTRY_PAGE})
    assert scout_hospital(gateway, HOSPITAL) == []
    assert gateway.calls == [([ENTRY_URL], "basic")] and page.called


# What a hospital CMS serves, with HTTP 200, for a policy URL that does not exist or a page that
# needs JavaScript: the menu (which names financial assistance), a footer (which names a policy and
# an application) and not a word about the policy itself (2.8h review).
SHELL_HTML = (
    "<html><head><title>Page not found</title></head><body><nav><ul><li><a href='/'>Home</a></li>"
    "<li><a href='/our-providers/'>Find a doctor</a></li>"
    "<li><a href='/locations-and-directions/'>Find a location</a></li>"
    "<li><a href='/patients-visitors/financial-assistance/'>Financial Assistance</a></li>"
    "<li><a href='/patients-visitors/paying-for-care/'>Pay a bill</a></li></ul></nav>"
    "<main><h1>Page not found</h1><p>Please enable JavaScript to continue.</p></main>"
    "<footer><a href='/privacy-policy/'>Privacy policy</a> "
    "<a href='/careers/application.pdf'>Employment application (PDF)</a></footer></body></html>"
)
# A short entry page that does talk about the policy and links to it relative to its own URL.
RELATIVE_PDF = "https://www.example.org/patients/docs/fap.pdf"
SHORT_ENTRY_HTML = (
    "<html><body><nav><a href='/our-providers/'>Find a doctor</a></nav>"
    "<main><h1>Financial Assistance</h1><p>The hospital offers free and discounted care to "
    "patients who cannot pay under its financial assistance policy, whatever their insurance. "
    "Read the <a href='docs/fap.pdf'>Financial Assistance Policy (PDF)</a> or call "
    "508-555-0100 to ask for an application by mail.</p></main></body></html>"
)


@respx.mock
def test_a_navigation_shell_downloaded_for_a_page_tavily_could_not_fetch_is_not_stored():
    page = html_route(body=SHELL_HTML)
    shell = html_text(SHELL_HTML, base_url=ENTRY_URL)
    assert MIN_CHARS <= len(shell) < THIN_CHARS  # long enough to pass as a document before
    gateway = DepthGateway(basic={}, advanced={})
    with httpx.Client() as http:
        docs = scout_hospital(gateway, HOSPITAL, http=http)
    assert page.called
    assert docs == []
    assert gateway.calls == [([ENTRY_URL], "basic")]  # nothing to follow, nothing re-extracted


@respx.mock
def test_a_short_downloaded_page_about_the_policy_is_kept_and_its_relative_links_followed():
    page = html_route(body=SHORT_ENTRY_HTML)
    gateway = DepthGateway(basic={RELATIVE_PDF: SAMPLE_POLICY_TEXT}, advanced={})
    with httpx.Client() as http:
        docs = scout_hospital(gateway, HOSPITAL, http=http)
    assert page.called
    assert gateway.calls == [([ENTRY_URL], "basic"), ([RELATIVE_PDF], "basic")]
    assert [(doc.url, doc.doc_class) for doc in docs] == [(ENTRY_URL, "fap"), (RELATIVE_PDF, "fap")]
    assert MIN_CHARS <= len(docs[0].text) < THIN_CHARS
    assert f"[Financial Assistance Policy (PDF)]({RELATIVE_PDF})" in docs[0].text


class CappedGateway(DepthGateway):
    """The advanced extraction would pass the hard credit cap."""

    def extract(self, urls, **kwargs):
        if kwargs.get("depth") == "advanced":
            self.calls.append((list(urls), "advanced"))
            raise BudgetExceeded("Tavily cap of 1000 credits would be exceeded (used 999).")
        return super().extract(urls, **kwargs)


@respx.mock
def test_a_credit_cap_at_the_advanced_pass_leaves_the_free_download_to_run():
    page = html_route()
    gateway = CappedGateway(basic={ENTRY_URL: NAV_ONLY, FAP_PDF: SAMPLE_POLICY_TEXT}, advanced={})
    with httpx.Client() as http:
        docs = scout_hospital(gateway, HOSPITAL, http=http)
    assert page.called
    assert gateway.calls == [
        ([ENTRY_URL], "basic"),
        ([ENTRY_URL], "advanced"),
        ([FAP_PDF], "basic"),
    ]
    assert [(doc.url, doc.doc_class) for doc in docs] == [(ENTRY_URL, "fap"), (FAP_PDF, "fap")]
    assert "250% of the Federal Poverty" in docs[0].text


class ThinTavily:
    """A Tavily client whose basic extraction of the policy page is navigation only."""

    def search(self, query, **kwargs):
        return {"results": [{"url": ENTRY_URL, "title": "Financial Assistance", "score": 0.9}]}

    def extract(self, urls, **kwargs):
        text = PAGE_TEXT if kwargs["extract_depth"] == "advanced" else NAV_ONLY
        return {"results": [{"url": url, "raw_content": text} for url in urls]}


def test_the_advanced_re_extraction_is_booked_at_two_credits_per_five_pages(tmp_path):
    governor = Governor(Ledger(tmp_path / "usage.jsonl"), 100, Decimal("1"))
    docs = scout_hospital(TavilyGateway(ThinTavily(), governor), HOSPITAL)
    assert [doc.text for doc in docs] == [PAGE_TEXT]
    # Two searches (one credit each), the basic extraction (one) and the advanced one (two).
    assert governor.summary()["tavily"][0] == Decimal("5")
