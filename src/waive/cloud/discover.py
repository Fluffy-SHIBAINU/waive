"""Read-only discovery of what the project offers, and the cost report for gate U6.1 (task 6.3).

Only `version`, `get`, `list`, `config get`, `billing … calculator estimate` and `--help` calls:
nothing here creates, changes or deletes a resource, and the wrapper refuses mutating verbs
anyway. Ids and raw CLI output go to a gitignored JSON file; the committed report carries names,
counts, presets and prices only (the project id appears masked as `project-…last4`).
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from waive.cloud.costs import (
    COMPUTE_PRICES,
    FREE,
    PRICES_DATE,
    PRICES_EFFECTIVE,
    SOURCES,
    Footprint,
    Price,
    Scenario,
    daily,
    disk_hourly,
    endpoint_hourly,
    parse_preset,
    postgres_hourly,
    reconcile,
    window_cost,
)
from waive.cloud.nebius import (
    MUTATING_VERBS,
    Nebius,
    NebiusError,
    id_of,
    items,
    mask_id,
    name_of,
)

__all__ = [
    "MUTATING_VERBS",  # re-exported so tests of the discovery can assert on it
    "Discovery",
    "discover",
    "platform_known",
    "render_gate_message",
    "render_report",
    "smallest_cpu_preset",
    "write_discovery",
]

PREFIX = "waive-"
VM_DISK_GIB = 20  # the fallback VM's boot disk (plan task 6.6b)
ALT_PRESET = "4vcpu-16gb"  # if the endpoint refuses 2vcpu-8gb (plan "not verified" item 1)
DISK_TYPE = "network_ssd"
# Discovery key -> report label for the listings that count existing `waive-*` resources.
LISTINGS = {
    "endpoint": ("endpoints", ("ai", "endpoint", "list")),
    "postgres": ("clusters", ("msp", "postgresql", "v1alpha1", "cluster", "list")),
    "secret": ("secrets", ("mysterybox", "secret", "list")),
    "registry": ("registries", ("registry", "list")),
    "instance": ("instances", ("compute", "instance", "list")),
}
_COMMAND_LINE = re.compile(r"^\s+stop\s*$", re.MULTILINE)


@dataclass
class Discovery:
    cli_version: str
    project_id_masked: str
    project_id_source: str
    project_warning: str
    project_name: str
    region: str | None
    platforms: dict[str, list[str]]  # platform name -> preset names, in the CLI's order
    preset_resources: dict[str, dict[str, tuple[int, int]]]  # platform -> preset -> (vcpu, gib)
    networks: list[tuple[str, str]]  # (name, id)
    subnets: list[tuple[str, str]]
    existing: dict[str, list[str]]  # kind -> names starting with "waive-" ([] if listing failed)
    estimates: dict[str, Decimal | None]  # "cpu-d3/2vcpu-8gb" or "network_ssd/32" -> $/hour
    postgres_create_help: str
    postgres_help: str
    endpoint_create_help: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def postgres_can_stop(self) -> bool:
        """The plan assumed no stop command; the CLI (0.12.287) lists `cluster stop`."""
        return _COMMAND_LINE.search(self.postgres_help) is not None

    def listing_failed(self, kind: str) -> bool:
        return LISTINGS[kind][0] in self.raw.get("errors", {})


class _Recording:
    """Wraps the runner so the raw file can hold every argv (ids included; it is gitignored)."""

    def __init__(self, runner: Callable[..., Any]) -> None:
        self.runner = runner
        self.calls: list[list[str]] = []

    def __call__(self, argv: Any) -> Any:
        self.calls.append(list(argv))
        return self.runner(argv)


def _find_key(payload: Any, wanted: tuple[str, ...]) -> str | None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in wanted and isinstance(value, str):
                return value
            found = _find_key(value, wanted)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_key(value, wanted)
            if found:
                return found
    return None


def _presets(platform: dict[str, Any]) -> dict[str, tuple[int, int]]:
    """Preset name -> (vcpu, GiB), from `spec.presets[].resources` or the name itself."""
    found: dict[str, tuple[int, int]] = {}
    presets = platform.get("spec", {}).get("presets") or platform.get("presets") or []
    for preset in presets:
        if isinstance(preset, str):
            name, resources = preset, {}
        elif isinstance(preset, dict):
            name = str(preset.get("name") or preset.get("id") or "")
            resources = preset.get("resources") or {}
        else:
            continue
        if not name:
            continue
        vcpu, gib = resources.get("vcpu_count"), resources.get("memory_gibibytes")
        if isinstance(vcpu, int) and isinstance(gib, int):
            found[name] = (vcpu, gib)
        else:
            try:
                found[name] = parse_preset(name)
            except ValueError:
                found[name] = (0, 0)
    return found


def _waive_names(payload: Any) -> list[str]:
    return [name_of(item) for item in items(payload) if name_of(item).startswith(PREFIX)]


def _cost(payload: Any) -> Decimal:
    """`{"hourly_cost": {"general": {"total": {"cost": "0.066", …}}}}` -> Decimal("0.066")."""
    try:
        return Decimal(str(payload["hourly_cost"]["general"]["total"]["cost"]))
    except (KeyError, TypeError, InvalidOperation) as error:
        raise NebiusError(
            f"calculator answer has no hourly_cost.general.total.cost: {error}"
        ) from None


def discover(nebius: Nebius, footprint: Footprint, *, vm_disk_gib: int = VM_DISK_GIB) -> Discovery:
    recording = _Recording(nebius.runner)
    nebius = replace(nebius, runner=recording)
    raw: dict[str, Any] = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "project_id_source": nebius.project_id_source,
        "errors": {},
        "calls": recording.calls,
    }

    def attempt(key: str, *command: str, parent: bool = True, json_output: bool = True) -> Any:
        try:
            raw[key] = nebius.run(*command, parent=parent, json_output=json_output)
        except NebiusError as error:
            raw["errors"][key] = str(error)
            raw[key] = {} if json_output else ""
        return raw[key]

    version = attempt("version", "version", parent=False, json_output=False).strip()
    project = attempt("project", "iam", "v2", "project", "get", nebius.project_id, parent=False)
    platforms: dict[str, list[str]] = {}
    preset_resources: dict[str, dict[str, tuple[int, int]]] = {}
    for platform in items(attempt("platforms", "compute", "platform", "list")):
        name = name_of(platform) or id_of(platform)
        preset_resources[name] = _presets(platform)
        platforms[name] = list(preset_resources[name])
    networks = [
        (name_of(n), id_of(n)) for n in items(attempt("networks", "vpc", "network", "list"))
    ]
    subnets = [(name_of(s), id_of(s)) for s in items(attempt("subnets", "vpc", "subnet", "list"))]
    existing = {
        kind: _waive_names(attempt(key, *command)) for kind, (key, command) in LISTINGS.items()
    }

    estimates: dict[str, Decimal | None] = {}
    spec = "--resource-spec-compute-instance-spec-"
    for preset in dict.fromkeys((footprint.endpoint_preset, ALT_PRESET)):
        key = f"{footprint.platform}/{preset}"
        payload = attempt(
            f"estimate:{key}",
            "billing",
            "v1alpha1",
            "calculator",
            "estimate",
            f"{spec}parent-id",
            nebius.project_id,
            f"{spec}resources-platform",
            footprint.platform,
            f"{spec}resources-preset",
            preset,
            parent=False,
        )
        estimates[key] = _estimate(raw, f"estimate:{key}", payload)
    spec = "--resource-spec-compute-disk-spec-"
    for gib in dict.fromkeys((footprint.disk_gib, vm_disk_gib)):
        key = f"{DISK_TYPE}/{gib}"
        payload = attempt(
            f"estimate:{key}",
            "billing",
            "v1alpha1",
            "calculator",
            "estimate",
            f"{spec}parent-id",
            nebius.project_id,
            f"{spec}type",
            DISK_TYPE,
            f"{spec}size-gibibytes",
            str(gib),
            parent=False,
        )
        estimates[key] = _estimate(raw, f"estimate:{key}", payload)

    postgres_create_help = attempt(
        "postgres_create_help",
        *LISTINGS["postgres"][1][:-1],
        "create",
        "--help",
        parent=False,
        json_output=False,
    )
    postgres_help = attempt(
        "postgres_help", *LISTINGS["postgres"][1][:-1], "--help", parent=False, json_output=False
    )
    endpoint_create_help = attempt(
        "endpoint_create_help",
        "ai",
        "endpoint",
        "create",
        "--help",
        parent=False,
        json_output=False,
    )
    region = None
    if isinstance(project, dict):
        region = project.get("spec", {}).get("region") or _find_key(
            project, ("region", "region_id")
        )
    return Discovery(
        cli_version=version,
        project_id_masked=mask_id(nebius.project_id),
        project_id_source=nebius.project_id_source,
        project_warning=nebius.warning,
        project_name=name_of(project) if isinstance(project, dict) else "",
        region=region,
        platforms=platforms,
        preset_resources=preset_resources,
        networks=networks,
        subnets=subnets,
        existing=existing,
        estimates=estimates,
        postgres_create_help=postgres_create_help,
        postgres_help=postgres_help,
        endpoint_create_help=endpoint_create_help,
        raw=raw,
    )


def _estimate(raw: dict[str, Any], key: str, payload: Any) -> Decimal | None:
    if key in raw["errors"]:
        return None
    try:
        return _cost(payload)
    except NebiusError as error:
        raw["errors"][key] = str(error)
        return None


def smallest_cpu_preset(platforms: dict[str, list[str]], platform: str) -> str | None:
    candidates = []
    for preset in platforms.get(platform, []):
        try:
            candidates.append((parse_preset(preset), preset))
        except ValueError:
            continue
    return min(candidates)[1] if candidates else None


# --- the report -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    resource: str
    preset: str
    price: Price
    always_hours: Decimal
    stopped_hours: Decimal
    stopped_note: str = ""


def _docs(fn: Callable[..., Decimal], *args: Any) -> Decimal | None:
    try:
        return fn(*args)
    except (KeyError, ValueError):
        return None


def cost_rows(d: Discovery, fp: Footprint, sc: Scenario) -> tuple[list[Row], list[Row]]:
    """(main footprint rows, fallback VM rows). The "stopped between demos" column runs the
    endpoint or VM for the demo hours only; the database and every disk bill for the whole
    window because neither can be switched off without being deleted (the cluster's `stop`
    suspends it, with no published billing rule)."""
    compute_key = f"{fp.platform}/{fp.endpoint_preset}"
    endpoint = reconcile(
        _docs(endpoint_hourly, fp.platform, fp.endpoint_preset),
        d.estimates.get(compute_key),
        docs_url=SOURCES["compute"],
    )
    postgres = reconcile(
        _docs(postgres_hourly, fp.postgres_preset, 0), None, docs_url=SOURCES["postgresql"]
    )
    pg_disk = reconcile(
        disk_hourly(fp.disk_gib),
        d.estimates.get(f"{DISK_TYPE}/{fp.disk_gib}"),
        docs_url=SOURCES["postgresql"],
    )
    free = Price(Decimal("0"), "docs", False, "free per the docs; the calculator has no entry")
    vm_disk = reconcile(
        disk_hourly(VM_DISK_GIB),
        d.estimates.get(f"{DISK_TYPE}/{VM_DISK_GIB}"),
        docs_url=SOURCES["compute"],
    )
    main = [
        Row(
            "Serverless AI endpoint `waive-web`",
            f"`{fp.platform}` `{fp.endpoint_preset}`, port 8000",
            endpoint,
            sc.hours,
            sc.demo_hours,
            "runs only for demos",
        ),
        Row(
            "Managed PostgreSQL `waive-db`, host",
            f"`{fp.postgres_preset}`, 1 host, PostgreSQL 16",
            postgres,
            sc.hours,
            sc.hours,
            "kept running: bills until deleted",
        ),
        Row(
            "Managed PostgreSQL `waive-db`, disk",
            f"{fp.disk_gib} GiB network-ssd",
            pg_disk,
            sc.hours,
            sc.hours,
            "kept with the cluster",
        ),
        Row("Container Registry `waive-registry`", "—", free, sc.hours, sc.hours),
        Row("SecretStash secret `waive-secrets`", "—", free, sc.hours, sc.hours),
    ]
    fallback = [
        Row(
            "Fallback VM `waive-vm` (Compose: app + PostgreSQL + Caddy)",
            f"`{fp.platform}` `{fp.endpoint_preset}`",
            replace(endpoint, note=endpoint.note + "; same Compute price as the endpoint"),
            sc.hours,
            sc.demo_hours,
            "stopped between demos: compute not charged",
        ),
        Row(
            "Fallback VM boot disk",
            f"{VM_DISK_GIB} GiB network-ssd",
            vm_disk,
            sc.hours,
            sc.hours,
            "charged while the VM is stopped",
        ),
    ]
    return main, fallback


def _sum(values: list[Decimal | None]) -> Decimal | None:
    return None if any(v is None for v in values) else sum(values, Decimal("0"))


def _total(rows: list[Row], hours_of: Callable[[Row], Decimal]) -> Decimal | None:
    """Unrounded window cost of several rows; `_money` rounds it once."""
    return _sum(
        [None if row.price.hourly is None else row.price.hourly * hours_of(row) for row in rows]
    )


def _money(value: Decimal | None, places: str = "0.01") -> str:
    if value is None:
        return "n/a"
    return f"{value.quantize(Decimal(places), rounding=ROUND_HALF_UP)}"


def _verified(price: Price) -> str:
    if price.hourly is None:
        return "unknown"
    if price.verified:
        extra = " (differs from the docs)" if "differs" in price.note else ""
        return f"calculator + docs{extra}"
    if price.hourly == 0:
        return "free per docs (unverified)"
    return "docs only (unverified)"


def _table(rows: list[Row], total_label: str) -> list[str]:
    lines = [
        "| Resource | Chosen preset | $/hour | $/day | 2-week window, always on | "
        "2-week window, stopped between demos | Verified |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        h = row.price.hourly
        note = f" ({row.stopped_note})" if row.stopped_note else ""
        lines.append(
            f"| {row.resource} | {row.preset} | {_money(h, '0.0001')} | {_money(daily(h))} | "
            f"{_money(window_cost(h, row.always_hours))} | "
            f"{_money(window_cost(h, row.stopped_hours))}{note} | {_verified(row.price)} |"
        )
    hourly = _sum([row.price.hourly for row in rows])
    lines.append(
        f"| **{total_label}** | | **{_money(hourly, '0.0001')}** | **{_money(daily(hourly))}** "
        f"| **{_money(_total(rows, lambda row: row.always_hours))}** | "
        f"**{_money(_total(rows, lambda row: row.stopped_hours))}** | |"
    )
    return lines


def _platform_notes(d: Discovery, fp: Footprint) -> list[str]:
    notes = []
    if fp.platform not in d.platforms:
        notes.append(
            f"- `{fp.platform}` is not in this project's platform listing; set "
            "`WAIVE_CLOUD_PLATFORM` to one of the `cpu-*` platforms above."
        )
    elif fp.endpoint_preset not in d.platforms[fp.platform]:
        smallest = smallest_cpu_preset(d.platforms, fp.platform)
        notes.append(
            f"- `{fp.endpoint_preset}` is not a `{fp.platform}` preset here; the smallest is "
            f"`{smallest}` — set `WAIVE_CLOUD_PRESET` and re-run."
        )
    if "cpu-e2" not in d.platforms and d.region != "eu-north1":
        notes.append(
            f"- `cpu-e2` (Intel, eu-north1 only) is not offered in {d.region or 'this region'}; "
            "the plan's Managed PostgreSQL command names `cpu-e2`, so task 6.5 must pass "
            f"`--config-template-resources-platform {fp.platform}` instead (unverified until the "
            "create call validates it; a rejected create creates nothing)."
        )
    return notes


def render_report(d: Discovery, fp: Footprint, sc: Scenario, generated: str) -> str:
    cpu = {k: v for k, v in d.platforms.items() if k.startswith("cpu-")}
    gpu = sorted(k for k in d.platforms if not k.startswith("cpu-"))
    main, fallback = cost_rows(d, fp, sc)
    main_hourly = _sum([row.price.hourly for row in main])
    main_stopped = _total(main, lambda row: row.stopped_hours)
    db_hourly = _sum([row.price.hourly for row in main[1:3]])
    db_recreated = None if db_hourly is None else db_hourly * sc.demo_hours
    recreated_total = (
        None
        if main_stopped is None or db_hourly is None
        else main_stopped - db_hourly * sc.hours + db_hourly * sc.demo_hours
    )
    lines = [
        "# Nebius AI Cloud: discovery and cost estimate for Waive (gate U6.1)",
        "",
        f"Generated by `waive cloud discover` on {generated}. Read-only: nothing was created, "
        "changed or deleted (the wrapper refuses mutating verbs). Resource ids, every CLI argv and "
        "the raw output are in `var/cloud-discovery.json` (gitignored); this file holds names, "
        "counts, presets and prices only.",
        "",
        "## Project",
        "",
        f"- Nebius CLI: {d.cli_version or 'unknown'}",
        f"- Project: {d.project_name or 'unknown'} ({d.project_id_masked}, id from "
        f"{d.project_id_source}); region: "
        f"{d.region or 'not in the project record — read it from the console URL'}",
        f"- Networks: {', '.join(name for name, _ in d.networks) or 'none listed'}; "
        f"subnets: {', '.join(name for name, _ in d.subnets) or 'none listed'}"
        + (" (one subnet: the endpoint needs no `--subnet-id`)" if len(d.subnets) == 1 else ""),
    ]
    if d.project_warning:
        lines += ["", f"> **Warning (gate U0.3):** {d.project_warning}"]
    lines += ["", "## Platforms and presets offered to this project", ""]
    if cpu:
        for platform, presets in sorted(cpu.items()):
            shown = ", ".join(presets) if presets else "presets not in the listing (see raw JSON)"
            lines.append(f"- `{platform}`: {shown}")
    else:
        lines.append(
            "- no `cpu-*` platform in the listing (see the raw JSON); the docs list `cpu-d3` "
            "(AMD EPYC Genoa, all regions) and `cpu-e2` (Intel Ice Lake, eu-north1 only)"
        )
    if gpu:
        lines.append(f"- GPU platforms (not used): {', '.join(f'`{p}`' for p in gpu)}")
    lines += _platform_notes(d, fp)
    lines += [
        "",
        "## Cost table",
        "",
        f"USD. List prices effective {PRICES_EFFECTIVE}, docs re-read {PRICES_DATE}; "
        '"calculator" means `nebius billing v1alpha1 calculator estimate` returned the same '
        f"figure on {generated} (read-only). Window: {sc.window_days} days = {sc.hours} hours; "
        f'"stopped between demos" runs the endpoint {sc.demo_hours_per_day} h/day = '
        f"{sc.demo_hours} demo hours and keeps the database and disks for the whole window.",
        "",
        *_table(main, "Total, endpoint + database"),
        "",
        f"- Both running: ≈ ${_money(main_hourly, '0.0001')}/hour, ≈ ${_money(daily(main_hourly))}/day; "
        f"the ${fp.budget_usd} budget (master plan, through 2026-10-30) buys about "
        f"{fp.hours_in_budget()} hours with both running, or about "
        f"{int(fp.budget_usd / fp.postgres)} hours of the database alone (docs prices).",
        f"- Database deleted between windows instead of kept: `waive-db` then costs ≈ "
        f"${_money(db_recreated)} for the {sc.demo_hours} demo hours (plus a few minutes of "
        "creation per window, `waive cloud cleanup` + task 6.5's create + `waive db copy "
        f"--to-cloud`), and the window total becomes ≈ ${_money(recreated_total)}.",
        (
            '- The CLI has `nebius msp postgresql v1alpha1 cluster stop` ("Suspends the PostgreSQL '
            'cluster to save resources") and `start` ("Wakes up suspended PostgreSQL cluster"); '
            "the docs publish no billing rule for a suspended cluster, so a third option — "
            "suspend between windows — is **unverified** and not priced here."
            if d.postgres_can_stop
            else "- This CLI's `msp postgresql v1alpha1 cluster --help` lists no `stop` command; "
            "the database bills until deleted."
        ),
        f"- A stopped endpoint is not billed (docs, {SOURCES['serverless']}). "
        f"Free: {', '.join(FREE)}.",
        "",
        "### Fallback: one VM with Docker Compose and Caddy (plan task 6.6b)",
        "",
        "Used only if the endpoint cannot serve a plain web app, or if chosen at U6.1 for cost: "
        "the VM replaces the managed cluster (PostgreSQL runs in a container on the VM's disk), "
        "HTTPS comes from Caddy + Let's Encrypt on a `sslip.io` name. Registry and SecretStash "
        "stay free.",
        "",
        *_table(fallback, "Total, fallback VM"),
        "",
        "## Existing `waive-` resources (counts; should all be 0 before gate U6.1)",
        "",
    ]
    for kind, names in d.existing.items():
        shown = "unknown (listing failed)" if d.listing_failed(kind) else str(len(names))
        lines.append(f"- {kind}: {shown}")
    lines += [
        "",
        "## Gate U6.1 — what the user approves",
        "",
        render_gate_message(d, fp, sc),
        "",
        "## Price sources",
        "",
        f"- `nebius billing v1alpha1 calculator estimate` (Compute instances and disks; no "
        f"Managed PostgreSQL spec), run {generated}",
        *[
            f"- {url} (read {PRICES_DATE}; prices effective {PRICES_EFFECTIVE})"
            for url in SOURCES.values()
        ],
        "",
        "## Unverified — to confirm on the day",
        "",
        "- Managed PostgreSQL prices come from the docs only (the calculator cannot price a "
        "cluster): $0.034 per vCPU-hour + $0.009 per GiB-hour, network-ssd $0.071 per GiB per 730 h.",
        "- The endpoint accepts `2vcpu-8gb`: `ai endpoint create --help` says the default preset is "
        '"minimum available preset for the platform" and `/dev/shm` defaults to 0 for CPU '
        f"platforms, so CPU endpoints exist; if `2vcpu-8gb` is refused, `{ALT_PRESET}` costs "
        f"${_money(d.estimates.get(f'{fp.platform}/{ALT_PRESET}'), '0.0001')}/hour "
        f"(calculator) and the report is regenerated with `WAIVE_CLOUD_PRESET={ALT_PRESET}`.",
        "- The endpoint pulls from the project's own Container Registry without extra credentials.",
        "- The smallest disk Managed PostgreSQL accepts: `cluster create --help` names no minimum "
        f"(its example uses 96 GiB); the plan tries {fp.disk_gib} GiB and uses whatever minimum "
        "the create call names (a rejected call creates nothing).",
        "- The docs document Managed PostgreSQL on `cpu-e2` only, but its pricing page lists AMD "
        "EPYC Genoa in eu-west2 and this project offers `cpu-d3` only; the create call decides.",
        "- Billing of a suspended (`cluster stop`) cluster: not published.",
        "- Egress and public IPs: the Compute pricing page lists no prices; the endpoint's managed "
        "HTTPS URL needs no public IP, so $0 is assumed.",
        "- Spend is visible only in the console (Billing → Usage); no CLI command exposes it.",
    ]
    if d.raw.get("errors"):
        lines += ["", "## CLI calls that failed during discovery", ""]
        lines += [f"- `{key}`: {error}" for key, error in d.raw["errors"].items()]
    return "\n".join(lines) + "\n"


def render_gate_message(d: Discovery, fp: Footprint, sc: Scenario) -> str:
    main, fallback = cost_rows(d, fp, sc)
    pg_hourly = fp.postgres
    main_hourly = _sum([row.price.hourly for row in main])
    always = _total(main, lambda row: row.always_hours)
    stopped = _total(main, lambda row: row.stopped_hours)
    db_hourly = _sum([row.price.hourly for row in main[1:3]])
    recreated = (
        None
        if stopped is None or db_hourly is None
        else stopped - db_hourly * sc.hours + db_hourly * sc.demo_hours
    )
    vm_disk_day = daily(fallback[1].price.hourly)
    suspend = (
        "; the CLI also has a `cluster stop` that suspends it, billing while suspended unverified"
        if d.postgres_can_stop
        else ""
    )
    return (
        "**Gate U6.1 — approve Nebius resources and costs.** The plan creates four things in "
        f"project *{d.project_name or 'unknown'}* (region *{d.region or 'unknown'}*), all named "
        f"`waive-*`: a Container Registry (free), a Managed PostgreSQL cluster "
        f"`{fp.postgres_preset}` + {fp.disk_gib} GiB (≈ ${pg_hourly:.3f}/h, ≈ "
        f"${daily(pg_hourly)}/day, billed until deleted{suspend}), a SecretStash secret with the "
        "six runtime secrets (free), and a CPU Serverless AI endpoint "
        f"`{fp.platform} {fp.endpoint_preset}` on port 8000 with no endpoint auth (≈ "
        f"${fp.endpoint:.3f}/h while running, $0 stopped; the public HTTPS URL is managed by "
        f"Nebius). Both running ≈ ${_money(main_hourly)}/h; the ${fp.budget_usd} Phase 6 budget "
        f"covers ≈ {fp.hours_in_budget()} hours. Over a {sc.window_days}-day judging window: ≈ "
        f"${_money(always)} always on; ≈ ${_money(stopped)} with the endpoint run only for "
        f"{sc.demo_hours} demo hours and the database kept; ≈ ${_money(recreated)} if the "
        "database is also deleted between windows. Fallback (one `cpu-d3 "
        f"{fp.endpoint_preset}` VM `waive-vm` with Compose + Caddy instead of the endpoint and "
        f"the cluster): ≈ ${fp.endpoint:.3f}/h running, ≈ ${_money(vm_disk_day)}/day stopped. "
        "Please answer: (1) approve these resources; (2) keep the database between demo windows "
        f"(≈ ${daily(pg_hourly)}/day) or delete and recreate it per window; (3) confirm the "
        "endpoint runs only when you say so (`waive cloud start|stop`). Details: "
        "`docs/reports/cloud-costs.md`."
    )


def write_discovery(
    d: Discovery, fp: Footprint, sc: Scenario, report_path: Path, raw_path: Path
) -> str:
    raw = dict(d.raw)
    raw["footprint"] = {
        "platform": fp.platform,
        "endpoint_preset": fp.endpoint_preset,
        "postgres_preset": fp.postgres_preset,
        "disk_gib": fp.disk_gib,
        "budget_usd": str(fp.budget_usd),
        "window_days": sc.window_days,
        "demo_hours_per_day": str(sc.demo_hours_per_day),
    }
    raw["estimates_hourly"] = {k: None if v is None else str(v) for k, v in d.estimates.items()}
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(raw, indent=1, default=str), encoding="utf-8")
    text = render_report(d, fp, sc, datetime.now(UTC).date().isoformat())
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(text, encoding="utf-8")
    return text


def platform_known(platform: str) -> bool:
    return platform in COMPUTE_PRICES
