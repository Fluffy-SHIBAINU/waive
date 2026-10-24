"""End-to-end atlas build for one hospital or one state (spec §8)."""

import logging
import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import cache
from typing import Any, Literal
from urllib.parse import unquote, urljoin, urlparse

from sqlalchemy.orm import Session

from waive.ai.client import MAX_SERVER_MESSAGE_CHARS, AIClient, AIOutputError
from waive.atlas import repo
from waive.atlas.discover import (
    CONFIRMED_CONFIDENCE,
    DOMAIN_UNCONFIRMED_REASON,
    MIN_CONFIDENCE,
    PLACE_UNCONFIRMED_REASON,
    _names_city,
    _names_hospital,
    _plain,
    discover_domain,
    domain_unconfirmed,
    host_of,
    name_tokens,
    place_unconfirmed,
    rejected_domains,
)
from waive.atlas.eve_import import keep_owner_decision, ruled_ceiling, stored_ruling
from waive.atlas.fetch import is_asset_host
from waive.atlas.publish import (
    carry_over_reported,
    conflict_reason,
    copy_fields,
    critical_conflicts,
    decide_status,
    document_conflicts,
    drop_fields,
    publish_sheet,
    resolve_conflicts,
    sheet_inconsistencies,
)
from waive.atlas.schema import HospitalRef, ProcedureSheet, SheetStatus, SourceDoc, SourceKind
from waive.atlas.scout import (
    is_dental_program,
    navigation_shells,
    scout_hospital,
    store_scouted,
)
from waive.atlas.skiplist import SkipEntry
from waive.atlas.states import CODE_OF_NAME, STATE_NAMES
from waive.atlas.structure import (
    another_kaiser_region,
    document_states,
    is_kaiser_host,
    structure_sheet,
    written_in_english,
)
from waive.atlas.tavily_gateway import Depth, TavilyGateway
from waive.atlas.verify import (
    CEILING_FREE_BAND_REASON,
    DENTAL_REASON,
    FOREIGN_OWNER_REASON,
    HOLDING_REASONS,
    LENT_DOCUMENT_REASON,
    OTHER_REGION_REASON,
    OTHER_STATE_DOCUMENT_REASON,
    PATIENT_SHARE_REASON,
    SCHEDULE_OWNER_REASON,
    SIBLING_POLICY_REASON,
    TRANSLATION_REASON,
    VerificationReport,
    income_limit_problem,
    prune_lists,
    trim_quotes,
    verify_sheet,
)
from waive.db import HospitalRow, ReviewItemRow

log = logging.getLogger(__name__)

Outcome = Literal["published", "held", "skipped", "failed"]
# A document that states income rules names the poverty level; one that does not cannot supply
# a free-care band or a discount table, whatever else it says.
INCOME_RULE_WORDS = re.compile(r"poverty|\bfpl\b|\bfpg\b", re.IGNORECASE)
# A structurer error is the model name, the status and the server's message (at most
# MAX_SERVER_MESSAGE_CHARS); the review detail and the note keep the whole of it.
ERROR_DETAIL_CHARS = MAX_SERVER_MESSAGE_CHARS + 100
# Fields that describe one hospital's own desk, never a system's policy: a sibling's borrowed
# documents cannot supply them (Condell's sheet told patients to return the application to
# Lutheran General's business office, review of 7.9).
BORROWED_SPECIFIC_PATHS = (
    "apply.submit_methods",
    "contacts.phone",
    "contacts.hours",
    "contacts.languages",
    "coverage.facilities",
    "coverage.provider_list_url",
)


@dataclass
class BuildResult:
    ccn: str
    name: str
    outcome: Outcome
    version: int | None = None
    notes: list[str] = field(default_factory=list)


def carry_over_state_programs(
    sheet: ProcedureSheet, previous: ProcedureSheet, set_aside: Collection[str] = ()
) -> ProcedureSheet:
    """A rebuild re-reads hospital documents only; the state overlay (and its source) must survive.
    One quoted from a document the build set aside does not: St. Francis-Emory's (110129) state
    programmes were quoted from Emory Healthcare's policy, which a rebuild detaches."""
    if sheet.programs.state_programs is not None or previous.programs.state_programs is None:
        return sheet
    cited = previous.programs.state_programs
    if cited.source_id in set_aside:
        return sheet
    sources = list(sheet.sources)
    if cited.source_id and all(source.id != cited.source_id for source in sources):
        sources.extend(s for s in previous.sources if s.id == cited.source_id)
    return sheet.model_copy(
        update={
            "programs": sheet.programs.model_copy(update={"state_programs": cited}),
            "sources": sources,
        }
    )


def _drop_navigation_shells(
    session: Session,
    ccn: str,
    sources_with_text: list[tuple[SourceDoc, str]],
    result: BuildResult,
) -> list[tuple[SourceDoc, str]]:
    """Stored pages that are the site's navigation shell (one identical menu page at several
    URLs, see scout.navigation_shells) are detached from the hospital and kept away from the
    structurer, which otherwise fills the schema from outside knowledge and loses every field to
    verification (Milford Regional: three "policy" URLs, one menu). The note says so plainly
    instead of listing fifteen "quote not found" rejections."""
    shells = navigation_shells(
        (source.url or "", text)
        for source, text in sources_with_text
        if source.kind is SourceKind.HOSPITAL_WEB
    )
    if not shells:
        return sources_with_text
    dropped = [source for source, _ in sources_with_text if source.url in shells]
    for source in dropped:
        repo.unlink_source(session, ccn, source.id)
    chars = next(len(text) for source, text in sources_with_text if source.url in shells)
    repo.add_review_item(session, ccn, "navigation_shell", {"urls": sorted(shells), "chars": chars})
    result.notes.append(
        f"{len(dropped)} stored pages are the site's navigation shell, not policy documents "
        f"(the same {chars}-character menu page at {len(shells)} URLs); detached and left out "
        "of the structurer"
    )
    return _without(sources_with_text, {source.id for source in dropped})


def _drop_dental_documents(
    session: Session,
    ccn: str,
    sources_with_text: list[tuple[SourceDoc, str]],
    result: BuildResult,
) -> list[tuple[SourceDoc, str]]:
    """Stored documents of a dental assistance program (scout.is_dental_program) are detached and
    kept from the structurer. Kaiser Northwest's dental policy was stored for fifteen California
    Kaiser hospitals before the scout could tell it from their policy, nine sheets published its
    dental bands (task 7.16), and a rebuild from stored documents would read it again."""
    dental = [
        source
        for source, text in sources_with_text
        if source.kind is SourceKind.HOSPITAL_WEB
        and is_dental_program(source.url or "", source.title, text)
    ]
    if not dental:
        return sources_with_text
    for source in dental:
        repo.unlink_source(session, ccn, source.id)
    detail = [{"id": source.id, "url": source.url} for source in dental]
    repo.add_review_item(session, ccn, "dental_documents", {"sources": detail})
    listed = ", ".join(source.id for source in dental)
    plural = "s are" if len(dental) > 1 else " is"
    result.notes.append(
        f"{len(dental)} stored document{plural} about a dental assistance program, not the "
        f"hospital's financial assistance ({listed}); detached and left out of the structurer"
    )
    return _without(sources_with_text, {source.id for source in dental})


def _drop_other_state_documents(
    session: Session,
    hospital: HospitalRef,
    sources_with_text: list[tuple[SourceDoc, str]],
    result: BuildResult,
) -> list[tuple[SourceDoc, str]]:
    """A health system that publishes one policy per state (Adventist Health: California,
    Hawaii, Oregon) has them side by side on one site, and the scout stores whichever the search
    returns. A document that names only other states is detached and kept from the structurer,
    which cited the Hawaii table on six California sheets (task 7.9) and AdventHealth's Illinois
    page on a Florida sheet (review of 7.9), as long as some document names the hospital's own
    state or none at all. Documents naming no state stay, and when every document is another
    state's nothing is set aside: there is nothing to prefer. Except on Kaiser's sites: there a
    document that names only other states, by its region's marker, its address or its title
    (structure.another_kaiser_region), is another region's policy, set aside even when it is all
    there is rather than publish Kaiser Washington's charity care policy on a California sheet
    (Kaiser San Francisco, 050076, had stored nothing else but the dental policy; task 7.16).
    And anywhere, a document whose address or the subject of its title names only other states
    (other_state_documents) is set aside even when it is all there is: Ascension Providence
    (450042, Waco) had stored only Maryland's, Florida's, Oklahoma's and Illinois's pages and
    published Maryland's free-care limit (the 2026-10-24 audit's ruling). A state named only by a
    code in parentheses or after the title's first segment still waits for something to prefer."""
    ccn, state = hospital.ccn, hospital.state
    named = {
        source.id: document_states(source.title, source.url or "", hospital)
        for source, _ in sources_with_text
        if source.kind is SourceKind.HOSPITAL_WEB
    }
    other = [
        source
        for source, _ in sources_with_text
        if named.get(source.id) and state not in named[source.id]
    ]
    claimed = other_state_documents(hospital, sources_with_text)
    if len(other) == len(sources_with_text):
        other = [
            source for source in other if is_kaiser_host(source.url or "") or source.id in claimed
        ]
    already = {source.id for source in other}
    other += [
        source
        for source, _ in sources_with_text
        if source.id in claimed and source.id not in already
    ]
    if not other:
        return sources_with_text
    for source in other:
        repo.unlink_source(session, ccn, source.id)
    # The states each names besides the hospital's: a title can name both, and clause C reads only
    # its first segment.
    detail = [{"id": source.id, "states": sorted(named[source.id] - {state})} for source in other]
    repo.add_review_item(session, ccn, "other_state_documents", {"state": state, "sources": detail})
    listed = ", ".join(f"{entry['id']}: {'/'.join(entry['states'])}" for entry in detail)
    plural = "s are" if len(other) > 1 else " is"
    result.notes.append(
        f"{len(other)} stored document{plural} another state's version of the system policy "
        f"({listed}); detached and left out of the structurer"
    )
    return _without(sources_with_text, {source.id for source in other})


def _drop_foreign_documents(
    session: Session,
    hospital: HospitalRef,
    sources_with_text: list[tuple[SourceDoc, str]],
    registry: list[HospitalRow],
    result: BuildResult,
) -> list[tuple[SourceDoc, str]]:
    """Another organisation's or another hospital's policy that does not cover the hospital
    (foreign_documents, sibling_policies; `registry` is the hospitals of its state) is detached and
    kept from the structurer, and a `foreign_documents` item lists each with its address, its
    clause (A2, S or B) and its reason, for an admin to re-link one the hospital really does use.
    The place check then reads the hospital's own documents: Clarke County's cost-estimate page
    named Osceola, and UnityPoint's policy passed with it (161348)."""
    clauses = _owner_clauses(hospital, sources_with_text, registry)
    dropped = [source for source, _ in sources_with_text if source.id in clauses]
    if not dropped:
        return sources_with_text
    for source in dropped:
        repo.unlink_source(session, hospital.ccn, source.id)
    detail = [
        {
            "id": source.id,
            "url": source.url,
            "clause": clauses[source.id],
            "reason": CLAUSE_REASONS[clauses[source.id]],
        }
        for source in dropped
    ]
    repo.add_review_item(session, hospital.ccn, "foreign_documents", {"sources": detail})
    listed = ", ".join(source.id for source in dropped)
    plural = "s are" if len(dropped) > 1 else " is"
    result.notes.append(
        f"{len(dropped)} stored document{plural} another organisation's or another hospital's "
        f"policy that does not cover the hospital ({listed}); detached and left out of the "
        "structurer"
    )
    return _without(sources_with_text, clauses)


LENT_DOCUMENTS = "lent_documents"


