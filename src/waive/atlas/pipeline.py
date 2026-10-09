"""End-to-end atlas build for one hospital or one state (spec §8)."""

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from sqlalchemy.orm import Session

from waive.ai.client import MAX_SERVER_MESSAGE_CHARS, AIClient, AIOutputError
from waive.atlas import repo
from waive.atlas.discover import MIN_CONFIDENCE, discover_domain, host_of
from waive.atlas.publish import (
    carry_over_reported,
    copy_fields,
    critical_conflicts,
    decide_status,
    drop_fields,
    publish_sheet,
    resolve_conflicts,
    sheet_inconsistencies,
)
from waive.atlas.schema import HospitalRef, ProcedureSheet, SheetStatus, SourceDoc, SourceKind
from waive.atlas.scout import navigation_shells, scout_hospital, store_scouted
from waive.atlas.structure import structure_sheet
from waive.atlas.tavily_gateway import TavilyGateway
from waive.atlas.verify import PATIENT_SHARE_REASON, trim_quotes, verify_sheet

Outcome = Literal["published", "held", "skipped", "failed"]
# A structurer error is the model name, the status and the server's message (at most
# MAX_SERVER_MESSAGE_CHARS); the review detail and the note keep the whole of it.
ERROR_DETAIL_CHARS = MAX_SERVER_MESSAGE_CHARS + 100


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
    merged = trim_quotes(merged, texts)
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
    if not sources_with_text:
        docs = scout_hospital(gateway, hospital)
        if not docs:
            repo.add_review_item(session, ccn, "no_documents", {"domain": row.website_domain})
            result.notes.append("no financial assistance documents found")
            return result
        sources = store_scouted(session, ccn, docs, today)
        sources_with_text = [(s, doc.text) for s, doc in zip(sources, docs, strict=True)]
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
        return result
    sheet = trim_quotes(sheet, texts)
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
    sheet = drop_fields(sheet, [path for path, _ in report.rejected])

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
            secondary = trim_quotes(secondary, texts)
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
