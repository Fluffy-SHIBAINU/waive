"""Find and fetch a hospital's financial assistance documents (spec §8 step 3)."""

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.discover import host_of
from waive.atlas.fetch import DOCUMENT_HOSTS, download_text, host_allowed_for
from waive.atlas.schema import HospitalRef, SourceDoc, SourceKind
from waive.atlas.tavily_gateway import ExtractedPage, SearchHit, TavilyGateway

DocClass = Literal["fap", "application", "summary", "billing"]
CLASS_ORDER: tuple[DocClass, ...] = ("fap", "application", "summary", "billing")
MAX_URLS = 4
MAX_LINKED_URLS = 4
MAX_CHARS = 60_000
MIN_CHARS = 200
# A web page Tavily's basic extraction rendered shorter than this is tried once more at the
# advanced depth, then downloaded directly (task 2.8h; Cape Cod Hospital's page: 643 characters
# of menu). Two credits per five pages for the advanced pass, none for the download.
THIN_CHARS = 1_000
# Markdown links as Tavily Extract renders them: [label](url) or [label](url "title"); images are
# skipped. Bare PDF URLs are picked up separately.
MARKDOWN_LINK = re.compile(r'(?<!!)\[([^\]]*)\]\(\s*<?([^\s<>()"]+)>?(?:\s+"[^"]*")?\s*\)')
BARE_PDF_URL = re.compile(r"""https?://[^\s<>()\[\]"']+\.pdf""", re.IGNORECASE)
LINK_KEYWORDS = (
    "financial assistance",
    "financial-assistance",
    "financialassistance",
    "charity",
    "policy",
    "application",
    "plain language",
    "plain-language",
    ".pdf",
)
FAP_TOKEN = re.compile(r"(?<![a-z0-9])fap(?![a-z0-9])")
# Asset-host links (fetch.DOCUMENT_HOSTS) are followed only when the label names the policy.
OFFSITE_KEYWORDS = (
    "financial assistance",
    "financial-assistance",
    "financialassistance",
    "charity",
    "billing and collection",
    "billing-and-collection",
)
IMAGE_SUFFIXES = (".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico")
QUERIES = (
    "financial assistance policy charity care free discounted care",
    "financial assistance application form plain language summary billing and collections policy",
)
MAP_PATHS = [r".*financial.*", r".*charity.*", r".*assistance.*", r".*billing.*", r".*fap.*"]
TITLES = {
    "fap": "Financial Assistance Policy",
    "application": "Financial Assistance Application",
    "summary": "Plain Language Summary",
    "billing": "Billing and Collections Policy",
}


def classify_doc(url: str, title: str) -> DocClass | None:
    text = f"{url} {title}".lower().replace("_", "-")
    # Asset hosts put the MIME type in the query string; "application/pdf" is not an application.
    text = text.replace("application%2fpdf", " ").replace("application/pdf", " ")
    financial = any(k in text for k in ("financial", "charity", "fap", "assistance"))
    if financial and any(k in text for k in ("applic", "form")):
        return "application"
    if financial and any(k in text for k in ("plain", "summary")):
        return "summary"
    if any(k in text for k in ("billing", "collection", "prices-and-billing", "pay-your-bill")):
        return "billing"
    if any(
        k in text
        for k in (
            "financial-assistance",
            "financial assistance",
            "charity",
            "/fap",
            "financialassistance",
        )
    ):
        return "fap"
    return None


def select_urls(hits: list[SearchHit]) -> list[tuple[str, DocClass]]:
    classified: list[tuple[str, DocClass, float]] = []
    seen: set[str] = set()
    for hit in sorted(hits, key=lambda h: h.score, reverse=True):
        if hit.url in seen:
            continue
        seen.add(hit.url)
        doc_class = classify_doc(hit.url, hit.title)
        if doc_class is not None:
            classified.append((hit.url, doc_class, hit.score))
    chosen: list[tuple[str, DocClass]] = []
    for wanted in CLASS_ORDER:
        for url, doc_class, _score in classified:
            if doc_class == wanted and (url, doc_class) not in chosen:
                chosen.append((url, doc_class))
                break
    for url, doc_class, _score in classified:
        if len(chosen) >= MAX_URLS:
            break
        if (url, doc_class) not in chosen:
            chosen.append((url, doc_class))
    return chosen[:MAX_URLS]


