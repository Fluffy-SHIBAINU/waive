# Waive Phase 6 — Deploy on Nebius AI Cloud Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Waive web app on a public HTTPS URL on Nebius AI Cloud — a container image in Container Registry, PostgreSQL in Managed Service for PostgreSQL, secrets in SecretStash (the CLI still calls it `mysterybox`), the app on a CPU Serverless AI endpoint — with `waive cloud start|stop|status|cleanup` to run it only during build and demo windows, and every paid step behind gate U6.1.

**Architecture:** The image is a two-stage build (uv resolves `uv.lock` into a venv; a slim Python 3.12 runtime runs `uvicorn waive.web.app:create_app --factory` as a non-root user on port 8000). Production is selected by environment only: `WAIVE_ENV=production` refuses the SQLite default, `WAIVE_LEDGER_BACKEND=db` moves the spend ledger from `var/usage.jsonl` into a `usage_events` table so the stateless container keeps its budget history, and the six secrets arrive as `--env-secret` variables from one SecretStash secret. A new `waive.cloud` package wraps the `nebius` CLI through `subprocess` with argument lists (no shell): `discover` is read-only and writes the cost table, `secrets push` and `deploy` compose the exact create commands from `.env`, `start|stop|status|cleanup` operate only on resources named `waive-*`. The local SQLite atlas is copied into PostgreSQL with `waive db copy` (cases excluded). Fallback if the endpoint cannot serve a plain web app: one `cpu-d3` VM with Docker Compose (app + PostgreSQL + Caddy).

**Tech Stack:** Docker (multi-stage, `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` → `python:3.12-slim-bookworm`), psycopg 3, SQLAlchemy 2, pydantic-settings, Typer, the `nebius` CLI (`ai endpoint`, `registry`, `msp postgresql v1alpha1`, `mysterybox`, `vpc`, `compute`), curl, jq, pytest (fakes for every CLI call; no network in unit tests).

**Spec:** `docs/superpowers/specs/2026-10-02-waive-design.md` (§11 privacy, §14 deployment, §15 costs, §18 open items). Master plan Phase 6 section: `docs/superpowers/plans/2026-10-02-waive-master-plan.md`. Gates and the project id: `docs/PROGRESS.md` (U0.3, U0.4, U0.5, U6.1).

## Global Constraints

- No cloud resource is created, changed or deleted before gate **U6.1** is closed in chat. Tasks 6.1–6.3 create nothing in the cloud; 6.3 runs only `nebius ... version|list|get|--help`. Task 6.3 ends with STOP for U6.1. Tasks 6.4–6.8 need U0.5 (CLI installed, `nebius profile create` done) and U6.1.
- Every state-changing `nebius` command in this plan is written as: the gate it needs → the command → a read-only verification command. Resource names all start with `waive-` (`waive-registry`, `waive-db`, `waive-secrets`, `waive-web`, fallback `waive-vm`); `waive cloud cleanup` refuses any other name.
- `NEBIUS_PROJECT_ID` lives only in `.env` (gitignored). It never appears in a committed file: the cost report prints the project *name* and region; resource ids go to `var/cloud-discovery.json` (gitignored). Tests assert the report contains no ids.
- Secret values (`NEBIUS_API_KEY`, `TAVILY_API_KEY`, `WAIVE_VAULT_KEY`, `WAIVE_TOKEN_SECRET`, `WAIVE_ADMIN_TOKEN`, the PostgreSQL password / `WAIVE_CLOUD_DATABASE_URL`) are never printed, logged or committed. `waive cloud secrets push` prints key names only; `Nebius.run(..., redact=True)` withholds CLI stderr for commands that carry secrets; uvicorn runs with `--no-access-log` because capability tokens are in URLs.
- Budget (master plan): AI Cloud ≤ **$30 through 2026-10-30**. Published list prices read 2026-10-02 (effective 2026-10-01): endpoint `cpu-d3 2vcpu-8gb` **$0.066/h**; Managed PostgreSQL `2vcpu-8gb` + 32 GiB **≈ $0.143/h**; both ≈ **$0.209/h → about 143 hours** of running within $30. A stopped endpoint is not billed; a PostgreSQL cluster bills until deleted (no stop command is documented). Container Registry and SecretStash are free. Run the endpoint only during build and demo windows; the U6.1 message states this.
- `WAIVE_REQUIRE_ZDR=true` and `WAIVE_ZDR_CONFIRMED=false` in production until gate **U0.4** closes: on the public URL a bill photo reaches the ZDR check and the senior sees "your helper needs to finish setting things up". Do not relax this on a public URL (strangers could upload real bills); the full senior flow stays a local test (U4.1) until U0.4.
- Development keeps SQLite (`sqlite:///var/waive.db`) and the file ledger (`var/usage.jsonl`); nothing in this phase changes local behaviour. `cases` rows are never copied between databases unless `--include-cases` is passed explicitly.
- Deviations from the master plan's Phase 6 sketch, by instruction of the orchestrator: no Object Storage bucket (documents stay as text in `source_docs`; a bucket is a Phase 7 item); the in-app scheduler is Phase 7.1; WeasyPrint system libraries are unnecessary (Phase 4 chose reportlab). The master plan's task numbers are kept as listed at the top of each task here.
- Python `>=3.12,<3.13`, uv, ruff; every task ends with `uv run ruff format . && uv run ruff check . && uv run pytest` green. **248 tests pass at the start of the phase**; each task states its expected count.
- Interfaces relied on from earlier phases (verified against the code on 2026-10-02): `config.Settings` (fields `nebius_api_key`, `tavily_api_key`, `nebius_project_id`, `ledger_path`, `database_url`, `vault_key`, `token_secret`, `admin_token`, `tavily_credit_cap`, `token_factory_usd_cap`, `require_zdr`, `zdr_confirmed`, `model_*`); `governor.Ledger(path).record(UsageEvent) / totals(provider) -> (Decimal, Decimal)`, `Governor(ledger, tavily_credit_cap, token_factory_usd_cap)`, `make_governor(settings)`, `UsageEvent(provider, units, usd, purpose, ts)`; `db.Base / make_engine / init_db / upgrade_schema / session_scope / HospitalRow / SheetRow / SourceDocRow / CaseRow / hospital_sources`; `web.app.create_app(settings, *, engine, ai, cipher, signer, today_fn)` with `app.state.settings` and `app.state.deps`; `web.routes_admin.admin_home` (reads the ledger for the budget card); `cli.app`, `cli._engine(settings)`, `cli.db_app`; `atlas.repo.upsert_hospital / save_source`, `atlas.publish.publish_sheet`, `atlas.samples.st_example_sheet / SAMPLE_POLICY_TEXT`; `doctor._nebius_cli_check` (already reports "Nebius CLI: profile configured" for U0.5); the web routes `GET /healthz`, `GET /`, `GET /atlas`, `GET /atlas/{ccn}`, `GET /atlas/{ccn}.json`, `POST /cases` (form field `state`).

## Verified Nebius facts (read 2026-10-02 with WebFetch; nothing here was tested against a live project — the CLI is not installed on the build machine)

| Topic | Verified | Source |
|---|---|---|
| CLI install and login | `curl -sSL https://artifacts.nebius.cloud/cli/install.sh \| bash`; `nebius version`; `nebius profile create` opens a browser sign-in, lets you pick tenant and project, and sets that profile as default; `nebius profile list`; non-interactive form takes `--profile`, `--endpoint api.nebius.cloud`, `--federation-endpoint auth.nebius.com`, `--parent-id <project_ID>` | https://docs.nebius.com/cli/install.md, https://docs.nebius.com/cli/configure.md |
| Access tokens | `nebius iam get-access-token`, valid 12 hours | https://docs.nebius.com/iam/authorization/access-tokens.md |
| Regions | `eu-north1` (Finland), `eu-west1`/`eu-west2` (France), `eu-south1`, `me-west1`, `us-central1`, `us-north1`, `uk-south1`, `uk-south2`. Serverless AI and Managed PostgreSQL: eu-north1, eu-west1, eu-west2, me-west1, us-central1, uk-south1 (Serverless also uk-south2). Container Registry and SecretStash: all regions | https://docs.nebius.com/overview/regions.md |
| CPU platforms and presets | `cpu-d3` (AMD EPYC 9654, all regions): `2vcpu-8gb`, `4vcpu-16gb`, `8vcpu-32gb`, …; `cpu-e2` (Intel Xeon Gold 6338, eu-north1 only): `2vcpu-8gb`, `4vcpu-16gb`, …; list what a project offers with `nebius compute platform list --parent-id <project_ID>` | https://docs.nebius.com/compute/virtual-machines/types.md, https://docs.nebius.com/compute/virtual-machines/list-platforms.md |
| Compute prices (USD, from 2026-10-01; billed per second, priced per hour) | Non-GPU AMD EPYC Genoa: **$0.015 per vCPU-hour, $0.0045 per GiB-hour**; Non-GPU Intel Ice Lake (eu-north1): **$0.012 per vCPU-hour, $0.0045 per GiB-hour**; network SSD **$0.071 per GiB-month**; stopped VMs: compute not charged, disks still charged | https://docs.nebius.com/compute/resources/pricing.md |
| Serverless AI endpoints | CLI family `nebius ai endpoint`. `create --name --image <registry/path:tag> --platform <id> --preset <preset> --disk-size <n>Gi (default `250Gi`; verified 2026-10-09 in CLI 0.12.287 and the docs: a running endpoint is billed for "computing resources and storage" at Compute prices, so 6.6 passes `--disk-size` explicitly from `Settings.cloud_endpoint_disk_gib`) --container-port <port> [--env "K=V,K2=V2"] [--env-secret KEY=<secret selector>] [--subnet-id] [--public] [--auth token --token …] [--registry-username/--registry-password or --registry-secret] [--volume …]`. `--auth` omitted (default) = no authentication. `--public` only adds a public IP to the container VM and is "not required to reach the endpoint from the internet, because each HTTP port is available through the endpoint's managed HTTPS URL". `--subnet-id` is required only if the project has several subnets. Secret selector = secret name, id, version id, or `mbsec-…@mbsecver-…`; the payload key must equal the environment variable name. `list`, `get <id>`, `start --id`, `stop --id`, `delete --id`. URL: `nebius ai endpoint get <id> --format json \| jq -r '.status.public_endpoints[] \| select(startswith("https://")) \| head -1'`. Quickstart deploys `nginx:alpine` on `--platform cpu-d3 --preset 4vcpu-16gb`. "While an endpoint is stopped, you are not billed for computing resources or storage." Serverless AI has no prices of its own: Compute prices apply | https://docs.nebius.com/serverless/endpoints/manage.md, https://docs.nebius.com/serverless/quickstart/endpoints.md, https://docs.nebius.com/serverless/pricing-quotas.md, https://docs.nebius.com/serverless/overview.md |
| Container Registry | `nebius registry create --name <name> --format json`; registry host `cr.<region>.nebius.cloud`; image path `cr.$REGION_ID.nebius.cloud/$REGISTRY_PATH/<image>:<tag>` where the quickstart derives `REGISTRY_PATH` with `jq -r ".metadata.id" \| cut -d- -f 2`; `nebius registry configure-helper` installs a Docker credential helper (no `docker login`); alternative `nebius iam get-access-token \| docker login cr.<region>.nebius.cloud --username iam --password-stdin`; CI/CD uses a service-account static key from `nebius iam static-key issue` (up to 3 years). **Free of charge** | https://docs.nebius.com/container-registry/quickstart.md, https://docs.nebius.com/container-registry/authentication.md, https://docs.nebius.com/container-registry/resources/pricing.md |
| Managed PostgreSQL | `nebius msp postgresql v1alpha1 cluster create --name … --description … --network-id … --bootstrap-db-name … --bootstrap-user-name … --bootstrap-user-password … --config-version 16 --config-template-disk-type network-ssd --config-template-disk-size-gibibytes 96 --config-template-resources-platform cpu-e2 --config-template-resources-preset 2vcpu-8gb --config-template-hosts-count 1 --config-public-access`; `cluster list --page-size 10`; `cluster get-by-name --name <n> --format jsonpath='{.metadata.id}'`; `cluster update --id …`; `cluster delete --id …`. Password: ≥ 8 chars with upper, lower and special. Endpoint host: `--format jsonpath='{.status.connection_endpoints.public_read_write}'`, port 5432, `sslmode=verify-full`, CA `curl "https://storage.eu-north1.nebius.cloud/msp-certs/ca.pem" -o ~/.postgresql/root.crt`. Users and databases beyond the bootstrap ones are SQL (`CREATE USER … PASSWORD '…'`, `GRANT CONNECT …`). Prices: **$0.034 per vCPU-hour, $0.009 per GiB-hour** (docs example: `4vcpu-16gb` = $0.28/h), network SSD **$0.071 per GiB-month**, public access free | https://docs.nebius.com/postgresql/quickstart.md, https://docs.nebius.com/postgresql/clusters/manage.md, https://docs.nebius.com/postgresql/databases/connect.md, https://docs.nebius.com/postgresql/databases/users.md, https://docs.nebius.com/postgresql/resources/pricing.md |
| SecretStash (MysteryBox) | `nebius mysterybox secret create --name "<n>" --description "<d>" --secret-version-payload '[{"key":"K","string_value":"V"}, …]'` (several keys per version); `nebius mysterybox secret list`; `nebius mysterybox secret-version list --parent-id mbsec-…`; `nebius mysterybox payload get --secret-id mbsec-… [--version-id mbsecver-…]` (needs role `mysterybox.payload-viewer`). **Free in preview** | https://docs.nebius.com/mysterybox/secrets/create.md, https://docs.nebius.com/mysterybox/secrets/get.md, https://docs.nebius.com/mysterybox/resources/pricing.md |
| VPC ids | `nebius vpc network list`, `nebius vpc subnet list`, `… get-by-name --name <n>`; the id is `metadata.id` | https://docs.nebius.com/vpc/networking/resources.md |
| Compute VM (fallback) | `nebius compute instance create --name … --resources-platform … --resources-preset … --boot-disk-managed-disk-name … --boot-disk-managed-disk-type network_ssd --boot-disk-managed-disk-size-gibibytes 10 --boot-disk-managed-disk-source-image-family-image-family ubuntu24.04-driverless --boot-disk-attach-mode READ_WRITE --network-interfaces '[{"name":"eth0","ip_address":{},"public_ip_address":{},"subnet_id":"<subnet_ID>"}]'`; `instance start|stop|delete|get|list`; users are provisioned through cloud-init | https://docs.nebius.com/compute/virtual-machines/manage.md |
| Billing view | Console → Billing → Usage (filters: region, project, service, product). No CLI command is documented | https://docs.nebius.com/signup-billing/usage/view.md |

**Not verified (confirm on the day; each item names where the plan copes):**

1. Whether a Serverless AI endpoint accepts `--preset 2vcpu-8gb` (the quickstart used `4vcpu-16gb`). 6.6 tries `2vcpu-8gb` first; if rejected, `4vcpu-16gb` costs $0.132/h and 6.3's report is regenerated with `--preset 4vcpu-16gb` in `.env`.
2. Whether an endpoint pulls from the project's own Container Registry without credentials. 6.6 tries without; the fallback passes a static key through `--registry-username iam --registry-password …` (`waive cloud deploy --registry-key-env`).
3. JSON shapes of `nebius … list` (`{"items": [...]}` with `metadata.name`/`metadata.id`, `status.*`), the `compute platform list` preset field, and the project record's region field. `waive.cloud` parsers are tolerant and 6.3 saves the raw JSON to `var/cloud-discovery.json` so the implementer can adjust them after the first real run.
4. Whether `nebius ai endpoint` has an `update` command (none is documented); redeploying a new image is delete + `waive cloud deploy`, and the URL may change — 6.8 records the current one.
5. The smallest disk size Managed PostgreSQL accepts (the docs show 96 GiB; 6.5 tries 32 and uses the minimum the CLI names) and whether platforms other than `cpu-e2` are allowed for clusters in non-`eu-north1` projects (6.3 captures `cluster create --help`). Whether a cluster can be stopped (6.3 captures the help text; the plan assumes no).
6. Flag names for `nebius mysterybox secret delete`, `nebius registry delete` (the plan uses `--id`, as `ai endpoint delete` and `cluster delete` do), for adding a secret version, and for cloud-init user data on `compute instance create`. Run the command with `--help` first; `waive cloud cleanup` shows each command before asking.
7. Whether `nebius ai endpoint create` returns the resource or an operation record. `waive cloud deploy` prints whichever id it finds and points to `waive cloud status`.
8. The disk type the CLI uses for an endpoint's container disk (`--disk-size` has no type flag; the docs' API example uses `NETWORK_SSD`). 6.3 prices the disk at the network-ssd rate and 6.6 passes `--disk-size 32Gi` (`Settings.cloud_endpoint_disk_gib`) so the 250Gi default (≈ $0.0243/h) never applies; the endpoint record after 6.6 (`spec.disk.type`) settles it.

## Cost assumptions used in this plan (USD, list prices effective 2026-10-01, read 2026-10-02)

