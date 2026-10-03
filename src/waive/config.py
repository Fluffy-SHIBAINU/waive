"""Typed settings loaded from the environment and `.env` (spec section 11)."""

from decimal import Decimal
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="WAIVE_",
        extra="ignore",
        populate_by_name=True,
    )

    nebius_api_key: SecretStr | None = Field(default=None, validation_alias="NEBIUS_API_KEY")
    tavily_api_key: SecretStr | None = Field(default=None, validation_alias="TAVILY_API_KEY")
    nebius_project_id: str | None = Field(default=None, validation_alias="NEBIUS_PROJECT_ID")

    token_factory_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    model_reason: str = "nvidia/nemotron-3-super-120b-a12b"
    model_fast: str = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
    # Third opinion when the reasoning and fast models disagree on a critical field.
    model_tiebreak: str = "nvidia/Nemotron-3_5-Lightning"
    model_vision: str = "openbmb/MiniCPM-V-4_5"

    require_zdr: bool = True
    zdr_confirmed: bool = False

    tavily_credit_cap: int = 1000
    token_factory_usd_cap: Decimal = Decimal("15")
    ledger_path: Path = Path("var/usage.jsonl")
    database_url: str = "sqlite:///var/waive.db"
    vault_key: SecretStr | None = None
    token_secret: SecretStr | None = None
