"""Content-hash refresh of a hospital's stored documents (spec §8 step 8; §15: refreshes extract
only documents whose hash changed).

Each stored hospital document is fetched again through the channel the scout used — Tavily
Extract for web pages (one credit per five URLs), a direct download for PDFs on asset hosts where
Tavily fails (free). The text is truncated and hashed exactly as the scout did; an equal hash only
moves the source's `fetched_on`, a different hash stores the new document, replaces the old one
on the hospital and re-structures from stored sources (Token Factory only, no Tavily).
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, cast
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from waive.ai.client import AIClient
from waive.atlas import repo
from waive.atlas.fetch import download_text
from waive.atlas.pipeline import BuildResult, build_hospital
from waive.atlas.schema import SourceDoc, SourceKind
from waive.atlas.scout import (
    CLASS_ORDER,
    DOCUMENT_HOSTS,
    MAX_CHARS,
    MIN_CHARS,
    DocClass,
    source_id_for,
)
from waive.atlas.tavily_gateway import TavilyGateway

log = logging.getLogger(__name__)

RefreshOutcome = Literal["unchanged", "restructured", "skipped", "failed"]
PURPOSE = "atlas.refresh"


@dataclass
class RefreshResult:
    ccn: str
    name: str
    outcome: RefreshOutcome
    checked: int = 0
    changed: list[str] = field(default_factory=list)  # ids of the superseded sources
    unreachable: list[str] = field(default_factory=list)  # ids that could not be fetched
    build: BuildResult | None = None


def doc_class_of(source_id: str) -> DocClass:
    """The scout's ids are `<class>-<sha16>`; anything else is treated as a policy."""
    prefix = source_id.split("-", 1)[0]
    return cast(DocClass, prefix) if prefix in CLASS_ORDER else "fap"


def is_asset_host(url: str) -> bool:
    host = urlparse(url).netloc.lower().split(":")[0]
    return any(host == d or host.endswith("." + d) for d in DOCUMENT_HOSTS)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_texts(gateway: TavilyGateway, urls: list[str], http: httpx.Client) -> dict[str, str]:
    """Current text per URL, truncated like the scout's. Asset-host URLs are downloaded directly
    (no credits); the rest go through one Tavily Extract call, and any URL that comes back
    empty is downloaded as a fallback. URLs that yield nothing are absent from the result."""
    texts: dict[str, str] = {}
    direct = [url for url in urls if is_asset_host(url)]
    pages = [url for url in urls if url not in direct]
    for url in direct:
        if text := download_text(url, http):
            texts[url] = text
    if pages:
        for page in gateway.extract(pages, purpose=PURPOSE):
            if len(page.text.strip()) >= MIN_CHARS:
                texts[page.url] = page.text
        for url in pages:
            if url not in texts and (text := download_text(url, http)):
                texts[url] = text
    return {url: text[:MAX_CHARS] for url, text in texts.items()}


def refresh_hospital(
    session: Session,
    gateway: TavilyGateway,
    ai: AIClient,
    ccn: str,
    today: date,
    http: httpx.Client | None = None,
) -> RefreshResult:
    row = repo.get_hospital(session, ccn)
    if row is None:
        return RefreshResult(ccn, "?", "failed")
    current = [
        (source, text)
        for source, text in repo.sources_for(session, ccn)
        if source.kind is SourceKind.HOSPITAL_WEB and source.url
    ]
    if not current:
        return RefreshResult(ccn, row.name, "skipped")
    owned = http is None
    client = http or httpx.Client(follow_redirects=True, timeout=30.0)
    try:
        texts = fetch_texts(gateway, [source.url for source, _ in current], client)
    finally:
        if owned:
            client.close()

    result = RefreshResult(ccn, row.name, "unchanged", checked=len(current))
    for source, _old_text in current:
        text = texts.get(source.url or "")
        if text is None:
            result.unreachable.append(source.id)
            continue
        sha = _hash(text)
        if sha == source.sha256:
            repo.touch_source(session, source.id, today)
            continue
        replacement = SourceDoc(
            id=source_id_for(doc_class_of(source.id), sha),
            kind=SourceKind.HOSPITAL_WEB,
            url=source.url,
            title=source.title,
            fetched_on=today,
            sha256=sha,
        )
        repo.save_source(session, replacement, text, ccn)
        repo.unlink_source(session, ccn, source.id)
        result.changed.append(source.id)
        log.info("document changed for %s: %s", ccn, source.url)
    if result.changed:
        result.build = build_hospital(session, gateway, ai, ccn, today, reuse_sources=True)
        result.outcome = "restructured"
    return result
