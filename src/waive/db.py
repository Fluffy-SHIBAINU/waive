"""Database models and session helpers (spec section 6)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    exc,
    inspect,
    literal,
    text,
)
from sqlalchemy.engine import Dialect, Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateColumn

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


hospital_sources = Table(
    "hospital_sources",
    Base.metadata,
    Column("ccn", ForeignKey("hospitals.ccn"), primary_key=True),
    Column("source_id", ForeignKey("source_docs.id"), primary_key=True),
)


class HospitalRow(Base):
    __tablename__ = "hospitals"

    ccn: Mapped[str] = mapped_column(String(12), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str] = mapped_column(String(200), default="")
    city: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(2), index=True)
    zip: Mapped[str] = mapped_column(String(10), default="")
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    hospital_type: Mapped[str] = mapped_column(String(100))
    ownership: Mapped[str] = mapped_column(String(100))
    website_domain: Mapped[str | None] = mapped_column(String(200), nullable=True)
    domain_confidence: Mapped[float | None] = mapped_column(nullable=True)
    system: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    sources: Mapped[list[SourceDocRow]] = relationship(
        secondary=hospital_sources, back_populates="hospitals"
    )


class SourceDocRow(Base):
    __tablename__ = "source_docs"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    fetched_on: Mapped[date] = mapped_column(Date)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    hospitals: Mapped[list[HospitalRow]] = relationship(
        secondary=hospital_sources, back_populates="sources"
    )


class SheetRow(Base):
    __tablename__ = "sheets"
    __table_args__ = (UniqueConstraint("ccn", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ccn: Mapped[str] = mapped_column(ForeignKey("hospitals.ccn"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    body: Mapped[dict[str, Any]] = mapped_column(JSON)
    diff: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewItemRow(Base):
    __tablename__ = "review_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ccn: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(40))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CaseRow(Base):
    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    state: Mapped[str] = mapped_column(String(2))
    ccn: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="new")
    token_generation: Mapped[int] = mapped_column(Integer, default=1)
    sealed: Mapped[str | None] = mapped_column(Text, nullable=True)
    prediction: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # De-identified summary for the scoreboard: decision enum, triage class, matched flag, date.
    outcome: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AccountRow(Base):
    """Someone who signed in (Phase 9). Known only by the provider's name and a keyed one-way
    hash of the provider's subject id (auth/accounts.py): no email, name or picture is asked
    for, and there is no column to keep one in."""

    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("provider", "subject_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(20))
    subject_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SessionRow(Base):
    """A signed-in browser. `id` is the SHA-256 of the random id the cookie holds
    (auth/sessions.py), so a copy of this table is not a set of live sessions."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AccountCaseRow(Base):
    """A case its account keeps in its history. The case's links are not stored: opening a case
    from the history mints a fresh one. Deleting the case deletes this row (cases/service.py)."""

    __tablename__ = "account_cases"

    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    case_id: Mapped[str] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ContributionRow(Base):
    """A patient's photo of a public document, waiting for or past admin review. Holds the
    transcribed text only while it passed the personal-information check; never a case id."""

    __tablename__ = "contributions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ccn: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    case_hash: Mapped[str] = mapped_column(String(16))
    photo_class: Mapped[str] = mapped_column(String(40))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    reject_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_on: Mapped[date] = mapped_column(Date)


class ReportedEvidenceRow(Base):
    """One de-identified report: a hospital, a sheet field, an enum value and a one-way case hash.
    Free text, amounts and identifiers never belong here (spec §10, §11)."""

    __tablename__ = "reported_evidence"
    __table_args__ = (UniqueConstraint("ccn", "field_path", "value", "case_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ccn: Mapped[str] = mapped_column(String(12), index=True)
    field_path: Mapped[str] = mapped_column(String(60))
    value: Mapped[str] = mapped_column(String(60))
    case_hash: Mapped[str] = mapped_column(String(16))
    created_on: Mapped[date] = mapped_column(Date)


class UsageEventRow(Base):
    """One paid call for the budget governor: provider, amounts and purpose. Never request or
    response content (spec §11). Amounts are exact decimal strings, as in var/usage.jsonl."""

    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(20), index=True)
    units: Mapped[str] = mapped_column(String(40))
    usd: Mapped[str] = mapped_column(String(40))
    purpose: Mapped[str] = mapped_column(String(80))
    ts: Mapped[str] = mapped_column(String(40))


# SQLite admits one writer at a time, and a scouting batch holds its write lock for a whole
# hospital (tens of seconds). A second process, the web app during a batch, waits this long for
# its turn instead of failing after SQLite's default five seconds; write-ahead logging lets its
# reads through meanwhile. Production uses PostgreSQL and none of this applies.
SQLITE_BUSY_SECONDS = 60


def _use_write_ahead_log(dbapi_connection: Any, _record: Any) -> None:
    """Best effort: the switch needs the file to itself for a moment, and while another process
    is mid-write SQLite refuses it at once (it does not wait). The connection then works in the
    file's current mode, still with the busy wait; a later connection makes the switch."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        log.debug("write-ahead logging not enabled yet: the database is busy")
    finally:
        cursor.close()


def make_engine(url: str) -> Engine:
    if url.endswith(":memory:"):
        return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"timeout": SQLITE_BUSY_SECONDS})
        event.listen(engine, "connect", _use_write_ahead_log)
        return engine
    return create_engine(url, pool_pre_ping=True)


def _add_column_ddl(table: Table, column: Column[Any], dialect: Dialect) -> str | None:
    """`ALTER TABLE ... ADD COLUMN ...` for `column` in the engine's own SQL, or None when existing
    rows could not satisfy it: NOT NULL with neither a server default nor a constant model default.
    A constant model default (`status="new"`) is written into the DDL so old rows get it too."""
    spec = str(CreateColumn(column).compile(dialect=dialect))
    if not column.nullable and column.server_default is None:
        default = column.default
        if default is None or not default.is_scalar:
            return None
        try:
            rendered = literal(default.arg, type_=column.type).compile(
                dialect=dialect, compile_kwargs={"literal_binds": True}
            )
        except exc.CompileError:
            return None
        spec += f" DEFAULT {rendered}"
    return f"ALTER TABLE {dialect.identifier_preparer.format_table(table)} ADD COLUMN {spec}"


def upgrade_schema(engine: Engine) -> list[str]:
    """Add the columns the models define but existing tables lack (`create_all` only creates
    missing tables, never alters one). Returns the added columns as `table.column`; a required
    column that cannot be filled for existing rows is left alone and reported in the log."""
    added: list[str] = []
    with engine.begin() as connection:
        inspector = inspect(connection)
        existing = set(inspector.get_table_names())
        for table in Base.metadata.sorted_tables:
            if table.name not in existing:
                continue
            present = {info["name"] for info in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                ddl = _add_column_ddl(table, column, engine.dialect)
                if ddl is None:
                    log.warning(
                        "cannot add %s.%s: NOT NULL without a default; add it by hand",
                        table.name,
                        column.name,
                    )
                    continue
                connection.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")
    return added


def init_db(engine: Engine) -> list[str]:
    """Create missing tables, then add missing columns to the tables that already existed.
    Returns the columns `upgrade_schema` added."""
    Base.metadata.create_all(engine)
    return upgrade_schema(engine)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Phase 10: the hospital ledger's tables live in waive/ledger/tables.py. Importing them here
# registers them on `Base`, so `init_db` creates them wherever the app or the CLI starts. The
# import is circular on purpose and safe: `Base` is defined above, before this line runs.
from waive.ledger import tables as _ledger_tables  # noqa: E402, F401
