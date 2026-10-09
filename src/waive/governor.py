"""Spend tracking and hard caps for paid APIs (spec section 15)."""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
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

    def events(self, provider: str, since: str | None = None) -> list[UsageEvent]:
        """Recorded calls for `provider`, oldest first. `since` is an ISO-8601 UTC timestamp in
        the format `_now()` writes; timestamps are compared as text, which is correct for that
        format."""
        found: list[UsageEvent] = []
        if not self._path.exists():
            return found
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row["provider"] != provider or (since is not None and row["ts"] < since):
                continue
            found.append(
                UsageEvent(
                    row["provider"],
                    Decimal(row["units"]),
                    Decimal(row["usd"]),
                    row["purpose"],
                    row["ts"],
                )
            )
        return found


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def day_start(today: date) -> str:
    """The timestamp `_now()` writes at midnight UTC on `today`; the lower bound of a day."""
    return f"{today.isoformat()}T00:00:00+00:00"


class Governor:
    def __init__(
        self,
        ledger: "Ledger | DbLedger",
        tavily_credit_cap: int,
        token_factory_usd_cap: Decimal,
        token_factory_daily_usd_cap: Decimal | None = None,
    ) -> None:
        self._ledger = ledger
        self._tavily_cap = Decimal(tavily_credit_cap)
        self._tf_cap = token_factory_usd_cap
        self._tf_daily_cap = token_factory_daily_usd_cap

    def ensure_tavily(self, credits: Decimal) -> None:
        used, _ = self._ledger.totals("tavily")
        if used + credits > self._tavily_cap:
            raise BudgetExceeded(
                f"Tavily cap of {self._tavily_cap} credits would be exceeded (used {used})."
            )

    def record_tavily(self, credits: Decimal, purpose: str) -> None:
        self._ledger.record(UsageEvent("tavily", credits, Decimal("0"), purpose, _now()))

    def ensure_token_factory(self, today: date | None = None) -> None:
        _, usd = self._ledger.totals("token_factory")
        if usd >= self._tf_cap:
            raise BudgetExceeded(f"Token Factory cap of ${self._tf_cap} reached (used ${usd}).")
        if self._tf_daily_cap is not None:
            used_today = self.token_factory_used_today(today or datetime.now(UTC).date())
            if used_today >= self._tf_daily_cap:
                raise BudgetExceeded(
                    f"Token Factory daily cap of ${self._tf_daily_cap} reached today "
                    f"(used ${used_today}); reading resumes at midnight UTC."
                )

    def token_factory_used_today(self, today: date) -> Decimal:
        """Dollars spent on Token Factory in the UTC day `today`."""
        events = self._ledger.events("token_factory", day_start(today))
        return sum((event.usd for event in events), Decimal("0"))

    def record_token_factory(
        self, prompt_tokens: int, completion_tokens: int, usd: Decimal, purpose: str
    ) -> None:
        tokens = Decimal(prompt_tokens + completion_tokens)
        self._ledger.record(UsageEvent("token_factory", tokens, usd, purpose, _now()))

    def summary(self) -> dict[str, tuple[Decimal, Decimal]]:
        return {provider: self._ledger.totals(provider) for provider in ("tavily", "token_factory")}

    def tavily_used_since(self, since: str) -> Decimal:
        return sum((event.units for event in self._ledger.events("tavily", since)), Decimal("0"))

    def tavily_used_today(self, today: date) -> Decimal:
        """Credits spent in the UTC day `today`; the scheduler's daily budget counts these."""
        return self.tavily_used_since(day_start(today))

    def tavily_by_day(self, since: date, today: date) -> list[tuple[str, Decimal]]:
        """Credits per UTC day from `since` to `today` inclusive, zero-filled, oldest first."""
        days = {
            (since + timedelta(days=n)).isoformat(): Decimal("0")
            for n in range((today - since).days + 1)
        }
        for event in self._ledger.events("tavily", day_start(since)):
            day = event.ts[:10]
            if day in days:
                days[day] += event.units
        return sorted(days.items())


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

    def events(self, provider: str, since: str | None = None) -> list[UsageEvent]:
        query = select(UsageEventRow).where(UsageEventRow.provider == provider)
        if since is not None:
            query = query.where(UsageEventRow.ts >= since)
        with session_scope(self._engine) as session:
            rows = session.scalars(query.order_by(UsageEventRow.id)).all()
        return [
            UsageEvent(row.provider, Decimal(row.units), Decimal(row.usd), row.purpose, row.ts)
            for row in rows
        ]


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
        make_ledger(settings, engine),
        settings.tavily_credit_cap,
        settings.token_factory_usd_cap,
        settings.token_factory_daily_usd_cap,
    )
