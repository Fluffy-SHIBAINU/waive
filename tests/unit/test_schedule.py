import base64
from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from pydantic import SecretStr

from waive.atlas import repo
from waive.atlas.schedule import (
    DEMAND_WEIGHTS,
    JOB_ID,
    NEVER_SCOUTED_DAYS,
    RETRY_AFTER_DAYS,
    build_queue,
    make_scheduler,
    priority_score,
    run_once,
    scheduler_states,
    scout_tick,
)
from waive.atlas.schema import SourceDoc, SourceKind
from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.config import Settings
from waive.db import CaseRow, init_db, make_engine, session_scope
from waive.governor import Governor, Ledger
from waive.web.app import create_app

from tests.unit.test_pipeline import (
    HOSPITAL,
    REAL_HOSPITAL,
    TODAY,
    FakeAI,
    FakeGateway,
    make_engine_with_hospital,
)

# 229999 is the demo hospital and stays out of every queue, so the tests use real-looking CCNs.
REAL = {**REAL_HOSPITAL, "website_domain": "realgeneral.org"}
NEW = {**HOSPITAL, "ccn": "220010", "name": "NEW HOSPITAL", "city": "QUINCY"}
THIRD = {**HOSPITAL, "ccn": "220045", "name": "THIRD COMMUNITY HOSPITAL", "city": "SALEM"}
FOURTH = {**HOSPITAL, "ccn": "220050", "name": "FOURTH HOSPITAL", "city": "LYNN"}


def source(source_id, fetched_on, sha="a" * 64):
    return SourceDoc(
        id=source_id,
        kind=SourceKind.HOSPITAL_WEB,
        url=f"https://www.example.org/{source_id}.pdf",
        title="Financial Assistance Policy",
        fetched_on=fetched_on,
        sha256=sha,
    )


def test_priority_score_formula():
    assert priority_score(10, 9, 0.0) == 90.0
    assert priority_score(10, 9, 1.0) == 18.0  # the floor keeps accurate sheets on the rota
    assert priority_score(10, 9, None) == 45.0  # unknown accuracy counts as a coin flip
    assert priority_score(0, 0, None) == 0.5  # never below one day times one unit of demand


def test_build_queue_orders_by_staleness_demand_and_accuracy():
    engine = make_engine_with_hospital(REAL, NEW, THIRD, FOURTH)
    with session_scope(engine) as session:
        # REAL: scouted ten days ago; two open cases; a re-scout request counting three cases.
        repo.save_source(session, source("fap-real", TODAY - timedelta(days=10)), "text", "220031")
        for n in range(2):
            session.add(CaseRow(id=f"case{n}", state="MA", ccn="220031", status="evaluated"))
        session.add(
            CaseRow(id="done", state="MA", ccn="220031", status="evaluated", outcome={"matched": 1})
        )
        repo.add_review_item(
            session,
            "220031",
            "rescout_request",
            {"reason": "an outcome contradicts the sheet", "cases": ["a", "b", "c"], "count": 3},
        )
        # THIRD was scouted today; FOURTH failed this week.
        repo.save_source(session, source("fap-third", TODAY, sha="b" * 64), "text", "220045")
        repo.add_review_item(session, "220050", "no_documents", {"domain": None})
        # Two bills named hospitals we could not match at intake: one is REAL (by FAP address).
        repo.add_review_item(
            session,
            None,
            "scout_request",
            {
                "hospital_name": "Real General",
                "fap_url": "https://www.realgeneral.org/financial-assistance",
                "state": "MA",
            },
        )
        repo.add_review_item(
            session,
            None,
            "scout_request",
            {"hospital_name": "Nowhere Clinic", "fap_url": None, "state": "MA"},
        )
        session.flush()
        report = build_queue(session, TODAY, ("MA",))

    assert [entry.ccn for entry in report.entries] == ["220031", "220010"]
    real, new = report.entries
    assert (real.staleness_days, real.demand, real.priority) == (10, 11, 55.0)
    assert real.has_sources and real.rescout_requested
    assert "2 open case(s)" in real.reasons
    assert "rescout_request ×3" in real.reasons
    assert "1 bill(s) named this hospital" in real.reasons
    assert (new.staleness_days, new.demand, new.priority) == (NEVER_SCOUTED_DAYS, 1, 45.0)
    assert not new.has_sources and not new.rescout_requested and "never scouted" in new.reasons
    assert report.skipped == {
        "220045": "scouted today",
        "220050": f"recent failure; retried after {RETRY_AFTER_DAYS} days",
    }
    assert [r["hospital_name"] for r in report.unmatched_requests] == ["Nowhere Clinic"]
    assert report.without_sheet == 4


