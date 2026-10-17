"""`waive ledger ...` (Phase 10): crosswalk, behaviour, exposure, show.

Every command downloads public files into the ledger directory (`WAIVE_LEDGER_DIR`,
`var/ledger` by default), writes the ledger tables and prints counts. None spends a Tavily
credit or calls a model. Registered on the main CLI as the `ledger` command group."""

import json
from datetime import UTC, datetime

import httpx
import typer
from rich.console import Console
from rich.table import Table

from waive.config import Settings
from waive.db import init_db, make_engine, session_scope
from waive.ledger.crosswalk import run_crosswalk, set_manual, write_unmatched
from waive.ledger.download import cms_catalog
from waive.ledger.exposure import load_exposure
from waive.ledger.hcris import load_cost_reports
from waive.ledger.irs990 import load_schedule_h
from waive.ledger.metrics import compute_behaviour, compute_exposure_metrics
from waive.ledger.tables import MATCH_METHODS
from waive.ledger.view import ledger_view

ledger_app = typer.Typer(
    no_args_is_help=True, help="Hospital ledger: free federal data joined to the atlas."
)
console = Console()
# A list option's default is a module-level singleton (ruff B008): "--set CCN=EIN", repeatable.
SET_OPTION = typer.Option(None, "--set", help="CCN=EIN, set by hand (no download); repeatable")


def _engine(settings: Settings):
    engine = make_engine(settings.database_url)
    init_db(engine)
    return engine


def _today():
    return datetime.now(UTC).date()


def _states(text: str | None) -> tuple[str, ...]:
    return tuple(s.strip().upper() for s in (text or "").split(",") if s.strip())


@ledger_app.command("crosswalk")
def ledger_crosswalk(
    states: str = typer.Option("MA", "--states", help="Comma-separated state codes"),
    refresh: bool = typer.Option(False, "--refresh", help="Download the files again"),
    set_ein: list[str] | None = SET_OPTION,
) -> None:
    """Tie each hospital to its EIN (Community Benefit Insight) and NPI (CMS Hospital
    Enrollments). Hospitals left unmatched are listed in var/ledger/unmatched-XX.csv with
    their best candidates, for `--set CCN=EIN`."""
    settings = Settings()
    today = _today()
    with session_scope(_engine(settings)) as session:
        if set_ein:
            for item in set_ein:
                ccn, _, ein = item.partition("=")
                row = set_manual(session, ccn.strip(), ein.strip(), today)
                console.print(f"{row.ccn}: EIN {row.ein} set by hand")
            return
        table = Table("State", "Hospitals", "Matched", "Unmatched", *MATCH_METHODS)
        with httpx.Client() as http:
            catalog = cms_catalog(http, settings.ledger_dir / "catalog", refresh=refresh)
            for state in _states(states):
                report = run_crosswalk(
                    session, http, state, settings.ledger_dir, catalog, today, refresh=refresh
                )
                session.commit()
                table.add_row(
                    state,
                    str(report.hospitals),
                    str(report.matched),
                    str(report.unmatched),
                    *(str(report.by_method.get(method, 0)) for method in MATCH_METHODS),
                )
                if report.unmatched:
                    path = write_unmatched(report, settings.ledger_dir / f"unmatched-{state}.csv")
                    console.print(f"{state}: {report.unmatched} unmatched, candidates in {path}")
        console.print(table)


