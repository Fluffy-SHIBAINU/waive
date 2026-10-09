"""Price arithmetic for the Nebius footprint (spec §15). The constants are the published list
prices effective 2026-10-01, re-read 2026-10-09; the calculator figures in the fixtures were
returned by `nebius billing v1alpha1 calculator estimate` the same day. Every number is a Decimal:
no float ever touches a price."""

from decimal import Decimal

import pytest

from waive.cloud.costs import (
    EFFECTIVE,
    HOURS_PER_MONTH,
    MATCH_TOLERANCE,
    PRICES_DATE,
    PRICES_EFFECTIVE,
    SOURCES,
    Footprint,
    Price,
    Scenario,
    daily,
    disk_hourly,
    endpoint_hourly,
    monthly,
    parse_preset,
    postgres_hourly,
    reconcile,
    source_note,
    window_cost,
)


def test_parse_preset_reads_vcpu_and_gib():
    assert parse_preset("2vcpu-8gb") == (2, 8)
    assert parse_preset("16vcpu-64gb") == (16, 64)
    with pytest.raises(ValueError):
        parse_preset("medium")
    with pytest.raises(ValueError):
        parse_preset("1gpu-16vcpu-200gb")


def test_endpoint_hourly_follows_the_compute_price_list():
    assert endpoint_hourly("cpu-d3", "2vcpu-8gb") == Decimal("0.066")  # 2×0.015 + 8×0.0045
    assert endpoint_hourly("cpu-e2", "2vcpu-8gb") == Decimal("0.060")  # 2×0.012 + 8×0.0045
    assert endpoint_hourly("cpu-d3", "4vcpu-16gb") == Decimal("0.132")
    with pytest.raises(KeyError):
        endpoint_hourly("gpu-h100-sxm", "1gpu-16vcpu-200gb")


def test_disk_and_postgres_hourly_reproduce_the_docs_examples():
    assert HOURS_PER_MONTH == 730
    # 32 GiB of network-ssd: 32 × 0.071 / 730 = 0.0031123288…; the calculator returned
    # 0.00311232 (it rounds the per-second rate), which `reconcile` treats as a match.
    assert disk_hourly(32).quantize(Decimal("0.0000001")) == Decimal("0.0031123")
    assert abs(disk_hourly(32) - Decimal("0.00311232")) < MATCH_TOLERANCE
    # docs: "using a 4vcpu-16gb cluster for 1 hour costs 4 × $0.034 + 16 × $0.009 = $0.28"
    assert postgres_hourly("4vcpu-16gb", 0) == Decimal("0.28")
    assert postgres_hourly("2vcpu-8gb", 32).quantize(Decimal("0.0001")) == Decimal("0.1431")


def test_monthly_daily_and_window_costs_are_cents():
    assert monthly(Decimal("0.066")) == Decimal("48.18")
    assert daily(Decimal("0.066")) == Decimal("1.58")
    assert window_cost(Decimal("0.066"), Decimal("336")) == Decimal("22.18")
    assert window_cost(Decimal("0.066"), Decimal("0")) == Decimal("0.00")


def test_footprint_totals_and_hours_within_budget():
    footprint = Footprint("cpu-d3", "2vcpu-8gb", "2vcpu-8gb", 32)
    assert footprint.total.quantize(Decimal("0.0001")) == Decimal("0.2091")
    assert footprint.hours_in_budget() == 143
    assert monthly(footprint.endpoint) == Decimal("48.18")
    assert Footprint("cpu-d3", "2vcpu-8gb", "2vcpu-8gb", 32, Decimal("10")).hours_in_budget() == 47
    # The endpoint's container disk (`ai endpoint create --disk-size`, CLI default 250Gi) bills
    # at the Compute disk rate while the endpoint runs: 32 GiB adds 0.0031/h to the total.
    with_disk = Footprint("cpu-d3", "2vcpu-8gb", "2vcpu-8gb", 32, endpoint_disk_gib=32)
    assert with_disk.endpoint == Decimal("0.066")  # compute only, unchanged
    assert with_disk.endpoint_disk == disk_hourly(32)
    assert with_disk.total.quantize(Decimal("0.0001")) == Decimal("0.2122")
    assert with_disk.hours_in_budget() == 141
    assert footprint.endpoint_disk == Decimal("0")
    # The CLI default would cost 250 × 0.071 / 730 ≈ 0.0243/h, +37% on the endpoint's compute.
    assert disk_hourly(250).quantize(Decimal("0.0001")) == Decimal("0.0243")


def test_effective_date_is_scoped_to_the_compute_page():
    """Re-read 2026-10-09: only the Compute pricing page announces the 1 October 2026 change;
    the PostgreSQL, Serverless, Container Registry and SecretStash pages carry no effective date."""
    assert set(SOURCES) == {"compute", "serverless", "postgresql", "registry", "secretstash"}
    assert EFFECTIVE == {"compute": PRICES_EFFECTIVE}
    assert source_note("compute") == f"read {PRICES_DATE}; prices effective {PRICES_EFFECTIVE}"
    assert source_note("postgresql") == f"read {PRICES_DATE}; no effective date on the page"


def test_scenario_hours():
    scenario = Scenario()
    assert (scenario.window_days, scenario.demo_hours_per_day) == (14, Decimal("2"))
    assert scenario.hours == Decimal("336")
    assert scenario.demo_hours == Decimal("28")
    assert Scenario(window_days=7, demo_hours_per_day=Decimal("1.5")).demo_hours == Decimal("10.5")


def test_reconcile_prefers_the_calculator_and_marks_docs_only_prices_unverified():
    verified = reconcile(Decimal("0.066"), Decimal("0.066"), docs_url="https://docs.example/p")
    assert verified == Price(Decimal("0.066"), "calculator", True, "calculator matches the docs")

    differing = reconcile(Decimal("0.066"), Decimal("0.07"), docs_url="https://docs.example/p")
    assert differing.hourly == Decimal("0.07") and differing.verified
    assert "docs say 0.066" in differing.note

    docs_only = reconcile(Decimal("0.14"), None, docs_url="https://docs.example/p")
    assert docs_only.hourly == Decimal("0.14") and not docs_only.verified
    assert docs_only.source == "docs" and "https://docs.example/p" in docs_only.note

    # A price nobody could find is still a Price: the report prints it as unknown rather than 0.
    unknown = reconcile(None, None, docs_url="https://docs.example/p")
    assert unknown.hourly is None and not unknown.verified
    assert daily(unknown.hourly) is None and window_cost(unknown.hourly, Decimal("336")) is None
