"""Database models and session helpers (spec section 6)."""

from __future__ import annotations

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
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool


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


def make_engine(url: str) -> Engine:
    if url.endswith(":memory:"):
        return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(url)
    return create_engine(url, pool_pre_ping=True)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


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
