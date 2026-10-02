"""Command-line entry point: `uv run waive ...`."""

import httpx
import typer
from rich.console import Console
from rich.table import Table

from waive.ai.client import AIClient
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