def test_a_merged_scout_request_counts_each_bill_once():
    engine = make_engine_with_hospital(REAL)
    with session_scope(engine) as session:
        repo.add_review_item(
            session,
            None,
            "scout_request",
            {
                "hospital_name": "Real General",
                "fap_url": "realgeneral.org",  # a bare domain, as the case service stores it
                "state": "MA",
                "cases": ["a", "b", "c"],
                "count": 3,
            },
        )
        [entry] = build_queue(session, TODAY, ("MA",)).entries
    assert "3 bill(s) named this hospital" in entry.reasons
    assert entry.demand == 1 + 3 * DEMAND_WEIGHTS["scout_request"]


def test_build_queue_filters_by_state_and_skips_the_demo_hospital():
    other_state = {**NEW, "ccn": "330010", "state": "NY", "name": "EMPIRE HOSPITAL"}
    engine = make_engine_with_hospital(HOSPITAL, NEW, other_state)
    with session_scope(engine) as session:
        assert [e.ccn for e in build_queue(session, TODAY, ("MA",)).entries] == ["220010"]
        assert [e.ccn for e in build_queue(session, TODAY).entries] == ["330010", "220010"]
        assert "229999" not in build_queue(session, TODAY).skipped


class SpendingGateway(FakeGateway):
    """The pipeline's fake, but every search books five credits, as a real scout roughly does."""

    def __init__(self, governor):
        self.governor = governor

    def search(self, query, **kwargs):
        self.governor.record_tavily(Decimal("5"), "atlas.scout")
        return super().search(query, **kwargs)


def make_governor_for(tmp_path, cap=1000):
    return Governor(Ledger(tmp_path / "usage.jsonl"), cap, Decimal("15"))


def test_run_once_stops_at_the_daily_budget(tmp_path):
    engine = make_engine_with_hospital(REAL, NEW, THIRD)
    governor = make_governor_for(tmp_path)
    with session_scope(engine) as session:
        report = run_once(
            session, SpendingGateway(governor), FakeAI(), governor, TODAY, daily_cap=12
        )
        # NEW comes first (equal priority, alphabetical); it has no domain yet, so the build runs
        # three searches (discovery + two scouting queries) = 15 credits, past the 12-credit day.
        assert [(r.ccn, r.outcome) for r in report.results] == [("220010", "published")]
        assert report.stopped == "daily budget"
        assert (report.used_before, report.used_after) == (Decimal("0"), Decimal("15"))
        assert repo.latest_sheet(session, "220031") is None


def test_run_once_builds_the_queue_notes_rescouts_and_stops_when_empty(tmp_path):
    engine = make_engine_with_hospital(REAL, NEW)
    governor = make_governor_for(tmp_path)
    with session_scope(engine) as session:
        repo.save_source(session, source("fap-real", TODAY - timedelta(days=10)), "text", "220031")
        item = repo.add_review_item(
            session, "220031", "rescout_request", {"reason": "x", "cases": ["a"], "count": 1}
        )
        report = run_once(
            session, SpendingGateway(governor), FakeAI(), governor, TODAY, daily_cap=100
        )
        assert sorted(r.ccn for r in report.results) == ["220010", "220031"]
        assert report.stopped == "queue empty"
        assert session.get(type(item), item.id).detail["rescouted_on"] == TODAY.isoformat()
        assert session.get(type(item), item.id).status == "open"  # the admin still gives a verdict
        # Everything was scouted today: a second run finds nothing to do and spends nothing.
        again = run_once(
            session, SpendingGateway(governor), FakeAI(), governor, TODAY, daily_cap=100
        )
        assert again.results == [] and again.used_before == again.used_after


