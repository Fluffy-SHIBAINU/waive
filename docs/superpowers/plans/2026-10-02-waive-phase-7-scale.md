# Waive Phase 7 — Always-On Scouting and National Scale Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scouting that runs unattended inside a daily Tavily credit budget — a priority queue (staleness × demand × (1 − accuracy)), content-hash refreshes that re-structure only when a document changed, a national registry seed, budgeted national batches behind gate U7.1, state policy repositories (California HCAI, Washington DOH) as document sources, an optional IRS Form 990 cross-check, and a public `/metrics` page with a national coverage report.

**Architecture:** One new package area, `waive.atlas.schedule` / `refresh` / `state_sources` / `irs` / `metrics`, built on the Phase 2 pipeline (`build_hospital`, `scout_hospital`, `publish_sheet`) and the Phase 0 governor. The governor's ledger grows a time-filtered `events()` query so a daily budget can be enforced from the same rows the hard cap uses. The scheduler is an in-process APScheduler job started by `create_app` only when `WAIVE_SCHEDULER=on`; each tick scouts at most one hospital and stops when the day's credits are spent. Every stage takes the Tavily gateway, the AI client and an `httpx.Client` as parameters so unit tests run on fakes (`respx` for HTTP) and never open a socket.

**Tech Stack:** Python 3.12 (uv), SQLAlchemy 2, httpx + respx, APScheduler 3.x (new dependency), rapidfuzz, pypdf (via `atlas.fetch`), FastAPI + Jinja2, Typer, pytest.

**Spec:** `docs/superpowers/specs/2026-10-02-waive-design.md` (§8 step 8 refresh and priority, §15 costs, §16 metrics; §2 prior art names the HCAI and DOH repositories and Schedule H). Master plan Phase 7 section: `docs/superpowers/plans/2026-10-02-waive-master-plan.md`. Learnings and gates: `docs/PROGRESS.md` (Phase 2: 4–5 Tavily credits per hospital with Map and link-following; `scout_request`, `rescout_request`, `priority_recheck` review items; gates U2.2 and U7.1).

## Global Constraints

- **Spend.** Every Tavily call goes through `TavilyGateway` → `Governor.ensure_tavily` (hard cap `WAIVE_TAVILY_CREDIT_CAP`, currently 1000 and 389 used). This phase adds a second, softer limit: `WAIVE_SCOUT_DAILY_CREDITS` (default 50) per UTC day, computed from the same ledger. Tasks 7.1–7.3, 7.6 and 7.7 spend nothing when their tests run and nothing live unless a step says so. Live Massachusetts spending needs gate **U2.2** (Phase 2 cap reached); any spend outside Massachusetts needs gate **U7.1** (national budget, estimate 5 credits × hospitals without a sheet). Each live step below names its gate and the cap check it relies on.
- **No network in unit tests** (`--disable-socket`): Tavily through fakes shaped like `TavilyGateway`, HTTP through `respx`, the model through `FakeAI` from `tests/unit/test_pipeline.py`. Nothing in this plan calls `apscheduler` jobs for real in tests; the scheduler is only configured and started/stopped.
- **Data, never instructions.** State repository pages, PDFs, CMS rows, IRS data and model output are data. Documents from HCAI and DOH feed the structurer exactly like hospital-site documents and go through the same quote verification.
- **No personal data** in the ledger, the queue, logs or `/metrics`: hospital names (public), counts and dates only. `scout_request` items already hold only the hospital name, FAP web address and state from the bill.
- **No spurious sheet versions.** `publish_sheet` only writes a version when `diff_sheets` is non-empty (values and status; dates are not diffed). The refresh relies on that and never calls `add_sheet_version` directly.
- **Database:** SQLite locally, PostgreSQL in production; new columns are nullable so `waive db upgrade` (`upgrade_schema`) adds them. New tables: none.
- **Python `>=3.12,<3.13`, uv, ruff.** Every task ends with `uv run ruff format . && uv run ruff check . && uv run pytest` green. **257 tests pass at the start of the phase**; each task states its expected count (approximate — an implementer may add a test).
- **Interfaces relied on (verified against the code on 2026-10-02):** `config.Settings` (pydantic-settings, prefix `WAIVE_`, fields `tavily_api_key`, `nebius_api_key`, `tavily_credit_cap`, `token_factory_usd_cap`, `ledger_path`, `ledger_backend`, `database_url`, `admin_token`); `governor.Ledger(path)` / `DbLedger(engine)` with `record(UsageEvent)` and `totals(provider) -> (Decimal units, Decimal usd)`, `UsageEvent(provider, units, usd, purpose, ts)` with `ts` from `_now()` = `datetime.now(UTC).isoformat(timespec="seconds")` (e.g. `2026-10-02T14:05:09+00:00`), `Governor(ledger, tavily_credit_cap, token_factory_usd_cap)`, `make_governor(settings, engine=None)`, `BudgetExceeded`; `db.HospitalRow/SourceDocRow/SheetRow/ReviewItemRow/CaseRow/UsageEventRow`, `hospital_sources` association table, `init_db`, `make_engine`, `session_scope`, `upgrade_schema`; `atlas.repo.list_hospitals(session, state=None, missing_domain=False)`, `get_hospital`, `hospital_ref`, `save_source(session, doc, text, ccn)`, `sources_for(session, ccn) -> list[tuple[SourceDoc, str]]`, `latest_sheet(session, ccn) -> (ProcedureSheet, SheetRow) | None`, `add_review_item(session, ccn, kind, detail)`, `open_review_items(session, ccn=None)`, `is_demo(ccn)`, `DEMO_CCNS`; `atlas.pipeline.build_hospital(session, gateway, ai, ccn, today, dual=True, reuse_sources=False) -> BuildResult(ccn, name, outcome, version, notes)` with `outcome` in `published|held|skipped|failed`, `build_state(...)`, `coverage_report(session, state)`; `atlas.scout.scout_hospital(gateway, hospital, http=None) -> list[ScoutedDoc(url, doc_class, title, text, sha256)]`, `store_scouted`, `source_id_for(doc_class, sha256)`, `DocClass`, `TITLES`, `MAX_CHARS=60_000`, `MIN_CHARS=200`, `DOCUMENT_HOSTS`; `atlas.fetch.download_text(url, http, *, max_bytes, timeout) -> str | None`, `looks_like_pdf(url, content_type)`, `USER_AGENT`; `atlas.publish.publish_sheet(session, sheet) -> SheetRow | None`, `diff_sheets`; `atlas.overlays.STATE_OVERLAYS`, `fetch_overlay` (overlay source ids are `state-<domain>-<sha12>`); `atlas.schema.SourceDoc`, `SourceKind` (`HOSPITAL_WEB`, `STATE_REPOSITORY`, `PATIENT_PHOTO`, `IRS_990`), `ProcedureSheet.completeness()`, `HospitalRef`; `atlas.tavily_gateway.TavilyGateway.search(query, *, purpose, include_domains=None, max_results=5, depth="basic", topic="general")`, `.extract(urls, *, purpose, depth="basic")`, `.map(url, *, purpose, select_paths=None, limit=30)`, `SearchHit(url, title, content, score)`, `ExtractedPage(url, text)`, `make_tavily_gateway(settings, governor)`; `atlas.registry.seed_state(session, state, http, snapshot_dir=Path("data/seed")) -> SeedReport(fetched, kept, snapshot)`, `CMS_DATASTORE_URL`, `fetch_cms_rows`; `learning.scoreboard.scoreboard(session, state=None) -> list[HospitalScore(ccn, name, outcomes, matched, accuracy, sheet_version, flag_level)]`, `MIN_OUTCOMES=3`; `learning.contributions.rebuild_from_sources`, `NoTavily`; `cases.match.match_hospital(extract: BillExtract, hospitals: list[HospitalRef]) -> list[MatchCandidate(ccn, name, score)]`, `is_confident(candidates)`; `cases.extract.BillExtract(hospital_name=None, fap_url=None, ...)`; `web.app.create_app(settings, *, engine, ai, cipher, signer, today_fn)`, `web.deps.deps_of(request)`, `render(request, name, **context)`; `cli.app`, `cli.atlas_app`, `cli._engine(settings)`, `cli.console`; tests reuse `tests/unit/test_pipeline.py` (`HOSPITAL`, `REAL_HOSPITAL`, `TODAY`, `FakeGateway`, `FakeAI`, `POLICY_TEXT`, `OTHER_SENTENCE`, `make_engine_with_hospital`), `tests/unit/test_fetch.py` (`sample_pdf`, `pdf_response`), `tests/unit/test_web_app.py` (`make_client`).
- **Deviations from the master plan sketch, by this plan:** 7.2 makes the scheduler *refresh-first* (a hospital with stored documents is re-checked by content hash; a full re-scout happens only for never-scouted hospitals, for `rescout_request` demand, or when every document is unreachable), because §15 says refreshes extract only what changed. 7.4 expresses gate U7.1 in code as `WAIVE_NATIONAL_SCOUTING=on`, which the user sets when closing the gate. The master plan's task numbers are kept.

## Verified facts (read 2026-10-02 with WebFetch and WebSearch; nothing here was exercised against a live run)

| Topic | Verified | Unverified (assumed; mark in code comments) | Source |
|---|---|---|---|
| California HCAI lookup | Page "Hospital Fair Pricing Policy Lookup" at `https://hcai.ca.gov/affordability/hospital-fair-billing-program/hospital-fair-pricing-policy-lookup/`: a search box ("hospital name, city, county or zip code"), an alphabetical paginated list ("1-16 of 469 results", 30 pages), per-hospital links of the form `https://hcai.ca.gov/affordability/hospital-billing-policies/<slug>/` (examples: `adventist-health-and-rideout`, `adventist-health-bakersfield`, `usc-arcadia-hospital`); no CSV/JSON/API offered. Per-hospital page (USC Arcadia) shows name, aliases, address, License #, HCAI ID (`106190529`, an OSHPD id, not the CMS CCN), and links "Charity Care Policy", "Discount Payment Policy", "Application for Charity Care & Discount Payment", "Debt Collection Policy", each with an effective date, to `https://api.hdc.hcai.ca.gov/Public/Extract/Attachment?id=<guid>` (charity and discount policies shared one guid there). Search-result titles read `<HOSPITAL NAME> - HCAI`. Statute: Health and Safety Code §§127400–127446 (AB 774, AB 1020) per the search summary | Slug derivation from the CMS name for *every* hospital (one example checked); the HTML markup around the attachment links (labels assumed to be the nearest preceding heading or the link text); attachment Content-Type (assumed PDF, possibly `application/octet-stream`); whether CMS names match HCAI names | lookup page, USC Arcadia page, WebSearch result list |
| Washington DOH policies | Page "Hospital Policies" at `https://doh.wa.gov/licenses-permits-and-certificates/facilities-z/hospitals/hospital-policies`: one searchable table indexed by hospital name, city and county; each row lists policies as hyperlinked text (Admissions, **Charity Care**, Charter, End of Life, Non-discrimination, Nurse Staffing, Reproductive Health); link patterns `/sites/default/files/hospital-policies/MortonAD.pdf`, `/sites/default/files/2024-07/CCP173.pdf` (relative, PDFs on doh.wa.gov). The page states no income thresholds | Exact `<table>` markup (cells assumed in order name, city, county, policies); name matching threshold; whether every hospital's charity care link is a PDF | DOH hospital policies page |
| ProPublica Nonprofit Explorer API | Base `https://projects.propublica.org/nonprofits/api/v2`, GET only, no key. `GET /organizations/<ein>.json` → keys `organization`, `filings_with_data` (fields incl. `tax_prd_yr`, `totrevenue`, `pdf_url`), `filings_without_data`. `GET /search.json?q=…&state[id]=XX&page=N` (25 per page). Schedule H fields are **not** exposed. PDF downloads are rate limited; use is subject to ProPublica's Data Terms of Use | Search response keys (assumed `organizations[]` with `ein`, `name`, `city`, `state`); `organization.subsection_code` (assumed integer 3 for 501(c)(3)); any text pattern for Schedule H Part I lines 3a/3b in the filing PDF | https://projects.propublica.org/nonprofits/api |
| CMS registry | `https://data.cms.gov/provider-data/api/1/datastore/query/xubh-q36u/0` with `conditions[0][property]=state`, paging by `limit`/`offset` (Phase 2, live for MA) | Availability for all 51 jurisdictions in one run (rate limits unknown) | `src/waive/atlas/registry.py` |
| APScheduler | Listed in spec §6; **not installed** (`uv.lock` has no entry) | API used below is APScheduler 3.x (`BackgroundScheduler`, `IntervalTrigger`, `add_job(..., id, max_instances, coalesce, misfire_grace_time)`, `get_job`, `shutdown(wait=False)`); pin `>=3.10,<4` because 4.x changes the API | — |
| Credits per hospital | Phase 2 measured ≈4–5 Tavily credits per hospital (two searches, often a Map, one or two Extract calls) | National estimate = 5 × hospitals without a sheet | `docs/PROGRESS.md` |
| State order by population | — | The `STATE_POPULATION_ORDER` tuple in 7.4 is the 2020 census ranking from memory; it only decides which state is scouted first | — |

## File structure

| File | Responsibility | Task |
|---|---|---|
| `src/waive/governor.py` (modify) | `Ledger.events` / `DbLedger.events` (time-filtered), `Governor.tavily_used_today`, `tavily_by_day`, `day_start` | 7.1, 7.7 |
| `src/waive/config.py`, `.env.example` (modify) | `scheduler`, `scheduler_interval_minutes`, `scheduler_states`, `scout_daily_credits`, `national_scouting` | 7.1, 7.4 |
| `src/waive/atlas/schedule.py` (create) | Priority queue, budgeted run, national batch, APScheduler job | 7.1, 7.2, 7.4 |
| `src/waive/atlas/refresh.py` (create) | Content-hash refresh of stored documents | 7.2 |
| `src/waive/atlas/repo.py` (modify) | `touch_source`, `unlink_source`, `ccns_with_sheets`, `ein` field | 7.1, 7.2, 7.6 |
| `src/waive/atlas/registry.py` (modify) | `STATES`, `seed_all_states`, `SeedReport.state/error` | 7.3 |
| `src/waive/atlas/state_sources.py` (create) | HCAI and DOH repositories as document sources | 7.5 |
| `src/waive/atlas/overlays.py`, `pipeline.py`, `fetch.py`, `learning/contributions.py` (modify) | `is_overlay_source`, repository documents in `build_hospital`, `download_text(assume_pdf=)` | 7.5 |
| `src/waive/atlas/irs.py` (create), `src/waive/db.py` (modify) | ProPublica lookup, EIN column, Schedule H best-effort parse, `irs_mismatch` | 7.6 |
| `src/waive/atlas/metrics.py` (create), `src/waive/web/routes_metrics.py` (create), `templates/metrics.html` (create), `static/waive.css` (modify) | Metrics, `/metrics`, `/metrics.json`, national report writer | 7.7 |
| `src/waive/web/app.py` (modify) | Lifespan starts/stops the scheduler; metrics router | 7.1, 7.7 |
| `src/waive/cli.py` (modify) | `atlas schedule`, `atlas refresh`, `atlas seed --all-states`, `atlas build --national`, `atlas irs`, `atlas report --national` | all |
| `tests/unit/test_schedule.py`, `test_refresh.py`, `test_state_sources.py`, `test_irs.py`, `test_metrics.py` (create); `test_governor.py`, `test_registry.py`, `test_config.py`, `test_db_upgrade.py` (modify) | Tests | all |
| `docs/reports/atlas-national.md` (create by hand in 7.4, generated from 7.7) | National coverage report with a preserved run log | 7.4, 7.7 |

---

### Task 7.1: Scheduler and priority queue

**Files:**
- Modify: `pyproject.toml` (dependency), `src/waive/config.py`, `.env.example`, `src/waive/governor.py`, `src/waive/atlas/repo.py`, `src/waive/web/app.py`, `src/waive/cli.py`
- Create: `src/waive/atlas/schedule.py`
- Test: `tests/unit/test_governor.py` (add), `tests/unit/test_config.py` (add), `tests/unit/test_schedule.py` (create)

**Interfaces:**
- Consumes: `build_hospital`, `repo.*`, `scoreboard`, `match_hospital`/`is_confident`, `BillExtract`, `Governor`, `Settings`, `create_app`.
- Produces (`waive.governor`): `day_start(today: date) -> str`; `Ledger.events(provider: str, since: str | None = None) -> list[UsageEvent]` and the same on `DbLedger`; `Governor.tavily_used_since(since: str) -> Decimal`; `Governor.tavily_used_today(today: date) -> Decimal`.
- Produces (`waive.atlas.repo`): `ccns_with_sheets(session) -> set[str]`.
- Produces (`waive.atlas.schedule`): constants `NEVER_SCOUTED_DAYS = 90`, `DEFAULT_ACCURACY = 0.5`, `ACCURACY_TERM_FLOOR = 0.2`, `CREDITS_PER_HOSPITAL = Decimal("5")`, `RETRY_AFTER_DAYS = 30`, `FAILURE_KINDS`, `DEMAND_WEIGHTS`, `JOB_ID = "scout"`; `priority_score(staleness_days: int, demand: int, accuracy: float | None) -> float`; `QueueEntry(ccn, name, state, staleness_days, demand, accuracy, priority, reasons, has_sources, rescout_requested)`; `QueueReport(entries, skipped, unmatched_requests, without_sheet)`; `newest_fetch_by_ccn(session) -> dict[str, date]` (newest hospital-document fetch date per CCN; 7.7 reuses it); `build_queue(session, today, states: tuple[str, ...] = ()) -> QueueReport`; `Stop` literal; `RunReport(today, daily_cap, used_before, used_after, results, stopped)`; `run_entries(session, gateway, ai, governor, today, entries, *, daily_cap, limit=None) -> RunReport`; `run_once(session, gateway, ai, governor, today, *, daily_cap, states=(), limit=None) -> RunReport`; `scheduler_states(settings) -> tuple[str, ...]`; `scout_tick(engine, settings, governor, ai) -> RunReport | None`; `make_scheduler(engine, settings, governor, ai) -> BackgroundScheduler`.
- Produces (`waive.config.Settings`): `scheduler: Literal["on", "off"] = "off"`, `scheduler_interval_minutes: int = 30`, `scheduler_states: str = "MA"`, `scout_daily_credits: int = 50`.
- Produces (CLI): `waive atlas schedule [--dry-run|--run] [--limit N] [--state XX] [--top N]`.

- [ ] **Step 1: Add the dependency and settings**

Run: `uv add "apscheduler>=3.10,<4"`

In `src/waive/config.py`, after the `cloud_pg_disk_gib` line and before the validator, add:

```python
    # Phase 7: unattended scouting (spec §8 step 8). Off by default; a long-running `waive serve`
    # or the production container turns it on. Each tick scouts at most one hospital and stops
    # once WAIVE_SCOUT_DAILY_CREDITS Tavily credits were spent in the current UTC day.
    scheduler: Literal["on", "off"] = "off"
    scheduler_interval_minutes: int = 30
    # Comma-separated states the scheduler may touch; empty means every seeded state (gate U7.1).
    scheduler_states: str = "MA"
    scout_daily_credits: int = 50
```

In `.env.example`, after `WAIVE_CLOUD_PG_DISK_GIB=32`, add:

```
# Phase 7 scheduler: on/off, minutes between ticks, states it may scout (empty = all), credits per UTC day
WAIVE_SCHEDULER=off
WAIVE_SCHEDULER_INTERVAL_MINUTES=30
WAIVE_SCHEDULER_STATES=MA
WAIVE_SCOUT_DAILY_CREDITS=50
```

