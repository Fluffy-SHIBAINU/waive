"""`create_all` never alters an existing table; `upgrade_schema` adds the columns it misses."""

import logging

from sqlalchemy import inspect, text

from waive.db import CaseRow, init_db, make_engine, session_scope, upgrade_schema

# `cases` as an older model defined it: no `status` (NOT NULL, Python default "new") and no
# `outcome` (nullable JSON, added in Phase 5).
OLD_CASES = """
CREATE TABLE cases (
    id VARCHAR(32) NOT NULL PRIMARY KEY,
    state VARCHAR(2) NOT NULL,
    ccn VARCHAR(12),
    token_generation INTEGER NOT NULL,
    sealed TEXT,
    prediction JSON,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""
OLD_CASE_ROW = (
    "INSERT INTO cases (id, state, ccn, token_generation, created_at, updated_at) "
    "VALUES ('abc', 'MA', '229999', 2, '2026-10-02 00:00:00', '2026-10-02 00:00:00')"
)
# `review_items` without `kind` (NOT NULL, no default: cannot be filled for existing rows) and
# without `status` (NOT NULL, Python default "open").
OLD_REVIEW_ITEMS = """
CREATE TABLE review_items (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    ccn VARCHAR(12),
    detail JSON NOT NULL,
    created_at DATETIME NOT NULL
)
"""


def old_engine(*statements):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
    return engine


def test_fresh_database_needs_no_upgrade():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    assert init_db(engine) == []
    assert upgrade_schema(engine) == []


def test_upgrade_adds_missing_columns_and_keeps_existing_rows():
    engine = old_engine(OLD_CASES, OLD_CASE_ROW)
    assert init_db(engine) == ["cases.status", "cases.outcome"]
    columns = {column["name"] for column in inspect(engine).get_columns("cases")}
    assert {"status", "outcome"} <= columns
    with session_scope(engine) as session:
        row = session.get(CaseRow, "abc")
        assert (row.state, row.ccn, row.token_generation) == ("MA", "229999", 2)
        assert (row.status, row.outcome) == ("new", None)  # the model default fills old rows
        row.outcome = {"decision": "denied"}
    with session_scope(engine) as session:
        assert session.get(CaseRow, "abc").outcome == {"decision": "denied"}
    assert upgrade_schema(engine) == []  # a second run changes nothing


def test_required_columns_without_a_default_are_skipped_and_reported(caplog):
    engine = old_engine(OLD_REVIEW_ITEMS)
    with caplog.at_level(logging.WARNING, logger="waive.db"):
        assert init_db(engine) == ["review_items.status"]
    assert "review_items.kind" in caplog.text
    columns = {column["name"] for column in inspect(engine).get_columns("review_items")}
    assert "status" in columns and "kind" not in columns
