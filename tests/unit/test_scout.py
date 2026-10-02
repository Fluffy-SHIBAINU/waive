import hashlib
from datetime import date

from waive.atlas import repo
from waive.atlas.samples import SAMPLE_POLICY_TEXT, st_example_sheet
from waive.atlas.schema import SourceKind
from waive.atlas.scout import (
    ScoutedDoc,
    classify_doc,
    policy_links,
    scout_hospital,
    select_urls,
    source_id_for,
    store_scouted,
)
from waive.atlas.tavily_gateway import ExtractedPage, SearchHit
from waive.db import init_db, make_engine, session_scope

HOSPITAL = st_example_sheet().hospital


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
        return [ExtractedPage(url=url, text=SAMPLE_POLICY_TEXT) for url in urls]


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


ENTRY_PAGE = (
    "# Financial Assistance\n"
    "St. Example helps patients who cannot afford their care. Read the full policy and the "
    "application form below, or call 617-555-0100 for help in any language. Interpreters are "
    "available at no cost. Our partner hospital publishes its own charity care policy.\n"
    "[Financial Assistance Policy (PDF)](https://www.example.org/docs/fap.pdf)\n"
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
    gateway = SamePageGateway(
        SAMPLE_POLICY_TEXT + "\n[Policy (PDF)](https://www.example.org/fap.pdf)\n"
    )
    docs = scout_hospital(gateway, HOSPITAL)
    assert len(gateway.extracted) == 2
    assert [doc.url for doc in docs] == ["https://www.example.org/patients/financial-assistance"]


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