Append to `tests/unit/test_config.py`:

```python
def test_scheduler_settings_default_off():
    from waive.config import Settings

    settings = Settings(_env_file=None)
    assert settings.scheduler == "off"
    assert (settings.scheduler_interval_minutes, settings.scheduler_states) == (30, "MA")
    assert settings.scout_daily_credits == 50
    on = Settings(_env_file=None, scheduler="on", scheduler_states="MA,RI", scout_daily_credits=20)
    assert (on.scheduler, on.scout_daily_credits) == ("on", 20)
```

- [ ] **Step 2: Write the failing governor tests**

Append to `tests/unit/test_governor.py` (add `from datetime import date` to its imports and `day_start` to the `waive.governor` import line):

```python
def test_events_since_filters_by_timestamp(tmp_path):
    ledger = Ledger(tmp_path / "usage.jsonl")
    ledger.record(
        UsageEvent("tavily", Decimal("3"), Decimal("0"), "atlas.scout", "2026-10-01T23:59:59+00:00")
    )
    ledger.record(
        UsageEvent("tavily", Decimal("4"), Decimal("0"), "atlas.scout", "2026-10-02T08:00:00+00:00")
    )
    ledger.record(
        UsageEvent("token_factory", Decimal("10"), Decimal("0.01"), "x", "2026-10-02T09:00:00+00:00")
    )
    assert day_start(date(2026, 10, 2)) == "2026-10-02T00:00:00+00:00"
    assert [e.units for e in ledger.events("tavily")] == [Decimal("3"), Decimal("4")]
    since = day_start(date(2026, 10, 2))
    assert [e.units for e in ledger.events("tavily", since=since)] == [Decimal("4")]
    governor = Governor(ledger, 100, Decimal("1"))
    assert governor.tavily_used_today(date(2026, 10, 2)) == Decimal("4")
    assert governor.tavily_used_today(date(2026, 10, 3)) == Decimal("0")
    assert Ledger(tmp_path / "none.jsonl").events("tavily") == []


def test_db_ledger_events_since():
    ledger = DbLedger(memory_engine())
    ledger.record(
        UsageEvent("tavily", Decimal("3"), Decimal("0"), "atlas.scout", "2026-10-01T23:59:59+00:00")
    )
    ledger.record(
        UsageEvent("tavily", Decimal("4"), Decimal("0"), "atlas.refresh", "2026-10-02T08:00:00+00:00")
    )
    since = day_start(date(2026, 10, 2))
    events = ledger.events("tavily", since=since)
    assert [(e.units, e.purpose) for e in events] == [(Decimal("4"), "atlas.refresh")]
    assert Governor(ledger, 100, Decimal("1")).tavily_used_since(since) == Decimal("4")
```

- [ ] **Step 3: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_governor.py tests/unit/test_config.py -v`
Expected: the two new governor tests FAIL with `ImportError: cannot import name 'day_start'`; the config test passes already (settings were added in step 1).

- [ ] **Step 4: Implement the ledger query**

In `src/waive/governor.py`, change the imports to:

```python
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
```

Add to the `Ledger` class after `totals`:

```python
    def events(self, provider: str, since: str | None = None) -> list[UsageEvent]:
        """Recorded calls for `provider`, oldest first. `since` is an ISO-8601 UTC timestamp in
        the format `_now()` writes; timestamps are compared as text, which is correct for that
        format."""
        found: list[UsageEvent] = []
        if not self._path.exists():
            return found
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row["provider"] != provider or (since is not None and row["ts"] < since):
                continue
            found.append(
                UsageEvent(
                    row["provider"],
                    Decimal(row["units"]),
                    Decimal(row["usd"]),
                    row["purpose"],
                    row["ts"],
                )
            )
        return found
```

Replace `_now` with:

```python
def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def day_start(today: date) -> str:
    """The timestamp `_now()` writes at midnight UTC on `today`; the lower bound of a day."""
    return f"{today.isoformat()}T00:00:00+00:00"
```

Add to `Governor` after `summary`:

```python
    def tavily_used_since(self, since: str) -> Decimal:
        return sum(
            (event.units for event in self._ledger.events("tavily", since)), Decimal("0")
        )

    def tavily_used_today(self, today: date) -> Decimal:
        """Credits spent in the UTC day `today`; the scheduler's daily budget counts these."""
        return self.tavily_used_since(day_start(today))

    def tavily_by_day(self, since: date, today: date) -> list[tuple[str, Decimal]]:
        """Credits per UTC day from `since` to `today` inclusive, zero-filled, oldest first."""
        days = {
            (since + timedelta(days=n)).isoformat(): Decimal("0")
            for n in range((today - since).days + 1)
        }
        for event in self._ledger.events("tavily", day_start(since)):
            day = event.ts[:10]
            if day in days:
                days[day] += event.units
        return sorted(days.items())
```

Add to `DbLedger` after `totals`:

```python
    def events(self, provider: str, since: str | None = None) -> list[UsageEvent]:
        query = select(UsageEventRow).where(UsageEventRow.provider == provider)
        if since is not None:
            query = query.where(UsageEventRow.ts >= since)
        with session_scope(self._engine) as session:
            rows = session.scalars(query.order_by(UsageEventRow.id)).all()
        return [
            UsageEvent(row.provider, Decimal(row.units), Decimal(row.usd), row.purpose, row.ts)
            for row in rows
        ]
```

Add to `src/waive/atlas/repo.py` (after `sheet_versions`; `SheetRow` is already imported):

```python
def ccns_with_sheets(session: Session) -> set[str]:
    """Hospitals that have at least one stored sheet version (any status)."""
    return set(session.scalars(select(SheetRow.ccn).distinct()))
```

- [ ] **Step 5: Run the governor tests**

Run: `uv run pytest tests/unit/test_governor.py -v`
Expected: all pass (10 tests).

- [ ] **Step 6: Write the failing scheduler tests**

`tests/unit/test_schedule.py`:

```python
import base64
from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from pydantic import SecretStr

