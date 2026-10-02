import subprocess

from pydantic import SecretStr
from typer.testing import CliRunner

from waive.cli import app
from waive.config import Settings
from waive.doctor import run_checks

MODELS = [
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    "nvidia/nemotron-3-super-120b-a12b",
    "openbmb/MiniCPM-V-4_5",
]


class FakeAI:
    def __init__(self, models):
        self.models = models

    def list_models(self):
        return self.models


class FakeTavily:
    def search(self, query, **kwargs):
        return ["hit"]


def settings(**overrides):
    values = {
        "nebius_api_key": SecretStr("tf"),
        "tavily_api_key": SecretStr("tv"),
        "nebius_project_id": "project-test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def no_cli(_name):
    return None


def by_name(checks):
    return {check.name: check for check in checks}


def test_offline_checks_report_keys_without_values():
    checks = by_name(
        run_checks(settings(), live=False, ai_factory=None, tavily_factory=None, which=no_cli)
    )
    assert checks["NEBIUS_API_KEY"].status == "ok"
    assert checks["TAVILY_API_KEY"].status == "ok"
    assert checks["Zero data retention"].status == "warn"
    assert checks["Nebius CLI"].status == "warn"
    assert all("tf" != check.detail and "tv" != check.detail for check in checks.values())


def test_missing_key_fails():
    checks = by_name(
        run_checks(
            settings(nebius_api_key=None),
            live=False,
            ai_factory=None,
            tavily_factory=None,
            which=no_cli,
        )
    )
    assert checks["NEBIUS_API_KEY"].status == "fail"


def test_live_checks_confirm_models_and_tavily():
    checks = by_name(
        run_checks(
            settings(),
            live=True,
            ai_factory=lambda: FakeAI(MODELS),
            tavily_factory=lambda: FakeTavily(),
            which=no_cli,
        )
    )
    assert checks["Token Factory models"].status == "ok"
    assert checks["Tavily search"].status == "ok"


def test_live_check_lists_missing_models():
    checks = by_name(
        run_checks(
            settings(),
            live=True,
            ai_factory=lambda: FakeAI(["nvidia/Some-Other-Nemotron"]),
            tavily_factory=lambda: FakeTavily(),
            which=no_cli,
        )
    )
    model_check = checks["Token Factory models"]
    assert model_check.status == "fail"
    assert "nvidia/Some-Other-Nemotron" in model_check.detail


def test_nebius_cli_with_profile_is_ok():
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="default\n", stderr="")

    checks = by_name(
        run_checks(
            settings(),
            live=False,
            ai_factory=None,
            tavily_factory=None,
            which=lambda name: "/usr/local/bin/nebius",
            run=run,
        )
    )
    assert checks["Nebius CLI"].status == "ok"


def test_cli_exit_code_reflects_failures(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ["NEBIUS_API_KEY", "TAVILY_API_KEY", "NEBIUS_PROJECT_ID"]:
        monkeypatch.delenv(name, raising=False)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "NEBIUS_API_KEY" in result.output