| Item | Formula | Hourly | Monthly (730 h) |
|---|---|---|---|
| Endpoint `cpu-d3 2vcpu-8gb` | 2 × 0.015 + 8 × 0.0045 | $0.0660 | $48.18 |
| Endpoint `cpu-e2 2vcpu-8gb` (eu-north1 only) | 2 × 0.012 + 8 × 0.0045 | $0.0600 | $43.80 |
| Endpoint `cpu-d3 4vcpu-16gb` (if 2vcpu is refused) | 4 × 0.015 + 16 × 0.0045 | $0.1320 | $96.36 |
| PostgreSQL `2vcpu-8gb` | 2 × 0.034 + 8 × 0.009 | $0.1400 | $102.20 |
| Endpoint container disk 32 GiB (`--disk-size 32Gi`; the CLI default `250Gi` would be $0.0243/h, +37%; billed only while the endpoint runs; disk type unverified, network-ssd rate assumed — 6.3 review) | 32 × 0.071 / 730 | $0.0031 | $2.27 |
| PostgreSQL disk 32 GiB network-ssd | 32 × 0.071 / 730 | $0.0031 | $2.27 |
| Container Registry, SecretStash, PostgreSQL public access, endpoint HTTPS URL | — | $0 | $0 |
| **Endpoint + PostgreSQL** | | **$0.2122** | **$154.92** |

$30 buys ≈ 141 hours with both running, or ≈ 209 hours of the database alone. The database is the cost driver and cannot be paused: between demo windows either keep it (≈ $3.44/day) or delete it with `waive cloud cleanup` and recreate it in ~10 minutes with 6.5's commands plus `waive db copy --to-cloud` (the atlas is reproducible from the local database; cases are not copied by design). The U6.1 message presents both options.

## Shell conventions for tasks 6.4–6.8

Run from the repo root in one terminal session after gates U0.5 and U6.1. `jq` is used for JSON (`brew install jq` if missing). Set once per session:

```bash
export PROJECT_ID="$(uv run python -c 'from waive.config import Settings; print(Settings().nebius_project_id or "")')"
[ -n "$PROJECT_ID" ] || echo "NEBIUS_PROJECT_ID is missing in .env (gate U0.3)"
export REGION_ID="eu-north1"        # replace with the region printed in docs/reports/cloud-costs.md
nebius profile list                 # must list the default profile (gate U0.5)
```

`$PROJECT_ID` is passed explicitly as `--parent-id` so a wrong default profile cannot create resources elsewhere. Never `echo` a password variable; never paste one into chat.

---

### Task 6.1: Dockerfile, `.dockerignore`, production settings

**Files:**
- Create: `Dockerfile`, `.dockerignore`
- Modify: `pyproject.toml` (add `psycopg[binary]>=3.2`), `src/waive/config.py`, `.env.example`
- Test: `tests/unit/test_dockerfile.py`, `tests/unit/test_config.py` (two tests added)

