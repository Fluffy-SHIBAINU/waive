"""Find each hospital's official website domain with Tavily Search (spec §8 step 2)."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.schema import HospitalRef
from waive.atlas.states import STATE_NAME, US_STATES, states_named
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
        # Task 7.15a: directories, an association, a newsroom and a staffing firm the national
        # batches took for hospital sites. lownhospitalsindex.org (three OSF hospitals, 140012,
        # 140179 and 141310), hospitalbills411.com (Keck Hospital of USC, 050696),
        # healthbillcentral.com (Loyola, 140276, and the Hospital of the University of
        # Pennsylvania, 390111) and brightbillapp.com (OSF Saint Elizabeth, 140110) list hospitals;
        # calhospital.org is the California Hospital Association (for Community Memorial Ojai,
        # 051334), njspotlightnews.org a newsroom (Heights University Hospital, 310016),
        # communityplatform.us a county resource directory (CHRISTUS Santa Rosa, 450237) and
        # vituity.com a physician staffing firm (Memorial Hospital Los Banos, 050528).
        "lownhospitalsindex.org",
        "hospitalbills411.com",
        "healthbillcentral.com",
        "brightbillapp.com",
        "calhospital.org",
        "njspotlightnews.org",
        "communityplatform.us",
        "vituity.com",
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
# What a page that merely mentions the hospital in its body is worth (a page title naming it, or
# its phone on the page, is worth 1.0).
MENTION_EVIDENCE = 0.3
# A domain an admin confirmed in the console (or set by hand as known to be right): the website
# and place checks below stand down for it.
CONFIRMED_CONFIDENCE = 1.0


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


# The registry writes "ST JOSEPHS", pages "St. Joseph's" (a straight or a curly apostrophe): both
# sides lose their apostrophes before any matching, or a possessive saint's name is never found
# (review of 7.15b: St. Joseph's Hospital and Medical Center, Phoenix, lost all its evidence).
_APOSTROPHES = re.compile("['\u2019]")


def _plain(text: str) -> str:
    """`text` lower-case, without apostrophes."""
    return _APOSTROPHES.sub("", text.lower())


def name_tokens(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", _plain(name))
    return {w for w in words if len(w) >= 4 and w not in GENERIC_WORDS}


def evidence_tokens(name: str) -> set[str]:
    """The words a page must carry to name the hospital: its distinctive tokens, or, when fewer
    than two are left ("Akron General Medical Center" keeps only "akron"), every word of four
    letters or more, so a children's hospital in the same town does not pass on the town's name
    alone (360027, task 7.9). The generic words only add to the distinctive one: _names_hospital
    wants that one too (7.15a: "community" and "hospital" stood in for "bernardino")."""
    tokens = name_tokens(name)
    if len(tokens) >= 2:
        return tokens
    return {w for w in re.findall(r"[a-z0-9]+", _plain(name)) if len(w) >= 4}


def _names_hospital(hospital: HospitalRef, text: str) -> bool:
    """Whether `text` names the hospital: two of its evidence tokens, among them every distinctive
    word of a name that has fewer than two. Community Hospital of San Bernardino (050089) was
    matched to Mountains Community Hospital's page at confidence 0.9 (7.15a)."""
    text = _plain(text)
    tokens = evidence_tokens(hospital.name)
    distinctive = name_tokens(hospital.name)
    if not tokens or (len(distinctive) < 2 and not all(token in text for token in distinctive)):
        return False
    return sum(token in text for token in tokens) >= min(2, len(tokens))


def _phone_on(hospital: HospitalRef, hit: SearchHit) -> bool:
    digits = re.sub(r"\D", "", hospital.phone or "")
    return bool(digits) and digits in re.sub(r"\D", "", f"{hit.title} {hit.content}")


def _hit_text(hit: SearchHit) -> str:
    return f"{hit.title} {hit.content} {hit.url}"


def _names_city(hospital: HospitalRef, text: str) -> bool:
    """Whether `text` names the hospital's town as whole words ("Dover" is not in "Andover")."""
    city = " ".join(_plain(hospital.city).split())
    return bool(city) and re.search(rf"\b{re.escape(city)}\b", _plain(text)) is not None


