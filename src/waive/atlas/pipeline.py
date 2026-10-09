"""End-to-end atlas build for one hospital or one state (spec §8)."""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from sqlalchemy.orm import Session

from waive.ai.client import MAX_SERVER_MESSAGE_CHARS, AIClient, AIOutputError
from waive.atlas import repo
from waive.atlas.discover import MIN_CONFIDENCE, discover_domain, host_of
from waive.atlas.publish import (
    DOCUMENT_CONFLICT_REASON,
    carry_over_reported,
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
from waive.atlas.scout import navigation_shells, scout_hospital, store_scouted
from waive.atlas.structure import document_states, structure_sheet
from waive.atlas.tavily_gateway import TavilyGateway
from waive.atlas.verify import PATIENT_SHARE_REASON, prune_lists, trim_quotes, verify_sheet
from waive.db import HospitalRow, ReviewItemRow

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


def carry_over_state_programs(sheet: ProcedureSheet, previous: ProcedureSheet) -> ProcedureSheet:
    """A rebuild re-reads hospital documents only; the state overlay (and its source) must survive."""
    if sheet.programs.state_programs is not None or previous.programs.state_programs is None:
        return sheet
    cited = previous.programs.state_programs
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
    return [(source, text) for source, text in sources_with_text if source.url not in shells]


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
    state's nothing is set aside: there is nothing to prefer."""
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
    if not other or len(other) == len(sources_with_text):
        return sources_with_text
    for source in other:
        repo.unlink_source(session, ccn, source.id)
    detail = [{"id": source.id, "states": sorted(named[source.id])} for source in other]
    repo.add_review_item(session, ccn, "other_state_documents", {"state": state, "sources": detail})
    listed = ", ".join(f"{entry['id']}: {'/'.join(entry['states'])}" for entry in detail)
    plural = "s are" if len(other) > 1 else " is"
    result.notes.append(
        f"{len(other)} stored document{plural} another state's version of the system policy "
        f"({listed}); detached and left out of the structurer"
    )
    gone = {source.id for source in other}
    return [(source, text) for source, text in sources_with_text if source.id not in gone]


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
) -> Borrowed | None:
    """The hospital's documents plus the policy documents of a sibling on the same website that
    has a published sheet, or None when no sibling can help. Searching a system site by hospital
    name returns a thin page or nothing while the system's policy is already stored for a
    sibling (Adventist Health White Memorial, AdventHealth Riverview, Advocate Christ and
    Condell, task 7.9). Only documents that state income rules (they name the poverty level) are
    shared, they are linked to this hospital so --reuse-sources finds them next time, and a
    `shared_sources` item records whose they were: a system may keep per-hospital variants."""
    domain = row.website_domain
    if not domain:
        return None
    own_ids = {source.id for source, _ in own}
    for sibling in repo.siblings_on_domain(session, domain, row.ccn):
        latest = repo.latest_sheet(session, sibling.ccn)
        if latest is None or latest[0].status is not SheetStatus.PUBLISHED:
            continue
        shared = [
            (source, text)
            for source, text in repo.sources_for(session, sibling.ccn)
            if source.kind is SourceKind.HOSPITAL_WEB
            and source.id not in own_ids
            and INCOME_RULE_WORDS.search(text)
        ]
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
            f"{domain}; the hospital's own documents state no income rules"
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
    return sheet.eligibility.free_care_max_fpl is None and sheet.eligibility.discount_tiers is None


def _ground(sheet: ProcedureSheet, texts: dict[str, str]) -> tuple[ProcedureSheet, list[str]]:
    """Each quote cut to the span its source contains, then each list cut to the items that span
    names. The structurer filtered lists against the model's whole quote; the kept sentence may
    name fewer (review of 7.9), so the pruning runs again here, where verify_sheet checks it."""
    return prune_lists(trim_quotes(sheet, texts), texts)