**Interfaces:**
- Produces: `Settings.env: Literal["development", "production"]` (default `development`; `production` rejects a `sqlite` `database_url`), `Settings.ledger_backend: Literal["file", "db"]` (default `file`), `Settings.cloud_database_url: SecretStr | None`, `Settings.registry: str | None`, `Settings.cloud_platform: str = "cpu-d3"`, `Settings.cloud_preset: str = "2vcpu-8gb"`, `Settings.cloud_pg_preset: str = "2vcpu-8gb"`, `Settings.cloud_pg_disk_gib: int = 32`, `Settings.cloud_endpoint_disk_gib: int = 32` (added by the 6.3 review: the endpoint's container disk, passed as `--disk-size <n>Gi` by 6.6; the CLI default is 250Gi). Image `waive:dev` listening on 8000, user `waive`, CA for Managed PostgreSQL at `/app/certs/msp-ca.pem` (the same relative path `certs/msp-ca.pem` works locally from the repo root).

- [ ] **Step 1: Add the PostgreSQL driver**

Run: `uv add "psycopg[binary]>=3.2"`
Expected: `pyproject.toml` gains `"psycopg[binary]>=3.2"` and `uv.lock` updates.

- [ ] **Step 2: Write the failing tests**

Append to `tests/unit/test_config.py` (add `import pytest` and `from pydantic import ValidationError` to the imports):

```python
def test_production_rejects_the_sqlite_default(monkeypatch):
    clear_env(monkeypatch)
    with pytest.raises(ValidationError, match="WAIVE_DATABASE_URL"):
        Settings(_env_file=None, env="production")
    settings = Settings(
        _env_file=None,
        env="production",
        database_url="postgresql+psycopg://waive:pw@db.example.net:5432/waive",
    )
    assert settings.env == "production"


def test_development_defaults_keep_sqlite_and_the_file_ledger(monkeypatch):
    clear_env(monkeypatch)
    settings = Settings(_env_file=None)
    assert settings.env == "development"
    assert settings.ledger_backend == "file"
    assert settings.database_url == "sqlite:///var/waive.db"
    assert settings.cloud_database_url is None and settings.registry is None
    assert (settings.cloud_platform, settings.cloud_preset) == ("cpu-d3", "2vcpu-8gb")
    assert (settings.cloud_pg_preset, settings.cloud_pg_disk_gib) == ("2vcpu-8gb", 32)
```

`tests/unit/test_dockerfile.py`:

```python
"""The production image: two stages, uv-built, non-root, app factory on port 8000 (spec §14)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def dockerfile_lines():
    return [line.strip() for line in (ROOT / "Dockerfile").read_text().splitlines() if line.strip()]


def test_two_stages_built_with_uv_without_dev_dependencies():
    lines = dockerfile_lines()
    froms = [line for line in lines if line.startswith("FROM ")]
    assert len(froms) == 2
    assert "astral-sh/uv" in froms[0] and "python:3.12-slim" in froms[1]
    text = "\n".join(lines)
    assert "uv sync --frozen --no-dev --no-install-project" in text
    assert "uv sync --frozen --no-dev --no-editable" in text
    assert "COPY . " not in text and "COPY ./ " not in text  # the context is copied selectively


def test_runs_the_app_factory_as_non_root_on_port_8000():
    lines = dockerfile_lines()
    cmd = next(line for line in lines if line.startswith("CMD "))
    assert "USER waive" in lines and "EXPOSE 8000" in lines
    assert lines.index("USER waive") < lines.index(cmd)
    for token in (
        '"uvicorn"',
        '"waive.web.app:create_app"',
        '"--factory"',
        '"--port", "8000"',
        '"--no-access-log"',  # capability tokens travel in URLs; access logs must not keep them
    ):
        assert token in cmd


def test_build_context_excludes_secrets_tests_and_local_data():
    ignored = {line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()}
    assert {".env", ".env.*", "!.env.example", "var/", "tests/", "docs/", ".git", "*.db"} <= ignored
```

- [ ] **Step 3: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_dockerfile.py -v`
Expected: the two new config tests FAIL (`Settings` has no field `env`; pydantic ignores unknown fields, so the first fails with `DID NOT RAISE` and the second with `AttributeError: 'Settings' object has no attribute 'env'`); the three Dockerfile tests FAIL with `FileNotFoundError: .../Dockerfile`.

- [ ] **Step 4: Implement the settings**

Replace `src/waive/config.py` with:

```python
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

    @model_validator(mode="after")
    def _production_needs_postgres(self) -> Self:
        if self.env == "production" and self.database_url.startswith("sqlite"):
            raise ValueError(
                "WAIVE_ENV=production needs WAIVE_DATABASE_URL pointing at PostgreSQL; "
                "the SQLite default is for development only"
            )
        return self
```

Append to `.env.example`:

```
# Deployment (Phase 6). Development keeps SQLite and the file ledger; the container sets
# WAIVE_ENV=production and WAIVE_LEDGER_BACKEND=db itself.
WAIVE_ENV=development
WAIVE_LEDGER_BACKEND=file
# Filled by tasks 6.4 and 6.5 (image registry path; production database URL with password).
WAIVE_REGISTRY=
WAIVE_CLOUD_DATABASE_URL=
WAIVE_CLOUD_PLATFORM=cpu-d3
WAIVE_CLOUD_PRESET=2vcpu-8gb
WAIVE_CLOUD_PG_PRESET=2vcpu-8gb
WAIVE_CLOUD_PG_DISK_GIB=32
```

- [ ] **Step 5: Write the Dockerfile and `.dockerignore`**

`Dockerfile`:

```dockerfile
# syntax=docker/dockerfile:1
# Stage 1: resolve uv.lock into a virtual environment, then install the project itself.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# Stage 2: a slim runtime without uv or build tools, running as a non-root user.
FROM python:3.12-slim-bookworm AS runtime
RUN groupadd --system waive \
    && useradd --system --gid waive --home-dir /app --shell /usr/sbin/nologin waive
WORKDIR /app
COPY --from=builder --chown=waive:waive /app/.venv /app/.venv
# CA certificate for Managed PostgreSQL (sslmode=verify-full), from
# https://docs.nebius.com/postgresql/databases/connect.md
ADD --chown=waive:waive https://storage.eu-north1.nebius.cloud/msp-certs/ca.pem /app/certs/msp-ca.pem
RUN mkdir -p /app/var && chown waive:waive /app/var
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER waive
EXPOSE 8000
# --no-access-log: request paths carry capability tokens. --proxy-headers: the managed HTTPS
# front end terminates TLS, so the app must trust X-Forwarded-* for scheme and client address.
CMD ["uvicorn", "waive.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
```

`.dockerignore`:

```
.git
.venv
.env
.env.*
!.env.example
var/
data/raw/
data/cache/
certs/
tests/
docs/
.hypothesis/
.pytest_cache/
.ruff_cache/
.coverage
htmlcov/
*.db
*.sqlite
*.sqlite3
__pycache__/
*.py[cod]
.claude/
.DS_Store
```

- [ ] **Step 6: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_dockerfile.py -v`
Expected: 8 passed (3 existing config tests, 2 new, 3 Dockerfile).

- [ ] **Step 7: Build the image and smoke-test it locally with SQLite**

```bash
docker build -t waive:dev .
docker image ls waive:dev
docker run --rm -d --name waive-smoke -p 8000:8000 \
  -e WAIVE_VAULT_KEY="$(uv run python -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())')" \
  -e WAIVE_TOKEN_SECRET="$(uv run python -c 'import secrets; print(secrets.token_hex(32))')" \
  waive:dev
for _ in 1 2 3 4 5 6 7 8 9 10; do curl -fsS http://127.0.0.1:8000/healthz && break; sleep 1; done
curl -fsS -o /dev/null -w 'home: %{http_code}\n' http://127.0.0.1:8000/
docker exec waive-smoke id -u
docker exec waive-smoke ls -l /app/certs/msp-ca.pem /app/var
docker logs waive-smoke | head -20
docker rm -f waive-smoke
docker run --rm -e WAIVE_ENV=production waive:dev; echo "exit code: $?"
```

Expected: the build finishes (first run downloads the base images, ~2–4 minutes). Measured in 6.1b (2026-10-08, arm64 build): `docker image ls` shows **507 MB** uncompressed; `docker image inspect` `.Size` is 106,208,315 bytes (content/compressed); the runtime `.venv` alone is 216 MB (sqlalchemy, openai, uvloop, psycopg binary, pillow, cryptography, pygments, reportlab) — the production dependency set with no dev packages, pip or uv in the runtime stage, so the earlier 250–350 MB estimate was low, not a build mistake. Then `{"ok":true}` then `home: 200`; `id -u` prints a non-zero uid; `msp-ca.pem` exists and `/app/var` contains `waive.db`; the logs show uvicorn started and no access-log lines; the last command prints a pydantic `ValidationError` mentioning `WAIVE_DATABASE_URL` and a non-zero exit code. If the `ADD https://…ca.pem` line fails with a 4xx/5xx, re-check the URL on https://docs.nebius.com/postgresql/databases/connect.md and update the Dockerfile; do not switch to `sslmode=require`.

- [ ] **Step 8: Lint, format, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pytest
git add Dockerfile .dockerignore pyproject.toml uv.lock .env.example src/waive/config.py tests/unit/test_config.py tests/unit/test_dockerfile.py
git commit -m "feat: production image (uv multi-stage, non-root) and production settings (6.1)"
```

Expected: 253 tests pass.

---

### Task 6.2: Database-backed usage ledger

**Files:**
- Modify: `src/waive/db.py` (add `UsageEventRow`), `src/waive/governor.py` (add `DbLedger`, `make_ledger`, `make_governor(settings, engine=None)`), `src/waive/web/app.py` (one governor per app on `app.state.governor`), `src/waive/web/routes_admin.py` (budget card reads `app.state.governor`)
- Test: `tests/unit/test_governor.py` (four tests added)

**Interfaces:**
- Consumes: `Settings.ledger_backend`, `Settings.database_url` (6.1); `db.init_db / make_engine / session_scope`.
- Produces: `db.UsageEventRow` (table `usage_events`: `id`, `provider`, `units`, `usd`, `purpose`, `ts` — amounts as exact decimal strings, never content); `governor.DbLedger(engine)` with `record(event)` and `totals(provider)` exactly like `Ledger`; `governor.make_ledger(settings, engine=None) -> Ledger | DbLedger`; `governor.make_governor(settings, engine=None)`; `app.state.governor` on every app from `create_app`. The file ledger and every existing caller (`cli.py`, `doctor.py`, tests) keep working unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_governor.py` (extend the imports to: `import base64`, `from pydantic import SecretStr`, `from sqlalchemy import func, select`, `from waive.cases.vault import FieldCipher, TokenSigner, new_key`, `from waive.config import Settings`, `from waive.db import UsageEventRow, init_db, make_engine, session_scope`, `from waive.governor import BudgetExceeded, DbLedger, Governor, Ledger, UsageEvent, make_ledger`, `from waive.web.app import create_app`):

```python
def memory_engine():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return engine


def test_db_ledger_records_and_totals_like_the_file_ledger():
    engine = memory_engine()
    governor = Governor(DbLedger(engine), 10, Decimal("1.00"))
    governor.record_tavily(Decimal("2"), "atlas.scout")
    governor.record_token_factory(1000, 500, Decimal("0.0123"), "atlas.structure")
    assert governor.summary() == {
        "tavily": (Decimal("2"), Decimal("0")),
        "token_factory": (Decimal("1500"), Decimal("0.0123")),
    }
    # A second ledger over the same database sees the same history (that is the point).
    assert Governor(DbLedger(engine), 10, Decimal("1")).summary()["tavily"][0] == Decimal("2")
    columns = {column.name for column in UsageEventRow.__table__.columns}
    assert columns == {"id", "provider", "units", "usd", "purpose", "ts"}  # amounts only


def test_db_ledger_cap_check_sees_earlier_rows():
    governor = Governor(DbLedger(memory_engine()), 3, Decimal("1"))
    governor.record_tavily(Decimal("2"), "t")
    with pytest.raises(BudgetExceeded):
        governor.ensure_tavily(Decimal("2"))


def test_make_ledger_picks_the_backend_from_settings(tmp_path):
    file_settings = Settings(_env_file=None, ledger_path=tmp_path / "usage.jsonl")
    assert isinstance(make_ledger(file_settings), Ledger)
    db_settings = Settings(_env_file=None, ledger_backend="db", ledger_path=tmp_path / "usage.jsonl")
    engine = make_engine("sqlite+pysqlite:///:memory:")  # no init_db: make_ledger must do it
    ledger = make_ledger(db_settings, engine=engine)
    assert isinstance(ledger, DbLedger)
    ledger.record(UsageEvent("tavily", Decimal("1"), Decimal("0"), "t", "2026-10-02T00:00:00+00:00"))
    assert ledger.totals("tavily") == (Decimal("1"), Decimal("0"))
    assert not (tmp_path / "usage.jsonl").exists()


def test_create_app_uses_the_database_ledger_when_configured():
    engine = memory_engine()
    settings = Settings(_env_file=None, nebius_api_key=SecretStr("k"), ledger_backend="db")
    app = create_app(
        settings,
        engine=engine,
        cipher=FieldCipher(base64.b64decode(new_key())),
        signer=TokenSigner("x" * 40),
    )
    app.state.governor.record_tavily(Decimal("1"), "t")
    assert app.state.governor.summary()["tavily"] == (Decimal("1"), Decimal("0"))
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(UsageEventRow)) == 1
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_governor.py -v`
Expected: FAIL at import with `ImportError: cannot import name 'UsageEventRow' from 'waive.db'`.

- [ ] **Step 3: Implement**

Add to `src/waive/db.py` after `ReportedEvidenceRow`:

```python
class UsageEventRow(Base):
    """One paid call for the budget governor: provider, amounts and purpose. Never request or
    response content (spec §11). Amounts are exact decimal strings, as in var/usage.jsonl."""

    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(20), index=True)
    units: Mapped[str] = mapped_column(String(40))
    usd: Mapped[str] = mapped_column(String(40))
    purpose: Mapped[str] = mapped_column(String(80))
    ts: Mapped[str] = mapped_column(String(40))
```

Replace the bottom of `src/waive/governor.py` (everything from `def make_governor`) and extend its imports:

```python
from sqlalchemy import select
from sqlalchemy.engine import Engine

from waive.db import UsageEventRow, init_db, make_engine, session_scope
```

```python
class DbLedger:
    """The `Ledger` interface stored in the `usage_events` table, so a stateless container keeps
    its spend history in the database it already has."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, event: UsageEvent) -> None:
        row = {key: str(value) for key, value in asdict(event).items()}
        with session_scope(self._engine) as session:
            session.add(UsageEventRow(**row))

    def totals(self, provider: str) -> tuple[Decimal, Decimal]:
        units = usd = Decimal("0")
        query = select(UsageEventRow.units, UsageEventRow.usd).where(
            UsageEventRow.provider == provider
        )
        with session_scope(self._engine) as session:
            rows = session.execute(query).all()
        for row_units, row_usd in rows:
            units += Decimal(row_units)
            usd += Decimal(row_usd)
        return units, usd


def make_ledger(settings: Settings, engine: Engine | None = None) -> Ledger | DbLedger:
    """`WAIVE_LEDGER_BACKEND=db` → the `usage_events` table (created if missing); otherwise the
    JSON-lines file at `WAIVE_LEDGER_PATH`."""
    if settings.ledger_backend == "db":
        engine = engine or make_engine(settings.database_url)
        init_db(engine)
        return DbLedger(engine)
    return Ledger(settings.ledger_path)


def make_governor(settings: Settings, engine: Engine | None = None) -> Governor:
    return Governor(
        make_ledger(settings, engine), settings.tavily_credit_cap, settings.token_factory_usd_cap
    )
```

Change the `Governor.__init__` annotation from `ledger: Ledger` to `ledger: "Ledger | DbLedger"` (the class is defined later in the file; the string annotation is enough).

In `src/waive/web/app.py` replace `_default_ai` and the body of `create_app` up to `app.mount`:

```python
def _default_ai(settings: Settings, governor):
    if settings.nebius_api_key is None:
        return None
    return AIClient(settings, governor)


def create_app(
    settings: Settings | None = None,
    *,
    engine=None,
    ai=None,
    cipher=None,
    signer=None,
    today_fn=None,
) -> FastAPI:
    configure_logging()
    settings = settings or Settings()
    engine = engine or make_engine(settings.database_url)
    init_db(engine)
    governor = make_governor(settings, engine)
    app = FastAPI(title="Waive", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.governor = governor
    app.state.deps = Deps(
        engine=engine,
        ai=ai if ai is not None else _default_ai(settings, governor),
        cipher=cipher or cipher_from_settings(settings),
        signer=signer or signer_from_settings(settings),
        today_fn=today_fn or today_utc,
        templates=Jinja2Templates(directory=str(TEMPLATES_DIR)),
    )
```

In `src/waive/web/routes_admin.py` delete `from waive.governor import Ledger` and replace the three ledger lines in `admin_home` with:

```python
    governor = request.app.state.governor
    tavily_used, _ = governor.summary()["tavily"]
    _, tf_used = governor.summary()["token_factory"]
```

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_governor.py tests/unit/test_web_admin.py tests/unit/test_ai_client.py tests/unit/test_doctor.py -v`
Expected: all pass (the admin budget card still shows the file-ledger fixture because `Settings(ledger_path=…)` keeps `ledger_backend="file"`). If `test_web_admin` fails on the budget numbers, the fixture's `LEDGER` file is read through `app.state.governor` — check that `create_app` received the same `settings` object the test built.

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pytest
git add src/waive/db.py src/waive/governor.py src/waive/web/app.py src/waive/web/routes_admin.py tests/unit/test_governor.py
git commit -m "feat: usage ledger in the database when WAIVE_LEDGER_BACKEND=db (6.2)"
```

Expected: 257 tests pass.

---

### Task 6.3: `waive cloud discover` — read-only discovery and the cost table (gate U0.5), then STOP for U6.1

**Files:**
- Create: `src/waive/cloud/__init__.py`, `src/waive/cloud/nebius.py`, `src/waive/cloud/costs.py`, `src/waive/cloud/discover.py`, `src/waive/cloud/cli.py`
- Modify: `src/waive/cli.py` (mount `cloud_app`)
- Test: `tests/unit/test_cloud_costs.py`, `tests/unit/test_cloud_discover.py`

**Interfaces:**
- Consumes: `Settings.nebius_project_id`, `Settings.cloud_platform / cloud_preset / cloud_pg_preset / cloud_pg_disk_gib` (6.1).
- Produces: `cloud.nebius.Nebius(project_id, runner=run_subprocess, binary="nebius")` with `run(*args, parent=True, json_output=True, redact=False) -> Any` (adds `--parent-id <project>` and `--format json`, raises `NebiusError`), `text(*args) -> str`, `Nebius.from_settings(settings, runner=None)` (raises `NebiusMissing` when the CLI or the project id is absent); helpers `items(payload) -> list[dict]`, `name_of(resource) -> str`, `id_of(resource) -> str`; `Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]`. `cloud.costs`: `PRICES_DATE`, `HOURS_PER_MONTH = 730`, `COMPUTE_PRICES`, `POSTGRES_PRICE`, `NETWORK_SSD_GIB_MONTH`, `parse_preset("2vcpu-8gb") -> (2, 8)`, `endpoint_hourly(platform, preset) -> Decimal`, `postgres_hourly(preset, disk_gib) -> Decimal`, `monthly(hourly) -> Decimal`, `Footprint(platform, endpoint_preset, postgres_preset, disk_gib, budget_usd=Decimal("30"))` with `.endpoint`, `.postgres`, `.total`, `.hours_in_budget()`. `cloud.discover`: `PREFIX = "waive-"`, `Discovery` dataclass, `discover(nebius) -> Discovery`, `smallest_cpu_preset(platforms, platform) -> str | None`, `render_report(discovery, footprint, generated) -> str`, `write_discovery(discovery, footprint, report_path, raw_path) -> str`. `cloud.cli.cloud_app` (Typer) with `discover`; later tasks add `secrets push`, `deploy`, `start`, `stop`, `status`, `cleanup`. The test fake `tests/unit/test_cloud_discover.py::FakeRunner(answers)` is reused by 6.6 and 6.7.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_cloud_costs.py`:

```python
"""Price arithmetic for the Nebius footprint (spec §15). Numbers are the published list prices
effective 2026-10-01, read 2026-10-02 — see the URLs in waive/cloud/costs.py."""

from decimal import Decimal

import pytest

from waive.cloud.costs import Footprint, endpoint_hourly, monthly, parse_preset, postgres_hourly


def test_parse_preset_reads_vcpu_and_gib():
    assert parse_preset("2vcpu-8gb") == (2, 8)
    assert parse_preset("16vcpu-64gb") == (16, 64)
    with pytest.raises(ValueError):
        parse_preset("medium")


def test_endpoint_hourly_follows_the_compute_price_list():
    assert endpoint_hourly("cpu-d3", "2vcpu-8gb") == Decimal("0.066")  # 2×0.015 + 8×0.0045
    assert endpoint_hourly("cpu-e2", "2vcpu-8gb") == Decimal("0.060")  # 2×0.012 + 8×0.0045
    assert endpoint_hourly("cpu-d3", "4vcpu-16gb") == Decimal("0.132")
    with pytest.raises(KeyError):
        endpoint_hourly("gpu-h100-sxm", "1gpu-16vcpu-200gb")


def test_postgres_hourly_reproduces_the_docs_example_and_adds_disk():
    # docs: "using a 4vcpu-16gb cluster for 1 hour costs 4 × $0.034 + 16 × $0.009 = $0.28"
    assert postgres_hourly("4vcpu-16gb", 0) == Decimal("0.28")
    assert postgres_hourly("2vcpu-8gb", 32).quantize(Decimal("0.0001")) == Decimal("0.1431")


def test_footprint_totals_and_hours_within_budget():
    footprint = Footprint("cpu-d3", "2vcpu-8gb", "2vcpu-8gb", 32)
    assert footprint.total.quantize(Decimal("0.0001")) == Decimal("0.2091")
    assert footprint.hours_in_budget() == 143
    assert monthly(footprint.endpoint) == Decimal("48.18")
    assert Footprint("cpu-d3", "2vcpu-8gb", "2vcpu-8gb", 32, Decimal("10")).hours_in_budget() == 47
```

`tests/unit/test_cloud_discover.py`:

```python
"""The nebius CLI wrapper and the read-only discovery. Every CLI call is answered by FakeRunner;
no `nebius` binary is needed and none is run."""

import json
import subprocess

import pytest
from typer.testing import CliRunner

from waive.cli import app
from waive.cloud.costs import Footprint
from waive.cloud.discover import discover, smallest_cpu_preset, write_discovery
from waive.cloud.nebius import Nebius, NebiusError, items


class FakeRunner:
    """Answers `nebius …` invocations from a table keyed by the longest matching prefix of the
    arguments after `nebius`. An Exception value makes that command fail with its text as stderr."""

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


ANSWERS = {
    "version": "nebius 0.12.0\n",
    "iam project get project-test": {
        "metadata": {"name": "waive", "id": "project-test"},
        "status": {"region": "eu-north1"},
    },
    "compute platform list": {
        "items": [
            {
                "metadata": {"name": "cpu-d3"},
                "spec": {"presets": [{"name": "4vcpu-16gb"}, {"name": "2vcpu-8gb"}, {"name": "8vcpu-32gb"}]},
            },
            {"metadata": {"name": "gpu-h100-sxm"}, "spec": {"presets": [{"name": "1gpu-16vcpu-200gb"}]}},
        ]
    },
    "vpc network list": {"items": [{"metadata": {"name": "default-network", "id": "vpcnetwork-e00abc"}}]},
    "vpc subnet list": {"items": [{"metadata": {"name": "default-subnet", "id": "vpcsubnet-e00def"}}]},
    "ai endpoint list": {"items": []},
    "msp postgresql v1alpha1 cluster list": {"items": [{"metadata": {"name": "other-db", "id": "c1"}}]},
    "msp postgresql v1alpha1 cluster create --help": "Usage: nebius msp postgresql v1alpha1 cluster create [flags]\n  --config-template-resources-preset string\n",
    "mysterybox secret list": {"items": [{"metadata": {"name": "waive-secrets", "id": "mbsec-1"}}]},
    "registry list": {"items": []},
    "ai endpoint create --help": "Usage: nebius ai endpoint create [flags]\n  --preset string\n",
}


def test_wrapper_adds_parent_and_json_and_parses():
    runner = FakeRunner(ANSWERS)
    nebius = Nebius("project-test", runner=runner)
    payload = nebius.run("vpc", "network", "list")
    assert items(payload)[0]["metadata"]["id"] == "vpcnetwork-e00abc"
    assert runner.calls[0] == ["nebius", "vpc", "network", "list", "--parent-id", "project-test", "--format", "json"]
    assert nebius.text("version") == "nebius 0.12.0\n"
    assert runner.calls[1] == ["nebius", "version"]
    assert items([{"metadata": {"name": "bare-list"}}, "noise"]) == [{"metadata": {"name": "bare-list"}}]


def test_wrapper_failures_name_the_command_and_can_withhold_stderr():
    runner = FakeRunner({"mysterybox secret create": RuntimeError("payload was: tf-secret-123")})
    nebius = Nebius("project-test", runner=runner)
    with pytest.raises(NebiusError) as plain:
        nebius.run("mysterybox", "secret", "create", "--name", "x")
    assert "tf-secret-123" in str(plain.value)
    with pytest.raises(NebiusError) as redacted:
        nebius.run("mysterybox", "secret", "create", "--name", "x", redact=True)
    assert "tf-secret-123" not in str(redacted.value)
    assert "mysterybox secret create" in str(redacted.value)


def test_discover_is_read_only_and_the_report_holds_no_ids(tmp_path):
    runner = FakeRunner(ANSWERS)
    found = discover(Nebius("project-test", runner=runner))
    assert (found.project_name, found.region) == ("waive", "eu-north1")
    assert found.platforms["cpu-d3"] == ["4vcpu-16gb", "2vcpu-8gb", "8vcpu-32gb"]
    assert smallest_cpu_preset(found.platforms, "cpu-d3") == "2vcpu-8gb"
    assert smallest_cpu_preset(found.platforms, "cpu-e2") is None
    assert found.networks == [("default-network", "vpcnetwork-e00abc")]
    assert found.existing == {"endpoint": [], "postgres": [], "secret": ["waive-secrets"], "registry": []}
    assert "--config-template-resources-preset" in found.postgres_help
    for call in runner.calls:  # only version, get, list and --help
        assert not ({"create", "delete", "update", "start", "stop"} & set(call)) or "--help" in call, call

    footprint = Footprint("cpu-d3", "2vcpu-8gb", "2vcpu-8gb", 32)
    text = write_discovery(found, footprint, tmp_path / "cloud-costs.md", tmp_path / "raw.json")
    assert text == (tmp_path / "cloud-costs.md").read_text()
    assert "eu-north1" in text and "`cpu-d3`" in text and "0.0660" in text and "143 hours" in text
    assert "waive-secrets" in text and "docs.nebius.com" in text
    assert "project-test" not in text and "vpcnetwork-e00abc" not in text  # ids stay out of git
    assert "project-test" in (tmp_path / "raw.json").read_text()


def test_cli_discover_explains_gate_u05_when_the_cli_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("waive.cloud.nebius.shutil.which", lambda name: None)
    result = CliRunner().invoke(app, ["cloud", "discover", "--out", str(tmp_path / "r.md")])
    assert result.exit_code == 2
    assert "U0.5" in result.output and "install.sh" in result.output
    assert not (tmp_path / "r.md").exists()
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_cloud_costs.py tests/unit/test_cloud_discover.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.cloud'`.

- [ ] **Step 3: Implement the CLI wrapper and the prices**

`src/waive/cloud/__init__.py`:

```python
"""Deployment on Nebius AI Cloud through the `nebius` CLI (spec §14, §15)."""
```

`src/waive/cloud/nebius.py`:

```python
"""A thin, testable wrapper around the `nebius` CLI (gate U0.5).

Every call is `subprocess.run` with an argument list — no shell, so nothing is expanded, globbed
or written to a shell history. `--format json` is added to machine-readable calls and the project
id from `.env` (`NEBIUS_PROJECT_ID`) becomes `--parent-id`. Commands that carry secret values run
with `redact=True`, which keeps the CLI's stderr out of error messages. CLI reference:
https://docs.nebius.com/cli/configure.md
"""

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from waive.config import Settings

INSTALL_HINT = (
    "install it with `curl -sSL https://artifacts.nebius.cloud/cli/install.sh | bash`, then run "
    "`nebius profile create` (browser sign-in) — gate U0.5"
)

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class NebiusError(RuntimeError):
    """A `nebius` command failed."""


class NebiusMissing(NebiusError):
    """The CLI or its configuration is absent (gates U0.3 and U0.5)."""


def run_subprocess(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=600, check=False)


@dataclass
class Nebius:
    project_id: str
    runner: Runner = run_subprocess
    binary: str = "nebius"

    @classmethod
    def from_settings(cls, settings: Settings, runner: Runner | None = None) -> "Nebius":
        if shutil.which(cls.binary) is None:
            raise NebiusMissing(f"the nebius CLI is not installed: {INSTALL_HINT}")
        if not settings.nebius_project_id:
            raise NebiusMissing("NEBIUS_PROJECT_ID is not set in .env (gate U0.3)")
        return cls(settings.nebius_project_id, runner or run_subprocess)

    def run(
        self,
        *args: str,
        parent: bool = True,
        json_output: bool = True,
        redact: bool = False,
    ) -> Any:
        argv = [self.binary, *args]
        if parent:
            argv += ["--parent-id", self.project_id]
        if json_output:
            argv += ["--format", "json"]
        result = self.runner(argv)
        if result.returncode != 0:
            shown = " ".join(args[:3])  # subcommand words only, never flag values
            detail = (
                "stderr withheld because the command carried secret values"
                if redact
                else result.stderr.strip()[:500]
            )
            raise NebiusError(f"`nebius {shown}` failed with exit code {result.returncode}: {detail}")
        if not json_output:
            return result.stdout
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def text(self, *args: str) -> str:
        return self.run(*args, parent=False, json_output=False)


def items(payload: Any) -> list[dict[str, Any]]:
    """List responses arrive as `{"items": [...]}`; a bare list is tolerated."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [item for item in payload.get("items", []) if isinstance(item, dict)]
    return []


def name_of(resource: dict[str, Any]) -> str:
    return str(resource.get("metadata", {}).get("name", ""))


def id_of(resource: dict[str, Any]) -> str:
    return str(resource.get("metadata", {}).get("id", ""))
```

`src/waive/cloud/costs.py`:

```python
"""Published Nebius list prices (USD, effective 2026-10-01) and the arithmetic for Waive's
footprint (spec §15). Sources, read 2026-10-02:

- Compute (Serverless AI endpoints bill at Compute prices):
  https://docs.nebius.com/compute/resources/pricing.md and
  https://docs.nebius.com/serverless/pricing-quotas.md
- Managed PostgreSQL: https://docs.nebius.com/postgresql/resources/pricing.md
- Container Registry (free): https://docs.nebius.com/container-registry/resources/pricing.md
- SecretStash (free in preview): https://docs.nebius.com/mysterybox/resources/pricing.md

Update the constants when the pages change; the console's Billing → Usage page is the source of
truth once resources exist (no CLI command exposes spend).
"""

import re
from dataclasses import dataclass
from decimal import Decimal

PRICES_DATE = "2026-10-02"
HOURS_PER_MONTH = 730
SOURCES = (
    "https://docs.nebius.com/compute/resources/pricing.md",
    "https://docs.nebius.com/serverless/pricing-quotas.md",
    "https://docs.nebius.com/postgresql/resources/pricing.md",
    "https://docs.nebius.com/container-registry/resources/pricing.md",
    "https://docs.nebius.com/mysterybox/resources/pricing.md",
)


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
    match = _PRESET.match(preset)
    if match is None:
        raise ValueError(f"unknown preset format {preset!r}; expected something like 2vcpu-8gb")
    return int(match.group(1)), int(match.group(2))


def endpoint_hourly(platform: str, preset: str) -> Decimal:
    price = COMPUTE_PRICES[platform]
    vcpu, gib = parse_preset(preset)
    return price.vcpu_hour * vcpu + price.gib_hour * gib


def postgres_hourly(preset: str, disk_gib: int) -> Decimal:
    vcpu, gib = parse_preset(preset)
    disk = NETWORK_SSD_GIB_MONTH * disk_gib / HOURS_PER_MONTH
    return POSTGRES_PRICE.vcpu_hour * vcpu + POSTGRES_PRICE.gib_hour * gib + disk


def monthly(hourly: Decimal) -> Decimal:
    return (hourly * HOURS_PER_MONTH).quantize(Decimal("0.01"))


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
```

- [ ] **Step 4: Implement discovery and the report**

`src/waive/cloud/discover.py`:

```python
"""Read-only discovery of what the project offers (task 6.3). Only `version`, `get`, `list` and
`--help` calls — nothing here creates, changes or deletes a resource. Ids and raw JSON go to a
gitignored file; the committed report carries names, presets and prices only."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from waive.cloud.costs import FREE, PRICES_DATE, SOURCES, Footprint, monthly, parse_preset
from waive.cloud.nebius import Nebius, NebiusError, id_of, items, name_of

PREFIX = "waive-"


@dataclass
class Discovery:
    cli_version: str
    project_name: str
    region: str | None
    platforms: dict[str, list[str]]  # platform id -> preset names
    networks: list[tuple[str, str]]  # (name, id)
    subnets: list[tuple[str, str]]
    existing: dict[str, list[str]]  # kind -> names starting with "waive-"
    postgres_help: str
    endpoint_help: str
    raw: dict[str, Any] = field(default_factory=dict)


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


def _presets(platform: dict[str, Any]) -> list[str]:
    presets = platform.get("spec", {}).get("presets") or platform.get("presets") or []
    names: list[str] = []
    for preset in presets:
        if isinstance(preset, str):
            names.append(preset)
        elif isinstance(preset, dict):
            name = preset.get("name") or preset.get("id") or ""
            if name:
                names.append(str(name))
    return names


def _waive_names(payload: Any) -> list[str]:
    return [name_of(item) for item in items(payload) if name_of(item).startswith(PREFIX)]


def discover(nebius: Nebius) -> Discovery:
    raw: dict[str, Any] = {"errors": {}}

    def attempt(key: str, *command: str, parent: bool = True, json_output: bool = True) -> Any:
        try:
            raw[key] = nebius.run(*command, parent=parent, json_output=json_output)
        except NebiusError as error:
            raw["errors"][key] = str(error)
            raw[key] = {} if json_output else ""
        return raw[key]

    version = attempt("version", "version", parent=False, json_output=False).strip()
    project = attempt("project", "iam", "project", "get", nebius.project_id, parent=False)
    platforms = {
        (name_of(p) or id_of(p)): _presets(p)
        for p in items(attempt("platforms", "compute", "platform", "list"))
    }
    networks = [(name_of(n), id_of(n)) for n in items(attempt("networks", "vpc", "network", "list"))]
    subnets = [(name_of(s), id_of(s)) for s in items(attempt("subnets", "vpc", "subnet", "list"))]
    existing = {
        "endpoint": _waive_names(attempt("endpoints", "ai", "endpoint", "list")),
        "postgres": _waive_names(
            attempt("clusters", "msp", "postgresql", "v1alpha1", "cluster", "list")
        ),
        "secret": _waive_names(attempt("secrets", "mysterybox", "secret", "list")),
        "registry": _waive_names(attempt("registries", "registry", "list")),
    }
    postgres_help = attempt(
        "postgres_help", "msp", "postgresql", "v1alpha1", "cluster", "create", "--help",
        parent=False, json_output=False,
    )
    endpoint_help = attempt(
        "endpoint_help", "ai", "endpoint", "create", "--help", parent=False, json_output=False
    )
    return Discovery(
        cli_version=version,
        project_name=name_of(project) if isinstance(project, dict) else "",
        region=_find_key(project, ("region", "region_id")),
        platforms=platforms,
        networks=networks,
        subnets=subnets,
        existing=existing,
        postgres_help=postgres_help,
        endpoint_help=endpoint_help,
        raw=raw,
    )


def smallest_cpu_preset(platforms: dict[str, list[str]], platform: str) -> str | None:
    candidates = []
    for preset in platforms.get(platform, []):
        try:
            candidates.append((parse_preset(preset), preset))
        except ValueError:
            continue
    return min(candidates)[1] if candidates else None


def render_report(d: Discovery, fp: Footprint, generated: str) -> str:
    cpu = {k: v for k, v in d.platforms.items() if k.startswith("cpu-")}
    lines = [
        "# Nebius AI Cloud: discovery and cost estimate for Waive",
        "",
        f"Generated by `waive cloud discover` on {generated}. Read-only: nothing was created. "
        "Resource ids and the raw CLI output are in `var/cloud-discovery.json` (not committed).",
        "",
        "## Project",
        "",
        f"- Nebius CLI: {d.cli_version or 'unknown'}",
        f"- Project: {d.project_name or 'unknown'}; region: "
        f"{d.region or 'not in the project record — read it from the console URL'}",
        f"- Networks: {', '.join(name for name, _ in d.networks) or 'none listed'}; "
        f"subnets: {', '.join(name for name, _ in d.subnets) or 'none listed'}",
        "",
        "## CPU platforms and presets available to this project",
        "",
    ]
    if cpu:
        for platform, presets in sorted(cpu.items()):
            shown = ", ".join(presets) if presets else "presets not in the listing (see raw JSON)"
            lines.append(f"- `{platform}`: {shown}")
    else:
        lines.append(
            "- no `cpu-*` platform in the listing (see the raw JSON); the docs list `cpu-d3` "
            "(AMD EPYC Genoa, all regions) and `cpu-e2` (Intel Ice Lake, eu-north1 only)"
        )
    lines += [
        "",
        f"## Footprint and cost (USD list prices effective 2026-10-01, read {PRICES_DATE})",
        "",
        "| Resource | Preset | $/hour running | $/month at 730 h |",
        "|---|---|---|---|",
        f"| Serverless AI endpoint `{PREFIX}web` | {fp.platform} {fp.endpoint_preset} | "
        f"{fp.endpoint:.4f} | {monthly(fp.endpoint)} |",
        f"| Managed PostgreSQL `{PREFIX}db` | {fp.postgres_preset}, {fp.disk_gib} GiB network-ssd | "
        f"{fp.postgres:.4f} | {monthly(fp.postgres)} |",
        f"| Container Registry `{PREFIX}registry` | — | 0 | 0 (free) |",
        f"| SecretStash `{PREFIX}secrets` | — | 0 | 0 (free in preview) |",
        f"| **Total while both run** | | **{fp.total:.4f}** | **{monthly(fp.total)}** |",
        "",
        f"Budget ${fp.budget_usd} (master plan, through 2026-10-30) buys about "
        f"{fp.hours_in_budget()} hours with both running, or about "
        f"{int(fp.budget_usd / fp.postgres)} hours of the database alone. A stopped endpoint is "
        "not billed. The database bills until it is deleted: no stop command is documented "
        "(`nebius msp postgresql v1alpha1 cluster create --help` is saved in the raw JSON). "
        "Between demo windows either keep it (≈ $"
        f"{(fp.postgres * 24).quantize(Decimal('0.01'))}/day) or delete it with "
        "`waive cloud cleanup` and recreate it from the local atlas with `waive db copy --to-cloud`.",
        "",
        f"Free: {', '.join(FREE)}.",
        "",
        "## Existing `waive-` resources",
        "",
    ]
    for kind, names in d.existing.items():
        lines.append(f"- {kind}: {', '.join(names) if names else 'none'}")
    lines += [
        "",
        "## What gate U6.1 approves",
        "",
        f"1. Container Registry `{PREFIX}registry` (free) and one image push per deploy.",
        f"2. Managed PostgreSQL `{PREFIX}db`: {fp.postgres_preset}, {fp.disk_gib} GiB, public access "
        f"with TLS, ≈ ${fp.postgres:.3f}/h until deleted.",
        f"3. SecretStash secret `{PREFIX}secrets` with six keys (free).",
        f"4. Serverless AI endpoint `{PREFIX}web`: {fp.platform} {fp.endpoint_preset}, port 8000, "
        f"no endpoint auth (public web app), ≈ ${fp.endpoint:.3f}/h while running, $0 stopped.",
        "5. The running schedule: endpoint started only for build and demo windows; database kept "
        "or recreated per window (choose one).",
        "",
        "## Price sources",
        "",
        *[f"- {url}" for url in SOURCES],
        "",
        "## To confirm on the day (not verifiable from the docs)",
        "",
        "- the endpoint accepts `2vcpu-8gb` (the quickstart used `4vcpu-16gb`, $0.132/h)",
        "- the endpoint pulls from the project's own registry without extra credentials",
        "- the smallest PostgreSQL disk size the CLI accepts, and the allowed platforms outside eu-north1",
        "- whether the cluster can be stopped (see the saved `--help` text)",
    ]
    if d.raw.get("errors"):
        lines += ["", "## CLI calls that failed during discovery", ""]
        lines += [f"- `{key}`: {error}" for key, error in d.raw["errors"].items()]
    return "\n".join(lines) + "\n"


def write_discovery(d: Discovery, fp: Footprint, report_path: Path, raw_path: Path) -> str:
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(d.raw, indent=1, default=str), encoding="utf-8")
    text = render_report(d, fp, datetime.now(UTC).date().isoformat())
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(text, encoding="utf-8")
    return text
