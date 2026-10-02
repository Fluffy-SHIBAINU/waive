import os
from decimal import Decimal
from pathlib import Path

from pydantic import SecretStr

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
