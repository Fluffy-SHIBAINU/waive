# Waive Phase 0 — Foundations and Connectivity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runnable Python project with typed settings, a spend governor, Token Factory and Tavily adapters, and a `waive doctor` command that proves both APIs work without leaking secrets.

**Architecture:** Single `waive` package (src layout) managed by uv. External services sit behind small classes (`AIClient`, `TavilyGateway`) that take injected dependencies, so tests never touch the network. A JSON-lines ledger records every paid call, and the `Governor` enforces caps before each call.

**Tech Stack:** Python 3.12, uv, pydantic-settings, openai SDK, tavily-python, typer, rich, pytest, respx, ruff.

**Spec:** `docs/superpowers/specs/2026-10-02-waive-design.md`

## Global Constraints

- Python `>=3.12,<3.13`; package name `waive`; src layout.
- Token Factory base URL `https://api.tokenfactory.nebius.com/v1/`; key env var `NEBIUS_API_KEY`; Tavily key env var `TAVILY_API_KEY`.
- Never print, log or commit secret values; `.env` is gitignored.
- `WAIVE_REQUIRE_ZDR=true` by default; calls flagged `phi=True` raise until `WAIVE_ZDR_CONFIRMED=true`.
- Development caps: Tavily 1,000 credits; Token Factory $15.
- Tests never call real APIs unless marked `live`; the default `pytest` run excludes `live`.
- Tests construct `Settings(_env_file=None, ...)` so a developer's `.env` never leaks into tests.
- License: Apache-2.0.

---

### Task 0.1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `.python-version`, `src/waive/__init__.py`, `tests/unit/test_smoke.py`, `.env.example`, `Makefile`, `README.md`, `LICENSE`

**Interfaces:**
- Produces: importable package `waive` with `waive.__version__ == "0.1.0"`; `uv run pytest` and `uv run ruff check .` working.

- [ ] **Step 1: Install the toolchain**

Run: `uv --version || brew install uv` then `uv python install 3.12`
Expected: uv prints a version; Python 3.12 installed.

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "waive"
version = "0.1.0"
description = "Hospital financial assistance for seniors, from a photo of the bill."
readme = "README.md"
requires-python = ">=3.12,<3.13"
license = { text = "Apache-2.0" }
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "jinja2>=3.1",
  "pydantic>=2.8",
  "pydantic-settings>=2.4",
  "openai>=1.40",
  "tavily-python>=0.5",
  "typer>=0.12",
  "rich>=13.7",
  "httpx>=0.27",
]

[dependency-groups]
dev = [
  "pytest>=8.3",
  "pytest-cov>=5.0",
  "respx>=0.21",
  "hypothesis>=6.100",
  "ruff>=0.6",
]