```

- [ ] **Step 5: Implement the `waive cloud` command group**

`src/waive/cloud/cli.py`:

```python
"""`waive cloud …`: deployment commands wrapping the nebius CLI (Phase 6)."""

from decimal import Decimal
from pathlib import Path

import typer
from rich.console import Console

from waive.cloud.costs import Footprint
from waive.cloud.discover import discover, smallest_cpu_preset, write_discovery
from waive.cloud.nebius import Nebius, NebiusError, NebiusMissing
from waive.config import Settings

cloud_app = typer.Typer(
    no_args_is_help=True, help="Nebius AI Cloud deployment (wraps the nebius CLI)."
)
console = Console()


def _nebius(settings: Settings) -> Nebius:
    try:
        return Nebius.from_settings(settings)
    except NebiusMissing as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=2) from None


@cloud_app.command("discover")
def cloud_discover(
    out: Path = typer.Option(Path("docs/reports/cloud-costs.md"), "--out"),  # noqa: B008
    raw: Path = typer.Option(Path("var/cloud-discovery.json"), "--raw"),  # noqa: B008
    budget: str = typer.Option("30", "--budget", help="AI Cloud budget in USD (master plan: 30)"),
) -> None:
    """Read-only: list platforms, networks and existing waive- resources; write the cost table."""
    settings = Settings()
    nebius = _nebius(settings)
    try:
        found = discover(nebius)
    except NebiusError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    preset = smallest_cpu_preset(found.platforms, settings.cloud_platform) or settings.cloud_preset
    footprint = Footprint(
        settings.cloud_platform,
        preset,
        settings.cloud_pg_preset,
        settings.cloud_pg_disk_gib,
        Decimal(budget),
    )
    write_discovery(found, footprint, out, raw)
    console.print(f"Wrote {out} and {raw}. Nothing was created. Next: gate U6.1.")
    if found.raw.get("errors"):
        console.print(f"[yellow]{len(found.raw['errors'])} CLI call(s) failed; see the report.[/yellow]")
```

Add to `src/waive/cli.py` after the `learn_app` block:

```python
from waive.cloud.cli import cloud_app

app.add_typer(cloud_app, name="cloud")
```

(Keep the import with the other imports at the top of the file so ruff's isort rule passes.)

- [ ] **Step 6: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_cloud_costs.py tests/unit/test_cloud_discover.py -v`
Expected: 8 passed. If `test_cli_discover_explains_gate_u05_when_the_cli_is_missing` sees exit code 1 instead of 2, `_nebius` is catching the wrong exception class — `NebiusMissing` must be raised from `from_settings` before any command runs.

- [ ] **Step 7: Lint, format, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pytest
git add src/waive/cloud src/waive/cli.py tests/unit/test_cloud_costs.py tests/unit/test_cloud_discover.py
git commit -m "feat: waive cloud discover — read-only Nebius discovery and cost table (6.3)"
```

Expected: 265 tests pass.

- [ ] **Step 8: Run discovery against the real project (gate U0.5 only — read-only)**

Precondition: `uv run waive doctor` shows `Nebius CLI | OK | profile configured`. If it shows `not installed` or `no profile`, stop here and send the user the U0.5 instructions from `docs/PROGRESS.md`; nothing else in this task needs the CLI.

```bash
uv run waive cloud discover
cat docs/reports/cloud-costs.md
grep -n "preset\|platform\|disk-size\|stop" var/cloud-discovery.json | head -40
```

Expected: the report names the region and the `cpu-*` platforms with presets; `var/cloud-discovery.json` contains the ids and the two `--help` texts. Read the PostgreSQL help text and note in the report's "To confirm" section (by hand, before committing) the minimum disk size and allowed platforms if the help text states them, and whether `cluster stop` exists (`nebius msp postgresql v1alpha1 cluster --help`). If the project's region is not `eu-north1`, set `WAIVE_CLOUD_PLATFORM=cpu-d3` (already the default); if it is `eu-north1`, `cpu-e2` is $0.006/h cheaper — keep `cpu-d3` unless the user prefers otherwise. If a listing failed (the report's last section), check `nebius <service> --help` for the right subcommand and fix `discover.py` plus the `ANSWERS` keys in the test before committing.

```bash
git add docs/reports/cloud-costs.md
git commit -m "docs: Nebius discovery and cost table for Phase 6 (6.3)"
```

- [ ] **Step 9: STOP for gate U6.1**

Do not continue to 6.4. Report to the orchestrator the report path and this message for the user, with the numbers filled in from the report:

> **Gate U6.1 — approve Nebius resources and costs.** The plan creates four things in project *<name>* (region *<region>*), all named `waive-*`: a Container Registry (free), a Managed PostgreSQL cluster `2vcpu-8gb` + 32 GiB (≈ $0.143/h, ≈ $3.44/day, billed until deleted), a SecretStash secret with the six runtime secrets (free), and a CPU Serverless AI endpoint `cpu-d3 2vcpu-8gb` on port 8000 with no endpoint auth (≈ $0.066/h while running, $0 stopped; the public HTTPS URL is managed by Nebius). Both running ≈ $0.21/h; the $30 Phase 6 budget covers ≈ 143 hours. Please answer: (1) approve these resources; (2) keep the database between demo windows (≈ $3.44/day) or delete and recreate it per window; (3) confirm the endpoint runs only when you say so (`waive cloud start|stop`). Details: `docs/reports/cloud-costs.md`.

---

### Task 6.4: Container Registry and the first image push (gates U0.5 and U6.1)

**Files:**
- Create: `deploy/push.sh`
- Modify: `.env` (by hand: `WAIVE_REGISTRY=…`; never committed)

**Interfaces:**
- Consumes: `Settings.registry` (6.1), image `waive:dev` build recipe (6.1).
- Produces: registry `waive-registry`; image reference `cr.<region>.nebius.cloud/<registry path>/waive:<short git sha>`; `deploy/push.sh [tag]` builds for `linux/amd64`, pushes and verifies.

- [ ] **Step 1: Write the push script**

`deploy/push.sh`:

```bash
#!/usr/bin/env bash
# Build the production image for x86_64 (the Mac builds arm64 by default) and push it to the
# project's Container Registry. Usage: deploy/push.sh [tag]  — default tag: short git SHA.
# Needs: gate U6.1, a nebius profile, `nebius registry configure-helper` run once, and
# WAIVE_REGISTRY=cr.<region>.nebius.cloud/<registry path> in .env (task 6.4 step 3).
set -euo pipefail
cd "$(dirname "$0")/.."
TAG="${1:-$(git rev-parse --short HEAD)}"
REGISTRY="$(uv run python -c 'from waive.config import Settings; print(Settings().registry or "")')"
if [ -z "$REGISTRY" ]; then
  echo "WAIVE_REGISTRY is not set in .env (see task 6.4 step 3)" >&2
  exit 1
