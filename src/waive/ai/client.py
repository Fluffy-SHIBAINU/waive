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
_REPAIR_PROMPT = (
    "That was not valid JSON for the requested schema. Reply with only the corrected JSON object."
)


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
        # openai>=3 defaults to its vendored `httpx2` stack, which respx cannot intercept; a
        # standard httpx.Client keeps the transport mockable so tests never reach the network.
        if http_client is None:
            http_client = httpx.Client()
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