def policy_links(text: str, base_domain: str) -> list[tuple[str, str]]:
    """Links in extracted page text that look like policy documents: (url, label) pairs.

    Keeps markdown links and bare PDF URLs whose label or URL mentions financial assistance,
    charity, policy, application, plain language, FAP or a PDF; resolves relative URLs against
    the hospital's site; drops other registered domains (subdomains are kept, and so are clearly
    labelled policies on a DOCUMENT_HOSTS asset host); skips anchors, icons and the site root;
    de-duplicates.
    """
    base_url = f"https://www.{base_domain}"
    base_host = host_of(base_url)
    found: dict[str, str] = {}
    candidates = [(label, url) for label, url in MARKDOWN_LINK.findall(text)]
    candidates.extend(("", url) for url in BARE_PDF_URL.findall(text))
    for raw_label, raw_url in candidates:
        raw_url = raw_url.strip()
        if raw_url.startswith("#"):
            continue  # an in-page anchor (accordion toggles are rendered this way)
        url, _fragment = urldefrag(urljoin(base_url, raw_url))
        parts = urlparse(url)
        if (
            parts.scheme not in ("http", "https")
            or parts.path in ("", "/")
            or parts.path.lower().endswith(IMAGE_SUFFIXES)
        ):
            continue
        label = " ".join(raw_label.replace("#", " ").split())
        haystack = f"{label} {url}".lower().replace("_", "-")
        if host_of(url) == base_host:
            wanted = any(k in haystack for k in LINK_KEYWORDS) or FAP_TOKEN.search(haystack)
        elif _is_document_host(parts.netloc.lower()):
            wanted = any(k in label.lower() for k in OFFSITE_KEYWORDS) or FAP_TOKEN.search(
                label.lower()
            )
        else:
            wanted = False
        if not wanted:
            continue
        if url not in found or (label and not found[url]):
            found[url] = label
    return list(found.items())


def _is_document_host(netloc: str) -> bool:
    host = netloc.split(":")[0]
    return any(host == d or host.endswith("." + d) for d in DOCUMENT_HOSTS)