def _structure_and_verify(
    session: Session,
    ai: AIClient,
    hospital: HospitalRef,
    sources_with_text: list[tuple[SourceDoc, str]],
    today: date,
    result: BuildResult,
) -> tuple[ProcedureSheet, list[str]] | None:
    """The primary draft, trimmed to the spans the sources contain, verified and stripped of
    rejected fields, with the reasons an admin must look first (holds). None when the structurer
    gave nothing usable; `result` then carries the failed outcome."""
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
    sheet, pruned = _ground(sheet, texts)
    skipped = [*skipped, *pruned]
    report = verify_sheet(sheet, texts)
    if skipped or report.rejected:
        repo.add_review_item(
            session, ccn, "verification", {"skipped": skipped, "rejected": report.rejected}
        )
        result.notes.extend(skipped)
        result.notes.extend(f"{path}: {reason}" for path, reason in report.rejected)
    holds: list[str] = []
    if any(reason == PATIENT_SHARE_REASON for _, reason in report.rejected):
        # The table says what the patient pays (co-pay, X% of charges); the schema cannot express
        # that band, and publishing the free-care remainder alone would deny the co-pay band.
        tiers = sheet.eligibility.discount_tiers
        repo.add_review_item(
            session, ccn, "patient_share_table", {"quote": tiers.quote if tiers else None}
        )
        holds.append(PATIENT_SHARE_REASON)
    return drop_fields(sheet, [path for path, _ in report.rejected]), holds


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
) -> tuple[ProcedureSheet, list[str]]:
    """A third model settles the critical fields the first two dispute (task 2.8j). Best effort
    like the cross-check: when its call fails, the conflicts stand and the primary is kept."""
    ccn = hospital.ccn
    try:
        third, _ = structure_sheet(ai, "tiebreak", hospital, sources_with_text, today)
    except AIOutputError as error:
        repo.add_review_item(session, ccn, "tiebreak_failed", {"error": str(error)[:300]})
        result.notes.append("tie-break model gave no usable output")
        return primary, conflicts
    merged, remaining, detail = resolve_conflicts(primary, secondary, third, conflicts)
    texts = {source.id: text for source, text in sources_with_text}
    merged, _pruned = _ground(merged, texts)
    report = verify_sheet(merged, texts)
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


