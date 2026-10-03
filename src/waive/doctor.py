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
    wanted = [
        settings.model_reason,
        settings.model_fast,
        settings.model_tiebreak,
        settings.model_vision,
    ]
    missing = [model for model in wanted if model not in available]
    if not missing:
        return Check(
            "Token Factory models",
            "ok",
            f"{len(available)} models; all {len(wanted)} configured found",
        )
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
