"""Database access for the atlas: hospitals, sources, sheet versions, review items."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from waive.atlas.schema import HospitalRef, ProcedureSheet, SourceDoc
from waive.db import HospitalRow, ReviewItemRow, SheetRow, SourceDocRow

HOSPITAL_FIELDS = (
    "name",
    "address",
    "city",
    "state",
    "zip",
    "phone",
    "hospital_type",
    "ownership",
    "website_domain",
    "domain_confidence",
    "system",
)

# Fictional hospitals seeded by `waive demo seed` (St. Example Medical Center). They stay in the
# database so the phone demo works, but public exports, reports and the atlas list skip them.
DEMO_CCNS: frozenset[str] = frozenset({"229999"})


def is_demo(ccn: str) -> bool:
    return ccn in DEMO_CCNS


def upsert_hospital(session: Session, data: dict[str, Any]) -> HospitalRow:
    row = session.get(HospitalRow, data["ccn"]) or HospitalRow(ccn=data["ccn"])
    for field in HOSPITAL_FIELDS:
        if field in data:
            setattr(row, field, data[field])
    session.add(row)
    session.flush()
    return row


def get_hospital(session: Session, ccn: str) -> HospitalRow | None:
    return session.get(HospitalRow, ccn)


def list_hospitals(
    session: Session, state: str | None = None, missing_domain: bool = False
) -> list[HospitalRow]:
    query = select(HospitalRow).order_by(HospitalRow.name)
    if state:
        query = query.where(HospitalRow.state == state.upper())
    if missing_domain:
        query = query.where(HospitalRow.website_domain.is_(None))
    return list(session.scalars(query))


def hospital_ref(row: HospitalRow) -> HospitalRef:
    return HospitalRef(
        ccn=row.ccn,
        name=row.name,
        city=row.city,
        state=row.state,
        zip=row.zip,
        phone=row.phone,
        ownership=row.ownership,
        website_domain=row.website_domain,
        system=row.system,
    )


def save_source(session: Session, doc: SourceDoc, text: str, ccn: str) -> SourceDocRow:
    row = session.get(SourceDocRow, doc.id)
    if row is None:
        row = SourceDocRow(
            id=doc.id,
            kind=doc.kind.value,
            url=doc.url,
            title=doc.title,
            fetched_on=doc.fetched_on,
            sha256=doc.sha256,
            effective_date=doc.effective_date,
            text=text,
        )
        session.add(row)
    hospital = session.get(HospitalRow, ccn)
    if hospital is not None and hospital not in row.hospitals:
        row.hospitals.append(hospital)
    session.flush()
    return row


def _to_source_doc(row: SourceDocRow) -> SourceDoc:
    return SourceDoc(
        id=row.id,
        kind=row.kind,
        url=row.url,
        title=row.title,
        fetched_on=row.fetched_on,
        sha256=row.sha256,
        effective_date=row.effective_date,
    )


def sources_for(session: Session, ccn: str) -> list[tuple[SourceDoc, str]]:
    hospital = session.get(HospitalRow, ccn)
    if hospital is None:
        return []
    return [(_to_source_doc(row), row.text) for row in hospital.sources]


def latest_sheet(session: Session, ccn: str) -> tuple[ProcedureSheet, SheetRow] | None:
    row = session.scalars(
        select(SheetRow).where(SheetRow.ccn == ccn).order_by(SheetRow.version.desc()).limit(1)
    ).first()
    if row is None:
        return None
    return ProcedureSheet.model_validate(row.body), row


def add_sheet_version(
    session: Session, sheet: ProcedureSheet, diff: dict[str, Any] | None
) -> SheetRow:
    row = SheetRow(
        ccn=sheet.hospital.ccn,
        version=sheet.version,
        status=sheet.status.value,
        body=sheet.model_dump(mode="json"),
        diff=diff,
    )
    session.add(row)
    session.flush()
    return row


def list_latest_sheets(session: Session, state: str) -> list[ProcedureSheet]:
    sheets = []
    for hospital in list_hospitals(session, state=state):
        found = latest_sheet(session, hospital.ccn)
        if found is not None:
            sheets.append(found[0])
    return sheets


def add_review_item(
    session: Session, ccn: str | None, kind: str, detail: dict[str, Any]
) -> ReviewItemRow:
    row = ReviewItemRow(ccn=ccn, kind=kind, detail=detail)
    session.add(row)
    session.flush()
    return row


def open_review_items(session: Session, ccn: str | None = None) -> list[ReviewItemRow]:
    query = select(ReviewItemRow).where(ReviewItemRow.status == "open")
    if ccn:
        query = query.where(ReviewItemRow.ccn == ccn)
    return list(session.scalars(query.order_by(ReviewItemRow.id)))


def set_review_status(session: Session, item_id: int, status: str) -> ReviewItemRow:
    row = session.get(ReviewItemRow, item_id)
    if row is None:
        raise KeyError(item_id)
    row.status = status
    session.flush()
    return row


def sheet_versions(session: Session, ccn: str) -> list[SheetRow]:
    """Every stored version of a hospital's sheet, newest first, each with its stored diff."""
    query = select(SheetRow).where(SheetRow.ccn == ccn).order_by(SheetRow.version.desc())
    return list(session.scalars(query))


def ccns_with_sheets(session: Session) -> set[str]:
    """Hospitals that have at least one stored sheet version (any status)."""
    return set(session.scalars(select(SheetRow.ccn).distinct()))
