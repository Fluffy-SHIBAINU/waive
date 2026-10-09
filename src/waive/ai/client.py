"""Budget-aware wrapper around Nebius Token Factory (OpenAI-compatible API)."""

import json
import re
from decimal import Decimal
from typing import Any, TypeVar

import httpx
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from waive.config import Settings
from waive.governor import Governor

T = TypeVar("T", bound=BaseModel)

# Dollars per million (input, output) tokens. Verified with `waive doctor --live` on 2026-10-02.
PRICES_PER_MILLION: dict[str, tuple[Decimal, Decimal]] = {
    "nvidia/nemotron-3-super-120b-a12b": (Decimal("0.30"), Decimal("0.90")),
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": (Decimal("0.06"), Decimal("0.24")),
    "nvidia/Nemotron-3_5-Lightning": (Decimal("0.06"), Decimal("0.24")),
    "openbmb/MiniCPM-V-4_5": (Decimal("0.658"), Decimal("1.11")),
    "google/gemma-3-27b-it": (Decimal("0.10"), Decimal("0.30")),
}
# Deliberately high so unknown models never under-count spend.
FALLBACK_PRICE = (Decimal("1.00"), Decimal("3.00"))

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_EMPTY_OBJECT = re.compile(r"\{\s*\}")
_REPAIR_PROMPT = (
    "That was not valid JSON for the requested schema. Reply with only the corrected JSON object."
)
_EMPTY_PROMPT = (
    "That JSON object was empty. Fill every field the input states, as the schema describes, "
    "and reply with only the JSON object."
)


class EmptyAnswer(RuntimeError):
    """The model returned "{}"; retried once in free-text mode before being accepted."""


class ZDRRequired(RuntimeError):
    """Raised when personal data would be sent before zero data retention is confirmed."""


class AIOutputError(RuntimeError):
    """Raised when model output cannot be parsed into the requested schema."""


def estimate_usd(model: str, prompt_tokens: int, completion_tokens: int) -> Decimal:
    price_in, price_out = PRICES_PER_MILLION.get(model, FALLBACK_PRICE)
    return (price_in * prompt_tokens + price_out * completion_tokens) / Decimal(1_000_000)


def schema_hint(schema: type[BaseModel]) -> str:
    compact = json.dumps(schema.model_json_schema(), separators=(",", ":"))
    return (
        "Reply with one JSON object that matches this JSON Schema exactly. Use only these "
        f"property names; use null for anything the input does not state.\n{compact}"
    )


def with_schema_hint(
    messages: list[dict[str, Any]], schema: type[BaseModel]
) -> list[dict[str, Any]]:
    hint = schema_hint(schema)
    conversation = [dict(message) for message in messages]
    if conversation and conversation[0].get("role") == "system":
        conversation[0]["content"] = f"{conversation[0]['content']}\n\n{hint}"
    else:
        conversation.insert(0, {"role": "system", "content": hint})
    return conversation


def request_options(model: str) -> dict[str, Any]:
    """Nemotron models reason before answering by default; those thinking tokens count against
    max_tokens and can starve a long JSON answer, so thinking is switched off for them."""
    if model.lower().startswith("nvidia/"):
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    return {}


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
            "tiebreak": self._settings.model_tiebreak,
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
        conversation = with_schema_hint(messages, schema)
        last_error: Exception | None = None
        options: dict[str, Any] = {"response_format": {"type": "json_object"}}
        for attempt in range(2):
            self._governor.ensure_token_factory()
            response = self._client.chat.completions.create(
                model=model,
                messages=conversation,
                temperature=0,
                max_tokens=max_tokens,
                **options,
                **request_options(model),
            )
            usage = response.usage
            if usage is not None:
                self._governor.record_token_factory(
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    estimate_usd(model, usage.prompt_tokens, usage.completion_tokens),
                    purpose,
                )
            choice = response.choices[0]
            text = choice.message.content or ""
            if choice.finish_reason == "length":
                raise AIOutputError(f"{model} output was cut off at {max_tokens} tokens")
            try:
                payload = extract_json(text)
                if attempt == 0 and _EMPTY_OBJECT.fullmatch(payload):
                    raise EmptyAnswer("empty JSON object")
                return schema.model_validate_json(payload)
            except EmptyAnswer:
                # Nemotron Super sometimes answers "{}" in json_object mode yet fills the schema
                # when asked as free text (extract_json copes with prose and fences).
                options = {}
                repair = _EMPTY_PROMPT
            except (ValidationError, AIOutputError) as error:
                last_error = error
                repair = _REPAIR_PROMPT
            conversation = [
                *conversation,
                {"role": "assistant", "content": text},
                {"role": "user", "content": repair},
            ]
        # The answer may carry personal values (a bill amount, a name), so the error names the
        # fields and error types only and drops the chain: pydantic's text quotes input values.
        if isinstance(last_error, ValidationError):
            detail = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['type']}"
                for e in last_error.errors(include_input=False, include_url=False)
            )
        else:
            detail = str(last_error) if last_error is not None else "no answer"
        raise AIOutputError(f"model output did not match {schema.__name__} ({detail})") from None