def _evidence(hospital: HospitalRef, hit: SearchHit) -> float:
    """1.0 when the page title names the hospital or its phone appears; 0.3 for a mere mention
    in the page body (a referral or news page), which is not proof of an official site. A name
    with no distinctive word at all ("COMMUNITY MEDICAL CENTER") is named by a title only when
    the hit names the hospital's town too: Fresno's Community Medical Centers is not Community
    Medical Center in Toms River (310041, 7.15a)."""
    if _phone_on(hospital, hit):
        return 1.0
    title_can_prove = bool(name_tokens(hospital.name)) or _names_city(hospital, _hit_text(hit))
    if title_can_prove and _names_hospital(hospital, hit.title):
        return 1.0
    if _names_hospital(hospital, hit.content):
        return MENTION_EVIDENCE
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
    lowered = _plain(blob)
    words = set(re.findall(r"[a-z0-9]+", lowered))
    if _plain(hospital.city) not in lowered and not (name_tokens(hospital.name) & words):
        return f"the documents name neither {hospital.city.title()} nor the hospital"
    return None


# Discovery is sure of a site (0.9) when a page title names the hospital and the hit names its
# place, or a page carries its phone; otherwise it guesses (0.6). Of about 20 sheets published from
# a guess, three were another hospital's website on day one (7.11a: Bellevue Hospital OH from
# Overlake's in Bellevue, Washington; Bryn Mawr from bonsecours.com; St. Joseph Bethpage from St.
# Joseph's Health in Syracuse). Decision 7.9a-1: such a sheet is held until an admin confirms the
# site. A title alone was not enough either (7.15a): "Financial Assistance | Good Samaritan
# Hospital" is San Jose's page, not Cincinnati's.
DOMAIN_UNCONFIRMED_REASON = (
    "the hospital's website was matched with low confidence and is not confirmed yet"
)


def domain_unconfirmed(domain: str | None, confidence: float | None) -> bool:
    """Whether a sheet built from `domain` waits for an admin: the site was matched with less
    than MIN_CONFIDENCE, or set with no confidence recorded, and nobody confirmed it (1.0).
    Without a domain there is nothing to confirm."""
    if not domain or confidence == CONFIRMED_CONFIDENCE:
        return False
    return confidence is None or confidence < MIN_CONFIDENCE


def rejected_domains(session: Session, ccn: str) -> frozenset[str]:
    """The websites an admin found are not this hospital's (`clear_wrong_domain` records each on
    a `domain` review item, open or not). 7.11a left three hospitals paused because rediscovery
    would likely have found the same wrong site again."""
    items = repo.review_items_of_kind(session, ccn, "domain")
    return frozenset(item.detail["cleared"] for item in items if item.detail.get("cleared"))


# Task 7.15a: discovery chose same-named hospitals elsewhere, at confidence 0.9. Good Samaritan
# Hospital in Cincinnati (360134) got goodsamsanjose.com, whose title beat TriHealth's page with
# the Cincinnati address; Mission Regional in Mission, Texas (450176) got Mission Health in
# Asheville, Huntington Hospital on Long Island (330045) Cabell Huntington in West Virginia, and
# North Shore University Hospital in Manhasset (330106) NorthShore's page, which names Illinois.
# A hit that names the hospital (even in passing) and its place scores PLACE_BONUS more. One that
# names other states but not the hospital's scores OTHER_STATE_PENALTY less and is never sure; it
# is not skipped, because right sites name neighbouring states too (review of 7.15b: The Christ
# Hospital's "Greater Cincinnati, Northern Kentucky and Southeast Indiana", Barnes-Jewish's
# "teaching hospital of Washington University", CVPH's "University of Vermont Health Network"),
# and skipping them left those hospitals with no site at all.
PLACE_BONUS = 0.8
OTHER_STATE_PENALTY = 1.0
# A state's name used for a street is an address, not the state ("85 Washington Street").
_STATE_STREET = re.compile(
    rf"\b(?:{STATE_NAME}) (?:street|st|avenue|ave|road|rd|boulevard|blvd|drive|dr|lane|ln|way"
    r"|place|pl|court|ct|parkway|pkwy|pike|square|sq|terrace)\b"
)


