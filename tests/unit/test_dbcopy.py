"""`copy_database` moves the atlas, its sources, the de-identified learning tables and the spend
history into another database (production PostgreSQL); cases, accounts and sessions never move.
Every name, document and amount here is fictional."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, event, func, insert, inspect, select
from sqlalchemy.dialects import postgresql, sqlite

from waive import dbcopy
from waive.db import (
    AccountCaseRow,
    AccountRow,
    Base,
    CaseRow,
    ContributionRow,
    HospitalRow,
    ReportedEvidenceRow,
    ReviewItemRow,
    SessionRow,
    SheetRow,
    SourceDocRow,
    UsageEventRow,
    hospital_sources,
    init_db,
    make_engine,
    session_scope,
)
from waive.dbcopy import (
    COPIED_TABLES,
    EXCLUDED_TABLES,
    TargetNotEmpty,
    UnclassifiedTable,
    copy_database,
    mask_url,
    read_ledger,
    redact,
    reset_sequences,
    same_database,
    tables_to_copy,
)
from waive.governor import DbLedger, Governor, Ledger, UsageEvent

CASE_HASH = "0123456789abcdef"  # 16 hex characters, as learning.hashing makes them
COPY_ORDER = [
    "contributions",
    "hospitals",
    "reported_evidence",
    "review_items",
    "source_docs",
    "hospital_sources",
    "sheets",
]
# Two Tavily calls in the same second for the same purpose are two calls: both must arrive.
LEDGER = [
    {
        "provider": "tavily",
        "units": "1",
        "usd": "0",
        "purpose": "search",
        "ts": "2026-10-02T20:55:46+00:00",
    },
    {
        "provider": "tavily",
        "units": "1",
        "usd": "0",
        "purpose": "search",
        "ts": "2026-10-02T20:55:46+00:00",
    },
    {
        "provider": "tavily",
        "units": "2",
        "usd": "0",
        "purpose": "extract",
        "ts": "2026-10-03T07:00:00+00:00",
    },
    {
        "provider": "token_factory",
        "units": "1834",
        "usd": "0.000123450",
        "purpose": "structure",
        "ts": "2026-10-03T08:00:00+00:00",
    },
    {
        "provider": "token_factory",
        "units": "912",
        "usd": "0.004100000",
        "purpose": "vision",
        "ts": "2026-10-04T09:30:00+00:00",
    },
]


def database(tmp_path, name):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    init_db(engine)
    return engine


def table(name):
    return Base.metadata.tables[name]


def rows(engine, which):
    query = select(which).order_by(*which.primary_key.columns)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(query).mappings()]


def count(engine, which):
    with engine.connect() as connection:
        return connection.scalar(select(func.count()).select_from(which))


def hospital(ccn, name):
    return HospitalRow(
        ccn=ccn,
        name=name,
        city="BOSTON",
        state="MA",
        zip="02118",
        phone="617-555-0100",
        hospital_type="Acute Care Hospitals",
        ownership="Voluntary non-profit - Private",
        website_domain="example.org",
        domain_confidence=0.9,
        updated_at=datetime(2026, 10, 2, 23, 29, 55, tzinfo=UTC),
    )


def source_doc(doc_id, text, sha256="a" * 64):
    return SourceDocRow(
        id=doc_id,
        kind="hospital_web",
        url=f"https://example.org/{doc_id}.pdf",
        title="Financial assistance policy",
        fetched_on=date(2026, 10, 1),
        sha256=sha256,
        text=text,
    )


def seed_atlas(engine):
    """At least one row in every copied table, with ids that do not start at 1."""
    with session_scope(engine) as session:
        session.add_all(
            [
                hospital("229999", "ST. EXAMPLE MEDICAL CENTER"),
                hospital("229998", "FICTIONAL GENERAL HOSPITAL"),
                source_doc("fap-0000000000000001", "Free care up to 250% of the poverty level."),
                source_doc("summary-0000000000000002", "A plain-language summary.", "b" * 64),
            ]
        )
        session.flush()
        session.execute(
            insert(hospital_sources),
            [
                {"ccn": "229999", "source_id": "fap-0000000000000001"},
                {"ccn": "229998", "source_id": "summary-0000000000000002"},
            ],
        )
        session.add_all(
            [
                SheetRow(
                    id=7,
                    ccn="229999",
                    version=1,
                    status="published",
                    body={"hospital": {"ccn": "229999"}, "free_up_to_fpl": 250},
                    diff=None,
                    created_at=datetime(2026, 10, 2, 21, 8, 38, tzinfo=UTC),
                ),
                SheetRow(
                    id=9,
                    ccn="229999",
                    version=2,
                    status="held",
                    body={"hospital": {"ccn": "229999"}, "free_up_to_fpl": 300},
                    diff={"changed": ["eligibility.free_up_to_fpl"]},
                    created_at=datetime(2026, 10, 9, 8, 0, 0, tzinfo=UTC),
                ),
                ReviewItemRow(
                    id=42,
                    ccn=None,
                    kind="scout_request",
                    detail={
                        "hospital_name": "Fictional Clinic",
                        "fap_url": "example.net",
                        "state": "MA",
                        "cases": [CASE_HASH],
                        "count": 1,
                    },
                ),
                ContributionRow(
                    id=5,
                    ccn="229999",
                    case_hash=CASE_HASH,
                    photo_class="fap",
                    sha256="c" * 64,
                    text="Patients with income up to 250% of the poverty level receive free care.",
                    reject_reasons=[],
                    status="open",
                    created_on=date(2026, 10, 3),
                ),
                ReportedEvidenceRow(
                    id=3,
                    ccn="229999",
                    field_path="apply.documents_required",
                    value="photo_id",
                    case_hash=CASE_HASH,
                    created_on=date(2026, 10, 3),
                ),
            ]
        )


def seed_personal(engine, case_id="case-synthetic-1"):
    """A case, an account, its sign-in session and its history entry."""
    with session_scope(engine) as session:
        session.add(
            CaseRow(
                id=case_id,
                state="MA",
                ccn="229999",
                status="new",
                token_generation=1,
                sealed="sealed-synthetic-blob",
            )
        )
        account = AccountRow(provider="google", subject_hash="d" * 64)
        session.add(account)
        session.flush()
        session.add(
            SessionRow(
                id="e" * 64, account_id=account.id, expires_at=datetime(2026, 11, 1, tzinfo=UTC)
            )
        )
        session.add(AccountCaseRow(account_id=account.id, case_id=case_id))


def write_ledger(path, events):
    """The governor's JSON-lines format, with a blank line at the end as a hand edit leaves."""
    path.write_text("".join(json.dumps(e) + "\n" for e in events) + "\n", encoding="utf-8")