def _leave_out_lent_documents(
    session: Session,
    hospital: HospitalRef,
    sources_with_text: list[tuple[SourceDoc, str]],
    registry: list[HospitalRow],
    result: BuildResult,
) -> list[tuple[SourceDoc, str]]:
    """Another state's documents the hospital holds only through a loan from that state
    (_lent_from_another_state; `registry` is the hospitals of its own state) are kept from the
    structurer, as _documents_set_aside keeps every field from quoting them. They stay linked, and
    one open `lent_documents` item lists each with its lender and the state the loan came from,
    for an admin to unlink: a rebuild of Longmont United (060003) from its stored documents would
    otherwise publish Mercy San Juan's (050516) decision deadline again."""
    lent = _lent_from_another_state(session, hospital, sources_with_text, registry)
    if not lent:
        return sources_with_text
    detail = {
        "sources": [
            {"id": source_id, "lender": loan.lender, "origin_state": loan.origin_state}
            for source_id, loan in sorted(lent.items())
        ]
    }
    _file_once(session, hospital.ccn, LENT_DOCUMENTS, detail)
    listed = ", ".join(source.id for source, _ in sources_with_text if source.id in lent)
    plural = "s are" if len(lent) > 1 else " is"
    result.notes.append(
        f"{len(lent)} stored document{plural} lent by another state's hospital ({listed}); left "
        "out of the structurer and still linked"
    )
    return _without(sources_with_text, lent)


def _english_rules_and_translations(
    sources_with_text: list[tuple[SourceDoc, str]],
) -> tuple[bool, list[tuple[SourceDoc, str]]]:
    """Whether a web document in English states income rules (it names the poverty level, as
    INCOME_RULE_WORDS reads it), and the web documents in other languages."""
    english_rules = False
    translations: list[tuple[SourceDoc, str]] = []
    for source, text in sources_with_text:
        if source.kind is not SourceKind.HOSPITAL_WEB:
            continue
        if not written_in_english(text):
            translations.append((source, text))
        elif INCOME_RULE_WORDS.search(text):
            english_rules = True
    return english_rules, translations


def _never_the_hospitals(source: SourceDoc, text: str, hospital: HospitalRef) -> str | None:
    """Why a web document is never this hospital's, whatever else is stored, or None: it is a
    dental program's (scout.is_dental_program) or another Kaiser region's
    (structure.another_kaiser_region). A build detaches both before structuring
    (_drop_dental_documents, _drop_other_state_documents), and no sibling lends them."""
    if source.kind is not SourceKind.HOSPITAL_WEB:
        return None
    url = source.url or ""
    if is_dental_program(url, source.title, text):
        return DENTAL_REASON
    if another_kaiser_region(source.title, url, hospital):
        return OTHER_REGION_REASON
    return None


# Documents that belong to another hospital or another state (the 2026-10-24 publication audit,
# docs/superpowers/plans/2026-10-24-waive-audit-rules/owner-scope/). No check asked whether a stored
# document covers the hospital: Clarke County Hospital (161348) published UnityPoint Health's
# 1.BR.34, stored with Eve's draft from files.unitypoint.org, whose Schedule D lists other UnityPoint
# hospitals and not Clarke, and the place check passed on Clarke's own cost-estimate page. Stored
# pages keep their links as written, Markdown with labels included.
_LINK = re.compile(r"https?://[^\s)\]\"'<>|]+", re.IGNORECASE)
_MARKDOWN_LINK = re.compile(r"\[([^\]]{3,200})\]\(\s*([^)\s]+)")
_POLICY_LABEL = re.compile(
    r"financial assistance|charity|financial aid|\bpolicy\b|\bfap\b", re.IGNORECASE
)
# A document's own list of the hospitals it covers: "Emory Healthcare Hospitals include:",
# "Hospitals (listed below)", "applies to the following entities", "The following ... Hospitals and
# Hospital Organizations are covered under Policy 1.BR.34", and that template's scope, "included in
# attached Schedule D". PDF text doubles spaces, hence \s+. A policy that applies to all of a
# system's hospitals ("Applies to: All Franciscan Alliance Hospitals") leaves none out.
_SCHEDULE_SCOPE = re.compile(
    r"included\s+in\s+(?:the\s+)?attached\s+schedule\s+[a-z]\b", re.IGNORECASE
)
_COVERED_LIST = re.compile(
    r"hospitals\s+include\s*:|facilities\s+include\s*:|\(listed\s+below\)"
    r"|applies\s+to\s+the\s+following\s+(?:hospitals|facilities|entities)"
    r"|the\s+following\b.{0,120}?\b(?:hospitals|facilities)\b.{0,60}?\bare\s+covered\b",
    re.IGNORECASE | re.DOTALL,
)
_APPLIES_TO_ALL = re.compile(r"\bappl(?:y|ies)\s+to\s*:?\s*all\b", re.IGNORECASE)
# What follows a title's first " | " or " - " is the site's name, not the document's subject.
_TITLE_SEGMENT = re.compile(r"\s+[|–—-]\s+")


def _web_documents(sources: Iterable[tuple[SourceDoc, str]]) -> list[tuple[SourceDoc, str]]:
    return [
        (source, text)
        for source, text in sources
        if source.kind is SourceKind.HOSPITAL_WEB and source.url
    ]


def _single_spaced(text: str) -> str:
    """`text` lower-case, without apostrophes (discover._plain), on one line with single spaces."""
    return " ".join(_plain(text).split())


_WORD = re.compile(r"[a-z0-9]+")


@cache
def _town_words(city: str) -> frozenset[str]:
    return frozenset(_WORD.findall(_plain(city)))


@cache
def _has_distinctive_name(name: str) -> bool:
    return bool(name_tokens(name))


def _hospitals_named(plain: str, rows: Iterable[HospitalRow]) -> list[HospitalRow]:
    """The registry rows `plain` (_single_spaced) names by name (discover._names_hospital) and by
    town (discover._names_city). A named town's words are all words of the text, so that cheap
    test leaves out nearly every row first: a recheck reads documents against the whole registry
    (_lent_from_another_state)."""
    words = set(_WORD.findall(plain))
    return [
        row
        for row in rows
        if (town := _town_words(row.city))
        and town <= words
        and _has_distinctive_name(row.name)
        and _names_hospital(row, plain)  # a registry row carries the name it reads
        and _names_city(row, plain)
    ]


def _names_hospital_or_town(hospital: HospitalRef, text: str) -> bool:
    plain = _single_spaced(text)
    return _names_city(hospital, plain) or _names_hospital(hospital, plain)


def _lists_other_hospitals(hospital: HospitalRef, text: str, registry: list[HospitalRow]) -> bool:
    """Whether `text` covers less than all of a system's hospitals and names another registry
    hospital of the state, by its name and its town (_hospitals_named)."""
    if _APPLIES_TO_ALL.search(text):
        return False
    return any(row.ccn != hospital.ccn for row in _hospitals_named(_single_spaced(text), registry))


def foreign_documents(
    hospital: HospitalRef,
    sources: list[tuple[SourceDoc, str]],
    registry: list[HospitalRow],
) -> set[str]:
    """The web documents of another organisation whose own list of covered hospitals leaves this
    one out (FOREIGN_OWNER_REASON); `registry` is the hospitals of its state.

    Clause A2: the document is off the hospital's website and the asset hosts, no stored page on
    the website links to its site, its text names neither the hospital, nor its town, nor the
    website's name ("UNC Health Appalachian" names unchealthappalachian.org), and it carries its
    own covered-hospital list naming another hospital of the state. Every other document on that
    site that meets the first three conditions goes with it: UnityPoint's application beside its
    1.BR.34 for Clarke County (161348); Emory Healthcare's policy for St. Francis-Emory (110129).

    Clause S, on the hospital's own website or a site it does not link: the 1.BR.34 template's
    scope, "included in attached Schedule D", with a Schedule naming the state's other hospitals
    and never this one or its town. Carle Hoopeston (141316) quoted Carle Health West Region's
    policy on carle.org, whose Schedule D lists Methodist, Pekin and Proctor.

    A system document with no list of its own stays: off the website and unlinked is no proof of
    another owner (CommonSpirit's G-003 for CHI Oakes, 351315; Corewell's, Piedmont's, Sentara's
    and Franciscan's documents on their CDNs and statement sites)."""
    return set(_foreign_clauses(hospital, sources, registry))


def _foreign_clauses(
    hospital: HospitalRef,
    sources: list[tuple[SourceDoc, str]],
    registry: list[HospitalRow],
) -> dict[str, str]:
    """foreign_documents' documents, each with its clause: "A2" for one off the website and
    unlinked, and those on its site that go with it (FOREIGN_OWNER_REASON says so), and "S" for
    one only the schedule clause sets aside, as Carle West's policy on carle.org itself is
    (SCHEDULE_OWNER_REASON)."""
    if not hospital.website_domain:
        return {}
    home = host_of(f"https://www.{hospital.website_domain}")
    site_name = home.split(".")[0]
    documents = _web_documents(sources)
    linked = {
        host_of(link)
        for source, text in documents
        if host_of(source.url or "") == home
        for link in _LINK.findall(text)
    }

    def unclaimed(source: SourceDoc, text: str) -> bool:
        host = host_of(source.url or "")
        return (
            host != home
            and not is_asset_host(source.url or "")
            and host not in linked
            and not _names_hospital_or_town(hospital, text)
            and site_name not in re.sub(r"[^a-z0-9]", "", text.lower())
        )

    off_the_website = {
        source.id
        for source, text in documents
        if unclaimed(source, text)
        and (_COVERED_LIST.search(text) or _SCHEDULE_SCOPE.search(text))
        and _lists_other_hospitals(hospital, text, registry)
    }
    schedule = {
        source.id
        for source, text in documents
        if _SCHEDULE_SCOPE.search(text)
        and (host_of(source.url or "") == home or host_of(source.url or "") not in linked)
        and not _names_hospital_or_town(hospital, text)
        and _lists_other_hospitals(hospital, text, registry)
    }
    found = off_the_website | schedule
    hosts = {host_of(source.url or "") for source, _ in documents if source.id in found}
    off_the_website |= {
        source.id
        for source, text in documents
        if host_of(source.url or "") in hosts and unclaimed(source, text)
    }
    clauses = dict.fromkeys(schedule, "S")
    clauses.update(dict.fromkeys(off_the_website, "A2"))
    return clauses


def _same_url(url: str) -> str:
    """`url` as two links to one document compare: unquoted, lower-case, https, without www. or a
    trailing slash."""
    return unquote(url).rstrip("/").lower().replace("http://", "https://").replace("://www.", "://")


def sibling_policies(
    hospital: HospitalRef,
    sources: list[tuple[SourceDoc, str]],
    registry: list[HospitalRow],
) -> set[str]:
    """Clause B (SIBLING_POLICY_REASON): another hospital's policy where the hospital's site links
    its own. A stored page links a policy under a label naming the hospital or its town, and links
    this document only under labels that do not, one of them naming another registry hospital of
    the state (`registry`), and the document never names the hospital or its town. Rochester
    Regional's page links a "Canton-Potsdam Hospital Financial Assistance Policy", never stored,
    beside the "Rochester General Hospital Financial Assistance Policy" that Canton-Potsdam's sheet
    (330197) quoted. Penn Medicine's summary, linked under Lancaster General's name as well, names
    The Chester County Hospital (390179) and stays."""
    documents = _web_documents(sources)
    labels: dict[str, list[str]] = {}
    for source, text in documents:
        for written, href in _MARKDOWN_LINK.findall(text):
            label = " ".join(written.split())
            if _POLICY_LABEL.search(label):
                labels.setdefault(_same_url(urljoin(source.url or "", href)), []).append(label)
    own = {
        url
        for url, found in labels.items()
        if any(_names_hospital_or_town(hospital, label) for label in found)
    }
    if not own:
        return set()
    others = [row for row in registry if row.ccn != hospital.ccn]
    policies: set[str] = set()
    for source, text in documents:
        url = _same_url(source.url or "")
        found = labels.get(url, [])
        if url in own or not found or _names_hospital_or_town(hospital, text):
            continue
        if any(
            _names_hospital(other, _single_spaced(label)) for other in others for label in found
        ):
            policies.add(source.id)
    return policies