fi
IMAGE="$REGISTRY/waive:$TAG"
docker build --platform linux/amd64 -t "waive:$TAG" -t "$IMAGE" .
docker push "$IMAGE"
docker manifest inspect "$IMAGE" >/dev/null
echo "pushed $IMAGE"
```

Run: `chmod +x deploy/push.sh && bash -n deploy/push.sh && deploy/push.sh`
Expected: `bash -n` prints nothing (syntax OK); the run exits 1 with `WAIVE_REGISTRY is not set in .env` — the script's own precondition check, before anything cloud-side exists.

- [ ] **Step 2: Create the registry (gate U6.1 — first state-changing cloud command)**

```bash
nebius registry create --parent-id "$PROJECT_ID" --name waive-registry --format json
```

Verify:

```bash
nebius registry list --parent-id "$PROJECT_ID" --format json | jq -r '.items[] | "\(.metadata.name) \(.metadata.id)"'
```

Expected: one line `waive-registry registry-…`. If `.items` is not the key, print the whole output (`| jq .`) and read the id from it.

- [ ] **Step 3: Record the registry path and configure Docker**

```bash
REGISTRY_ID="$(nebius registry list --parent-id "$PROJECT_ID" --format json | jq -r '.items[] | select(.metadata.name=="waive-registry") | .metadata.id')"
REGISTRY_PATH="$(printf '%s' "$REGISTRY_ID" | cut -d- -f2)"   # as the quickstart derives it
printf 'WAIVE_REGISTRY=cr.%s.nebius.cloud/%s\n' "$REGION_ID" "$REGISTRY_PATH" >> .env
nebius registry configure-helper
```

Verify:

```bash
uv run python -c 'from waive.config import Settings; print(Settings().registry)'
grep -c 'nebius' ~/.docker/config.json
```

Expected: `cr.<region>.nebius.cloud/<path>`; a count ≥ 1 (the credential helper is registered, so no `docker login` is needed). Confirm the path against the image path shown on the registry's page in the console; if the console shows the full id (`registry-…`) as the path, set `WAIVE_REGISTRY` to that instead. Fallback if the helper does not work: `nebius iam get-access-token | docker login "cr.$REGION_ID.nebius.cloud" --username iam --password-stdin` (token valid 12 hours).

- [ ] **Step 4: Build for amd64 and push**

The Mac builds arm64 by default (6.1b's local `waive:dev` is arm64: `docker image inspect waive:dev --format '{{.Architecture}}'`), and the endpoint platform `cpu-d3` is x86_64, so an image pushed without `--platform linux/amd64` would fail at exec time. Build the amd64 image once and smoke-test it under emulation before pushing; the script's own `--platform linux/amd64` build then hits the cache:

```bash
docker build --platform linux/amd64 -t waive:amd64 .
docker image inspect waive:amd64 --format '{{.Architecture}}'   # must print amd64
docker run --rm -d --name waive-smoke-amd64 --platform linux/amd64 -p 8000:8000 \
  -e WAIVE_VAULT_KEY="$(uv run python -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())')" \
  -e WAIVE_TOKEN_SECRET="$(uv run python -c 'import secrets; print(secrets.token_hex(32))')" \
  waive:amd64
for _ in $(seq 1 20); do curl -fsS http://127.0.0.1:8000/healthz && break; sleep 2; done
docker rm -f waive-smoke-amd64
deploy/push.sh
```

Expected: `amd64`; `{"ok":true}` (emulated startup is slower than native, hence the longer wait). If the emulated container never answers, do not push; check `docker logs waive-smoke-amd64` first.

Verify (the script already runs `docker manifest inspect`):

```bash
TAG="$(git rev-parse --short HEAD)"
docker manifest inspect "$(uv run python -c 'from waive.config import Settings; print(Settings().registry)')/waive:$TAG" | jq '.schemaVersion, (.config.digest // .manifests[0].digest)'
```

Expected: the amd64 build takes several minutes the first time (emulated `uv sync`); the push shows layers uploaded; `manifest inspect` prints a schema version and a digest. If `docker push` fails with `unauthorized`, re-run `nebius registry configure-helper` (it must run as your user, not root) or use the `docker login` fallback above.

- [ ] **Step 5: Commit the script**

```bash
git add deploy/push.sh
git commit -m "chore: amd64 image build and push script for Nebius Container Registry (6.4)"
```

Expected: `git status` shows `.env` is not staged (it is gitignored). Record for PROGRESS: the registry name, the image tag, and that no money was spent (the registry is free).

---

### Task 6.5: Managed PostgreSQL, schema upgrade and atlas copy (gates U0.5 and U6.1)

**Files:**
- Modify: `src/waive/db.py` (add `copy_tables`), `src/waive/cli.py` (add `waive db copy`), `.gitignore` (add `/certs/`), `.env` (by hand: `WAIVE_CLOUD_DATABASE_URL=…`; never committed)
- Test: `tests/unit/test_db_copy.py`

**Interfaces:**
- Consumes: `db.Base.metadata.sorted_tables`, `init_db`, `make_engine`; `Settings.cloud_database_url` (6.1).
- Produces: `db.copy_tables(source: Engine, target: Engine, *, skip: Iterable[str] = ("cases",)) -> dict[str, int]` (rows read per table; inserts ignore primary-key duplicates; PostgreSQL sequences are moved past the copied ids); `waive db copy [--to <url> | --to-cloud] [--include-cases]`; cluster `waive-db` with database `waive`, user `waive`; `WAIVE_CLOUD_DATABASE_URL=postgresql+psycopg://waive:<password>@<host>:5432/waive?sslmode=verify-full&sslrootcert=certs/msp-ca.pem` (the relative CA path resolves to `/app/certs/msp-ca.pem` in the container and `./certs/msp-ca.pem` from the repo root).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_db_copy.py`:

```python
"""`copy_tables` moves the atlas and learning tables into a fresh database and leaves cases behind."""

from sqlalchemy import func, select

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import SAMPLE_POLICY_TEXT, st_example_sheet
from waive.db import (
    CaseRow,
    HospitalRow,
    SheetRow,
    copy_tables,
    init_db,
    make_engine,
    session_scope,
)

HOSPITAL = {
    "ccn": "229999",
    "name": "ST. EXAMPLE MEDICAL CENTER",
    "city": "BOSTON",
    "state": "MA",
    "zip": "02118",
    "phone": "617-555-0100",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
    "website_domain": "example.org",
}


def seeded_source():
    source = make_engine("sqlite+pysqlite:///:memory:")
    init_db(source)
    with session_scope(source) as session:
        repo.upsert_hospital(session, HOSPITAL)
        sheet = st_example_sheet()
        publish_sheet(session, sheet)
        repo.save_source(session, sheet.sources[0], SAMPLE_POLICY_TEXT, "229999")
        session.add(CaseRow(id="abc", state="MA", status="new", token_generation=1))
    return source


def count(engine, model):
    with session_scope(engine) as session:
        return session.scalar(select(func.count()).select_from(model))


def test_copy_moves_atlas_rows_and_skips_cases_and_is_idempotent():
    source, target = seeded_source(), make_engine("sqlite+pysqlite:///:memory:")
    init_db(target)
    counts = copy_tables(source, target)
    assert counts["hospitals"] == 1 and counts["sheets"] == 1
    assert counts["source_docs"] == 1 and counts["hospital_sources"] == 1
    assert "cases" not in counts
    with session_scope(target) as session:
        assert session.get(HospitalRow, "229999").name == HOSPITAL["name"]
        assert session.get(CaseRow, "abc") is None
        sheet = session.scalar(select(SheetRow))
        assert sheet.version == 1 and sheet.body["hospital"]["ccn"] == "229999"
    copy_tables(source, target)  # a second run inserts nothing new
    assert count(target, HospitalRow) == 1 and count(target, SheetRow) == 1


def test_copy_can_include_cases_when_asked():
    source, target = seeded_source(), make_engine("sqlite+pysqlite:///:memory:")
    init_db(target)
    counts = copy_tables(source, target, skip=())
    assert counts["cases"] == 1
    assert count(target, CaseRow) == 1
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_db_copy.py -v`
Expected: FAIL with `ImportError: cannot import name 'copy_tables' from 'waive.db'`.

- [ ] **Step 3: Implement**

Add to `src/waive/db.py` (imports: add `from collections.abc import Iterable`, `from sqlalchemy import select`, `from sqlalchemy.dialects.postgresql import insert as pg_insert`, `from sqlalchemy.dialects.sqlite import insert as sqlite_insert`, `from sqlalchemy.engine import Connection`), after `init_db`:

```python
def _insert_ignoring_duplicates(table: Table, dialect_name: str):
    if dialect_name == "postgresql":
        return pg_insert(table).on_conflict_do_nothing()
    if dialect_name == "sqlite":
        return sqlite_insert(table).on_conflict_do_nothing()
    return table.insert()


def _bump_sequence(connection: Connection, table: Table) -> None:
    """After inserting rows with explicit ids, move the PostgreSQL sequence past them so the next
    autoincrement does not collide. No-op elsewhere and for non-integer keys."""
    if connection.dialect.name != "postgresql":
        return
    keys = list(table.primary_key.columns)
    if len(keys) != 1 or not isinstance(keys[0].type, Integer):
        return
    name, column = table.name, keys[0].name  # our own metadata, not user input
    connection.execute(
        text(
            f"SELECT setval(pg_get_serial_sequence('{name}', '{column}'), "
            f"COALESCE((SELECT MAX({column}) FROM {name}), 0) + 1, false)"
        )
    )


def copy_tables(
    source: Engine, target: Engine, *, skip: Iterable[str] = ("cases",)
) -> dict[str, int]:
    """Copy every table's rows from `source` into `target`, which must already have the schema
    (`init_db(target)`). Tables in `skip` are left out — `cases` by default, because personal data
    does not move between databases this way. Rows whose primary key already exists in the target
    are not inserted again. Returns the number of rows read per copied table."""
    skipped = set(skip)
    counts: dict[str, int] = {}
    with source.connect() as read, target.begin() as write:
        for table in Base.metadata.sorted_tables:
            if table.name in skipped:
                continue
            rows = [dict(row) for row in read.execute(select(table)).mappings()]
            if rows:
                write.execute(_insert_ignoring_duplicates(table, write.dialect.name), rows)
            counts[table.name] = len(rows)
            _bump_sequence(write, table)
    return counts
```

Add to `src/waive/cli.py` after `db_upgrade` (import `copy_tables` from `waive.db`):

```python
@db_app.command("copy")
def db_copy(
    to: str | None = typer.Option(
        None, "--to", help="Target SQLAlchemy URL (it holds the password; prefer --to-cloud)"
    ),
    to_cloud: bool = typer.Option(
        False, "--to-cloud", help="Target is WAIVE_CLOUD_DATABASE_URL from .env"
    ),
    include_cases: bool = typer.Option(
        False, "--include-cases", help="Also copy cases (personal data) — never to a shared database"
    ),
) -> None:
    """Copy the local database (atlas, sources, learning tables) into another database.
    The target schema is created or upgraded first. Cases stay behind unless --include-cases."""
    settings = Settings()
    if to_cloud:
        if settings.cloud_database_url is None or not settings.cloud_database_url.get_secret_value():
            console.print("WAIVE_CLOUD_DATABASE_URL is not set in .env (task 6.5)")
            raise typer.Exit(code=2)
        to = settings.cloud_database_url.get_secret_value()
    if not to:
        raise typer.BadParameter("give --to <url> or --to-cloud")
    target = make_engine(to)
    init_db(target)
    counts = copy_tables(_engine(settings), target, skip=() if include_cases else ("cases",))
    for name, rows in counts.items():
        console.print(f"{name}: {rows} rows")
    console.print("Done. The target URL was not printed.")
```

Add `/certs/` to `.gitignore` under "Local data and artifacts".

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_db_copy.py -v`
Expected: 2 passed. If the first test fails on `sheet.body["hospital"]["ccn"]`, inspect `st_example_sheet().model_dump(mode="json")` and use the key the sample sheet actually has for the CCN (the assertion exists only to prove JSON columns survive the copy).

- [ ] **Step 5: Rehearse on a local PostgreSQL 16 in Docker (free; no gate)**

This proves the schema, JSON columns, `VARCHAR` lengths (SQLite ignores them, PostgreSQL enforces them) and the production image against real PostgreSQL before anything is paid for.

```bash
docker run -d --name waive-pg-local -e POSTGRES_USER=waive -e POSTGRES_PASSWORD=localpw -e POSTGRES_DB=waive -p 5433:5432 postgres:16
for _ in 1 2 3 4 5 6 7 8 9 10; do docker exec waive-pg-local pg_isready -U waive && break; sleep 1; done
LOCAL_PG="postgresql+psycopg://waive:localpw@127.0.0.1:5433/waive"
WAIVE_DATABASE_URL="$LOCAL_PG" uv run waive db upgrade
uv run waive db copy --to "$LOCAL_PG"
WAIVE_DATABASE_URL="$LOCAL_PG" uv run waive atlas report --state MA --out /tmp/pg-report.md && head -12 /tmp/pg-report.md
docker run --rm -d --name waive-smoke -p 8000:8000 \
  -e WAIVE_ENV=production -e WAIVE_LEDGER_BACKEND=db \
  -e WAIVE_DATABASE_URL="postgresql+psycopg://waive:localpw@host.docker.internal:5433/waive" \
  -e WAIVE_VAULT_KEY="$(uv run python -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())')" \
  -e WAIVE_TOKEN_SECRET="$(uv run python -c 'import secrets; print(secrets.token_hex(32))')" \
  waive:dev
for _ in 1 2 3 4 5 6 7 8 9 10; do curl -fsS http://127.0.0.1:8000/healthz && break; sleep 1; done
curl -fsS http://127.0.0.1:8000/atlas | grep -c 'href="/atlas/'
docker rm -f waive-smoke waive-pg-local
```

Expected: `db upgrade` prints `Schema is up to date.` (fresh tables), `db copy` lists every table with its row count (`hospitals: 46 rows`, `sheets: …`, `cases` absent, `usage_events: 0 rows`), the report's first lines match `docs/reports/atlas-ma.md` (same published count), `{"ok":true}`, and the atlas page lists ≥ 20 hospital links. If `db copy` fails with `value too long for type character varying(N)`, widen that column in `src/waive/db.py` to `Text` (the cloud database will be created fresh, so `create_all` picks the change up; locally SQLite is unaffected), add a note to the commit message, and re-run from `docker rm -f waive-pg-local`. Naive `created_at` values from SQLite are stored as UTC by the PostgreSQL session default; that is the intended meaning.

- [ ] **Step 6: Lint, format, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pytest
git add .gitignore src/waive/db.py src/waive/cli.py tests/unit/test_db_copy.py
git commit -m "feat: waive db copy — move the atlas into another database, cases excluded (6.5)"
```

Expected: 267 tests pass.

- [ ] **Step 7: Create the cluster (gate U6.1 — this starts billing at ≈ $0.143/h)**

Use the platform and disk size from `docs/reports/cloud-costs.md` ("To confirm on the day"): `cpu-e2` in `eu-north1`, otherwise `cpu-d3`; disk 32 GiB unless the help text names a larger minimum.

```bash
NETWORK_ID="$(nebius vpc network list --parent-id "$PROJECT_ID" --format json | jq -r '.items[0].metadata.id')"
PG_PASSWORD="$(uv run python -c 'import secrets; print("Wv" + secrets.token_urlsafe(24) + "!")')"
nebius msp postgresql v1alpha1 cluster create \
  --parent-id "$PROJECT_ID" \
  --name waive-db \
  --description "Waive production database (Phase 6)" \
  --network-id "$NETWORK_ID" \
  --bootstrap-db-name waive \
  --bootstrap-user-name waive \
  --bootstrap-user-password "$PG_PASSWORD" \
  --config-version 16 \
  --config-template-disk-type network-ssd \
  --config-template-disk-size-gibibytes 32 \
  --config-template-resources-platform cpu-e2 \
  --config-template-resources-preset 2vcpu-8gb \
  --config-template-hosts-count 1 \
  --config-public-access \
  --format json
```

Verify (repeat until the endpoint is filled; creation takes a few minutes):

```bash
nebius msp postgresql v1alpha1 cluster get-by-name --parent-id "$PROJECT_ID" --name waive-db --format json | jq '.status'
```

Expected: a status with `connection_endpoints.public_read_write` set to a host name. If `create` rejects the disk size or platform, use the value its message names and re-run (nothing was created on a rejected call; confirm with `cluster list`). The password is only in `$PG_PASSWORD` in this shell and, next, in `.env`.

- [ ] **Step 8: Record the connection string, download the CA, upgrade and copy**

```bash
PG_HOST="$(nebius msp postgresql v1alpha1 cluster get-by-name --parent-id "$PROJECT_ID" --name waive-db --format jsonpath='{.status.connection_endpoints.public_read_write}')"
mkdir -p certs && curl -fsS "https://storage.eu-north1.nebius.cloud/msp-certs/ca.pem" -o certs/msp-ca.pem
printf 'WAIVE_CLOUD_DATABASE_URL=postgresql+psycopg://waive:%s@%s:5432/waive?sslmode=verify-full&sslrootcert=certs/msp-ca.pem\n' "$PG_PASSWORD" "$PG_HOST" >> .env
unset PG_PASSWORD
CLOUD_DB="$(uv run python -c 'from waive.config import Settings; print(Settings().cloud_database_url.get_secret_value())')"
WAIVE_DATABASE_URL="$CLOUD_DB" uv run waive db upgrade
uv run waive db copy --to-cloud
WAIVE_DATABASE_URL="$CLOUD_DB" uv run waive atlas report --state MA --out /tmp/cloud-report.md && head -12 /tmp/cloud-report.md
unset CLOUD_DB
```

