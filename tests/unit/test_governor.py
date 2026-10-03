import base64
import json
from datetime import date
from decimal import Decimal

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.config import Settings
from waive.db import UsageEventRow, init_db, make_engine, session_scope
from waive.governor import (
    BudgetExceeded,
    DbLedger,
    Governor,
    Ledger,
    UsageEvent,
    day_start,
    make_ledger,
)
from waive.web.app import create_app


def make(tmp_path, tavily_cap=10, tf_cap="1.00"):
    return Governor(Ledger(tmp_path / "usage.jsonl"), tavily_cap, Decimal(tf_cap))


def test_tavily_cap_blocks_before_spending(tmp_path):
    governor = make(tmp_path, tavily_cap=3)
    governor.ensure_tavily(Decimal("2"))
    governor.record_tavily(Decimal("2"), "test")
    with pytest.raises(BudgetExceeded):
        governor.ensure_tavily(Decimal("2"))


def test_ledger_persists_across_instances(tmp_path):
    make(tmp_path).record_tavily(Decimal("1"), "first")
    assert make(tmp_path).summary()["tavily"] == (Decimal("1"), Decimal("0"))


def test_token_factory_cap_blocks_once_reached(tmp_path):
    governor = make(tmp_path, tf_cap="0.50")
    governor.ensure_token_factory()
    governor.record_token_factory(1000, 500, Decimal("0.50"), "test")
    with pytest.raises(BudgetExceeded):
        governor.ensure_token_factory()


def test_ledger_rows_hold_no_content(tmp_path):
    make(tmp_path).record_token_factory(10, 5, Decimal("0.01"), "bill-extract")
    row = json.loads((tmp_path / "usage.jsonl").read_text().splitlines()[0])
    assert set(row) == {"provider", "units", "usd", "purpose", "ts"}
    assert row["units"] == "15"


def memory_engine():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return engine


def test_db_ledger_records_and_totals_like_the_file_ledger():
    engine = memory_engine()
    governor = Governor(DbLedger(engine), 10, Decimal("1.00"))
    governor.record_tavily(Decimal("2"), "atlas.scout")
    governor.record_token_factory(1000, 500, Decimal("0.0123"), "atlas.structure")
    assert governor.summary() == {
        "tavily": (Decimal("2"), Decimal("0")),
        "token_factory": (Decimal("1500"), Decimal("0.0123")),
    }
    # A second ledger over the same database sees the same history (that is the point).
    assert Governor(DbLedger(engine), 10, Decimal("1")).summary()["tavily"][0] == Decimal("2")
    columns = {column.name for column in UsageEventRow.__table__.columns}
    assert columns == {"id", "provider", "units", "usd", "purpose", "ts"}  # amounts only


def test_db_ledger_cap_check_sees_earlier_rows():
    governor = Governor(DbLedger(memory_engine()), 3, Decimal("1"))
    governor.record_tavily(Decimal("2"), "t")
    with pytest.raises(BudgetExceeded):
        governor.ensure_tavily(Decimal("2"))


def test_make_ledger_picks_the_backend_from_settings(tmp_path):
    file_settings = Settings(_env_file=None, ledger_path=tmp_path / "usage.jsonl")
    assert isinstance(make_ledger(file_settings), Ledger)
    db_settings = Settings(
        _env_file=None, ledger_backend="db", ledger_path=tmp_path / "usage.jsonl"
    )
    engine = make_engine("sqlite+pysqlite:///:memory:")  # no init_db: make_ledger must do it
    ledger = make_ledger(db_settings, engine=engine)
    assert isinstance(ledger, DbLedger)
    ledger.record(
        UsageEvent("tavily", Decimal("1"), Decimal("0"), "t", "2026-10-02T00:00:00+00:00")
    )
    assert ledger.totals("tavily") == (Decimal("1"), Decimal("0"))
    assert not (tmp_path / "usage.jsonl").exists()


def test_create_app_uses_the_database_ledger_when_configured():
    engine = memory_engine()
    settings = Settings(_env_file=None, nebius_api_key=SecretStr("k"), ledger_backend="db")
    app = create_app(
        settings,
        engine=engine,
        cipher=FieldCipher(base64.b64decode(new_key())),
        signer=TokenSigner("x" * 40),
    )
    app.state.governor.record_tavily(Decimal("1"), "t")
    assert app.state.governor.summary()["tavily"] == (Decimal("1"), Decimal("0"))
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(UsageEventRow)) == 1


def test_events_since_filters_by_timestamp(tmp_path):
    ledger = Ledger(tmp_path / "usage.jsonl")
    ledger.record(
        UsageEvent("tavily", Decimal("3"), Decimal("0"), "atlas.scout", "2026-10-01T23:59:59+00:00")
    )
    ledger.record(
        UsageEvent("tavily", Decimal("4"), Decimal("0"), "atlas.scout", "2026-10-02T08:00:00+00:00")
    )
    ledger.record(
        UsageEvent(
            "token_factory", Decimal("10"), Decimal("0.01"), "x", "2026-10-02T09:00:00+00:00"
        )
    )
    assert day_start(date(2026, 10, 2)) == "2026-10-02T00:00:00+00:00"
    assert [e.units for e in ledger.events("tavily")] == [Decimal("3"), Decimal("4")]
    since = day_start(date(2026, 10, 2))
    assert [e.units for e in ledger.events("tavily", since=since)] == [Decimal("4")]
    governor = Governor(ledger, 100, Decimal("1"))
    assert governor.tavily_used_today(date(2026, 10, 2)) == Decimal("4")
    assert governor.tavily_used_today(date(2026, 10, 3)) == Decimal("0")
    assert Ledger(tmp_path / "none.jsonl").events("tavily") == []


def test_db_ledger_events_since():
    ledger = DbLedger(memory_engine())
    ledger.record(
        UsageEvent("tavily", Decimal("3"), Decimal("0"), "atlas.scout", "2026-10-01T23:59:59+00:00")
    )
    ledger.record(
        UsageEvent(
            "tavily", Decimal("4"), Decimal("0"), "atlas.refresh", "2026-10-02T08:00:00+00:00"
        )
    )
    since = day_start(date(2026, 10, 2))
    events = ledger.events("tavily", since=since)
    assert [(e.units, e.purpose) for e in events] == [(Decimal("4"), "atlas.refresh")]
    assert Governor(ledger, 100, Decimal("1")).tavily_used_since(since) == Decimal("4")