CLAUSE_REASONS = {
    "A2": FOREIGN_OWNER_REASON,
    "S": SCHEDULE_OWNER_REASON,
    "B": SIBLING_POLICY_REASON,
}


def _owner_clauses(
    hospital: HospitalRef,
    sources: list[tuple[SourceDoc, str]],
    registry: list[HospitalRow],
) -> dict[str, str]:
    """The web documents clauses A2, S and B set aside, each with its clause (CLAUSE_REASONS); A2
    or S comes first when B holds too."""
    clauses = _foreign_clauses(hospital, sources, registry)
    for source_id in sibling_policies(hospital, sources, registry):
        clauses.setdefault(source_id, "B")
    return clauses


# "Kansas City", "Washington County", "Virginia Beach", "Delaware Valley", "Oklahoma City": a
# place, not the state. verify._PLACE_AFTER also reads "California Hospitals" as a place, which is
# just what another state's version of a policy calls itself, so it is not reused here.
_PLACE_NAME_AFTER = re.compile(r"[- ]+(?:count(?:y|ies)|city|beach|valley)(?![a-z])")


def _states_as_states(text: str) -> set[str]:
    """The codes of the states `text` names by name (states.states_named), but not where the name
    is a place's: a Missouri hospital's Kansas City page is not Kansas's (Saint Luke's East,
    260216, Lee's Summit), nor Delaware Valley Hospital (331312, New York) Delaware's."""
    lowered = text.lower()
    return {
        CODE_OF_NAME[match.group(1)]
        for match in STATE_NAMES.finditer(lowered)
        if not _PLACE_NAME_AFTER.match(lowered, match.end())
    }


def _states_claimed(hospital: HospitalRef, source: SourceDoc) -> set[str]:
    """The states the document's address path and the subject of its title name, the states in
    the hospital's own name or town left out (Sierra Nevada Memorial's policy, 050150, is
    California's)."""
    path = unquote(urlparse(source.url or "").path)
    subject = _TITLE_SEGMENT.split(source.title or "")[0]
    named = _states_as_states(re.sub(r"[-_./%+]", " ", f"{subject} {path}"))
    return named - _states_as_states(f"{hospital.name} {hospital.city}")


def other_state_documents(hospital: HospitalRef, sources: list[tuple[SourceDoc, str]]) -> set[str]:
    """Clause C (OTHER_STATE_DOCUMENT_REASON): the web documents whose address path or the subject
    of whose title names states, none of them the hospital's: another state's version of the
    system policy. The title counts up to its first " | " or " - ", since the site's name follows:
    "Financial Assistance | North Mississippi Health Services" is an Alabama hospital's page
    (010044). It holds whatever else is stored: Ascension Providence (450042, Waco) had only
    Maryland's, Florida's, Oklahoma's and Illinois's pages, and published the free-care limit of
    "Financial assistance Ascension Saint Agnes in Maryland"."""
    return {
        source.id
        for source, _ in _web_documents(sources)
        if (named := _states_claimed(hospital, source)) and hospital.state not in named
    }


def _without(
    sources_with_text: list[tuple[SourceDoc, str]], ids: Iterable[str]
) -> list[tuple[SourceDoc, str]]:
    gone = set(ids)
    return [(source, text) for source, text in sources_with_text if source.id not in gone]


def _utc(moment: datetime) -> datetime:
    """`moment` in UTC: SQLite hands timestamps back without their zone."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _held_through_loans(session: Session, ccn: str) -> dict[str, list[str]]:
    """The documents hospital `ccn` holds only through loans, each with its lenders' CCNs. A loan
    is a `shared_sources` item: _share_sibling_sources files one for the documents it links, and
    nothing else links a document stored for one hospital to another. The link table says no
    more, and a scout or an Eve import links a document without a record of its own, so a
    document is the hospital's own as well when one of the hospital's sheet versions older than
    the document's first loan lists it among its sources: Eve's imported draft lists the
    documents Eve cited, and a build every document it read."""
    lenders: dict[str, list[str]] = {}
    first_lent: dict[str, datetime] = {}
    for item in repo.review_items_of_kind(session, ccn, "shared_sources"):
        detail = item.detail or {}
        lent_on = _utc(item.created_at)
        for source_id in detail.get("sources", []):
            lenders.setdefault(source_id, []).append(detail.get("from_ccn") or "")
            first_lent[source_id] = min(first_lent.get(source_id, lent_on), lent_on)
    if not lenders:
        return {}
    for version in repo.sheet_versions(session, ccn):
        for source in (version.body or {}).get("sources", []):
            source_id = source.get("id")
            if source_id in first_lent and _utc(version.created_at) < first_lent[source_id]:
                lenders.pop(source_id, None)
    return lenders


def _loan_origins(
    session: Session, lenders: list[str], source_id: str, seen: frozenset[str]
) -> list[HospitalRow]:
    """The registry hospitals loans of `source_id` from `lenders` trace back to: a lender that
    holds the document on its own, or, for one that holds it only through a loan too, that loan's
    origins (Mercy Redding, 051319, had Brazosport's Texas application from Mercy Merced, 050444,
    which had it from Brazosport, 450072)."""
    origins: list[HospitalRow] = []
    for lender_ccn in lenders:
        lender = repo.get_hospital(session, lender_ccn) if lender_ccn not in seen else None
        if lender is None:
            continue
        upstream = _held_through_loans(session, lender_ccn).get(source_id)
        traced = (
            _loan_origins(session, upstream, source_id, seen | {lender_ccn}) if upstream else []
        )
        origins.extend(traced or [lender])
    return origins


@dataclass(frozen=True)
class Loan:
    """How a hospital holds a document only through loans: its lenders' CCNs and the registry
    hospitals the loans trace back to (_loan_origins)."""

    lenders: list[str]
    origins: list[HospitalRow]

    @property
    def lender(self) -> str:
        """The first lender its `shared_sources` items name."""
        return next((ccn for ccn in self.lenders if ccn), "")

    @property
    def origin_state(self) -> str:
        return "/".join(sorted({origin.state for origin in self.origins}))


def _loans_from_another_state(session: Session, ccn: str, state: str) -> dict[str, Loan]:
    """The documents hospital `ccn` holds only through loans (_held_through_loans) that trace back
    to a registry hospital of a state other than `state`, whatever they say."""
    loans: dict[str, Loan] = {}
    for source_id, lenders in _held_through_loans(session, ccn).items():
        origins = _loan_origins(session, lenders, source_id, frozenset({ccn}))
        if any(origin.state != state for origin in origins):
            loans[source_id] = Loan(lenders, origins)
    return loans


def _covers_the_state(
    hospital: HospitalRef, source: SourceDoc, text: str, registry: list[HospitalRow]
) -> bool:
    """Whether the document says it reaches the hospital's state: it names the hospital or its
    town, another hospital of the state (`registry`) by name and town (Saint Joseph's page names
    Kentucky's Saint Joseph hospitals, for Berea, 181329), the state by name (HSHS's programme names
    Wisconsin, for St. Vincent, 520075), or the state's code in the subject of its title
    ("Financial Assistance AR, OK, Springfield & St. Louis", Mercy's page for Tishomingo,
    371304)."""
    if _names_hospital_or_town(hospital, text):
        return True
    if hospital.state in _states_as_states(f"{source.title} {text}"):
        return True
    if hospital.state in re.findall(r"\b[A-Z]{2}\b", _TITLE_SEGMENT.split(source.title or "")[0]):
        return True
    named = _hospitals_named(_single_spaced(text), registry)
    return any(row.ccn != hospital.ccn for row in named)


def _another_states_document(
    plain: str, origins: set[str], hospitals_elsewhere: list[HospitalRow]
) -> bool:
    """Whether `plain` (_single_spaced) is a hospital's document of another state: it names, by
    name and town, a hospital of a state a loan came from (`origins`), or two hospitals of one
    other state. A single name from anywhere is not enough: "Mission Regional Medical Center"
    (450176, Mission, Texas) is in every "our mission", and Washington Hospital (390042,
    Washington, Pennsylvania) in CommonSpirit's Washington provisions. A document naming two
    hospitals each in three states or more is a system's, not a state's version: CommonSpirit's
    financial assistance landing page lists hospitals of California, Iowa, Nebraska, New York,
    North Dakota and Texas."""
    named = Counter(row.state for row in _hospitals_named(plain, hospitals_elsewhere))
    if sum(1 for count in named.values() if count >= 2) >= 3:
        return False
    return any(named[state] for state in origins) or any(count >= 2 for count in named.values())


def _lent_from_another_state(
    session: Session,
    hospital: HospitalRef,
    sources_with_text: list[tuple[SourceDoc, str]],
    registry: list[HospitalRow],
) -> dict[str, Loan]:
    """The documents the hospital holds only through loans tracing back to registry hospitals of
    other states alone (_loans_from_another_state) that are another state's
    (_another_states_document) and say nothing that reaches the hospital's own state
    (_covers_the_state; `registry` is the hospitals of that state). Before siblings lent only
    within the state, 25 loans crossed one: Mercy Medical Center Merced (050444, California)
    lent Mercy San Juan's (050516) application to Longmont United (060003, Colorado), St.
    Anthony (060015) and Bob Wilson Memorial (171388, Kansas), whose published sheets quote its
    decision deadline, and nothing in its address or title names California, so
    other_state_documents cannot tell. Merced lent CHI St. Vincent's Arkansas page with it. A
    system-wide document lent across states stays: CommonSpirit's G-003 names no hospital, and
    those sheets quote it for their income rules, as CHI Oakes (351315) correctly does."""
    loans = _loans_from_another_state(session, hospital.ccn, hospital.state)
    held = {source.id: (source, text) for source, text in sources_with_text}
    elsewhere: list[HospitalRow] | None = None
    lent: dict[str, Loan] = {}
    for source_id, loan in loans.items():
        if source_id not in held or any(o.state == hospital.state for o in loan.origins):
            continue
        source, text = held[source_id]
        if _covers_the_state(hospital, source, text, registry):
            continue
        if elsewhere is None:
            elsewhere = [row for row in repo.list_hospitals(session) if row.state != hospital.state]
        states = {origin.state for origin in loan.origins}
        if _another_states_document(_single_spaced(text), states, elsewhere):
            lent[source_id] = loan
    return lent


def _documents_set_aside(
    hospital: HospitalRef,
    sources_with_text: list[tuple[SourceDoc, str]],
    registry: list[HospitalRow],
    lent: Collection[str],
) -> dict[str, str]:
    """The stored web documents no field may be quoted from, each with verify_sheet's reason, in
    this order:
    - a dental program's and another Kaiser region's (DENTAL_REASON, OTHER_REGION_REASON);
    - another state's version of the system policy (other_state_documents,
      OTHER_STATE_DOCUMENT_REASON), even when every document is another state's;
    - another state's document `lent` by a hospital of that state (_lent_from_another_state,
      LENT_DOCUMENT_REASON), whose link stays;
    - another organisation's or another hospital's policy that does not cover the hospital
      (foreign_documents, sibling_policies; FOREIGN_OWNER_REASON, SCHEDULE_OWNER_REASON,
      SIBLING_POLICY_REASON), judged against `registry`, the hospitals of its state;
    - among what is left, the translations while a web document in English states the income
      rules (TRANSLATION_REASON).
    A build keeps each of those from the structurer (_documents_for_structurer). A sheet built
    before a rule still quotes such a document while it stays linked: the nine published Kaiser
    California sheets quoting Kaiser Northwest's dental policy passed recheck (review of 7.16)."""
    set_aside = {
        source.id: reason
        for source, text in sources_with_text
        if (reason := _never_the_hospitals(source, text, hospital))
    }
    rest = _without(sources_with_text, set_aside)
    other_state = other_state_documents(hospital, rest)
    set_aside.update((source_id, OTHER_STATE_DOCUMENT_REASON) for source_id in other_state)
    rest = _without(rest, other_state)
    loaned = {source.id for source, _ in rest if source.id in lent}
    set_aside.update((source_id, LENT_DOCUMENT_REASON) for source_id in loaned)
    rest = _without(rest, loaned)
    clauses = _owner_clauses(hospital, rest, registry)
    set_aside.update((source_id, CLAUSE_REASONS[clause]) for source_id, clause in clauses.items())
    rest = _without(rest, clauses)
    english_rules, translations = _english_rules_and_translations(rest)
    if english_rules:
        set_aside.update((source.id, TRANSLATION_REASON) for source, _ in translations)
    return set_aside