def _is_pdf(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


def _linked_documents(
    texts: dict[str, str], base_domain: str, fetched: set[str]
) -> list[tuple[str, DocClass, str]]:
    """Policy documents one link away from the web pages already fetched, best first."""
    candidates: dict[str, tuple[DocClass, str]] = {}
    for page_url, text in texts.items():
        if _is_pdf(page_url):
            continue
        for url, label in policy_links(text, base_domain):
            if url in fetched or url in candidates:
                continue
            doc_class = classify_doc(url, label)
            if doc_class is not None:
                candidates[url] = (doc_class, label)
    ranked = sorted(candidates.items(), key=lambda item: CLASS_ORDER.index(item[1][0]))
    return [(url, doc_class, label) for url, (doc_class, label) in ranked[:MAX_LINKED_URLS]]


@dataclass(frozen=True)
class ScoutedDoc:
    url: str
    doc_class: DocClass
    title: str
    text: str
    sha256: str


def _scouted(url: str, doc_class: DocClass, title: str, text: str) -> ScoutedDoc | None:
    text = text[:MAX_CHARS]
    if len(text.strip()) < MIN_CHARS:
        return None
    return ScoutedDoc(url, doc_class, title, text, hashlib.sha256(text.encode("utf-8")).hexdigest())


class Downloader:
    """Direct downloads for documents Tavily Extract returned empty (asset-host PDFs, task 2.8c)
    or rendered as navigation only (web pages, task 2.8h), kept to the hospital's own domain and
    the asset hosts.

    Opens one HTTP client on first use when none was given, and closes only what it opened.
    """

    def __init__(self, http: httpx.Client | None, domain: str | None) -> None:
        self._http = http
        self._owned: httpx.Client | None = None
        self._allowed = host_allowed_for(domain)

    def text(self, url: str) -> str | None:
        if self._http is None:
            self._http = self._owned = httpx.Client(timeout=30.0)
        return download_text(url, self._http, allowed=self._allowed, allow_html=True)

    def close(self) -> None:
        if self._owned is not None:
            self._owned.close()


def _is_thin(url: str, text: str | None) -> bool:
    """A web page Tavily rendered, but mostly as its menu. A PDF's text is what it is, and no text
    at all is a fetch failure, which the download handles without another extraction."""
    return 0 < len((text or "").strip()) < THIN_CHARS and not _is_pdf(url)


def fill_texts(
    gateway: TavilyGateway,
    urls: list[str],
    pages: list[ExtractedPage],
    downloads: Downloader,
    *,
    purpose: str,
) -> dict[str, str]:
    """Text by URL for `urls`, starting from Tavily's basic extraction `pages`: web pages that came
    back thin are re-extracted once at the advanced depth (two credits per five pages), and
    documents still thin, missing or nearly empty are downloaded directly (no Tavily spend). A
    longer text replaces a shorter one; nothing already fetched is thrown away."""
    by_url = {page.url: page.text for page in pages}
    thin = [url for url in urls if _is_thin(url, by_url.get(url))]
    if thin:
        for page in gateway.extract(thin, purpose=purpose, depth="advanced"):
            if len(page.text.strip()) > len((by_url.get(page.url) or "").strip()):
                by_url[page.url] = page.text
    for url in urls:
        text = by_url.get(url) or ""
        if len(text.strip()) < MIN_CHARS or _is_thin(url, text):
            downloaded = downloads.text(url)
            if downloaded and len(downloaded.strip()) > len(text.strip()):
                by_url[url] = downloaded
    return by_url


def scout_hospital(
    gateway: TavilyGateway, hospital: HospitalRef, http: httpx.Client | None = None
) -> list[ScoutedDoc]:
    if not hospital.website_domain:
        return []
    hits: list[SearchHit] = []
    for query in QUERIES:
        hits.extend(
            gateway.search(
                f"{hospital.name} {query}",
                purpose="atlas.scout",
                include_domains=[hospital.website_domain],
                max_results=8,
            )
        )
    selected = select_urls(hits)
    if not any(doc_class == "fap" for _, doc_class in selected):
        # Search indexes miss many policy pages; map the site and look at the paths.
        mapped = gateway.map(
            f"https://www.{hospital.website_domain}",
            purpose="atlas.scout",
            select_paths=MAP_PATHS,
            limit=30,
        )
        hits.extend(SearchHit(url=url, title="", content="", score=0.5) for url in mapped)
        selected = select_urls(hits)
    if not selected:
        return []
    downloads = Downloader(http, hospital.website_domain)
    try:
        return _fetch_documents(gateway, hospital.website_domain, hits, selected, downloads)
    finally:
        downloads.close()


def _fetch_documents(
    gateway: TavilyGateway,
    domain: str,
    hits: list[SearchHit],
    selected: list[tuple[str, DocClass]],
    downloads: Downloader,
) -> list[ScoutedDoc]:
    urls = [url for url, _ in selected]
    pages = gateway.extract(urls, purpose="atlas.scout")
    by_url = fill_texts(gateway, urls, pages, downloads, purpose="atlas.scout")
    docs: list[ScoutedDoc] = []
    for url, doc_class in selected:
        title = next((h.title for h in hits if h.url == url and h.title), TITLES[doc_class])
        if doc := _scouted(url, doc_class, title, by_url.get(url) or ""):
            docs.append(doc)

    # Entry pages usually only link to the policy; follow those links one step on the same site.
    linked = _linked_documents(by_url, domain, set(urls))
    if linked:
        linked_urls = [url for url, _, _ in linked]
        more = gateway.extract(linked_urls, purpose="atlas.scout")
        by_url = fill_texts(gateway, linked_urls, more, downloads, purpose="atlas.scout")
        seen = {doc.sha256 for doc in docs}
        for url, doc_class, label in linked:
            doc = _scouted(url, doc_class, label or TITLES[doc_class], by_url.get(url) or "")
            if doc and doc.sha256 not in seen:
                docs.append(doc)
                seen.add(doc.sha256)
    return docs


def source_id_for(doc_class: str, sha256: str) -> str:
    return f"{doc_class}-{sha256[:16]}"


def store_scouted(
    session: Session, ccn: str, docs: list[ScoutedDoc], today: date
) -> list[SourceDoc]:
    stored: list[SourceDoc] = []
    for doc in docs:
        source = SourceDoc(
            id=source_id_for(doc.doc_class, doc.sha256),
            kind=SourceKind.HOSPITAL_WEB,
            url=doc.url,
            title=doc.title,
            fetched_on=today,
            sha256=doc.sha256,
        )
        repo.save_source(session, source, doc.text, ccn)
        stored.append(source)
    return stored
