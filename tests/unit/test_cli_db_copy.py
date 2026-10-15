"""`waive db copy` on the command line. The target comes from WAIVE_COPY_TO_URL (or --to) so the
password need not sit in argv, and it is only ever printed without its user and password."""

from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from waive import cli
from waive.cli import app
from waive.db import CaseRow, HospitalRow, init_db, make_engine, session_scope
from waive.dbcopy import CopyReport

from tests.unit.test_dbcopy import LEDGER, hospital, rows, seed_atlas, seed_personal, write_ledger

CLOUD_URL = "postgresql+psycopg://copier:S3cret-pw@db.example.net:5432/waive?sslmode=require"


@pytest.fixture
def local(monkeypatch, tmp_path):
    """The developer's machine: a seeded local database and spend ledger, and no .env."""
    monkeypatch.chdir(tmp_path)
    url = f"sqlite:///{tmp_path / 'local.db'}"
    monkeypatch.setenv("WAIVE_DATABASE_URL", url)
    monkeypatch.setenv("WAIVE_LEDGER_BACKEND", "file")
    monkeypatch.setenv("WAIVE_LEDGER_PATH", str(tmp_path / "usage.jsonl"))
    monkeypatch.delenv("WAIVE_COPY_TO_URL", raising=False)
    engine = make_engine(url)
    init_db(engine)
    seed_atlas(engine)
    seed_personal(engine)
    write_ledger(tmp_path / "usage.jsonl", LEDGER)
    yield engine
    engine.dispose()


def target(tmp_path):
    return f"sqlite:///{tmp_path / 'cloud.db'}"


def test_copy_takes_the_target_from_the_environment_and_lists_what_moved(
    local, monkeypatch, tmp_path
):
    monkeypatch.setenv("WAIVE_COPY_TO_URL", target(tmp_path))
    result = CliRunner().invoke(app, ["db", "copy"])
    assert result.exit_code == 0, result.output
    assert "hospitals: 2 row(s)" in result.output and "sheets: 2 row(s)" in result.output
    assert "5 spend event(s)" in result.output
    assert "Not copied (personal data): account_cases, accounts, cases, sessions" in result.output
    copied = make_engine(target(tmp_path))
    with session_scope(copied) as session:
        assert session.get(HospitalRow, "229999") is not None
        assert session.get(CaseRow, "case-synthetic-1") is None
    copied.dispose()


def test_dry_run_prints_the_counts_and_writes_nothing(local, tmp_path):
    result = CliRunner().invoke(app, ["db", "copy", "--dry-run", "--to", target(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output and "hospitals: 2 row(s)" in result.output
    untouched = make_engine(target(tmp_path))
    assert inspect(untouched).get_table_names() == []
    untouched.dispose()


def test_without_a_target_it_says_how_to_give_one(local):
    result = CliRunner().invoke(app, ["db", "copy"])
    assert result.exit_code == 2
    assert "WAIVE_COPY_TO_URL" in result.output and "--to" in result.output


def test_the_local_database_is_not_a_target(local):
    result = CliRunner().invoke(app, ["db", "copy", "--to", "sqlite:///local.db"])  # cwd: tmp_path
    assert result.exit_code == 2
    assert "same database" in result.output


def test_a_target_with_hospitals_is_replaced_only_when_asked(local, tmp_path):
    other = make_engine(target(tmp_path))
    init_db(other)
    with session_scope(other) as session:
        session.add(hospital("220001", "FICTIONAL CLOUD HOSPITAL"))
    result = CliRunner().invoke(app, ["db", "copy", "--to", target(tmp_path)])
    assert result.exit_code == 1
    assert "1 hospital" in result.output and "--replace" in result.output
    assert [row["ccn"] for row in rows(other, HospitalRow.__table__)] == ["220001"]

    result = CliRunner().invoke(app, ["db", "copy", "--to", target(tmp_path), "--replace"])
    assert result.exit_code == 0, result.output
    assert [row["ccn"] for row in rows(other, HospitalRow.__table__)] == ["229998", "229999"]
    other.dispose()


def test_the_target_is_printed_without_user_or_password(local, monkeypatch, tmp_path):
    seen = {}

    def copy(source, target, **options):  # no connection is opened to the fictional server
        seen.update(options)
        return CopyReport(rows={"hospitals": 2}, usage_events=5, usage_source="usage.jsonl")

    monkeypatch.setattr(cli, "copy_database", copy)
    monkeypatch.setenv("WAIVE_COPY_TO_URL", CLOUD_URL)
    result = CliRunner().invoke(app, ["db", "copy"])
    assert result.exit_code == 0, result.output
    assert "postgresql+psycopg://db.example.net:5432/waive" in result.output
    assert "S3cret-pw" not in result.output and "copier" not in result.output
    assert seen == {
        "ledger_path": Path(tmp_path / "usage.jsonl"),
        "replace": False,
        "dry_run": False,
    }
    help_text = CliRunner().invoke(app, ["db", "copy", "--help"]).output
    assert "WAIVE_COPY_TO_URL" in help_text and "S3cret-pw" not in help_text


def test_a_failed_copy_prints_the_driver_message_without_the_password(local, monkeypatch):
    def copy(source, target, **options):
        raise OperationalError("SELECT 1", {}, Exception(f"could not reach {CLOUD_URL}"))

    monkeypatch.setattr(cli, "copy_database", copy)
    monkeypatch.setenv("WAIVE_COPY_TO_URL", CLOUD_URL)
    result = CliRunner().invoke(app, ["db", "copy"])
    assert result.exit_code == 1
    assert "no rows were written" in result.output and "could not reach" in result.output
    assert "S3cret-pw" not in result.output and "copier" not in result.output