Verify: the report from the cloud database shows the same hospital and published counts as `docs/reports/atlas-ma.md`; `git status` shows no change to tracked files (`.env` and `certs/` are ignored). If `db upgrade` fails with a certificate error, check that `certs/msp-ca.pem` is a PEM file (`head -1` prints `-----BEGIN CERTIFICATE-----`) and that the URL has `sslmode=verify-full`; if it fails with `password authentication failed`, the `!` or another character in the URL was altered by the shell — reset the password with `nebius msp postgresql v1alpha1 cluster update --id <cluster id> --parent-id "$PROJECT_ID" --bootstrap-user-password "<new>"` and rewrite the `.env` line. Record for PROGRESS: cluster name, preset, disk, the hourly rate, and the creation time (billing started).

---

### Task 6.6: SecretStash secret and the Serverless AI endpoint (gates U0.5 and U6.1)

**Files:**
- Create: `src/waive/cloud/deploy.py`
- Modify: `src/waive/cloud/cli.py` (add `secrets push` and `deploy`)
- Test: `tests/unit/test_cloud_deploy.py`

**Interfaces:**
- Consumes: `Nebius`, `id_of`, `items`, `name_of` (6.3); `Settings.cloud_database_url / cloud_platform / cloud_preset / cloud_endpoint_disk_gib / tavily_credit_cap / token_factory_usd_cap` (6.1); `FakeRunner` from `tests/unit/test_cloud_discover.py`.
- Produces: `deploy.SECRET_NAME = "waive-secrets"`, `deploy.ENDPOINT_NAME = "waive-web"`, `deploy.SECRET_KEYS` (the six variable names, in order), `deploy.secret_payload(settings) -> list[dict[str, str]]` (raises `ValueError` naming missing keys), `deploy.push_secrets(nebius, settings) -> str` (secret id or `""`), `deploy.plain_env(settings) -> dict[str, str]`, `deploy.endpoint_create_args(settings, *, image, subnet_id=None, secret=SECRET_NAME, registry_key=None) -> list[str]`, `deploy.find_endpoint(nebius, name=ENDPOINT_NAME) -> dict | None`, `deploy.endpoint_url(endpoint) -> str | None`; CLI `waive cloud secrets push [--yes]`, `waive cloud deploy --image <ref> [--subnet-id] [--secret] [--registry-key-env NAME] [--yes]`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_cloud_deploy.py`:

```python
"""Composing the SecretStash secret and the endpoint create command from .env, without printing
secret values. Every nebius call is answered by FakeRunner."""

import json

import pytest
from pydantic import SecretStr

from waive.cloud.deploy import (
    ENDPOINT_NAME,
    SECRET_KEYS,
    SECRET_NAME,
    endpoint_create_args,
    endpoint_url,
    find_endpoint,
    push_secrets,
    secret_payload,
)
from waive.cloud.nebius import Nebius, NebiusError
from waive.config import Settings

from tests.unit.test_cloud_discover import FakeRunner

VALUES = {
    "nebius_api_key": SecretStr("tf-key-value"),
    "tavily_api_key": SecretStr("tv-key-value"),
    "vault_key": SecretStr("vault-key-value"),
    "token_secret": SecretStr("token-secret-value"),
    "admin_token": SecretStr("admin-token-value"),
    "cloud_database_url": SecretStr("postgresql+psycopg://waive:pw@db.example.net:5432/waive"),
}


def settings(**overrides):
    return Settings(_env_file=None, **{**VALUES, **overrides})


def test_secret_payload_holds_the_six_keys_and_names_missing_ones():
    payload = secret_payload(settings())
    assert [entry["key"] for entry in payload] == list(SECRET_KEYS)
    assert SECRET_KEYS == (
        "NEBIUS_API_KEY", "TAVILY_API_KEY", "WAIVE_VAULT_KEY",
        "WAIVE_TOKEN_SECRET", "WAIVE_ADMIN_TOKEN", "WAIVE_DATABASE_URL",
    )
    assert payload[5]["string_value"].startswith("postgresql+psycopg://")
    with pytest.raises(ValueError, match="WAIVE_ADMIN_TOKEN, WAIVE_DATABASE_URL"):
        secret_payload(settings(admin_token=None, cloud_database_url=SecretStr("")))


def test_push_secrets_sends_one_secret_and_withholds_values_from_errors():
    runner = FakeRunner({"mysterybox secret create": {"metadata": {"id": "mbsec-42", "name": SECRET_NAME}}})
    assert push_secrets(Nebius("project-test", runner=runner), settings()) == "mbsec-42"
    call = runner.calls[0]
    assert call[:4] == ["nebius", "mysterybox", "secret", "create"]
    assert call[call.index("--name") + 1] == SECRET_NAME
    payload = json.loads(call[call.index("--secret-version-payload") + 1])
    assert {entry["key"] for entry in payload} == set(SECRET_KEYS)
    assert call[-4:] == ["--parent-id", "project-test", "--format", "json"]

    failing = FakeRunner({"mysterybox secret create": RuntimeError("echo tf-key-value")})
    with pytest.raises(NebiusError) as error:
        push_secrets(Nebius("project-test", runner=failing), settings())
    assert "tf-key-value" not in str(error.value)


def test_endpoint_create_args_match_the_documented_flags():
    args = endpoint_create_args(settings(), image="cr.eu-north1.nebius.cloud/e00abc/waive:1a2b3c4")
    assert args[:3] == ["ai", "endpoint", "create"]
    assert args[args.index("--name") + 1] == ENDPOINT_NAME
    assert args[args.index("--platform") + 1] == "cpu-d3"
    assert args[args.index("--preset") + 1] == "2vcpu-8gb"
    assert args[args.index("--container-port") + 1] == "8000"
    assert args[args.index("--disk-size") + 1] == "32Gi"  # never the CLI default 250Gi (6.3 report)
    env = args[args.index("--env") + 1]
    assert "WAIVE_ENV=production" in env and "WAIVE_LEDGER_BACKEND=db" in env
    assert "WAIVE_REQUIRE_ZDR=true" in env and "WAIVE_ZDR_CONFIRMED=false" in env
    assert "WAIVE_TAVILY_CREDIT_CAP=1000" in env and "WAIVE_TOKEN_FACTORY_USD_CAP=15" in env
    secrets = [args[i + 1] for i, token in enumerate(args) if token == "--env-secret"]
    assert secrets == [f"{key}={SECRET_NAME}" for key in SECRET_KEYS]
    assert "--auth" not in args and "--public" not in args and "--subnet-id" not in args
    assert "tf-key-value" not in " ".join(args)

    with_extras = endpoint_create_args(
        settings(), image="img:1", subnet_id="vpcsubnet-e00def", registry_key="static-key-value"
    )
    assert with_extras[with_extras.index("--subnet-id") + 1] == "vpcsubnet-e00def"
    assert with_extras[with_extras.index("--registry-username") + 1] == "iam"
    assert with_extras[with_extras.index("--registry-password") + 1] == "static-key-value"


def test_find_endpoint_and_url_pick_the_https_address():
    listing = {"items": [
        {"metadata": {"name": "other-app", "id": "ep-0"}, "status": {"public_endpoints": ["https://o.example"]}},
        {"metadata": {"name": ENDPOINT_NAME, "id": "ep-1"},
         "status": {"state": "RUNNING", "public_endpoints": ["http://10.0.0.5:8000", "https://waive-web-x.serverless.example"]}},
    ]}
    nebius = Nebius("project-test", runner=FakeRunner({"ai endpoint list": listing}))
    endpoint = find_endpoint(nebius)
    assert endpoint["metadata"]["id"] == "ep-1"
    assert endpoint_url(endpoint) == "https://waive-web-x.serverless.example"
    assert endpoint_url({"status": {}}) is None
    assert find_endpoint(Nebius("project-test", runner=FakeRunner({"ai endpoint list": {"items": []}}))) is None
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_cloud_deploy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.cloud.deploy'`.

- [ ] **Step 3: Implement**

`src/waive/cloud/deploy.py`:

```python
"""Compose the SecretStash secret and the Serverless AI endpoint from `.env` (spec §14).

Documented flags (read 2026-10-02): `nebius mysterybox secret create --name --description
--secret-version-payload '[{"key":…,"string_value":…}]'`
(https://docs.nebius.com/mysterybox/secrets/create.md); `nebius ai endpoint create --name --image
--platform --preset --disk-size <n>Gi --container-port --env "K=V,…" --env-secret KEY=<secret selector>
[--subnet-id] [--registry-username/--registry-password]`; `--auth` omitted means no
authentication — the app enforces its own capability links
(https://docs.nebius.com/serverless/endpoints/manage.md). The payload key of the secret must equal
the environment variable name, so every variable gets its own `--env-secret KEY=waive-secrets`."""

import json
from typing import Any

from pydantic import SecretStr

from waive.cloud.nebius import Nebius, id_of, items, name_of
from waive.config import Settings

SECRET_NAME = "waive-secrets"
ENDPOINT_NAME = "waive-web"
SECRET_KEYS = (
    "NEBIUS_API_KEY",
    "TAVILY_API_KEY",
    "WAIVE_VAULT_KEY",
    "WAIVE_TOKEN_SECRET",
    "WAIVE_ADMIN_TOKEN",
    "WAIVE_DATABASE_URL",
)
CONTAINER_PORT = 8000


def _secret_values(settings: Settings) -> dict[str, SecretStr | None]:
    return {
        "NEBIUS_API_KEY": settings.nebius_api_key,
        "TAVILY_API_KEY": settings.tavily_api_key,
        "WAIVE_VAULT_KEY": settings.vault_key,
        "WAIVE_TOKEN_SECRET": settings.token_secret,
        "WAIVE_ADMIN_TOKEN": settings.admin_token,
        "WAIVE_DATABASE_URL": settings.cloud_database_url,
    }


def secret_payload(settings: Settings) -> list[dict[str, str]]:
    values = _secret_values(settings)
    missing = [key for key in SECRET_KEYS if values[key] is None or not values[key].get_secret_value()]
    if missing:
        raise ValueError(f"missing in .env: {', '.join(missing)}")
    return [{"key": key, "string_value": values[key].get_secret_value()} for key in SECRET_KEYS]


def push_secrets(nebius: Nebius, settings: Settings) -> str:
    """Create the secret with one version holding all six keys. Returns the secret id when the CLI
    prints the resource; `""` otherwise (then `nebius mysterybox secret list` shows it)."""
    created = nebius.run(
        "mysterybox",
        "secret",
        "create",
        "--name",
        SECRET_NAME,
        "--description",
        "Waive runtime secrets (Phase 6)",
        "--secret-version-payload",
        json.dumps(secret_payload(settings)),
        redact=True,
    )
    return id_of(created) if isinstance(created, dict) else ""


def plain_env(settings: Settings) -> dict[str, str]:
    """Non-secret environment for the container. ZDR stays unconfirmed until gate U0.4."""
    return {
        "WAIVE_ENV": "production",
        "WAIVE_LEDGER_BACKEND": "db",
        "WAIVE_REQUIRE_ZDR": "true",
        "WAIVE_ZDR_CONFIRMED": "false",
        "WAIVE_TAVILY_CREDIT_CAP": str(settings.tavily_credit_cap),
        "WAIVE_TOKEN_FACTORY_USD_CAP": str(settings.token_factory_usd_cap),
    }


def endpoint_create_args(
    settings: Settings,
    *,
    image: str,
    subnet_id: str | None = None,
    secret: str = SECRET_NAME,
    registry_key: str | None = None,
) -> list[str]:
    env = ",".join(f"{key}={value}" for key, value in plain_env(settings).items())
    args = [
        "ai", "endpoint", "create",
        "--name", ENDPOINT_NAME,
        "--image", image,
        "--platform", settings.cloud_platform,
        "--preset", settings.cloud_preset,
        "--container-port", str(CONTAINER_PORT),
        # Explicit: the CLI default is 250Gi, billed at Compute disk prices while running.
        "--disk-size", f"{settings.cloud_endpoint_disk_gib}Gi",
        "--env", env,
    ]
    for key in SECRET_KEYS:
        args += ["--env-secret", f"{key}={secret}"]
    if subnet_id:
        args += ["--subnet-id", subnet_id]
    if registry_key:
        args += ["--registry-username", "iam", "--registry-password", registry_key]
    return args


def find_endpoint(nebius: Nebius, name: str = ENDPOINT_NAME) -> dict[str, Any] | None:
    for endpoint in items(nebius.run("ai", "endpoint", "list")):
        if name_of(endpoint) == name:
            return endpoint
    return None


def endpoint_url(endpoint: dict[str, Any]) -> str | None:
    for address in endpoint.get("status", {}).get("public_endpoints", []) or []:
        if isinstance(address, str) and address.startswith("https://"):
            return address
    return None
```

Add to `src/waive/cloud/cli.py` (imports: `import os`, `from waive.cloud.deploy import SECRET_NAME, endpoint_create_args, id_of, push_secrets, secret_payload` — `id_of` from `waive.cloud.nebius`):

```python
secrets_app = typer.Typer(no_args_is_help=True, help="SecretStash secrets.")
cloud_app.add_typer(secrets_app, name="secrets")


@secrets_app.command("push")
def cloud_secrets_push(yes: bool = typer.Option(False, "--yes", help="Create without asking")) -> None:
    """Create the SecretStash secret `waive-secrets` from the six values in .env (gate U6.1).
    Prints key names only, never values."""
    settings = Settings()
    nebius = _nebius(settings)
    try:
        payload = secret_payload(settings)
    except ValueError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=2) from None
    console.print("Keys: " + ", ".join(entry["key"] for entry in payload))
    if not yes and not typer.confirm(f"Create secret {SECRET_NAME} in the project now?"):
        raise typer.Exit(code=1)
    secret_id = push_secrets(nebius, settings)
    console.print(f"Created {SECRET_NAME} ({secret_id or 'id not in the output'}); values not printed.")


@cloud_app.command("deploy")
def cloud_deploy(
    image: str = typer.Option(..., "--image", help="cr.<region>.nebius.cloud/<path>/waive:<tag>"),
    subnet_id: str | None = typer.Option(None, "--subnet-id", help="Only if the project has several subnets"),
    secret: str = typer.Option(SECRET_NAME, "--secret", help="SecretStash secret with the six keys"),
    registry_key_env: str | None = typer.Option(
        None, "--registry-key-env", help="Env var holding a registry static key (only if the pull fails)"
    ),
    yes: bool = typer.Option(False, "--yes", help="Create without asking"),
) -> None:
    """Create the public Serverless AI endpoint `waive-web` (gate U6.1; bills while running)."""
    settings = Settings()
    nebius = _nebius(settings)
    registry_key = os.environ.get(registry_key_env, "") if registry_key_env else None
    if registry_key_env and not registry_key:
        console.print(f"[red]{registry_key_env} is empty in this shell[/red]")
        raise typer.Exit(code=2)
    args = endpoint_create_args(
        settings, image=image, subnet_id=subnet_id, secret=secret, registry_key=registry_key or None
    )
    shown = ["***" if args[i - 1] == "--registry-password" else a for i, a in enumerate(args)]
    console.print("nebius " + " ".join(shown) + f" --parent-id <project> --format json")
    if not yes and not typer.confirm("Create this endpoint now? It bills while running."):
        raise typer.Exit(code=1)
    created = nebius.run(*args, redact=registry_key is not None)
    endpoint_id = id_of(created) if isinstance(created, dict) else ""
    console.print(
        f"Create requested for waive-web ({endpoint_id or 'id not in the output'}). "
        "Run `uv run waive cloud status` in a minute for the URL."
    )
```

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_cloud_deploy.py tests/unit/test_cloud_discover.py -v`
Expected: 8 passed.

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pytest
git add src/waive/cloud/deploy.py src/waive/cloud/cli.py tests/unit/test_cloud_deploy.py
git commit -m "feat: waive cloud secrets push and deploy compose the SecretStash and endpoint commands (6.6)"
```

Expected: 271 tests pass.

- [ ] **Step 6: Create the secret (gate U6.1)**

```bash
uv run waive cloud secrets push
```

Verify:

```bash
nebius mysterybox secret list --parent-id "$PROJECT_ID" --format json | jq -r '.items[] | "\(.metadata.name) \(.metadata.id)"'
```

Expected: `Keys: NEBIUS_API_KEY, TAVILY_API_KEY, WAIVE_VAULT_KEY, WAIVE_TOKEN_SECRET, WAIVE_ADMIN_TOKEN, WAIVE_DATABASE_URL`, a confirmation prompt, then `Created waive-secrets (mbsec-…)`; the listing shows `waive-secrets`. The values pass through the `nebius` process's argument list for the duration of the call (the CLI has no documented stdin or file input for the payload); they are not echoed and not in the shell history because `waive cloud` builds the command. If the secret already exists (a re-run), add a version instead: `nebius mysterybox secret-version create --help` shows the flags — the payload format is the same JSON array.

- [ ] **Step 7: Create the endpoint (gate U6.1 — bills ≈ $0.066/h while running)**

```bash
TAG="$(git rev-parse --short HEAD)"      # the tag pushed in 6.4; re-run deploy/push.sh first if the code changed since
IMAGE="$(uv run python -c 'from waive.config import Settings; print(Settings().registry)')/waive:$TAG"
uv run waive cloud deploy --image "$IMAGE"
```

Pass `--subnet-id <id from var/cloud-discovery.json>` only if `discover` listed more than one subnet (the CLI says so otherwise). Verify (repeat until the status is running; the first start pulls the image):

