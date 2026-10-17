"""Copy the local database into another one, in practice the production PostgreSQL (spec §14):
the atlas, its sources, the de-identified learning tables and the spend history the budget
governor counts against (spec §15). Personal data never leaves this way (spec §11)."""

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, fields
from datetime import UTC
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import (
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    delete,
    func,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import ArgumentError

from waive.db import Base, UsageEventRow, init_db
from waive.governor import UsageEvent

# Copied with their own primary keys. `contributions` and `reported_evidence` qualify: neither has
# a foreign key to `cases`; they count cases through a one-way 16-character hash and otherwise
# hold enum values and dates, plus, for a contribution, the text of a public document, kept only
# once it passed the personal-information check (rejected text is never stored). They are the
# de-identified rows one-tap delete leaves on purpose (spec §10, §11); `waive learn audit`
# checks them.
COPIED_TABLES = frozenset(
    {
        "hospitals",
        "source_docs",
        "hospital_sources",
        "sheets",
        "review_items",
        "contributions",
        "reported_evidence",
        # Phase 10: the hospital ledger, derived from public federal files; public like the atlas.
        "hospital_ids",
        "ledger_cost_reports",
        "ledger_schedule_h",
        "ledger_exposure",
        "ledger_metrics",
    }
)
# Personal data: never read from the source, never touched in the target.
EXCLUDED_TABLES = frozenset({"cases", "accounts", "sessions", "account_cases"})
# The spend ledger, carried over by `_new_events`: added to the target's own, never replacing it.
LEDGER_TABLE = UsageEventRow.__tablename__
EVENT_FIELDS = tuple(item.name for item in fields(UsageEvent))  # as governor.Ledger writes them
# Rows per read and per insert: source_docs hold whole documents, tens of megabytes in all.
BATCH_SIZE = 200
SOURCE_DATABASE = "the source database"
DEFAULT_PORTS = {"postgresql": 5432}


class UnclassifiedTable(RuntimeError):
    """A table is in neither COPIED_TABLES nor EXCLUDED_TABLES. Whether it may leave this
    database is a decision, never a default."""


class TargetNotEmpty(RuntimeError):
    """The target already has an atlas; replacing it has to be asked for."""


@dataclass(frozen=True)
class CopyReport:
    """What a copy moved, or in a dry run what it would move."""

    rows: dict[str, int]  # per copied table, parents first
    usage_events: int = 0  # spend events added to the target's ledger
    usage_already: int = 0  # spend events the target already had (an earlier copy), not re-added
    usage_source: str = ""  # SOURCE_DATABASE or the ledger file; empty when there were none
    deleted: dict[str, int] = field(default_factory=dict)  # with replace, children first
    dry_run: bool = False


def tables_to_copy(metadata: MetaData) -> list[Table]:
    """The copied tables in foreign-key order, parents first. A table in neither list stops the
    copy, so a new table is copied or kept back on purpose."""
    classified = COPIED_TABLES | EXCLUDED_TABLES | {LEDGER_TABLE}
    unknown = sorted(table.name for table in metadata.sorted_tables if table.name not in classified)
    if unknown:
        raise UnclassifiedTable(
            f"{', '.join(unknown)}: add to COPIED_TABLES or EXCLUDED_TABLES in waive/dbcopy.py"
        )
    return [table for table in metadata.sorted_tables if table.name in COPIED_TABLES]


def mask_url(url: str) -> str:
    """Driver, host, port and database of `url`, for printing: never the user, the password or
    the query string, which can carry a password too."""
    parsed = make_url(url)
    shown = URL.create(
        parsed.drivername, host=parsed.host, port=parsed.port, database=parsed.database
    )
    return shown.render_as_string(hide_password=False)


def redact(message: str, url: str) -> str:
    """`message` with `url` and its password, as typed and as decoded, replaced by ***. For
    printing an error a driver raised."""
    secrets = {url}
    typed = re.search(r"//[^/@:]*:([^@/]*)@", url)
    if typed:
        secrets.add(typed.group(1))
    with suppress(ArgumentError):
        secrets.add(make_url(url).password or "")
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        message = message.replace(secret, "***")
    return message


def same_database(first: str, second: str) -> bool:
    """Whether two URLs name one database, whatever their user, password, driver or query, so a
    copy is never pointed back at its own source."""
    a, b = make_url(first), make_url(second)
    backend = a.get_backend_name()
    if backend != b.get_backend_name():
        return False
    if backend == "sqlite":  # an in-memory database is never another URL's
        files = [
            Path(url.database).expanduser().resolve()
            for url in (a, b)
            if url.database not in (None, "", ":memory:")
        ]
        return len(files) == 2 and files[0] == files[1]
    port = DEFAULT_PORTS.get(backend)
    return ((a.host or "").lower(), a.port or port, a.database) == (
        (b.host or "").lower(),
        b.port or port,
        b.database,
    )


def read_ledger(path: Path) -> list[dict[str, str]]:
    """The spend events in a JSON-lines ledger (governor.Ledger), oldest first, each value the
    exact string the file holds: amounts stay decimal strings, timestamps stay as written."""
    events: list[dict[str, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            event = {name: str(row[name]) for name in EVENT_FIELDS}
            for amount in ("units", "usd"):  # the governor sums these as Decimal
                Decimal(event[amount])
        except (ValueError, KeyError, TypeError, InvalidOperation) as error:
            raise ValueError(f"{path} line {number} is not a spend event") from error
        events.append(event)
    return events


def reset_sequences(connection: Connection, tables: Iterable[Table]) -> list[str]:
    """On PostgreSQL, move each integer primary key's sequence past the highest id in its table:
    rows inserted with their own ids leave the sequence behind, and the app's next insert would
    collide with one. Returns the statements run; none elsewhere, as SQLite takes the next id
    from the table itself."""
    if connection.dialect.name != "postgresql":
        return []
    quote = connection.dialect.identifier_preparer.quote
    statements = []
    for table in tables:
        keys = list(table.primary_key.columns)
        if len(keys) != 1 or not isinstance(keys[0].type, Integer):
            continue  # text and composite keys have no sequence
        name, column = table.name, keys[0].name  # our own metadata, not input
        statements.append(
            f"SELECT setval(pg_get_serial_sequence('{name}', '{column}'), "
            f"COALESCE((SELECT MAX({quote(column)}) FROM {quote(name)}), 0) + 1, false)"
        )
    for statement in statements:
        connection.execute(text(statement))
    return statements


def _count(connection: Connection, table: Table) -> int:
    return int(connection.scalar(select(func.count()).select_from(table)) or 0)


def _ledger_events(connection: Connection) -> list[dict[str, str]]:
    ledger = UsageEventRow.__table__
    query = select(*(ledger.c[name] for name in EVENT_FIELDS)).order_by(ledger.c.id)
    return [dict(row) for row in connection.execute(query).mappings()]


def _new_events(
    events: list[dict[str, str]], existing: list[dict[str, str]]
) -> list[dict[str, str]]:
    """`events` less the ones the target already has, repeats counted: two calls in the same
    second for the same purpose are two events, and an earlier copy's events are not added again
    (while the target's own spending, which `events` never holds, stays as it is)."""

    def key(event: Mapping[str, str]) -> tuple[str, ...]:
        return tuple(event[name] for name in EVENT_FIELDS)

    have = Counter(key(event) for event in existing)
    new = []
    for event in events:
        if have[key(event)]:
            have[key(event)] -= 1
        else:
            new.append(event)
    return new


def _portable(row: Mapping[str, Any], texts: list[str], stamps: list[str]) -> dict[str, Any]:
    """`row` as PostgreSQL takes it. Text loses NUL characters, which PostgreSQL refuses and a few
    fetched documents carry for no meaning. Naive timestamps, as SQLite hands back values written
    in UTC, are marked UTC so the target's time zone setting cannot shift them."""
    values = dict(row)
    for name in texts:
        if values[name] is not None and "\x00" in values[name]:
            values[name] = values[name].replace("\x00", "")
    for name in stamps:
        if values[name] is not None and values[name].tzinfo is None:
            values[name] = values[name].replace(tzinfo=UTC)
    return values


def _copy_rows(read: Connection, write: Connection, table: Table, batch_size: int) -> int:
    """Stream `table` from `read` into `write`, `batch_size` rows at a time, primary keys kept."""
    texts = [column.name for column in table.columns if isinstance(column.type, String)]
    stamps = [
        column.name
        for column in table.columns
        if isinstance(column.type, DateTime) and column.type.timezone
    ]
    query = select(table).order_by(*table.primary_key.columns)
    result = read.execute(query.execution_options(yield_per=batch_size))
    copied = 0
    for batch in result.mappings().partitions():
        write.execute(insert(table), [_portable(row, texts, stamps) for row in batch])
        copied += len(batch)
    return copied


def copy_database(
    source: Engine,
    target: Engine,
    *,
    ledger_path: Path | None = None,
    replace: bool = False,
    dry_run: bool = False,
    batch_size: int = BATCH_SIZE,
) -> CopyReport:
    """Copy COPIED_TABLES from `source` (current schema) into `target` with their primary keys,
    then the spend history: the source's `usage_events` rows when it has any, otherwise the
    JSON-lines ledger at `ledger_path`. Events the target already has are not added twice and its
    own are never removed, so the governor's caps there count everything spent.

    The target's schema is created or upgraded first. A target that already has hospitals is
    refused unless `replace`, which empties the copied tables, children first, in the copy's own
    transaction: a failed copy leaves the target's rows as they were. A dry run reads both
    databases, refuses the same way, and writes nothing, not even the schema."""
    tables = tables_to_copy(Base.metadata)
    by_name = {table.name: table for table in tables}
    ledger = UsageEventRow.__table__
    if not dry_run:
        init_db(target)
    with source.connect() as read, target.connect() as write:
        present = set(inspect(write).get_table_names())
        hospitals = _count(write, by_name["hospitals"]) if "hospitals" in present else 0
        if hospitals and not replace:
            raise TargetNotEmpty(f"the target already has {hospitals} hospital(s)")
        events, origin = _ledger_events(read), SOURCE_DATABASE
        if not events and ledger_path is not None and ledger_path.exists():
            events, origin = read_ledger(ledger_path), str(ledger_path)
        new = _new_events(events, _ledger_events(write) if LEDGER_TABLE in present else [])
        if dry_run:
            deleted = {
                table.name: _count(write, table)
                for table in reversed(tables)
                if replace and table.name in present
            }
            copied = {table.name: _count(read, table) for table in tables}
        else:
            deleted = {}
            if replace:
                for table in reversed(tables):
                    deleted[table.name] = write.execute(delete(table)).rowcount
            copied = {table.name: _copy_rows(read, write, table, batch_size) for table in tables}
            if new:
                write.execute(insert(ledger), new)
            reset_sequences(write, [*tables, ledger])
            write.commit()
    return CopyReport(
        rows=copied,
        usage_events=len(new),
        usage_already=len(events) - len(new),
        usage_source=origin if events else "",
        deleted=deleted,
        dry_run=dry_run,
    )
