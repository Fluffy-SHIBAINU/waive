import os
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from waive.config import Settings

ENV_VARS = ["NEBIUS_API_KEY", "TAVILY_API_KEY", "NEBIUS_PROJECT_ID"]


def clear_env(monkeypatch):
    for name in ENV_VARS + [key for key in os.environ if key.startswith("WAIVE_")]:
        monkeypatch.delenv(name, raising=False)


def test_defaults(monkeypatch):
    clear_env(monkeypatch)
    settings = Settings(_env_file=None)
    assert settings.nebius_api_key is None
    assert settings.tavily_api_key is None
    assert settings.require_zdr is True
    assert settings.zdr_confirmed is False
    assert settings.tavily_credit_cap == 1000
    assert settings.token_factory_usd_cap == Decimal("15")
    assert settings.token_factory_base_url == "https://api.tokenfactory.nebius.com/v1/"
    assert settings.ledger_path == Path("var/usage.jsonl")


def test_reads_environment_and_hides_secrets(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("NEBIUS_API_KEY", "tf-secret-123")
    monkeypatch.setenv("WAIVE_ZDR_CONFIRMED", "true")
    monkeypatch.setenv("WAIVE_TAVILY_CREDIT_CAP", "250")
    settings = Settings(_env_file=None)
    assert settings.nebius_api_key.get_secret_value() == "tf-secret-123"
    assert settings.zdr_confirmed is True
    assert settings.tavily_credit_cap == 250
    assert "tf-secret-123" not in repr(settings)


def test_accepts_field_names_in_code(monkeypatch):
    clear_env(monkeypatch)
    settings = Settings(_env_file=None, nebius_api_key=SecretStr("k"), zdr_confirmed=True)
    assert settings.nebius_api_key.get_secret_value() == "k"
    assert settings.zdr_confirmed is True


def test_production_rejects_the_sqlite_default(monkeypatch):
    clear_env(monkeypatch)
    with pytest.raises(ValidationError, match="WAIVE_DATABASE_URL"):
        Settings(_env_file=None, env="production")
    settings = Settings(
        _env_file=None,
        env="production",
        database_url="postgresql+psycopg://waive:pw@db.example.net:5432/waive",
        ledger_backend="db",
    )
    assert settings.env == "production"


def test_production_rejects_the_file_ledger(monkeypatch):
    """A container's file ledger vanishes on every cold start, and with it the spend history the
    caps are counted against; production must keep the ledger in the database."""
    clear_env(monkeypatch)
    with pytest.raises(ValidationError, match="WAIVE_LEDGER_BACKEND"):
        Settings(
            _env_file=None,
            env="production",
            database_url="postgresql+psycopg://waive:pw@db.example.net:5432/waive",
        )


def test_development_defaults_keep_sqlite_and_the_file_ledger(monkeypatch):
    clear_env(monkeypatch)
    settings = Settings(_env_file=None)
    assert settings.env == "development"
    assert settings.ledger_backend == "file"
    assert settings.database_url == "sqlite:///var/waive.db"
    assert settings.cloud_database_url is None and settings.registry is None
    assert (settings.cloud_platform, settings.cloud_preset) == ("cpu-d3", "2vcpu-8gb")
    assert (settings.cloud_pg_preset, settings.cloud_pg_disk_gib) == ("2vcpu-8gb", 32)


def test_scheduler_settings_default_off(monkeypatch):
    clear_env(monkeypatch)
    settings = Settings(_env_file=None)
    assert settings.scheduler == "off"
    assert (settings.scheduler_interval_minutes, settings.scheduler_states) == (30, "MA")
    assert settings.scout_daily_credits == 50
    on = Settings(_env_file=None, scheduler="on", scheduler_states="MA,RI", scout_daily_credits=20)
    assert (on.scheduler, on.scout_daily_credits) == ("on", 20)