```bash
nebius ai endpoint list --parent-id "$PROJECT_ID" --format json | jq '.items[] | {name: .metadata.name, id: .metadata.id, status: .status}'
uv run waive cloud status
URL="$(nebius ai endpoint get "$(nebius ai endpoint list --parent-id "$PROJECT_ID" --format json | jq -r '.items[] | select(.metadata.name=="waive-web") | .metadata.id')" --format json | jq -r '.status.public_endpoints[] | select(startswith("https://"))' | head -1)"
curl -fsS "$URL/healthz"
```

Expected: `{"ok":true}` over HTTPS at the managed URL. Fallbacks, in order:

1. `--preset 2vcpu-8gb` rejected → set `WAIVE_CLOUD_PRESET=4vcpu-16gb` in `.env`, re-run `uv run waive cloud discover` (report now says $0.132/h), tell the user the new rate, and re-run `deploy`.
2. Status shows an image pull failure → in the console create a service account with a role that can pull from Container Registry, then `nebius iam static-key issue --help` to issue a key for it (up to 3 years), put the key into the shell only (`read -s REGISTRY_KEY; export REGISTRY_KEY`), delete the failed endpoint (`nebius ai endpoint delete --id <id>`; verify with `list`) and re-run `uv run waive cloud deploy --image "$IMAGE" --registry-key-env REGISTRY_KEY`.
3. The container starts but `/healthz` fails → the endpoint's log view in the console shows uvicorn's output; a `ValidationError` there means a secret or env name is misspelled (compare with `SECRET_KEYS`), `could not connect to server` means the database host or TLS settings — test the same URL locally with `WAIVE_DATABASE_URL="$(uv run python -c 'from waive.config import Settings; print(Settings().cloud_database_url.get_secret_value())')" uv run waive db upgrade`.
4. The endpoint cannot serve a plain web app at all (no HTTP route, request timeouts) → task 6.6b.

Record for PROGRESS: endpoint id, URL, preset, start time (billing started). Then, unless the smoke test (6.8) follows immediately, stop it: `uv run waive cloud stop`.

- [ ] **Step 6.6b (fallback only): one Compute VM with Docker Compose, PostgreSQL and Caddy**

Only if step 7's fallback 4 applies, or the user chose this at U6.1 for cost reasons (a stopped VM costs only its disk: 20 GiB × $0.071 = $1.42/month; running `cpu-d3 2vcpu-8gb` costs $0.066/h and replaces the managed cluster, so delete `waive-db` with `waive cloud cleanup` once the VM holds the data). HTTPS comes from Caddy with a Let's Encrypt certificate for the host name `<public-ip-with-dashes>.sslip.io` (sslip.io resolves such names to the ip; no DNS to buy).

Files to create: `deploy/vm/compose.yaml`, `deploy/vm/Caddyfile`, `deploy/vm/cloud-init.yaml`.

`deploy/vm/compose.yaml`:

```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: waive
      POSTGRES_DB: waive
      POSTGRES_PASSWORD: ${PG_PASSWORD}
    volumes: ["pgdata:/var/lib/postgresql/data"]
    ports: ["127.0.0.1:5432:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U waive"]
      interval: 5s
      retries: 20
    restart: unless-stopped
  app:
    image: ${WAIVE_IMAGE}
    env_file: .env
    environment:
      WAIVE_ENV: production
      WAIVE_LEDGER_BACKEND: db
      WAIVE_DATABASE_URL: postgresql+psycopg://waive:${PG_PASSWORD}@db:5432/waive
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped
  caddy:
    image: caddy:2
    ports: ["80:80", "443:443"]
    environment:
      WAIVE_DOMAIN: ${WAIVE_DOMAIN}
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
    depends_on: [app]
    restart: unless-stopped
volumes:
  pgdata: {}
  caddy_data: {}
```

`deploy/vm/Caddyfile`:

```
{$WAIVE_DOMAIN} {
    reverse_proxy app:8000
}
```

`deploy/vm/cloud-init.yaml` is generated so it carries your public key and nothing secret:

```bash
cat > deploy/vm/cloud-init.yaml <<EOF
#cloud-config
# Fallback host for Waive: Docker + Compose. The image is loaded over SSH; secrets are copied over
# SSH into /opt/waive/.env. Nothing secret is in this file.
package_update: true
packages: [docker.io, docker-compose-v2]
users:
  - name: ubuntu
    groups: [sudo, docker]
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - $(cat ~/.ssh/id_ed25519.pub)
runcmd:
  - systemctl enable --now docker
  - mkdir -p /opt/waive
  - chown ubuntu:ubuntu /opt/waive
EOF
grep -c "ssh-ed25519" deploy/vm/cloud-init.yaml   # 1
```

Create the VM (gate U6.1):

```bash
SUBNET_ID="$(nebius vpc subnet list --parent-id "$PROJECT_ID" --format json | jq -r '.items[0].metadata.id')"
nebius compute instance create --help | grep -i "user-data\|cloud-init"     # confirm the exact flag name below
nebius compute instance create \
  --parent-id "$PROJECT_ID" \
  --name waive-vm \
  --resources-platform cpu-d3 \
  --resources-preset 2vcpu-8gb \
  --boot-disk-managed-disk-name waive-vm-boot \
  --boot-disk-managed-disk-type network_ssd \
  --boot-disk-managed-disk-size-gibibytes 20 \
  --boot-disk-managed-disk-source-image-family-image-family ubuntu24.04-driverless \
  --boot-disk-attach-mode READ_WRITE \
  --network-interfaces "[{\"name\": \"eth0\", \"ip_address\": {}, \"public_ip_address\": {}, \"subnet_id\": \"$SUBNET_ID\"}]" \
  --cloud-init-user-data "$(cat deploy/vm/cloud-init.yaml)" \
  --format json
```

Verify and collect the address (field path per `jq .status`):

```bash
VM_ID="$(nebius compute instance list --parent-id "$PROJECT_ID" --format json | jq -r '.items[] | select(.metadata.name=="waive-vm") | .metadata.id')"
nebius compute instance get "$VM_ID" --format json | jq '.status'
VM_IP="$(nebius compute instance get "$VM_ID" --format json | jq -r '.. | .public_ip_address? // empty | .address? // empty' | head -1)"
ssh -o StrictHostKeyChecking=accept-new "ubuntu@$VM_IP" 'docker --version && docker compose version'
```

Ship the image, the files and the secrets, then start (nothing secret goes through argv; `.env` travels over SSH):

```bash
docker save "waive:$TAG" | gzip | ssh "ubuntu@$VM_IP" 'gunzip | docker load'
scp deploy/vm/compose.yaml deploy/vm/Caddyfile "ubuntu@$VM_IP:/opt/waive/"
uv run python - <<'EOF' | ssh "ubuntu@$VM_IP" 'umask 077 && cat > /opt/waive/.env'
import secrets
from waive.config import Settings
s = Settings()
lines = {
    "NEBIUS_API_KEY": s.nebius_api_key.get_secret_value(),
    "TAVILY_API_KEY": s.tavily_api_key.get_secret_value(),
    "WAIVE_VAULT_KEY": s.vault_key.get_secret_value(),
    "WAIVE_TOKEN_SECRET": s.token_secret.get_secret_value(),
    "WAIVE_ADMIN_TOKEN": s.admin_token.get_secret_value(),
    "WAIVE_REQUIRE_ZDR": "true",
    "WAIVE_ZDR_CONFIRMED": "false",
    "WAIVE_TAVILY_CREDIT_CAP": str(s.tavily_credit_cap),
    "WAIVE_TOKEN_FACTORY_USD_CAP": str(s.token_factory_usd_cap),
    "PG_PASSWORD": "Wv" + secrets.token_urlsafe(24),
}
print("\n".join(f"{k}={v}" for k, v in lines.items()))
EOF
ssh "ubuntu@$VM_IP" "cd /opt/waive && printf 'WAIVE_IMAGE=waive:$TAG\nWAIVE_DOMAIN=%s.sslip.io\n' \"\$(echo $VM_IP | tr . -)\" >> .env && docker compose up -d"
ssh -L 5433:127.0.0.1:5432 "ubuntu@$VM_IP" -N -f
VM_PG_PASSWORD="$(ssh "ubuntu@$VM_IP" "grep '^PG_PASSWORD=' /opt/waive/.env | cut -d= -f2-")"
uv run waive db copy --to "postgresql+psycopg://waive:${VM_PG_PASSWORD}@127.0.0.1:5433/waive"
unset VM_PG_PASSWORD
curl -fsS "https://$(echo "$VM_IP" | tr . -).sslip.io/healthz"
```

Expected: `{"ok":true}` over HTTPS within a minute (Caddy obtains the certificate on first request). Stop and start with `nebius compute instance stop "$VM_ID"` / `start "$VM_ID"` (verify with `get … | jq .status`); the compose stack restarts with the VM. Commit `deploy/vm/compose.yaml` and `deploy/vm/Caddyfile` only (`cloud-init.yaml` holds your public key — add `deploy/vm/cloud-init.yaml` to `.gitignore`).

---

### Task 6.7: `waive cloud start | stop | status | cleanup`

**Files:**
- Create: `src/waive/cloud/ops.py`
- Modify: `src/waive/cloud/cli.py` (four commands)
- Test: `tests/unit/test_cloud_ops.py`

**Interfaces:**
- Consumes: `Nebius`, `items`, `name_of`, `id_of` (6.3); `ENDPOINT_NAME`, `find_endpoint`, `endpoint_url` (6.6); `Footprint` (6.3); `Settings.cloud_*`, `Settings.zdr_confirmed` (6.1); `FakeRunner`.
- Produces: `ops.PREFIX = "waive-"`, `ops.Resource(kind, name, id, state)` (`kind` ∈ `endpoint | postgres | secret | registry`), `ops.LIST_COMMANDS`, `ops.DELETE_COMMANDS`, `ops.CLEANUP_ORDER = ("endpoint", "postgres", "secret", "registry")`, `ops.list_waive_resources(nebius) -> list[Resource]`, `ops.start(nebius) -> Resource`, `ops.stop(nebius) -> Resource` (raise `LookupError` when `waive-web` does not exist), `ops.CloudStatus(resources, url)`, `ops.status(nebius) -> CloudStatus`, `ops.render_status(status, settings) -> str`, `ops.delete_resource(nebius, resource)` (raises `ValueError` for any name outside the prefix), `ops.cleanup(nebius, confirm: Callable[[Resource], bool]) -> list[Resource]`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_cloud_ops.py`:

```python
"""start/stop/status/cleanup touch only resources named waive-*; every CLI call is faked."""

import pytest
from pydantic import SecretStr

from waive.cloud.nebius import Nebius
from waive.cloud.ops import (
    CLEANUP_ORDER,
    Resource,
    cleanup,
    delete_resource,
    list_waive_resources,
    render_status,
    start,
    status,
    stop,
)
from waive.config import Settings

from tests.unit.test_cloud_discover import FakeRunner

LISTINGS = {
    "ai endpoint list": {"items": [
        {"metadata": {"name": "waive-web", "id": "ep-1"},
         "status": {"state": "RUNNING", "public_endpoints": ["https://waive-web-x.serverless.example"]}},
        {"metadata": {"name": "other-app", "id": "ep-9"}, "status": {"state": "RUNNING"}},
    ]},
    "msp postgresql v1alpha1 cluster list": {"items": [
        {"metadata": {"name": "waive-db", "id": "pg-1"}, "status": {"phase": "RUNNING"}},
    ]},
    "mysterybox secret list": {"items": [{"metadata": {"name": "waive-secrets", "id": "mbsec-1"}}]},
    "registry list": {"items": [{"metadata": {"name": "waive-registry", "id": "registry-1"}}]},
    "ai endpoint stop": {},
    "ai endpoint start": {},
    "ai endpoint delete": {},
    "msp postgresql v1alpha1 cluster delete": {},
    "mysterybox secret delete": {},
    "registry delete": {},
}


def nebius():
    runner = FakeRunner(LISTINGS)
    return Nebius("project-test", runner=runner), runner


def test_list_keeps_only_waive_resources_with_their_state():
    found = list_waive_resources(nebius()[0])
    assert [(r.kind, r.name, r.id, r.state) for r in found] == [
        ("endpoint", "waive-web", "ep-1", "RUNNING"),
        ("postgres", "waive-db", "pg-1", "RUNNING"),
        ("secret", "waive-secrets", "mbsec-1", "unknown"),
        ("registry", "waive-registry", "registry-1", "unknown"),
    ]


def test_stop_and_start_address_the_endpoint_by_id_without_parent():
    client, runner = nebius()
    assert stop(client).id == "ep-1"
    assert runner.calls[-1] == ["nebius", "ai", "endpoint", "stop", "--id", "ep-1", "--format", "json"]
    assert start(client).id == "ep-1"
    assert runner.calls[-1] == ["nebius", "ai", "endpoint", "start", "--id", "ep-1", "--format", "json"]
    empty = Nebius("project-test", runner=FakeRunner({"ai endpoint list": {"items": []}}))
    with pytest.raises(LookupError):
        stop(empty)


def test_status_shows_url_cost_and_the_zdr_gate():
    client, _ = nebius()
    settings = Settings(_env_file=None, nebius_project_id="project-test")
    text = render_status(status(client), settings)
    assert "https://waive-web-x.serverless.example" in text
    assert "waive-web" in text and "RUNNING" in text and "waive-db" in text
    assert "$0.0660/h" in text and "$0.1431/h" in text and "$0.2122/h" in text  # incl. endpoint disk
    assert "ZDR confirmed: false" in text and "U0.4" in text
    assert "project-test" not in text


def test_cleanup_deletes_only_confirmed_waive_resources_in_order():
    client, runner = nebius()
    deleted = cleanup(client, confirm=lambda resource: resource.kind != "registry")
    assert [r.kind for r in deleted] == ["endpoint", "postgres", "secret"]
    assert CLEANUP_ORDER == ("endpoint", "postgres", "secret", "registry")
    delete_calls = [call for call in runner.calls if "delete" in call]
    assert delete_calls == [
        ["nebius", "ai", "endpoint", "delete", "--id", "ep-1", "--format", "json"],
        ["nebius", "msp", "postgresql", "v1alpha1", "cluster", "delete", "--id", "pg-1", "--format", "json"],
        ["nebius", "mysterybox", "secret", "delete", "--id", "mbsec-1", "--format", "json"],
    ]
    with pytest.raises(ValueError):
        delete_resource(client, Resource("endpoint", "other-app", "ep-9", "RUNNING"))
    assert all("ep-9" not in call for call in runner.calls)
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_cloud_ops.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.cloud.ops'`.

- [ ] **Step 3: Implement**

`src/waive/cloud/ops.py`:

```python
"""Operate the deployment: start/stop the endpoint, show status and cost, delete waive-* resources
(spec §14). Documented commands: `nebius ai endpoint start|stop|delete --id <id>`,
`nebius msp postgresql v1alpha1 cluster delete --id <id>`
(https://docs.nebius.com/serverless/endpoints/manage.md,
https://docs.nebius.com/postgresql/clusters/manage.md). The delete flags for secrets and
registries are assumed to be `--id` as well; `waive cloud cleanup` prints each command before
asking, so a wrong flag fails visibly and harmlessly."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from waive.cloud.costs import Footprint
from waive.cloud.deploy import ENDPOINT_NAME, endpoint_url, find_endpoint
from waive.cloud.nebius import Nebius, id_of, items, name_of
from waive.config import Settings

PREFIX = "waive-"
Kind = Literal["endpoint", "postgres", "secret", "registry"]

LIST_COMMANDS: dict[Kind, tuple[str, ...]] = {
    "endpoint": ("ai", "endpoint", "list"),
    "postgres": ("msp", "postgresql", "v1alpha1", "cluster", "list"),
    "secret": ("mysterybox", "secret", "list"),
    "registry": ("registry", "list"),
}
DELETE_COMMANDS: dict[Kind, tuple[str, ...]] = {
    "endpoint": ("ai", "endpoint", "delete", "--id"),
    "postgres": ("msp", "postgresql", "v1alpha1", "cluster", "delete", "--id"),
    "secret": ("mysterybox", "secret", "delete", "--id"),
    "registry": ("registry", "delete", "--id"),
}
# The endpoint uses the secret and the image, so it goes first; the registry last.
CLEANUP_ORDER: tuple[Kind, ...] = ("endpoint", "postgres", "secret", "registry")


@dataclass(frozen=True)
class Resource:
    kind: Kind
    name: str
    id: str
    state: str


def _state(resource: dict) -> str:
    status = resource.get("status", {}) or {}
    for key in ("state", "phase", "status"):
        value = status.get(key)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def list_waive_resources(nebius: Nebius) -> list[Resource]:
    found: list[Resource] = []
    for kind in CLEANUP_ORDER:
        for item in items(nebius.run(*LIST_COMMANDS[kind])):
            if name_of(item).startswith(PREFIX):
                found.append(Resource(kind, name_of(item), id_of(item), _state(item)))
    return found


def _endpoint(nebius: Nebius) -> Resource:
    endpoint = find_endpoint(nebius)
    if endpoint is None:
        raise LookupError(f"no endpoint named {ENDPOINT_NAME}; run `waive cloud deploy` first")
    return Resource("endpoint", ENDPOINT_NAME, id_of(endpoint), _state(endpoint))


def start(nebius: Nebius) -> Resource:
    endpoint = _endpoint(nebius)
    nebius.run("ai", "endpoint", "start", "--id", endpoint.id, parent=False)
    return endpoint


def stop(nebius: Nebius) -> Resource:
    endpoint = _endpoint(nebius)
    nebius.run("ai", "endpoint", "stop", "--id", endpoint.id, parent=False)
    return endpoint


@dataclass(frozen=True)
class CloudStatus:
    resources: list[Resource]
    url: str | None


def status(nebius: Nebius) -> CloudStatus:
    resources = list_waive_resources(nebius)
    endpoint = find_endpoint(nebius)
    return CloudStatus(resources, endpoint_url(endpoint) if endpoint else None)


def render_status(state: CloudStatus, settings: Settings) -> str:
    footprint = Footprint(
        settings.cloud_platform,
        settings.cloud_preset,
        settings.cloud_pg_preset,
        settings.cloud_pg_disk_gib,
        endpoint_disk_gib=settings.cloud_endpoint_disk_gib,
    )
    lines = ["Resources named waive-*:"]
    lines += [f"  {r.kind:<9} {r.name:<16} {r.state}" for r in state.resources] or ["  none"]
    lines.append(f"Public URL: {state.url or 'none (endpoint absent or not started)'}")
    running = any(r.kind == "endpoint" and r.state.upper() == "RUNNING" for r in state.resources)
    has_db = any(r.kind == "postgres" for r in state.resources)
    endpoint_rate = footprint.endpoint if running else 0
    db_rate = footprint.postgres if has_db else 0
    lines.append(
        f"Estimated rate: endpoint ${footprint.endpoint:.4f}/h ({'running' if running else 'stopped: $0'}), "
        f"database ${footprint.postgres:.4f}/h ({'present' if has_db else 'absent'}), "
        f"total ${footprint.total:.4f}/h when both run; now ≈ ${endpoint_rate + db_rate:.4f}/h"
    )
    lines.append(
        f"ZDR confirmed: {str(settings.zdr_confirmed).lower()} (gate U0.4; the endpoint runs with "
        "WAIVE_ZDR_CONFIRMED=false until the gate closes and the endpoint is recreated)"
    )
    lines.append("Spend to date: console → Billing → Usage (no CLI command).")
    return "\n".join(lines)


def delete_resource(nebius: Nebius, resource: Resource) -> None:
    if not resource.name.startswith(PREFIX):
        raise ValueError(f"refusing to delete {resource.kind} {resource.name!r}: not a waive- resource")
    nebius.run(*DELETE_COMMANDS[resource.kind], resource.id, parent=False)


def cleanup(nebius: Nebius, confirm: Callable[[Resource], bool]) -> list[Resource]:
    deleted: list[Resource] = []
    for resource in list_waive_resources(nebius):  # already in CLEANUP_ORDER
        if confirm(resource):
            delete_resource(nebius, resource)
            deleted.append(resource)
    return deleted
```

