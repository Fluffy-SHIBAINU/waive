"""scripts/audit_rules_check.py: the dry recheck of the verifier's rules on a copy of the atlas,
which lists what `waive atlas recheck` and `--withdraw` would do without touching the atlas."""

import importlib.util
import os
from pathlib import Path

import pytest

from waive.atlas import repo
from waive.atlas.discover import DOMAIN_UNCONFIRMED_REASON
from waive.atlas.pipeline import build_hospital
from waive.atlas.schema import Cited
from waive.atlas.tavily_gateway import SearchHit
from waive.atlas.verify import ASSET_REASON
from waive.db import init_db, make_engine, session_scope

from tests.unit.test_pipeline import FREE_CARE_QUOTE, REAL_HOSPITAL, TODAY, FakeAI, FakeGateway

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_rules_check.py"


def load_script():
    spec = importlib.util.spec_from_file_location("audit_rules_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SiteGateway(FakeGateway):
    """FakeGateway's hospital site and policy on another domain, so the hospitals here are not
    siblings on one website."""

    def __init__(self, domain: str):
        self.domain = domain

    def search(self, query, **kwargs):
        return [
            SearchHit(
                hit.url.replace("example.org", self.domain), hit.title, hit.content, hit.score
            )
            for hit in super().search(query, **kwargs)
        ]


@pytest.fixture
def atlas(tmp_path, monkeypatch):
    """A file database with three published Boston sheets: 220031 keeps an asset test its quote
    does not support, 220032 is clean, and 220033's website was matched with low confidence."""
    monkeypatch.delenv("WAIVE_DATABASE_URL", raising=False)
    path = tmp_path / "atlas.db"
    engine = make_engine(f"sqlite:///{path}")
    init_db(engine)
    with session_scope(engine) as session:
        for ccn, domain in (("220031", "example.org"), ("220032", "second.org")):
            repo.upsert_hospital(session, {**REAL_HOSPITAL, "ccn": ccn})
            result = build_hospital(session, SiteGateway(domain), FakeAI(), ccn, TODAY)
            assert result.outcome == "published", result.notes
        repo.upsert_hospital(session, {**REAL_HOSPITAL, "ccn": "220033"})
        result = build_hospital(session, SiteGateway("third.org"), FakeAI(), "220033", TODAY)
        assert result.outcome == "published", result.notes
        repo.get_hospital(session, "220033").domain_confidence = 0.6
        sheet, _ = repo.latest_sheet(session, "220031")
        asset_test = Cited(
            value=False,
            quote=FREE_CARE_QUOTE,
            source_id=sheet.eligibility.free_care_max_fpl.source_id,
            checked_on=TODAY,
        )
        stale = sheet.model_copy(
            update={
                "eligibility": sheet.eligibility.model_copy(update={"asset_test": asset_test}),
                "version": 2,
            }
        )
        repo.add_sheet_version(session, stale, {"eligibility.asset_test": {"old": None}})
    engine.dispose()
    return path


def snapshot(path: Path) -> tuple[bytes, int]:
    return path.read_bytes(), path.stat().st_mtime_ns


def test_lists_what_recheck_and_withdraw_would_do_without_touching_the_atlas(atlas, capsys):
    before = snapshot(atlas)
    assert load_script().main(["--db", str(atlas)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"MA 220031 eligibility.asset_test: {ASSET_REASON}",
        f"MA 220033 sheet: {DOMAIN_UNCONFIRMED_REASON}: third.org (confidence 0.6)",
        "MA 220033 held",
        "2 sheets listed, 1 field dropped, 1 sheet held, 0 kept as the owner ruled",
    ]
    assert snapshot(atlas) == before
    assert "WAIVE_DATABASE_URL" not in os.environ
    # The copy's withdraw never reached the atlas: its sheets are as they were.
    engine = make_engine(f"sqlite:///{atlas}")
    with session_scope(engine) as session:
        sheet, _ = repo.latest_sheet(session, "220031")
        assert (sheet.version, sheet.eligibility.asset_test.value) == (2, False)
        assert repo.latest_sheet(session, "220033")[0].status.value == "published"
    engine.dispose()


def test_writes_to_out_and_refuses_a_missing_atlas(atlas, tmp_path, capsys):
    script = load_script()
    out = tmp_path / "check.txt"
    assert script.main(["--db", str(atlas), "--out", str(out)]) == 0
    assert capsys.readouterr().out == ""
    assert out.read_text().splitlines()[-1].startswith("2 sheets listed")
    assert script.main(["--db", str(tmp_path / "missing.db")]) == 2
    assert "no atlas at" in capsys.readouterr().err
    assert not (tmp_path / "missing.db").exists()


def test_differences_show_only_the_lines_one_side_has():
    script = load_script()
    ours = script.Check(
        lines=[
            ("MA", "220031", "eligibility.asset_test: reason"),
            ("MD", "210003", "eligibility.free_care_max_fpl: other benchmark"),
            ("MD", "210003", "held"),
        ],
        fields=2,
        held=1,
        kept=0,
    )
    theirs = script.Check(
        lines=[
            ("MA", "220031", "eligibility.asset_test: reason"),
            ("CT", "070011", "eligibility.discount_tiers: old reason"),
        ],
        fields=2,
        held=0,
        kept=0,
    )
    assert script.differences(ours, theirs, "main") == [
        "- CT 070011 eligibility.discount_tiers: old reason",
        "+ MD 210003 eligibility.free_care_max_fpl: other benchmark",
        "+ MD 210003 held",
        "this code: 2 sheets listed, 2 fields dropped, 1 sheet held, 0 kept as the owner ruled",
        "main: 2 sheets listed, 2 fields dropped, 0 sheets held, 0 kept as the owner ruled",
        "3 lines differ on 2 sheets (2 only with this code, 1 only with main)",
    ]
