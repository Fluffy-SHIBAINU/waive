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
    # Optional spend ceiling per UTC day for Token Factory. The lifetime cap above never resets,
    # so without this one burst of anonymous uploads would switch photo reading off for good.
    token_factory_daily_usd_cap: Decimal | None = None
    ledger_path: Path = Path("var/usage.jsonl")
    database_url: str = "sqlite:///var/waive.db"
    vault_key: SecretStr | None = None
    token_secret: SecretStr | None = None
    # Admin console sign-in (at least 16 characters); the console is off when unset.
    admin_token: SecretStr | None = None
    # Largest request body the web app accepts (phone photos are 2-6 MB); bigger gets a 413 page.
    max_upload_bytes: int = 10 * 1024 * 1024
    # Spec §11 "requests are rate-limited": fixed one-minute windows for case creation (which
    # needs no sign-in and mints two capability links), one process-wide and one per client; and
    # wrong admin tokens per 15 minutes before sign-in locks for everyone.
    cases_per_minute: int = 60
    cases_per_client_per_minute: int = 10
    admin_login_failures: int = 10

    # Sign-in with Google (Phase 9). Optional: while either value is empty, sign-in is hidden and
    # the app works as before. The values come from an OAuth client of type "Web application"
    # in the Google Cloud console, whose redirect URI is <this site>/auth/google/callback.
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    # Sign-in starts and returns per minute: process-wide, and per client (best effort).
    sign_ins_per_minute: int = 60
    sign_ins_per_client_per_minute: int = 10

    # Deployment (spec §14). `production` is set on the endpoint, never in a developer's .env:
    # it refuses the SQLite default so a misconfigured container cannot silently start empty.
    env: Literal["development", "production"] = "development"

    # The Kindred ID wall (web/kindred_auth.py): every request needs the family's session cookie
    # except paths under these prefixes, comma-separated. The senior and caregiver links (`/s/`,
    # `/c/`) are walled too while Waive is a private beta; add them here when it opens.
    wall_exempt_prefixes: str = "/healthz,/static/,/robots.txt,/favicon.ico"
    # Local development only: this address stands in for a session, and only for a request
    # that arrived at localhost. The verifier ignores it whenever `env` is production.
    kindred_auth_bypass: str | None = Field(default=None, validation_alias="KINDRED_AUTH_BYPASS")
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
    # The endpoint's container disk, passed as `ai endpoint create --disk-size <n>Gi` (task 6.6).
    # The CLI default is 250Gi, which bills ≈ $0.024/h (+37% on the endpoint) while it runs.
    cloud_endpoint_disk_gib: int = 32

    # Phase 7: unattended scouting (spec §8 step 8). Off by default; a long-running `waive serve`
    # or the production container turns it on. Each tick scouts at most one hospital and stops
    # once WAIVE_SCOUT_DAILY_CREDITS Tavily credits were spent in the current UTC day.
    scheduler: Literal["on", "off"] = "off"
    scheduler_interval_minutes: int = 30
    # Comma-separated states the scheduler may touch; empty means every seeded state (gate U7.1).
    scheduler_states: str = "MA"
    scout_daily_credits: int = 50
    # Tavily Extract depth for the first pass over every hospital page (task 7.10). "advanced"
    # renders pages more fully at two credits per five URLs instead of one; about nine
    # credits per hospital instead of seven.
    scout_extract_depth: Literal["basic", "advanced"] = "basic"

    # Phase 10: the hospital ledger. Downloads of public files land under `ledger_dir`, never in
    # the repository. The scout queue multiplies a hospital's priority by
    # (1 + weight × its senior-exposure rank) once `waive ledger exposure` has run; the weight
    # defaults to 0 so the queue is unchanged until the founder turns it on.
    ledger_dir: Path = Path("var/ledger")
    scout_exposure_weight: float = 0.0

    @property
    def wall_exempt(self) -> tuple[str, ...]:
        """The path prefixes the Kindred ID wall lets through without a session."""
        return tuple(p.strip() for p in self.wall_exempt_prefixes.split(",") if p.strip())

    @property
    def wall_env(self) -> dict[str, str]:
        """What the verifier's bypass reads: Waive's own environment name and the bypass address,
        from these settings rather than the process environment, so `WAIVE_ENV=production` in a
        `.env` file counts the same as in the container."""
        return {"WAIVE_ENV": self.env, "KINDRED_AUTH_BYPASS": self.kindred_auth_bypass or ""}

    @property
    def sign_in_configured(self) -> bool:
        """Whether sign-in with Google is on: both the client id and its secret are set."""
        secret = self.google_client_secret
        return bool(self.google_client_id and secret and secret.get_secret_value())

    @model_validator(mode="after")
    def _production_needs_postgres(self) -> Self:
        if self.env == "production":
            if self.database_url.startswith("sqlite"):
                raise ValueError(
                    "WAIVE_ENV=production needs WAIVE_DATABASE_URL pointing at PostgreSQL; "
                    "the SQLite default is for development only"
                )
            if self.ledger_backend != "db":
                # A container's file ledger is lost on every cold start, and with it the spend
                # history the caps are counted against.
                raise ValueError(
                    "WAIVE_ENV=production needs WAIVE_LEDGER_BACKEND=db so the spend ledger "
                    "survives container restarts"
                )
        return self