@ledger_app.command("behaviour")
def ledger_behaviour(
    states: str | None = typer.Option(
        None, "--states", help="Fetch tax filings for these states' hospitals (default: all)"
    ),
    years: int = typer.Option(3, "--years", help="Cost-report fiscal years to load"),
    index_years: str = typer.Option(
        "2024,2025,2026", "--index-years", help="IRS e-file index years"
    ),
    skip_990: bool = typer.Option(False, "--skip-990", help="Cost reports and metrics only"),
    refresh: bool = typer.Option(False, "--refresh", help="Download the files again"),
) -> None:
    """Load CMS cost reports (every hospital) and IRS Schedule H facts (matched EINs), then
    compute the free-care gap and the collection flags."""
    settings = Settings()
    today = _today()
    with session_scope(_engine(settings)) as session, httpx.Client() as http:
        catalog = cms_catalog(http, settings.ledger_dir / "catalog", refresh=refresh)
        hcris = load_cost_reports(
            session, http, settings.ledger_dir, catalog, today=today, years=years, refresh=refresh
        )
        session.commit()
        console.print(
            f"Cost reports: {hcris.rows_read} rows read from {len(hcris.files)} file(s); "
            f"{hcris.hospitals_loaded} hospitals loaded; by fiscal year {hcris.by_year}"
        )
        if not skip_990:
            index = tuple(int(year) for year in _states(index_years))
            for state in _states(states) or (None,):
                report = load_schedule_h(
                    session,
                    http,
                    settings.ledger_dir,
                    today=today,
                    index_years=index,
                    refresh=refresh,
                    state=state,
                )
                session.commit()
                console.print(
                    f"Schedule H{' ' + state if state else ''}: {report.eins_wanted} EINs wanted, "
                    f"{report.returns_found} returns found, {report.batches_downloaded} batch "
                    f"files, {report.xml_fetched} returns fetched, {report.hospitals_loaded} "
                    f"hospitals loaded, {len(report.facilities_unmatched)} facilities unmatched"
                )
                for item in report.facilities_unmatched[:20]:
                    console.print(f"  unmatched facility: {item}")
                for item in report.facilities_unanswered[:20]:
                    console.print(f"  facility without a Section B answer: {item}")
                for item in report.returns_missing[:20]:
                    console.print(f"  return missing from its batch file: {item}")
        behaviour = compute_behaviour(session, today)
    table = Table("Measure", "Hospitals")
    for name, value in vars(behaviour).items():
        table.add_row(name.replace("_", " "), str(value))
    console.print(table)


@ledger_app.command("exposure")
def ledger_exposure(
    acs_year: int = typer.Option(2024, "--acs-year", help="ACS 5-year vintage (end year)"),
    refresh: bool = typer.Option(False, "--refresh", help="Download the files again"),
) -> None:
    """Load Medicare inpatient counts, the service-area ZIP codes and the Census poor-senior
    counts for every hospital, then compute the senior-exposure score and rank."""
    settings = Settings()
    today = _today()
    with session_scope(_engine(settings)) as session, httpx.Client() as http:
        catalog = cms_catalog(http, settings.ledger_dir / "catalog", refresh=refresh)
        loaded = load_exposure(
            session,
            http,
            settings.ledger_dir,
            catalog,
            today=today,
            refresh=refresh,
            acs_year=acs_year,
        )
        session.commit()
        metrics = compute_exposure_metrics(session, today)
    table = Table("Measure", "Count")
    for report in (loaded, metrics):
        for name, value in vars(report).items():
            table.add_row(name.replace("_", " "), str(value))
    console.print(table)


@ledger_app.command("show")
def ledger_show(ccn: str = typer.Option(..., "--ccn", help="Hospital CCN")) -> None:
    """Print one hospital's ledger: the three cards as the page shows them, then the JSON."""
    settings = Settings()
    with session_scope(_engine(settings)) as session:
        view = ledger_view(session, ccn)
    if view is None:
        console.print(f"{ccn}: no ledger data")
        raise typer.Exit(code=1)
    for label, metric in (
        ("Free care, promised vs spent", view.gap),
        ("Seniors at risk here", view.exposure),
        ("Collections", view.collections),
    ):
        console.print(f"[bold]{label}[/bold]: {metric.headline or 'Not enough data yet'}")
        if metric.sentence:
            console.print(f"  {metric.sentence}")
        if metric.advice:
            console.print(f"  What to do: {metric.advice}")
        if metric.missing:
            console.print(f"  Missing: {'; '.join(metric.missing)}")
        for source in metric.sources:
            console.print(f"  Source: {source.name}, {source.vintage}")
    typer.echo(json.dumps(view.model_dump(mode="json"), indent=1))
