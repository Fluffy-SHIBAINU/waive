"""`waive atlas build`, `schedule` and `import-eve` and the scout's skip list (task 7.12b).

Every test runs in a temporary folder with its own database and its own skip list, so the real
list under data/ and the real database are never read."""

import json
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from waive.atlas import repo
from waive.atlas.pipeline import BuildResult
from waive.atlas.schedule import RunReport
from waive.atlas.skiplist import EVE_SKIP
from waive.cli import app
from waive.db import init_db, make_engine, session_scope

from tests.unit.test_pipeline import REAL_HOSPITAL

BLOCKED = "site blocks server fetches (403)"
NO_POLICY = "scout found no official policy"
NEW_HOSPITAL = {**REAL_HOSPITAL, "ccn": "220010", "name": "NEW HOSPITAL"}


@pytest.fixture
def workdir(monkeypatch, tmp_path):
    """A temporary working folder with a local database holding Real General (on the skip list)
    and New Hospital (not), and a skip list that also names a Puerto Rico hospital the registry
    does not have; no .env and no model or Tavily key."""
    monkeypatch.chdir(tmp_path)
    url = f"sqlite:///{tmp_path / 'local.db'}"
    monkeypatch.setenv("WAIVE_DATABASE_URL", url)
    monkeypatch.setenv("WAIVE_LEDGER_PATH", str(tmp_path / "usage.jsonl"))
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    skip = tmp_path / "skip.json"
    skip.write_text(
        json.dumps(
            [
                {
                    "ccn": "220031",
                    "state": "MA",
                    "name": "REAL GENERAL HOSPITAL",
                    "reason": BLOCKED,
                    "urls_seen": [],
                    "last_tried": "2026-10-23",
                },
                {
                    "ccn": "400001",
                    "state": "PR",
                    "name": "HOSPITAL SAN JUAN",
                    "reason": NO_POLICY,
                    "urls_seen": ["https://www.example.org/fap"],
                    "last_tried": "2026-10-23",
                },
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WAIVE_SCOUT_SKIP_LIST", str(skip))
    engine = make_engine(url)
    init_db(engine)
    with session_scope(engine) as session:
        repo.upsert_hospital(session, REAL_HOSPITAL)
        repo.upsert_hospital(session, NEW_HOSPITAL)
    yield engine
    engine.dispose()


def keys(monkeypatch):
    """Dummy keys, so the command can make its clients; nothing here reaches the network."""
    monkeypatch.setenv("NEBIUS_API_KEY", "not-a-real-key")
    monkeypatch.setenv("TAVILY_API_KEY", "not-a-real-key")


def run(*args: str) -> str:
    result = CliRunner().invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return " ".join(result.output.split())  # Rich wraps long lines at 80 columns


def must_not_run(*args, **kwargs):
    raise AssertionError("a hospital on the skip list was sent to the pipeline")


def test_atlas_build_refuses_a_hospital_on_the_skip_list_and_builds_nothing(workdir, monkeypatch):
    # No keys are set: the refusal comes before any client is made, so it costs nothing and works
    # in a batch of `--state XX --ccn NNNNNN --reuse-sources` commands that continues past it.
    monkeypatch.setattr("waive.cli.build_hospital", must_not_run)
    monkeypatch.setattr("waive.cli.build_state", must_not_run)
    text = run("atlas", "build", "--state", "MA", "--ccn", "220031", "--reuse-sources")
    assert (
        "220031 is on the skip list (eve-skip: site blocks server fetches (403)); not built" in text
    )
    assert "Spend so far" not in text
    with session_scope(workdir) as session:
        assert repo.latest_sheet(session, "220031") is None


def test_atlas_build_still_builds_a_hospital_that_is_not_on_the_skip_list(workdir, monkeypatch):
    keys(monkeypatch)
    built = []

    def fake_build_hospital(session, gateway, ai, ccn, today, **kwargs):
        built.append(ccn)
        return BuildResult(ccn, "NEW HOSPITAL", "published", version=1)

    monkeypatch.setattr("waive.cli.build_hospital", fake_build_hospital)
    text = run("atlas", "build", "--state", "MA", "--ccn", "220010")
    assert built == ["220010"]
    assert "on the skip list" not in text and "published" in text


def test_atlas_build_hands_the_skip_list_to_the_state_batch_and_says_how_many_were_left_out(
    workdir, monkeypatch
):
    keys(monkeypatch)
    seen = {}

    def fake_build_state(session, gateway, ai, state, today, **kwargs):
        seen.update(kwargs)
        return [BuildResult("220010", "NEW HOSPITAL", "published", version=1)]

    monkeypatch.setattr("waive.cli.build_state", fake_build_state)
    text = run("atlas", "build", "--state", "ma")
    # Two hospitals are on the list; one of them is in Massachusetts and in the registry.
    assert sorted(seen["skip"]) == ["220031", "400001"]
    assert seen["skip"]["220031"].reason == BLOCKED
    assert "Skip list: 1 hospitals in MA left out" in text
    # A state with no hospital on the list says nothing about it.
    assert "Skip list" not in run("atlas", "build", "--state", "RI")


def test_schedule_dry_run_leaves_the_skip_list_out_and_says_how_many(workdir):
    text = run("atlas", "schedule", "--dry-run")
    assert "Queue: 1 hospitals in MA (1 without a sheet); skipped 1 (1 on the skip list)" in text
    # The table has the unlisted hospital's row and not the listed one's (CCNs do not wrap).
    assert "220010" in text and "220031" not in text


def test_schedule_run_hands_the_skip_list_to_the_run(workdir, monkeypatch):
    keys(monkeypatch)
    seen = {}

    def fake_run_once(session, gateway, ai, governor, today, **kwargs):
        seen.update(kwargs)
        return RunReport(today, kwargs["daily_cap"], Decimal("0"), Decimal("0"))

    monkeypatch.setattr("waive.cli.run_once", fake_run_once)
    text = run("atlas", "schedule", "--run", "--limit", "1")
    assert sorted(seen["skip"]) == ["220031", "400001"]
    assert "Stopped: queue empty" in text


def import_eve(tmp_path, *extra: str) -> str:
    drafts = tmp_path / "drafts"
    drafts.mkdir(exist_ok=True)
    return run(
        "atlas",
        "import-eve",
        "--dir",
        str(drafts),
        "--out",
        str(tmp_path / "out"),
        "--skip",
        str(tmp_path / "skip.json"),
        *extra,
    )


def test_import_eve_files_the_skip_items_with_db_and_a_rerun_files_none(workdir, tmp_path):
    # Without --db nothing is stored, so nothing is filed.
    text = import_eve(tmp_path)
    assert "eve-skip" not in text
    with session_scope(workdir) as session:
        assert repo.open_review_items(session, kind=EVE_SKIP) == []
    text = import_eve(tmp_path, "--db")
    assert "filed 2 eve-skip review items for the 2 hospitals on the skip list" in text
    with session_scope(workdir) as session:
        items = repo.open_review_items(session, kind=EVE_SKIP)
        assert [(item.ccn, item.detail["reason"]) for item in items] == [
            ("220031", BLOCKED),
            ("400001", NO_POLICY),
        ]
        assert items[1].detail["urls_seen"] == ["https://www.example.org/fap"]
    again = import_eve(tmp_path, "--db")
    assert "filed 0 eve-skip review items for the 2 hospitals on the skip list" in again
    with session_scope(workdir) as session:
        assert len(repo.open_review_items(session, kind=EVE_SKIP)) == 2


def test_import_eve_without_a_skip_list_file_files_nothing(workdir, tmp_path):
    # Without --skip the command looks for data/atlas-eve/_skip.json; run from the temporary
    # folder it is not there, which is an empty list, not an error.
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    text = run("atlas", "import-eve", "--dir", str(drafts), "--out", str(tmp_path / "out"), "--db")
    assert "filed 0 eve-skip review items for the 0 hospitals on the skip list" in text