from waive.atlas import repo
from waive.atlas.schedule import (
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
        again = run_once(session, SpendingGateway(governor), FakeAI(), governor, TODAY, daily_cap=100)
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
```

- [ ] **Step 7: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_schedule.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.atlas.schedule'`.

- [ ] **Step 8: Implement the queue and the budgeted run**

`src/waive/atlas/schedule.py`:

```python
"""Always-on scouting: a priority queue over the registry and a budgeted job (spec §8 step 8,
§15 daily budget, §16 metrics).

Priority = staleness × demand × (1 − accuracy). Staleness is days since the newest document was
fetched; demand counts open cases and review items asking for this hospital; accuracy comes from
the scoreboard. One job runs inside the app (APScheduler) and scouts at most one hospital per
tick, never past the day's Tavily budget; the same code runs from `waive atlas schedule`.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from waive.ai.client import AIClient
from waive.atlas import repo
from waive.atlas.pipeline import BuildResult, build_hospital
from waive.atlas.schema import HospitalRef
from waive.atlas.tavily_gateway import TavilyGateway, make_tavily_gateway
from waive.cases.extract import BillExtract
from waive.cases.match import is_confident, match_hospital
from waive.config import Settings
from waive.db import CaseRow, ReviewItemRow, SourceDocRow, hospital_sources, session_scope
from waive.governor import BudgetExceeded, Governor
from waive.learning.scoreboard import MIN_OUTCOMES, scoreboard

log = logging.getLogger(__name__)

# A hospital nobody has scouted counts as a quarter stale: new coverage matters, but a sheet that
# patients are actively contradicting can outrank it.
NEVER_SCOUTED_DAYS = 90
# Below MIN_OUTCOMES scored outcomes the scoreboard says nothing; assume a coin flip.
DEFAULT_ACCURACY = 0.5
# A perfectly accurate sheet still goes stale: keep a fifth of the staleness pressure.
ACCURACY_TERM_FLOOR = 0.2
# Phase 2 measured 4–5 Tavily credits per hospital (two searches, a Map, one or two Extracts).
CREDITS_PER_HOSPITAL = Decimal("5")
# Failed discoveries and empty scouts are not retried for a month.
RETRY_AFTER_DAYS = 30
FAILURE_KINDS = ("domain", "no_documents")
DEMAND_WEIGHTS = {"open_case": 1, "rescout_request": 2, "priority_recheck": 2, "scout_request": 2}
# Overlay sources (ids `state-…`, the shared mass.gov page) say nothing about a hospital's own
# documents; repository copies (ids `repo-…`, task 7.5) do.
FRESHNESS_KINDS = ("hospital_web", "state_repository")
JOB_ID = "scout"

Stop = Literal["queue empty", "limit reached", "daily budget", "credit cap"]


@dataclass(frozen=True)
class QueueEntry:
    ccn: str
    name: str
    state: str
    staleness_days: int
    demand: int
    accuracy: float | None
    priority: float
    reasons: tuple[str, ...]
    has_sources: bool
    rescout_requested: bool


@dataclass
class QueueReport:
    entries: list[QueueEntry]
    skipped: dict[str, str]
    unmatched_requests: list[dict[str, str | None]]
    without_sheet: int


def priority_score(staleness_days: int, demand: int, accuracy: float | None) -> float:
    """staleness × demand × (1 − accuracy), floored so that a fresh, perfect sheet still comes
    back around and a hospital nobody asked about still refreshes on age alone."""
    known = DEFAULT_ACCURACY if accuracy is None else accuracy
    accuracy_term = max(ACCURACY_TERM_FLOOR, 1.0 - known)
    return round(max(1, staleness_days) * max(1, demand) * accuracy_term, 2)


def newest_fetch_by_ccn(session: Session) -> dict[str, date]:
    query = (
        select(hospital_sources.c.ccn, func.max(SourceDocRow.fetched_on))
        .join(SourceDocRow, SourceDocRow.id == hospital_sources.c.source_id)
        .where(SourceDocRow.kind.in_(FRESHNESS_KINDS), SourceDocRow.id.not_like("state-%"))
        .group_by(hospital_sources.c.ccn)
    )
    return {ccn: fetched for ccn, fetched in session.execute(query)}


def _open_cases_by_ccn(session: Session) -> dict[str, int]:
    """Cases matched to a hospital that have no outcome yet: people are waiting on this sheet."""
    query = (
        select(CaseRow.ccn, func.count())
        .where(CaseRow.ccn.is_not(None), CaseRow.outcome.is_(None))
        .group_by(CaseRow.ccn)
    )
    return {ccn: int(count) for ccn, count in session.execute(query)}


def _recent_failures(session: Session, today: date) -> set[str]:
    cutoff = today - timedelta(days=RETRY_AFTER_DAYS)
    rows = session.scalars(
        select(ReviewItemRow).where(
            ReviewItemRow.status == "open", ReviewItemRow.kind.in_(FAILURE_KINDS)
        )
    )
    return {row.ccn for row in rows if row.ccn and row.created_at.date() >= cutoff}


def _request_matches(
    items: list[ReviewItemRow], by_state: dict[str, list[HospitalRef]]
) -> tuple[dict[str, int], list[dict[str, str | None]]]:
    """`scout_request` items carry a bill's hospital name and FAP web address but no CCN; the
    bill matcher attributes them to a registry hospital when it is confident."""
    matched: dict[str, int] = {}
    unmatched: list[dict[str, str | None]] = []
    for item in items:
        detail = item.detail or {}
        refs = by_state.get(str(detail.get("state") or "").upper(), [])
        extract = BillExtract(
            hospital_name=detail.get("hospital_name"), fap_url=detail.get("fap_url")
        )
        candidates = match_hospital(extract, refs) if refs else []
        if candidates and is_confident(candidates):
            matched[candidates[0].ccn] = matched.get(candidates[0].ccn, 0) + 1
        else:
            unmatched.append(
                {"hospital_name": detail.get("hospital_name"), "state": detail.get("state")}
            )
    return matched, unmatched


def build_queue(session: Session, today: date, states: tuple[str, ...] = ()) -> QueueReport:
    hospitals = [row for row in repo.list_hospitals(session) if not repo.is_demo(row.ccn)]
    if states:
        wanted = {state.upper() for state in states}
        hospitals = [row for row in hospitals if row.state in wanted]
    newest = newest_fetch_by_ccn(session)
    open_cases = _open_cases_by_ccn(session)
    failures = _recent_failures(session, today)
    sheeted = repo.ccns_with_sheets(session)
    accuracy = {
        score.ccn: score.accuracy for score in scoreboard(session) if score.outcomes >= MIN_OUTCOMES
    }
    items = repo.open_review_items(session)
    signals: dict[str, dict[str, int]] = {}
    for item in items:
        if item.ccn is None or item.kind not in ("rescout_request", "priority_recheck"):
            continue
        weight = int((item.detail or {}).get("count", 1)) if item.kind == "rescout_request" else 1
        kinds = signals.setdefault(item.ccn, {})
        kinds[item.kind] = kinds.get(item.kind, 0) + weight
    by_state: dict[str, list[HospitalRef]] = {}
    for row in hospitals:
        by_state.setdefault(row.state, []).append(repo.hospital_ref(row))
    requests, unmatched = _request_matches(
        [item for item in items if item.kind == "scout_request" and item.ccn is None], by_state
    )

    entries: list[QueueEntry] = []
    skipped: dict[str, str] = {}
    without_sheet = 0
    for row in hospitals:
        without_sheet += row.ccn not in sheeted
        if row.ccn in failures:
            skipped[row.ccn] = f"recent failure; retried after {RETRY_AFTER_DAYS} days"
            continue
        fetched = newest.get(row.ccn)
        staleness = NEVER_SCOUTED_DAYS if fetched is None else (today - fetched).days
        if fetched is not None and staleness <= 0:
            skipped[row.ccn] = "scouted today"
            continue
        demand = 1
        reasons: list[str] = []
        if cases := open_cases.get(row.ccn, 0):
            demand += DEMAND_WEIGHTS["open_case"] * cases
            reasons.append(f"{cases} open case(s)")
        for kind, count in sorted(signals.get(row.ccn, {}).items()):
            demand += DEMAND_WEIGHTS[kind] * count
            reasons.append(f"{kind} ×{count}")
        if bills := requests.get(row.ccn, 0):
            demand += DEMAND_WEIGHTS["scout_request"] * bills
            reasons.append(f"{bills} bill(s) named this hospital")
        if fetched is None:
            reasons.append("never scouted")
        entries.append(
            QueueEntry(
                ccn=row.ccn,
                name=row.name,
                state=row.state,
                staleness_days=staleness,
                demand=demand,
                accuracy=accuracy.get(row.ccn),
                priority=priority_score(staleness, demand, accuracy.get(row.ccn)),
                reasons=tuple(reasons),
                has_sources=fetched is not None,
                rescout_requested="rescout_request" in signals.get(row.ccn, {}),
            )
        )
    entries.sort(key=lambda entry: (-entry.priority, entry.name))
    return QueueReport(entries, skipped, unmatched, without_sheet)


@dataclass
class RunReport:
    today: date
    daily_cap: int
    used_before: Decimal
    used_after: Decimal
    results: list[BuildResult] = field(default_factory=list)
    stopped: Stop = "queue empty"


def _note_scouted(session: Session, ccn: str, today: date) -> None:
    """Mark the items that asked for this scout; they stay open for the admin's verdict."""
    for item in repo.open_review_items(session, ccn):
        if item.kind in ("rescout_request", "priority_recheck"):
            item.detail = {**(item.detail or {}), "rescouted_on": today.isoformat()}
    session.flush()


def _scout(
    session: Session,
    gateway: TavilyGateway,
    ai: AIClient,
    entry: QueueEntry,
    today: date,
) -> BuildResult:
    return build_hospital(session, gateway, ai, entry.ccn, today, reuse_sources=False)


def run_entries(
    session: Session,
    gateway: TavilyGateway,
    ai: AIClient,
    governor: Governor,
    today: date,
    entries: list[QueueEntry],
    *,
    daily_cap: int,
    limit: int | None = None,
) -> RunReport:
    """Scout `entries` in order until the list, the limit or the day's credits run out. The hard
    cap still applies inside the gateway (`BudgetExceeded` stops the run)."""
    used = governor.tavily_used_today(today)
    report = RunReport(today, daily_cap, used, used)
    for entry in entries:
        if limit is not None and len(report.results) >= limit:
            report.stopped = "limit reached"
            break
        if Decimal(daily_cap) - report.used_after < CREDITS_PER_HOSPITAL:
            report.stopped = "daily budget"
            break
        try:
            result = _scout(session, gateway, ai, entry, today)
        except BudgetExceeded as error:
            session.rollback()
            report.results.append(BuildResult(entry.ccn, entry.name, "failed", notes=[str(error)]))
            report.stopped = "credit cap"
            break
        except Exception as error:  # keep the run going; the failure is in the report
            session.rollback()
            result = BuildResult(entry.ccn, entry.name, "failed", notes=[type(error).__name__])
        else:
            _note_scouted(session, entry.ccn, today)
        report.results.append(result)
        session.commit()
        report.used_after = governor.tavily_used_today(today)
        log.info("scout %s %s: %s", entry.ccn, entry.name, result.outcome)
    return report


def run_once(
    session: Session,
    gateway: TavilyGateway,
    ai: AIClient,
    governor: Governor,
    today: date,
    *,
    daily_cap: int,
    states: tuple[str, ...] = (),
    limit: int | None = None,
) -> RunReport:
    queue = build_queue(session, today, states)
    return run_entries(
        session, gateway, ai, governor, today, queue.entries, daily_cap=daily_cap, limit=limit
    )


def scheduler_states(settings: Settings) -> tuple[str, ...]:
    return tuple(s.strip().upper() for s in settings.scheduler_states.split(",") if s.strip())


def scout_tick(
    engine: Engine, settings: Settings, governor: Governor, ai: AIClient | None
) -> RunReport | None:
    """One scheduler tick: at most one hospital, inside the daily budget. None when a key is
    missing — the app runs without scouting and nothing is spent."""
    if ai is None or settings.tavily_api_key is None:
        log.info("scout tick skipped: no model key or no Tavily key")
        return None
    gateway = make_tavily_gateway(settings, governor)
    today = datetime.now(UTC).date()
    with session_scope(engine) as session:
        report = run_once(
            session,
            gateway,
            ai,
            governor,
            today,
            daily_cap=settings.scout_daily_credits,
            states=scheduler_states(settings),
            limit=1,
        )
    log.info(
        "scout tick: %d built, %s credits used today, stopped: %s",
        len(report.results),
        report.used_after,
        report.stopped,
    )
    return report


def make_scheduler(engine: Engine, settings: Settings, governor: Governor, ai: AIClient | None):
    """A background scheduler with the one scouting job, not yet started."""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        scout_tick,
        IntervalTrigger(minutes=settings.scheduler_interval_minutes),
        args=[engine, settings, governor, ai],
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    return scheduler
```

- [ ] **Step 9: Start the scheduler from the app factory**

In `src/waive/web/app.py`, add `from contextlib import asynccontextmanager` to the imports and replace the line `app = FastAPI(title="Waive", docs_url=None, redoc_url=None)` with:

```python
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = None
        if settings.scheduler == "on":
            from waive.atlas.schedule import make_scheduler

            scheduler = make_scheduler(engine, settings, governor, app.state.deps.ai)
            scheduler.start()
        app.state.scheduler = scheduler
        yield
        if scheduler is not None:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="Waive", docs_url=None, redoc_url=None, lifespan=lifespan)
```

(`app.state.deps` is assigned a few lines later in `create_app`; the lifespan body only runs at startup, after it exists.)

- [ ] **Step 10: Add the CLI command**

In `src/waive/cli.py`, add to the imports:

```python
from waive.atlas.schedule import CREDITS_PER_HOSPITAL, build_queue, run_once, scheduler_states
```

Add after `atlas_report`:

```python
@atlas_app.command("schedule")
def atlas_schedule(
    dry_run: bool = typer.Option(
        True, "--dry-run/--run", help="List the queue (default) or scout its top within the budget"
    ),
    limit: int = typer.Option(1, "--limit", help="Hospitals to scout with --run"),
    state: str | None = typer.Option(None, "--state", help="One state; default WAIVE_SCHEDULER_STATES"),
    top: int = typer.Option(20, "--top", help="Queue rows to print"),
) -> None:
    """Show the scouting priority queue and today's Tavily budget; --run spends credits."""
    settings = Settings()
    governor = make_governor(settings)
    today = datetime.now(UTC).date()
    states = (state.upper(),) if state else scheduler_states(settings)
    with session_scope(_engine(settings)) as session:
        queue = build_queue(session, today, states)
        table = Table("Priority", "CCN", "Hospital", "St", "Stale", "Demand", "Accuracy", "Why")
        for entry in queue.entries[:top]:
            table.add_row(
                f"{entry.priority:.1f}",
                entry.ccn,
                entry.name,
                entry.state,
                str(entry.staleness_days),
                str(entry.demand),
                "" if entry.accuracy is None else f"{entry.accuracy:.0%}",
                "; ".join(entry.reasons)[:80],
            )
        console.print(table)
        used = governor.tavily_used_today(today)
        console.print(
            f"Queue: {len(queue.entries)} hospitals in {', '.join(states) or 'all states'} "
            f"({queue.without_sheet} without a sheet); skipped {len(queue.skipped)}; "
            f"unmatched bill requests {len(queue.unmatched_requests)}"
        )
        console.print(
            f"Daily budget: {used} of {settings.scout_daily_credits} credits used today "
            f"(UTC); about {CREDITS_PER_HOSPITAL} per hospital; "
            f"estimate for every hospital without a sheet: "
            f"{CREDITS_PER_HOSPITAL * queue.without_sheet} credits"
        )
        if dry_run:
            return
        ai = AIClient(settings, governor)
        gateway = make_tavily_gateway(settings, governor)
        report = run_once(
            session,
            gateway,
            ai,
            governor,
            today,
            daily_cap=settings.scout_daily_credits,
            states=states,
            limit=limit,
        )
    for result in report.results:
        console.print(f"{result.ccn} {result.name}: {result.outcome}; " + "; ".join(result.notes))
    console.print(
        f"Stopped: {report.stopped}; credits today {report.used_before} → {report.used_after}"
    )
```

- [ ] **Step 11: Run the tests, lint, format**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest`
Expected: about 268 tests pass (257 + 1 config + 2 governor + 8 schedule).

If `test_make_scheduler_configures_one_interval_job` fails on `get_job` returning None before `start()`, APScheduler's pending-job lookup differs from the assumption above: replace the assertion with `[job] = scheduler.get_jobs()` and keep the rest.

- [ ] **Step 12: Dry run on the local database (free) and commit**

Run: `uv run waive atlas schedule --dry-run`
Expected: a table of Massachusetts hospitals with priorities (every MA hospital has sources from Phase 2, so staleness is days since 2026-10-02/03; the eleven held hospitals without income rules sit near the top only if they carry review items), "Daily budget: 0 of 50 credits used today". No credit is spent.

```bash
git add pyproject.toml uv.lock .env.example src/waive/config.py src/waive/governor.py src/waive/atlas/repo.py src/waive/atlas/schedule.py src/waive/web/app.py src/waive/cli.py tests/unit/test_config.py tests/unit/test_governor.py tests/unit/test_schedule.py
git commit -m "feat: scouting priority queue, daily credit budget and in-app scheduler (7.1)"
```

**Gate note.** `waive atlas schedule --run` and `WAIVE_SCHEDULER=on` spend Tavily credits (≈5 per hospital): on Massachusetts they need gate **U2.2** (Phase 2 cap) and stay under `WAIVE_TAVILY_CREDIT_CAP` through `Governor.ensure_tavily`; outside Massachusetts (`WAIVE_SCHEDULER_STATES` widened or emptied) they need gate **U7.1**. Do not switch the scheduler on in `.env` until one of those gates is closed.

---

### Task 7.2: Content-hash refresh

**Files:**
- Create: `src/waive/atlas/refresh.py`
- Modify: `src/waive/atlas/repo.py` (`touch_source`, `unlink_source`), `src/waive/atlas/schedule.py` (`_scout` becomes refresh-first), `src/waive/cli.py`
- Test: `tests/unit/test_refresh.py` (create), `tests/unit/test_schedule.py` (add one test)

**Interfaces:**
- Consumes: `repo.sources_for`, `repo.save_source`, `scout.source_id_for`, `scout.MAX_CHARS`, `scout.MIN_CHARS`, `scout.DOCUMENT_HOSTS`, `fetch.download_text`, `fetch.looks_like_pdf`, `build_hospital(..., reuse_sources=True)`, `TavilyGateway.extract`.
- Produces (`waive.atlas.repo`): `touch_source(session, source_id: str, fetched_on: date) -> None`; `unlink_source(session, ccn: str, source_id: str) -> None`.
- Produces (`waive.atlas.refresh`): `RefreshOutcome = Literal["unchanged", "restructured", "skipped", "failed"]`; `RefreshResult(ccn, name, outcome, checked, changed, unreachable, build)`; `doc_class_of(source_id: str) -> DocClass`; `is_asset_host(url: str) -> bool`; `fetch_texts(gateway, urls: list[str], http: httpx.Client) -> dict[str, str]`; `refresh_hospital(session, gateway, ai, ccn, today, http: httpx.Client | None = None) -> RefreshResult`.
- Produces (CLI): `waive atlas refresh (--ccn X | --state XX [--limit N])`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_refresh.py`:

```python
import hashlib
from datetime import timedelta

import httpx
import respx

from waive.atlas import repo
from waive.atlas.pipeline import build_hospital
from waive.atlas.refresh import doc_class_of, fetch_texts, is_asset_host, refresh_hospital
from waive.atlas.schema import SourceDoc, SourceKind
from waive.atlas.tavily_gateway import ExtractedPage
from waive.db import SourceDocRow, session_scope

from tests.unit.test_fetch import pdf_response, sample_pdf
from tests.unit.test_pipeline import (
    HOSPITAL,
    OTHER_SENTENCE,
    POLICY_TEXT,
    TODAY,
    FakeAI,
    FakeGateway,
    make_engine_with_hospital,
)

LATER = TODAY + timedelta(days=20)
POLICY_URL = "https://www.example.org/financial-assistance-policy.pdf"
REVISED_TEXT = POLICY_TEXT + "Revised October 2026.\n"
CANTO_URL = "https://h.canto.com/direct/document/abc/def/original?content-type=application%2Fpdf"


class RefreshGateway(FakeGateway):
    """Extract answers from a table of current texts and records what it was asked for."""

    def __init__(self, texts):
        self.texts = texts
        self.extracted = []

    def extract(self, urls, **kwargs):
        self.extracted.append(list(urls))
        return [ExtractedPage(url, self.texts.get(url, "")) for url in urls]


class RevisedAI(FakeAI):
    """Reads 300% once the revised document is in the prompt (quoting the sentence that holds it)."""

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        limit = "300" if "Revised October 2026" in messages[1]["content"] else "250"
        self.free_limits = {"reason": limit, "fast": limit, "tiebreak": limit}
        return super().complete_json(role, messages, schema, phi=phi, purpose=purpose)


def seeded_engine():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        assert build_hospital(session, FakeGateway(), FakeAI(), "229999", TODAY).version == 1
    return engine


def test_doc_class_of_and_asset_hosts():
    assert doc_class_of("fap-0123456789abcdef") == "fap"
    assert doc_class_of("application-0123") == "application"
    assert doc_class_of("no-such-class") == "fap"
    assert is_asset_host("https://baystatehealth.canto.com/direct/document/x")
    assert not is_asset_host("https://www.example.org/fap.pdf")


def test_unchanged_documents_touch_the_source_and_create_no_version():
    engine = seeded_engine()
    gateway = RefreshGateway({POLICY_URL: POLICY_TEXT})
    with session_scope(engine) as session, httpx.Client() as http:
        result = refresh_hospital(session, gateway, FakeAI(), "229999", LATER, http)
        assert (result.outcome, result.checked, result.changed, result.unreachable) == (
            "unchanged",
            1,
            [],
            [],
        )
        assert gateway.extracted == [[POLICY_URL]]
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.version == 1
        [(source, _text)] = repo.sources_for(session, "229999")
        assert source.fetched_on == LATER  # staleness resets without a spurious version


def test_changed_document_is_stored_relinked_and_restructured():
    engine = seeded_engine()
    gateway = RefreshGateway({POLICY_URL: REVISED_TEXT})
    with session_scope(engine) as session, httpx.Client() as http:
        [(old, _)] = repo.sources_for(session, "229999")
        result = refresh_hospital(session, gateway, RevisedAI(), "229999", LATER, http)
        assert result.outcome == "restructured"
        assert result.changed == [old.id]
        assert result.build is not None and result.build.version == 2
        sheet, _ = repo.latest_sheet(session, "229999")
        assert sheet.eligibility.free_care_max_fpl.value == 300
        assert sheet.eligibility.free_care_max_fpl.quote == OTHER_SENTENCE
        [(current, text)] = repo.sources_for(session, "229999")
        assert current.id != old.id and current.id.startswith("fap-")
        assert current.sha256 == hashlib.sha256(REVISED_TEXT.encode("utf-8")).hexdigest()
        assert text == REVISED_TEXT and current.fetched_on == LATER
        assert session.get(SourceDocRow, old.id) is not None  # version 1 still cites it
        assert [s.id for s in sheet.sources] == [current.id]


@respx.mock
def test_asset_host_documents_are_downloaded_without_tavily():
    engine = make_engine_with_hospital()
    pdf = sample_pdf()
    respx.get(CANTO_URL).mock(return_value=pdf_response(pdf, "application/octet-stream"))
    gateway = RefreshGateway({})
    with session_scope(engine) as session, httpx.Client() as http:
        stored = SourceDoc(
            id="fap-" + "c" * 16,
            kind=SourceKind.HOSPITAL_WEB,
            url=CANTO_URL,
            title="FAP",
            fetched_on=TODAY,
            sha256="c" * 64,
        )
        repo.save_source(session, stored, "old text from a previous download", "229999")
        texts = fetch_texts(gateway, [CANTO_URL], http)
        assert "250% of the Federal Poverty" in texts[CANTO_URL]
        assert gateway.extracted == []  # Tavily cannot fetch asset hosts; nothing was spent
        result = refresh_hospital(session, gateway, FakeAI(), "229999", LATER, http)
        assert result.checked == 1 and result.changed == ["fap-" + "c" * 16]


@respx.mock
def test_empty_extract_falls_back_to_a_direct_download():
    respx.get(POLICY_URL).mock(return_value=pdf_response(sample_pdf()))
    gateway = RefreshGateway({POLICY_URL: ""})
    with httpx.Client() as http:
        texts = fetch_texts(gateway, [POLICY_URL], http)
    assert gateway.extracted == [[POLICY_URL]]
    assert "250% of the Federal Poverty" in texts[POLICY_URL]


@respx.mock
def test_unreachable_documents_are_reported_and_leave_the_sheet_alone():
    engine = seeded_engine()
    respx.get(POLICY_URL).mock(return_value=httpx.Response(404))
    gateway = RefreshGateway({POLICY_URL: ""})
    with session_scope(engine) as session, httpx.Client() as http:
        [(stored, _)] = repo.sources_for(session, "229999")
        result = refresh_hospital(session, gateway, FakeAI(), "229999", LATER, http)
        assert (result.outcome, result.unreachable, result.changed) == ("unchanged", [stored.id], [])
        [(source, _)] = repo.sources_for(session, "229999")
        assert source.fetched_on == TODAY  # not touched: we learned nothing about it


def test_hospital_without_web_documents_is_skipped():
    engine = make_engine_with_hospital()
    with session_scope(engine) as session:
        result = refresh_hospital(session, RefreshGateway({}), FakeAI(), "229999", LATER)
        assert (result.outcome, result.checked) == ("skipped", 0)
        missing = refresh_hospital(session, RefreshGateway({}), FakeAI(), "000000", LATER)
        assert missing.outcome == "failed"
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_refresh.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.atlas.refresh'`.

- [ ] **Step 3: Add the repository helpers**

Append to `src/waive/atlas/repo.py` (add `from datetime import date` to its imports):

```python
def touch_source(session: Session, source_id: str, fetched_on: date) -> None:
    """A re-fetch found the same content: record when, so staleness resets without a new sheet
    version (sheet versions only change when a value changes)."""
    row = session.get(SourceDocRow, source_id)
    if row is not None:
        row.fetched_on = fetched_on
        session.flush()


def unlink_source(session: Session, ccn: str, source_id: str) -> None:
    """Detach a superseded document from a hospital. The row stays: older sheet versions cite it."""
    hospital = session.get(HospitalRow, ccn)
    row = session.get(SourceDocRow, source_id)
    if hospital is not None and row is not None and hospital in row.hospitals:
        row.hospitals.remove(hospital)
        session.flush()
```

- [ ] **Step 4: Implement the refresh**

`src/waive/atlas/refresh.py`:

```python
"""Content-hash refresh of a hospital's stored documents (spec §8 step 8; §15: refreshes extract
only documents whose hash changed).

Each stored hospital document is fetched again through the channel the scout used — Tavily
Extract for web pages (one credit per five URLs), a direct download for PDFs on asset hosts where
Tavily fails (free). The text is truncated and hashed exactly as the scout did; an equal hash only
moves the source's `fetched_on`, a different hash stores the new document, replaces the old one
on the hospital and re-structures from stored sources (Token Factory only, no Tavily).
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, cast
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from waive.ai.client import AIClient
from waive.atlas import repo
from waive.atlas.fetch import download_text
from waive.atlas.pipeline import BuildResult, build_hospital
from waive.atlas.schema import SourceDoc, SourceKind
from waive.atlas.scout import (
    CLASS_ORDER,
    DOCUMENT_HOSTS,
    MAX_CHARS,
    MIN_CHARS,
    DocClass,
    source_id_for,
)
from waive.atlas.tavily_gateway import TavilyGateway

log = logging.getLogger(__name__)

RefreshOutcome = Literal["unchanged", "restructured", "skipped", "failed"]
PURPOSE = "atlas.refresh"


@dataclass
class RefreshResult:
    ccn: str
    name: str
    outcome: RefreshOutcome
    checked: int = 0
    changed: list[str] = field(default_factory=list)  # ids of the superseded sources
    unreachable: list[str] = field(default_factory=list)  # ids that could not be fetched
    build: BuildResult | None = None


def doc_class_of(source_id: str) -> DocClass:
    """The scout's ids are `<class>-<sha16>`; anything else is treated as a policy."""
    prefix = source_id.split("-", 1)[0]
    return cast(DocClass, prefix) if prefix in CLASS_ORDER else "fap"


def is_asset_host(url: str) -> bool:
    host = urlparse(url).netloc.lower().split(":")[0]
    return any(host == d or host.endswith("." + d) for d in DOCUMENT_HOSTS)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_texts(gateway: TavilyGateway, urls: list[str], http: httpx.Client) -> dict[str, str]:
    """Current text per URL, truncated like the scout's. Asset-host URLs are downloaded directly
    (no credits); the rest go through one Tavily Extract call, and any URL that comes back
    empty is downloaded as a fallback. URLs that yield nothing are absent from the result."""
    texts: dict[str, str] = {}
    direct = [url for url in urls if is_asset_host(url)]
    pages = [url for url in urls if url not in direct]
    for url in direct:
        if text := download_text(url, http):
            texts[url] = text
    if pages:
        for page in gateway.extract(pages, purpose=PURPOSE):
            if len(page.text.strip()) >= MIN_CHARS:
                texts[page.url] = page.text
        for url in pages:
            if url not in texts and (text := download_text(url, http)):
                texts[url] = text
    return {url: text[:MAX_CHARS] for url, text in texts.items()}


def refresh_hospital(
    session: Session,
    gateway: TavilyGateway,
    ai: AIClient,
    ccn: str,
    today: date,
    http: httpx.Client | None = None,
) -> RefreshResult:
    row = repo.get_hospital(session, ccn)
    if row is None:
        return RefreshResult(ccn, "?", "failed")
    current = [
        (source, text)
        for source, text in repo.sources_for(session, ccn)
        if source.kind is SourceKind.HOSPITAL_WEB and source.url
    ]
    if not current:
        return RefreshResult(ccn, row.name, "skipped")
    owned = http is None
    client = http or httpx.Client(follow_redirects=True, timeout=30.0)
    try:
        texts = fetch_texts(gateway, [source.url for source, _ in current], client)
    finally:
        if owned:
            client.close()

    result = RefreshResult(ccn, row.name, "unchanged", checked=len(current))
    for source, _old_text in current:
        text = texts.get(source.url or "")
        if text is None:
            result.unreachable.append(source.id)
            continue
        sha = _hash(text)
        if sha == source.sha256:
            repo.touch_source(session, source.id, today)
            continue
        replacement = SourceDoc(
            id=source_id_for(doc_class_of(source.id), sha),
            kind=SourceKind.HOSPITAL_WEB,
            url=source.url,
            title=source.title,
            fetched_on=today,
            sha256=sha,
        )
        repo.save_source(session, replacement, text, ccn)
        repo.unlink_source(session, ccn, source.id)
        result.changed.append(source.id)
        log.info("document changed for %s: %s", ccn, source.url)
    if result.changed:
        result.build = build_hospital(session, gateway, ai, ccn, today, reuse_sources=True)
        result.outcome = "restructured"
    return result
```

- [ ] **Step 5: Run the refresh tests**

Run: `uv run pytest tests/unit/test_refresh.py -v`
Expected: 7 passed. If `test_changed_document_is_stored_relinked_and_restructured` fails because `build_hospital` with `reuse_sources=True` still sees the old document, check that `unlink_source` ran before the build and that `sources_for` reads `hospital.sources` fresh (`session.flush()` is in `unlink_source`; add `session.expire(hospital, ["sources"])` after the removal if the relationship is cached).

- [ ] **Step 6: Make the scheduler refresh-first**

Write the failing test first — append to `tests/unit/test_schedule.py`:

```python
def test_run_once_refreshes_hospitals_with_documents_and_rescouts_on_request(tmp_path):
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
```

Run: `uv run pytest tests/unit/test_schedule.py -k refreshes -v` → FAIL (`outcome` is `published`, not `skipped`, because `_scout` always rebuilds).

Then replace `_scout` in `src/waive/atlas/schedule.py` with:

```python
def _scout(
    session: Session,
    gateway: TavilyGateway,
    ai: AIClient,
    entry: QueueEntry,
    today: date,
) -> BuildResult:
    """Refresh by content hash when documents are stored and nobody contradicted the sheet; a
    full re-scout (search, map, extract) for never-scouted hospitals, for re-scout requests, and
    when every stored document is unreachable."""
    if entry.has_sources and not entry.rescout_requested:
        refreshed = refresh_hospital(session, gateway, ai, entry.ccn, today)
        if refreshed.outcome == "restructured" and refreshed.build is not None:
            return refreshed.build
        if refreshed.outcome == "unchanged" and len(refreshed.unreachable) < refreshed.checked:
            return BuildResult(
                entry.ccn,
                entry.name,
                "skipped",
                notes=[f"unchanged ({refreshed.checked} documents checked)"],
            )
    return build_hospital(session, gateway, ai, entry.ccn, today, reuse_sources=False)
```

and add `from waive.atlas.refresh import refresh_hospital` to its imports.

Run: `uv run pytest tests/unit/test_schedule.py -v` → all pass.

- [ ] **Step 7: Add the CLI command**

In `src/waive/cli.py` add `from waive.atlas.refresh import refresh_hospital` and, after `atlas_schedule`:

```python
@atlas_app.command("refresh")
def atlas_refresh(
    state: str | None = typer.Option(None, "--state"),
    ccn: str | None = typer.Option(None, "--ccn"),
    limit: int | None = typer.Option(None, "--limit"),
) -> None:
    """Re-fetch stored documents and re-structure only the hospitals whose documents changed.
    Spends about one Tavily credit per five web documents; asset-host PDFs are free."""
    settings = Settings()
    governor = make_governor(settings)
    ai = AIClient(settings, governor)
    gateway = make_tavily_gateway(settings, governor)
    today = datetime.now(UTC).date()
    if not ccn and not state:
        raise typer.BadParameter("give --ccn or --state")
    with session_scope(_engine(settings)) as session, httpx.Client(
        follow_redirects=True, timeout=30.0
    ) as http:
        ccns = [ccn] if ccn else [row.ccn for row in repo.list_hospitals(session, state=state)]
        results = []
        for one in ccns[:limit]:
            results.append(refresh_hospital(session, gateway, ai, one, today, http))
            session.commit()
    table = Table("CCN", "Hospital", "Outcome", "Checked", "Changed", "Unreachable", "Version")
    for result in results:
        table.add_row(
            result.ccn,
            result.name,
            result.outcome,
            str(result.checked),
            str(len(result.changed)),
            str(len(result.unreachable)),
            str(result.build.version if result.build and result.build.version else ""),
        )
    console.print(table)
    tavily_credits, _ = governor.summary()["tavily"]
    _, tf_usd = governor.summary()["token_factory"]
    console.print(f"Spend so far: {tavily_credits} Tavily credits, ${tf_usd:.4f} Token Factory")
```

- [ ] **Step 8: Lint, format, full suite, commit**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest`
Expected: about 276 tests pass.

```bash
git add src/waive/atlas/refresh.py src/waive/atlas/repo.py src/waive/atlas/schedule.py src/waive/cli.py tests/unit/test_refresh.py tests/unit/test_schedule.py
git commit -m "feat: content-hash refresh of stored documents; scheduler refreshes before re-scouting (7.2)"
```

**Gate note.** `waive atlas refresh --state MA` costs about 1 credit per 5 web documents (≈ 40 credits for all 46 MA hospitals, less for asset-host PDFs) plus Token Factory tokens only for hospitals whose documents changed. It needs gate **U2.2**; the hard cap check is `Governor.ensure_tavily` inside `TavilyGateway.extract`. A good first live run, once U2.2 is closed: `uv run waive atlas refresh --ccn 220077` (Baystate Medical Center: asset-host PDFs, 0 credits expected) and then `--ccn 220086` (Boston Medical Center, web pages, ≤ 2 credits).

---

### Task 7.3: National registry seed

**Files:**
- Modify: `src/waive/atlas/registry.py`, `src/waive/cli.py`
- Test: `tests/unit/test_registry.py` (add)

**Interfaces:**
- Consumes: `seed_state`, `CMS_DATASTORE_URL`, `httpx.Client`.
- Produces (`waive.atlas.registry`): `STATES: tuple[str, ...]` (50 states + DC, 51 codes); `SeedReport` gains `state: str = ""` and `error: str | None = None` and `snapshot` becomes `Path | None`; `seed_all_states(session, http, snapshot_dir=Path("data/seed"), states=STATES, progress=None) -> list[SeedReport]`.
- Produces (CLI): `waive atlas seed --all-states` (and `--state XX` as before).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_registry.py` (add `from waive.atlas.registry import STATES, seed_all_states` to the import block):

```python
VT_ROWS = [
    {
        "facility_id": "470003",
        "facility_name": "RUTLAND REGIONAL MEDICAL CENTER",
        "address": "160 ALLEN STREET",
        "citytown": "RUTLAND",
        "state": "VT",
        "zip_code": "05701",
        "telephone_number": "(802) 775-7111",
        "hospital_type": "Acute Care Hospitals",
        "hospital_ownership": "Voluntary non-profit - Private",
    }
]


def test_states_cover_fifty_states_and_dc():
    assert len(STATES) == 51 and len(set(STATES)) == 51
    assert {"MA", "CA", "WA", "DC", "WY"} <= set(STATES)
    assert all(len(code) == 2 and code.isupper() for code in STATES)


@respx.mock
def test_seed_all_states_continues_past_a_failing_state(tmp_path):
    def by_state(request):
        state = request.url.params["conditions[0][value]"]
        if state == "RI":
            return httpx.Response(500)
        rows = {"MA": ROWS, "VT": VT_ROWS}[state]
        return httpx.Response(200, json={"results": rows, "count": len(rows)})

    respx.get(CMS_DATASTORE_URL).mock(side_effect=by_state)
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    seen = []
    with session_scope(engine) as session, httpx.Client() as http:
        reports = seed_all_states(
            session, http, snapshot_dir=tmp_path, states=("MA", "RI", "VT"), progress=seen.append
        )
    assert [(r.state, r.fetched, r.kept) for r in reports] == [
        ("MA", 4, 2),
        ("RI", 0, 0),
        ("VT", 1, 1),
    ]
    assert reports[1].error is not None and "500" in reports[1].error
    assert reports[1].snapshot is None
    assert reports[0].snapshot.exists() and reports[2].snapshot.exists()
    assert len(seen) == 3
    with session_scope(engine) as session:
        assert [h.ccn for h in repo.list_hospitals(session, "VT")] == ["470003"]
        assert len(repo.list_hospitals(session)) == 3
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_registry.py -v`
Expected: FAIL with `ImportError: cannot import name 'STATES'`.

- [ ] **Step 3: Implement**

In `src/waive/atlas/registry.py`, after `NONPROFIT_PREFIX`, add:

```python
# The 50 states and the District of Columbia, as CMS spells them in the `state` column.
STATES: tuple[str, ...] = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL",
    "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME",
    "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI",
    "WY",
)  # fmt: skip
```

Replace `SeedReport` and `seed_state` with:

```python
@dataclass(frozen=True)
class SeedReport:
    fetched: int
    kept: int
    snapshot: Path | None
    state: str = ""
    error: str | None = None


def seed_state(
    session: Session, state: str, http: httpx.Client, snapshot_dir: Path = Path("data/seed")
) -> SeedReport:
    rows = fetch_cms_rows(state, http)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).date().isoformat()
    snapshot = snapshot_dir / f"cms-hospitals-{state.upper()}-{stamp}.json"
    snapshot.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    kept = 0
    for row in rows:
        if is_eligible(row):
            repo.upsert_hospital(session, row_to_hospital(row))
            kept += 1
    return SeedReport(fetched=len(rows), kept=kept, snapshot=snapshot, state=state.upper())


def seed_all_states(
    session: Session,
    http: httpx.Client,
    snapshot_dir: Path = Path("data/seed"),
    states: tuple[str, ...] = STATES,
    progress: Callable[[SeedReport], None] | None = None,
) -> list[SeedReport]:
    """Seed every state in turn (free: the CMS datastore needs no key). A state whose request
    fails is reported with `error` and skipped; the others still land. Commits after each state
    so a crash mid-way keeps what was seeded."""
    reports: list[SeedReport] = []
    for state in states:
        try:
            report = seed_state(session, state, http, snapshot_dir)
        except (httpx.HTTPError, ValueError, KeyError) as error:
            report = SeedReport(
                fetched=0,
                kept=0,
                snapshot=None,
                state=state.upper(),
                error=f"{type(error).__name__}: {error}"[:200],
            )
        session.commit()
        reports.append(report)
        if progress is not None:
            progress(report)
    return reports
```

Add `from collections.abc import Callable` to the module imports.

- [ ] **Step 4: Run the registry tests**

Run: `uv run pytest tests/unit/test_registry.py -v`
Expected: 7 passed (the existing `test_seed_state_keeps_eligible_and_writes_snapshot` still passes: `report.snapshot` is a `Path` on success).

- [ ] **Step 5: Extend the CLI**

Replace `atlas_seed` in `src/waive/cli.py` with (add `from waive.atlas.registry import seed_all_states, seed_state` to the imports, replacing the existing `seed_state` import):

```python
@atlas_app.command("seed")
def atlas_seed(
    state: str | None = typer.Option(None, "--state", help="Two-letter state code"),
    all_states: bool = typer.Option(False, "--all-states", help="All 50 states and DC"),
) -> None:
    """Load nonprofit acute-care and critical-access hospitals from CMS into the registry.
    Free: the CMS datastore API needs no key."""
    settings = Settings()
    if not state and not all_states:
        raise typer.BadParameter("give --state XX or --all-states")
    with session_scope(_engine(settings)) as session, httpx.Client() as http:
        if state:
            report = seed_state(session, state, http)
            console.print(
                f"Fetched {report.fetched} {state.upper()} hospitals; kept {report.kept} "
                f"nonprofit acute-care/critical-access. Snapshot: {report.snapshot}"
            )
            return
        reports = seed_all_states(
            session,
            http,
            progress=lambda r: console.print(
                f"{r.state}: {r.kept}/{r.fetched} kept" + (f" — {r.error}" if r.error else "")
            ),
        )
    table = Table("State", "Fetched", "Kept", "Snapshot")
    for report in reports:
        table.add_row(
            report.state,
            str(report.fetched),
            str(report.kept),
            report.error or (report.snapshot.name if report.snapshot else ""),
        )
    table.add_row(
        "total", str(sum(r.fetched for r in reports)), str(sum(r.kept for r in reports)), ""
    )
    console.print(table)
    failed = [r.state for r in reports if r.error]
    if failed:
        console.print(f"Re-run with --state for: {', '.join(failed)}")
```

- [ ] **Step 6: Lint, format, full suite, commit**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest`
Expected: about 278 tests pass.

```bash
git add src/waive/atlas/registry.py src/waive/cli.py tests/unit/test_registry.py
git commit -m "feat: national registry seed from CMS for all states and DC (7.3)"
```

- [ ] **Step 7: Live seed (free; no gate needed)**

Run: `uv run waive atlas seed --all-states`
Expected: 51 lines, a totals row of roughly 5,000 fetched and 2,500–3,000 kept (nonprofit acute-care and critical-access; the spec's §2 figure is 2,978 nonprofit community hospitals). Snapshots `data/seed/cms-hospitals-XX-2026-10-DD.json` (estimated 5–8 MB in total). Re-run any failed state with `--state XX`.

Then: `uv run waive atlas schedule --dry-run --state CA --top 5` prints never-scouted California hospitals with priority 45.0 and spends nothing.

Commit the snapshots and record the totals in `docs/PROGRESS.md` (the orchestrator does this): 

```bash
git add data/seed/
git commit -m "data: CMS registry snapshots for all states and DC"
```

If the snapshots exceed 20 MB, add `/data/seed/cms-hospitals-*.json` to `.gitignore` instead, keep only the MA snapshot tracked, and say so in PROGRESS.

---

### Task 7.4: Budgeted national scouting (gate U7.1)

**Files:**
- Modify: `src/waive/config.py`, `.env.example`, `src/waive/atlas/schedule.py`, `src/waive/cli.py`
- Create: `docs/reports/atlas-national.md` (by hand, run log only; 7.7 generates the rest)
- Test: `tests/unit/test_schedule.py` (add), `tests/unit/test_cli_gates.py` (create)

**Interfaces:**
- Consumes: `build_queue`, `run_entries`, `repo.ccns_with_sheets`, `Settings`.
- Produces (`waive.config.Settings`): `national_scouting: Literal["on", "off"] = "off"`.
- Produces (`waive.atlas.schedule`): `STATE_POPULATION_ORDER: tuple[str, ...]`; `Order = Literal["population", "demand"]`; `national_entries(session, today, order="population") -> list[QueueEntry]` (hospitals without a sheet); `national_batch(session, gateway, ai, governor, today, *, daily_cap, limit, order="population") -> RunReport`.
- Produces (CLI): `waive atlas build --national --limit N [--order population|demand] [--daily-cap N]`; refuses with exit code 2 while `WAIVE_NATIONAL_SCOUTING=off`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_schedule.py`:

```python
def test_national_entries_order_by_state_population_or_demand():
    from waive.atlas.schedule import STATE_POPULATION_ORDER, national_entries

    assert len(STATE_POPULATION_ORDER) == 51 and STATE_POPULATION_ORDER[:3] == ("CA", "TX", "FL")
    california = {**NEW, "ccn": "050010", "state": "CA", "name": "GOLDEN HOSPITAL"}
    wyoming = {**NEW, "ccn": "530010", "state": "WY", "name": "ALPINE HOSPITAL"}
    engine = make_engine_with_hospital(REAL, california, wyoming)
    with session_scope(engine) as session:
        # REAL already has a sheet; it is refreshed by the scheduler, never part of a batch.
        from waive.atlas.pipeline import build_hospital

        build_hospital(session, FakeGateway(), FakeAI(), "220031", TODAY)
        session.add(CaseRow(id="c1", state="WY", ccn="530010", status="evaluated"))
        session.flush()
        by_population = national_entries(session, TODAY)
        assert [e.ccn for e in by_population] == ["050010", "530010"]
        by_demand = national_entries(session, TODAY, order="demand")
        assert [e.ccn for e in by_demand] == ["530010", "050010"]  # the open case wins


def test_national_batch_respects_limit_and_budget(tmp_path):
    from waive.atlas.schedule import national_batch

    california = {**NEW, "ccn": "050010", "state": "CA", "name": "GOLDEN HOSPITAL"}
    texas = {**NEW, "ccn": "450010", "state": "TX", "name": "LONE STAR HOSPITAL"}
    engine = make_engine_with_hospital(california, texas)
    governor = make_governor_for(tmp_path)
    with session_scope(engine) as session:
        report = national_batch(
            session, SpendingGateway(governor), FakeAI(), governor, TODAY, daily_cap=100, limit=1
        )
        assert [(r.ccn, r.outcome) for r in report.results] == [("050010", "published")]
        assert report.stopped == "limit reached"
        report = national_batch(
            session, SpendingGateway(governor), FakeAI(), governor, TODAY, daily_cap=18, limit=5
        )
        assert report.results == [] and report.stopped == "daily budget"  # 15 of 18 already spent
```

`tests/unit/test_cli_gates.py`:

```python
from typer.testing import CliRunner

from waive.cli import app


def test_national_build_refuses_until_gate_u71_is_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .env here: settings come from the environment only
    monkeypatch.setenv("WAIVE_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("WAIVE_LEDGER_PATH", str(tmp_path / "usage.jsonl"))
    monkeypatch.delenv("WAIVE_NATIONAL_SCOUTING", raising=False)
    result = CliRunner().invoke(app, ["atlas", "build", "--national", "--limit", "1"])
    assert result.exit_code == 2
    assert "U7.1" in result.output and "WAIVE_NATIONAL_SCOUTING" in result.output


def test_build_needs_a_state_or_national(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["atlas", "build"])
    assert result.exit_code == 2
    assert "--state" in result.output
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_schedule.py tests/unit/test_cli_gates.py -v`
Expected: the two schedule tests FAIL with `ImportError` (no `national_entries`); the CLI tests FAIL (`--national` is not a known option; `--state` is required by Typer with a different message).

- [ ] **Step 3: Add the setting**

In `src/waive/config.py`, after `scout_daily_credits`, add:

```python
    # Gate U7.1 in code: `waive atlas build --national` refuses to run while this is off. The user
    # sets it to "on" in .env when approving the national Tavily budget.
    national_scouting: Literal["on", "off"] = "off"
```

In `.env.example`, after `WAIVE_SCOUT_DAILY_CREDITS=50`, add:

```
# Gate U7.1: set to on only after approving the national Tavily budget (and raise WAIVE_TAVILY_CREDIT_CAP)
WAIVE_NATIONAL_SCOUTING=off
```

- [ ] **Step 4: Implement the national batch**

Append to `src/waive/atlas/schedule.py`:

```python
# 2020 census ranking, largest first (from memory; verify against census.gov before relying on
# the exact order — it only decides which state's hospitals a batch reaches first).
STATE_POPULATION_ORDER: tuple[str, ...] = (
    "CA", "TX", "FL", "NY", "PA", "IL", "OH", "GA", "NC", "MI",
    "NJ", "VA", "WA", "AZ", "MA", "TN", "IN", "MD", "MO", "WI",
    "CO", "MN", "SC", "AL", "LA", "KY", "OR", "OK", "CT", "UT",
    "IA", "NV", "AR", "MS", "KS", "NM", "NE", "ID", "WV", "HI",
    "NH", "ME", "RI", "MT", "DE", "SD", "ND", "AK", "DC", "VT",
    "WY",
)  # fmt: skip

Order = Literal["population", "demand"]


def national_entries(session: Session, today: date, order: Order = "population") -> list[QueueEntry]:
    """Hospitals without any sheet yet, in the order a national batch should take them:
    by state population (coverage where most people are) or by queue priority (where bills and
    cases already point)."""
    sheeted = repo.ccns_with_sheets(session)
    pending = [entry for entry in build_queue(session, today).entries if entry.ccn not in sheeted]
    if order == "population":
        rank = {state: index for index, state in enumerate(STATE_POPULATION_ORDER)}
        pending.sort(key=lambda e: (rank.get(e.state, len(rank)), -e.priority, e.name))
    return pending


def national_batch(
    session: Session,
    gateway: TavilyGateway,
    ai: AIClient,
    governor: Governor,
    today: date,
    *,
    daily_cap: int,
    limit: int,
    order: Order = "population",
) -> RunReport:
    """One budgeted batch of first-time scouts (gate U7.1). About CREDITS_PER_HOSPITAL each."""
    entries = national_entries(session, today, order)
    return run_entries(
        session, gateway, ai, governor, today, entries, daily_cap=daily_cap, limit=limit
    )
```

- [ ] **Step 5: Extend `waive atlas build`**

In `src/waive/cli.py`, add `national_batch` to the `waive.atlas.schedule` import line and replace `atlas_build` with:

```python
@atlas_app.command("build")
def atlas_build(
    state: str | None = typer.Option(None, "--state"),
    national: bool = typer.Option(
        False, "--national", help="Hospitals without a sheet, any state (gate U7.1)"
    ),
    order: str = typer.Option("population", "--order", help="population or demand"),
    daily_cap: int | None = typer.Option(
        None, "--daily-cap", help="Override WAIVE_SCOUT_DAILY_CREDITS for this run"
    ),
    limit: int | None = typer.Option(None, "--limit", help="Max hospitals this run"),
    ccn: str | None = typer.Option(None, "--ccn", help="Build one hospital"),
    dual: bool = typer.Option(
        True, "--dual/--no-dual", help="Cross-check critical fields with the fast model"
    ),
    rebuild: bool = typer.Option(
        False, "--rebuild", help="Also rebuild hospitals that already have a sheet"
    ),
    reuse_sources: bool = typer.Option(
        False, "--reuse-sources", help="Re-structure from stored documents; no Tavily spend"
    ),
) -> None:
    """Discover, scout, structure, verify and publish procedure sheets. Spends Tavily credits."""
    if not (state or national or ccn):
        raise typer.BadParameter("give --state XX, --ccn NNNNNN or --national")
    settings = Settings()
    if national and settings.national_scouting != "on":
        console.print(
            "National scouting is behind gate U7.1: approve the national Tavily budget, raise "
            "WAIVE_TAVILY_CREDIT_CAP in .env and set WAIVE_NATIONAL_SCOUTING=on. Nothing was spent."
        )
        raise typer.Exit(code=2)
    if order not in ("population", "demand"):
        raise typer.BadParameter("--order must be population or demand")
    governor = make_governor(settings)
    ai = AIClient(settings, governor)
    gateway = make_tavily_gateway(settings, governor)
    today = datetime.now(UTC).date()
    stopped = None
    with session_scope(_engine(settings)) as session:
        if ccn:
            results = [
                build_hospital(
                    session, gateway, ai, ccn, today, dual=dual, reuse_sources=reuse_sources
                )
            ]
        elif national:
            report = national_batch(
                session,
                gateway,
                ai,
                governor,
                today,
                daily_cap=daily_cap or settings.scout_daily_credits,
                limit=limit or 10,
                order=order,  # type: ignore[arg-type]
            )
            results, stopped = report.results, report.stopped
        else:
            results = build_state(
                session,
                gateway,
                ai,
                state,
                today,
                limit=limit,
                dual=dual,
                only_missing=not rebuild,
                reuse_sources=reuse_sources,
            )
    table = Table("CCN", "Hospital", "Outcome", "Version", "Notes")
    for result in results:
        table.add_row(
            result.ccn,
            result.name,
            result.outcome,
            str(result.version or ""),
            "; ".join(result.notes)[:120],
        )
    console.print(table)
    if stopped:
        console.print(f"Stopped: {stopped}")
    tavily_credits, _ = governor.summary()["tavily"]
    _, tf_usd = governor.summary()["token_factory"]
    console.print(f"Spend so far: {tavily_credits} Tavily credits, ${tf_usd:.4f} Token Factory")
```

Note the gate check happens before `AIClient`/`make_tavily_gateway` are built, so the refusal needs no keys (the CLI test has none).

- [ ] **Step 6: Run the tests, lint, format**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest`
Expected: about 282 tests pass.

- [ ] **Step 7: Create the run log file**

Write `docs/reports/atlas-national.md` with exactly this content (task 7.7's generator keeps everything from the `## Run log` heading on and regenerates the part above it):

```markdown
# Atlas coverage — national

Generated by `waive atlas report --national` from task 7.7 on. Until then this file holds the run log only.

## Run log

| Date (ET) | Batch | Hospitals | Published | Held | Credits before → after | Notes |
|---|---|---|---|---|---|---|
```

- [ ] **Step 8: Commit**

```bash
git add src/waive/config.py .env.example src/waive/atlas/schedule.py src/waive/cli.py tests/unit/test_schedule.py tests/unit/test_cli_gates.py docs/reports/atlas-national.md
git commit -m "feat: budgeted national scouting batches behind gate U7.1 (7.4)"
```

- [ ] **Step 9: Open gate U7.1 (the orchestrator; no spend)**

Run `uv run waive atlas schedule --dry-run --top 0` with `WAIVE_SCHEDULER_STATES=` empty for this one command (`WAIVE_SCHEDULER_STATES= uv run waive atlas schedule --dry-run --top 0`) to read "N without a sheet" and the credit estimate (5 × N; expect roughly 12,000–15,000 credits for 2,500–3,000 hospitals — above the master plan's 4,800–6,600 estimate because Phase 2 measured 4–5 credits per hospital rather than 2–3). Put in `docs/PROGRESS.md` under gate **U7.1**: the count, the estimate, the ask (approve a total; suggest a first tranche such as 1,000 credits ≈ 200 hospitals in population order), and the two `.env` changes the user makes to close it: raise `WAIVE_TAVILY_CREDIT_CAP` to the approved total and set `WAIVE_NATIONAL_SCOUTING=on`. STOP here until the user answers.

- [ ] **Step 10: Live batches (after U7.1; the loop runs one per iteration)**

Each iteration: `uv run waive atlas build --national --limit 10 --daily-cap 100` (≈ 50 credits; `--daily-cap` lets the orchestrator run two batches a day while the in-app scheduler keeps its own 50), then `uv run waive atlas report --national` (from 7.7 on), then append one row to the run log table in `docs/reports/atlas-national.md`:

```
| 2026-10-DD HH:MM | population #1 | 10 | 6 | 4 | 389 → 441 | CA; two held: no income rules in reachable text |
```

Update the Spend table in `docs/PROGRESS.md` from the `Spend so far` line and commit (`docs: national batch N`). Stop batches when the approved total is within 50 credits of the cap; the governor's `ensure_tavily` is the hard stop.

---

### Task 7.5: State repositories as sources (California HCAI, Washington DOH)

**Files:**
- Create: `src/waive/atlas/state_sources.py`
- Modify: `src/waive/atlas/overlays.py` (`is_overlay_source`), `src/waive/atlas/fetch.py` (`download_text(assume_pdf=)`), `src/waive/atlas/pipeline.py` (repository documents in `build_hospital`), `src/waive/learning/contributions.py` (use `is_overlay_source`)
- Test: `tests/unit/test_state_sources.py` (create), `tests/unit/test_fetch.py` (add one test)

**Interfaces:**
- Consumes: `ScoutedDoc`, `TITLES`, `MAX_CHARS`, `MIN_CHARS`, `DocClass` from `scout`; `download_text`, `USER_AGENT` from `fetch`; `repo.save_source`; `SourceDoc`, `SourceKind.STATE_REPOSITORY`; `TavilyGateway.search`.
- Produces (`waive.atlas.overlays`): `OVERLAY_ID_PREFIX = "state-"`; `is_overlay_source(source: SourceDoc) -> bool`.
- Produces (`waive.atlas.fetch`): `download_text(url, http, *, max_bytes=15_000_000, timeout=30.0, assume_pdf=False)`.
- Produces (`waive.atlas.state_sources`): constants `HCAI_LOOKUP_URL`, `HCAI_HOSPITAL_URL`, `HCAI_PAGE_PREFIX`, `HCAI_ATTACHMENT_PREFIX`, `WA_DOH_POLICIES_URL`, `NAME_MATCH = 88`, `PURPOSE = "atlas.state_repository"`; `slugify(name) -> str`; `classify_hcai(label) -> DocClass | None`; `fetch_html(http, url) -> str | None`; `class CaliforniaHcai` and `class WashingtonDoh`, each with `state: str` and `find_documents(hospital: HospitalRef, gateway, http) -> list[ScoutedDoc]`; `STATE_REPOSITORIES: dict[str, CaliforniaHcai | WashingtonDoh]`; `repository_documents(hospital, gateway, http=None) -> list[ScoutedDoc]`; `repository_source_id(state, doc_class, sha256) -> str` (`repo-ca-fap-<sha12>`); `store_repository_docs(session, ccn, state, docs, today) -> list[SourceDoc]`.
- `build_hospital` behaviour: after web scouting (or when no official domain was found), a CA/WA hospital also receives its repository documents (de-duplicated by SHA-256); a hospital with no domain and no repository documents is still `skipped` with a `domain` review item.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_fetch.py`:

```python
@respx.mock
def test_download_text_can_be_told_the_body_is_a_pdf():
    url = "https://api.hdc.hcai.ca.gov/Public/Extract/Attachment?id=7b714d73"
    respx.get(url).mock(return_value=pdf_response(sample_pdf(), "application/octet-stream"))
    with httpx.Client() as http:
        assert download_text(url, http) is None  # no .pdf marker, generic type: not trusted
        text = download_text(url, http, assume_pdf=True)
    assert text is not None and "250% of the Federal Poverty" in text
```

`tests/unit/test_state_sources.py`:

```python
import httpx
import pytest
import respx

from waive.atlas import repo
from waive.atlas.overlays import is_overlay_source
from waive.atlas.pipeline import build_hospital
from waive.atlas.samples import SAMPLE_POLICY_TEXT
from waive.atlas.schema import HospitalRef, SourceDoc, SourceKind
from waive.atlas.scout import ScoutedDoc
from waive.atlas.state_sources import (
    HCAI_ATTACHMENT_PREFIX,
    HCAI_HOSPITAL_URL,
    WA_DOH_POLICIES_URL,
    CaliforniaHcai,
    WashingtonDoh,
    classify_hcai,
    repository_documents,
    slugify,
    store_repository_docs,
)
from waive.atlas.tavily_gateway import SearchHit
from waive.db import session_scope
from waive.learning.contributions import NoTavily

from tests.unit.test_fetch import pdf_response, sample_pdf
from tests.unit.test_pipeline import HOSPITAL, POLICY_TEXT, TODAY, FakeAI, make_engine_with_hospital

CA_HOSPITAL = HospitalRef(
    ccn="050425",
    name="USC ARCADIA HOSPITAL",
    city="ARCADIA",
    state="CA",
    zip="91007",
    phone="626-555-0100",
    ownership="Voluntary non-profit - Private",
)
WA_HOSPITAL = HospitalRef(
    ccn="501339",
    name="ARBOR HEALTH MORTON HOSPITAL",
    city="MORTON",
    state="WA",
    zip="98356",
    phone=None,
    ownership="Voluntary non-profit - Other",
)
CHARITY = HCAI_ATTACHMENT_PREFIX + "7b714d73"
APPLICATION = HCAI_ATTACHMENT_PREFIX + "bc6c99b7"
DEBT = HCAI_ATTACHMENT_PREFIX + "5be7ea8a"
HCAI_PAGE = f"""<html><head><title>USC ARCADIA HOSPITAL - HCAI</title></head><body>
<h1>Hospital Fair Pricing Policy Lookup</h1><h2>USC ARCADIA HOSPITAL</h2>
<p>300 West Huntington Drive, Arcadia, CA 91007. HCAI ID: 106190529</p>
<h3>Charity Care Policy</h3><p>Effective 07/29/2026 <a href="{CHARITY}">Download</a></p>
<h3>Discount Payment Policy</h3><p>Effective 07/29/2026 <a href="{CHARITY}">Download</a></p>
<h3>Application for Charity Care &amp; Discount Payment</h3><p><a href="{APPLICATION}">Download</a></p>
<h3>Debt Collection Policy</h3><p><a href="{DEBT}">Download</a></p>
<p><a href="/affordability/hospital-fair-billing-program/">Back to the program page</a></p>
</body></html>"""
APPLICATION_TEXT = (
    "Application for Charity Care and Discount Payment. Please complete every section. "
    "Household size, monthly income from all sources, and the documents you attach. "
    "Return the form to Patient Financial Services within 30 days of the request.\n"
)
DEBT_TEXT = (
    "Debt Collection Policy. The hospital will not send a bill to collections before 180 days "
    "after the first statement and will not do so while an application for financial "
    "assistance is pending. Collection agencies must follow this policy.\n"
)
DOH_PAGE = """<html><body><table>
<thead><tr><th>Hospital</th><th>City</th><th>County</th><th>Policies</th></tr></thead>
<tbody>
<tr><td>Arbor Health - Morton Hospital</td><td>Morton</td><td>Lewis</td>
<td><a href="/sites/default/files/hospital-policies/MortonAD.pdf">Admissions</a>
<a href="/sites/default/files/2024-07/CCP173.pdf">Charity Care</a></td></tr>
<tr><td>Arbor Health - Other Hospital</td><td>Olympia</td><td>Thurston</td>
<td><a href="/sites/default/files/2024-07/CCP999.pdf">Charity Care</a></td></tr>
</tbody></table></body></html>"""
WA_CHARITY_URL = "https://doh.wa.gov/sites/default/files/2024-07/CCP173.pdf"


class SearchOnlyGateway:
    def __init__(self, hits):
        self.hits = hits
        self.queries = []

    def search(self, query, **kwargs):
        self.queries.append((query, kwargs))
        return self.hits

    def extract(self, urls, **kwargs):
        raise AssertionError("state repositories never use Tavily Extract")


def test_slugify_matches_hcai_urls():
    assert slugify("USC ARCADIA HOSPITAL") == "usc-arcadia-hospital"
    assert slugify("ADVENTIST HEALTH AND RIDEOUT") == "adventist-health-and-rideout"
    assert slugify("CHILDREN'S HOSPITAL OF LOS ANGELES") == "childrens-hospital-of-los-angeles"
    assert slugify("ST. JOHN'S REGIONAL MEDICAL CTR / OXNARD") == "st-johns-regional-medical-ctr-oxnard"
    assert HCAI_HOSPITAL_URL.format(slug="usc-arcadia-hospital").endswith("/usc-arcadia-hospital/")


def test_classify_hcai_labels():
    assert classify_hcai("Download Charity Care Policy") == "fap"
    assert classify_hcai("Discount Payment Policy") == "fap"
    assert classify_hcai("Application for Charity Care & Discount Payment") == "application"
    assert classify_hcai("Debt Collection Policy") == "billing"
    assert classify_hcai("Back to the program page") is None


@respx.mock
def test_hcai_slug_page_yields_deduplicated_documents_without_tavily():
    respx.get(HCAI_HOSPITAL_URL.format(slug="usc-arcadia-hospital")).mock(
        return_value=httpx.Response(200, text=HCAI_PAGE, headers={"content-type": "text/html"})
    )
    respx.get(CHARITY).mock(return_value=pdf_response(sample_pdf(), "application/octet-stream"))
    respx.get(APPLICATION).mock(return_value=pdf_response(sample_pdf(APPLICATION_TEXT)))
    respx.get(DEBT).mock(return_value=pdf_response(sample_pdf(DEBT_TEXT)))
    with httpx.Client() as http:
        docs = CaliforniaHcai().find_documents(CA_HOSPITAL, NoTavily(), http)
    assert [(d.doc_class, d.url) for d in docs] == [
        ("fap", CHARITY),
        ("application", APPLICATION),
        ("billing", DEBT),
    ]
    assert "250% of the Federal Poverty" in docs[0].text
    assert docs[0].title == "Charity Care Policy"
    assert len({d.sha256 for d in docs}) == 3


@respx.mock
def test_hcai_falls_back_to_one_tavily_search_when_the_slug_misses():
    guessed = HCAI_HOSPITAL_URL.format(slug="usc-arcadia-hospital")
    actual = HCAI_HOSPITAL_URL.format(slug="usc-arcadia-hospital-formerly-methodist")
    respx.get(guessed).mock(return_value=httpx.Response(404))
    respx.get(actual).mock(
        return_value=httpx.Response(200, text=HCAI_PAGE, headers={"content-type": "text/html"})
    )
    respx.get(CHARITY).mock(return_value=pdf_response(sample_pdf()))
    respx.get(APPLICATION).mock(return_value=httpx.Response(404))
    respx.get(DEBT).mock(return_value=httpx.Response(404))
    gateway = SearchOnlyGateway(
        [
            SearchHit("https://hcai.ca.gov/affordability/hospital-fair-billing-program/", "x", "", 0.9),
            SearchHit(actual, "USC ARCADIA HOSPITAL - HCAI", "", 0.8),
        ]
    )
    with httpx.Client() as http:
        docs = CaliforniaHcai().find_documents(CA_HOSPITAL, gateway, http)
    assert [d.doc_class for d in docs] == ["fap"]
    [(query, kwargs)] = gateway.queries
    assert "USC ARCADIA HOSPITAL" in query
    assert kwargs["include_domains"] == ["hcai.ca.gov"]
    assert kwargs["purpose"] == "atlas.state_repository"


@respx.mock
def test_hcai_rejects_a_page_about_another_hospital():
    other = HCAI_PAGE.replace("USC ARCADIA HOSPITAL", "COLLEGE HOSPITAL")
    respx.get(HCAI_HOSPITAL_URL.format(slug="usc-arcadia-hospital")).mock(
        return_value=httpx.Response(200, text=other, headers={"content-type": "text/html"})
    )
    with httpx.Client() as http:
        assert CaliforniaHcai().find_documents(CA_HOSPITAL, SearchOnlyGateway([]), http) == []


@respx.mock
def test_doh_table_row_gives_the_charity_care_pdf():
    respx.get(WA_DOH_POLICIES_URL).mock(
        return_value=httpx.Response(200, text=DOH_PAGE, headers={"content-type": "text/html"})
    )
    respx.get(WA_CHARITY_URL).mock(return_value=pdf_response(sample_pdf()))
    repository = WashingtonDoh()
    with httpx.Client() as http:
        docs = repository.find_documents(WA_HOSPITAL, NoTavily(), http)
        assert [(d.doc_class, d.url) for d in docs] == [("fap", WA_CHARITY_URL)]
        assert docs[0].title == "Charity care policy filed with Washington DOH"
        unknown = WA_HOSPITAL.model_copy(update={"name": "SEATTLE GENERAL", "city": "SEATTLE"})
        assert repository.find_documents(unknown, NoTavily(), http) == []
        # The same city-less name must not match a different city's row.
        elsewhere = WA_HOSPITAL.model_copy(update={"city": "SPOKANE"})
        assert repository.find_documents(elsewhere, NoTavily(), http) == []
    assert respx.calls.call_count == 2  # the table was fetched once and cached


def test_store_repository_docs_uses_repo_ids_and_the_overlay_check_ignores_them():
    engine = make_engine_with_hospital({**HOSPITAL, "state": "CA"})
    doc = ScoutedDoc(CHARITY, "fap", "Charity Care Policy", SAMPLE_POLICY_TEXT, "f" * 64)
    with session_scope(engine) as session:
        [source] = store_repository_docs(session, "229999", "CA", [doc], TODAY)
        assert source.id == "repo-ca-fap-" + "f" * 12
        assert source.kind is SourceKind.STATE_REPOSITORY
        assert not is_overlay_source(source)
        [(stored, text)] = repo.sources_for(session, "229999")
        assert stored == source and text == SAMPLE_POLICY_TEXT
    overlay = SourceDoc(
        id="state-mass-abc123",
        kind=SourceKind.STATE_REPOSITORY,
        url="https://www.mass.gov/x",
        title="x",
        fetched_on=TODAY,
        sha256="0" * 64,
    )
    assert is_overlay_source(overlay)


@pytest.fixture
def repository_stub(monkeypatch):
    doc = ScoutedDoc(CHARITY, "fap", "Charity Care Policy", POLICY_TEXT, "d" * 64)
    calls = []

    def fake(hospital, gateway, http=None):
        calls.append(hospital.ccn)
        return [doc] if hospital.state == "CA" else []

    monkeypatch.setattr("waive.atlas.pipeline.repository_documents", fake)
    return calls


def test_build_hospital_publishes_from_the_repository_when_no_domain_is_found(repository_stub):
    class NoDomainGateway:
        def search(self, query, **kwargs):
            return []

        def extract(self, urls, **kwargs):
            return []

    engine = make_engine_with_hospital({**HOSPITAL, "state": "CA", "website_domain": None})
    with session_scope(engine) as session:
        result = build_hospital(session, NoDomainGateway(), FakeAI(), "229999", TODAY)
        assert result.outcome == "published"
        assert "no official domain found" in result.notes
        sheet, _ = repo.latest_sheet(session, "229999")
        assert [s.kind for s in sheet.sources] == [SourceKind.STATE_REPOSITORY]
        assert sheet.eligibility.free_care_max_fpl.value == 250
        kinds = sorted(i.kind for i in repo.open_review_items(session, "229999"))
        assert kinds == ["domain", "verification"]
    assert repository_stub == ["229999"]


def test_build_hospital_outside_ca_and_wa_still_skips_without_a_domain(repository_stub):
    class NoDomainGateway:
        def search(self, query, **kwargs):
            return []

    engine = make_engine_with_hospital({**HOSPITAL, "website_domain": None})
    with session_scope(engine) as session:
        result = build_hospital(session, NoDomainGateway(), FakeAI(), "229999", TODAY)
        assert result.outcome == "skipped"
    assert repository_stub == []  # MA has no repository; nothing was asked


def test_repository_documents_returns_nothing_for_states_without_a_repository():
    assert repository_documents(HospitalRef(**{**CA_HOSPITAL.model_dump(), "state": "MA"}), NoTavily()) == []
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_state_sources.py tests/unit/test_fetch.py -v`
Expected: `test_state_sources.py` FAILS at import (`No module named 'waive.atlas.state_sources'`); the new fetch test FAILS with `TypeError: download_text() got an unexpected keyword argument 'assume_pdf'`.

- [ ] **Step 3: Small changes to `fetch.py`, `overlays.py`, `contributions.py`**

In `src/waive/atlas/fetch.py`, change the signature and the type check:

```python
def download_text(
    url: str,
    http: httpx.Client,
    *,
    max_bytes: int = 15_000_000,
    timeout: float = 30.0,
    assume_pdf: bool = False,
) -> str | None:
    """The text of the PDF at `url`, or None.

    None when the body is HTML (Tavily handles pages), is not a PDF, exceeds `max_bytes`, yields
    fewer than MIN_TEXT_CHARS characters, cannot be parsed, or when the request fails. Never raises.
    `assume_pdf` trusts a generic Content-Type when the caller knows the link is a document
    (state repositories serve attachments without a .pdf path).
    """
```

and inside the `with` block replace

```python
            if "html" in content_type.lower() or not looks_like_pdf(url, content_type):
                return None
```

with

```python
            if "html" in content_type.lower():
                return None
            if not assume_pdf and not looks_like_pdf(url, content_type):
                return None
```

In `src/waive/atlas/overlays.py`, after `STATE_OVERLAYS`, add:

```python
# Overlay sources are shared state pages cited for `programs.state_programs`; their ids start
# with this prefix (see fetch_overlay). Copies of a hospital's own policy from a state repository
# (task 7.5) use `repo-…` ids and are hospital documents like any other.
OVERLAY_ID_PREFIX = "state-"


def is_overlay_source(source: SourceDoc) -> bool:
    return source.kind is SourceKind.STATE_REPOSITORY and source.id.startswith(OVERLAY_ID_PREFIX)
```

In `src/waive/learning/contributions.py`, add `from waive.atlas.overlays import is_overlay_source` and change the filter in `rebuild_from_sources` to:

```python
    hospital_docs = [
        source for source, _ in repo.sources_for(session, ccn) if not is_overlay_source(source)
    ]
```

(`SourceKind` stays imported there for `source_for`.)

- [ ] **Step 4: Implement the repositories**

`src/waive/atlas/state_sources.py`:

```python
"""State repositories of hospital policies as document sources (spec §2 prior art, §8 step 6).

California's HCAI collects every hospital's charity care, discount payment and debt collection
policy (Health and Safety Code §§127400–127446); Washington's DOH publishes each hospital's
charity care policy. A copy from either repository is the hospital's own policy, so it feeds the
structurer like a document from the hospital's site, with `SourceKind.STATE_REPOSITORY` and an
id starting with `repo-` (the Massachusetts overlay uses `state-`, see overlays.is_overlay_source).

Verified 2026-10-02 (WebFetch): the HCAI lookup page, one HCAI per-hospital page (USC Arcadia:
attachment links to api.hdc.hcai.ca.gov/Public/Extract/Attachment?id=<guid> under the headings
Charity Care Policy, Discount Payment Policy, Application…, Debt Collection Policy) and the DOH
Hospital Policies table (one row per hospital: name, city, county, policy links such as
/sites/default/files/2024-07/CCP173.pdf). UNVERIFIED and marked below: the slug rule for every
hospital name, the exact HTML markup both sites use, attachment content types.
"""

import hashlib
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx
from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.fetch import USER_AGENT, download_text
from waive.atlas.schema import HospitalRef, SourceDoc, SourceKind
from waive.atlas.scout import MAX_CHARS, MIN_CHARS, TITLES, DocClass, ScoutedDoc
from waive.atlas.tavily_gateway import TavilyGateway

log = logging.getLogger(__name__)

HCAI_LOOKUP_URL = (
    "https://hcai.ca.gov/affordability/hospital-fair-billing-program/"
    "hospital-fair-pricing-policy-lookup/"
)
HCAI_PAGE_PREFIX = "https://hcai.ca.gov/affordability/hospital-billing-policies/"
HCAI_HOSPITAL_URL = HCAI_PAGE_PREFIX + "{slug}/"
HCAI_ATTACHMENT_PREFIX = "https://api.hdc.hcai.ca.gov/Public/Extract/Attachment?id="
WA_DOH_POLICIES_URL = (
    "https://doh.wa.gov/licenses-permits-and-certificates/facilities-z/hospitals/hospital-policies"
)
NAME_MATCH = 88
PURPOSE = "atlas.state_repository"
LABEL_TAGS = ("h1", "h2", "h3", "h4", "strong", "dt", "th")


def slugify(name: str) -> str:
    """HCAI's per-hospital path from a CMS name (UNVERIFIED beyond the examples in the tests:
    `usc-arcadia-hospital`, `adventist-health-and-rideout`). Apostrophes vanish, other
    punctuation becomes a hyphen."""
    text = re.sub(r"['’]", "", name.lower()).replace("&", " and ")
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", text)).strip("-")


def classify_hcai(label: str) -> DocClass | None:
    text = label.lower()
    if "application" in text:
        return "application"
    if "debt" in text or "collection" in text:
        return "billing"
    if "charity" in text or "discount" in text:
        return "fap"
    return None


def fetch_html(http: httpx.Client, url: str) -> str | None:
    """A 200 HTML page's text, else None; never raises."""
    try:
        response = http.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True
        )
    except Exception as error:  # network and URL errors alike: the caller has a fallback
        log.debug("fetch of %s failed: %s", url, type(error).__name__)
        return None
    if response.status_code != 200 or "html" not in response.headers.get("content-type", ""):
        return None
    return response.text


def _doc(url: str, doc_class: DocClass, title: str, text: str | None) -> ScoutedDoc | None:
    if text is None:
        return None
    text = text[:MAX_CHARS]
    if len(text.strip()) < MIN_CHARS:
        return None
    return ScoutedDoc(url, doc_class, title, text, hashlib.sha256(text.encode("utf-8")).hexdigest())


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("&", " and "))


