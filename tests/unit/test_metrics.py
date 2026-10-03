import base64
from datetime import date, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from pydantic import SecretStr

from waive.atlas import repo
from waive.atlas.metrics import (
    RUN_LOG_HEADING,
    atlas_metrics,
    metrics_json,
    national_report,
    write_national_report,
)
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.atlas.schema import SheetStatus, SourceDoc, SourceKind
from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.config import Settings
from waive.db import init_db, make_engine, session_scope
from waive.governor import Governor, Ledger, UsageEvent
from waive.web.app import create_app

from tests.unit.test_pipeline import HOSPITAL, REAL_HOSPITAL

TODAY = date(2026, 10, 12)
EMPIRE = {**HOSPITAL, "ccn": "330010", "state": "NY", "name": "EMPIRE HOSPITAL", "city": "ALBANY"}
GREEN = {
    **HOSPITAL,
    "ccn": "470003",
    "state": "VT",
    "name": "GREEN MOUNTAIN HOSPITAL",
    "city": "RUTLAND",
}


def sheet_for(hospital, status=SheetStatus.PUBLISHED):
    sample = st_example_sheet()
    ref = sample.hospital.model_copy(
        update={"ccn": hospital["ccn"], "name": hospital["name"], "state": hospital["state"]}
    )
    return sample.model_copy(update={"hospital": ref, "status": status})


def seed(engine):
    """MA: the demo hospital (hidden) and REAL (published, document 10 days old);
    NY: held; VT: no sheet."""
    with session_scope(engine) as session:
        for hospital in (HOSPITAL, REAL_HOSPITAL, EMPIRE, GREEN):
            repo.upsert_hospital(session, hospital)
        publish_sheet(session, st_example_sheet())
        publish_sheet(session, sheet_for(REAL_HOSPITAL))
        publish_sheet(session, sheet_for(EMPIRE, SheetStatus.HELD))
        repo.save_source(
            session,
            SourceDoc(
                id="fap-real",
                kind=SourceKind.HOSPITAL_WEB,
                url="https://www.realgeneral.org/fap.pdf",
                title="FAP",
                fetched_on=TODAY - timedelta(days=10),
                sha256="a" * 64,
            ),
            "text",
            "220031",
        )
        repo.add_review_item(
            session, "330010", "conflict", {"paths": ["eligibility.discount_tiers"]}
        )


def ledger_with_spend(tmp_path):
    ledger = Ledger(tmp_path / "usage.jsonl")
    ledger.record(
        UsageEvent("tavily", Decimal("3"), Decimal("0"), "atlas.scout", "2026-10-11T10:00:00+00:00")
    )
    ledger.record(
        UsageEvent(
            "tavily", Decimal("7"), Decimal("0"), "atlas.refresh", "2026-10-12T09:00:00+00:00"
        )
    )
    ledger.record(
        UsageEvent("tavily", Decimal("99"), Decimal("0"), "old", "2026-09-01T09:00:00+00:00")
    )
    return ledger


def test_atlas_metrics_counts_states_ages_and_credits(tmp_path):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    seed(engine)
    governor = Governor(ledger_with_spend(tmp_path), 1000, Decimal("15"))
    with session_scope(engine) as session:
        m = atlas_metrics(session, TODAY, governor, daily_cap=50)
    assert (m.hospitals, m.published, m.held, m.none) == (3, 1, 1, 1)
    assert [(s.state, s.hospitals, s.published, s.held, s.none) for s in m.states] == [
        ("MA", 1, 1, 0, 0),
        ("NY", 1, 0, 1, 0),
        ("VT", 1, 0, 0, 1),
    ]
    assert m.states[0].share == 1.0 and m.states[2].share == 0.0
    assert m.median_sheet_age_days == 10
    assert m.documented_share == 0.83  # the sample sheet has 5 of the 6 core fields
    assert (m.accuracy, m.scored_outcomes, m.review_open) == (None, 0, 1)
    assert len(m.credits_by_day) == 7
    assert m.credits_by_day[0] == ("2026-10-06", Decimal("0"))
    assert m.credits_by_day[-2:] == [("2026-10-11", Decimal("3")), ("2026-10-12", Decimal("7"))]
    assert (m.credits_today, m.daily_cap) == (Decimal("7"), 50)
    payload = metrics_json(m)
    assert payload["hospitals"] == 3 and payload["states"][1]["state"] == "NY"
    assert payload["credits_by_day"][-1] == {"day": "2026-10-12", "credits": "7"}
    assert payload["credits_today"] == "7" and payload["today"] == "2026-10-12"


def test_atlas_metrics_without_a_governor_or_data():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        m = atlas_metrics(session, TODAY)
    assert (m.hospitals, m.states, m.median_sheet_age_days, m.documented_share) == (
        0,
        [],
        None,
        None,
    )
    assert m.credits_by_day == [] and m.credits_today == Decimal("0")


def test_national_report_and_run_log_are_preserved_across_regeneration(tmp_path):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    seed(engine)
    with session_scope(engine) as session:
        text = national_report(atlas_metrics(session, TODAY, daily_cap=50))
    assert text.startswith("# Atlas coverage — national\n")
    assert "Hospitals in registry: 3" in text and "Published sheets: 1 (33%)" in text
    assert "| NY | 1 | 0 | 1 | 0 | 0% |" in text and "| MA | 1 | 1 | 0 | 0 | 100% |" in text
    assert "Median sheet age: 10 days" in text
    path = tmp_path / "atlas-national.md"
    write_national_report(path, text)
    first = path.read_text()
    assert first.count(RUN_LOG_HEADING) == 1 and first.rstrip().endswith("|---|")
    path.write_text(first + "| 2026-10-12 | population #1 | 10 | 6 | 4 | 389 → 441 | CA |\n")
    write_national_report(
        path, text.replace("Hospitals in registry: 3", "Hospitals in registry: 4")
    )
    second = path.read_text()
    assert "Hospitals in registry: 4" in second and "Hospitals in registry: 3" not in second
    assert "| 2026-10-12 | population #1 |" in second and second.count(RUN_LOG_HEADING) == 1


def test_metrics_pages(tmp_path):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    seed(engine)
    ledger_with_spend(tmp_path)
    settings = Settings(
        _env_file=None,
        nebius_api_key=SecretStr("k"),
        ledger_path=tmp_path / "usage.jsonl",
        scout_daily_credits=50,
    )
    app = create_app(
        settings,
        engine=engine,
        cipher=FieldCipher(base64.b64decode(new_key())),
        signer=TokenSigner("x" * 40),
        today_fn=lambda: TODAY,
    )
    client = TestClient(app)
    page = client.get("/metrics")
    assert page.status_code == 200
    assert "Hospitals in registry" in page.text and "<td>NY</td>" in page.text
    assert "10 days" in page.text and "7 of 50" in page.text
    assert "229999" not in page.text and "ST. EXAMPLE" not in page.text
    data = client.get("/metrics.json").json()
    assert data["published"] == 1 and data["credits_today"] == "7"