def test_run_once_respects_limit_and_survives_a_failing_build(tmp_path):
    class BrokenAI(FakeAI):
        def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
            raise RuntimeError("model down")

    engine = make_engine_with_hospital(REAL, NEW, THIRD)
    governor = make_governor_for(tmp_path)
    with session_scope(engine) as session:
        report = run_once(
            session, SpendingGateway(governor), BrokenAI(), governor, TODAY, daily_cap=100, limit=2
        )
        assert [r.outcome for r in report.results] == ["failed", "failed"]
        assert report.results[0].notes == ["RuntimeError"]
        assert report.stopped == "limit reached"


def test_make_scheduler_configures_one_interval_job(tmp_path):
    settings = Settings(
        _env_file=None, scheduler="on", scheduler_interval_minutes=45, scheduler_states="MA, ri"
    )
    assert scheduler_states(settings) == ("MA", "RI")
    assert scheduler_states(Settings(_env_file=None, scheduler_states="")) == ()
    engine = make_engine("sqlite+pysqlite:///:memory:")
    scheduler = make_scheduler(engine, settings, make_governor_for(tmp_path), None)
    job = scheduler.get_job(JOB_ID)
    assert job is not None
    assert job.func is scout_tick
    assert job.trigger.interval == timedelta(minutes=45)
    assert job.max_instances == 1
    assert not scheduler.running


def test_scout_tick_without_keys_spends_nothing(tmp_path):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    governor = make_governor_for(tmp_path)
    assert scout_tick(engine, Settings(_env_file=None), governor, None) is None
    assert governor.summary()["tavily"] == (Decimal("0"), Decimal("0"))


def make_app(tmp_path, **overrides):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    settings = Settings(
        _env_file=None,
        nebius_api_key=SecretStr("k"),
        ledger_path=tmp_path / "usage.jsonl",
        **overrides,
    )
    return create_app(
        settings,
        engine=engine,
        cipher=FieldCipher(base64.b64decode(new_key())),
        signer=TokenSigner("x" * 40),
    )


def test_create_app_starts_the_scheduler_only_when_switched_on(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"ok": True}
        assert app.state.scheduler is None

    app = make_app(tmp_path, scheduler="on", scheduler_interval_minutes=60)
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"ok": True}
        assert app.state.scheduler.running
        assert app.state.scheduler.get_job(JOB_ID).trigger.interval == timedelta(hours=1)
    assert not app.state.scheduler.running


def test_run_once_refreshes_hospitals_with_documents_and_rescouts_on_request(tmp_path, monkeypatch):
    from waive.atlas.tavily_gateway import ExtractedPage

    from tests.unit.test_pipeline import POLICY_TEXT

    class Recording(SpendingGateway):
        extracted: list[list[str]] = []

        def extract(self, urls, **kwargs):
            Recording.extracted.append(list(urls))
            return [ExtractedPage(url, POLICY_TEXT) for url in urls]

    engine = make_engine_with_hospital(REAL, THIRD)
    governor = make_governor_for(tmp_path)
    with session_scope(engine) as session:
        # Build both once so they have a sheet and stored documents; THIRD then gets a request.
        first = run_once(session, Recording(governor), FakeAI(), governor, TODAY, daily_cap=100)
        assert sorted(r.ccn for r in first.results) == ["220031", "220045"]
        Recording.extracted.clear()
        repo.add_review_item(session, "220045", "rescout_request", {"cases": ["a"], "count": 1})
        later = TODAY + timedelta(days=5)
        # The ledger stamps rows with the real clock; move it to `later` so the day's budget
        # sees this run's own spend, as it does in production where both are the same day.
        monkeypatch.setattr("waive.governor._now", lambda: f"{later.isoformat()}T12:00:00+00:00")
        report = run_once(session, Recording(governor), FakeAI(), governor, later, daily_cap=100)
        by_ccn = {r.ccn: r for r in report.results}
        # REAL was refreshed: one Extract of its stored URL, no search, nothing changed.
        assert by_ccn["220031"].outcome == "skipped"
        assert by_ccn["220031"].notes == ["unchanged (1 documents checked)"]
        # THIRD was re-scouted in full because of the request (searches spent credits).
        assert by_ccn["220045"].outcome == "published"
        assert report.used_after - report.used_before == Decimal("10")
        [(source, _)] = repo.sources_for(session, "220031")
        assert source.fetched_on == later
