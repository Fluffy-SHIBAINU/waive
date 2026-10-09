"""Typed settings loaded from the environment and `.env` (spec section 11)."""

from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
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
    # Admin console sign-in (at least 16 characters); the console is off when unset.
    admin_token: SecretStr | None = None
    # Largest request body the web app accepts (phone photos are 2-6 MB); bigger gets a 413 page.
    max_upload_bytes: int = 10 * 1024 * 1024

    # Deployment (spec §14). `production` is set on the endpoint, never in a developer's .env:
    # it refuses the SQLite default so a misconfigured container cannot silently start empty.
    env: Literal["development", "production"] = "development"
    # `db` keeps the spend ledger in the `usage_events` table (stateless containers); `file`
    # keeps `var/usage.jsonl` for local work.
    ledger_backend: Literal["file", "db"] = "file"
    # The production database URL as the container sees it (filled by task 6.5); used locally
    # by `waive cloud secrets push` and `waive db copy --to-cloud`.
    cloud_database_url: SecretStr | None = None
    # Image registry path, e.g. cr.eu-north1.nebius.cloud/<registry path> (task 6.4).
    registry: str | None = None
    cloud_platform: str = "cpu-d3"
    cloud_preset: str = "2vcpu-8gb"
    cloud_pg_preset: str = "2vcpu-8gb"
    cloud_pg_disk_gib: int = 32

    # Phase 7: unattended scouting (spec §8 step 8). Off by default; a long-running `waive serve`
    # or the production container turns it on. Each tick scouts at most one hospital and stops
    # once WAIVE_SCOUT_DAILY_CREDITS Tavily credits were spent in the current UTC day.
    scheduler: Literal["on", "off"] = "off"
    scheduler_interval_minutes: int = 30
    # Comma-separated states the scheduler may touch; empty means every seeded state (gate U7.1).
    scheduler_states: str = "MA"
    scout_daily_credits: int = 50

    @model_validator(mode="after")
    def _production_needs_postgres(self) -> Self:
        if self.env == "production" and self.database_url.startswith("sqlite"):
            raise ValueError(
                "WAIVE_ENV=production needs WAIVE_DATABASE_URL pointing at PostgreSQL; "
                "the SQLite default is for development only"
            )
        return self