def governor(ledger):
    return Governor(ledger, 1000, Decimal("15"))


def test_every_copied_table_arrives_with_its_ids_and_values(tmp_path):
    source, target = database(tmp_path, "local.db"), make_engine(f"sqlite:///{tmp_path / 'c.db'}")
    seed_atlas(source)
    report = copy_database(source, target)  # the target has no schema yet: the copy creates it
    assert list(report.rows) == COPY_ORDER  # parents before children
    assert report.rows == {
        "contributions": 1,
        "hospitals": 2,
        "reported_evidence": 1,
        "review_items": 1,
        "source_docs": 2,
        "hospital_sources": 2,
        "sheets": 2,
    }
    for name in COPIED_TABLES:
        assert rows(target, table(name)) == rows(source, table(name)), name
    assert [row["id"] for row in rows(target, SheetRow.__table__)] == [7, 9]
    assert [row["id"] for row in rows(target, ReviewItemRow.__table__)] == [42]


def test_cases_accounts_and_sessions_never_leave(tmp_path):
    source, target = database(tmp_path, "local.db"), database(tmp_path, "cloud.db")
    seed_atlas(source)
    seed_personal(source)
    report = copy_database(source, target)
    assert not set(report.rows) & EXCLUDED_TABLES
    for name in EXCLUDED_TABLES:
        assert count(target, table(name)) == 0, name
    assert count(source, CaseRow.__table__) == 1  # the local database keeps its own


