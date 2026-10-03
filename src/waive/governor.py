"""Spend tracking and hard caps for paid APIs (spec section 15)."""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine

from waive.config import Settings
from waive.db import UsageEventRow, init_db, make_engine, session_scope


class BudgetExceeded(RuntimeError):
    """Raised before a paid call that would break a configured cap."""


@dataclass(frozen=True)
class UsageEvent:
    provider: str
    units: Decimal
    usd: Decimal
    purpose: str
    ts: str


class Ledger:
    """Append-only JSON-lines record of paid calls. Stores amounts, never content."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def record(self, event: UsageEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        row = {key: str(value) for key, value in asdict(event).items()}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    def totals(self, provider: str) -> tuple[Decimal, Decimal]:
        units = usd = Decimal("0")
        if not self._path.exists():
            return units, usd
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row["provider"] == provider:
                units += Decimal(row["units"])
                usd += Decimal(row["usd"])
        return units, usd


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Governor:
    def __init__(
        self, ledger: "Ledger | DbLedger", tavily_credit_cap: int, token_factory_usd_cap: Decimal
    ) -> None:
        self._ledger = ledger
        self._tavily_cap = Decimal(tavily_credit_cap)
        self._tf_cap = token_factory_usd_cap

    def ensure_tavily(self, credits: Decimal) -> None:
        used, _ = self._ledger.totals("tavily")
        if used + credits > self._tavily_cap:
            raise BudgetExceeded(
                f"Tavily cap of {self._tavily_cap} credits would be exceeded (used {used})."
            )

    def record_tavily(self, credits: Decimal, purpose: str) -> None:
        self._ledger.record(UsageEvent("tavily", credits, Decimal("0"), purpose, _now()))

    def ensure_token_factory(self) -> None:
        _, usd = self._ledger.totals("token_factory")
        if usd >= self._tf_cap:
            raise BudgetExceeded(f"Token Factory cap of ${self._tf_cap} reached (used ${usd}).")

    def record_token_factory(
        self, prompt_tokens: int, completion_tokens: int, usd: Decimal, purpose: str
    ) -> None:
        tokens = Decimal(prompt_tokens + completion_tokens)
        self._ledger.record(UsageEvent("token_factory", tokens, usd, purpose, _now()))

    def summary(self) -> dict[str, tuple[Decimal, Decimal]]:
        return {provider: self._ledger.totals(provider) for provider in ("tavily", "token_factory")}


class DbLedger:
    """The `Ledger` interface stored in the `usage_events` table, so a stateless container keeps
    its spend history in the database it already has."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, event: UsageEvent) -> None:
        row = {key: str(value) for key, value in asdict(event).items()}
        with session_scope(self._engine) as session:
            session.add(UsageEventRow(**row))

    def totals(self, provider: str) -> tuple[Decimal, Decimal]:
        units = usd = Decimal("0")
        query = select(UsageEventRow.units, UsageEventRow.usd).where(
            UsageEventRow.provider == provider
        )
        with session_scope(self._engine) as session:
            rows = session.execute(query).all()
        for row_units, row_usd in rows:
            units += Decimal(row_units)
            usd += Decimal(row_usd)
        return units, usd


def make_ledger(settings: Settings, engine: Engine | None = None) -> Ledger | DbLedger:
    """`WAIVE_LEDGER_BACKEND=db` → the `usage_events` table (created if missing); otherwise the
    JSON-lines file at `WAIVE_LEDGER_PATH`."""
    if settings.ledger_backend == "db":
        engine = engine or make_engine(settings.database_url)
        init_db(engine)
        return DbLedger(engine)
    return Ledger(settings.ledger_path)


def make_governor(settings: Settings, engine: Engine | None = None) -> Governor:
    return Governor(
        make_ledger(settings, engine), settings.tavily_credit_cap, settings.token_factory_usd_cap
    )