class _Links(HTMLParser):
    """Anchors as (href, link text, label) where label is the last heading or bold text seen
    before the link — HCAI pages put a heading above each attachment (markup UNVERIFIED)."""

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.links: list[tuple[str, str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []
        self._label = ""
        self._label_parts: list[str] | None = None
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        href = dict(attrs).get("href")
        if tag == "a" and href:
            self._href, self._text = href, []
        elif tag in LABEL_TAGS:
            self._label_parts = []
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split()), self._label))
            self._href = None
        elif tag in LABEL_TAGS and self._label_parts is not None:
            self._label = " ".join("".join(self._label_parts).split()) or self._label
            self._label_parts = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)
        if self._label_parts is not None:
            self._label_parts.append(data)
        if self._in_title:
            self.title += data


class _Rows(HTMLParser):
    """Table rows as (cell texts, [(href, link text)]) — the DOH table has one row per hospital
    (markup UNVERIFIED beyond the column order name, city, county, policies)."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[list[str], list[tuple[str, str]]]] = []
        self._cells: list[str] | None = None
        self._links: list[tuple[str, str]] = []
        self._cell: list[str] | None = None
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._cells, self._links = [], []
        elif tag in ("td", "th") and self._cells is not None:
            self._cell = []
        elif tag == "a" and self._cells is not None:
            self._href, self._link_text = dict(attrs).get("href"), []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._cells is not None:
            self._cells.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "a" and self._href is not None:
            self._links.append((self._href, " ".join("".join(self._link_text).split())))
            self._href = None
        elif tag == "tr" and self._cells is not None:
            self.rows.append((self._cells, self._links))
            self._cells = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        if self._href is not None:
            self._link_text.append(data)


class CaliforniaHcai:
    """HCAI Hospital Fair Pricing Policy Lookup. The slug guess costs nothing; when it misses,
    one Tavily Search on hcai.ca.gov (1 credit) finds the hospital's page."""

    state = "CA"

    def find_documents(
        self, hospital: HospitalRef, gateway: TavilyGateway, http: httpx.Client
    ) -> list[ScoutedDoc]:
        page = self._page(hospital, gateway, http)
        if page is None:
            return []
        links = _Links()
        links.feed(page)
        # Search-result titles read "<NAME> - HCAI"; a page about another hospital is rejected.
        if fuzz.token_set_ratio(_norm(hospital.name), _norm(links.title)) < NAME_MATCH:
            return []
        docs: list[ScoutedDoc] = []
        seen: set[str] = set()
        for href, text, label in links.links:
            if not href.startswith(HCAI_ATTACHMENT_PREFIX):
                continue
            doc_class = classify_hcai(f"{text} {label}")
            if doc_class is None:
                continue
            title = label if classify_hcai(label) else (text or TITLES[doc_class])
            doc = _doc(href, doc_class, title, download_text(href, http, assume_pdf=True))
            if doc is not None and doc.sha256 not in seen:
                docs.append(doc)
                seen.add(doc.sha256)
        return docs

    def _page(
        self, hospital: HospitalRef, gateway: TavilyGateway, http: httpx.Client
    ) -> str | None:
        html = fetch_html(http, HCAI_HOSPITAL_URL.format(slug=slugify(hospital.name)))
        if html is not None:
            return html
        hits = gateway.search(
            f'"{hospital.name}" Hospital Fair Pricing Policy Lookup',
            purpose=PURPOSE,
            include_domains=["hcai.ca.gov"],
            max_results=5,
        )
        for hit in hits:
            if hit.url.startswith(HCAI_PAGE_PREFIX):
                return fetch_html(http, hit.url)
        return None


