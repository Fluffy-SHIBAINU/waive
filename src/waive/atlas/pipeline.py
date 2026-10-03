"""End-to-end atlas build for one hospital or one state (spec §8)."""

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from sqlalchemy.orm import Session

from waive.ai.client import AIClient, AIOutputError
from waive.atlas import repo
from waive.atlas.discover import MIN_CONFIDENCE, discover_domain
from waive.atlas.publish import (
    copy_fields,
    critical_conflicts,
    decide_status,
    drop_fields,
    publish_sheet,
    resolve_conflicts,
)
from waive.atlas.schema import HospitalRef, ProcedureSheet, SourceDoc, SourceKind
from waive.atlas.scout import scout_hospital, store_scouted
from waive.atlas.structure import structure_sheet
from waive.atlas.tavily_gateway import TavilyGateway
from waive.atlas.verify import trim_quotes, verify_sheet

Outcome = Literal["published", "held", "skipped", "failed"]


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
    if not sources_with_text:
        docs = scout_hospital(gateway, hospital)
        if not docs:
            repo.add_review_item(session, ccn, "no_documents", {"domain": row.website_domain})
            result.notes.append("no financial assistance documents found")
            return result
        sources = store_scouted(session, ccn, docs, today)
        sources_with_text = [(s, doc.text) for s, doc in zip(sources, docs, strict=True)]
    texts = {source.id: text for source, text in sources_with_text}

    sheet, skipped = structure_sheet(ai, "reason", hospital, sources_with_text, today)
    sheet = trim_quotes(sheet, texts)
    report = verify_sheet(sheet, texts)
    if skipped or report.rejected:
        repo.add_review_item(
            session, ccn, "verification", {"skipped": skipped, "rejected": report.rejected}
        )
        result.notes.extend(skipped)
        result.notes.extend(f"{path}: {reason}" for path, reason in report.rejected)
    sheet = drop_fields(sheet, [path for path, _ in report.rejected])

    conflicts: list[str] = []
    if dual:
        try:
            secondary, _ = structure_sheet(ai, "fast", hospital, sources_with_text, today)
        except AIOutputError as error:
            # The cross-check is best effort: record the failure, keep the verified primary.
            repo.add_review_item(session, ccn, "crosscheck_failed", {"error": str(error)[:300]})
            result.notes.append("cross-check model gave no usable output")
        else:
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
    status = decide_status(sheet, conflicts)
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