def test_personal_tables_are_listed_and_nothing_copied_points_at_them():
    assert EXCLUDED_TABLES == {"cases", "accounts", "sessions", "account_cases"}
    assert not COPIED_TABLES & EXCLUDED_TABLES
    assert [copied.name for copied in tables_to_copy(Base.metadata)] == COPY_ORDER
    for copied in tables_to_copy(Base.metadata):
        for key in copied.foreign_keys:
            assert key.column.table.name in COPIED_TABLES, (copied.name, key.target_fullname)
    # The learning tables count cases through a one-way hash and never hold a case id.
    for name in ("contributions", "reported_evidence"):
        assert "case_hash" in table(name).columns and "case_id" not in table(name).columns


def test_a_table_nobody_classified_stops_the_copy(tmp_path, monkeypatch):
    metadata = MetaData()
    for known in Base.metadata.sorted_tables:
        known.to_metadata(metadata)
    Table("payment_methods", metadata, Column("id", Integer, primary_key=True))
    with pytest.raises(UnclassifiedTable, match="payment_methods"):
        tables_to_copy(metadata)

    source, target = database(tmp_path, "local.db"), database(tmp_path, "cloud.db")
    seed_atlas(source)
    monkeypatch.setattr(dbcopy, "Base", SimpleNamespace(metadata=metadata))
    with pytest.raises(UnclassifiedTable, match="payment_methods"):
        copy_database(source, target)
    assert count(target, HospitalRow.__table__) == 0


def test_the_spend_ledger_file_becomes_the_targets_usage_events(tmp_path):
    source, target = database(tmp_path, "local.db"), database(tmp_path, "cloud.db")
    seed_atlas(source)
    path = tmp_path / "usage.jsonl"
    write_ledger(path, LEDGER)
    report = copy_database(source, target, ledger_path=path)
    assert (report.usage_events, report.usage_already, report.usage_source) == (5, 0, str(path))

    from_file, from_target = governor(Ledger(path)), governor(DbLedger(target))
    assert from_target.summary() == from_file.summary()
    assert from_target.summary()["tavily"] == (Decimal("4"), Decimal("0"))
    for day in (date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 4)):
        assert from_target.tavily_used_today(day) == from_file.tavily_used_today(day)
        assert from_target.token_factory_used_today(day) == from_file.token_factory_used_today(day)
    stored = [
        {name: row[name] for name in ("provider", "units", "usd", "purpose", "ts")}
        for row in rows(target, UsageEventRow.__table__)
    ]
    assert stored == LEDGER  # exact decimal strings and timestamps, in the file's order


def test_copying_again_adds_only_new_spend_and_keeps_what_the_target_spent(tmp_path):
    source, target = database(tmp_path, "local.db"), database(tmp_path, "cloud.db")
    seed_atlas(source)
    path = tmp_path / "usage.jsonl"
    write_ledger(path, LEDGER[:1])  # copied between the two same-second calls
    copy_database(source, target, ledger_path=path)
    cloud = UsageEvent("tavily", Decimal("1"), Decimal("0"), "doctor", "2026-10-05T10:00:00+00:00")
    DbLedger(target).record(cloud)  # the production app spent a credit
    write_ledger(path, LEDGER)  # and the local machine spent more
    report = copy_database(source, target, ledger_path=path, replace=True)
    assert (report.usage_events, report.usage_already) == (4, 1)
    assert DbLedger(target).totals("tavily") == (Decimal("5"), Decimal("0"))  # 4 local + 1 cloud
    assert DbLedger(target).totals("token_factory") == Ledger(path).totals("token_factory")


def test_spend_rows_in_the_source_database_are_copied_instead_of_the_file(tmp_path):
    source, target = database(tmp_path, "local.db"), database(tmp_path, "cloud.db")
    seed_atlas(source)
    DbLedger(source).record(
        UsageEvent(
            "token_factory", Decimal("100"), Decimal("0.0025"), "structure", "2026-10-06T12:00:00Z"
        )
    )
    path = tmp_path / "usage.jsonl"
    write_ledger(path, LEDGER)
    report = copy_database(source, target, ledger_path=path)
    assert (report.usage_events, report.usage_source) == (1, "the source database")
    assert DbLedger(target).totals("token_factory") == (Decimal("100"), Decimal("0.0025"))
    assert DbLedger(target).totals("tavily") == (Decimal("0"), Decimal("0"))