class WashingtonDoh:
    """Washington DOH Hospital Policies table. The table is fetched once per process (free) and
    the hospital's row is found by name and city; its "Charity Care" link is the policy PDF."""

    state = "WA"

    def __init__(self) -> None:
        self._rows: list[tuple[list[str], list[tuple[str, str]]]] | None = None

    def _table(self, http: httpx.Client) -> list[tuple[list[str], list[tuple[str, str]]]]:
        if self._rows is None:
            parser = _Rows()
            parser.feed(fetch_html(http, WA_DOH_POLICIES_URL) or "")
            self._rows = parser.rows
        return self._rows

    def find_documents(
        self, hospital: HospitalRef, gateway: TavilyGateway, http: httpx.Client
    ) -> list[ScoutedDoc]:
        best: tuple[float, list[tuple[str, str]]] | None = None
        for cells, links in self._table(http):
            if not cells:
                continue
            score = fuzz.token_set_ratio(_norm(hospital.name), _norm(cells[0]))
            city_ok = len(cells) < 2 or not cells[1] or hospital.city.lower() == cells[1].lower()
            if score >= NAME_MATCH and city_ok and (best is None or score > best[0]):
                best = (score, links)
        if best is None:
            return []
        docs: list[ScoutedDoc] = []
        for href, text in best[1]:
            if "charity" not in text.lower():
                continue
            url = urljoin(WA_DOH_POLICIES_URL, href)
            doc = _doc(
                url,
                "fap",
                "Charity care policy filed with Washington DOH",
                download_text(url, http, assume_pdf=True),
            )
            if doc is not None:
                docs.append(doc)
        return docs


