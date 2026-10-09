"""The nebius CLI wrapper and the read-only discovery (task 6.3). Every CLI call is answered by
FakeRunner from JSON recorded from the real CLI on 2026-10-09 (project and resource ids masked);
no `nebius` binary is needed and none is run."""

import json
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from waive.cli import app
from waive.cloud import cli as cloud_cli
from waive.cloud.costs import Footprint, Scenario
from waive.cloud.discover import (
    MUTATING_VERBS,
    discover,
    render_gate_message,
    smallest_cpu_preset,
    write_discovery,
)
from waive.cloud.nebius import SAFE_FLAGS, Nebius, NebiusError, NebiusMissing, items, mask_ids
from waive.config import Settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cloud"


def fixture(name: str):
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


class FakeRunner:
    """Answers `nebius …` invocations from a table keyed by the longest matching prefix of the
    arguments after `nebius`. An Exception value makes that command fail with its text as stderr.
    Reused by tasks 6.6 and 6.7."""

    def __init__(self, answers):
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        joined = " ".join(argv[1:])
        for key in sorted(self.answers, key=len, reverse=True):
            if joined.startswith(key):
                answer = self.answers[key]
                if isinstance(answer, Exception):
                    return subprocess.CompletedProcess(argv, 1, "", str(answer))
                stdout = answer if isinstance(answer, str) else json.dumps(answer)
                return subprocess.CompletedProcess(argv, 0, stdout, "")
        return subprocess.CompletedProcess(argv, 1, "", f"unexpected command: {joined}")


CALC = "billing v1alpha1 calculator estimate"
VM = f"{CALC} --resource-spec-compute-instance-spec-parent-id project-test "
VM += "--resource-spec-compute-instance-spec-resources-platform cpu-d3 "
VM += "--resource-spec-compute-instance-spec-resources-preset "
DISK = f"{CALC} --resource-spec-compute-disk-spec-parent-id project-test "
DISK += "--resource-spec-compute-disk-spec-type network_ssd "
DISK += "--resource-spec-compute-disk-spec-size-gibibytes "

ANSWERS = {
    "version": fixture("version.txt"),
    "config get parent-id": "project-test\n",
    "iam v2 project get project-test": fixture("project.json"),
    "compute platform list": fixture("platforms.json"),
    "vpc network list": fixture("networks.json"),
    "vpc subnet list": fixture("subnets.json"),
    "ai endpoint list": fixture("empty.json"),
    "msp postgresql v1alpha1 cluster list": fixture("empty.json"),
    "mysterybox secret list": fixture("empty.json"),
    "registry list": fixture("empty.json"),
    "compute instance list": fixture("empty.json"),
    f"{VM}2vcpu-8gb": fixture("calculator_vm_cpu_d3_2vcpu_8gb.json"),
    f"{VM}4vcpu-16gb": fixture("calculator_vm_cpu_d3_4vcpu_16gb.json"),
    f"{DISK}32": fixture("calculator_disk_network_ssd_32gib.json"),
    f"{DISK}20": fixture("calculator_disk_network_ssd_20gib.json"),
    "msp postgresql v1alpha1 cluster create --help": fixture("cluster_create_help.txt"),
    "msp postgresql v1alpha1 cluster --help": fixture("cluster_help.txt"),
    "ai endpoint create --help": fixture("endpoint_create_help.txt"),
    "ai endpoint --help": "  NAME\n      nebius ai endpoint\n",
}
FOOTPRINT = Footprint("cpu-d3", "2vcpu-8gb", "2vcpu-8gb", 32, endpoint_disk_gib=32)