def test_a_ledger_line_that_is_not_a_spend_event_is_named(tmp_path):
    path = tmp_path / "usage.jsonl"
    path.write_text(json.dumps(LEDGER[0]) + '\n{"provider": "tavily", "units": "1"}\n')
    with pytest.raises(ValueError, match="line 2"):
        read_ledger(path)
    path.write_text(json.dumps({**LEDGER[0], "usd": "about a dollar"}) + "\n")
    with pytest.raises(ValueError, match="line 1"):
        read_ledger(path)


def test_a_target_that_already_has_hospitals_is_refused(tmp_path):
    source, target = database(tmp_path, "local.db"), database(tmp_path, "cloud.db")
    seed_atlas(source)
    with session_scope(target) as session:
        session.add(hospital("220001", "FICTIONAL CLOUD HOSPITAL"))
    with pytest.raises(TargetNotEmpty, match="1 hospital"):
        copy_database(source, target)
    assert [row["ccn"] for row in rows(target, HospitalRow.__table__)] == ["220001"]
    assert count(target, SheetRow.__table__) == 0


def test_replace_empties_the_copied_tables_and_leaves_personal_data_and_spend_alone(tmp_path):
    source, target = database(tmp_path, "local.db"), database(tmp_path, "cloud.db")
    seed_atlas(source)
    with session_scope(target) as session:
        session.add(hospital("220001", "FICTIONAL CLOUD HOSPITAL"))
        session.flush()
        session.add(SheetRow(id=1, ccn="220001", version=1, status="published", body={}))
    seed_personal(target, "case-in-the-cloud")
    spent = UsageEvent("tavily", Decimal("3"), Decimal("0"), "search", "2026-10-07T09:00:00+00:00")
    DbLedger(target).record(spent)

    report = copy_database(source, target, replace=True)
    assert list(report.deleted) == COPY_ORDER[::-1]  # children before parents
    assert report.deleted["sheets"] == 1 and report.deleted["hospitals"] == 1
    for name in COPIED_TABLES:
        assert rows(target, table(name)) == rows(source, table(name)), name
    assert [row["id"] for row in rows(target, CaseRow.__table__)] == ["case-in-the-cloud"]
    for name in ("accounts", "sessions", "account_cases"):
        assert count(target, table(name)) == 1, name
    assert DbLedger(target).totals("tavily") == (Decimal("3"), Decimal("0"))


def test_a_dry_run_counts_and_writes_nothing(tmp_path):
    source = database(tmp_path, "local.db")
    seed_atlas(source)
    seed_personal(source)
    path = tmp_path / "usage.jsonl"
    write_ledger(path, LEDGER)

    fresh = make_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    report = copy_database(source, fresh, ledger_path=path, dry_run=True)
    assert report.dry_run and list(report.rows) == COPY_ORDER
    assert report.rows["hospitals"] == 2 and report.rows["sheets"] == 2
    assert (report.usage_events, report.usage_source) == (5, str(path))
    assert inspect(fresh).get_table_names() == []  # not even the schema

    occupied = database(tmp_path, "occupied.db")
    with session_scope(occupied) as session:
        session.add(hospital("220001", "FICTIONAL CLOUD HOSPITAL"))
    with pytest.raises(TargetNotEmpty):  # a dry run tells what the real copy would do
        copy_database(source, occupied, ledger_path=path, dry_run=True)
    report = copy_database(source, occupied, ledger_path=path, dry_run=True, replace=True)
    assert report.deleted["hospitals"] == 1
    assert [row["ccn"] for row in rows(occupied, HospitalRow.__table__)] == ["220001"]
    for name in COPIED_TABLES - {"hospitals"} | {"usage_events"}:
        assert count(occupied, table(name)) == 0, name


def test_documents_are_read_and_written_a_batch_at_a_time(tmp_path):
    source, target = database(tmp_path, "local.db"), database(tmp_path, "cloud.db")
    with session_scope(source) as session:
        session.add_all(
            source_doc(f"fap-{n:016d}", "policy text " * 1000, f"{n:064d}") for n in range(5)
        )
    sizes = []

    @event.listens_for(target, "before_execute")
    def watch(connection, clause, multiparams, params, execution_options):
        if getattr(clause, "is_insert", False) and clause.table.name == "source_docs":
            sizes.append(len(multiparams) or 1)  # one row arrives as `params`

    report = copy_database(source, target, batch_size=2)
    assert sizes == [2, 2, 1] and report.rows["source_docs"] == 5


