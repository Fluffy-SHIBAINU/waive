"""Published Nebius list prices (USD, effective 2026-10-01) and the arithmetic for Waive's
footprint (spec §15). Every price is a Decimal. Sources, re-read 2026-10-09:

- Compute (Serverless AI endpoints bill at Compute prices; "while an endpoint is stopped, you are
  not billed for computing resources or storage"):
  https://docs.nebius.com/compute/resources/pricing.md and
  https://docs.nebius.com/serverless/pricing-quotas.md
- Managed PostgreSQL ("public access to clusters is free of charge"; no stop/pause billing rule):
  https://docs.nebius.com/postgresql/resources/pricing.md
- Container Registry (free): https://docs.nebius.com/container-registry/resources/pricing.md
- SecretStash (free in preview): https://docs.nebius.com/mysterybox/resources/pricing.md

`nebius billing v1alpha1 calculator estimate` prices Compute instances and disks (read-only; the
discovery uses it to verify the figures below) but has no Managed PostgreSQL spec, so the
cluster price stays docs-only. The console's Billing → Usage page is the source of truth once
resources exist; no CLI command exposes spend.
"""

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

PRICES_DATE = "2026-10-09"
PRICES_EFFECTIVE = "2026-10-01"
HOURS_PER_MONTH = 730
HOURS_PER_DAY = 24
SOURCES = {
    "compute": "https://docs.nebius.com/compute/resources/pricing.md",
    "serverless": "https://docs.nebius.com/serverless/pricing-quotas.md",
    "postgresql": "https://docs.nebius.com/postgresql/resources/pricing.md",
    "registry": "https://docs.nebius.com/container-registry/resources/pricing.md",
    "secretstash": "https://docs.nebius.com/mysterybox/resources/pricing.md",
}
CENT = Decimal("0.01")
# Calculator and docs agree when they are within a hundredth of a cent per hour (the docs
# round the disk rate per 730 h; the calculator does not).
MATCH_TOLERANCE = Decimal("0.0001")


@dataclass(frozen=True)
class PlatformPrice:
    vcpu_hour: Decimal
    gib_hour: Decimal


COMPUTE_PRICES = {
    # Non-GPU AMD EPYC Genoa, all regions.
    "cpu-d3": PlatformPrice(Decimal("0.015"), Decimal("0.0045")),
    # Non-GPU Intel Ice Lake, eu-north1 only.
    "cpu-e2": PlatformPrice(Decimal("0.012"), Decimal("0.0045")),
}
# Managed PostgreSQL hosts, both CPU families (per vCPU-hour, per GiB-hour).
POSTGRES_PRICE = PlatformPrice(Decimal("0.034"), Decimal("0.009"))
NETWORK_SSD_GIB_MONTH = Decimal("0.071")
FREE = (
    "Container Registry",
    "SecretStash (preview)",
    "PostgreSQL public access",
    "the endpoint's managed HTTPS URL",
)

_PRESET = re.compile(r"^(\d+)vcpu-(\d+)gb$")


def parse_preset(preset: str) -> tuple[int, int]:
    """`2vcpu-8gb` -> (2, 8). GPU presets (`1gpu-16vcpu-200gb`) are not CPU presets."""
    match = _PRESET.match(preset)
    if match is None:
        raise ValueError(f"unknown preset format {preset!r}; expected something like 2vcpu-8gb")
    return int(match.group(1)), int(match.group(2))


def endpoint_hourly(platform: str, preset: str) -> Decimal:
    price = COMPUTE_PRICES[platform]
    vcpu, gib = parse_preset(preset)
    return price.vcpu_hour * vcpu + price.gib_hour * gib


def disk_hourly(gib: int) -> Decimal:
    return NETWORK_SSD_GIB_MONTH * gib / HOURS_PER_MONTH


def postgres_hourly(preset: str, disk_gib: int) -> Decimal:
    vcpu, gib = parse_preset(preset)
    return POSTGRES_PRICE.vcpu_hour * vcpu + POSTGRES_PRICE.gib_hour * gib + disk_hourly(disk_gib)


def cents(value: Decimal | None) -> Decimal | None:
    """Round once, at the end, half up — never sum already-rounded cells."""
    return None if value is None else value.quantize(CENT, rounding=ROUND_HALF_UP)


def monthly(hourly: Decimal | None) -> Decimal | None:
    return None if hourly is None else cents(hourly * HOURS_PER_MONTH)


def daily(hourly: Decimal | None) -> Decimal | None:
    return None if hourly is None else cents(hourly * HOURS_PER_DAY)


def window_cost(hourly: Decimal | None, hours: Decimal) -> Decimal | None:
    return None if hourly is None else cents(hourly * hours)


@dataclass(frozen=True)
class Price:
    """An hourly price and where it came from. `hourly` is None when nobody could price the
    item; the report prints that as unknown rather than as zero."""

    hourly: Decimal | None
    source: str  # "calculator", "docs" or "none"
    verified: bool
    note: str = ""


def reconcile(docs_hourly: Decimal | None, api_hourly: Decimal | None, *, docs_url: str) -> Price:
    """Prefer the calculator's figure (it is what billing will charge); fall back to the docs and
    say so. A calculator figure that disagrees with the docs is used but the difference is noted."""
    if api_hourly is not None:
        if docs_hourly is not None and abs(api_hourly - docs_hourly) <= MATCH_TOLERANCE:
            return Price(api_hourly, "calculator", True, "calculator matches the docs")
        if docs_hourly is not None:
            return Price(
                api_hourly,
                "calculator",
                True,
                f"calculator differs from the docs (docs say {docs_hourly})",
            )
        return Price(api_hourly, "calculator", True, "calculator only; not on the docs page")
    if docs_hourly is not None:
        return Price(
            docs_hourly,
            "docs",
            False,
            f"no calculator estimate; docs {docs_url} read {PRICES_DATE}",
        )
    return Price(None, "none", False, f"no price found in the calculator or at {docs_url}")


@dataclass(frozen=True)
class Footprint:
    platform: str
    endpoint_preset: str
    postgres_preset: str
    disk_gib: int
    budget_usd: Decimal = Decimal("30")

    @property
    def endpoint(self) -> Decimal:
        return endpoint_hourly(self.platform, self.endpoint_preset)

    @property
    def postgres(self) -> Decimal:
        return postgres_hourly(self.postgres_preset, self.disk_gib)

    @property
    def total(self) -> Decimal:
        return self.endpoint + self.postgres

    def hours_in_budget(self) -> int:
        return int(self.budget_usd / self.total)


@dataclass(frozen=True)
class Scenario:
    """The judging window and the plan's start/stop strategy: the endpoint runs only for demos."""

    window_days: int = 14
    demo_hours_per_day: Decimal = Decimal("2")

    @property
    def hours(self) -> Decimal:
        return Decimal(self.window_days * HOURS_PER_DAY)

    @property
    def demo_hours(self) -> Decimal:
        return self.demo_hours_per_day * self.window_days