def settings(**overrides) -> Settings:
    values = {"nebius_project_id": "project-test"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_wrapper_adds_parent_json_and_safety_flags_and_parses():
    runner = FakeRunner(ANSWERS)
    nebius = Nebius("project-test", runner=runner)
    payload = nebius.run("vpc", "network", "list")
    assert items(payload)[0]["metadata"]["name"] == "default-network"
    assert runner.calls[0] == [
        "nebius",
        "vpc",
        "network",
        "list",
        "--parent-id",
        "project-test",
        "--format",
        "json",
        *SAFE_FLAGS,
    ]
    assert "--no-browser" in SAFE_FLAGS
    assert nebius.text("version") == "0.12.287\n"
    assert runner.calls[1] == ["nebius", "version", *SAFE_FLAGS]
    assert nebius.text("ai", "endpoint", "--help").startswith("  NAME")
    assert runner.calls[2] == ["nebius", "ai", "endpoint", "--help"]  # help needs no flags
    assert items([{"metadata": {"name": "bare-list"}}, "noise"]) == [
        {"metadata": {"name": "bare-list"}}
    ]
    assert items({}) == [] and items("") == []


def test_wrapper_refuses_mutating_verbs_unless_opted_out():
    runner = FakeRunner({"registry create": {"metadata": {"id": "registry-x"}}})
    with pytest.raises(NebiusError, match="read-only"):
        Nebius("project-test", runner=runner).run("registry", "create", "--name", "waive-registry")
    assert runner.calls == []  # refused before the CLI ran
    Nebius("project-test", runner=runner).text("registry", "create", "--help")  # help is fine
    writable = Nebius("project-test", runner=runner, read_only=False)
    assert writable.run("registry", "create", "--name", "waive-registry")["metadata"]["id"]


def test_wrapper_read_only_is_an_allowlist_not_a_denylist():
    """CLI 0.12.287 has verbs the old denylist never named: `purge` (storage bucket, permanent),
    `revoke` (iam static-key), `deactivate` (iam access-key), `ssh`/`logs` (ai endpoint),
    `docker-credential` (registry), `get-secret-once` (iam access-key). Only reads run."""
    refused = [
        ("storage", "bucket", "purge", "--id", "x"),
        ("iam", "static-key", "revoke", "--id", "x"),
        ("iam", "access-key", "deactivate", "--id", "x"),
        ("ai", "endpoint", "ssh", "--id", "x"),
        ("ai", "endpoint", "logs", "--id", "x"),
        ("registry", "docker-credential"),
        ("iam", "access-key", "get-secret-once", "--id", "x"),
        ("iam", "v2", "access-key", "get-secret", "--id", "x"),
        ("profile", "activate", "other"),
        ("compute", "instance", "delete", "list"),  # a read verb never excuses a mutating one
    ]
    for args in refused:
        runner = FakeRunner({" ".join(args): {}})
        with pytest.raises(NebiusError, match="read-only"):
            Nebius("project-test", runner=runner).run(*args)
        assert runner.calls == [], args
    allowed = [
        ("version",),
        ("config", "get", "parent-id"),
        ("compute", "platform", "list"),
        ("iam", "v2", "project", "get", "project-test"),
        ("ai", "endpoint", "get-by-name", "--name", "waive-web"),
        ("compute", "instance", "batch-get", "--ids", "x"),
        ("storage", "bucket", "list-with-filter"),
        ("billing", "v1alpha1", "calculator", "estimate", "--resource-spec-x", "y"),
        ("storage", "bucket", "purge", "--help"),
    ]
    for args in allowed:
        runner = FakeRunner({" ".join(args): {}})
        Nebius("project-test", runner=runner).run(*args)
        assert len(runner.calls) == 1, args


def test_wrapper_masks_resource_ids_in_error_text():
    """The real CLI's errors embed the full project id ("… not found in container 'project-…'")
    and echo the argv; the message must never carry one, because it ends up in the report."""
    stderr = (
        "Error: rpc error: code = NotFound desc = get preset spec: platform 'cpu-e2' not found in "
        "container 'project-test'; parent vpcnetwork-e00abcdef12345678 tenantuseraccount-e00zz "
        "mbsec-e00secret1234 (argv: --parent-id project-test)"
    )
    runner = FakeRunner({"compute platform list": RuntimeError(stderr)})
    with pytest.raises(NebiusError) as error:
        Nebius("project-test", runner=runner).run("compute", "platform", "list")
    message = str(error.value)
    assert "project-test" not in message and "project-…test" in message
    assert "vpcnetwork-e00abcdef12345678" not in message and "vpcnetwork-…5678" in message
    assert "tenantuseraccount-e00zz" not in message and "mbsec-e00secret1234" not in message
    assert "platform 'cpu-e2' not found" in message  # the useful part survives
    assert mask_ids("waive-secrets and cpu-d3 2vcpu-8gb stay") == (
        "waive-secrets and cpu-d3 2vcpu-8gb stay"
    )
    # A kind the list does not name is still caught by the `e0…` shape of real ids.
    assert mask_ids("unknown kind mspcluster-e00abcdefgh") == "unknown kind mspcluster-…efgh"


def test_wrapper_failures_name_the_command_and_can_withhold_stderr():
    runner = FakeRunner({"mysterybox secret create": RuntimeError("payload was: tf-secret-123")})
    nebius = Nebius("project-test", runner=runner, read_only=False)
    with pytest.raises(NebiusError) as plain:
        nebius.run("mysterybox", "secret", "create", "--name", "x")
    assert "tf-secret-123" in str(plain.value)
    with pytest.raises(NebiusError) as redacted:
        nebius.run("mysterybox", "secret", "create", "--name", "x", redact=True)
    assert "tf-secret-123" not in str(redacted.value)
    assert "mysterybox secret create" in str(redacted.value)


def test_wrapper_timeouts_become_errors():
    def slow(argv):
        raise subprocess.TimeoutExpired(argv, 1)

    with pytest.raises(NebiusError, match="timed out"):
        Nebius("project-test", runner=slow).run("ai", "endpoint", "list")


def test_from_settings_needs_the_cli_and_a_project_id(monkeypatch):
    runner = FakeRunner(ANSWERS)
    monkeypatch.setattr("waive.cloud.nebius.shutil.which", lambda name: None)
    with pytest.raises(NebiusMissing, match="U0.5"):
        Nebius.from_settings(settings(), runner=runner)
    monkeypatch.setattr("waive.cloud.nebius.shutil.which", lambda name: "/usr/local/bin/nebius")
    assert Nebius.from_settings(settings(), runner=runner).project_id == "project-test"
    assert runner.calls == []  # a proper id in .env needs no CLI call
    with pytest.raises(NebiusMissing, match="U0.3"):
        Nebius.from_settings(settings(nebius_project_id=None), runner=runner)


def test_from_settings_falls_back_to_the_profile_when_env_holds_a_non_project_id(monkeypatch):
    """Recorded 2026-10-09: .env held a `tenantuseraccount-…` id, which every parented call
    rejects; the CLI profile created at U0.5 carries the real project id."""
    monkeypatch.setattr("waive.cloud.nebius.shutil.which", lambda name: "/usr/local/bin/nebius")
    runner = FakeRunner(ANSWERS)
    nebius = Nebius.from_settings(settings(nebius_project_id="tenantuseraccount-e00x"), runner)
    assert nebius.project_id == "project-test"
    assert nebius.project_id_source == "the nebius CLI profile (`nebius config get parent-id`)"
    assert "tenantuseraccount" in nebius.warning and "tenantuseraccount-e00x" not in nebius.warning
    assert runner.calls == [["nebius", "config", "get", "parent-id", *SAFE_FLAGS]]

    no_profile = FakeRunner({"config get parent-id": RuntimeError("not set")})
    with pytest.raises(NebiusMissing, match="U0.3"):
        Nebius.from_settings(settings(nebius_project_id="tenantuseraccount-e00x"), no_profile)


def test_discover_collects_the_inventory_from_recorded_cli_output():
    runner = FakeRunner(ANSWERS)
    found = discover(Nebius("project-test", runner=runner), FOOTPRINT)
    assert found.cli_version == "0.12.287"
    assert (found.project_name, found.region) == ("default-project-eu-west2", "eu-west2")
    assert found.project_id_masked == "project-…test"
    assert list(found.platforms) == ["gpu-b300-sxm", "cpu-d3"]
    assert found.platforms["cpu-d3"][:3] == ["2vcpu-8gb", "4vcpu-16gb", "8vcpu-32gb"]
    assert found.preset_resources["cpu-d3"]["2vcpu-8gb"] == (2, 8)
    assert smallest_cpu_preset(found.platforms, "cpu-d3") == "2vcpu-8gb"
    assert smallest_cpu_preset(found.platforms, "cpu-e2") is None
    assert smallest_cpu_preset(found.platforms, "gpu-b300-sxm") is None  # no CPU-only preset
    [(network_name, network_id)] = found.networks
    assert network_name == "default-network" and network_id.startswith("vpcnetwork-")
    assert [name for name, _ in found.subnets] == ["default-subnet-y2ptnw0i"]
    assert found.existing == {
        "endpoint": [],
        "postgres": [],
        "secret": [],
        "registry": [],
        "instance": [],
    }
    assert found.estimates == {
        "cpu-d3/2vcpu-8gb": Decimal("0.066"),
        "cpu-d3/4vcpu-16gb": Decimal("0.132"),
        "network_ssd/32": Decimal("0.00311232"),
        "network_ssd/20": Decimal("0.0019452"),
    }
    assert "--config-template-disk-size-gibibytes" in found.postgres_create_help
    assert found.postgres_can_stop is True  # the CLI lists `cluster stop` (suspend)
    assert "minimum available preset" in found.endpoint_create_help
    assert found.raw["errors"] == {}
    assert found.raw["calls"][0] == ["nebius", "version", *SAFE_FLAGS]


def test_discover_never_runs_a_mutating_verb_and_always_disables_the_browser():
    runner = FakeRunner(ANSWERS)
    discover(Nebius("project-test", runner=runner), FOOTPRINT)
    assert len(runner.calls) >= 16
    for call in runner.calls:
        verbs = set(call) & MUTATING_VERBS
        assert not verbs or "--help" in call, call
        if "--help" not in call:
            assert "--no-browser" in call, call
        assert "--force" not in call
    assert {"create", "delete", "update", "start", "stop", "restart"} <= MUTATING_VERBS


def test_help_text_is_stripped_of_ansi_codes_before_it_is_read():
    """The real CLI (0.12.287) bolds command names even into a pipe; the first live run missed
    `cluster stop` because of that."""
    answers = dict(ANSWERS)
    answers["msp postgresql v1alpha1 cluster --help"] = (
        "\x1b[1m\x1b[0m  \x1b[1mCOMMANDS\x1b[0m\n      \x1b[1mstop\x1b[0m\n"
        "        Suspends the PostgreSQL cluster to save resources.\n"
    )
    runner = FakeRunner(answers)
    assert (
        Nebius("project-test", runner=runner)
        .text("msp", "postgresql", "v1alpha1", "cluster", "--help")
        .startswith("  COMMANDS\n      stop\n")
    )
    found = discover(Nebius("project-test", runner=runner), FOOTPRINT)
    assert "\x1b" not in found.postgres_help and found.postgres_can_stop is True


def test_discover_survives_failed_calls_and_marks_missing_prices():
    answers = dict(ANSWERS)
    answers[f"{VM}2vcpu-8gb"] = RuntimeError(fixture("calculator_vm_cpu_e2_2vcpu_8gb.err.txt"))
    answers["mysterybox secret list"] = RuntimeError("PermissionDenied")
    answers["msp postgresql v1alpha1 cluster --help"] = "  COMMANDS\n      list\n"
    runner = FakeRunner(answers)
    found = discover(Nebius("project-test", runner=runner), FOOTPRINT)
    assert found.estimates["cpu-d3/2vcpu-8gb"] is None
    assert found.existing["secret"] == []
    assert found.postgres_can_stop is False
    assert set(found.raw["errors"]) == {"estimate:cpu-d3/2vcpu-8gb", "secrets"}
    assert "platform 'cpu-e2' not found" in found.raw["errors"]["estimate:cpu-d3/2vcpu-8gb"]


def test_report_prices_both_windows_and_keeps_ids_out(tmp_path):
    runner = FakeRunner(ANSWERS)
    found = discover(Nebius("project-test", runner=runner), FOOTPRINT)
    report, raw = tmp_path / "cloud-costs.md", tmp_path / "raw.json"
    text = write_discovery(found, FOOTPRINT, Scenario(), report, raw)
    assert text == report.read_text(encoding="utf-8")

    assert "default-project-eu-west2" in text and "eu-west2" in text and "project-…test" in text
    assert "project-test" not in text and "vpcnetwork-" not in text and "vpcsubnet-" not in text
    assert "project-test" in raw.read_text(encoding="utf-8")

    header = "| Resource | Chosen preset | $/hour | $/day | 2-week window, always on |"
    assert header in text
    assert "| 2-week window, stopped between demos |" in text and "| Verified |" in text
    # Endpoint: 0.066/h → 1.58/day → 22.18 for 336 h → 1.85 for 28 demo hours (calculator).
    assert "`waive-web`" in text and "0.0660" in text and "1.58" in text
    assert "22.18" in text and "1.85" in text and "calculator" in text
    # The endpoint's container disk (CLI default 250Gi; 6.6 passes --disk-size 32Gi) bills only
    # while the endpoint runs: 32 GiB → 0.0031/h → 1.05 always on, 0.09 for 28 demo hours.
    disk_row = next(line for line in text.splitlines() if "`waive-web`, container disk" in line)
    assert "32 GiB" in disk_row and "| 0.0031 |" in disk_row and "| 1.05 |" in disk_row
    assert "| 0.09 (" in disk_row and "type unverified" in disk_row
    assert "default is 250Gi" in text and "--disk-size" in text and "$0.0243/h" in text
    # PostgreSQL compute is docs-only: 0.14/h → 3.36/day → 47.04 for 336 h; disk 32 GiB 0.0031/h.
    assert "`waive-db`" in text and "0.1400" in text and "3.36" in text and "47.04" in text
    assert "docs only" in text and "unverified" in text.lower()
    assert "0.0031" in text and "32 GiB" in text
    # Totals are rounded once from the unrounded sum (0.21222464/h), not summed from cells.
    assert "| **0.2122** | **5.09** | **71.31** | **50.02** |" in text
    assert "≈ $5.94" in text  # database deleted between windows: 1.85 + 0.09 + 4.01 + free
    # Free services, the budget line and the running schedule.
    assert "Container Registry" in text and "SecretStash" in text and "141 hours" in text
    assert "28 demo hours" in text and "14 days" in text
    # Only the Compute page carries the 1 October 2026 change; the other four pages have no
    # effective date, so they are stamped with the read date only.
    sources = text.split("## Price sources")[1].split("## Unverified")[0]
    assert "compute/resources/pricing.md (read 2026-10-09; prices effective 2026-10-01)" in sources
    assert sources.count("prices effective 2026-10-01") == 1
    assert sources.count("no effective date on the page") == 4
    assert "Compute list prices effective 2026-10-01" in text
    # The fallback host: cpu-d3 VM 0.066/h + 20 GiB boot disk; stopped VM pays the disk only.
    assert "`waive-vm`" in text and "20 GiB" in text and "0.0019" in text
    # The plan assumed no stop for PostgreSQL; the CLI has one and its billing is unverified.
    assert "cluster stop" in text and "suspend" in text.lower()
    assert "docs.nebius.com/postgresql/resources/pricing" in text
    assert "## Gate U6.1" in text
    assert "## CLI calls that failed" not in text


def test_report_shows_existing_counts_only_and_lists_failures(tmp_path):
    answers = dict(ANSWERS)
    answers["mysterybox secret list"] = {
        "items": [
            {"metadata": {"name": "waive-secrets", "id": "mbsec-e00xyz"}},
            {"metadata": {"name": "other", "id": "mbsec-e00other"}},
        ]
    }
    answers["registry list"] = RuntimeError("PermissionDenied: unknown subject")
    # The recorded shape of a calculator failure: the CLI's stderr carries the full project id.
    answers[f"{VM}2vcpu-8gb"] = RuntimeError(
        "Error: rpc error: code = NotFound desc = get preset spec: platform 'cpu-d3' not found "
        "in container 'project-test' (parent vpcnetwork-e00abcdef12345678)"
    )
    runner = FakeRunner(answers)
    found = discover(Nebius("project-test", runner=runner), FOOTPRINT)
    assert found.existing["secret"] == ["waive-secrets"]
    text = write_discovery(found, FOOTPRINT, Scenario(), tmp_path / "r.md", tmp_path / "raw.json")
    existing = text.split("## Existing `waive-` resources")[1].split("## Gate")[0]
    assert "- secret: 1" in existing and "- registry: unknown (listing failed)" in existing
    assert "waive-secrets" not in existing and "other" not in existing  # counts only, no names
    assert "mbsec-e00xyz" not in text  # and never an id
    assert "## CLI calls that failed" in text and "`registries`" in text
    assert "PermissionDenied" in text
    failures = text.split("## CLI calls that failed")[1]
    assert "`estimate:cpu-d3/2vcpu-8gb`" in failures and "platform 'cpu-d3' not found" in failures
    assert "project-test" not in text and "project-…test" in failures
    assert "vpcnetwork-e00abcdef12345678" not in text and "vpcnetwork-…5678" in failures
    assert "project-test" in (tmp_path / "raw.json").read_text(encoding="utf-8")  # gitignored


def test_gate_message_has_the_numbers_the_user_must_approve():
    runner = FakeRunner(ANSWERS)
    found = discover(Nebius("project-test", runner=runner), FOOTPRINT)
    message = render_gate_message(found, FOOTPRINT, Scenario())
    assert message.startswith("**Gate U6.1 — approve Nebius resources and costs.**")
    assert "*default-project-eu-west2*" in message and "*eu-west2*" in message
    assert "`waive-*`" in message and "$0.066/h" in message and "$0.143/h" in message
    assert "$3.43/day" in message and "$0.21/h" in message and "141 hours" in message
    assert "cpu-d3 2vcpu-8gb" in message and "(1)" in message and "(3)" in message
    # The endpoint's 32 GiB container disk (≈ $0.003/h) is part of what the user approves.
    assert "32 GiB container disk" in message and "$0.003/h" in message
    # The fallback sentence quotes the fallback total (compute + 20 GiB boot disk), not compute.
    assert "$0.068/h running" in message and "$0.05/day stopped" in message
    assert "docs/reports/cloud-costs.md" in message
    assert "project-test" not in message


def test_cli_discover_writes_the_report_and_prints_the_gate(monkeypatch, tmp_path):
    runner = FakeRunner(ANSWERS)
    monkeypatch.setattr("waive.cloud.nebius.shutil.which", lambda name: "/opt/nebius")
    monkeypatch.setattr(cloud_cli, "RUNNER", runner)
    monkeypatch.setattr(cloud_cli, "load_settings", settings)
    out, raw = tmp_path / "reports" / "cloud-costs.md", tmp_path / "var" / "raw.json"
    result = CliRunner().invoke(
        app, ["cloud", "discover", "--out", str(out), "--raw", str(raw), "--budget", "30"]
    )
    assert result.exit_code == 0, result.output
    assert out.exists() and raw.exists()
    assert "Nothing was created" in result.output and "Gate U6.1" in result.output
    assert "141 hours" in result.output and "32 GiB container disk" in result.output
    assert "project-test" not in result.output
    for call in runner.calls:
        assert not (set(call) & MUTATING_VERBS) or "--help" in call, call


def test_cli_discover_warns_when_env_holds_a_non_project_id(monkeypatch, tmp_path):
    runner = FakeRunner(ANSWERS)
    monkeypatch.setattr("waive.cloud.nebius.shutil.which", lambda name: "/opt/nebius")
    monkeypatch.setattr(cloud_cli, "RUNNER", runner)
    monkeypatch.setattr(
        cloud_cli, "load_settings", lambda: settings(nebius_project_id="tenantuseraccount-e00x")
    )
    out, raw = tmp_path / "cloud-costs.md", tmp_path / "raw.json"
    result = CliRunner().invoke(app, ["cloud", "discover", "--out", str(out), "--raw", str(raw)])
    assert result.exit_code == 0, result.output
    assert "tenantuseraccount" in result.output and "nebius config get parent-id" in result.output
    text = out.read_text(encoding="utf-8")
    assert "tenantuseraccount" in text and "tenantuseraccount-e00x" not in text


def test_cli_discover_explains_gate_u05_when_the_cli_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("waive.cloud.nebius.shutil.which", lambda name: None)
    monkeypatch.setattr(cloud_cli, "load_settings", settings)
    result = CliRunner().invoke(app, ["cloud", "discover", "--out", str(tmp_path / "r.md")])
    assert result.exit_code == 2
    assert "U0.5" in result.output and "install.sh" in result.output
    assert not (tmp_path / "r.md").exists()