def _states_in(hospital: HospitalRef, text: str) -> tuple[bool, set[str]]:
    """Whether `text` names the hospital's state, and which other states it names. A state is
    named written out, or the hospital's own also as its code after a comma or before a ZIP
    ("Cincinnati, OH", "OH 45220") as place_unconfirmed reads it; a state's name used for a street
    is no state. The hospital's own state counts wherever it is named, its own name included
    (Massachusetts General); another state inside its name or town ("Kansas City", "Sierra Nevada
    Memorial") is not one the page names."""
    state = hospital.state.upper()
    named = states_named(_STATE_STREET.sub(" ", " ".join(text.lower().split())))
    own = state in named or re.search(rf",\s*{state}\b|\b{state}\s+\d{{5}}\b", text) is not None
    return own, named - states_named(f"{hospital.name} {hospital.city}") - {state}


def names_place(hospital: HospitalRef, hit: SearchHit) -> bool:
    """Whether a hit names where the hospital is: its state, or its town when the town's words
    are none of the hospital's name tokens. Huntington Hospital is named for its town, so every
    Huntington Hospital's page names "Huntington"; Mission Regional in Mission likewise."""
    text = _hit_text(hit)
    if _states_in(hospital, text)[0]:
        return True
    city_words = set(re.findall(r"[a-z0-9]+", _plain(hospital.city)))
    return not city_words & name_tokens(hospital.name) and _names_city(hospital, text)


def names_only_other_states(hospital: HospitalRef, hit: SearchHit) -> bool:
    """Whether a hit names one or more states, none of them the hospital's."""
    own, other = _states_in(hospital, _hit_text(hit))
    return bool(other) and not own


def pick_domain(
    hospital: HospitalRef, hits: list[SearchHit], rejected: Iterable[str] = ()
) -> DomainResult | None:
    """The best hit's site, never a directory, an asset host (cloudinary.com, given to Mercy
    Harvard, 141335, holds several health systems' PDFs) or a site an admin rejected. Sure (0.9)
    only when the title names the hospital and the hit names its place, or the phone matches,
    and never when the hit names other states but not the hospital's."""
    from waive.atlas.fetch import is_asset_host  # fetch imports host_of from this module

    refused = frozenset(rejected)
    best: DomainResult | None = None
    best_score = float("-inf")
    for hit in hits:
        host = host_of(hit.url)
        if not host or is_directory(host) or is_asset_host(hit.url) or host in refused:
            continue
        elsewhere = names_only_other_states(hospital, hit)
        evidence = _evidence(hospital, hit)
        placed = not elsewhere and names_place(hospital, hit)
        score = hit.score + evidence
        if evidence >= MENTION_EVIDENCE and placed:
            score += PLACE_BONUS
        if elsewhere:
            score -= OTHER_STATE_PENALTY
        if score > best_score:
            best_score = score
            sure = not elsewhere and evidence >= 1.0 and (placed or _phone_on(hospital, hit))
            best = DomainResult(host, 0.9 if sure else 0.6, hit.url)
    return best


def discover_domain(
    gateway: TavilyGateway, hospital: HospitalRef, rejected: Iterable[str] = ()
) -> DomainResult | None:
    query = f"{hospital.name} {hospital.city} {hospital.state} hospital financial assistance"
    hits = gateway.search(query, purpose="atlas.discover", max_results=5)
    return pick_domain(hospital, hits, rejected)


def run_discovery(
    session: Session, gateway: TavilyGateway, state: str, limit: int | None = None
) -> list[tuple[str, DomainResult | None]]:
    results: list[tuple[str, DomainResult | None]] = []
    rows = repo.list_hospitals(session, state=state, missing_domain=True)
    for row in rows[:limit]:
        result = discover_domain(
            gateway, repo.hospital_ref(row), rejected_domains(session, row.ccn)
        )
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
