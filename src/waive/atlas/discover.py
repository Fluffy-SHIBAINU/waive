"""Find each hospital's official website domain with Tavily Search (spec §8 step 2)."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.schema import HospitalRef
from waive.atlas.states import US_STATES, states_named
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
        # A billing directory with one "financial assistance" page per hospital; discovery took
        # it for Harrington Hospital's site (220019), and the four pages scouted there described
        # hospitals in Georgia and Texas (Milford review).
        "billfairly.com",
        # Third-party guides and directories the first national batch mistook for hospital sites
        # (task 7.9): careroute.ai's "financial assistance" summaries were published as three
        # Advocate and AdventHealth sheets; fairvisithealth.com's pages described eight other
        # hospitals; carelistings.com and seniorhealthdatabase.com are listings.
        "careroute.ai",
        "fairvisithealth.com",
        "carelistings.com",
        "seniorhealthdatabase.com",
        # Second national batch: payerprice.com (price transparency listings) and causeiq.com
        # (nonprofit profiles) name the hospital in every page title and were taken for seven
        # hospitals' own sites; merriam-webster.com answered for "Christus".
        "payerprice.com",
        "causeiq.com",
        "merriam-webster.com",
    }
)
# Government hosts (illinois.gov, paauditor.gov, mass.gov, medicare.gov) publish records about
# hospitals, never a hospital's own policy.
DIRECTORY_SUFFIXES = (".gov",)
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
    return host.endswith(DIRECTORY_SUFFIXES) or any(
        host == d or host.endswith("." + d) for d in DIRECTORY_DOMAINS
    )


def name_tokens(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", name.lower())
    return {w for w in words if len(w) >= 4 and w not in GENERIC_WORDS}


def evidence_tokens(name: str) -> set[str]:
    """The words a page must carry to name the hospital: its distinctive tokens, or, when fewer
    than two are left ("Akron General Medical Center" keeps only "akron"), every word of four
    letters or more, so a children's hospital in the same town does not pass on the town's name
    alone (360027, task 7.9)."""
    tokens = name_tokens(name)
    if len(tokens) >= 2:
        return tokens
    return {w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) >= 4}


def _evidence(hospital: HospitalRef, hit: SearchHit) -> float:
    """1.0 when the page title names the hospital or its phone appears; 0.3 for a mere mention
    in the page body (a referral or news page), which is not proof of an official site."""
    tokens = evidence_tokens(hospital.name)
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


# Discovery can land on a same-named hospital somewhere else: Bellevue Hospital in Ohio was
# published from Overlake Medical Center in Bellevue, Washington; Barnesville Hospital in Ohio from
# providence.org; Chilton Medical Center from another New Jersey hospital's site (national batch
# 2). A hospital's own policy names its state in an address or a notice, and names the hospital
# or its town somewhere; measured on the 112 national sheets published on 2026-10-10, five fail
# this and at least three of those five were the wrong site.
PLACE_UNCONFIRMED_REASON = "the documents cannot be tied to this hospital's place"


def place_unconfirmed(hospital: HospitalRef, texts: Iterable[str]) -> str | None:
    """Why the scouted documents cannot be tied to where the hospital is, or None when they can
    (or when there is nothing to judge). The state counts as named when it is written out, or as
    its code after a comma or before a ZIP ("Boston, MA", "MA 02118"); the hospital counts as
    named when its town or any distinctive word of its name appears."""
    blob = " ".join(texts)
    state = hospital.state.upper()
    if not blob.strip() or state not in US_STATES:
        return None
    code = re.search(rf",\s*{state}\b|\b{state}\s+\d{{5}}\b", blob)
    if state not in states_named(blob) and not code:
        return f"the documents never name {US_STATES[state].title()}"
    lowered = blob.lower()
    words = set(re.findall(r"[a-z0-9]+", lowered))
    if hospital.city.lower() not in lowered and not (name_tokens(hospital.name) & words):
        return f"the documents name neither {hospital.city.title()} nor the hospital"
    return None


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
