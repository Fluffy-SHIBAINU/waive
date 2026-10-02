import json
from decimal import Decimal

import pytest

from waive.governor import BudgetExceeded, Governor, Ledger


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
