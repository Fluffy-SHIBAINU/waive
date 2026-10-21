"""Phase 9, task 9.6: accounts, sessions and the cases an account keeps. No provider yet: an
account is a provider name and a keyed hash of that provider's subject id, nothing else."""

import base64
import hashlib
import hmac
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import inspect, select, text
from typer.testing import CliRunner

from waive.atlas import repo
from waive.auth import accounts, sessions
from waive.cases.service import CaseContext, delete_case, purge_cases, start_case
from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.cli import app as cli_app
from waive.db import (
    AccountCaseRow,
    AccountRow,
    Base,
    CaseRow,
    ReviewItemRow,
    SessionRow,
    init_db,
    make_engine,
    session_scope,
)
from waive.demo import forget_cases
from waive.learning.hashing import case_hash

TODAY = date(2026, 10, 11)
SECRET = "s" * 40
NEW_TABLES = ("accounts", "sessions", "account_cases")


@pytest.fixture
def ctx():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        yield CaseContext(
            session=session,
            ai=None,
            cipher=FieldCipher(base64.b64decode(new_key())),
            signer=TokenSigner(SECRET),
            today=TODAY,
        )


def account(ctx, subject="subject-1", provider="google"):
    digest = accounts.subject_hash(SECRET, provider, subject)
    return accounts.get_or_create(ctx.session, provider, digest)


def count(ctx, model):
    return len(ctx.session.scalars(select(model)).all())


def test_the_new_tables_hold_a_provider_a_subject_hash_and_dates_and_nothing_about_a_person():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    inspector = inspect(engine)
    columns = {
        table: {column["name"] for column in inspector.get_columns(table)} for table in NEW_TABLES
    }
    assert columns == {
        "accounts": {"id", "provider", "subject_hash", "created_at"},
        "sessions": {"id", "account_id", "created_at", "expires_at"},
        "account_cases": {"account_id", "case_id", "added_at"},
    }
    personal = ("email", "name", "picture", "photo", "phone", "address", "birth", "locale")
    for table, names in columns.items():
        for name in names:
            assert not any(word in name for word in personal), (table, name)
    # One account per provider subject; one link per account and case.
    [unique] = inspector.get_unique_constraints("accounts")
    assert unique["column_names"] == ["provider", "subject_hash"]
    assert inspector.get_pk_constraint("account_cases")["constrained_columns"] == [
        "account_id",
        "case_id",
    ]


def test_the_subject_hash_is_keyed_stable_and_one_way():
    digest = accounts.subject_hash(SECRET, "google", "108204268033311374519")
    assert digest == accounts.subject_hash(SECRET, "google", "108204268033311374519")
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    assert "108204268033311374519" not in digest
    # HMAC-SHA256 under the token secret: without the secret the hash cannot be recomputed.
    expected = hmac.new(
        SECRET.encode(),
        accounts.subject_message("google", "108204268033311374519"),
        hashlib.sha256,
    ).hexdigest()
    assert digest == expected
    assert digest != hashlib.sha256(b"108204268033311374519").hexdigest()
    others = {
        accounts.subject_hash("t" * 40, "google", "108204268033311374519"),
        accounts.subject_hash(SECRET, "apple", "108204268033311374519"),
        accounts.subject_hash(SECRET, "google", "108204268033311374510"),
        # The provider and the subject cannot run into each other.
        accounts.subject_hash(SECRET, "goo", "gle108204268033311374519"),
    }
    assert len(others) == 4 and digest not in others
    # The web app holds the secret inside its link signer: the same hash comes out of it.
    signer = TokenSigner(SECRET)
    assert accounts.signer_subject_hash(signer, "google", "108204268033311374519") == digest


def test_the_subject_hash_never_matches_a_link_signature():
    """The token secret also signs the case links. A link's signed part is URL-safe base64, and
    the subject message carries zero bytes, so one can never stand in for the other."""
    message = accounts.subject_message("google", "abc")
    assert b"\x00" in message
    token = TokenSigner(SECRET).mint("case", "caregiver", 1, timedelta(days=1))
    assert "\x00" not in token


def test_get_or_create_makes_one_account_per_provider_subject(ctx):
    first = account(ctx)
    assert (first.provider, len(first.subject_hash)) == ("google", 64)
    assert first.created_at is not None
    assert account(ctx).id == first.id
    assert account(ctx, subject="subject-2").id != first.id
    assert account(ctx, provider="other").id != first.id
    assert count(ctx, AccountRow) == 3
    # Nothing about the subject itself is kept.
    stored = ctx.session.execute(text("SELECT * FROM accounts")).all()
    assert "subject-1" not in str(stored)