STATE_REPOSITORIES: dict[str, CaliforniaHcai | WashingtonDoh] = {
    "CA": CaliforniaHcai(),
    "WA": WashingtonDoh(),
}


def repository_documents(
    hospital: HospitalRef, gateway: TavilyGateway, http: httpx.Client | None = None
) -> list[ScoutedDoc]:
    """The hospital's policy documents from its state's repository, or [] (no repository, not
    listed, or a failure — repositories are a bonus, never a reason to fail a build)."""
    repository = STATE_REPOSITORIES.get(hospital.state.upper())
    if repository is None:
        return []
    owned = http is None
    client = http or httpx.Client(follow_redirects=True, timeout=30.0)
    try:
        return repository.find_documents(hospital, gateway, client)
    except Exception as error:
        log.warning("state repository lookup failed for %s: %s", hospital.ccn, type(error).__name__)
        return []
    finally:
        if owned:
            client.close()


def repository_source_id(state: str, doc_class: str, sha256: str) -> str:
    return f"repo-{state.lower()}-{doc_class}-{sha256[:12]}"


def store_repository_docs(
    session: Session, ccn: str, state: str, docs: list[ScoutedDoc], today: date
) -> list[SourceDoc]:
    stored: list[SourceDoc] = []
    for doc in docs:
        source = SourceDoc(
            id=repository_source_id(state, doc.doc_class, doc.sha256),
            kind=SourceKind.STATE_REPOSITORY,
            url=doc.url,
            title=doc.title,
            fetched_on=today,
            sha256=doc.sha256,
        )
        repo.save_source(session, source, doc.text, ccn)
        stored.append(source)
    return stored
```

(The module's imports also need `from datetime import date`, placed with the standard-library imports.)

- [ ] **Step 5: Wire the repositories into `build_hospital`**

In `src/waive/atlas/pipeline.py`:

Add imports:

```python
from waive.atlas.overlays import is_overlay_source
from waive.atlas.state_sources import (
    STATE_REPOSITORIES,
    repository_documents,
    store_repository_docs,
)
```

Replace the domain-discovery block (from `if not row.website_domain:` through `session.flush()`) with:

```python
    if not row.website_domain:
        found = discover_domain(gateway, repo.hospital_ref(row))
        if found is None:
            repo.add_review_item(session, ccn, "domain", {"found": None})
            result.notes.append("no official domain found")
            if row.state.upper() not in STATE_REPOSITORIES:
                return result
            result.notes.append("trying the state repository")
        else:
            row.website_domain, row.domain_confidence = found.domain, found.confidence
            if found.confidence < MIN_CONFIDENCE:
                repo.add_review_item(
                    session, ccn, "domain", {"found": found.domain, "evidence": found.evidence_url}
                )
                result.notes.append(f"domain {found.domain} needs review")
            session.flush()
```

Replace the sources block (from `# State-repository documents are overlay citations` through the `no_documents` early return) with:

```python
    # Overlay citations (the shared mass.gov page) are not the hospital's policy and, at 100k+
    # characters, they drown the structurer; repository copies of the hospital's own policy stay.
    sources_with_text = (
        [
            (source, text)
            for source, text in repo.sources_for(session, ccn)
            if not is_overlay_source(source)
        ]
        if reuse_sources
        else []
    )
    if not sources_with_text:
        docs = scout_hospital(gateway, hospital) if hospital.website_domain else []
        sources = store_scouted(session, ccn, docs, today)
        sources_with_text = [(s, doc.text) for s, doc in zip(sources, docs, strict=True)]
        seen = {doc.sha256 for doc in docs}
        state_docs = [d for d in repository_documents(hospital, gateway) if d.sha256 not in seen]
        state_sources = store_repository_docs(session, ccn, hospital.state, state_docs, today)
        sources_with_text.extend(
            (s, doc.text) for s, doc in zip(state_sources, state_docs, strict=True)
        )
        if state_docs:
            result.notes.append(f"{len(state_docs)} document(s) from the state repository")
        if not sources_with_text:
            repo.add_review_item(session, ccn, "no_documents", {"domain": row.website_domain})
            result.notes.append("no financial assistance documents found")
            return result
```

`SourceKind` is no longer used in `pipeline.py`; remove it from that import line.

- [ ] **Step 6: Run the tests, lint, format**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest`
Expected: about 294 tests pass (11 new in `test_state_sources.py`, 1 in `test_fetch.py`), including the existing `test_reuse_sources_leaves_state_overlay_documents_out_of_the_structurer` (overlay ids still start with `state-`).

- [ ] **Step 7: Commit**

```bash
git add src/waive/atlas/state_sources.py src/waive/atlas/overlays.py src/waive/atlas/fetch.py src/waive/atlas/pipeline.py src/waive/learning/contributions.py tests/unit/test_state_sources.py tests/unit/test_fetch.py
git commit -m "feat: California HCAI and Washington DOH policy repositories as document sources (7.5)"
```

- [ ] **Step 8: Live check (after U7.1; 0–1 Tavily credits plus Token Factory)**

Pick one CA and one WA hospital from the registry (`uv run waive atlas schedule --dry-run --state CA --top 3`), then `uv run waive atlas build --ccn <CA ccn>` and `--ccn <WA ccn>`. Expected notes include "N document(s) from the state repository"; the sheet page lists a `state_repository` source whose URL is an `api.hdc.hcai.ca.gov` attachment or a `doh.wa.gov/sites/default/files/...pdf`. If the HCAI slug misses for most hospitals (`gateway.search` calls in the ledger with purpose `atlas.state_repository`), adjust `slugify` from the real URLs seen and note the rule in PROGRESS. Discovery and scouting of the hospital's own site still spend their usual 4–5 credits; the governor's cap check is `ensure_tavily` in every gateway call.

---

### Task 7.6: IRS Form 990 Schedule H cross-check (optional)

**Files:**
- Create: `src/waive/atlas/irs.py`
- Modify: `src/waive/db.py` (`HospitalRow.ein`), `src/waive/atlas/repo.py` (`HOSPITAL_FIELDS`), `src/waive/cli.py`
- Test: `tests/unit/test_irs.py` (create), `tests/unit/test_db_upgrade.py` (add)

**Interfaces:**
- Consumes: `repo.get_hospital`, `repo.latest_sheet`, `repo.add_review_item`, `fetch.download_text(..., assume_pdf=True)`, `fetch.USER_AGENT`, `httpx.Client`.
- Produces (`waive.db.HospitalRow`): `ein: Mapped[str | None]` (nullable `String(10)`; `waive db upgrade` adds it to existing databases).
- Produces (`waive.atlas.irs`): `PROPUBLICA_API`, `CHARITY_SUBSECTION = 3`, `NAME_MATCH = 85`; `EinMatch(ein, name, city, score)`; `IrsOrganization(ein, name, subsection_code, tax_year, pdf_url)`; `ScheduleHThresholds(free_care_fpl, discount_fpl)`; `IrsOutcome = Literal["confirmed", "match", "mismatch", "not_501c3", "not_found", "no_sheet", "no_thresholds"]`; `find_ein(name, city, state, http) -> EinMatch | None`; `organization(ein, http) -> IrsOrganization`; `schedule_h_thresholds(text) -> ScheduleHThresholds`; `crosscheck_hospital(session, ccn, http, *, download_pdf=False) -> IrsOutcome`.
- Review item kinds produced: `irs_not_found`, `irs_not_501c3`, `irs_mismatch`.
- Produces (CLI): `waive atlas irs --state XX [--limit N] [--pdf] [--pause SECONDS]` (free: no Tavily, no Token Factory).

What the ProPublica API can and cannot give (verified 2026-10-02, see the facts table): the EIN by name search, the organization's 501(c) subsection and the latest filing's PDF link — but **not** Schedule H fields. The thresholds therefore come from a best-effort read of the filing PDF's text: Schedule H Part I lines 3a/3b are checkboxes (100/150/200/Other % for free care; 200/250/300/350/400/Other % for discounted care) and text extraction usually loses which box is ticked. The parser only reports a percentage when exactly one appears after the line's wording (typically a typed "Other" value); otherwise the result is `no_thresholds` and nothing is flagged. This keeps the signal honest: a mismatch is raised only on an unambiguous reading.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_db_upgrade.py`:

