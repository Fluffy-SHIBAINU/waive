"""Command-line entry point: `uv run waive ...`."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

from waive.ai.client import AIClient
from waive.atlas.overlays import run_overlay
from waive.atlas.pipeline import build_hospital, build_state, coverage_report
from waive.atlas.publish import export_state
from waive.atlas.registry import seed_state
from waive.atlas.tavily_gateway import make_tavily_gateway
from waive.cases.evaluate import evaluate_corpus, write_report
from waive.cases.synth import generate_corpus
from waive.cases.vault import new_key
from waive.config import Settings
from waive.db import init_db, make_engine, session_scope
from waive.demo import forget_cases, seed_demo
from waive.doctor import run_checks
from waive.governor import make_governor
from waive.learning.contributions import rebuild_from_sources

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
    console.print(f"WAIVE_VAULT_KEY={new_key()}")
    console.print(f"WAIVE_TOKEN_SECRET={new_key()}{new_key()}")


atlas_app = typer.Typer(no_args_is_help=True, help="Build and inspect the hospital atlas.")
app.add_typer(atlas_app, name="atlas")


def _engine(settings: Settings):
    engine = make_engine(settings.database_url)
    init_db(engine)
    return engine


@atlas_app.command("seed")
def atlas_seed(state: str = typer.Option(..., "--state", help="Two-letter state code")) -> None:
    """Load nonprofit acute-care and critical-access hospitals from CMS into the registry."""
    settings = Settings()
    with session_scope(_engine(settings)) as session, httpx.Client() as http:
        report = seed_state(session, state, http)
    console.print(
        f"Fetched {report.fetched} {state.upper()} hospitals; kept {report.kept} nonprofit "
        f"acute-care/critical-access. Snapshot: {report.snapshot}"
    )


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
    state: str = typer.Option(..., "--state"),
    out: Path | None = typer.Option(None, "--out"),  # noqa: B008
) -> None:
    """Write a markdown coverage report."""
    settings = Settings()
    path = out or Path("docs/reports") / f"atlas-{state.lower()}.md"
    with session_scope(_engine(settings)) as session:
        text = coverage_report(session, state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    console.print(f"Wrote {path}")


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

    uvicorn.run("waive.web.app:create_app", host=host, port=port, factory=True)


demo_app = typer.Typer(no_args_is_help=True, help="Demo data.")
app.add_typer(demo_app, name="demo")


@demo_app.command("seed")
def demo_seed() -> None:
    """Add the fictional St. Example hospital and its policy."""
    settings = Settings()
    with session_scope(_engine(settings)) as session:
        seed_demo(session)
    console.print("Demo hospital seeded.")


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