def test_two_first_sign_ins_at_once_end_up_with_the_same_account_and_lose_nothing_else():
    """The loser's insert is undone in its own savepoint: a case it started before asking for
    the account, and what it links afterwards, survive in the same transaction."""
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    digest = accounts.subject_hash(SECRET, "google", "subject-1")
    with session_scope(engine) as session:
        winner = accounts.get_or_create(session, "google", digest).id
    with session_scope(engine) as session:
        context = CaseContext(
            session=session,
            ai=None,
            cipher=FieldCipher(base64.b64decode(new_key())),
            signer=TokenSigner(SECRET),
            today=TODAY,
        )
        links = start_case(context, "MA")
        looks = []

        def not_there_yet(*args, **kwargs):
            # The first look happens before the other sign-in commits.
            looks.append(args)
            return None

        session.scalar = not_there_yet
        assert accounts.get_or_create(session, "google", digest).id == winner
        assert len(looks) == 1
        assert accounts.link_case(session, winner, links.case_id) is True
    with session_scope(engine) as session:
        assert len(session.scalars(select(AccountRow)).all()) == 1
        assert session.get(CaseRow, links.case_id) is not None
        assert [row.id for row in accounts.cases_of(session, winner)] == [links.case_id]


def test_an_account_lists_only_its_own_cases_newest_first(ctx):
    mine, other = account(ctx), account(ctx, subject="subject-2")
    old, new, theirs = (start_case(ctx, "MA") for _ in range(3))
    ctx.session.get(CaseRow, old.case_id).created_at = datetime(2026, 9, 1, tzinfo=UTC)
    ctx.session.get(CaseRow, new.case_id).created_at = datetime(2026, 10, 1, tzinfo=UTC)
    ctx.session.flush()
    assert accounts.link_case(ctx.session, mine.id, old.case_id) is True
    assert accounts.link_case(ctx.session, mine.id, new.case_id) is True
    assert accounts.link_case(ctx.session, other.id, theirs.case_id) is True
    assert accounts.link_case(ctx.session, mine.id, old.case_id) is False  # once is enough
    assert [row.id for row in accounts.cases_of(ctx.session, mine.id)] == [
        new.case_id,
        old.case_id,
    ]
    assert [row.id for row in accounts.cases_of(ctx.session, other.id)] == [theirs.case_id]
    assert accounts.is_linked(ctx.session, mine.id, new.case_id)
    assert not accounts.is_linked(ctx.session, mine.id, theirs.case_id)
    assert count(ctx, AccountCaseRow) == 3
    with pytest.raises(KeyError):
        accounts.link_case(ctx.session, mine.id, "no-such-case")
    # The account keeps which cases are its own, never their links.
    stored = str(ctx.session.execute(text("SELECT * FROM account_cases")).all())
    assert old.caregiver_token not in stored and old.senior_token not in stored


def test_deleting_a_case_removes_it_from_every_account(ctx):
    mine, other = account(ctx), account(ctx, subject="subject-2")
    gone, kept = start_case(ctx, "MA"), start_case(ctx, "MA")
    for owner in (mine, other):
        accounts.link_case(ctx.session, owner.id, gone.case_id)
    accounts.link_case(ctx.session, mine.id, kept.case_id)
    delete_case(ctx, gone.case_id)
    assert [row.id for row in accounts.cases_of(ctx.session, mine.id)] == [kept.case_id]
    assert accounts.cases_of(ctx.session, other.id) == []
    links = ctx.session.scalars(select(AccountCaseRow)).all()
    assert [(link.account_id, link.case_id) for link in links] == [(mine.id, kept.case_id)]
    assert count(ctx, AccountRow) == 2  # the accounts themselves stay


def test_purging_and_the_demo_reset_remove_the_links_with_the_cases(ctx):
    mine = account(ctx)
    abandoned, live = start_case(ctx, "MA"), start_case(ctx, "MA")
    ctx.session.get(CaseRow, abandoned.case_id).created_at = datetime(2026, 10, 1, tzinfo=UTC)
    ctx.session.flush()
    for links in (abandoned, live):
        accounts.link_case(ctx.session, mine.id, links.case_id)
    assert purge_cases(ctx.session, TODAY) == {"abandoned": 1, "expired": 0}
    assert [row.case_id for row in ctx.session.scalars(select(AccountCaseRow))] == [live.case_id]
    assert forget_cases(ctx.session) == 1
    assert count(ctx, AccountCaseRow) == 0 and count(ctx, CaseRow) == 0
    assert count(ctx, AccountRow) == 1


def test_deleting_an_account_deletes_its_sessions_and_every_case_linked_to_it(ctx):
    mine, other = account(ctx), account(ctx, subject="subject-2")
    first, second, theirs, loose = (start_case(ctx, "MA") for _ in range(4))
    for links in (first, second):
        accounts.link_case(ctx.session, mine.id, links.case_id)
    accounts.link_case(ctx.session, other.id, theirs.case_id)
    # A case deletion is the existing one-tap delete: what the case left elsewhere goes too.
    repo.add_review_item(
        ctx.session,
        None,
        "scout_request",
        {"hospital_name": "Somewhere", "cases": [case_hash(first.case_id)], "count": 1},
    )
    my_sessions = [sessions.create(ctx.session, mine.id) for _ in range(2)]
    their_session = sessions.create(ctx.session, other.id)

    assert accounts.delete_account(ctx, mine.id) == 2
    assert {row.id for row in ctx.session.scalars(select(CaseRow))} == {
        theirs.case_id,
        loose.case_id,
    }
    assert [row.id for row in ctx.session.scalars(select(AccountRow))] == [other.id]
    assert [row.case_id for row in ctx.session.scalars(select(AccountCaseRow))] == [theirs.case_id]
    assert count(ctx, ReviewItemRow) == 0
    for session_id in my_sessions:
        assert sessions.read(ctx.session, session_id) is None
    assert sessions.read(ctx.session, their_session) == other.id
    assert count(ctx, SessionRow) == 1
    # Deleting what is already gone is not an error.
    assert accounts.delete_account(ctx, mine.id) == 0


