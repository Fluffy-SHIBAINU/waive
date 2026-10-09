"""Command-line entry point: `uv run waive ...`."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

from waive.ai.client import AIClient
from waive.atlas import repo
from waive.atlas.metrics import atlas_metrics, national_report, write_national_report
from waive.atlas.overlays import run_overlay
from waive.atlas.pipeline import build_hospital, build_state, coverage_report
from waive.atlas.publish import export_state
from waive.atlas.refresh import refresh_hospital
from waive.atlas.registry import seed_all_states, seed_state
from waive.atlas.schedule import CREDITS_PER_HOSPITAL, build_queue, run_once, scheduler_states
from waive.atlas.tavily_gateway import make_tavily_gateway
from waive.cases.evaluate import evaluate_corpus, write_report
from waive.cases.service import purge_cases
from waive.cases.synth import generate_corpus
from waive.cases.vault import new_key
from waive.config import Settings
from waive.db import init_db, make_engine, session_scope
from waive.demo import DEMO_DIR, forget_cases, reset_demo, seed_demo
from waive.doctor import run_checks
from waive.gallery import DEFAULT_PORT, GalleryError, run_gallery
from waive.governor import make_governor
from waive.learning.contributions import rebuild_from_sources
from waive.learning.evidence import audit_evidence, publish_reported
from waive.learning.scoreboard import queue_prechecks, scoreboard

app = typer.Typer(no_args_is_help=True, help="Waive operations.")
console = Console()


@app.callback()
def main() -> None:
    """Waive command-line tools."""


@app.command()
def doctor(
    live: bool = typer.Option(
        False, "--live", help="Also call Token Factory and Tavily (spends up to 1 Tavily credit)."
    ),
) -> None:
    """Check configuration and connectivity without printing secrets."""
    settings = Settings()
    governor = make_governor(settings)
    checks = run_checks(
        settings,
        live=live,
        ai_factory=lambda: AIClient(settings, governor),
        tavily_factory=lambda: make_tavily_gateway(settings, governor),
    )
    table = Table("Check", "Status", "Detail")
    for check in checks:
        table.add_row(check.name, check.status.upper(), check.detail)
    console.print(table)
    tavily_credits, _ = governor.summary()["tavily"]
    _, tf_usd = governor.summary()["token_factory"]
    console.print(f"Spend so far: {tavily_credits} Tavily credits, ${tf_usd:.4f} Token Factory")
    raise typer.Exit(code=1 if any(check.status == "fail" for check in checks) else 0)


@app.command()
def keygen() -> None:
    """Print fresh secrets for .env (never commit them)."""
    # Plain echo, not the Rich console: Rich wraps at 80 columns when stdout is not a terminal,
    # which split the 107-character WAIVE_TOKEN_SECRET line in `waive keygen >> .env`.
    typer.echo(f"WAIVE_VAULT_KEY={new_key()}")
    typer.echo(f"WAIVE_TOKEN_SECRET={new_key()}{new_key()}")


atlas_app = typer.Typer(no_args_is_help=True, help="Build and inspect the hospital atlas.")
app.add_typer(atlas_app, name="atlas")


def _engine(settings: Settings):
    engine = make_engine(settings.database_url)
    init_db(engine)
    return engine


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


@atlas_app.command("build")
def atlas_build(
    state: str = typer.Option(..., "--state"),
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
    settings = Settings()
    governor = make_governor(settings)
    ai = AIClient(settings, governor)
    gateway = make_tavily_gateway(settings, governor)
    today = datetime.now(UTC).date()
    with session_scope(_engine(settings)) as session:
        if ccn:
            results = [
                build_hospital(
                    session, gateway, ai, ccn, today, dual=dual, reuse_sources=reuse_sources
                )
            ]
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
    tavily_credits, _ = governor.summary()["tavily"]
    _, tf_usd = governor.summary()["token_factory"]
    console.print(f"Spend so far: {tavily_credits} Tavily credits, ${tf_usd:.4f} Token Factory")


@atlas_app.command("overlay")
def atlas_overlay(state: str = typer.Option(..., "--state")) -> None:
    """Add cited state programs (for example the Massachusetts Health Safety Net) to sheets."""
    settings = Settings()
    governor = make_governor(settings)
    gateway = make_tavily_gateway(settings, governor)
    with session_scope(_engine(settings)) as session:
        count = run_overlay(session, gateway, state, datetime.now(UTC).date())
    console.print(f"Updated {count} sheets with {state.upper()} state programs")
    tavily_credits, _ = governor.summary()["tavily"]
    console.print(f"Spend so far: {tavily_credits} Tavily credits")


@atlas_app.command("export")
def atlas_export(
    state: str = typer.Option(..., "--state"),
    out: Path | None = typer.Option(None, "--out"),  # noqa: B008
) -> None:
    """Write the latest sheets for a state as open data (CC BY 4.0)."""
    settings = Settings()
    path = out or Path("data/atlas") / f"{state.lower()}.json"
    with session_scope(_engine(settings)) as session:
        count = export_state(session, state, path)
    console.print(f"Exported {count} sheets to {path}")


@atlas_app.command("report")
def atlas_report(
    state: str | None = typer.Option(None, "--state"),
    national: bool = typer.Option(False, "--national", help="All states; keeps the run log"),
    out: Path | None = typer.Option(None, "--out"),  # noqa: B008
) -> None:
    """Write a markdown coverage report (one state, or national with a preserved run log).
    No paid calls: counts come from the local database and the usage ledger."""
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


@atlas_app.command("schedule")
def atlas_schedule(
    dry_run: bool = typer.Option(
        True, "--dry-run/--run", help="List the queue (default) or scout its top within the budget"
    ),
    limit: int = typer.Option(1, "--limit", help="Hospitals to scout with --run"),
    state: str | None = typer.Option(
        None, "--state", help="One state; default WAIVE_SCHEDULER_STATES"
    ),
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
    with (
        session_scope(_engine(settings)) as session,
        httpx.Client(follow_redirects=True, timeout=30.0) as http,
    ):
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


corpus_app = typer.Typer(no_args_is_help=True, help="Synthetic test data.")
app.add_typer(corpus_app, name="corpus")


@corpus_app.command("generate")
def corpus_generate(
    count: int = typer.Option(30, "--count"),
    out: Path = typer.Option(Path("var/corpus"), "--out"),  # noqa: B008
    seed: int = typer.Option(7, "--seed"),
) -> None:
    """Write fictional bill images with ground truth JSON (no real data)."""
    paths = generate_corpus(out, count, seed)
    console.print(f"Wrote {len(paths)} bills to {out}")


eval_app = typer.Typer(no_args_is_help=True, help="Accuracy reports.")
app.add_typer(eval_app, name="eval")


@eval_app.command("bills")
def eval_bills(
    corpus: Path = typer.Option(Path("var/corpus"), "--corpus"),  # noqa: B008
    limit: int | None = typer.Option(None, "--limit"),
    out: Path = typer.Option(Path("docs/reports/bill-eval.md"), "--out"),  # noqa: B008
) -> None:
    """Run the vision model over the synthetic corpus and write per-field accuracy. Spends tokens."""
    settings = Settings()
    governor = make_governor(settings)
    ai = AIClient(settings, governor)
    scores = evaluate_corpus(ai, corpus, limit)
    write_report(scores, out)
    console.print(
        f"Wrote {out}: " + ", ".join(f"{k}={v:.0%}" for k, v in scores.items() if k != "n")
    )
    _, tf_usd = governor.summary()["token_factory"]
    console.print(f"Token Factory spend so far: ${tf_usd:.4f}")


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host"), port: int = typer.Option(8000, "--port")
) -> None:
    """Run the web app (phones on the same Wi-Fi can open http://<this-computer-ip>:8000)."""
    import uvicorn

    # access_log=False: request paths carry capability links (/s/<token>, /c/<token>), so the
    # access log would keep live credentials in the terminal; the container does the same.
    uvicorn.run("waive.web.app:create_app", host=host, port=port, factory=True, access_log=False)


db_app = typer.Typer(no_args_is_help=True, help="Database schema maintenance.")
app.add_typer(db_app, name="db")


@db_app.command("purge")
def db_purge() -> None:
    """Delete cases nobody can reach any more: created over a day ago without a photo, or older
    than the caregiver link's life. Their scout requests go too. Free."""
    settings = Settings()
    with session_scope(_engine(settings)) as session:
        counts = purge_cases(session, datetime.now(UTC).date())
    console.print(
        f"Deleted {counts['abandoned']} abandoned and {counts['expired']} expired case(s)."
    )


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Create missing tables and add columns the models define but the database lacks."""
    settings = Settings()
    added = init_db(make_engine(settings.database_url))
    for name in added:
        console.print(f"Added column {name}")
    console.print(f"Added {len(added)} column(s)." if added else "Schema is up to date.")


demo_app = typer.Typer(no_args_is_help=True, help="Demo data.")
app.add_typer(demo_app, name="demo")


@demo_app.command("seed")
def demo_seed() -> None:
    """Add the fictional St. Example hospital and its policy."""
    settings = Settings()
    with session_scope(_engine(settings)) as session:
        seed_demo(session)
    console.print("Demo hospital seeded.")


@demo_app.command("reset")
def demo_reset(
    out: Path = typer.Option(DEMO_DIR, "--out", help="Where the demo bill and letter go"),  # noqa: B008
    keep_files: bool = typer.Option(False, "--keep-files", help="Do not rewrite the demo images"),
) -> None:
    """Put the local database and the demo images back to the demo script's starting point.
    Deletes every case; real hospitals' sheets are untouched. No paid calls."""
    settings = Settings()
    with session_scope(_engine(settings)) as session:
        report = reset_demo(session, out, write_files=not keep_files)
    console.print(
        f"Deleted {report.cases_deleted} case(s); for the demo hospital: "
        f"{report.review_items_deleted} review item(s), {report.contributions_deleted} "
        f"contribution(s), {report.evidence_deleted} evidence row(s), "
        f"{report.sources_unlinked} source link(s), {report.documents_deleted} demo-only "
        f"document(s), {report.sheet_versions_deleted} sheet version(s). St. Example is back at "
        f"version {report.sheet_version}."
    )
    for path in report.files:
        console.print(f"Wrote {path}")


@demo_app.command("gallery")
def demo_gallery(
    out: Path = typer.Option(  # noqa: B008
        Path("docs/devpost/gallery"),
        "--out",
        help="Output folder, relative to the current directory (run from the repository root)",
    ),
    chrome: str | None = typer.Option(None, "--chrome", help="Chrome or Chromium binary"),
    live: bool = typer.Option(False, "--live", help="Use the real vision model (about $0.01)"),
    port: int = typer.Option(DEFAULT_PORT, "--port"),
    atlas_ccn: str = typer.Option("220031", "--atlas-ccn", help="Published sheet to photograph"),
    diagrams: bool = typer.Option(True, "--diagrams/--no-diagrams", help="Export README diagrams"),
) -> None:
    """Photograph the demo flow and the atlas pages with headless Chrome, and export the README
    diagrams, for the Devpost gallery. Spends nothing unless --live. Run it from the repository
    root. Creates one demo case in the local database; run `waive demo reset` afterwards."""
    settings = Settings()
    try:
        written, skipped = run_gallery(
            settings,
            out,
            chrome=chrome,
            live=live,
            port=port,
            atlas_ccn=atlas_ccn,
            diagrams=diagrams,
        )
    except GalleryError as error:
        console.print(str(error))
        raise typer.Exit(code=1) from error
    for path in written:
        console.print(f"Wrote {path}")
    for name, why in skipped.items():
        console.print(f"Skipped {name}: {why}")


@demo_app.command("forget-cases")
def demo_forget_cases() -> None:
    """Delete every case (all personal data) from the local database."""
    settings = Settings()
    with session_scope(_engine(settings)) as session:
        count = forget_cases(session)
    console.print(f"Deleted {count} cases.")


learn_app = typer.Typer(no_args_is_help=True, help="Learning loop operations.")
app.add_typer(learn_app, name="learn")


@learn_app.command("rebuild")
def learn_rebuild(ccn: str = typer.Option(..., "--ccn", help="Hospital to re-structure")) -> None:
    """Re-structure one sheet from stored documents (approved patient photos included). No Tavily."""
    settings = Settings()
    governor = make_governor(settings)
    ai = AIClient(settings, governor)
    with session_scope(_engine(settings)) as session:
        result = rebuild_from_sources(session, ai, ccn, datetime.now(UTC).date())
    console.print(
        f"{result.name}: {result.outcome}, version {result.version}; " + "; ".join(result.notes)
    )
    _, tf_usd = governor.summary()["token_factory"]
    console.print(f"Token Factory spend so far: ${tf_usd:.4f}")


@learn_app.command("publish-reported")
def learn_publish_reported(state: str = typer.Option(..., "--state")) -> None:
    """Publish patient-reported fields that reached the 5-case threshold. No paid calls."""
    settings = Settings()
    today = datetime.now(UTC).date()
    with session_scope(_engine(settings)) as session:
        bumped = [
            row.ccn
            for row in repo.list_hospitals(session, state=state)
            if publish_reported(session, row.ccn, today) is not None
        ]
    console.print(f"New versions for {len(bumped)} hospital(s): {', '.join(bumped) or 'none'}")


@learn_app.command("audit")
def learn_audit() -> None:
    """Check the learning tables for anything that is not an enum, a date, a count or a hash."""
    settings = Settings()
    with session_scope(_engine(settings)) as session:
        problems = audit_evidence(session)
    for problem in problems:
        console.print(problem)
    console.print("Evidence tables are clean." if not problems else f"{len(problems)} problem(s).")
    raise typer.Exit(code=1 if problems else 0)


@learn_app.command("scoreboard")
def learn_scoreboard(
    state: str | None = typer.Option(None, "--state"),
    queue: bool = typer.Option(False, "--queue", help="Open priority re-checks for low accuracy"),
) -> None:
    """Per-hospital prediction accuracy from recorded outcomes. No paid calls."""
    settings = Settings()
    with session_scope(_engine(settings)) as session:
        scores = scoreboard(session, state)
        queued = queue_prechecks(session) if queue else []
    table = Table("CCN", "Hospital", "Outcomes", "Matched", "Accuracy", "Sheet", "Flag", "Re-check")
    for score in scores:
        table.add_row(
            score.ccn,
            score.name,
            str(score.outcomes),
            str(score.matched),
            f"{score.accuracy:.0%}",
            str(score.sheet_version or ""),
            score.flag_level,
            "yes" if score.needs_recheck else "",
        )
    console.print(table)
    if queue:
        console.print(f"Queued priority re-checks: {', '.join(queued) or 'none'}")