def build_hospital(
    session: Session,
    gateway: TavilyGateway,
    ai: AIClient,
    ccn: str,
    today: date,
    dual: bool = True,
    reuse_sources: bool = False,
) -> BuildResult:
    row = repo.get_hospital(session, ccn)
    if row is None:
        return BuildResult(ccn, "?", "failed", notes=["hospital not in registry"])
    result = BuildResult(ccn, row.name, "skipped")

    if not row.website_domain:
        found = discover_domain(gateway, repo.hospital_ref(row))
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
        docs = scout_hospital(gateway, hospital)
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
    sources_with_text = _drop_other_state_documents(session, hospital, sources_with_text, result)

    first_pass = _structure_and_verify(session, ai, hospital, sources_with_text, today, result)
    if first_pass is None:
        return result
    sheet, holds = first_pass
    if borrowed is None and not holds and _lacks_income_rules(sheet):
        # The hospital's own pages gave no income rule: a sibling's policy on the same site may
        # (Advocate Christ: a language-menu shell of its own, the enterprise policy on Advocate
        # Illinois Masonic's sheet). One more structuring pass, Token Factory only.
        borrowed = _share_sibling_sources(session, row, sources_with_text, result)
        if borrowed is not None:
            sources_with_text = _drop_other_state_documents(
                session, hospital, borrowed.sources, result
            )
            second_pass = _structure_and_verify(
                session, ai, hospital, sources_with_text, today, result
            )
            if second_pass is None:
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
            # ungrounded value (for example a bare "300%") cannot veto a verified primary.
            secondary, _pruned = _ground(secondary, texts)
            ungrounded = [path for path, _ in verify_sheet(secondary, texts).rejected]
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
                )
            if conflicts:
                result.notes.append("critical fields disagree: " + ", ".join(conflicts))

    previous = repo.latest_sheet(session, ccn)
    if previous is not None:
        sheet = carry_over_state_programs(sheet, previous[0])
        sheet = carry_over_reported(sheet, previous[0])
    inconsistent = sheet_inconsistencies(sheet)
    if inconsistent:
        # Both models can make the same misread (a ceiling as free care), so the cross-check
        # never sees it; the sheet is held and an admin decides.
        repo.add_review_item(session, ccn, "inconsistent", {"problems": inconsistent})
        result.notes.extend(inconsistent)
    disagreeing = document_conflicts(sheet, texts)
    if disagreeing:
        # Two of the hospital's own documents name different free-care limits (AHMC Anaheim: a
        # web page at 250%, the newer PDF at 200%); which one is current is an admin's call.
        free = sheet.eligibility.free_care_max_fpl
        repo.add_review_item(
            session,
            ccn,
            "document_conflict",
            {
                "path": "eligibility.free_care_max_fpl",
                "published": str(free.value) if free else None,
                "source_id": free.source_id if free else None,
                "documents": disagreeing,
            },
        )
        holds.append(DOCUMENT_CONFLICT_REASON)
        listed = ", ".join(f"{entry['id']}: {'/'.join(entry['limits'])}%" for entry in disagreeing)
        result.notes.append(f"{DOCUMENT_CONFLICT_REASON} ({listed})")
    status = decide_status(sheet, conflicts, holds)
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
) -> list[BuildResult]:
    results: list[BuildResult] = []
    for row in repo.list_hospitals(session, state=state):
        if limit is not None and len(results) >= limit:
            break
        if only_missing and repo.latest_sheet(session, row.ccn) is not None:
            continue
        try:
            results.append(
                build_hospital(
                    session, gateway, ai, row.ccn, today, dual=dual, reuse_sources=reuse_sources
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
    free-care limit was quoted from that directory's page about a hospital in Vidalia, Georgia."""
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
        if source.kind is SourceKind.HOSPITAL_WEB and host_of(source.url or "") == domain
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


@dataclass
class Recheck:
    ccn: str
    name: str
    state: str
    status: SheetStatus
    rejected: list[tuple[str, str]]


def recheck_sheets(session: Session, state: str | None = None) -> list[Recheck]:
    """Every latest sheet whose stored fields the current verification rules reject, with the
    paths and reasons. No model call and no credits: a what-if over stored quotes and texts, run
    after a verifier change to find the published sheets to rebuild before exporting (four
    national sheets and sixteen Massachusetts fields kept fields the 7.9 rules had been written
    for, because they were not rebuilt; review of 7.9)."""
    found: list[Recheck] = []
    for row in repo.list_hospitals(session, state=state):
        if repo.is_demo(row.ccn):
            continue
        latest = repo.latest_sheet(session, row.ccn)
        if latest is None:
            continue
        sheet = latest[0]
        texts = {source.id: text for source, text in repo.sources_for(session, row.ccn)}
        report = verify_sheet(sheet, texts)
        if report.rejected:
            found.append(Recheck(row.ccn, row.name, row.state, sheet.status, report.rejected))
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
    rules. A `recheck` review item records what went."""
    results: list[BuildResult] = []
    for row in repo.list_hospitals(session, state=state):
        if repo.is_demo(row.ccn):
            continue
        latest = repo.latest_sheet(session, row.ccn)
        if latest is None:
            continue
        sheet = latest[0]
        if published_only and sheet.status is not SheetStatus.PUBLISHED:
            continue
        texts = {source.id: text for source, text in repo.sources_for(session, row.ccn)}
        grounded, pruned = _ground(sheet, texts)
        report = verify_sheet(grounded, texts)
        if not pruned and not report.rejected:
            continue
        result = BuildResult(row.ccn, row.name, "skipped")
        result.notes.extend(pruned)
        result.notes.extend(f"{path}: {reason}" for path, reason in report.rejected)
        withdrawn = drop_fields(grounded, [path for path, _ in report.rejected])
        status = (
            decide_status(withdrawn, [], [])
            if sheet.status is SheetStatus.PUBLISHED
            else SheetStatus.HELD
        )
        repo.add_review_item(
            session, row.ccn, "recheck", {"pruned": pruned, "rejected": report.rejected}
        )
        published = publish_sheet(session, withdrawn.model_copy(update={"status": status}))
        result.outcome = "published" if status is SheetStatus.PUBLISHED else "held"
        result.version = published.version if published else sheet.version
        results.append(result)
        session.commit()
    return results


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