def _set_aside_for(
    session: Session,
    row: HospitalRow,
    sources_with_text: list[tuple[SourceDoc, str]],
    registry: list[HospitalRow],
) -> dict[str, str]:
    """_documents_set_aside for a registry hospital, the documents another state's hospital lent
    it looked up (_lent_from_another_state), as recheck_sheets, withdraw_rejected and
    confirm_domain need it."""
    hospital = repo.hospital_ref(row)
    lent = _lent_from_another_state(session, hospital, sources_with_text, registry)
    return _documents_set_aside(hospital, sources_with_text, registry, lent)


def _file_once(session: Session, ccn: str, kind: str, detail: dict[str, Any]) -> None:
    """One open item of `kind` per hospital, its detail brought up to date: a build that runs
    again does not file it twice."""
    for item in repo.open_review_items(session, ccn, kind):
        if item.detail != detail:
            item.detail = detail
            session.flush()
        return
    repo.add_review_item(session, ccn, kind, detail)


ENGLISH_POLICY_MISSING = "english_policy_missing"


def _ask_for_the_english_policy(session: Session, ccn: str, translation_ids: list[str]) -> None:
    """One open item per hospital, naming the translations that stand in for the English policy
    until an admin, or a scout aimed at it, stores the English one."""
    detail = {
        "translations": translation_ids,
        "reason": "only translations state the income rules; scout the English policy",
    }
    _file_once(session, ccn, ENGLISH_POLICY_MISSING, detail)


def _set_aside_translations(
    session: Session, ccn: str, sources_with_text: list[tuple[SourceDoc, str]], result: BuildResult
) -> tuple[list[tuple[SourceDoc, str]], list[tuple[SourceDoc, str]]]:
    """The documents the structurer reads, and the translations kept from it. A system publishes
    its policy in a dozen languages side by side, and the scout stores whichever the search
    returns: six published sheets quoted a translation (BID Needham, 220083, in Chinese; Baptist
    Beaches and Nassau, 100117 and 100140, in Tagalog; task 7.15a), and seniors read those quotes
    on the public atlas. While a web document in English states the income rules, the hospital's
    web documents in other languages are kept from the structurer; they stay linked, so the place
    check and a later rebuild still see them. Otherwise the translations stay: four of those six
    hospitals stored only English billing pages and a job listing, none naming the poverty level,
    and setting the translations aside would have left a rebuild with no income rule at all
    (review of 7.15b). An `english_policy_missing` item asks for the English policy instead."""
    english_rules, translations = _english_rules_and_translations(sources_with_text)
    if not translations:
        return sources_with_text, []
    listed = ", ".join(source.id for source, _ in translations)
    plural = "s are" if len(translations) > 1 else " is"
    if not english_rules:
        _ask_for_the_english_policy(session, ccn, [source.id for source, _ in translations])
        result.notes.append(
            f"{len(translations)} stored document{plural} not in English ({listed}) and no "
            "English one states the income rules: kept for the structurer, and an "
            f"{ENGLISH_POLICY_MISSING} item asks for the English policy"
        )
        return sources_with_text, []
    result.notes.append(
        f"{len(translations)} stored document{plural} not in English ({listed}); left out of the "
        "structurer while an English one states the income rules, and still linked"
    )
    return _without(sources_with_text, {source.id for source, _ in translations}), translations


def _documents_for_structurer(
    session: Session,
    hospital: HospitalRef,
    sources_with_text: list[tuple[SourceDoc, str]],
    registry: list[HospitalRow],
    result: BuildResult,
) -> tuple[list[tuple[SourceDoc, str]], list[tuple[SourceDoc, str]]]:
    """What a structuring pass reads, from the hospital's own documents or a sibling's too: a
    dental program's documents, another state's version of a system policy and another
    organisation's or hospital's policy that does not cover the hospital (judged against
    `registry`, the hospitals of its state) detached, what another state's hospital lent left out,
    then the translations set aside. Those come back as well, for the place check, which reads
    all of the hospital's documents. The dental ones go first: they are not the hospital's at
    all, so they neither name its state for it nor leave another state's policy all there is to
    prefer."""
    kept = _drop_dental_documents(session, hospital.ccn, sources_with_text, result)
    kept = _drop_other_state_documents(session, hospital, kept, result)
    kept = _leave_out_lent_documents(session, hospital, kept, registry, result)
    kept = _drop_foreign_documents(session, hospital, kept, registry, result)
    return _set_aside_translations(session, hospital.ccn, kept, result)


def _hold_without(session: Session, ccn: str, gone: set[str], result: BuildResult) -> bool:
    """The latest sheet held without the fields it quotes from documents this build set aside
    (`gone`), as one built from a wrong website is (clear_wrong_domain): it cannot go on
    publishing a dental program's bands or another region's policy while no new sheet replaces
    it. True when a held version was published; a sheet that quotes none of them is left as it
    is."""
    latest = repo.latest_sheet(session, ccn)
    if latest is None:
        return False
    sheet = latest[0]
    dropped = [path for path, cited in sheet.field_paths() if cited.source_id in gone]
    if not dropped:
        return False
    held = drop_fields(sheet, dropped).model_copy(
        update={
            "status": SheetStatus.HELD,
            "sources": [source for source in sheet.sources if source.id not in gone],
        }
    )
    published = publish_sheet(session, held)
    result.version = published.version if published else sheet.version
    result.notes.append(
        f"{len(dropped)} fields quoted from the documents set aside dropped and the sheet held: "
        + ", ".join(dropped)
    )
    return True


def _nothing_left_to_structure(
    session: Session, row: HospitalRow, set_aside: set[str], result: BuildResult
) -> BuildResult:
    """Every document the build had was set aside before structuring (Kaiser Foundation Hospital
    and Rehab Center, 050073, had stored nothing but Kaiser Northwest's dental policy; task 7.16)
    and no sibling could lend one, so no model is asked. A `no_documents` item records it and
    keeps the scheduler off the hospital for a month (schedule.FAILURE_KINDS) before it is
    scouted again, and a sheet that quoted the set-aside documents is held without them."""
    repo.add_review_item(
        session,
        row.ccn,
        "no_documents",
        {"domain": row.website_domain, "set_aside": sorted(set_aside)},
    )
    result.notes.append(
        "no financial assistance documents left to structure once those are set aside"
    )
    if _hold_without(session, row.ccn, set_aside, result):
        result.outcome = "held"
    return result


@dataclass
class Borrowed:
    """A sibling's documents added to the hospital's own, and the review item that records it."""

    sources: list[tuple[SourceDoc, str]]
    shared_ids: set[str]
    item: ReviewItemRow


def _share_sibling_sources(
    session: Session,
    row: HospitalRow,
    own: list[tuple[SourceDoc, str]],
    result: BuildResult,
    because: str = "the hospital's own documents state no income rules",
) -> Borrowed | None:
    """The hospital's documents plus the policy documents of a sibling on the same website that
    has a published sheet, or None when no sibling can help. Searching a system site by hospital
    name returns a thin page or nothing while the system's policy is already stored for a
    sibling (Adventist Health White Memorial, AdventHealth Riverview, Advocate Christ and
    Condell, task 7.9). Only documents that state income rules (they name the poverty level) are
    shared, they are linked to this hospital so --reuse-sources finds them next time, and a
    `shared_sources` item records whose they were: a system may keep per-hospital variants. A
    dental program's or another Kaiser region's document is never lent: the build would detach
    it at once, and a sibling that has nothing else to lend does not end the search (Los Angeles,
    050138, held the dental policy and Kaiser Northwest's beside California's; review of 7.16).
    Only a sibling in the hospital's state lends, and never another state's version of the policy
    (other_state_documents) nor a document it holds only through a loan tracing back to a
    hospital of another state (_loans_from_another_state), whatever the document says: Mercy
    Medical Center Merced (050444, California) lent Mercy San Juan's (050516) application on
    commonspirit.org to Longmont United (060003, Colorado), St. Anthony (060015, Colorado) and Bob
    Wilson Memorial (171388, Kansas), whose sheets published its decision deadline, and St.
    Anthony would have passed it on to Longmont (the 2026-10-24 audit)."""
    domain = row.website_domain
    if not domain:
        return None
    hospital = repo.hospital_ref(row)
    own_ids = {source.id for source, _ in own}
    for sibling in repo.siblings_on_domain(session, domain, row.ccn):
        if sibling.state != row.state:
            continue
        latest = repo.latest_sheet(session, sibling.ccn)
        if latest is None or latest[0].status is not SheetStatus.PUBLISHED:
            continue
        # A sibling never passes on a loan from another state's hospital, whatever it says: St.
        # Anthony (060015, Colorado) held Mercy San Juan's application from Mercy Merced
        # (050444, California), and Longmont United (060003) would have had it from St. Anthony.
        from_elsewhere = _loans_from_another_state(session, sibling.ccn, row.state)
        policies = [
            (source, text)
            for source, text in repo.sources_for(session, sibling.ccn)
            if source.kind is SourceKind.HOSPITAL_WEB
            and source.id not in own_ids
            and source.id not in from_elsewhere
            and INCOME_RULE_WORDS.search(text)
            and not _never_the_hospitals(source, text, hospital)
        ]
        shared = _without(policies, other_state_documents(hospital, policies))
        if not shared:
            continue
        for source, _ in shared:
            repo.link_source(session, row.ccn, source.id)
        item = repo.add_review_item(
            session,
            row.ccn,
            "shared_sources",
            {"from_ccn": sibling.ccn, "domain": domain, "sources": [s.id for s, _ in shared]},
        )
        plural = "s" if len(shared) > 1 else ""
        result.notes.append(
            f"{len(shared)} policy document{plural} shared from {sibling.name} ({sibling.ccn}) on "
            f"{domain}; {because}"
        )
        return Borrowed([*own, *shared], {s.id for s, _ in shared}, item)
    return None


def _previously_borrowed(
    session: Session, ccn: str, sources_with_text: list[tuple[SourceDoc, str]]
) -> Borrowed | None:
    """Documents an earlier build shared from a sibling, now linked to this hospital and loaded
    by --reuse-sources as if its own. The `shared_sources` items say which they were, so a
    rebuild treats them the same way the first build did."""
    items = repo.review_items_of_kind(session, ccn, "shared_sources")
    if not items:
        return None
    shared_ids = {source_id for item in items for source_id in item.detail.get("sources", [])}
    present = shared_ids & {source.id for source, _ in sources_with_text}
    if not present:
        return None
    return Borrowed(sources_with_text, present, items[-1])