def test_rows_are_sent_the_way_postgresql_takes_them(tmp_path):
    source, target = database(tmp_path, "local.db"), database(tmp_path, "cloud.db")
    with session_scope(source) as session:
        session.add(hospital("229999", "ST. EXAMPLE MEDICAL CENTER"))
        session.add(source_doc("fap-0000000000000003", "Free care\x00 up to 250%.", "f" * 64))
    sent = {}

    @event.listens_for(target, "before_execute")
    def watch(connection, clause, multiparams, params, execution_options):
        if getattr(clause, "is_insert", False):
            sent[clause.table.name] = multiparams or [params]

    copy_database(source, target)
    # SQLite reads timestamps back without a zone; they were written in UTC and are sent so.
    [sent_hospital] = sent["hospitals"]
    assert sent_hospital["updated_at"] == datetime(2026, 10, 2, 23, 29, 55, tzinfo=UTC)
    # PostgreSQL refuses NUL characters in text; the document's own hash, which refresh
    # compares with a fresh fetch, stays.
    [row] = rows(target, SourceDocRow.__table__)
    assert row["text"] == "Free care up to 250%." and row["sha256"] == "f" * 64


class RecordingConnection:
    def __init__(self, dialect):
        self.dialect = dialect
        self.statements = []

    def execute(self, statement):
        self.statements.append(str(statement))


def test_postgresql_sequences_move_past_the_copied_ids():
    connection = RecordingConnection(postgresql.dialect())
    ran = reset_sequences(connection, [*tables_to_copy(Base.metadata), UsageEventRow.__table__])
    assert ran == connection.statements
    assert ran == [
        f"SELECT setval(pg_get_serial_sequence('{name}', 'id'), "
        f"COALESCE((SELECT MAX(id) FROM {name}), 0) + 1, false)"
        for name in ("contributions", "reported_evidence", "review_items", "sheets", "usage_events")
    ]  # text keys (hospitals, source_docs, hospital_sources) have no sequence


def test_sqlite_has_no_sequence_to_move():
    connection = RecordingConnection(sqlite.dialect())
    assert reset_sequences(connection, tables_to_copy(Base.metadata)) == []
    assert connection.statements == []


def test_the_printed_target_names_no_user_and_no_password():
    url = (
        "postgresql+psycopg://copier:S3cret-pw@db.example.net:5432/waive"
        "?sslmode=verify-full&password=Other-pw"
    )
    masked = mask_url(url)
    assert masked == "postgresql+psycopg://db.example.net:5432/waive"
    for hidden in ("copier", "S3cret-pw", "Other-pw", "sslmode"):
        assert hidden not in masked
    assert mask_url("sqlite:///var/waive.db") == "sqlite:///var/waive.db"


def test_a_driver_message_loses_the_password_in_any_spelling():
    url = "postgresql+psycopg://copier:p%40ss-W0rd@db.example.net:5432/waive"
    message = f"failed for {url}; password p@ss-W0rd; raw p%40ss-W0rd"
    cleaned = redact(message, url)
    assert "p@ss-W0rd" not in cleaned and "p%40ss-W0rd" not in cleaned
    assert "copier" not in cleaned  # a URL echoed whole goes whole, user name included
    assert cleaned.count("***") == 3


def test_same_database_sees_through_spelling(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert same_database("sqlite:///var/waive.db", f"sqlite:///{tmp_path / 'var' / 'waive.db'}")
    assert not same_database("sqlite:///var/waive.db", "sqlite:///var/other.db")
    assert same_database(
        "postgresql+psycopg://a:x@db.example.net/waive",
        "postgresql://b:y@DB.example.net:5432/waive",
    )
    assert not same_database(
        "postgresql+psycopg://a:x@db.example.net/waive",
        "postgresql+psycopg://a:x@db.example.net/w2",
    )
    assert not same_database("sqlite:///var/waive.db", "postgresql+psycopg://a:x@localhost/waive")