[project.scripts]
waive = "waive.cli:app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/waive"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q -m 'not live'"
markers = ["live: calls real external APIs and spends credits"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP"]
# Line length is handled by `ruff format`; pydantic generics use typing.Generic on purpose.
ignore = ["E501", "UP046", "UP047"]
```

And `.python-version`:

```
3.12
```

And an empty package marker `src/waive/__init__.py` (zero bytes for now).

- [ ] **Step 3: Write the failing test**

`tests/unit/test_smoke.py`:

```python
import waive


def test_package_has_version():
    assert waive.__version__ == "0.1.0"
```

- [ ] **Step 4: Run it to make sure it fails**

Run: `uv sync && uv run pytest tests/unit/test_smoke.py -v`
Expected: FAIL with `AttributeError: module 'waive' has no attribute '__version__'`

- [ ] **Step 5: Implement**

`src/waive/__init__.py`:

```python
"""Waive: hospital financial assistance from a photo of the bill."""

__version__ = "0.1.0"
```

- [ ] **Step 6: Run the test to make sure it passes**

Run: `uv run pytest tests/unit/test_smoke.py -v`
Expected: PASS

- [ ] **Step 7: Add project files**

`.env.example`:

```
# Nebius Token Factory key: https://tokenfactory.nebius.com/project/api-keys
NEBIUS_API_KEY=
# Tavily key: https://app.tavily.com
TAVILY_API_KEY=
# Nebius AI Cloud project ID (from your console URL)
NEBIUS_PROJECT_ID=
# Set to true only after zero data retention is enabled for Token Factory
WAIVE_REQUIRE_ZDR=true
WAIVE_ZDR_CONFIRMED=false
# Models (verify with: uv run waive doctor --live)
WAIVE_MODEL_REASON=nvidia/Nemotron-3-Super-120B-A12B
WAIVE_MODEL_FAST=nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B
WAIVE_MODEL_VISION=nvidia/Nemotron-Nano-V2-12b
# Development spend caps
WAIVE_TAVILY_CREDIT_CAP=1000
WAIVE_TOKEN_FACTORY_USD_CAP=15
```

`Makefile` (recipe lines start with a tab):

```make
.PHONY: install test lint fmt doctor
install:
	uv sync
test:
	uv run pytest
lint:
	uv run ruff check .
fmt:
	uv run ruff format .
doctor:
	uv run waive doctor
```

`README.md`:

```markdown
# Waive

Free or discounted hospital care you're owed, from a photo of the bill.

Status: in development for the Nebius x NVIDIA Global AI Hackathon (Personal AI track).
Design: `docs/superpowers/specs/2026-10-02-waive-design.md`.

## Setup

1. `brew install uv && uv python install 3.12`
2. `uv sync`
3. `cp .env.example .env` and fill in your keys (never commit `.env`).
4. `uv run waive doctor --live`
```

`LICENSE`:

Run: `curl -sSL https://www.apache.org/licenses/LICENSE-2.0.txt -o LICENSE && head -3 LICENSE`
Expected: the first lines contain `Apache License` and `Version 2.0, January 2004`.

- [ ] **Step 8: Format and lint**

Run: `uv run ruff format . && uv run ruff check .`
Expected: files formatted, then `All checks passed!`

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock .python-version src/waive/__init__.py tests/unit/test_smoke.py .env.example Makefile README.md LICENSE
git commit -m "chore: scaffold waive package with uv, pytest and ruff"
```

---

### Task 0.2: Settings

**Files:**
- Create: `src/waive/config.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `waive.config.Settings` with fields `nebius_api_key: SecretStr | None`, `tavily_api_key: SecretStr | None`, `nebius_project_id: str | None`, `token_factory_base_url: str`, `model_reason: str`, `model_fast: str`, `model_vision: str`, `require_zdr: bool`, `zdr_confirmed: bool`, `tavily_credit_cap: int`, `token_factory_usd_cap: Decimal`, `ledger_path: Path`. Construct with `Settings()` (reads `.env` and the environment) or `Settings(_env_file=None, field=value, ...)` in tests.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_config.py`:

```python
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
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.config'`

- [ ] **Step 3: Implement**

`src/waive/config.py`:

```python
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
    model_reason: str = "nvidia/Nemotron-3-Super-120B-A12B"
    model_fast: str = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
    model_vision: str = "nvidia/Nemotron-Nano-V2-12b"

    require_zdr: bool = True
    zdr_confirmed: bool = False

    tavily_credit_cap: int = 1000
    token_factory_usd_cap: Decimal = Decimal("15")
    ledger_path: Path = Path("var/usage.jsonl")
```

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/waive/config.py tests/unit/test_config.py
git commit -m "feat: typed settings with secret-safe API keys"
```

---

### Task 0.3: Usage ledger and governor

**Files:**
- Create: `src/waive/governor.py`
- Test: `tests/unit/test_governor.py`

**Interfaces:**
- Consumes: `Settings` (task 0.2).
- Produces: `BudgetExceeded(RuntimeError)`; `UsageEvent(provider, units, usd, purpose, ts)`; `Ledger(path)` with `record(event)` and `totals(provider) -> tuple[Decimal, Decimal]`; `Governor(ledger, tavily_credit_cap, token_factory_usd_cap)` with `ensure_tavily(credits: Decimal)`, `record_tavily(credits: Decimal, purpose: str)`, `ensure_token_factory()`, `record_token_factory(prompt_tokens: int, completion_tokens: int, usd: Decimal, purpose: str)`, `summary() -> dict[str, tuple[Decimal, Decimal]]`; `make_governor(settings) -> Governor`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_governor.py`:

```python
import json
from decimal import Decimal

import pytest

from waive.governor import BudgetExceeded, Governor, Ledger


def make(tmp_path, tavily_cap=10, tf_cap="1.00"):
    return Governor(Ledger(tmp_path / "usage.jsonl"), tavily_cap, Decimal(tf_cap))


def test_tavily_cap_blocks_before_spending(tmp_path):
    governor = make(tmp_path, tavily_cap=3)
    governor.ensure_tavily(Decimal("2"))
    governor.record_tavily(Decimal("2"), "test")
    with pytest.raises(BudgetExceeded):
        governor.ensure_tavily(Decimal("2"))


def test_ledger_persists_across_instances(tmp_path):
    make(tmp_path).record_tavily(Decimal("1"), "first")
    assert make(tmp_path).summary()["tavily"] == (Decimal("1"), Decimal("0"))


def test_token_factory_cap_blocks_once_reached(tmp_path):
    governor = make(tmp_path, tf_cap="0.50")
    governor.ensure_token_factory()
    governor.record_token_factory(1000, 500, Decimal("0.50"), "test")
    with pytest.raises(BudgetExceeded):
        governor.ensure_token_factory()


def test_ledger_rows_hold_no_content(tmp_path):
    make(tmp_path).record_token_factory(10, 5, Decimal("0.01"), "bill-extract")
    row = json.loads((tmp_path / "usage.jsonl").read_text().splitlines()[0])
    assert set(row) == {"provider", "units", "usd", "purpose", "ts"}
    assert row["units"] == "15"
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_governor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.governor'`

- [ ] **Step 3: Implement**

`src/waive/governor.py`:

```python
"""Spend tracking and hard caps for paid APIs (spec section 15)."""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from waive.config import Settings


class BudgetExceeded(RuntimeError):
    """Raised before a paid call that would break a configured cap."""


@dataclass(frozen=True)
class UsageEvent:
    provider: str
    units: Decimal
    usd: Decimal
    purpose: str
    ts: str


class Ledger:
    """Append-only JSON-lines record of paid calls. Stores amounts, never content."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def record(self, event: UsageEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        row = {key: str(value) for key, value in asdict(event).items()}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    def totals(self, provider: str) -> tuple[Decimal, Decimal]:
        units = usd = Decimal("0")
        if not self._path.exists():
            return units, usd
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row["provider"] == provider:
                units += Decimal(row["units"])
                usd += Decimal(row["usd"])
        return units, usd


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Governor:
    def __init__(self, ledger: Ledger, tavily_credit_cap: int, token_factory_usd_cap: Decimal) -> None:
        self._ledger = ledger
        self._tavily_cap = Decimal(tavily_credit_cap)
        self._tf_cap = token_factory_usd_cap

    def ensure_tavily(self, credits: Decimal) -> None:
        used, _ = self._ledger.totals("tavily")
        if used + credits > self._tavily_cap:
            raise BudgetExceeded(
                f"Tavily cap of {self._tavily_cap} credits would be exceeded (used {used})."
            )

    def record_tavily(self, credits: Decimal, purpose: str) -> None:
        self._ledger.record(UsageEvent("tavily", credits, Decimal("0"), purpose, _now()))

    def ensure_token_factory(self) -> None:
        _, usd = self._ledger.totals("token_factory")
        if usd >= self._tf_cap:
            raise BudgetExceeded(f"Token Factory cap of ${self._tf_cap} reached (used ${usd}).")

    def record_token_factory(
        self, prompt_tokens: int, completion_tokens: int, usd: Decimal, purpose: str
    ) -> None:
        tokens = Decimal(prompt_tokens + completion_tokens)
        self._ledger.record(UsageEvent("token_factory", tokens, usd, purpose, _now()))

    def summary(self) -> dict[str, tuple[Decimal, Decimal]]:
        return {provider: self._ledger.totals(provider) for provider in ("tavily", "token_factory")}


def make_governor(settings: Settings) -> Governor:
    return Governor(
        Ledger(settings.ledger_path), settings.tavily_credit_cap, settings.token_factory_usd_cap
    )
```

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_governor.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/waive/governor.py tests/unit/test_governor.py
git commit -m "feat: usage ledger and spend governor with hard caps"
```

---

### Task 0.4: Token Factory client

**Files:**
- Create: `src/waive/ai/__init__.py`, `src/waive/ai/client.py`
- Test: `tests/unit/test_ai_client.py`

**Interfaces:**
- Consumes: `Settings` (0.2), `Governor` (0.3).
- Produces: `AIClient(settings, governor, http_client=None)` with `model_for(role: str) -> str` (roles `reason`, `fast`, `vision`), `list_models() -> list[str]`, `complete_json(role, messages, schema, *, phi: bool, purpose: str, max_tokens: int = 2000) -> schema instance`; exceptions `ZDRRequired`, `AIOutputError`; helpers `estimate_usd(model, prompt_tokens, completion_tokens) -> Decimal` and `extract_json(text) -> str`; `PRICES_PER_MILLION` dict.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_ai_client.py`:

```python
from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import BaseModel, SecretStr

from waive.ai.client import AIClient, AIOutputError, ZDRRequired, estimate_usd, extract_json
from waive.config import Settings
from waive.governor import Governor, Ledger

BASE = "https://api.tokenfactory.nebius.com/v1"


class Answer(BaseModel):
    tier: str
    percent: int


def chat_payload(content: str) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


def make_client(tmp_path, **overrides):
    settings = Settings(
        _env_file=None,
        nebius_api_key=SecretStr("test-key"),
        ledger_path=tmp_path / "usage.jsonl",
        **overrides,
    )
    governor = Governor(
        Ledger(settings.ledger_path), settings.tavily_credit_cap, settings.token_factory_usd_cap
    )
    return AIClient(settings, governor), governor


USER = [{"role": "user", "content": "hi"}]


@respx.mock
def test_complete_json_validates_and_records_usage(tmp_path):
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=chat_payload('{"tier": "free", "percent": 100}'))
    )
    client, governor = make_client(tmp_path)
    result = client.complete_json("reason", USER, Answer, phi=False, purpose="test")
    assert result == Answer(tier="free", percent=100)
    units, usd = governor.summary()["token_factory"]
    assert units == Decimal("120")
    assert usd > 0


@respx.mock
def test_repairs_invalid_json_once(tmp_path):
    route = respx.post(f"{BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json=chat_payload("not json")),
            httpx.Response(
                200,
                json=chat_payload(
                    '<think>hmm</think>```json\n{"tier": "discount", "percent": 60}\n```'
                ),
            ),
        ]
    )
    client, _ = make_client(tmp_path)
    result = client.complete_json("fast", USER, Answer, phi=False, purpose="test")
    assert result.percent == 60
    assert route.call_count == 2


@respx.mock
def test_gives_up_after_second_bad_answer(tmp_path):
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=chat_payload('{"tier": 1}'))
    )
    client, _ = make_client(tmp_path)
    with pytest.raises(AIOutputError):
        client.complete_json("fast", USER, Answer, phi=False, purpose="test")


@respx.mock
def test_personal_data_blocked_until_zdr_confirmed(tmp_path):
    route = respx.post(f"{BASE}/chat/completions")
    client, _ = make_client(tmp_path)
    with pytest.raises(ZDRRequired):
        client.complete_json("vision", USER, Answer, phi=True, purpose="bill")
    assert route.call_count == 0


@respx.mock
def test_personal_data_allowed_after_zdr_confirmed(tmp_path):
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=chat_payload('{"tier": "free", "percent": 100}'))
    )
    client, _ = make_client(tmp_path, zdr_confirmed=True)
    result = client.complete_json("vision", USER, Answer, phi=True, purpose="bill")
    assert result.tier == "free"


@respx.mock
def test_list_models_is_sorted(tmp_path):
    respx.get(f"{BASE}/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "nvidia/b", "object": "model", "created": 0, "owned_by": "nvidia"},
                    {"id": "nvidia/a", "object": "model", "created": 0, "owned_by": "nvidia"},
                ],
            },
        )
    )
    client, _ = make_client(tmp_path)
    assert client.list_models() == ["nvidia/a", "nvidia/b"]


def test_unknown_role_is_rejected(tmp_path):
    client, _ = make_client(tmp_path)
    with pytest.raises(ValueError):
        client.model_for("poetry")


def test_estimate_usd_uses_price_table():
    assert estimate_usd("nvidia/Nemotron-3-Super-120B-A12B", 1_000_000, 1_000_000) == Decimal(
        "1.20"
    )


def test_extract_json_strips_reasoning_and_fences():
    assert extract_json('<think>x</think> ```json {"a": 1} ```') == '{"a": 1}'
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_ai_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.ai'`

- [ ] **Step 3: Implement**

`src/waive/ai/__init__.py`:

```python
"""Token Factory access for Waive."""

from waive.ai.client import AIClient, AIOutputError, ZDRRequired, estimate_usd

__all__ = ["AIClient", "AIOutputError", "ZDRRequired", "estimate_usd"]
```

`src/waive/ai/client.py`:

```python
"""Budget-aware wrapper around Nebius Token Factory (OpenAI-compatible API)."""

import re
from decimal import Decimal
from typing import Any, TypeVar

import httpx
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from waive.config import Settings
from waive.governor import Governor

T = TypeVar("T", bound=BaseModel)

# Dollars per million (input, output) tokens. Update after `waive doctor --live` (task 0.7).
PRICES_PER_MILLION: dict[str, tuple[Decimal, Decimal]] = {
    "nvidia/Nemotron-3-Super-120B-A12B": (Decimal("0.30"), Decimal("0.90")),
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": (Decimal("0.06"), Decimal("0.24")),
}
# Deliberately high so unknown models never under-count spend.
FALLBACK_PRICE = (Decimal("1.00"), Decimal("3.00"))

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_REPAIR_PROMPT = "That was not valid JSON for the requested schema. Reply with only the corrected JSON object."


class ZDRRequired(RuntimeError):
    """Raised when personal data would be sent before zero data retention is confirmed."""


class AIOutputError(RuntimeError):
    """Raised when model output cannot be parsed into the requested schema."""


def estimate_usd(model: str, prompt_tokens: int, completion_tokens: int) -> Decimal:
    price_in, price_out = PRICES_PER_MILLION.get(model, FALLBACK_PRICE)
    return (price_in * prompt_tokens + price_out * completion_tokens) / Decimal(1_000_000)


def extract_json(text: str) -> str:
    cleaned = _THINK.sub("", text)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end < start:
        raise AIOutputError("no JSON object in model output")
    return cleaned[start : end + 1]


class AIClient:
    def __init__(
        self, settings: Settings, governor: Governor, http_client: httpx.Client | None = None
    ) -> None:
        if settings.nebius_api_key is None:
            raise RuntimeError("NEBIUS_API_KEY is not set")
        self._settings = settings
        self._governor = governor
        self._client = OpenAI(
            base_url=settings.token_factory_base_url,
            api_key=settings.nebius_api_key.get_secret_value(),
            http_client=http_client,
            timeout=60.0,
            max_retries=2,
        )

    def model_for(self, role: str) -> str:
        models = {
            "reason": self._settings.model_reason,
            "fast": self._settings.model_fast,
            "vision": self._settings.model_vision,
        }
        if role not in models:
            raise ValueError(f"unknown model role: {role}")
        return models[role]

    def list_models(self) -> list[str]:
        return sorted(model.id for model in self._client.models.list())

    def complete_json(
        self,
        role: str,
        messages: list[dict[str, Any]],
        schema: type[T],
        *,
        phi: bool,
        purpose: str,
        max_tokens: int = 2000,
    ) -> T:
        if phi and self._settings.require_zdr and not self._settings.zdr_confirmed:
            raise ZDRRequired(
                "Personal data needs zero data retention. "
                "Set WAIVE_ZDR_CONFIRMED=true after enabling it for Token Factory."
            )
        model = self.model_for(role)
        conversation = list(messages)
        last_error: Exception | None = None
        for _attempt in range(2):
            self._governor.ensure_token_factory()
            response = self._client.chat.completions.create(
                model=model, messages=conversation, temperature=0, max_tokens=max_tokens
            )
            usage = response.usage
            if usage is not None:
                self._governor.record_token_factory(
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    estimate_usd(model, usage.prompt_tokens, usage.completion_tokens),
                    purpose,
                )
            text = response.choices[0].message.content or ""
            try:
                return schema.model_validate_json(extract_json(text))
            except (ValidationError, AIOutputError) as error:
                last_error = error
                conversation = [
                    *conversation,
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": _REPAIR_PROMPT},
                ]
        raise AIOutputError(f"model output did not match {schema.__name__}") from last_error
```

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_ai_client.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add src/waive/ai tests/unit/test_ai_client.py
git commit -m "feat: Token Factory client with JSON repair, ZDR gate and spend tracking"
```

---

### Task 0.5: Tavily gateway

**Files:**
- Create: `src/waive/atlas/__init__.py`, `src/waive/atlas/tavily_gateway.py`
- Test: `tests/unit/test_tavily_gateway.py`

**Interfaces:**
- Consumes: `Settings` (0.2), `Governor`, `BudgetExceeded` (0.3).
- Produces: `SearchHit(url, title, content, score)`, `ExtractedPage(url, text)`, `search_cost(depth) -> Decimal`, `extract_cost(successful_urls, depth) -> Decimal`, `TavilyGateway(client, governor)` with `search(query, *, purpose, include_domains=None, max_results=5, depth="basic", topic="general") -> list[SearchHit]` and `extract(urls, *, purpose, depth="basic") -> list[ExtractedPage]`; `make_tavily_gateway(settings, governor) -> TavilyGateway`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_tavily_gateway.py`:

```python
from decimal import Decimal

import pytest

from waive.atlas.tavily_gateway import TavilyGateway, extract_cost, search_cost
from waive.governor import BudgetExceeded, Governor, Ledger


class FakeTavily:
    def __init__(self):
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append(("search", query, kwargs))
        return {
            "results": [
                {
                    "url": "https://www.example.org/fap.pdf",
                    "title": "Financial Assistance Policy",
                    "content": "free care",
                    "score": 0.9,
                }
            ]
        }

    def extract(self, urls, **kwargs):
        self.calls.append(("extract", urls, kwargs))
        ok = [{"url": url, "raw_content": f"text of {url}"} for url in urls[:-1]]
        return {"results": ok, "failed_results": [{"url": urls[-1], "error": "timeout"}]}


def make(tmp_path, cap=10):
    governor = Governor(Ledger(tmp_path / "usage.jsonl"), cap, Decimal("15"))
    fake = FakeTavily()
    return TavilyGateway(fake, governor), fake, governor


def test_search_maps_results_and_spends_one_credit(tmp_path):
    gateway, fake, governor = make(tmp_path)
    hits = gateway.search(
        "St. Example financial assistance", purpose="test", include_domains=["example.org"]
    )
    assert hits[0].url == "https://www.example.org/fap.pdf"
    assert hits[0].score == 0.9
    assert fake.calls[0][2]["include_domains"] == ["example.org"]
    assert fake.calls[0][2]["search_depth"] == "basic"
    assert governor.summary()["tavily"][0] == Decimal("1")


def test_extract_charges_only_successful_urls(tmp_path):
    gateway, _, governor = make(tmp_path)
    urls = [f"https://www.example.org/doc{i}.pdf" for i in range(6)]
    pages = gateway.extract(urls, purpose="test")
    assert len(pages) == 5
    assert pages[0].text == "text of https://www.example.org/doc0.pdf"
    assert governor.summary()["tavily"][0] == Decimal("1")


def test_budget_blocks_before_calling_tavily(tmp_path):
    gateway, fake, governor = make(tmp_path, cap=1)
    governor.record_tavily(Decimal("1"), "earlier")
    with pytest.raises(BudgetExceeded):
        gateway.search("anything", purpose="test")
    assert fake.calls == []


def test_extract_with_no_urls_is_free(tmp_path):
    gateway, fake, _ = make(tmp_path)
    assert gateway.extract([], purpose="test") == []
    assert fake.calls == []


def test_cost_helpers():
    assert search_cost("basic") == Decimal("1")
    assert search_cost("advanced") == Decimal("2")
    assert extract_cost(6, "basic") == Decimal("2")
    assert extract_cost(5, "advanced") == Decimal("2")
    assert extract_cost(0, "basic") == Decimal("0")
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_tavily_gateway.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.atlas'`

- [ ] **Step 3: Implement**

`src/waive/atlas/__init__.py`:

```python
"""Hospital atlas: registry, scouting, procedure sheets and verification."""
```

`src/waive/atlas/tavily_gateway.py`:

```python
"""Budget-aware wrapper around the Tavily API (spec section 8)."""

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol

from waive.config import Settings
from waive.governor import Governor

Depth = Literal["basic", "advanced"]


class TavilyLike(Protocol):
    def search(self, query: str, **kwargs: Any) -> dict[str, Any]: ...

    def extract(self, urls: list[str], **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str
    content: str
    score: float


@dataclass(frozen=True)
class ExtractedPage:
    url: str
    text: str


def search_cost(depth: Depth) -> Decimal:
    return Decimal(2 if depth == "advanced" else 1)


def extract_cost(successful_urls: int, depth: Depth) -> Decimal:
    per_five = 2 if depth == "advanced" else 1
    return Decimal(math.ceil(successful_urls / 5) * per_five)


class TavilyGateway:
    def __init__(self, client: TavilyLike, governor: Governor) -> None:
        self._client = client
        self._governor = governor

    def search(
        self,
        query: str,
        *,
        purpose: str,
        include_domains: list[str] | None = None,
        max_results: int = 5,
        depth: Depth = "basic",
        topic: str = "general",
    ) -> list[SearchHit]:
        cost = search_cost(depth)
        self._governor.ensure_tavily(cost)
        raw = self._client.search(
            query=query,
            search_depth=depth,
            topic=topic,
            max_results=max_results,
            include_domains=include_domains or [],
        )
        self._governor.record_tavily(cost, purpose)
        return [
            SearchHit(
                url=item["url"],
                title=item.get("title") or "",
                content=item.get("content") or "",
                score=float(item.get("score") or 0.0),
            )
            for item in raw.get("results", [])
        ]

    def extract(
        self, urls: list[str], *, purpose: str, depth: Depth = "basic"
    ) -> list[ExtractedPage]:
        if not urls:
            return []
        self._governor.ensure_tavily(extract_cost(len(urls), depth))
        raw = self._client.extract(urls=urls, extract_depth=depth)
        results = raw.get("results", [])
        self._governor.record_tavily(extract_cost(len(results), depth), purpose)
        return [
            ExtractedPage(url=item["url"], text=item.get("raw_content") or "") for item in results
        ]


def make_tavily_gateway(settings: Settings, governor: Governor) -> TavilyGateway:
    from tavily import TavilyClient

    if settings.tavily_api_key is None:
        raise RuntimeError("TAVILY_API_KEY is not set")
    client = TavilyClient(api_key=settings.tavily_api_key.get_secret_value())
    return TavilyGateway(client, governor)
```

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_tavily_gateway.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/waive/atlas tests/unit/test_tavily_gateway.py
git commit -m "feat: budget-aware Tavily gateway for search and extract"
```

---

### Task 0.6: `waive doctor`

**Files:**
- Create: `src/waive/doctor.py`, `src/waive/cli.py`
- Test: `tests/unit/test_doctor.py`

**Interfaces:**
- Consumes: `Settings`, `make_governor`, `AIClient`, `make_tavily_gateway`, `BudgetExceeded`.
- Produces: `Check(name, status, detail)` with status `ok | warn | fail`; `run_checks(settings, *, live, ai_factory, tavily_factory, which=shutil.which, run=subprocess.run) -> list[Check]`; Typer `app` with commands `doctor [--live]` (exit code 1 when any check fails).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_doctor.py`:

```python
import subprocess

from pydantic import SecretStr
from typer.testing import CliRunner

from waive.cli import app
from waive.config import Settings
from waive.doctor import run_checks

MODELS = [
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    "nvidia/Nemotron-3-Super-120B-A12B",
    "nvidia/Nemotron-Nano-V2-12b",
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
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_doctor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.cli'`

- [ ] **Step 3: Implement**

`src/waive/doctor.py`:

```python
"""Environment and connectivity checks that never print secret values."""

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import SecretStr

from waive.config import Settings
from waive.governor import BudgetExceeded

Status = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


def _key_check(name: str, value: SecretStr | None) -> Check:
    if value is None or not value.get_secret_value():
        return Check(name, "fail", "missing — add it to .env")
    return Check(name, "ok", "set")


def _zdr_check(settings: Settings) -> Check:
    if settings.zdr_confirmed:
        return Check("Zero data retention", "ok", "confirmed by operator")
    if settings.require_zdr:
        return Check(
            "Zero data retention", "warn", "not confirmed: synthetic data only (gate U0.4)"
        )
    return Check("Zero data retention", "warn", "check disabled (WAIVE_REQUIRE_ZDR=false)")


def _nebius_cli_check(which: Callable[[str], str | None], run: Callable[..., Any]) -> Check:
    path = which("nebius")
    if path is None:
        return Check("Nebius CLI", "warn", "not installed (needed in Phase 6, gate U0.5)")
    try:
        result = run([path, "profile", "list"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as error:
        return Check("Nebius CLI", "warn", f"could not run: {type(error).__name__}")
    if result.returncode == 0 and result.stdout.strip():
        return Check("Nebius CLI", "ok", "profile configured")
    return Check("Nebius CLI", "warn", "no profile: run `nebius profile create`")


def _models_check(settings: Settings, ai_factory: Callable[[], Any]) -> Check:
    try:
        available = ai_factory().list_models()
    except Exception as error:  # report the type only, never the message
        return Check("Token Factory models", "fail", f"call failed: {type(error).__name__}")
    wanted = [settings.model_reason, settings.model_fast, settings.model_vision]
    missing = [model for model in wanted if model not in available]
    if not missing:
        return Check("Token Factory models", "ok", f"{len(available)} models; all 3 configured found")
    candidates = [model for model in available if "nemotron" in model.lower()][:10]
    return Check(
        "Token Factory models",
        "fail",
        f"missing {missing}; Nemotron models available: {candidates}",
    )


def _tavily_check(tavily_factory: Callable[[], Any]) -> Check:
    try:
        hits = tavily_factory().search(
            "Massachusetts General Hospital financial assistance policy",
            purpose="doctor",
            max_results=1,
        )
    except BudgetExceeded:
        return Check("Tavily search", "fail", "credit cap reached")
    except Exception as error:  # report the type only, never the message
        return Check("Tavily search", "fail", f"call failed: {type(error).__name__}")
    return Check("Tavily search", "ok", f"{len(hits)} result(s); 1 credit")


def run_checks(
    settings: Settings,
    *,
    live: bool,
    ai_factory: Callable[[], Any] | None,
    tavily_factory: Callable[[], Any] | None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
) -> list[Check]:
    checks = [
        _key_check("NEBIUS_API_KEY", settings.nebius_api_key),
        _key_check("TAVILY_API_KEY", settings.tavily_api_key),
        Check(
            "NEBIUS_PROJECT_ID",
            "ok" if settings.nebius_project_id else "warn",
            "set" if settings.nebius_project_id else "needed for deployment (gate U0.3)",
        ),
        _zdr_check(settings),
        _nebius_cli_check(which, run),
    ]
    if live:
        if settings.nebius_api_key is not None and ai_factory is not None:
            checks.append(_models_check(settings, ai_factory))
        if settings.tavily_api_key is not None and tavily_factory is not None:
            checks.append(_tavily_check(tavily_factory))
    return checks
```

`src/waive/cli.py`:

```python
"""Command-line entry point: `uv run waive ...`."""

import typer
from rich.console import Console
from rich.table import Table

from waive.ai.client import AIClient
from waive.atlas.tavily_gateway import make_tavily_gateway
from waive.config import Settings
from waive.doctor import run_checks
from waive.governor import make_governor

app = typer.Typer(no_args_is_help=True, help="Waive operations.")
console = Console()


@app.callback()
def main() -> None:
    """Waive command-line tools."""


@app.command()
def doctor(
    live: bool = typer.Option(
        False, "--live", help="Also call Token Factory and Tavily (spends up to 1 Tavily credit)."
    ),
) -> None:
    """Check configuration and connectivity without printing secrets."""
    settings = Settings()
    governor = make_governor(settings)
    checks = run_checks(
        settings,
        live=live,
        ai_factory=lambda: AIClient(settings, governor),
        tavily_factory=lambda: make_tavily_gateway(settings, governor),
    )
    table = Table("Check", "Status", "Detail")
    for check in checks:
        table.add_row(check.name, check.status.upper(), check.detail)
    console.print(table)
    tavily_credits, _ = governor.summary()["tavily"]
    _, tf_usd = governor.summary()["token_factory"]
    console.print(f"Spend so far: {tavily_credits} Tavily credits, ${tf_usd:.4f} Token Factory")
    raise typer.Exit(code=1 if any(check.status == "fail" for check in checks) else 0)
```

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_doctor.py -v`
Expected: 6 passed

- [ ] **Step 5: Run everything**

Run: `uv run ruff check . && uv run pytest`
Expected: `All checks passed!` and all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/waive/doctor.py src/waive/cli.py tests/unit/test_doctor.py
git commit -m "feat: waive doctor checks keys, ZDR flag, Nebius CLI and live APIs"
```

---

### Task 0.7: Live connectivity check

**Blocked by:** user gates U0.1 (`NEBIUS_API_KEY` in `.env`) and U0.2 (`TAVILY_API_KEY` in `.env`).

**Files:**
- Modify (only if the live check finds different model IDs): `.env.example`, `src/waive/config.py` (model defaults), `src/waive/ai/client.py` (`PRICES_PER_MILLION` keys), `tests/unit/test_doctor.py` (`MODELS`), `tests/unit/test_ai_client.py` (`test_estimate_usd_uses_price_table`)
- Modify: `docs/PROGRESS.md`

- [ ] **Step 1: Confirm the gates are closed**

Check `docs/PROGRESS.md`: U0.1 and U0.2 must be ticked by the user. If not, stop and ask the user to complete them.

- [ ] **Step 2: Run the live doctor**

Run: `uv run waive doctor --live`
Expected: `NEBIUS_API_KEY OK`, `TAVILY_API_KEY OK`, `Token Factory models OK`, `Tavily search OK`; spend line shows 1 Tavily credit.

- [ ] **Step 3: If models are missing, update the IDs**

When `Token Factory models` fails, its detail lists the Nemotron models available. Choose:
- reason: the Nemotron 3 Super model,
- fast: the Nemotron 3 Nano 30B (or Nemotron 3.5 Lightning) model,
- vision: the Nemotron Nano 12B VL model.

Put the exact IDs into `.env`, the defaults in `src/waive/config.py`, the keys of `PRICES_PER_MILLION` (prices from https://tokenfactory.nebius.com — model catalog), the `MODELS` list in `tests/unit/test_doctor.py`, and the model name in `test_estimate_usd_uses_price_table`. Re-run `uv run pytest` and `uv run waive doctor --live` until both pass.

- [ ] **Step 4: Record the results**

In `docs/PROGRESS.md`: tick task 0.7, record the verified model IDs under "Open items to verify", add the spend row, and add a log line.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: verify Token Factory models and Tavily connectivity"
```

---

## Self-review

- **Spec coverage:** §4 cloud accounts and ZDR → 0.2, 0.4, 0.6; §6 technology → 0.1; §11 secrets and ZDR gate → 0.2, 0.4, 0.6; §15 caps → 0.3; §18 model IDs → 0.7.
- **Placeholders:** none; model IDs are defaults that task 0.7 verifies.
- **Type consistency:** `Governor.record_tavily(credits: Decimal, purpose)` and `record_token_factory(prompt_tokens, completion_tokens, usd, purpose)` are used with the same signatures in 0.4, 0.5 and 0.6.