def _drop_borrowed_specifics(
    sheet: ProcedureSheet, borrowed: Borrowed, result: BuildResult
) -> ProcedureSheet:
    """A borrowed document's addresses, phones, hours, languages and facility lists are the
    sibling's (or the system's; nothing in the text tells which), so the hospital-specific fields
    it backs are dropped and listed in the `shared_sources` item for an admin to restore."""
    dropped = [
        path
        for path, cited in sheet.field_paths()
        if path in BORROWED_SPECIFIC_PATHS and cited.source_id in borrowed.shared_ids
    ]
    if not dropped:
        return sheet
    borrowed.item.detail = {**borrowed.item.detail, "dropped_fields": dropped}
    result.notes.append(
        "dropped from the shared documents (a sibling's desk, not this hospital's): "
        + ", ".join(dropped)
    )
    return drop_fields(sheet, dropped)


def _lacks_income_rules(sheet: ProcedureSheet) -> bool:
    """No free-care band and no table. An assistance ceiling alone still counts as lacking them
    here: a sibling's policy may give the table, and is worth one more pass before the sheet
    publishes on the ceiling (decision 7.9a-2)."""
    return sheet.eligibility.free_care_max_fpl is None and sheet.eligibility.discount_tiers is None


def _ground(sheet: ProcedureSheet, texts: dict[str, str]) -> tuple[ProcedureSheet, list[str]]:
    """Each quote cut to the span its source contains, then each list cut to the items that span
    names. The structurer filtered lists against the model's whole quote; the kept sentence may
    name fewer (review of 7.9), so the pruning runs again here, where verify_sheet checks it."""
    return prune_lists(trim_quotes(sheet, texts), texts)


def _apply_owner_decision(
    session: Session,
    ccn: str,
    sheet: ProcedureSheet,
    decision: Mapping[str, Any] | None,
    result: BuildResult,
) -> ProcedureSheet:
    """The primary read held to the owner's ruling on its free-care value
    (eve_import.keep_owner_decision), before it is grounded and verified: a value the owner ruled
    out cannot come back on a rebuild, and one moved to the assistance ceiling is judged by the
    ceiling rules like any other, but for the two readings the ruling wins over (a free-care band,
    a ceiling below the sheet's own rules; OWNER_RULING_CONFLICT). The notes go to the result and
    one `owner_decision` item records what was done."""
    if decision is None:
        return sheet
    kept, notes = keep_owner_decision(sheet, decision)
    if not notes:
        return sheet
    moved = (
        sheet.eligibility.assistance_ceiling_fpl is None
        and kept.eligibility.assistance_ceiling_fpl is not None
    )
    repo.add_review_item(
        session,
        ccn,
        "owner_decision",
        {
            "decision": decision["decision"],
            "value": decision["value"],
            "kept_as": "assistance_ceiling_fpl" if moved else "dropped",
            "decided_at": decision.get("at"),
        },
    )
    result.notes.extend(notes)
    return kept


def _structure_and_verify(
    session: Session,
    ai: AIClient,
    hospital: HospitalRef,
    sources_with_text: list[tuple[SourceDoc, str]],
    today: date,
    result: BuildResult,
    decision: Mapping[str, Any] | None = None,
) -> tuple[ProcedureSheet, list[str]] | None:
    """The primary draft, held to the owner's ruling, trimmed to the spans the sources contain,
    verified and stripped of rejected fields, with the reasons an admin must look first (holds).
    None when the structurer gave nothing usable; `result` then carries the failed outcome."""
    ccn = hospital.ccn
    texts = {source.id: text for source, text in sources_with_text}
    try:
        sheet, skipped = structure_sheet(ai, "reason", hospital, sources_with_text, today)
    except AIOutputError as error:
        # No usable primary draft, even from the smaller prompt structure_sheet falls back to
        # (task 7.8): the documents stay stored, the reason goes to review, and the batch sees
        # an ordinary failed outcome rather than an exception that would roll the scout back.
        # The open item also keeps the scheduler off this hospital (schedule.FAILURE_KINDS).
        repo.add_review_item(
            session, ccn, "structure_failed", {"error": str(error)[:ERROR_DETAIL_CHARS]}
        )
        result.outcome = "failed"
        result.notes.append(f"structurer gave no usable sheet: {error}"[:ERROR_DETAIL_CHARS])
        return None
    sheet = _apply_owner_decision(session, ccn, sheet, decision, result)
    sheet, pruned = _ground(sheet, texts)
    skipped = [*skipped, *pruned]
    report, overruled = _keep_owner_ruled_ceiling(sheet, verify_sheet(sheet, texts), decision)
    _record_ruling_conflicts(session, ccn, overruled, result.notes)
    if skipped or report.rejected:
        repo.add_review_item(
            session, ccn, "verification", {"skipped": skipped, "rejected": report.rejected}
        )
        result.notes.extend(skipped)
        result.notes.extend(f"{path}: {reason}" for path, reason in report.rejected)
    holds = _holding_reasons(report.rejected)
    _file_patient_share_table(session, ccn, sheet, holds)
    return drop_fields(sheet, [path for path, _ in report.rejected]), holds


def _file_patient_share_table(
    session: Session, ccn: str, sheet: ProcedureSheet, holds: list[str]
) -> None:
    """The table says what the patient pays (co-pay, X% of charges); the schema cannot express
    that band, and publishing the free-care remainder alone would deny the co-pay band. The item
    keeps the dropped table's quote for the admin."""
    if PATIENT_SHARE_REASON not in holds:
        return
    tiers = sheet.eligibility.discount_tiers
    repo.add_review_item(
        session, ccn, "patient_share_table", {"quote": tiers.quote if tiers else None}
    )


def _holding_reasons(rejected: list[tuple[str, str]]) -> list[str]:
    """The reasons among `rejected` that hold the sheet rather than only drop the field
    (verify.HOLDING_REASONS), each once: what is left would publish a worse answer (a table from
    70% with nothing below, once Russell Regional's misread ceiling goes)."""
    return list(dict.fromkeys(reason for _, reason in rejected if reason in HOLDING_REASONS))


# Owner rulings (data/atlas-eve/_decisions.json, review decision 2026-10-23) win over these
# readings. A `discount` ruling makes its value the assistance ceiling (keep_owner_decision), and
# that ceiling is neither rejected as the free-care band (verify.CEILING_FREE_BAND_REASON) nor held
# below the sheet's own rules (publish.sheet_inconsistencies). Each conflict is recorded instead,
# as an open item that reaches the owner: Russell Regional (171350) ruled 70% a discount limit,
# quoted from its 100%-discount line under a table to 200%; Baptist Health's "discount 300"
# sheets (180104, 180130 and others) put 300 in the ceiling next to a 301-400% band.
OWNER_RULING_CONFLICT = "owner_ruling_conflict"
CEILING_PATH = "eligibility.assistance_ceiling_fpl"
RULING_WINS_OVER = (CEILING_FREE_BAND_REASON,)


def _owner_ruled_ceiling(
    sheet: ProcedureSheet, decision: Mapping[str, Any] | None
) -> Decimal | None:
    """The sheet's assistance ceiling when it is the owner's: its value is the ruled value of a
    `discount` ruling. None otherwise."""
    ruled = ruled_ceiling(decision)
    ceiling = sheet.eligibility.assistance_ceiling_fpl
    return ruled if ceiling is not None and ruled is not None and ceiling.value == ruled else None


def _ruling_conflict(
    sheet: ProcedureSheet, decision: Mapping[str, Any] | None, reason: str
) -> dict[str, Any]:
    ceiling = sheet.eligibility.assistance_ceiling_fpl
    return {
        "path": CEILING_PATH,
        "reason": reason,
        "ruled_value": str(decision.get("value")) if decision else None,
        "quote": ceiling.quote if ceiling else None,
        "decided_at": decision.get("at") if decision else None,
    }


def _keep_owner_ruled_ceiling(
    sheet: ProcedureSheet, report: VerificationReport, decision: Mapping[str, Any] | None
) -> tuple[VerificationReport, list[dict[str, Any]]]:
    """`report` without the rejection the owner's ruled ceiling wins over, and that conflict. The
    ruling wins over the free-band reading only: verify_sheet stops at it, so the checks after it
    (another state's band or facilities, another benchmark) run here on the ruled ceiling. A
    rejection they give overturns the ruling, which then keeps nothing: the free-band rejection
    stays beside it, so the sheet is held as the same reading with no ruling is, and no conflict
    goes to the owner."""
    ceiling = sheet.eligibility.assistance_ceiling_fpl
    if ceiling is None or _owner_ruled_ceiling(sheet, decision) is None:
        return report, []
    overridden = [
        (path, reason)
        for path, reason in report.rejected
        if path == CEILING_PATH and reason in RULING_WINS_OVER
    ]
    if not overridden:
        return report, []
    if reason := income_limit_problem(ceiling.quote or "", sheet.hospital.state, ceiling.value):
        return VerificationReport(report.accepted, [*report.rejected, (CEILING_PATH, reason)]), []
    rest = [rejected for rejected in report.rejected if rejected not in overridden]
    kept = VerificationReport(accepted=[*report.accepted, CEILING_PATH], rejected=rest)
    return kept, [_ruling_conflict(sheet, decision, reason) for _, reason in overridden]


def _ruled_ceiling_problems(
    sheet: ProcedureSheet, decision: Mapping[str, Any] | None
) -> tuple[Decimal | None, list[dict[str, Any]]]:
    """The owner's ruled ceiling on `sheet` (for sheet_inconsistencies and decide_status), and the
    sheet-level problems it is spared, as conflicts for the owner."""
    ruled = _owner_ruled_ceiling(sheet, decision)
    if ruled is None:
        return None, []
    spared = sheet_inconsistencies(sheet)
    kept = sheet_inconsistencies(sheet, ruled)
    return ruled, [
        _ruling_conflict(sheet, decision, problem) for problem in spared if problem not in kept
    ]


def _conflict_key(detail: Mapping[str, Any]) -> tuple[Any, ...]:
    """What makes two conflicts one: the rule, the field and the ruling (its value and date). The
    quote may be re-trimmed between runs; the owner settles the reading, not the wording."""
    return tuple(detail.get(key) for key in ("path", "reason", "ruled_value", "decided_at"))


def _record_ruling_conflicts(
    session: Session, ccn: str, conflicts: list[dict[str, Any]], notes: list[str]
) -> int:
    """An open `owner_ruling_conflict` item for each conflict never filed before, open or settled
    (a recheck runs again and again; one the owner resolved stays resolved; a new ruling is a new
    conflict), and a note for each. Returns how many items were filed."""
    if not conflicts:
        return 0
    filed = {
        _conflict_key(item.detail or {})
        for item in repo.review_items_of_kind(session, ccn, OWNER_RULING_CONFLICT)
    }
    new = 0
    for conflict in conflicts:
        notes.append(
            f"{conflict['path']}: the owner's ruling ({conflict['ruled_value']}%) is kept over "
            f'"{conflict["reason"]}"; recorded for the owner'
        )
        if _conflict_key(conflict) not in filed:
            repo.add_review_item(session, ccn, OWNER_RULING_CONFLICT, conflict)
            filed.add(_conflict_key(conflict))
            new += 1
    return new


