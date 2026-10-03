"""De-identified evidence from patient outcomes: enums, bands and one-way case hashes (spec §10, §11)."""

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from waive.atlas.schema import DocType
from waive.db import ReportedEvidenceRow
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
