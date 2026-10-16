"""`waive ledger ...` on the command line: counts printed, nothing spent, hand-set EINs."""

from dataclasses import dataclass, field

import pytest
from typer.testing import CliRunner

from waive.atlas import repo as atlas_repo
from waive.cli import app
from waive.db import init_db, make_engine, session_scope
from waive.ledger import cli as ledger_cli
from waive.ledger import repo

from tests.unit.test_ledger_repo import MA, NJ


@pytest.fixture
def local(monkeypatch, tmp_path):
    """A local database with two registry hospitals, the ledger directory under tmp, no .env."""
    monkeypatch.chdir(tmp_path)
    url = f"sqlite:///{tmp_path / 'local.db'}"
    monkeypatch.setenv("WAIVE_DATABASE_URL", url)
    monkeypatch.setenv("WAIVE_LEDGER_DIR", str(tmp_path / "ledger"))
    engine = make_engine(url)
    init_db(engine)
    with session_scope(engine) as session:
        atlas_repo.upsert_hospital(session, MA)
        atlas_repo.upsert_hospital(session, NJ)
    yield engine
    engine.dispose()


def run(*args):
    result = CliRunner().invoke(app, ["ledger", *args])
    assert result.exit_code == 0, result.output
    return " ".join(result.output.replace("│", " ").split())  # Rich draws tables with boxes


def test_set_writes_a_manual_ein_without_downloading(local):
    text = run("crosswalk", "--set", "220001=04-2103606")
    assert "220001: EIN 042103606 set by hand" in text
    with session_scope(local) as session:
        row = repo.get_hospital_id(session, "220001")
        assert (row.ein, row.match_method, row.match_confidence) == ("042103606", "manual", 1.0)


@dataclass
class FakeCrosswalk:
    state: str
    hospitals: int = 2
    matched: int = 1
    unmatched: int = 1
    by_method: dict = field(default_factory=lambda: {"cbi": 1})
    unmatched_rows: list = field(default_factory=lambda: [{"ccn": "220001", "name": "X"}])


def test_crosswalk_prints_counts_per_state_and_writes_the_unmatched_file(local, monkeypatch):
    calls = []

    def fake_run(session, http, state, cache_dir, catalog, today, refresh=False):
        calls.append((state, cache_dir, refresh))
        return FakeCrosswalk(state)

    monkeypatch.setattr(ledger_cli, "cms_catalog", lambda http, cache_dir, refresh=False: [])
    monkeypatch.setattr(ledger_cli, "run_crosswalk", fake_run)
    written = []
    monkeypatch.setattr(
        ledger_cli, "write_unmatched", lambda report, path: written.append(path) or path
    )
    text = run("crosswalk", "--states", "ma,nj", "--refresh")
    assert [c[0] for c in calls] == ["MA", "NJ"] and all(c[2] for c in calls)
    assert "MA 2 1 1 1 0 0 0 0" in text and "NJ 2 1 1" in text
    assert [p.name for p in written] == ["unmatched-MA.csv", "unmatched-NJ.csv"]


def test_show_prints_the_cards_and_the_json(local):
    with session_scope(local) as session:
        repo.upsert_hospital_id(session, "220001", ein="042103606", match_method="cbi")
        repo.upsert_metrics(session, "220001", exposure_rank=0.9, exposure_label="high")
    text = run("show", "--ccn", "220001")
    assert "Seniors at risk here" in text and '"ein": "042103606"' in text
    result = CliRunner().invoke(app, ["ledger", "show", "--ccn", "000000"])
    assert result.exit_code == 1 and "no ledger data" in result.output