Add to `src/waive/cloud/cli.py` (imports: `from rich.table import Table`, `from waive.cloud.ops import DELETE_COMMANDS, cleanup, list_waive_resources, render_status, start, status, stop`):

```python
@cloud_app.command("status")
def cloud_status() -> None:
    """Read-only: waive-* resources, the public URL, the hourly rate and the ZDR flag."""
    settings = Settings()
    nebius = _nebius(settings)
    try:
        console.print(render_status(status(nebius), settings))
    except NebiusError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None


def _toggle(action) -> None:
    settings = Settings()
    nebius = _nebius(settings)
    try:
        endpoint = action(nebius)
    except (LookupError, NebiusError) as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    console.print(
        f"{action.__name__.capitalize()} requested for {endpoint.name} ({endpoint.id}). Verify: "
        f"nebius ai endpoint get {endpoint.id} --format json | jq .status"
    )


@cloud_app.command("start")
def cloud_start() -> None:
    """Start the endpoint (bills while running)."""
    _toggle(start)


@cloud_app.command("stop")
def cloud_stop() -> None:
    """Stop the endpoint (no compute or storage billing while stopped)."""
    _toggle(stop)


@cloud_app.command("cleanup")
def cloud_cleanup() -> None:
    """Delete waive-* resources one by one, asking before each. Never touches other names."""
    settings = Settings()
    nebius = _nebius(settings)
    found = list_waive_resources(nebius)
    if not found:
        console.print("No waive-* resources found.")
        return
    table = Table("Kind", "Name", "Id", "State", "Command")
    for resource in found:
        table.add_row(
            resource.kind, resource.name, resource.id, resource.state,
            "nebius " + " ".join(DELETE_COMMANDS[resource.kind]) + f" {resource.id}",
        )
    console.print(table)
    console.print("A deleted database is gone for good; its atlas can be rebuilt with `waive db copy --to-cloud`.")
    deleted = cleanup(
        nebius,
        confirm=lambda r: typer.confirm(f"Delete {r.kind} {r.name} ({r.id})?", default=False),
    )
    console.print(
        f"Deleted {len(deleted)} resource(s): {', '.join(r.name for r in deleted) or 'none'}. "
        "Verify with `uv run waive cloud status`."
    )
```

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_cloud_ops.py -v`
Expected: 4 passed. If `test_status_shows_url_cost_and_the_zdr_gate` fails on `$0.1431/h`, the `:.4f` format rounds `0.14311…` to `0.1431` — check that `cloud_pg_disk_gib` defaults to 32 in `Settings` (6.1).

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pytest
git add src/waive/cloud/ops.py src/waive/cloud/cli.py tests/unit/test_cloud_ops.py
git commit -m "feat: waive cloud start|stop|status|cleanup over waive- resources only (6.7)"
```

Expected: 275 tests pass.

- [ ] **Step 6: Exercise against the real project (gates U0.5 and U6.1; the endpoint exists from 6.6)**

```bash
uv run waive cloud status
uv run waive cloud stop
nebius ai endpoint get "$(nebius ai endpoint list --parent-id "$PROJECT_ID" --format json | jq -r '.items[] | select(.metadata.name=="waive-web") | .metadata.id')" --format json | jq .status
```

Expected: `status` prints the four resources, the URL, the rates and `ZDR confirmed: false (gate U0.4 …)`; after `stop`, the endpoint's status leaves `RUNNING` within a minute and `curl -sS -o /dev/null -w '%{http_code}\n' "$URL/healthz"` no longer returns 200. If the real listings use other state keys than `state`/`phase`/`status` (status shows `unknown`), read `var/cloud-discovery.json` or `jq .status` output, add the key to `_state`, and extend the `LISTINGS` fixture. Leave the endpoint stopped unless 6.8 follows now. Do not run `cleanup` here; it is for the end of the project (gate U8.4) or for recreating the database between demo windows if the user chose that at U6.1.

---

### Task 6.8: Public URL smoke test, stop/start check and the phone test (gates U0.5 and U6.1)

**Files:**
- Create: `deploy/smoke.sh`
- The orchestrator (not the implementer) records the URL, resource names, hourly rate and ZDR state in `docs/PROGRESS.md`.

**Interfaces:**
- Consumes: the public URL from `waive cloud status` (6.6/6.7); routes `GET /healthz`, `GET /`, `GET /atlas`, `GET /atlas/{ccn}`, `GET /atlas/{ccn}.json`, `POST /cases` (form field `state`).
- Produces: `deploy/smoke.sh <https-url>` exiting 0 only when every check passes.

- [ ] **Step 1: Write the smoke script**

`deploy/smoke.sh`:

```bash
#!/usr/bin/env bash
# Public URL smoke test (task 6.8): health, home page, atlas list, one sheet page and JSON, and a
# new case. Usage: deploy/smoke.sh https://<endpoint host>
set -euo pipefail
URL="${1:?usage: deploy/smoke.sh https://host}"
URL="${URL%/}"
health="$(curl -fsS "$URL/healthz")"
echo "healthz: $health"
test "$health" = '{"ok":true}'
curl -fsS "$URL/" | grep -q "never asks for money" && echo "home: ok"
atlas="$(curl -fsS "$URL/atlas")"
count="$(grep -o 'href="/atlas/[0-9]*"' <<<"$atlas" | sort -u | wc -l | tr -d ' ')"
echo "atlas: $count hospital links"
test "$count" -ge 20
first="$(grep -o 'href="/atlas/[0-9]*"' <<<"$atlas" | head -1 | grep -o '[0-9]*')"
echo "sheet $first: HTTP $(curl -fsS -o /dev/null -w '%{http_code}' "$URL/atlas/$first")"
curl -fsS "$URL/atlas/$first.json" | grep -q "\"ccn\"" && echo "sheet $first json: ok"
code="$(curl -sS -o /dev/null -w '%{http_code}' -X POST --data "state=MA" "$URL/cases")"
echo "new case: HTTP $code"
test "$code" = "200"
echo "all checks passed"
```

Run locally first, against the Docker image from 6.5 step 5 (no gate; the SQLite image has only the demo hospital, so expect the atlas check to fail there — the point is the script's syntax and the first three checks):

```bash
chmod +x deploy/smoke.sh && bash -n deploy/smoke.sh
docker run --rm -d --name waive-smoke -p 8000:8000 \
  -e WAIVE_VAULT_KEY="$(uv run python -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())')" \
  -e WAIVE_TOKEN_SECRET="$(uv run python -c 'import secrets; print(secrets.token_hex(32))')" \
  waive:dev
for _ in 1 2 3 4 5 6 7 8 9 10; do curl -fsS http://127.0.0.1:8000/healthz >/dev/null && break; sleep 1; done
deploy/smoke.sh http://127.0.0.1:8000 || echo "expected: stops at the atlas count on an empty database"
docker rm -f waive-smoke
```

Expected: `healthz: {"ok":true}`, `home: ok`, `atlas: 0 hospital links`, then the script exits 1 at `test "$count" -ge 20` and the echo line prints.

- [ ] **Step 2: Start the endpoint and run the smoke test on the public URL (gate U6.1; bills while running)**

```bash
uv run waive cloud start
uv run waive cloud status          # repeat until the endpoint shows RUNNING and a URL
URL="<the https URL from status>"
deploy/smoke.sh "$URL"
```

Expected: every line `ok`/200 and `all checks passed`; the atlas count equals the number of published non-demo Massachusetts sheets in `docs/reports/atlas-ma.md`. If `new case` returns 500, the vault secrets did not reach the container (`WAIVE_VAULT_KEY`/`WAIVE_TOKEN_SECRET` in SecretStash) — check the endpoint log in the console for `WAIVE_VAULT_KEY is not set`. If the home page is served over HTTPS but a redirect goes to `http://`, uvicorn is not trusting the proxy headers — confirm the CMD in the Dockerfile still has `--proxy-headers --forwarded-allow-ips *`.

- [ ] **Step 3: Verify stop and start (Phase 6 exit check)**

```bash
date; uv run waive cloud stop
sleep 60; curl -sS -o /dev/null -w 'while stopped: %{http_code}\n' "$URL/healthz" || echo "while stopped: no answer (expected)"
date; uv run waive cloud start
for _ in $(seq 1 24); do curl -fsS "$URL/healthz" && break; sleep 10; done
date; uv run waive cloud status
```

Expected: no 200 while stopped; `{"ok":true}` again within a few minutes of `start`; `status` shows RUNNING and the same URL (if the URL changed, record the new one). Note the start-up time for PROGRESS — it is how long before a demo the endpoint must be started.

- [ ] **Step 4: Phone test (the user, on their own phone; gate U6.1 covers the running time)**

Send the user these steps and keep the endpoint running until they answer, then stop it:

1. Open the URL on your phone (any browser). The home page should fit the screen without horizontal scrolling and the "Start a case" button should be easy to tap.
2. Tap **Start a case** → the case page shows the senior link and a share button. Tap the senior link.
3. The camera screen opens. Photograph `var/corpus/bill-000.jpg` shown on the laptop screen (`uv run waive corpus generate --count 1` if the folder is empty).
4. Expected while gate U0.4 is open: the "your helper needs to finish setting things up" screen — the photo reached the server and was refused by the ZDR check, as designed. After U0.4 closes (`WAIVE_ZDR_CONFIRMED=true` goes into the endpoint's `--env` and the endpoint is recreated), the read-back screen appears instead.
5. Open `<URL>/atlas` and one hospital page; the quotes and source links must be readable at phone size.

Then: `uv run waive cloud stop` unless a demo is scheduled.

- [ ] **Step 5: Commit and hand the record to the orchestrator**

```bash
git add deploy/smoke.sh
git commit -m "chore: public URL smoke test script (6.8)"
```

Report for `docs/PROGRESS.md` (the orchestrator writes it; no secrets, no project id): the public URL; resource names `waive-registry`, `waive-db` (preset, disk, created at), `waive-secrets`, `waive-web` (platform, preset, image tag); rates from `waive cloud status`; the running schedule agreed at U6.1; stop/start timings; the phone test outcome; `WAIVE_ZDR_CONFIRMED=false` on the endpoint pending U0.4; the AI Cloud spend row from the console's Billing → Usage page.

---

## Phase 6 exit checks (master plan)

- The public URL works on a phone: 6.8 steps 2 and 4.
- Stop and start verified: 6.8 step 3 (timings recorded).
- Cost table committed: `docs/reports/cloud-costs.md` from 6.3, regenerated if the preset changed.
- ZDR confirmation recorded: `waive cloud status` prints the flag; PROGRESS states that the endpoint runs with `WAIVE_ZDR_CONFIRMED=false` until U0.4 and how it is updated (change `plain_env`'s value only after U0.4, rebuild nothing — recreate the endpoint with `waive cloud deploy`).
- Budget: AI Cloud spend ≤ $30 through 2026-10-30, checked in the console after 6.8; the endpoint is stopped outside agreed windows.
- `uv run ruff format . && uv run ruff check . && uv run pytest` → 275 tests pass.

## Self-review

- **Spec coverage.** §14: image in Container Registry (6.4); Serverless AI endpoint on a CPU platform, smallest preset, container port 8000, no endpoint auth, `--env` and `--env-secret` from SecretStash, managed HTTPS URL (6.6); Managed PostgreSQL smallest preset (6.5); endpoints bill while running → `waive cloud start|stop` and the running-windows rule (6.7, Global Constraints); cleanup limited to `waive-` names with confirmation (6.7); fallback VM with Docker and Caddy (6.6b). Object Storage is deferred by instruction (documents are `source_docs.text`); recorded under deviations. §15: every price in the plan carries its source and the $30 cap is converted into hours (6.3 report, Cost assumptions). §11: the vault key comes from SecretStash in the cloud and `.env` locally (6.6); logs never carry personal content — access logs off (6.1); the usage ledger holds amounts only (6.2 test asserts the column set); `WAIVE_REQUIRE_ZDR=true` in production (6.6 `plain_env`). §18 open items: region, smallest CPU preset, smallest PostgreSQL preset and price are answered by 6.3's report and the facts table. Master-plan exit checks: all five listed above map to steps.
- **Gates.** 6.1 and 6.2 need no gate; 6.3 needs U0.5 and is read-only (test asserts no mutating verb without `--help`); every state-changing `nebius` command in 6.4–6.8 is preceded by "gate U6.1" and followed by a `list`/`get`/`curl` verification; 6.3 ends with the STOP and the exact U6.1 message.
- **Type consistency.** `Settings.env / ledger_backend / cloud_database_url / registry / cloud_platform / cloud_preset / cloud_pg_preset / cloud_pg_disk_gib` (6.1) are read by 6.2 (`make_ledger`), 6.3 (`Footprint`, `cloud_discover`), 6.5 (`db copy --to-cloud`), 6.6 (`plain_env`, `endpoint_create_args`, `secret_payload`) and 6.7 (`render_status`). `Nebius.run(*args, parent, json_output, redact)`, `items`, `name_of`, `id_of` (6.3) are used unchanged in 6.6 and 6.7; `FakeRunner(answers)` with `.calls` (6.3 test) is imported by the 6.6 and 6.7 tests. `ENDPOINT_NAME`, `find_endpoint`, `endpoint_url` (6.6) feed `ops.start/stop/status` (6.7). `copy_tables(source, target, *, skip)` (6.5) matches both CLI call sites. `Footprint("cpu-d3", "2vcpu-8gb", "2vcpu-8gb", 32, endpoint_disk_gib=32)` gives $0.0660 + $0.0031 + $0.1431 = $0.2122/h and 141 hours everywhere it is quoted (6.3 review: the endpoint's container disk is priced; without `endpoint_disk_gib` the footprint is $0.2091/h and 143 hours). Test counts: 248 → 253 → 257 → 265 → 267 → 271 → 275.
- **Placeholder scan.** Every code step is complete; the only `<…>` tokens are values the implementer reads from live output (ids, the URL, the region), each with the command that prints it. Angle-bracket arguments inside quoted `nebius` facts are the docs' own syntax.
- **Unverified items** are listed once (facts table, seven numbered points) and each is referenced where the plan copes with it; the first real `discover` run saves raw JSON precisely so parsers can be corrected before any paid step.
- **Known simplifications (carry forward).** No rate limiting on the endpoint (spec §11 asks for it; Phase 7 or 8). Redeploying a new image is delete + `deploy` until an `update` command is confirmed. The ledger's PostgreSQL rows are summed in Python (fine at this volume). The VM fallback copies the image over SSH instead of pulling from the registry, so it needs no registry credentials on the VM. `waive cloud status` estimates cost from `.env` presets rather than reading the endpoint's spec (field names unverified).