def _tiebreak(
    session: Session,
    ai: AIClient,
    hospital: HospitalRef,
    sources_with_text: list[tuple[SourceDoc, str]],
    today: date,
    primary: ProcedureSheet,
    secondary: ProcedureSheet,
    conflicts: list[str],
    result: BuildResult,
    decision: Mapping[str, Any] | None = None,
) -> tuple[ProcedureSheet, list[str]]:
    """A third model settles the critical fields the first two dispute (task 2.8j). Best effort
    like the cross-check: when its call fails, the conflicts stand and the primary is kept. Its
    read is held to the owner's ruling like the other two, or it could not agree with a primary
    that was."""
    ccn = hospital.ccn
    try:
        third, _ = structure_sheet(ai, "tiebreak", hospital, sources_with_text, today)
    except AIOutputError as error:
        repo.add_review_item(session, ccn, "tiebreak_failed", {"error": str(error)[:300]})
        result.notes.append("tie-break model gave no usable output")
        return primary, conflicts
    third, _ = keep_owner_decision(third, decision)
    merged, remaining, detail = resolve_conflicts(primary, secondary, third, conflicts)
    texts = {source.id: text for source, text in sources_with_text}
    merged, _pruned = _ground(merged, texts)
    report, _overruled = _keep_owner_ruled_ceiling(merged, verify_sheet(merged, texts), decision)
    if report.rejected:
        # The primary was verified before, so a rejection here is a cross-check field whose quote
        # does not hold up; it settles nothing, the primary's field returns and the conflict stands.
        restored, dropped = [], []
        for path, reason in report.rejected:
            if detail.get(path, {}).get("verdict") == "secondary":
                detail[path]["verdict"] = "unverified"
                restored.append(path)
                result.notes.append(f"{path}: cross-check quote failed verification")
            else:
                dropped.append(path)
                result.notes.append(f"{path}: {reason}")
        merged = drop_fields(copy_fields(merged, primary, restored), dropped)
        remaining = [path for path in conflicts if path in remaining or path in restored]
    repo.add_review_item(session, ccn, "tiebreak", detail)
    settled = [
        f"{path} ({entry['verdict']})"
        for path, entry in detail.items()
        if entry["verdict"] in ("primary", "secondary")
    ]
    if settled:
        result.notes.append("tie-break settled " + ", ".join(settled))
    return merged, remaining


def _unplaced(row: HospitalRow, sources_with_text: list[tuple[SourceDoc, str]]) -> str | None:
    """Why the hospital's web documents cannot be tied to its place, or None. A domain an admin
    confirmed (confidence 1.0) is trusted; a discovered or hand-entered one is checked."""
    if row.domain_confidence == CONFIRMED_CONFIDENCE:
        return None
    web_texts = [
        text for source, text in sources_with_text if source.kind is SourceKind.HOSPITAL_WEB
    ]
    return place_unconfirmed(repo.hospital_ref(row), web_texts)


def _unconfirmed_domain(row: HospitalRow) -> str | None:
    """The website and how sure discovery was, while nobody has confirmed a domain matched with
    low confidence (decision 7.9a-1), or None."""
    if not domain_unconfirmed(row.website_domain, row.domain_confidence):
        return None
    if row.domain_confidence is None:
        return f"{row.website_domain} (no confidence recorded)"
    return f"{row.website_domain} (confidence {row.domain_confidence:g})"


def _hold_for_domain(
    session: Session, row: HospitalRow, unconfirmed: str, holds: list[str], result: BuildResult
) -> None:
    """The fields stay for the admin to compare; "Confirm website" in the console publishes them
    (confirm_domain) and "Wrong website" clears the domain (reject_domain)."""
    repo.add_review_item(
        session,
        row.ccn,
        "domain_unconfirmed",
        {"domain": row.website_domain, "confidence": row.domain_confidence},
    )
    holds.append(DOMAIN_UNCONFIRMED_REASON)
    result.notes.append(f"{DOMAIN_UNCONFIRMED_REASON}: {unconfirmed}")


def _listed_limits(disagreeing: list[dict[str, Any]]) -> str:
    return ", ".join(f"{entry['id']}: {'/'.join(entry['limits'])}%" for entry in disagreeing)


def build_hospital(
    session: Session,
    gateway: TavilyGateway,
    ai: AIClient,
    ccn: str,
    today: date,
    dual: bool = True,
    reuse_sources: bool = False,
    extract_depth: Depth = "basic",
    decision: Mapping[str, Any] | None = None,
) -> BuildResult:
    row = repo.get_hospital(session, ccn)
    if row is None:
        return BuildResult(ccn, "?", "failed", notes=["hospital not in registry"])
    result = BuildResult(ccn, row.name, "skipped")
    if decision is None:
        # The ruling stored with the hospital (`import-eve --db`) holds a build that is given no
        # decision: the admin console's Rebuild button, `waive learn rebuild`, the refresh and the
        # scheduler pass none, and the server has no decisions file. A decision passed in wins.
        decision = stored_ruling(session, ccn)

    if not row.website_domain:
        found = discover_domain(gateway, repo.hospital_ref(row), rejected_domains(session, ccn))
        if found is None:
            repo.add_review_item(session, ccn, "domain", {"found": None})
            result.notes.append("no official domain found")
            return result
        row.website_domain, row.domain_confidence = found.domain, found.confidence
        if found.confidence < MIN_CONFIDENCE:
            repo.add_review_item(
                session, ccn, "domain", {"found": found.domain, "evidence": found.evidence_url}
            )
            result.notes.append(f"domain {found.domain} needs review")
        session.flush()

    hospital = repo.hospital_ref(row)
    # State-repository documents are overlay citations (run_overlay re-attaches them); they are
    # not the hospital's policy and, at 100k+ characters, they drown the structurer.
    sources_with_text = (
        [
            (source, text)
            for source, text in repo.sources_for(session, ccn)
            if source.kind is not SourceKind.STATE_REPOSITORY
        ]
        if reuse_sources
        else []
    )
    if sources_with_text:
        sources_with_text = _drop_navigation_shells(session, ccn, sources_with_text, result)
        if not sources_with_text:
            # Scouting again would fetch the same shell; the site needs a new domain or a
            # hand-set policy URL first, and the open item keeps the scheduler off (FAILURE_KINDS).
            repo.add_review_item(
                session, ccn, "no_documents", {"domain": row.website_domain, "shells_only": True}
            )
            result.notes.append(
                "no financial assistance documents left once the navigation shell is set aside; "
                "the hospital's policy is not in the stored documents"
            )
            return result
    borrowed: Borrowed | None = None
    if sources_with_text and reuse_sources:
        borrowed = _previously_borrowed(session, ccn, sources_with_text)
    if not sources_with_text and reuse_sources:
        # Nothing stored and no credits to spend: a sibling's documents on the same site first.
        borrowed = _share_sibling_sources(session, row, [], result)
        if borrowed is not None:
            sources_with_text = borrowed.sources
    if not sources_with_text:
        docs = scout_hospital(gateway, hospital, extract_depth=extract_depth)
        if docs:
            sources = store_scouted(session, ccn, docs, today)
            sources_with_text = [(s, doc.text) for s, doc in zip(sources, docs, strict=True)]
        else:
            borrowed = _share_sibling_sources(session, row, [], result)
            if borrowed is None:
                repo.add_review_item(session, ccn, "no_documents", {"domain": row.website_domain})
                result.notes.append("no financial assistance documents found")
                return result
            sources_with_text = borrowed.sources
    considered = {source.id for source, _ in sources_with_text}
    # The state's hospitals, for the covered-hospital lists another organisation's policy carries.
    registry = repo.list_hospitals(session, state=row.state)
    sources_with_text, translations = _documents_for_structurer(
        session, hospital, sources_with_text, registry, result
    )
    if not sources_with_text:
        # Every document was set aside: a sibling's policy on the same site next, as when nothing
        # is stored. Giving up here, the same command run again would find nothing stored and
        # borrow (San Francisco, 050076, from Los Angeles; review of 7.16). Documents an earlier
        # build borrowed are not this build's sharing, so they do not stop it.
        borrowed = _share_sibling_sources(
            session,
            row,
            [],
            result,
            "none of the hospital's own documents is left once those are set aside",
        )
        if borrowed is not None:
            considered |= borrowed.shared_ids
            sources_with_text, translations = _documents_for_structurer(
                session, hospital, borrowed.sources, registry, result
            )
    if not sources_with_text:
        return _nothing_left_to_structure(session, row, considered, result)
    # What the build detached (a dental program's, another state's): when the structurer then
    # fails, the sheet must not go on quoting it (review of 7.16).
    detached = considered - {source.id for source, _ in [*sources_with_text, *translations]}

    first_pass = _structure_and_verify(
        session, ai, hospital, sources_with_text, today, result, decision
    )
    if first_pass is None:
        _hold_without(session, ccn, detached, result)
        return result
    sheet, holds = first_pass
    if borrowed is None and not holds and _lacks_income_rules(sheet):
        # The hospital's own pages gave no income rule: a sibling's policy on the same site may
        # (Advocate Christ: a language-menu shell of its own, the enterprise policy on Advocate
        # Illinois Masonic's sheet). One more structuring pass, Token Factory only. The hospital's
        # own translations set aside on the first pass stay its own: Baptist Beaches and Nassau
        # link one Tagalog policy, and it is not the sibling's to lend (review of 7.15b).
        borrowed = _share_sibling_sources(session, row, [*sources_with_text, *translations], result)
        if borrowed is not None:
            lent = {source.id for source, _ in borrowed.sources}
            sources_with_text, shared_translations = _documents_for_structurer(
                session, hospital, borrowed.sources, registry, result
            )
            translations = [*translations, *shared_translations]
            detached |= lent - {source.id for source, _ in [*sources_with_text, *translations]}
            second_pass = _structure_and_verify(
                session, ai, hospital, sources_with_text, today, result, decision
            )
            if second_pass is None:
                _hold_without(session, ccn, detached, result)
                return result
            sheet, holds = second_pass
    if borrowed is not None:
        sheet = _drop_borrowed_specifics(sheet, borrowed, result)
    texts = {source.id: text for source, text in sources_with_text}

    conflicts: list[str] = []
    if dual:
        try:
            secondary, _ = structure_sheet(ai, "fast", hospital, sources_with_text, today)
        except AIOutputError as error:
            # The cross-check is best effort: record the failure, keep the verified primary.
            repo.add_review_item(
                session, ccn, "crosscheck_failed", {"error": str(error)[:ERROR_DETAIL_CHARS]}
            )
            result.notes.append("cross-check model gave no usable output")
        else:
            # A disagreement only counts when the cross-check's own quote verifies; an
            # ungrounded value (for example a bare "300%") cannot veto a verified primary. The
            # cross-check is held to the owner's ruling like the primary, or it would dispute the
            # ceiling the ruling made of a value both models read.
            secondary, _ = keep_owner_decision(secondary, decision)
            secondary, _pruned = _ground(secondary, texts)
            checked, _overruled = _keep_owner_ruled_ceiling(
                secondary, verify_sheet(secondary, texts), decision
            )
            ungrounded = [path for path, _ in checked.rejected]
            if ungrounded:
                secondary = drop_fields(secondary, ungrounded)
                result.notes.append(
                    "cross-check values ignored (unverified): " + ", ".join(ungrounded)
                )
            conflicts = critical_conflicts(sheet, secondary)
            if conflicts:
                repo.add_review_item(session, ccn, "conflict", {"paths": conflicts})
            if conflicts == ["programs.presumptive"]:
                # Income rules agree; publish them and leave the disputed program list to review.
                sheet = drop_fields(sheet, conflicts)
                conflicts = []
                result.notes.append("presumptive programs disagree; published without them")
            if conflicts:
                sheet, conflicts = _tiebreak(
                    session,
                    ai,
                    hospital,
                    sources_with_text,
                    today,
                    sheet,
                    secondary,
                    conflicts,
                    result,
                    decision,
                )
            if conflicts:
                result.notes.append("critical fields disagree: " + ", ".join(conflicts))

    previous = repo.latest_sheet(session, ccn)
    if previous is not None:
        set_aside = detached | {source.id for source, _ in translations}
        sheet = carry_over_state_programs(sheet, previous[0], set_aside)
        sheet = carry_over_reported(sheet, previous[0])
    ruled, overruled = _ruled_ceiling_problems(sheet, decision)
    _record_ruling_conflicts(session, ccn, overruled, result.notes)
    inconsistent = sheet_inconsistencies(sheet, ruled)
    if inconsistent:
        # Both models can make the same misread (a ceiling as free care), so the cross-check
        # never sees it; the sheet is held and an admin decides.
        repo.add_review_item(session, ccn, "inconsistent", {"problems": inconsistent})
        result.notes.extend(inconsistent)
    disagreeing = document_conflicts(sheet, texts)
    if disagreeing:
        # Two of the hospital's own documents name different free-care limits (AHMC Anaheim: a
        # web page at 250%, the newer PDF at 200%), or they state more than the ceiling a sheet
        # would publish alone (7.14); which one is right is an admin's call.
        free = sheet.eligibility.free_care_max_fpl
        path = "eligibility.free_care_max_fpl" if free else "eligibility.assistance_ceiling_fpl"
        rule = free or sheet.eligibility.assistance_ceiling_fpl
        repo.add_review_item(
            session,
            ccn,
            "document_conflict",
            {
                "path": path,
                "published": str(rule.value) if rule else None,
                "source_id": rule.source_id if rule else None,
                "documents": disagreeing,
            },
        )
        reason = conflict_reason(sheet)
        holds.append(reason)
        result.notes.append(f"{reason} ({_listed_limits(disagreeing)})")
    # Translations still say where the hospital is, as they do for recheck_sheets.
    unplaced = _unplaced(row, [*sources_with_text, *translations])
    if unplaced:
        repo.add_review_item(
            session,
            ccn,
            "place_unconfirmed",
            {"domain": row.website_domain, "reason": unplaced},
        )
        holds.append(PLACE_UNCONFIRMED_REASON)
        result.notes.append(f"{PLACE_UNCONFIRMED_REASON}: {unplaced}")
    unconfirmed = _unconfirmed_domain(row)
    if unconfirmed:
        _hold_for_domain(session, row, unconfirmed, holds, result)
    status = decide_status(sheet, conflicts, holds, ruled)
    published = publish_sheet(session, sheet.model_copy(update={"status": status}))
    result.outcome = "published" if status.value == "published" else "held"
    result.version = (
        published.version
        if published
        else (repo.latest_sheet(session, ccn) or (None, None))[0].version
    )
    return result


