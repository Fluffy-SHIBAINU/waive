"""End-to-end atlas build for one hospital or one state (spec §8)."""

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from sqlalchemy.orm import Session

from waive.ai.client import AIClient, AIOutputError
from waive.atlas import repo
from waive.atlas.discover import MIN_CONFIDENCE, discover_domain
from waive.atlas.publish import critical_conflicts, decide_status, drop_fields, publish_sheet
from waive.atlas.scout import scout_hospital, store_scouted
from waive.atlas.structure import structure_sheet
from waive.atlas.tavily_gateway import TavilyGateway
from waive.atlas.verify import verify_sheet

Outcome = Literal["published", "held", "skipped", "failed"]


@dataclass
class BuildResult:
    ccn: str
    name: str
    outcome: Outcome
    version: int | None = None
    notes: list[str] = field(default_factory=list)


def build_hospital(
    session: Session,
    gateway: TavilyGateway,
    ai: AIClient,
    ccn: str,
    today: date,
    dual: bool = True,
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
    docs = scout_hospital(gateway, hospital)
    if not docs:
        repo.add_review_item(session, ccn, "no_documents", {"domain": row.website_domain})
        result.notes.append("no financial assistance documents found")
        return result
    sources = store_scouted(session, ccn, docs, today)
    sources_with_text = [(source, doc.text) for source, doc in zip(sources, docs, strict=True)]
    texts = {source.id: doc.text for source, doc in zip(sources, docs, strict=True)}

    sheet, skipped = structure_sheet(ai, "reason", hospital, sources_with_text, today)
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
                result.notes.append("critical fields disagree: " + ", ".join(conflicts))

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
) -> list[BuildResult]:
    results: list[BuildResult] = []
    for row in repo.list_hospitals(session, state=state):
        if limit is not None and len(results) >= limit:
            break
        if only_missing and repo.latest_sheet(session, row.ccn) is not None:
            continue
        try:
            results.append(build_hospital(session, gateway, ai, row.ccn, today, dual=dual))
        except Exception as error:  # keep the batch going; the failure is in the report
            results.append(BuildResult(row.ccn, row.name, "failed", notes=[type(error).__name__]))
        session.commit()
    return results


def coverage_report(session: Session, state: str) -> str:
    rows = repo.list_hospitals(session, state=state)
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
