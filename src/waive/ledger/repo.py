"""Database access for the ledger: upserts keyed by CCN (and year), newest-year reads, and the
joined listing the public pages sort."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from waive.db import HospitalRow
from waive.ledger.tables import (
    CostReportRow,
    ExposureRow,
    HospitalIdRow,
    LedgerMetricsRow,
    ScheduleHRow,
)


def _upsert(session: Session, model: type, key: dict[str, Any], fields: dict[str, Any]) -> Any:
    """The row with primary key `key` (column -> value), created when missing, with `fields` set."""
    row = session.get(model, tuple(key.values()) if len(key) > 1 else next(iter(key.values())))
    if row is None:
        row = model(**key)
        session.add(row)
    for name, value in fields.items():
        setattr(row, name, value)
    session.flush()
    return row


def upsert_hospital_id(session: Session, ccn: str, **fields: Any) -> HospitalIdRow:
    return _upsert(session, HospitalIdRow, {"ccn": ccn}, fields)


def get_hospital_id(session: Session, ccn: str) -> HospitalIdRow | None:
    return session.get(HospitalIdRow, ccn)


def list_hospital_ids(session: Session, state: str | None = None) -> list[HospitalIdRow]:
    query = select(HospitalIdRow).join(HospitalRow, HospitalRow.ccn == HospitalIdRow.ccn)
    if state:
        query = query.where(HospitalRow.state == state.upper())
    return list(session.scalars(query.order_by(HospitalIdRow.ccn)))


def matched_eins(session: Session, state: str | None = None) -> dict[str, str]:
    """ccn -> ein for the hospitals that have one."""
    return {row.ccn: row.ein for row in list_hospital_ids(session, state) if row.ein}


def upsert_cost_report(
    session: Session, ccn: str, fiscal_year: int, **fields: Any
) -> CostReportRow:
    return _upsert(session, CostReportRow, {"ccn": ccn, "fiscal_year": fiscal_year}, fields)


def latest_cost_report(session: Session, ccn: str) -> CostReportRow | None:
    query = (
        select(CostReportRow)
        .where(CostReportRow.ccn == ccn)
        .order_by(CostReportRow.fiscal_year.desc())
        .limit(1)
    )
    return session.scalars(query).first()


def all_latest_cost_reports(session: Session) -> dict[str, CostReportRow]:
    """Every hospital's newest cost report, keyed by CCN (ranks are computed over these)."""
    newest: dict[str, CostReportRow] = {}
    for row in session.scalars(select(CostReportRow).order_by(CostReportRow.fiscal_year)):
        newest[row.ccn] = row
    return newest


def upsert_schedule_h(session: Session, ccn: str, tax_year: int, **fields: Any) -> ScheduleHRow:
    return _upsert(session, ScheduleHRow, {"ccn": ccn, "tax_year": tax_year}, fields)


def latest_schedule_h(session: Session, ccn: str) -> ScheduleHRow | None:
    query = (
        select(ScheduleHRow)
        .where(ScheduleHRow.ccn == ccn)
        .order_by(ScheduleHRow.tax_year.desc())
        .limit(1)
    )
    return session.scalars(query).first()


def all_latest_schedule_h(session: Session) -> dict[str, ScheduleHRow]:
    newest: dict[str, ScheduleHRow] = {}
    for row in session.scalars(select(ScheduleHRow).order_by(ScheduleHRow.tax_year)):
        newest[row.ccn] = row
    return newest


def upsert_exposure(session: Session, ccn: str, **fields: Any) -> ExposureRow:
    return _upsert(session, ExposureRow, {"ccn": ccn}, fields)


def get_exposure(session: Session, ccn: str) -> ExposureRow | None:
    return session.get(ExposureRow, ccn)


def all_exposure(session: Session) -> dict[str, ExposureRow]:
    return {row.ccn: row for row in session.scalars(select(ExposureRow))}


def upsert_metrics(session: Session, ccn: str, **fields: Any) -> LedgerMetricsRow:
    return _upsert(session, LedgerMetricsRow, {"ccn": ccn}, fields)


def get_metrics(session: Session, ccn: str) -> LedgerMetricsRow | None:
    return session.get(LedgerMetricsRow, ccn)


def list_metrics(
    session: Session, state: str | None = None
) -> list[tuple[HospitalRow, LedgerMetricsRow]]:
    """Every hospital with a metrics row, with the hospital, by CCN; one state when asked."""
    query = select(HospitalRow, LedgerMetricsRow).join(
        LedgerMetricsRow, LedgerMetricsRow.ccn == HospitalRow.ccn
    )
    if state:
        query = query.where(HospitalRow.state == state.upper())
    return [
        (hospital, metrics)
        for hospital, metrics in session.execute(query.order_by(HospitalRow.ccn))
    ]


def exposure_ranks(session: Session) -> dict[str, float]:
    """ccn -> exposure rank (0 to 1) for the hospitals that have one: the scout queue's factor."""
    query = select(LedgerMetricsRow.ccn, LedgerMetricsRow.exposure_rank).where(
        LedgerMetricsRow.exposure_rank.is_not(None)
    )
    return {ccn: float(rank) for ccn, rank in session.execute(query)}
