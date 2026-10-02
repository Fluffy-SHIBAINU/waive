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