def build_state(
    session: Session,
    gateway: TavilyGateway,
    ai: AIClient,
    state: str,
    today: date,
    limit: int | None = None,
    dual: bool = True,
    only_missing: bool = True,
    reuse_sources: bool = False,
    extract_depth: Depth = "basic",
    skip: Mapping[str, SkipEntry] | None = None,
    decisions: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[BuildResult]:
    """Build the state's hospitals that have no sheet yet (all of them with `only_missing` off).
    A hospital on the skip list (the ones Eve could not build) is never built and adds no result;
    `limit` counts hospitals built. `decisions` are the owner's rulings by CCN: each hospital is
    built with its own."""
    results: list[BuildResult] = []
    registry = repo.list_hospitals(session, state=state)
    hospitals = [row for row in registry if skip is None or row.ccn not in skip]
    if left_out := len(registry) - len(hospitals):
        log.info("skip list: %d hospitals in %s left out", left_out, state.upper())
    for row in hospitals:
        if limit is not None and len(results) >= limit:
            break
        if only_missing and repo.latest_sheet(session, row.ccn) is not None:
            continue
        try:
            results.append(
                build_hospital(
                    session,
                    gateway,
                    ai,
                    row.ccn,
                    today,
                    dual=dual,
                    reuse_sources=reuse_sources,
                    extract_depth=extract_depth,
                    decision=decisions.get(row.ccn) if decisions else None,
                )
            )
        except Exception as error:  # keep the batch going; the failure is in the report
            results.append(BuildResult(row.ccn, row.name, "failed", notes=[type(error).__name__]))
        session.commit()
    return results


def clear_wrong_domain(session: Session, ccn: str, reason: str) -> BuildResult:
    """An admin found that the discovered domain is not the hospital's site, so nothing scouted
    from it is the hospital's policy: its web documents are detached, every field they backed is
    dropped and the sheet is held in a new version (the state overlay and its source stay); the
    domain is cleared so the next build discovers it again, and a `domain` review item records
    why (and keeps the scheduler off this hospital meanwhile, schedule.FAILURE_KINDS). Harrington
    Hospital (220019): discovery picked billfairly.com, a billing directory, and the published
    free-care limit was quoted from that directory's page about a hospital in Vidalia, Georgia.

    The hospital's web documents on asset hosts go too: the scout reaches an asset host only by a
    link on the hospital's own site (scout.policy_links), so they were linked from the rejected
    one, and left behind they kept its limits on the held sheet (review of 7.13)."""
    row = repo.get_hospital(session, ccn)
    if row is None:
        return BuildResult(ccn, "?", "failed", notes=["hospital not in registry"])
    result = BuildResult(ccn, row.name, "skipped")
    domain = row.website_domain
    if not domain:
        result.notes.append("no domain set")
        return result
    row.website_domain, row.domain_confidence = None, None
    detached = [
        source
        for source, _ in repo.sources_for(session, ccn)
        if source.kind is SourceKind.HOSPITAL_WEB
        and (host_of(source.url or "") == domain or is_asset_host(source.url or ""))
    ]
    for source in detached:
        repo.unlink_source(session, ccn, source.id)
    gone = {source.id for source in detached}
    repo.add_review_item(
        session,
        ccn,
        "domain",
        {"found": None, "cleared": domain, "reason": reason, "sources": sorted(gone)},
    )
    result.notes.append(f"domain {domain} cleared ({reason}); {len(detached)} documents detached")
    latest = repo.latest_sheet(session, ccn)
    if latest is None:
        return result
    dropped = [path for path, cited in latest[0].field_paths() if cited.source_id in gone]
    sheet = drop_fields(latest[0], dropped).model_copy(
        update={
            "hospital": repo.hospital_ref(row),
            "status": SheetStatus.HELD,
            "sources": [source for source in latest[0].sources if source.id not in gone],
        }
    )
    published = publish_sheet(session, sheet)
    result.outcome = "held"
    result.version = published.version if published else latest[0].version
    if dropped:
        result.notes.append(
            f"{len(dropped)} fields quoted from them dropped: " + ", ".join(dropped)
        )
    return result


def _state_registries(rows: list[HospitalRow]) -> dict[str, list[HospitalRow]]:
    """`rows` by state. recheck_sheets and withdraw_rejected list the whole registry or one
    state's, so each state's list is all of its hospitals, as _documents_set_aside needs them."""
    registries: dict[str, list[HospitalRow]] = {}
    for row in rows:
        registries.setdefault(row.state, []).append(row)
    return registries


@dataclass
class Recheck:
    ccn: str
    name: str
    state: str
    status: SheetStatus
    rejected: list[tuple[str, str]]

    @property
    def waits_on_website(self) -> bool:
        """Listed only for the sheet-level checks, the unconfirmed website among them: a rebuild
        would be held again, so the remedy is `--withdraw`, then the admin's Confirm website
        (which also stands the place check down and re-checks the sheet in place, a gap between
        its bands included) or Wrong website."""
        return all(path == "sheet" for path, _ in self.rejected) and any(
            reason.startswith(DOMAIN_UNCONFIRMED_REASON) for _, reason in self.rejected
        )


def recheck_sheets(session: Session, state: str | None = None) -> list[Recheck]:
    """Every latest sheet whose stored fields the current verification rules reject, with the
    paths and reasons, and every published one the place or website check would hold (path
    "sheet"). No model call and no credits: a what-if over stored quotes and texts, run
    after a verifier change to find the published sheets to rebuild before exporting (four
    national sheets and sixteen Massachusetts fields kept fields the 7.9 rules had been written
    for, because they were not rebuilt; review of 7.9)."""
    found: list[Recheck] = []
    rows = repo.list_hospitals(session, state=state)
    registries = _state_registries(rows)
    for row in rows:
        if repo.is_demo(row.ccn):
            continue
        latest = repo.latest_sheet(session, row.ccn)
        if latest is None:
            continue
        sheet = latest[0]
        sources = repo.sources_for(session, row.ccn)
        texts = {source.id: text for source, text in sources}
        set_aside = _set_aside_for(session, row, sources, registries[row.state])
        decision = stored_ruling(session, row.ccn)
        report, _overruled = _keep_owner_ruled_ceiling(
            sheet, verify_sheet(sheet, texts, set_aside), decision
        )
        rejected = list(report.rejected)
        if sheet.status is SheetStatus.PUBLISHED:
            # The income rules left once the rejected fields go, as withdraw_rejected judges them.
            withdrawn = drop_fields(sheet, [path for path, _ in report.rejected])
            ruled, _spared = _ruled_ceiling_problems(withdrawn, decision)
            rejected.extend(
                ("sheet", problem) for problem in sheet_inconsistencies(withdrawn, ruled)
            )
        if sheet.status is SheetStatus.PUBLISHED and (unplaced := _unplaced(row, sources)):
            rejected.append(("sheet", f"{PLACE_UNCONFIRMED_REASON}: {unplaced}"))
        if sheet.status is SheetStatus.PUBLISHED and (unconfirmed := _unconfirmed_domain(row)):
            rejected.append(("sheet", f"{DOMAIN_UNCONFIRMED_REASON}: {unconfirmed}"))
        if rejected:
            found.append(Recheck(row.ccn, row.name, row.state, sheet.status, rejected))
    return found


def withdraw_rejected(
    session: Session, state: str | None = None, published_only: bool = True
) -> list[BuildResult]:
    """Re-verify each latest sheet under the current rules and publish a new version without the
    fields they reject: lists cut to the items their quotes name, everything else dropped. No
    model call: it is the pipeline's grounding and verification steps run again over stored
    quotes and texts, so a verifier fix reaches published sheets the same day without spending
    on a rebuild (the Massachusetts batch predates the 7.9 asset, insured and list rules; review
    of 7.9). A held sheet stays held; a published one keeps its status unless it loses its income
    rules, a rejection holds it (verify.HOLDING_REASONS), its income rules contradict each other
    (publish.sheet_inconsistencies: a published sheet is held for that even when no field is
    rejected, as Henry County, 361309, is for incomes from 100% to 200% no rule covers), its
    documents cannot be tied to the hospital's place, or its website was matched with low
    confidence and nobody confirmed it (decision 7.9a-1). A `recheck` review item records what
    went, when anything did; an `inconsistent` item the contradiction; an open
    `owner_ruling_conflict` item a reading an owner's ruling was kept over (a "skipped" result
    says which went to the owner)."""
    results: list[BuildResult] = []
    rows = repo.list_hospitals(session, state=state)
    registries = _state_registries(rows)
    for row in rows:
        if repo.is_demo(row.ccn):
            continue
        latest = repo.latest_sheet(session, row.ccn)
        if latest is None:
            continue
        sheet = latest[0]
        if published_only and sheet.status is not SheetStatus.PUBLISHED:
            continue
        sources = repo.sources_for(session, row.ccn)
        texts = {source.id: text for source, text in sources}
        grounded, pruned = _ground(sheet, texts)
        set_aside = _set_aside_for(session, row, sources, registries[row.state])
        decision = stored_ruling(session, row.ccn)
        report, overruled = _keep_owner_ruled_ceiling(
            grounded, verify_sheet(grounded, texts, set_aside), decision
        )
        withdrawn = drop_fields(grounded, [path for path, _ in report.rejected])
        ruled, spared = _ruled_ceiling_problems(withdrawn, decision)
        conflict_notes: list[str] = []
        # Filed even when nothing else changes: the conflict is the owner's to settle.
        filed = _record_ruling_conflicts(session, row.ccn, [*overruled, *spared], conflict_notes)
        is_published = sheet.status is SheetStatus.PUBLISHED
        unplaced = _unplaced(row, sources) if is_published else None
        unconfirmed = _unconfirmed_domain(row) if is_published else None
        # The sheet-level checks too: a published sheet with no rejected field may still leave
        # incomes no rule covers (Henry County, 361309: free care to 100%, bands from 200%).
        inconsistent = sheet_inconsistencies(withdrawn, ruled) if is_published else []
        if not (pruned or report.rejected or unplaced or unconfirmed or inconsistent):
            if filed:
                # Nothing to withdraw, but the owner was sent something: say what.
                results.append(
                    BuildResult(row.ccn, row.name, "skipped", sheet.version, conflict_notes)
                )
            continue
        result = BuildResult(row.ccn, row.name, "skipped")
        result.notes.extend(pruned)
        result.notes.extend(f"{path}: {reason}" for path, reason in report.rejected)
        result.notes.extend(conflict_notes)
        # A ceiling read from the free-care band (Russell Regional, 171350) or a band from another
        # state's facilities (Trinity Bettendorf, 160104) leaves a worse sheet once dropped.
        holds = _holding_reasons(report.rejected)
        _file_patient_share_table(session, row.ccn, grounded, holds)
        if inconsistent:
            repo.add_review_item(session, row.ccn, "inconsistent", {"problems": inconsistent})
            result.notes.extend(inconsistent)
        if unplaced:
            # The fields stay for the admin to compare; the sheet leaves the public atlas.
            repo.add_review_item(
                session,
                row.ccn,
                "place_unconfirmed",
                {"domain": row.website_domain, "reason": unplaced},
            )
            holds.append(PLACE_UNCONFIRMED_REASON)
            result.notes.append(f"{PLACE_UNCONFIRMED_REASON}: {unplaced}")
        if unconfirmed:
            _hold_for_domain(session, row, unconfirmed, holds, result)
        status = (
            decide_status(withdrawn, [], holds, ruled)
            if sheet.status is SheetStatus.PUBLISHED
            else SheetStatus.HELD
        )
        if pruned or report.rejected:
            repo.add_review_item(
                session, row.ccn, "recheck", {"pruned": pruned, "rejected": report.rejected}
            )
        published = publish_sheet(session, withdrawn.model_copy(update={"status": status}))
        result.outcome = "published" if status is SheetStatus.PUBLISHED else "held"
        result.version = published.version if published else sheet.version
        results.append(result)
        session.commit()
    return results


# A stored sheet cannot show every reason its build held it: neither model's draft nor the
# tie-break's is kept, and verification dropped a discount table that states the patient's share.
REBUILD_TO_RECHECK = "re-checking that needs model calls (Rebuild from stored documents)"
WRONG_WEBSITE_REASON = "an admin found it is not the hospital's website"


def _close_website_questions(session: Session, ccn: str, domain: str) -> None:
    """An admin answered whether `domain` is the hospital's own website: the open review items
    that asked are resolved, namely every website hold (whichever site it named: the hospital's
    website is settled either way), and the place check (a confirmed domain stands it down; a
    wrong one takes its documents away) and discovery's low-confidence `domain` item about it."""
    for item in repo.open_review_items(session, ccn):
        detail = item.detail or {}
        asked = detail.get("found") if item.kind == "domain" else detail.get("domain")
        if item.kind == "domain_unconfirmed" or (
            item.kind in ("domain", "place_unconfirmed") and asked == domain
        ):
            item.status = "resolved"
    session.flush()


def _recorded_holds(session: Session, ccn: str) -> tuple[list[str], list[tuple[str, str]]]:
    """What the builds and rechecks since the sheet was last published (all of them, when it never
    was) recorded and a stored sheet cannot show: the critical fields the two models disputed and
    no tie-break settled, and each dropped field whose reason holds the sheet (path, reason): a
    discount table that stated the patient's share, a ceiling read from the free-care band, a band
    from another state's facilities. A published version means its build had none left, so older
    review items do not count."""
    published = SheetStatus.PUBLISHED.value
    last = next((row for row in repo.sheet_versions(session, ccn) if row.status == published), None)
    disputed: set[str] = set()
    held: dict[tuple[str, str], None] = {}
    for item in repo.review_items_since(session, ccn, last.created_at if last else None):
        detail = item.detail or {}
        if item.kind == "conflict" and detail.get("paths") != ["programs.presumptive"]:
            disputed.update(detail.get("paths", []))
        elif item.kind == "tiebreak":
            disputed.difference_update(
                path
                for path, entry in detail.items()
                if entry.get("verdict") in ("primary", "secondary")
            )
        elif item.kind == "patient_share_table":
            held[("eligibility.discount_tiers", PATIENT_SHARE_REASON)] = None
        elif item.kind in ("verification", "recheck"):
            for path, reason in detail.get("rejected", []):
                if reason in HOLDING_REASONS:
                    held[(path, reason)] = None
    return sorted(disputed), list(held)


def confirm_domain(session: Session, ccn: str, domain: str) -> BuildResult:
    """An admin confirmed in the console that `domain` is the hospital's own website (decision
    7.9a-1): its confidence becomes 1.0, which also stands the place check down, the review items
    that asked are resolved, and the latest sheet is re-verified in place as `withdraw_rejected`
    does it, with no model call. It publishes when nothing else holds it. Every other reason is
    re-checked from the stored quotes and documents and named in the notes, except the holds a
    stored sheet cannot show (`_recorded_holds`): they keep it held, and the notes say that
    re-checking them takes a rebuild. A sheet built from another website (the domain moved after
    its build) is left as it is: its fields are that site's, so it waits for a build that scouts
    the confirmed one. Each confirmation is recorded in a resolved `website_confirmed` item."""
    row = repo.lock_hospital(session, ccn)
    if row is None:
        return BuildResult(ccn, "?", "failed", notes=["hospital not in registry"])
    result = BuildResult(ccn, row.name, "skipped")
    if row.website_domain != domain:
        # The admin's page is older than the hospital's current website.
        current = row.website_domain or "not set"
        result.notes.append(f"the website is {current} now, not {domain}; nothing confirmed")
        return result
    if row.domain_confidence != CONFIRMED_CONFIDENCE:
        record = repo.add_review_item(
            session,
            ccn,
            "website_confirmed",
            {"domain": domain, "previous_confidence": row.domain_confidence},
        )
        record.status = "resolved"
    row.domain_confidence = CONFIRMED_CONFIDENCE
    _close_website_questions(session, ccn, domain)
    result.notes.append(f"website {domain} confirmed")
    latest = repo.latest_sheet(session, ccn)
    if latest is None:
        result.notes.append("no sheet yet")
        return result
    sheet = latest[0]
    built_from = sheet.hospital.website_domain
    if built_from != domain:
        was = (
            f"was built from {built_from}, not {domain}"
            if built_from
            else f"was not built from {domain}"
        )
        result.notes.append(
            f"the sheet {was}; it stays {sheet.status.value} until a build scouts it: "
            f"uv run waive atlas build --state {row.state} --ccn {ccn}"
        )
        result.outcome = "published" if sheet.status is SheetStatus.PUBLISHED else "held"
        result.version = sheet.version
        return result
    sources = repo.sources_for(session, ccn)
    texts = {source.id: text for source, text in sources}
    grounded, pruned = _ground(sheet, texts)
    decision = stored_ruling(session, ccn)
    set_aside = _set_aside_for(session, row, sources, repo.list_hospitals(session, state=row.state))
    report, overruled = _keep_owner_ruled_ceiling(
        grounded, verify_sheet(grounded, texts, set_aside), decision
    )
    result.notes.extend(pruned)
    result.notes.extend(f"{path}: {reason}" for path, reason in report.rejected)
    # Read before this re-check files its own item: what it rejects is noted above, not recorded.
    disputed, recorded = _recorded_holds(session, ccn)
    if pruned or report.rejected:
        repo.add_review_item(
            session, ccn, "recheck", {"pruned": pruned, "rejected": report.rejected}
        )
    _file_patient_share_table(session, ccn, grounded, _holding_reasons(report.rejected))
    reverified = drop_fields(grounded, [path for path, _ in report.rejected])
    ruled, spared = _ruled_ceiling_problems(reverified, decision)
    _record_ruling_conflicts(session, ccn, [*overruled, *spared], result.notes)
    if disputed:
        listed = ", ".join(disputed)
        result.notes.append(f"critical fields disagree: {listed}; {REBUILD_TO_RECHECK}")
    result.notes.extend(f"{path}: {reason}; {REBUILD_TO_RECHECK}" for path, reason in recorded)
    holds = _holding_reasons([*recorded, *report.rejected])
    disagreeing = document_conflicts(reverified, texts)
    if disagreeing:
        reason = conflict_reason(reverified)
        holds.append(reason)
        result.notes.append(f"{reason} ({_listed_limits(disagreeing)})")
    if _lacks_income_rules(reverified) and reverified.eligibility.assistance_ceiling_fpl is None:
        result.notes.append(
            "no free-care band, discount table or assistance ceiling is left to publish"
        )
    inconsistent = sheet_inconsistencies(reverified, ruled)
    if inconsistent:
        # Recorded as a build and a withdraw record it: the note goes with this one request.
        repo.add_review_item(session, ccn, "inconsistent", {"problems": inconsistent})
    result.notes.extend(inconsistent)
    status = decide_status(reverified, disputed, holds, ruled)
    published = publish_sheet(session, reverified.model_copy(update={"status": status}))
    result.outcome = "published" if status is SheetStatus.PUBLISHED else "held"
    result.version = published.version if published else sheet.version
    return result


def reject_domain(session: Session, ccn: str, domain: str, reason: str = "") -> BuildResult:
    """An admin found in the console that `domain` is not the hospital's website: it is cleared
    as `clear_wrong_domain` does it (its documents detached, the fields they backed dropped, the
    sheet held; discovery never picks it again) and the open review items that asked about it
    are resolved."""
    row = repo.lock_hospital(session, ccn)
    if row is None:
        return BuildResult(ccn, "?", "failed", notes=["hospital not in registry"])
    if row.website_domain != domain:
        current = row.website_domain or "not set"
        note = f"the website is {current} now, not {domain}; nothing cleared"
        return BuildResult(ccn, row.name, "skipped", notes=[note])
    result = clear_wrong_domain(session, ccn, reason or WRONG_WEBSITE_REASON)
    _close_website_questions(session, ccn, domain)
    return result


def coverage_report(session: Session, state: str) -> str:
    rows = [r for r in repo.list_hospitals(session, state=state) if not repo.is_demo(r.ccn)]
    lines = [
        f"# Atlas coverage — {state.upper()}",
        "",
        f"Hospitals in registry: {len(rows)}",
        "",
        "| CCN | Hospital | Domain | Status | Version | Completeness | Fields | Sources |",
        "|---|---|---|---|---|---|---|---|",
    ]
    published = 0
    for row in rows:
        found = repo.latest_sheet(session, row.ccn)
        if found is None:
            lines.append(f"| {row.ccn} | {row.name} | {row.website_domain or ''} | none | | | | |")
            continue
        sheet, _ = found
        published += sheet.status.value == "published"
        lines.append(
            f"| {row.ccn} | {row.name} | {row.website_domain or ''} | {sheet.status.value} | "
            f"{sheet.version} | {sheet.completeness():.2f} | {len(sheet.field_paths())} | "
            f"{len(sheet.sources)} |"
        )
    lines.insert(
        3,
        f"Published sheets: {published} ({published / len(rows):.0%})"
        if rows
        else "Published sheets: 0",
    )
    return "\n".join(lines) + "\n"
