"""Command-line entry point: `uv run waive ...`."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

from waive.ai.client import AIClient
from waive.atlas.pipeline import build_hospital, build_state, coverage_report
from waive.atlas.publish import export_state
from waive.atlas.registry import seed_state
from waive.atlas.tavily_gateway import make_tavily_gateway
from waive.config import Settings
from waive.db import init_db, make_engine, session_scope
from waive.doctor import run_checks
from waive.governor import make_governor

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
) -> None:
    """Discover, scout, structure, verify and publish procedure sheets. Spends Tavily credits."""
    settings = Settings()
    governor = make_governor(settings)
    ai = AIClient(settings, governor)
    gateway = make_tavily_gateway(settings, governor)
    today = datetime.now(UTC).date()
    with session_scope(_engine(settings)) as session:
        if ccn:
            results = [build_hospital(session, gateway, ai, ccn, today, dual=dual)]
        else:
            results = build_state(
                session, gateway, ai, state, today, limit=limit, dual=dual, only_missing=not rebuild
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