```python
def test_upgrade_adds_the_hospital_ein_column():
    from sqlalchemy import inspect, text

    from waive.atlas import repo
    from waive.db import Base, make_engine, session_scope, upgrade_schema

    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE hospitals DROP COLUMN ein"))
    assert "ein" not in {c["name"] for c in inspect(engine).get_columns("hospitals")}
    assert upgrade_schema(engine) == ["hospitals.ein"]
    with session_scope(engine) as session:
        row = repo.upsert_hospital(
            session,
            {
                "ccn": "220031",
                "name": "REAL GENERAL HOSPITAL",
                "city": "WORCESTER",
                "state": "MA",
                "zip": "01608",
                "hospital_type": "Acute Care Hospitals",
                "ownership": "Voluntary non-profit - Private",
                "ein": "04-1234567",
            },
        )
        assert row.ein == "04-1234567"
```

`tests/unit/test_irs.py`:

```python
import httpx
import respx

from waive.atlas import repo
from waive.atlas.irs import (
    PROPUBLICA_API,
    ScheduleHThresholds,
    crosscheck_hospital,
    find_ein,
    organization,
    schedule_h_thresholds,
)
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.db import session_scope

from tests.unit.test_fetch import pdf_response, sample_pdf
from tests.unit.test_pipeline import HOSPITAL, make_engine_with_hospital

SEARCH_URL = f"{PROPUBLICA_API}/search.json"
ORG_URL = f"{PROPUBLICA_API}/organizations/123456789.json"
PDF_URL = "https://projects.propublica.org/nonprofits/download-filing?path=2024/990.pdf"
SEARCH = {
    "organizations": [
        {"ein": 987654321, "name": "St Example Foundation", "city": "BOSTON", "state": "MA"},
        {"ein": 123456789, "name": "St. Example Medical Center Inc", "city": "BOSTON", "state": "MA"},
        {"ein": 555555555, "name": "St. Example Medical Center", "city": "SPRINGFIELD", "state": "MA"},
    ]
}


def org_payload(subsection=3):
    return {
        "organization": {"ein": 123456789, "name": "ST EXAMPLE MEDICAL CENTER INC", "subsection_code": subsection},
        "filings_with_data": [
            {"tax_prd_yr": 2023, "totrevenue": 100, "pdf_url": "https://example.org/2023.pdf"},
            {"tax_prd_yr": 2024, "totrevenue": 120, "pdf_url": PDF_URL},
        ],
        "filings_without_data": [],
    }


FILLER = "Form 990 Return of Organization Exempt From Income Tax. " * 6
SCHEDULE_H_250 = (
    FILLER + "\nSchedule H (Form 990) 2024 Hospitals Part I Financial Assistance\n"
    "3a Did the organization use FPG to determine eligibility for free care? Yes. "
    "FPG family income limit for eligibility for free care: Other 250 %\n"
)
SCHEDULE_H_300 = SCHEDULE_H_250.replace("Other 250 %", "Other 300 %")
AMBIGUOUS = (
    "Schedule H Part I\n3a FPG family income limit for eligibility for free care: 100% 150% 200% Other %\n"
    "3b FPG family income limit for eligibility for discounted care: 200% 250% 300% 350% 400% Other %\n"
)


def test_schedule_h_thresholds_only_reports_an_unambiguous_percentage():
    assert schedule_h_thresholds(SCHEDULE_H_250) == ScheduleHThresholds(250, None)
    both = SCHEDULE_H_250 + "3b ... for discounted care: Other 400 %\n"
    assert schedule_h_thresholds(both) == ScheduleHThresholds(250, 400)
    assert schedule_h_thresholds(AMBIGUOUS) == ScheduleHThresholds(None, None)
    assert schedule_h_thresholds("no schedule here") == ScheduleHThresholds(None, None)


@respx.mock
def test_find_ein_needs_a_close_name_and_the_same_city():
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=SEARCH))
    with httpx.Client() as http:
        match = find_ein("ST. EXAMPLE MEDICAL CENTER", "BOSTON", "MA", http)
        assert (match.ein, match.city) == ("123456789", "BOSTON")
        assert find_ein("ST. EXAMPLE MEDICAL CENTER", "WORCESTER", "MA", http) is None
    params = route.calls.last.request.url.params
    assert params["q"] == "ST. EXAMPLE MEDICAL CENTER" and params["state[id]"] == "MA"


@respx.mock
def test_organization_picks_the_latest_filing():
    respx.get(ORG_URL).mock(return_value=httpx.Response(200, json=org_payload()))
    with httpx.Client() as http:
        org = organization("123456789", http)
    assert (org.subsection_code, org.tax_year, org.pdf_url) == (3, 2024, PDF_URL)


def seeded():
    engine = make_engine_with_hospital({**HOSPITAL, "website_domain": "example.org"})
    with session_scope(engine) as session:
        publish_sheet(session, st_example_sheet())
    return engine


@respx.mock
def test_crosscheck_stores_the_ein_and_confirms_501c3_without_a_pdf():
    search = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=SEARCH))
    respx.get(ORG_URL).mock(return_value=httpx.Response(200, json=org_payload()))
    engine = seeded()
    with session_scope(engine) as session, httpx.Client() as http:
        assert crosscheck_hospital(session, "229999", http) == "confirmed"
        assert repo.get_hospital(session, "229999").ein == "123456789"
        assert repo.open_review_items(session, "229999") == []
        # The EIN is kept: the second run skips the search.
        assert crosscheck_hospital(session, "229999", http) == "confirmed"
    assert search.call_count == 1


@respx.mock
def test_crosscheck_flags_a_mismatch_and_accepts_a_match():
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=SEARCH))
    respx.get(ORG_URL).mock(return_value=httpx.Response(200, json=org_payload()))
    pdf = respx.get(PDF_URL).mock(return_value=pdf_response(sample_pdf(SCHEDULE_H_300)))
    engine = seeded()
    with session_scope(engine) as session, httpx.Client() as http:
        assert crosscheck_hospital(session, "229999", http, download_pdf=True) == "mismatch"
        [item] = repo.open_review_items(session, "229999")
        assert item.kind == "irs_mismatch"
        assert item.detail["sheet_free_care_fpl"] == "250"
        assert item.detail["irs_free_care_fpl"] == 300
        assert item.detail["tax_year"] == 2024 and item.detail["pdf_url"] == PDF_URL
        pdf.mock(return_value=pdf_response(sample_pdf(SCHEDULE_H_250)))
        assert crosscheck_hospital(session, "229999", http, download_pdf=True) == "match"
        pdf.mock(return_value=pdf_response(sample_pdf(AMBIGUOUS + FILLER)))
        assert crosscheck_hospital(session, "229999", http, download_pdf=True) == "no_thresholds"


@respx.mock
def test_crosscheck_reports_non_charities_and_unknown_hospitals():
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=SEARCH))
    respx.get(ORG_URL).mock(return_value=httpx.Response(200, json=org_payload(subsection=4)))
    engine = seeded()
    with session_scope(engine) as session, httpx.Client() as http:
        assert crosscheck_hospital(session, "229999", http) == "not_501c3"
        [item] = repo.open_review_items(session, "229999")
        assert item.kind == "irs_not_501c3" and item.detail["subsection_code"] == 4
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"organizations": []}))
    engine = seeded()
    with session_scope(engine) as session, httpx.Client() as http:
        assert crosscheck_hospital(session, "229999", http) == "not_found"
        assert [i.kind for i in repo.open_review_items(session, "229999")] == ["irs_not_found"]
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_irs.py tests/unit/test_db_upgrade.py -v`
Expected: `test_irs.py` FAILS at import; the upgrade test FAILS because `ALTER TABLE hospitals DROP COLUMN ein` finds no such column.

- [ ] **Step 3: Add the column**

In `src/waive/db.py`, inside `HospitalRow` after `system`, add:

```python
    # Employer Identification Number from the IRS/ProPublica lookup (task 7.6); optional.
    ein: Mapped[str | None] = mapped_column(String(10), nullable=True)
```

In `src/waive/atlas/repo.py`, add `"ein",` to `HOSPITAL_FIELDS` (after `"system"`).

- [ ] **Step 4: Implement the lookup and cross-check**

`src/waive/atlas/irs.py`:

```python
"""IRS Form 990 cross-check through ProPublica's Nonprofit Explorer (spec §8 step 6; optional).

Verified 2026-10-02 against https://projects.propublica.org/nonprofits/api: API v2, GET only, no
key; `GET /organizations/<ein>.json` returns `organization`, `filings_with_data` (with
`tax_prd_yr`, `pdf_url`) and `filings_without_data`; `GET /search.json?q=&state[id]=` searches;
Schedule H fields are not exposed; PDF downloads are rate limited and use is subject to
ProPublica's Data Terms of Use. UNVERIFIED and marked below: the search response's key names,
the `subsection_code` field, and any text pattern in a filing PDF for Schedule H lines 3a/3b.
"""

import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

import httpx
from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.fetch import USER_AGENT, download_text

log = logging.getLogger(__name__)

PROPUBLICA_API = "https://projects.propublica.org/nonprofits/api/v2"
CHARITY_SUBSECTION = 3  # 501(c)(3)
NAME_MATCH = 85
# Schedule H, Part I, lines 3a and 3b: the wording, then the percentages on that line
# (UNVERIFIED against real filing text; tune with the first live PDF).
FREE_CARE_LINE = re.compile(r"free care[^\n]{0,240}", re.IGNORECASE)
DISCOUNTED_CARE_LINE = re.compile(r"discounted care[^\n]{0,240}", re.IGNORECASE)
PERCENT = re.compile(r"(\d{3})\s*%")
SCHEDULE_H_WINDOW = 20_000

IrsOutcome = Literal[
    "confirmed", "match", "mismatch", "not_501c3", "not_found", "no_sheet", "no_thresholds"
]


@dataclass(frozen=True)
class EinMatch:
    ein: str
    name: str
    city: str
    score: float


@dataclass(frozen=True)
class IrsOrganization:
    ein: str
    name: str
    subsection_code: int | None
    tax_year: int | None
    pdf_url: str | None


@dataclass(frozen=True)
class ScheduleHThresholds:
    free_care_fpl: int | None
    discount_fpl: int | None


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("&", " and "))


def _get_json(http: httpx.Client, url: str, params: dict[str, str] | None = None) -> Any:
    response = http.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=30.0)
    response.raise_for_status()
    return response.json()


def find_ein(name: str, city: str, state: str, http: httpx.Client) -> EinMatch | None:
    """The EIN of the organization whose name is close to the hospital's and whose city is the
    same. Conservative on purpose: hospital systems file under a parent, foundations share the
    name — those come back as None rather than as a wrong EIN."""
    payload = _get_json(http, f"{PROPUBLICA_API}/search.json", {"q": name, "state[id]": state.upper()})
    best: EinMatch | None = None
    for org in payload.get("organizations", []):  # UNVERIFIED key names: organizations[].ein/name/city
        candidate = str(org.get("name") or "")
        score = fuzz.token_set_ratio(_norm(name), _norm(candidate))
        if score < NAME_MATCH or str(org.get("city") or "").lower() != city.lower():
            continue
        if best is None or score > best.score:
            best = EinMatch(str(org.get("ein") or ""), candidate, str(org.get("city") or ""), score)
    return best if best is not None and best.ein else None


def organization(ein: str, http: httpx.Client) -> IrsOrganization:
    payload = _get_json(http, f"{PROPUBLICA_API}/organizations/{ein}.json")
    org = payload.get("organization") or {}
    filings = [f for f in payload.get("filings_with_data", []) if f.get("tax_prd_yr")]
    latest = max(filings, key=lambda f: int(f["tax_prd_yr"]), default=None)
    code = org.get("subsection_code")  # UNVERIFIED field name
    return IrsOrganization(
        ein=str(org.get("ein") or ein),
        name=str(org.get("name") or ""),
        subsection_code=int(code) if code is not None else None,
        tax_year=int(latest["tax_prd_yr"]) if latest else None,
        pdf_url=(latest or {}).get("pdf_url"),
    )


def _single(line: re.Pattern[str], text: str) -> int | None:
    values: set[int] = set()
    for match in line.finditer(text):
        values.update(int(value) for value in PERCENT.findall(match.group(0)))
    return values.pop() if len(values) == 1 else None


def schedule_h_thresholds(text: str) -> ScheduleHThresholds:
    """Best-effort read of Schedule H Part I lines 3a/3b from a filing's text. The lines are
    checkboxes, and extraction usually keeps every option's label, so a percentage is reported
    only when exactly one appears after the line's wording (a typed "Other" value)."""
    start = text.find("Schedule H")
    if start < 0:
        return ScheduleHThresholds(None, None)
    window = text[start : start + SCHEDULE_H_WINDOW]
    return ScheduleHThresholds(_single(FREE_CARE_LINE, window), _single(DISCOUNTED_CARE_LINE, window))


def crosscheck_hospital(
    session: Session, ccn: str, http: httpx.Client, *, download_pdf: bool = False
) -> IrsOutcome:
    """Store the EIN, confirm 501(c)(3) status and, with `download_pdf`, compare the sheet's free
    care limit with Schedule H. Review items only for findings; nothing for a clean result."""
    row = repo.get_hospital(session, ccn)
    if row is None:
        raise KeyError(ccn)
    if not row.ein:
        match = find_ein(row.name, row.city, row.state, http)
        if match is None:
            repo.add_review_item(session, ccn, "irs_not_found", {"name": row.name, "city": row.city})
            return "not_found"
        row.ein = match.ein
        session.flush()
    org = organization(row.ein, http)
    detail: dict[str, Any] = {
        "ein": row.ein,
        "irs_name": org.name,
        "subsection_code": org.subsection_code,
        "tax_year": org.tax_year,
    }
    if org.subsection_code is not None and org.subsection_code != CHARITY_SUBSECTION:
        repo.add_review_item(session, ccn, "irs_not_501c3", detail)
        return "not_501c3"
    found = repo.latest_sheet(session, ccn)
    if found is None:
        return "no_sheet"
    if not download_pdf or org.pdf_url is None:
        return "confirmed"
    thresholds = schedule_h_thresholds(download_text(org.pdf_url, http, assume_pdf=True) or "")
    cited = found[0].eligibility.free_care_max_fpl
    if thresholds.free_care_fpl is None or cited is None:
        return "no_thresholds"
    if Decimal(thresholds.free_care_fpl) == cited.value:
        return "match"
    repo.add_review_item(
        session,
        ccn,
        "irs_mismatch",
        {
            **detail,
            "sheet_free_care_fpl": str(cited.value),
            "irs_free_care_fpl": thresholds.free_care_fpl,
            "pdf_url": org.pdf_url,
        },
    )
    return "mismatch"
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/unit/test_irs.py tests/unit/test_db_upgrade.py -v`
Expected: all pass. If `sample_pdf(SCHEDULE_H_300)` text comes back from pypdf with the line broken (`Other 300 %` split from `free care`), shorten `SCHEDULE_H_250`'s 3a sentence so it fits one `drawString` line (under ~95 characters) — the regex only needs "free care" and the percentage on one extracted line.

- [ ] **Step 6: Add the CLI command**

In `src/waive/cli.py`, add `import time` to the standard-library imports and `from waive.atlas.irs import crosscheck_hospital`, then after `atlas_refresh`:

```python
@atlas_app.command("irs")
def atlas_irs(
    state: str = typer.Option(..., "--state"),
    limit: int | None = typer.Option(None, "--limit"),
    pdf: bool = typer.Option(False, "--pdf", help="Also read Schedule H from the filing PDF"),
    pause: float = typer.Option(1.0, "--pause", help="Seconds between hospitals (be polite)"),
) -> None:
    """Look up each hospital's EIN and 501(c)(3) status on ProPublica; optionally compare the
    free-care limit with Schedule H. Free: no Tavily, no Token Factory."""
    settings = Settings()
    table = Table("CCN", "Hospital", "EIN", "Outcome")
    with session_scope(_engine(settings)) as session, httpx.Client(
        follow_redirects=True, timeout=30.0
    ) as http:
        rows = [r for r in repo.list_hospitals(session, state=state) if not repo.is_demo(r.ccn)]
        for row in rows[:limit]:
            try:
                outcome = crosscheck_hospital(session, row.ccn, http, download_pdf=pdf)
            except httpx.HTTPError as error:
                outcome = f"error: {type(error).__name__}"
            session.commit()
            table.add_row(row.ccn, row.name, row.ein or "", outcome)
            time.sleep(pause)
    console.print(table)
```

- [ ] **Step 7: Lint, format, full suite, commit**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest`
Expected: about 301 tests pass.

```bash
git add src/waive/atlas/irs.py src/waive/db.py src/waive/atlas/repo.py src/waive/cli.py tests/unit/test_irs.py tests/unit/test_db_upgrade.py
git commit -m "feat: IRS 990 cross-check via ProPublica (EIN, 501(c)(3), Schedule H best effort) (7.6)"
```

- [ ] **Step 8: Live check (free; no gate)**

Run `uv run waive db upgrade` (adds `hospitals.ein` to `var/waive.db`), then `uv run waive atlas irs --state MA --limit 5` and, for one hospital with a published sheet, `uv run waive atlas irs --state MA --limit 1 --pdf`. Record in PROGRESS: how many of 5 resolved to an EIN (expect misses for system hospitals filing under a parent), whether `subsection_code` exists in the real payload (if not, remove the UNVERIFIED marker and the field, or fix its name), and what the Schedule H lines look like in the extracted text (adjust `FREE_CARE_LINE` from the real wording). Keep `--pause` at 1 second or more: ProPublica rate-limits PDF downloads.

---

### Task 7.7: Coverage map, metrics page and national report

**Files:**
- Create: `src/waive/atlas/metrics.py`, `src/waive/web/routes_metrics.py`, `src/waive/web/templates/metrics.html`
- Modify: `src/waive/web/app.py` (router), `src/waive/web/static/waive.css` (table and bar styles), `src/waive/cli.py` (`atlas report --national`)
- Test: `tests/unit/test_metrics.py` (create)

**Interfaces:**
- Consumes: `repo.list_hospitals`, `repo.is_demo`, `repo.open_review_items`, `SheetRow`, `ProcedureSheet.completeness()`, `schedule.newest_fetch_by_ccn`, `scoreboard`, `Governor.tavily_by_day`, `Governor.tavily_used_today`, `Settings.scout_daily_credits`, `deps_of`, `render`.
- Produces (`waive.atlas.metrics`): `StateCoverage(state, hospitals, published, held, none)` with property `share`; `Metrics(today, states, hospitals, published, held, none, median_sheet_age_days, documented_share, accuracy, scored_outcomes, review_open, credits_by_day, credits_today, daily_cap)`; `atlas_metrics(session, today, governor=None, daily_cap=0, days=7) -> Metrics`; `metrics_json(metrics) -> dict`; `national_report(metrics) -> str`; `RUN_LOG_HEADING = "## Run log"`; `write_national_report(path: Path, generated: str) -> None` (keeps everything from the run-log heading on).
- Produces (web): `GET /metrics` (HTML, public), `GET /metrics.json`.
- Produces (CLI): `waive atlas report --national [--out PATH]` → `docs/reports/atlas-national.md`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_metrics.py`:

```python
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
GREEN = {**HOSPITAL, "ccn": "470003", "state": "VT", "name": "GREEN MOUNTAIN HOSPITAL", "city": "RUTLAND"}


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
        repo.add_review_item(session, "330010", "conflict", {"paths": ["eligibility.discount_tiers"]})


def ledger_with_spend(tmp_path):
    ledger = Ledger(tmp_path / "usage.jsonl")
    ledger.record(UsageEvent("tavily", Decimal("3"), Decimal("0"), "atlas.scout", "2026-10-11T10:00:00+00:00"))
    ledger.record(UsageEvent("tavily", Decimal("7"), Decimal("0"), "atlas.refresh", "2026-10-12T09:00:00+00:00"))
    ledger.record(UsageEvent("tavily", Decimal("99"), Decimal("0"), "old", "2026-09-01T09:00:00+00:00"))
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
    assert (m.hospitals, m.states, m.median_sheet_age_days, m.documented_share) == (0, [], None, None)
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
    write_national_report(path, text.replace("Hospitals in registry: 3", "Hospitals in registry: 4"))
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
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.atlas.metrics'`.

- [ ] **Step 3: Implement the metrics**

`src/waive/atlas/metrics.py`:

```python
"""Atlas and cost metrics for the public /metrics page and the national report (spec §16)."""

import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.schedule import newest_fetch_by_ccn
from waive.atlas.schema import ProcedureSheet
from waive.db import SheetRow
from waive.governor import Governor
from waive.learning.scoreboard import scoreboard

RUN_LOG_HEADING = "## Run log"
RUN_LOG_HEADER = (
    RUN_LOG_HEADING
    + "\n\n| Date (ET) | Batch | Hospitals | Published | Held | Credits before → after | Notes |\n"
    "|---|---|---|---|---|---|---|\n"
)


@dataclass(frozen=True)
class StateCoverage:
    state: str
    hospitals: int
    published: int
    held: int
    none: int

    @property
    def share(self) -> float:
        return self.published / self.hospitals if self.hospitals else 0.0


@dataclass
class Metrics:
    today: date
    states: list[StateCoverage]
    hospitals: int
    published: int
    held: int
    none: int
    median_sheet_age_days: int | None
    documented_share: float | None
    accuracy: float | None
    scored_outcomes: int
    review_open: int
    credits_by_day: list[tuple[str, Decimal]]
    credits_today: Decimal
    daily_cap: int


def _latest_sheets(session: Session) -> dict[str, tuple[str, dict[str, Any]]]:
    """ccn -> (status, body) of each hospital's newest version, in one query."""
    newest = (
        select(SheetRow.ccn, func.max(SheetRow.version).label("version"))
        .group_by(SheetRow.ccn)
        .subquery()
    )
    query = select(SheetRow.ccn, SheetRow.status, SheetRow.body).join(
        newest, (SheetRow.ccn == newest.c.ccn) & (SheetRow.version == newest.c.version)
    )
    return {ccn: (status, body) for ccn, status, body in session.execute(query)}


def atlas_metrics(
    session: Session,
    today: date,
    governor: Governor | None = None,
    daily_cap: int = 0,
    days: int = 7,
) -> Metrics:
    hospitals = [row for row in repo.list_hospitals(session) if not repo.is_demo(row.ccn)]
    latest = _latest_sheets(session)
    newest = newest_fetch_by_ccn(session)
    per_state: dict[str, list[int]] = {}
    ages: list[int] = []
    completeness: list[float] = []
    for row in hospitals:
        tally = per_state.setdefault(row.state, [0, 0, 0, 0])
        tally[0] += 1
        found = latest.get(row.ccn)
        if found is None:
            tally[3] += 1
            continue
        status, body = found
        if status == "published":
            tally[1] += 1
            completeness.append(ProcedureSheet.model_validate(body).completeness())
        else:
            tally[2] += 1
        if (fetched := newest.get(row.ccn)) is not None:
            ages.append((today - fetched).days)
    states = [StateCoverage(state, *per_state[state]) for state in sorted(per_state)]
    scores = scoreboard(session)
    outcomes = sum(score.outcomes for score in scores)
    matched = sum(score.matched for score in scores)
    if governor is not None:
        by_day = governor.tavily_by_day(today - timedelta(days=days - 1), today)
        credits_today = governor.tavily_used_today(today)
    else:
        by_day, credits_today = [], Decimal("0")
    return Metrics(
        today=today,
        states=states,
        hospitals=len(hospitals),
        published=sum(s.published for s in states),
        held=sum(s.held for s in states),
        none=sum(s.none for s in states),
        median_sheet_age_days=int(statistics.median(ages)) if ages else None,
        documented_share=round(sum(completeness) / len(completeness), 2) if completeness else None,
        accuracy=round(matched / outcomes, 2) if outcomes else None,
        scored_outcomes=outcomes,
        review_open=len(repo.open_review_items(session)),
        credits_by_day=by_day,
        credits_today=credits_today,
        daily_cap=daily_cap,
    )


def metrics_json(metrics: Metrics) -> dict[str, Any]:
    return {
        "today": metrics.today.isoformat(),
        "hospitals": metrics.hospitals,
        "published": metrics.published,
        "held": metrics.held,
        "none": metrics.none,
        "median_sheet_age_days": metrics.median_sheet_age_days,
        "documented_share": metrics.documented_share,
        "accuracy": metrics.accuracy,
        "scored_outcomes": metrics.scored_outcomes,
        "review_open": metrics.review_open,
        "states": [
            {
                "state": s.state,
                "hospitals": s.hospitals,
                "published": s.published,
                "held": s.held,
                "none": s.none,
                "share": round(s.share, 3),
            }
            for s in metrics.states
        ],
        "credits_by_day": [{"day": day, "credits": str(c)} for day, c in metrics.credits_by_day],
        "credits_today": str(metrics.credits_today),
        "daily_cap": metrics.daily_cap,
        "license": "CC BY 4.0",
    }


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def national_report(metrics: Metrics) -> str:
    share = metrics.published / metrics.hospitals if metrics.hospitals else 0.0
    lines = [
        "# Atlas coverage — national",
        "",
        f"Generated {metrics.today.isoformat()} by `waive atlas report --national`. "
        "Counts only; open data, CC BY 4.0.",
        "",
        f"Hospitals in registry: {metrics.hospitals}",
        f"Published sheets: {metrics.published} ({share:.0%})",
        f"Held sheets: {metrics.held}; without a sheet: {metrics.none}",
        "Median sheet age: "
        + ("—" if metrics.median_sheet_age_days is None else f"{metrics.median_sheet_age_days} days"),
        f"Core fields documented (published sheets): {_pct(metrics.documented_share)}",
        f"Prediction accuracy: {_pct(metrics.accuracy)} over {metrics.scored_outcomes} outcomes",
        f"Open review items: {metrics.review_open}",
        "",
        "| State | Hospitals | Published | Held | None | Coverage |",
        "|---|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {s.state} | {s.hospitals} | {s.published} | {s.held} | {s.none} | {s.share:.0%} |"
        for s in metrics.states
    )
    if metrics.credits_by_day:
        lines += ["", "| Day (UTC) | Tavily credits |", "|---|---|"]
        lines.extend(f"| {day} | {credits} |" for day, credits in metrics.credits_by_day)
    return "\n".join(lines) + "\n"


def write_national_report(path: Path, generated: str) -> None:
    """Write the generated part and keep the run log the orchestrator appends by hand: everything
    from RUN_LOG_HEADING to the end of the existing file survives a regeneration."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    index = existing.find(RUN_LOG_HEADING)
    tail = existing[index:] if index >= 0 else RUN_LOG_HEADER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generated.rstrip("\n") + "\n\n" + tail, encoding="utf-8")
```

- [ ] **Step 4: Routes, template, styles**

`src/waive/web/routes_metrics.py`:

```python
"""Public atlas metrics (spec §16): counts, ages, accuracy and credits — never personal data."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from waive.atlas.metrics import atlas_metrics, metrics_json
from waive.db import session_scope
from waive.web.deps import deps_of, render

router = APIRouter()


def _metrics(request: Request):
    deps = deps_of(request)
    settings = request.app.state.settings
    with session_scope(deps.engine) as session:
        return atlas_metrics(
            session, deps.today_fn(), request.app.state.governor, settings.scout_daily_credits
        )


@router.get("/metrics", response_class=HTMLResponse)
def metrics_page(request: Request) -> HTMLResponse:
    return render(request, "metrics.html", m=_metrics(request))


@router.get("/metrics.json")
def metrics_data(request: Request) -> JSONResponse:
    return JSONResponse(metrics_json(_metrics(request)))
```

`src/waive/web/templates/metrics.html`:

```html
{% extends "base.html" %}
{% block title %}Atlas metrics{% endblock %}
{% block content %}
<h1>Atlas metrics</h1>
<p class="muted">As of {{ m.today.isoformat() }}. Counts only; no personal data. Open data, CC BY 4.0.</p>
<div class="card">
  <div class="row"><span>Hospitals in registry</span><strong>{{ m.hospitals }}</strong></div>
  <div class="row"><span>Published sheets</span><strong>{{ m.published }}{% if m.hospitals %} ({{ "%.0f" % (100 * m.published / m.hospitals) }}%){% endif %}</strong></div>
  <div class="row"><span>Held sheets</span><strong>{{ m.held }}</strong></div>
  <div class="row"><span>Median sheet age</span><strong>{% if m.median_sheet_age_days is none %}—{% else %}{{ m.median_sheet_age_days }} days{% endif %}</strong></div>
  <div class="row"><span>Core fields documented</span><strong>{% if m.documented_share is none %}—{% else %}{{ "%.0f" % (100 * m.documented_share) }}%{% endif %}</strong></div>
  <div class="row"><span>Prediction accuracy</span><strong>{% if m.accuracy is none %}— ({{ m.scored_outcomes }} outcomes){% else %}{{ "%.0f" % (100 * m.accuracy) }}% of {{ m.scored_outcomes }} outcomes{% endif %}</strong></div>
  <div class="row"><span>Open review items</span><strong>{{ m.review_open }}</strong></div>
</div>
<h2>Coverage by state</h2>
<table class="coverage">
  <thead><tr><th>State</th><th>Hospitals</th><th>Published</th><th>Held</th><th>Coverage</th></tr></thead>
  <tbody>
  {% for s in m.states %}
  <tr><td>{{ s.state }}</td><td>{{ s.hospitals }}</td><td>{{ s.published }}</td><td>{{ s.held }}</td>
      <td><span class="bar" style="width: {{ (60 * s.share) | round }}%" aria-hidden="true"></span>{{ "%.0f" % (100 * s.share) }}%</td></tr>
  {% else %}
  <tr><td colspan="5">No hospitals seeded yet.</td></tr>
  {% endfor %}
  </tbody>
</table>
<h2>Tavily credits per day</h2>
<table class="coverage">
  <thead><tr><th>Day (UTC)</th><th>Credits</th></tr></thead>
  <tbody>
  {% for day, credits in m.credits_by_day %}<tr><td>{{ day }}</td><td>{{ credits }}</td></tr>{% endfor %}
  </tbody>
</table>
<p class="muted">Today: {{ m.credits_today }} of {{ m.daily_cap }} scheduled-scouting credits.</p>
{% endblock %}
```

Append to `src/waive/web/static/waive.css`:

```css
/* Metrics page (task 7.7) */
.coverage { width: 100%; border-collapse: collapse; font-size: 18px; margin-bottom: 24px; }
.coverage th, .coverage td { text-align: left; padding: 10px 6px; border-bottom: 1px solid #cfcfcf; }
.bar { display: inline-block; height: 14px; background: #1f6f43; vertical-align: middle; margin-right: 8px; }
```

In `src/waive/web/app.py`, extend the router import to `from waive.web import routes_admin, routes_atlas, routes_caregiver, routes_metrics, routes_senior` and add `app.include_router(routes_metrics.router)` after the atlas router.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/unit/test_metrics.py -v`
Expected: 4 passed. If `documented_share` is `0.83` versus `0.8333`, check the `round(..., 2)`; if `median_sheet_age_days` is None, `newest_fetch_by_ccn` filtered the source out — its id must not start with `state-` and its kind must be `hospital_web`.

- [ ] **Step 6: CLI report**

Replace `atlas_report` in `src/waive/cli.py` (add `from waive.atlas.metrics import atlas_metrics, national_report, write_national_report` to the imports):

```python
@atlas_app.command("report")
def atlas_report(
    state: str | None = typer.Option(None, "--state"),
    national: bool = typer.Option(False, "--national", help="All states; keeps the run log"),
    out: Path | None = typer.Option(None, "--out"),  # noqa: B008
) -> None:
    """Write a markdown coverage report (one state, or national with a preserved run log)."""
    if not state and not national:
        raise typer.BadParameter("give --state XX or --national")
    settings = Settings()
    if national:
        governor = make_governor(settings)
        path = out or Path("docs/reports/atlas-national.md")
        with session_scope(_engine(settings)) as session:
            metrics = atlas_metrics(
                session, datetime.now(UTC).date(), governor, settings.scout_daily_credits
            )
        write_national_report(path, national_report(metrics))
        console.print(f"Wrote {path}")
        return
    path = out or Path("docs/reports") / f"atlas-{state.lower()}.md"
    with session_scope(_engine(settings)) as session:
        text = coverage_report(session, state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    console.print(f"Wrote {path}")
```

- [ ] **Step 7: Lint, format, full suite, local run, commit**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest`
Expected: about 305 tests pass.

Run: `uv run waive atlas report --national` (free) and `uv run waive serve`, open http://localhost:8000/metrics: the MA row shows 46 hospitals and the published share from Phase 2; the credits table shows the last seven days from `var/usage.jsonl`.

```bash
git add src/waive/atlas/metrics.py src/waive/web/routes_metrics.py src/waive/web/templates/metrics.html src/waive/web/static/waive.css src/waive/web/app.py src/waive/cli.py tests/unit/test_metrics.py docs/reports/atlas-national.md
git commit -m "feat: public /metrics page and national coverage report with preserved run log (7.7)"
```

---

## Phase 7 exit checks (the orchestrator runs these; record results in `docs/PROGRESS.md`)

- [ ] **Unattended run, 48 hours, within budget.** Needs gate **U2.2** (Massachusetts only) or **U7.1** (national). Set in `.env`: `WAIVE_SCHEDULER=on`, `WAIVE_SCOUT_DAILY_CREDITS=50`, `WAIVE_SCHEDULER_STATES=MA` (or empty after U7.1), then run `uv run waive serve` for 48 hours (or set the same variables on the Nebius endpoint once Phase 6 is deployed — the DB ledger makes the budget hold across container restarts). Check twice a day: `/metrics` → "Tavily credits per day" ≤ 50 on each day; `uv run waive doctor` totals; `uv run waive atlas schedule --dry-run` shows staleness resetting. Pass when both days stayed within 50 credits and the process never stopped. Expected first-day behaviour on MA: ≈ 46 refreshes at 0–1 credit each, a handful of re-structures where a document changed, no new versions where nothing changed.
- [ ] **National coverage report committed.** `uv run waive atlas report --national` after the last batch; commit `docs/reports/atlas-national.md` with its run log; copy the totals line into `docs/PROGRESS.md`.
- [ ] `uv run ruff format . && uv run ruff check . && uv run pytest` green; the test count and spend recorded in PROGRESS; gates U7.1 (closed or open with the estimate) and U2.2 reflected there.

---

## Self-review

- **Spec coverage.** §8 step 8 "Re-check documents by content hash on a schedule" → 7.2 (`refresh_hospital`, `fetch_texts`) and the refresh-first `_scout`; "Priority = staleness × case demand × (1 − accuracy)" → 7.1 (`priority_score`, `build_queue`, with explicit floors documented); "An unknown hospital on a bill triggers on-demand scouting" → 7.1 attributes `scout_request` items to registry hospitals through the bill matcher and lists the rest as unmatched (the never-seen-hospital case remains a review item: a bill from a hospital not in the CMS registry is outside the atlas by design). §15 "refreshes extract only documents whose hash changed" → 7.2 (an equal hash costs nothing beyond the fetch; re-structuring is Token Factory only); "national first pass" → 7.3 + 7.4, with the budget estimate corrected from Phase 2's measured 4–5 credits per hospital; development caps → the hard cap stays and the daily budget is added. §16 atlas metrics (hospitals covered, share of fields documented with verified quotes, median sheet age, per-hospital accuracy, review queue size) and cost (credits per day) → 7.7 (`atlas_metrics`, `/metrics`, `/metrics.json`, national report); "dollars per case" and "time to packet" are product metrics outside this phase (Phase 8 demo data). §2 prior art (HCAI, DOH, Schedule H) → 7.5 and 7.6. §8 step 6 "cross-checks against … IRS Schedule H limits" → 7.6 (`irs_mismatch`, honest about what the text yields). G6 "Always-on scouting within a credit budget" → 7.1 + 7.2. Master plan exit checks (48 hours unattended, national report) → the exit section.
- **Placeholder scan.** Every code step carries its code; every live step names its gate (U2.2 / U7.1 / none) and the cap check (`Governor.ensure_tavily` inside the gateway; the daily budget in `run_entries`). Unverified facts are marked in the facts table and in code comments (`UNVERIFIED`), never silently assumed: HCAI slug rule and markup, attachment content type, DOH table markup and name threshold, ProPublica search keys and `subsection_code`, Schedule H text patterns, census order, APScheduler 3.x API details.
- **Type consistency.** `QueueEntry(ccn, name, state, staleness_days, demand, accuracy, priority, reasons, has_sources, rescout_requested)` is constructed with those keywords in 7.1 and read by name in 7.2 (`has_sources`, `rescout_requested`) and 7.4 (`state`, `priority`, `name`). `run_entries(session, gateway, ai, governor, today, entries, *, daily_cap, limit)` is called the same way from `run_once` (7.1) and `national_batch` (7.4). `RefreshResult.build` is a `BuildResult | None` and `_scout` returns `BuildResult`. `newest_fetch_by_ccn(session) -> dict[str, date]` is defined in 7.1 and imported by 7.7. `download_text(..., assume_pdf=True)` (7.5) is used by 7.5 and 7.6. `Governor.tavily_used_today(today)` and `tavily_by_day(since, today)` (7.1) are used by 7.1, 7.4 and 7.7. Review item kinds introduced: `irs_not_found`, `irs_not_501c3`, `irs_mismatch`; existing kinds consumed: `scout_request`, `rescout_request`, `priority_recheck`, `domain`, `no_documents`.
- **Known simplifications (carry to the backlog):** the sheet's visible `checked_on` only moves on the next value change (dates are not versioned; the source row's `fetched_on` carries freshness and feeds the age metric); `WashingtonDoh` caches the policy table for the life of the process; Washington's statutory floors (HB 1616) are not encoded as a state-minimum check; HCAI slugs may need a hand-maintained alias map after the first live run; `scout_request` items stay open after a matched hospital is scouted (admins close them); the national batch has no per-state cap beyond the daily budget; the Schedule H parser is best effort and may never fire on checkbox-only filings.
