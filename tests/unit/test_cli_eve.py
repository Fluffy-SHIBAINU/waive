"""`waive atlas eve-websites` and the owner's rulings handed to `waive atlas build` (task 7.12c).

Every test runs in a temporary folder with its own database and its own decisions file, so the
real database and data/atlas-eve/_decisions.json are never read, and no command here makes a
paid call."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from waive.atlas import repo
from waive.atlas.pipeline import BuildResult
from waive.cli import app
from waive.config import Settings
from waive.db import init_db, make_engine, session_scope

from tests.unit.test_eve_import import GUESS_DOCS, RHODE_DOCS, SURE_DOCS, eve_hospital

DECISIONS = [
    {
        "ccn": "220010",
        "decision": "discount",
        "value": "300",
        "note": "",
        "at": "2026-10-23T17:31:54.394Z",
        "hospital": "NEW HOSPITAL",
        "state": "MA",
        "bucket": "hedged",
    },
    {
        "ccn": "220045",
        "decision": "unsure",
        "value": "200",
        "note": "cannot tell",
        "at": "2026-10-23T17:35:00.000Z",
        "hospital": "OTHER HOSPITAL",
        "state": "MA",
        "bucket": "hedged",
    },
]


@pytest.fixture
def workdir(monkeypatch, tmp_path):
    """A temporary working folder with an empty local database and its own decisions file (not
    yet written); no .env, no skip list and no model or Tavily key."""
    monkeypatch.chdir(tmp_path)
    url = f"sqlite:///{tmp_path / 'local.db'}"
    monkeypatch.setenv("WAIVE_DATABASE_URL", url)
    monkeypatch.setenv("WAIVE_LEDGER_PATH", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("WAIVE_SCOUT_SKIP_LIST", str(tmp_path / "no-skip-list.json"))
    monkeypatch.setenv("WAIVE_EVE_DECISIONS", str(tmp_path / "decisions.json"))
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    engine = make_engine(url)
    init_db(engine)
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


def write_decisions(tmp_path: Path) -> None:
    (tmp_path / "decisions.json").write_text(json.dumps(DECISIONS), encoding="utf-8")


def counts_line(set_for, sure, unsure, already, no_domain, refused) -> str:
    return (
        f"websites set for {set_for} Eve hospitals: {sure} sure (0.9), {unsure} unconfirmed "
        f"(0.6); {already} already had one, {no_domain} without one in the draft, "
        f"{refused} refused (directory or file host)"
    )


def test_eve_websites_sets_the_websites_and_prints_the_counts(workdir):
    with session_scope(workdir) as session:
        eve_hospital(
            session,
            "220031",
            "REAL GENERAL HOSPITAL",
            drafted="www.realgeneral.org",
            documents=SURE_DOCS,
        )
        eve_hospital(
            session,
            "220010",
            "CAPE VIEW MEDICAL CENTER",
            city="FALMOUTH",
            drafted="capeview.org",
            documents=GUESS_DOCS,
        )
        eve_hospital(session, "220050", "NO DOMAIN HOSPITAL", drafted=None)
    assert run("atlas", "eve-websites") == counts_line(2, 1, 1, 0, 1, 0)
    # Committed: a new session sees the websites.
    with session_scope(workdir) as session:
        rows = {
            row.ccn: (row.website_domain, row.domain_confidence)
            for row in repo.list_hospitals(session)
        }
    assert rows == {
        "220031": ("realgeneral.org", 0.9),
        "220010": ("capeview.org", 0.6),
        "220050": (None, None),
    }
    # A rerun sets nothing: both websites are now there.
    assert run("atlas", "eve-websites") == counts_line(0, 0, 0, 2, 1, 0)


def test_eve_websites_with_a_state_leaves_the_other_states_alone(workdir):
    with session_scope(workdir) as session:
        eve_hospital(
            session,
            "220031",
            "REAL GENERAL HOSPITAL",
            drafted="realgeneral.org",
            documents=SURE_DOCS,
        )
        eve_hospital(
            session,
            "410001",
            "BAYSIDE COMMUNITY HOSPITAL",
            city="NEWPORT",
            state="RI",
            drafted="baysidehealth.org",
            documents=RHODE_DOCS,
        )
    assert run("atlas", "eve-websites", "--state", "ri") == counts_line(1, 1, 0, 0, 0, 0)
    with session_scope(workdir) as session:
        assert repo.get_hospital(session, "410001").website_domain == "baysidehealth.org"
        assert repo.get_hospital(session, "220031").website_domain is None
    assert run("atlas", "eve-websites", "--state", "MA") == counts_line(1, 1, 0, 0, 0, 0)
    assert run("atlas", "eve-websites") == counts_line(0, 0, 0, 2, 0, 0)


def test_atlas_build_hands_a_hospital_the_ruling_for_its_ccn(workdir, tmp_path, monkeypatch):
    keys(monkeypatch)
    write_decisions(tmp_path)
    seen = {}

    def fake_build_hospital(session, gateway, ai, ccn, today, **kwargs):
        seen[ccn] = kwargs
        return BuildResult(ccn, "NEW HOSPITAL", "published", version=1)

    monkeypatch.setattr("waive.cli.build_hospital", fake_build_hospital)
    run("atlas", "build", "--state", "MA", "--ccn", "220010", "--reuse-sources")
    run("atlas", "build", "--state", "MA", "--ccn", "220099", "--reuse-sources")
    # The decision is the whole entry the review page wrote for that CCN, or None.
    assert seen["220010"].get("decision") == DECISIONS[0]
    assert seen["220010"]["reuse_sources"] is True
    assert "decision" in seen["220099"] and seen["220099"]["decision"] is None


def test_atlas_build_hands_the_state_batch_every_ruling(workdir, tmp_path, monkeypatch):
    keys(monkeypatch)
    write_decisions(tmp_path)
    seen = {}

    def fake_build_state(session, gateway, ai, state, today, **kwargs):
        seen.update(kwargs)
        return [BuildResult("220010", "NEW HOSPITAL", "published", version=1)]

    monkeypatch.setattr("waive.cli.build_state", fake_build_state)
    run("atlas", "build", "--state", "MA", "--rebuild", "--reuse-sources")
    assert seen.get("decisions") == {"220010": DECISIONS[0], "220045": DECISIONS[1]}


def test_a_missing_decisions_file_means_no_decisions(workdir, monkeypatch):
    # The container image carries no data/ folder; there nothing is ruled on.
    keys(monkeypatch)
    seen = {}

    def fake_build_hospital(session, gateway, ai, ccn, today, **kwargs):
        seen["hospital"] = kwargs
        return BuildResult(ccn, "NEW HOSPITAL", "published", version=1)

    def fake_build_state(session, gateway, ai, state, today, **kwargs):
        seen["state"] = kwargs
        return []

    monkeypatch.setattr("waive.cli.build_hospital", fake_build_hospital)
    monkeypatch.setattr("waive.cli.build_state", fake_build_state)
    run("atlas", "build", "--state", "MA", "--ccn", "220010")
    run("atlas", "build", "--state", "MA")
    assert "decision" in seen["hospital"] and seen["hospital"]["decision"] is None
    assert seen["state"].get("decisions") == {}


def test_a_decisions_file_that_cannot_be_read_stops_the_run_before_any_build(
    workdir, tmp_path, monkeypatch
):
    # A damaged file must not quietly mean "no rulings": the rebuild would bring back the value
    # the owner ruled out.
    keys(monkeypatch)
    (tmp_path / "decisions.json").write_text('[{"ccn": "220010"', encoding="utf-8")

    def must_not_run(*args, **kwargs):
        raise AssertionError("a build started although the owner's rulings could not be read")

    monkeypatch.setattr("waive.cli.build_hospital", must_not_run)
    monkeypatch.setattr("waive.cli.build_state", must_not_run)
    for extra in (["--ccn", "220010"], []):
        result = CliRunner().invoke(app, ["atlas", "build", "--state", "MA", *extra])
        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)  # json.JSONDecodeError


def test_import_eve_with_db_records_the_owner_rulings_and_a_rerun_records_none(workdir, tmp_path):
    # The server has no data/ folder, so the rulings go into the database with the hospitals:
    # one resolved `owner_ruling` item each, for hospitals with a sheet and without.
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    write_decisions(tmp_path)
    file = tmp_path / "decisions.json"
    args = ["atlas", "import-eve", "--dir", str(drafts), "--out", str(tmp_path / "out")]
    args += ["--db", "--decisions", str(file)]
    assert f"recorded 2 owner rulings from {file}" in run(*args)
    with session_scope(workdir) as session:
        for entry in DECISIONS:
            [item] = repo.review_items_of_kind(session, entry["ccn"], "owner_ruling")
            assert item.status == "resolved"
            assert item.detail == {key: value for key, value in entry.items() if key != "ccn"}
        # Settled decisions are not work for the admin queue or the public atlas.
        assert repo.open_review_items(session) == []
    assert f"recorded 0 owner rulings from {file}" in run(*args)
    with session_scope(workdir) as session:
        assert len(repo.review_items_of_kind(session, "220010", "owner_ruling")) == 1
    # The owner decides again: the new time is a new ruling, recorded beside the old one.
    changed = {**DECISIONS[0], "decision": "accept", "at": "2026-10-24T09:00:00.000Z"}
    file.write_text(json.dumps([changed]), encoding="utf-8")
    assert f"recorded 1 owner rulings from {file}" in run(*args)
    with session_scope(workdir) as session:
        items = repo.review_items_of_kind(session, "220010", "owner_ruling")
        assert [item.detail["decision"] for item in items] == ["discount", "accept"]


def test_import_eve_without_db_records_nothing(workdir, tmp_path):
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    write_decisions(tmp_path)
    args = ["atlas", "import-eve", "--dir", str(drafts), "--out", str(tmp_path / "out")]
    args += ["--decisions", str(tmp_path / "decisions.json")]
    assert "recorded" not in run(*args)
    with session_scope(workdir) as session:
        assert repo.review_items_of_kind(session, "220010", "owner_ruling") == []


def test_the_decisions_path_is_a_setting(monkeypatch, tmp_path):
    monkeypatch.delenv("WAIVE_EVE_DECISIONS", raising=False)
    assert Settings(_env_file=None).eve_decisions == Path("data/atlas-eve/_decisions.json")
    monkeypatch.setenv("WAIVE_EVE_DECISIONS", str(tmp_path / "elsewhere.json"))
    assert Settings(_env_file=None).eve_decisions == tmp_path / "elsewhere.json"