def test_a_session_is_a_random_id_that_reads_back_its_account_until_it_ends(ctx):
    mine = account(ctx)
    first = sessions.create(ctx.session, mine.id)
    second = sessions.create(ctx.session, mine.id)
    assert first != second
    # 32 random bytes, URL-safe: fit for a cookie as it is.
    assert len(first) >= 43 and set(first) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    assert sessions.read(ctx.session, first) == mine.id
    assert sessions.read(ctx.session, second) == mine.id
    for wrong in ("", "nope", first[:-1], first + "x", "x" * 5000):
        assert sessions.read(ctx.session, wrong) is None
    # A copy of the table is not a set of live sessions: the id itself is not in it.
    stored = str(ctx.session.execute(text("SELECT * FROM sessions")).all())
    assert first not in stored and second not in stored
    sessions.end(ctx.session, first)
    assert sessions.read(ctx.session, first) is None
    assert sessions.read(ctx.session, second) == mine.id
    sessions.end(ctx.session, first)  # ending twice is fine
    sessions.end(ctx.session, "")


def test_a_session_lasts_fourteen_days_and_expired_ones_are_purged(ctx):
    assert sessions.LIFETIME == timedelta(days=14)
    mine = account(ctx)
    start = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    old = sessions.create(ctx.session, mine.id, now=start)
    row = ctx.session.scalars(select(SessionRow)).one()
    assert row.account_id == mine.id
    assert row.expires_at.replace(tzinfo=UTC) - row.created_at.replace(tzinfo=UTC) == (
        timedelta(days=14)
    )
    last_moment = start + timedelta(days=14) - timedelta(seconds=1)
    assert sessions.read(ctx.session, old, now=last_moment) == mine.id
    assert sessions.read(ctx.session, old, now=start + timedelta(days=14)) is None
    assert sessions.read(ctx.session, old, now=start + timedelta(days=30)) is None

    later = start + timedelta(days=20)
    fresh = sessions.create(ctx.session, mine.id, now=later)
    assert sessions.purge_expired(ctx.session, now=later) == 1
    assert count(ctx, SessionRow) == 1
    assert sessions.read(ctx.session, fresh, now=later) == mine.id
    assert sessions.purge_expired(ctx.session, now=later) == 0


def test_db_purge_also_removes_sessions_that_ran_out(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WAIVE_DATABASE_URL", f"sqlite:///{tmp_path / 'waive.db'}")
    engine = make_engine(f"sqlite:///{tmp_path / 'waive.db'}")
    init_db(engine)
    with session_scope(engine) as session:
        mine = accounts.get_or_create(
            session, "google", accounts.subject_hash(SECRET, "google", "subject-1")
        )
        sessions.create(session, mine.id, now=datetime.now(UTC) - timedelta(days=15))
        live = sessions.create(session, mine.id)
    result = CliRunner().invoke(cli_app, ["db", "purge"])
    assert result.exit_code == 0, result.output
    assert "0 abandoned" in result.output and "1 expired sign-in session(s)" in result.output
    with session_scope(engine) as session:
        assert len(session.scalars(select(SessionRow)).all()) == 1
        assert sessions.read(session, live) == mine.id
    engine.dispose()


def test_an_existing_database_gets_the_new_tables_on_startup(tmp_path):
    """`init_db` runs on every start: a database made before sign-in existed gains the three
    tables and keeps what it had."""
    url = f"sqlite:///{tmp_path / 'waive.db'}"
    engine = make_engine(url)
    before = [table for table in Base.metadata.sorted_tables if table.name not in NEW_TABLES]
    Base.metadata.create_all(engine, tables=before)
    assert not set(NEW_TABLES) & set(inspect(engine).get_table_names())
    with session_scope(engine) as session:
        session.add(CaseRow(id="kept", state="MA", status="new", token_generation=3))
    engine.dispose()

    engine = make_engine(url)
    assert init_db(engine) == []  # new tables, no columns added to old ones
    assert set(NEW_TABLES) <= set(inspect(engine).get_table_names())
    with session_scope(engine) as session:
        assert session.get(CaseRow, "kept").token_generation == 3
        mine = accounts.get_or_create(
            session, "google", accounts.subject_hash(SECRET, "google", "subject-1")
        )
        accounts.link_case(session, mine.id, "kept")
        session_id = sessions.create(session, mine.id)
    with session_scope(engine) as session:
        assert sessions.read(session, session_id) == mine.id
        assert [row.id for row in accounts.cases_of(session, mine.id)] == ["kept"]
    assert init_db(engine) == []
    engine.dispose()
