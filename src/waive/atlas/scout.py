"""Find and fetch a hospital's financial assistance documents (spec §8 step 3)."""

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.schema import HospitalRef, SourceDoc, SourceKind
from waive.atlas.tavily_gateway import SearchHit, TavilyGateway

DocClass = Literal["fap", "application", "summary", "billing"]
CLASS_ORDER: tuple[DocClass, ...] = ("fap", "application", "summary", "billing")
MAX_URLS = 4
MAX_CHARS = 60_000
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


@dataclass(frozen=True)
class ScoutedDoc:
    url: str
    doc_class: DocClass
    title: str
    text: str
    sha256: str


def scout_hospital(gateway: TavilyGateway, hospital: HospitalRef) -> list[ScoutedDoc]:
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
    pages = gateway.extract([url for url, _ in selected], purpose="atlas.scout")
    by_url = {page.url: page.text for page in pages}
    docs: list[ScoutedDoc] = []
    for url, doc_class in selected:
        text = (by_url.get(url) or "")[:MAX_CHARS]
        if len(text.strip()) < 200:
            continue
        title = next((h.title for h in hits if h.url == url and h.title), TITLES[doc_class])
        docs.append(
            ScoutedDoc(
                url, doc_class, title, text, hashlib.sha256(text.encode("utf-8")).hexdigest()
            )
        )
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
