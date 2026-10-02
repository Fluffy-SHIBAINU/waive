"""Find each hospital's official website domain with Tavily Search (spec §8 step 2)."""

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.schema import HospitalRef
from waive.atlas.tavily_gateway import SearchHit, TavilyGateway

DIRECTORY_DOMAINS = frozenset(
    {
        "wikipedia.org",
        "healthgrades.com",
        "usnews.com",
        "yelp.com",
        "facebook.com",
        "linkedin.com",
        "instagram.com",
        "twitter.com",
        "x.com",
        "youtube.com",
        "medicare.gov",
        "cms.gov",
        "indeed.com",
        "glassdoor.com",
        "mapquest.com",
        "yellowpages.com",
        "zocdoc.com",
        "vitals.com",
        "webmd.com",
        "hospitalsafetygrade.org",
        "leapfroggroup.org",
        "ahd.com",
        "definitivehc.com",
        "npino.com",
        "npidb.org",
        "caredash.com",
        "bbb.org",
        "google.com",
        "bing.com",
        "dollarfor.org",
        "mass.gov",
        "wikidata.org",
        "bizapedia.com",
        "opencorporates.com",
        "propublica.org",
        "guidestar.org",
        "candid.org",
        "addictions.com",
        "findhelp.org",
        "networkofcare.org",
        "hospitalstats.org",
        "healthcare4ppl.com",
        "medicare-hospital-ratings.com",
        "211.org",
    }
)
GENERIC_WORDS = frozenset(
    {
        "hospital",
        "hospitals",
        "medical",
        "center",
        "centre",
        "health",
        "healthcare",
        "system",
        "regional",
        "community",
        "memorial",
        "general",
        "campus",
        "the",
        "and",
        "of",
        "inc",
        "saint",
    }
)
MIN_CONFIDENCE = 0.8


@dataclass(frozen=True)
class DomainResult:
    domain: str
    confidence: float
    evidence_url: str


def host_of(url: str) -> str:
    """The registered domain: jobs.bilh.org and planmygift.baystatehealth.org collapse to their
    parent, because policy documents live on the system's main site."""
    host = urlparse(url).netloc.lower().split(":")[0].removeprefix("www.")
    labels = host.split(".")
    if len(labels) > 2:
        host = ".".join(labels[-2:])
    return host


def is_directory(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in DIRECTORY_DOMAINS)


def name_tokens(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", name.lower())
    return {w for w in words if len(w) >= 4 and w not in GENERIC_WORDS}


def _evidence(hospital: HospitalRef, hit: SearchHit) -> float:
    """1.0 when the page title names the hospital or its phone appears; 0.3 for a mere mention
    in the page body (a referral or news page), which is not proof of an official site."""
    tokens = name_tokens(hospital.name)
    needed = min(2, len(tokens))
    title, body = hit.title.lower(), hit.content.lower()
    phone_digits = re.sub(r"\D", "", hospital.phone or "")
    has_phone = bool(phone_digits) and phone_digits in re.sub(
        r"\D", "", f"{hit.title} {hit.content}"
    )
    if has_phone or (tokens and sum(t in title for t in tokens) >= needed):
        return 1.0
    if tokens and sum(t in body for t in tokens) >= needed:
        return 0.3
    return 0.0


def pick_domain(hospital: HospitalRef, hits: list[SearchHit]) -> DomainResult | None:
    best: DomainResult | None = None
    best_score = -1.0
    for hit in hits:
        host = host_of(hit.url)
        if not host or is_directory(host):
            continue
        evidence = _evidence(hospital, hit)
        score = hit.score + evidence
        if score > best_score:
            best_score = score
            best = DomainResult(host, 0.9 if evidence >= 1.0 else 0.6, hit.url)
    return best


def discover_domain(gateway: TavilyGateway, hospital: HospitalRef) -> DomainResult | None:
    query = f"{hospital.name} {hospital.city} {hospital.state} hospital financial assistance"
    hits = gateway.search(query, purpose="atlas.discover", max_results=5)
    return pick_domain(hospital, hits)


def run_discovery(
    session: Session, gateway: TavilyGateway, state: str, limit: int | None = None
) -> list[tuple[str, DomainResult | None]]:
    results: list[tuple[str, DomainResult | None]] = []
    rows = repo.list_hospitals(session, state=state, missing_domain=True)
    for row in rows[:limit]:
        result = discover_domain(gateway, repo.hospital_ref(row))
        if result is not None:
            row.website_domain = result.domain
            row.domain_confidence = result.confidence
        if result is None or result.confidence < MIN_CONFIDENCE:
            repo.add_review_item(
                session,
                row.ccn,
                "domain",
                {
                    "found": None if result is None else result.domain,
                    "evidence": None if result is None else result.evidence_url,
                },
            )
        results.append((row.ccn, result))
    session.flush()
    return results
