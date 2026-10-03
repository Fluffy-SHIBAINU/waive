"""Public documents photographed by patients: checked, held for review, then sources (spec §10, §11)."""

import hashlib
import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from waive.ai.client import AIClient
from waive.atlas import repo
from waive.atlas.pipeline import BuildResult, build_hospital
from waive.atlas.schema import SourceDoc, SourceKind
from waive.db import ContributionRow
from waive.learning.classify import PUBLIC_CLASSES, PhotoClass
from waive.learning.hashing import case_hash

MIN_TEXT_CHARS = 200

# Patterns that mark a transcription as personal. A hospital's own mailing address and phone
# numbers are public and must pass, so there is no street-address pattern here: addresses of
# people are caught by the vision model's personal_info flag (prompt rule 2 in classify.py).
PERSONAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "account_number": re.compile(
        r"\b(?:acct|account|mrn|guarantor)\b[^\n]{0,20}?\d{5,}", re.IGNORECASE
    ),
    "long_number": re.compile(r"\b\d{9,}\b"),
    "date_of_birth": re.compile(r"\b(?:dob|date of birth|birth ?date)\b", re.IGNORECASE),
    "patient_name": re.compile(
        r"\b(?:patient(?: name)?|name|guarantor)\s*:\s*[A-Z][a-z]+|\bdear\s+(?:mr|mrs|ms|dr)\b",
        re.IGNORECASE,
    ),
}


def personal_info_hits(text: str) -> list[str]:
    return [name for name, pattern in PERSONAL_PATTERNS.items() if pattern.search(text)]


def _existing(session: Session, ccn: str | None, sha256: str) -> ContributionRow | None:
    query = select(ContributionRow).where(
        ContributionRow.sha256 == sha256,
        ContributionRow.ccn == ccn,
        ContributionRow.status.in_(["open", "approved"]),
    )
    return session.scalars(query).first()


def submit_contribution(
    session: Session,
    *,
    ccn: str | None,
    case_id: str,
    photo_class: PhotoClass,
    text: str | None,
    vision_flag: bool,
    today: date,
) -> ContributionRow:
    """Hold a public-document photo for admin review, or reject it on the spot when it may carry
    personal information or is too short to be a document. Rejected text is never stored."""
    if photo_class not in PUBLIC_CLASSES:
        raise ValueError(f"{photo_class.value} is not a public document class")
    text = (text or "").strip()
    reasons = personal_info_hits(text)
    if vision_flag:
        reasons.insert(0, "vision")
    if len(text) < MIN_TEXT_CHARS:
        reasons.append("too_short")
    sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if not reasons and (existing := _existing(session, ccn, sha256)) is not None:
        return existing
    row = ContributionRow(
        ccn=ccn,
        case_hash=case_hash(case_id),
        photo_class=photo_class.value,
        sha256=sha256,
        text="" if reasons else text,
        reject_reasons=reasons,
        status="rejected" if reasons else "open",
        created_on=today,
    )
    session.add(row)
    session.flush()
    return row


def list_contributions(session: Session, status: str = "open") -> list[ContributionRow]:
    query = select(ContributionRow).where(ContributionRow.status == status)
    return list(session.scalars(query.order_by(ContributionRow.id)))


def source_for(row: ContributionRow, today: date) -> SourceDoc:
    return SourceDoc(
        id=f"photo-{row.sha256[:16]}",
        kind=SourceKind.PATIENT_PHOTO,
        url=None,
        title=f"Patient photo: {row.photo_class.replace('_', ' ')}",
        fetched_on=today,
        sha256=row.sha256,
    )


def approve_contribution(session: Session, contribution_id: int, today: date) -> SourceDoc:
    row = session.get(ContributionRow, contribution_id)
    if row is None or row.status != "open":
        raise KeyError(contribution_id)
    if row.ccn is None:
        raise ValueError("a contribution needs a hospital before it can become a source")
    source = source_for(row, today)
    repo.save_source(session, source, row.text, row.ccn)
    row.status = "approved"
    session.flush()
    return source


def reject_contribution(session: Session, contribution_id: int, reason: str = "admin") -> None:
    row = session.get(ContributionRow, contribution_id)
    if row is None:
        raise KeyError(contribution_id)
    row.status = "rejected"
    row.reject_reasons = [*row.reject_reasons, reason]
    row.text = ""
    session.flush()


class NoTavily:
    """Rebuilds from stored sources never scout; a Tavily call here is a bug, not a cost."""

    def search(self, *args, **kwargs):
        raise RuntimeError("Tavily is not used when rebuilding from stored sources")

    def extract(self, *args, **kwargs):
        raise RuntimeError("Tavily is not used when rebuilding from stored sources")

    def map(self, *args, **kwargs):
        raise RuntimeError("Tavily is not used when rebuilding from stored sources")


def rebuild_from_sources(session: Session, ai: AIClient, ccn: str, today: date) -> BuildResult:
    """Re-structure a hospital's sheet from its stored documents, approved patient photos
    included. Spends Token Factory tokens (about $0.01 per hospital), never Tavily credits."""
    row = repo.get_hospital(session, ccn)
    if row is None or not row.website_domain:
        name = row.name if row is not None else "?"
        return BuildResult(ccn, name, "failed", notes=["hospital or its domain is missing"])
    hospital_docs = [
        source
        for source, _ in repo.sources_for(session, ccn)
        if source.kind is not SourceKind.STATE_REPOSITORY
    ]
    if not hospital_docs:
        return BuildResult(ccn, row.name, "skipped", notes=["no stored documents to rebuild from"])
    return build_hospital(session, NoTavily(), ai, ccn, today, reuse_sources=True)
