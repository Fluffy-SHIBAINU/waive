"""De-identified evidence from patient outcomes: enums, bands and one-way case hashes (spec §10, §11)."""

import re
from datetime import date
from typing import Literal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.schema import Cited, DocType, Layer, ProcedureSheet
from waive.db import CaseRow, ContributionRow, ReportedEvidenceRow, ReviewItemRow, SheetRow
from waive.learning.contributions import personal_info_hits
from waive.learning.hashing import case_hash

DOCUMENTS_PATH = "apply.documents_required"
SLIP_PATH = "accountability.slip"
# Every value in the evidence table must come from one of these fixed enums (spec §10).
ALLOWED_VALUES: dict[str, frozenset[str]] = {
    DOCUMENTS_PATH: frozenset(doc.value for doc in DocType),
    SLIP_PATH: frozenset({"denied_despite_policy", "partial_despite_free"}),
}


def add_evidence(
    session: Session, ccn: str, field_path: str, value: str, case_id: str, today: date
) -> ReportedEvidenceRow | None:
    """One row per (hospital, field, value, case); a repeat from the same case is ignored."""
    if value not in ALLOWED_VALUES.get(field_path, frozenset()):
        raise ValueError(f"{value!r} is not an allowed value for {field_path}")
    digest = case_hash(case_id)
    duplicate = session.scalars(
        select(ReportedEvidenceRow).where(
            ReportedEvidenceRow.ccn == ccn,
            ReportedEvidenceRow.field_path == field_path,
            ReportedEvidenceRow.value == value,
            ReportedEvidenceRow.case_hash == digest,
        )
    ).first()
    if duplicate is not None:
        return None
    row = ReportedEvidenceRow(
        ccn=ccn, field_path=field_path, value=value, case_hash=digest, created_on=today
    )
    session.add(row)
    session.flush()
    return row


def support(session: Session, ccn: str, field_path: str, value: str) -> int:
    """Distinct cases behind one reported value."""
    count = session.scalar(
        select(func.count(func.distinct(ReportedEvidenceRow.case_hash))).where(
            ReportedEvidenceRow.ccn == ccn,
            ReportedEvidenceRow.field_path == field_path,
            ReportedEvidenceRow.value == value,
        )
    )
    return int(count or 0)


def supported_values(session: Session, ccn: str, field_path: str) -> dict[str, int]:
    rows = session.execute(
        select(ReportedEvidenceRow.value, func.count(func.distinct(ReportedEvidenceRow.case_hash)))
        .where(ReportedEvidenceRow.ccn == ccn, ReportedEvidenceRow.field_path == field_path)
        .group_by(ReportedEvidenceRow.value)
    ).all()
    return {value: int(count) for value, count in rows}


PUBLISH_THRESHOLD = 5
FLAG_INTERNAL = 3
FLAG_PUBLIC = 5
FlagLevel = Literal["none", "internal", "public"]
# The only keys the clear `cases.outcome` summary may carry (spec §11).
OUTCOME_KEYS = frozenset({"decision", "recorded_on", "triage", "matched"})


def reported_documents(
    session: Session, ccn: str, sheet: ProcedureSheet, today: date
) -> Cited[list[DocType]] | None:
    """Documents at least PUBLISH_THRESHOLD distinct cases were asked for and the policy text
    does not list. Documented values are never duplicated into the reported layer."""
    documented = (
        {doc.value for doc in sheet.apply.documents_required.value}
        if sheet.apply.documents_required
        else set()
    )
    counts = supported_values(session, ccn, DOCUMENTS_PATH)
    chosen = sorted(
        value
        for value, count in counts.items()
        if count >= PUBLISH_THRESHOLD and value not in documented
    )
    if not chosen:
        return None
    weakest = min(counts[value] for value in chosen)
    return Cited[list[DocType]](
        value=[DocType(value) for value in chosen],
        layer=Layer.REPORTED,
        checked_on=today,
        confidence=min(1.0, weakest / 10),
        support_count=weakest,
    )


def publish_reported(session: Session, ccn: str, today: date) -> SheetRow | None:
    """A new sheet version when the reported documents changed; None otherwise."""
    found = repo.latest_sheet(session, ccn)
    if found is None:
        return None
    sheet, _ = found
    cited = reported_documents(session, ccn, sheet, today)
    if cited is None and sheet.apply.documents_reported is None:
        return None
    updated = sheet.model_copy(
        update={"apply": sheet.apply.model_copy(update={"documents_reported": cited})}
    )
    return publish_sheet(session, updated)


def slip_cases(session: Session, ccn: str) -> int:
    """Distinct cases that reported a denial or reduction contradicting the sheet."""
    count = session.scalar(
        select(func.count(func.distinct(ReportedEvidenceRow.case_hash))).where(
            ReportedEvidenceRow.ccn == ccn, ReportedEvidenceRow.field_path == SLIP_PATH
        )
    )
    return int(count or 0)


def slip_flag_level(session: Session, ccn: str) -> FlagLevel:
    cases = slip_cases(session, ccn)
    if cases >= FLAG_PUBLIC:
        return "public"
    if cases >= FLAG_INTERNAL:
        return "internal"
    return "none"


def raise_flags(session: Session, ccn: str) -> ReviewItemRow | None:
    """One open accountability flag per hospital, kept at the current level and count."""
    level = slip_flag_level(session, ccn)
    if level == "none":
        return None
    detail = {"level": level, "cases": slip_cases(session, ccn)}
    for item in repo.open_review_items(session, ccn):
        if item.kind == "accountability_flag":
            if item.detail != detail:
                item.detail = detail
                session.flush()
            return item
    return repo.add_review_item(session, ccn, "accountability_flag", detail)


def withdraw_slips(session: Session, ccn: str) -> int:
    """An admin found the sheet was wrong: the denials were not hospital slips. Removes the slip
    evidence for the hospital and closes its flag; returns the number of rows removed."""
    result = session.execute(
        delete(ReportedEvidenceRow).where(
            ReportedEvidenceRow.ccn == ccn, ReportedEvidenceRow.field_path == SLIP_PATH
        )
    )
    for item in repo.open_review_items(session, ccn):
        if item.kind == "accountability_flag":
            item.status = "withdrawn"
    session.flush()
    return int(result.rowcount or 0)


def audit_evidence(session: Session) -> list[str]:
    """Everything in the learning tables must be an enum value, a date, a count or a hash
    (spec §11, master plan Phase 5 exit check). Returns one line per problem; empty when clean."""
    problems: list[str] = []
    for row in session.scalars(select(ReportedEvidenceRow).order_by(ReportedEvidenceRow.id)):
        if row.value not in ALLOWED_VALUES.get(row.field_path, frozenset()):
            problems.append(
                f"evidence row {row.id}: {row.field_path}={row.value!r} is not an allowed enum value"
            )
        if not re.fullmatch(r"[0-9a-f]{16}", row.case_hash):
            problems.append(f"evidence row {row.id}: case_hash is not a 16-character hash")
    for row in session.scalars(select(CaseRow).where(CaseRow.outcome.is_not(None))):
        extra = sorted(set(row.outcome) - OUTCOME_KEYS)
        if extra:
            problems.append(f"a case outcome summary carries unexpected keys {extra}")
    for row in session.scalars(select(ContributionRow)):
        if row.status == "rejected" and row.text:
            problems.append(f"contribution {row.id}: rejected but its text was kept")
        elif row.text and personal_info_hits(row.text):
            problems.append(f"contribution {row.id}: text matches a personal-information pattern")
    return problems
