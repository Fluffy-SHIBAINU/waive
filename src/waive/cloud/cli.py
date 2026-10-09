"""`waive cloud …`: deployment commands wrapping the nebius CLI (Phase 6).

Task 6.3 adds `discover` (read-only). Later tasks add `secrets push`, `deploy`, `start`, `stop`,
`status` and `cleanup` behind gate U6.1.
"""

from decimal import Decimal, InvalidOperation
from pathlib import Path

import typer
from rich.console import Console

from waive.cloud.costs import Footprint, Scenario
from waive.cloud.discover import discover, platform_known, write_discovery
from waive.cloud.discover import render_gate_message as gate_message
from waive.cloud.nebius import Nebius, NebiusError, NebiusMissing, Runner, run_subprocess
from waive.config import Settings

cloud_app = typer.Typer(
    no_args_is_help=True, help="Nebius AI Cloud deployment (wraps the nebius CLI)."
)
console = Console()
# Tests replace these two: a fake runner answers every CLI call, and settings come without .env.
RUNNER: Runner = run_subprocess
load_settings = Settings


def _nebius(settings: Settings) -> Nebius:
    try:
        return Nebius.from_settings(settings, runner=RUNNER)
    except NebiusMissing as error:
        console.print(f"[red]{error}[/red]", soft_wrap=True)
        raise typer.Exit(code=2) from None


def _decimal(value: str, name: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation:
        console.print(f"[red]{name} must be a number, got {value!r}[/red]")
        raise typer.Exit(code=2) from None


@cloud_app.command("discover")
def cloud_discover(
    out: Path = typer.Option(Path("docs/reports/cloud-costs.md"), "--out"),  # noqa: B008
    raw: Path = typer.Option(Path("var/cloud-discovery.json"), "--raw"),  # noqa: B008
    budget: str = typer.Option("30", "--budget", help="AI Cloud budget in USD (master plan: 30)"),
    window_days: int = typer.Option(14, "--window-days", help="Judging window in days"),
    demo_hours_per_day: str = typer.Option(
        "2", "--demo-hours-per-day", help="Endpoint hours per day when stopped between demos"
    ),
) -> None:
    """Read-only: list platforms, networks, prices and existing waive- resources; write the cost
    table and print the gate U6.1 message. Creates nothing."""
    settings = load_settings()
    nebius = _nebius(settings)
    if nebius.warning:
        console.print(f"[yellow]{nebius.warning}[/yellow]", soft_wrap=True)
    if not platform_known(settings.cloud_platform):
        console.print(
            f"[red]no published price for platform {settings.cloud_platform!r}; add it to "
            "waive.cloud.costs.COMPUTE_PRICES from the Compute pricing page[/red]"
        )
        raise typer.Exit(code=2)
    footprint = Footprint(
        settings.cloud_platform,
        settings.cloud_preset,
        settings.cloud_pg_preset,
        settings.cloud_pg_disk_gib,
        _decimal(budget, "--budget"),
    )
    scenario = Scenario(window_days, _decimal(demo_hours_per_day, "--demo-hours-per-day"))
    try:
        found = discover(nebius, footprint)
    except NebiusError as error:
        console.print(f"[red]{error}[/red]", soft_wrap=True)
        raise typer.Exit(code=1) from None
    write_discovery(found, footprint, scenario, out, raw)
    console.print(f"Wrote {out} and {raw}. Nothing was created. Next: gate U6.1.", soft_wrap=True)
    if found.raw.get("errors"):
        console.print(
            f"[yellow]{len(found.raw['errors'])} CLI call(s) failed; see the report's last "
            "section.[/yellow]"
        )
    console.print()
    console.print(gate_message(found, footprint, scenario), soft_wrap=True, markup=False)
