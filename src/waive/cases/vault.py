"""Encryption for personal fields and signed capability links (spec §11)."""

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from waive.config import Settings

Scope = Literal["senior", "caregiver"]


def new_key() -> str:
    return base64.b64encode(os.urandom(32)).decode("ascii")


class FieldCipher:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("vault key must be 32 bytes")
        self._aead = AESGCM(key)

    def encrypt(self, obj: dict) -> str:
        nonce = os.urandom(12)
        data = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
        return base64.b64encode(nonce + self._aead.encrypt(nonce, data, None)).decode("ascii")

    def decrypt(self, blob: str) -> dict:
        try:
            raw = base64.b64decode(blob)
            data = self._aead.decrypt(raw[:12], raw[12:], None)
        except (InvalidTag, ValueError) as error:
            raise ValueError("sealed data is corrupt or the key is wrong") from error
        return json.loads(data)


def cipher_from_settings(settings: Settings) -> FieldCipher:
    if settings.vault_key is None:
        raise RuntimeError("WAIVE_VAULT_KEY is not set; run `uv run waive keygen`")
    return FieldCipher(base64.b64decode(settings.vault_key.get_secret_value()))


@dataclass(frozen=True)
class TokenClaims:
    case_id: str
    scope: Scope
    generation: int
    expires_at: datetime


def keyed_hash(secret: bytes, message: bytes) -> str:
    """HMAC-SHA256 of `message` under `secret`, as hex: a one-way identifier that cannot be
    recomputed, or matched against a guess, without the secret."""
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


class TokenSigner:
    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError("token secret must be at least 32 characters")
        self._secret = secret.encode("utf-8")

    def keyed_hash(self, message: bytes) -> str:
        """`keyed_hash` under the token secret, which stays in here (auth/accounts.py hashes a
        provider's subject id this way). A link's signed part is URL-safe base64 and never holds
        a zero byte, so a message must hold one: this can never be made to sign a link."""
        if b"\x00" not in message:
            raise ValueError("a keyed hash message must be set apart with a zero byte")
        return keyed_hash(self._secret, message)

    def _sign(self, payload: bytes) -> str:
        return (
            base64.urlsafe_b64encode(hmac.new(self._secret, payload, hashlib.sha256).digest())
            .decode()
            .rstrip("=")
        )

    def mint(
        self,
        case_id: str,
        scope: Scope,
        generation: int,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> str:
        now = now or datetime.now(UTC)
        claims = {"c": case_id, "s": scope, "g": generation, "e": int((now + ttl).timestamp())}
        payload = (
            base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode())
            .decode()
            .rstrip("=")
        )
        return f"{payload}.{self._sign(payload.encode())}"

    def verify(self, token: str, now: datetime | None = None) -> TokenClaims | None:
        now = now or datetime.now(UTC)
        try:
            payload, signature = token.split(".", 1)
        except ValueError:
            return None
        if not hmac.compare_digest(signature, self._sign(payload.encode())):
            return None
        padded = payload + "=" * (-len(payload) % 4)
        try:
            claims = json.loads(base64.urlsafe_b64decode(padded))
        except (ValueError, json.JSONDecodeError):
            return None
        expires_at = datetime.fromtimestamp(claims["e"], tz=UTC)
        if expires_at <= now or claims["s"] not in ("senior", "caregiver"):
            return None
        return TokenClaims(claims["c"], claims["s"], int(claims["g"]), expires_at)


def signer_from_settings(settings: Settings) -> TokenSigner:
    if settings.token_secret is None:
        raise RuntimeError("WAIVE_TOKEN_SECRET is not set; run `uv run waive keygen`")
    return TokenSigner(settings.token_secret.get_secret_value())
