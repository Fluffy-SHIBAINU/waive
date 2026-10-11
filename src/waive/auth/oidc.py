"""Sign-in with Google: the OpenID Connect authorization-code flow with PKCE (Phase 9).

Only the `openid` scope is asked for, so Google returns a subject id and nothing about the
person: no email, name or picture. The ID token is checked against Google's published keys
(RS256 signature, issuer, audience, expiry, nonce) before its subject is believed.

Nothing here logs or raises with a code, a state, a nonce, a verifier, a token, the client secret
or a subject id in it: a failure is a `SignInError` carrying one fixed word."""

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
SCOPE = "openid"
ALGORITHM = "RS256"
# Google rotates its signing keys every few days; ten minutes keeps a busy hour to a handful of
# fetches, and an unknown key id always gets one fresh look.
JWKS_TTL_SECONDS = 600
# Allowed difference between this machine's clock and Google's.
CLOCK_LEEWAY_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 10.0
MAX_SUBJECT_CHARS = 255


class SignInError(Exception):
    """Sign-in could not be completed. `reason` is one fixed word, safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Attempt:
    """What one sign-in attempt must remember between leaving for Google and coming back."""

    state: str
    nonce: str
    verifier: str


def new_attempt() -> Attempt:
    # 32 random bytes each: 43 URL-safe characters, the shortest PKCE verifier RFC 7636 allows.
    return Attempt(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        verifier=secrets.token_urlsafe(32),
    )


def code_challenge(verifier: str) -> str:
    """PKCE S256: Google gets the hash now and the verifier only with the code."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class GoogleSignIn:
    """One Google OAuth client. Every request goes through the `http` client it is given."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        http: httpx.Client,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: dict[str, Any] = {}
        self._keys_fetched_at: float | None = None

    def close(self) -> None:
        self._http.close()

    def authorization_url(self, attempt: Attempt, redirect_uri: str) -> str:
        query = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "state": attempt.state,
            "nonce": attempt.nonce,
            "code_challenge": code_challenge(attempt.verifier),
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZATION_ENDPOINT}?{urlencode(query)}"

    def exchange_code(self, code: str, verifier: str, redirect_uri: str) -> str:
        """Trade the code for the ID token. `redirect_uri` must be the one the attempt left with."""
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
        try:
            response = self._http.post(TOKEN_ENDPOINT, data=form, timeout=HTTP_TIMEOUT_SECONDS)
        except httpx.HTTPError:
            raise SignInError("token_endpoint_unreachable") from None
        if response.status_code != 200:
            raise SignInError("token_endpoint_refused")
        try:
            body = response.json()
        except ValueError:
            raise SignInError("token_endpoint_unreadable") from None
        token = body.get("id_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise SignInError("no_id_token")
        return token

    def _fetch_keys(self) -> dict[str, Any]:
        try:
            response = self._http.get(JWKS_URL, timeout=HTTP_TIMEOUT_SECONDS)
            if response.status_code != 200:
                raise SignInError("keys_unavailable")
            found = jwt.PyJWKSet.from_dict(response.json())
            keys = {
                key.key_id: key.key
                for key in found.keys
                if key.key_id and key.key_type == "RSA" and key.public_key_use in (None, "sig")
            }
        except (httpx.HTTPError, jwt.PyJWTError, ValueError, TypeError, AttributeError):
            raise SignInError("keys_unavailable") from None
        if not keys:
            raise SignInError("keys_unavailable")
        self._keys, self._keys_fetched_at = keys, self._clock()
        return keys

    def _signing_key(self, key_id: object) -> Any:
        if not isinstance(key_id, str) or not key_id:
            raise SignInError("id_token_key")
        with self._lock:
            fetched_at = self._keys_fetched_at
            fresh = fetched_at is not None and self._clock() - fetched_at < JWKS_TTL_SECONDS
            keys = self._keys if fresh else self._fetch_keys()
            if key_id not in keys and fresh:
                keys = self._fetch_keys()  # Google rotated its keys since the last look
        if key_id not in keys:
            raise SignInError("id_token_key")
        return keys[key_id]

    def verify_id_token(self, token: str, nonce: str) -> str:
        """The subject id of an ID token that passes every check, or `SignInError`."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise SignInError("id_token_malformed") from None
        # The algorithm is fixed here, never taken from the token: a token that names another
        # one ("none", or HS256 with the public key as the secret) is refused before any key.
        if header.get("alg") != ALGORITHM:
            raise SignInError("id_token_algorithm")
        key = self._signing_key(header.get("kid"))
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                audience=self._client_id,
                issuer=ISSUERS,
                leeway=CLOCK_LEEWAY_SECONDS,
                options={"require": ["iss", "aud", "sub", "exp", "iat"], "strict_aud": True},
            )
        except jwt.ExpiredSignatureError:
            raise SignInError("id_token_expired") from None
        except jwt.InvalidAudienceError:
            raise SignInError("id_token_audience") from None
        except jwt.InvalidIssuerError:
            raise SignInError("id_token_issuer") from None
        except jwt.InvalidSignatureError:
            raise SignInError("id_token_signature") from None
        except jwt.PyJWTError:
            raise SignInError("id_token_invalid") from None
        if claims.get("azp", self._client_id) != self._client_id:
            raise SignInError("id_token_party")
        found = claims.get("nonce")
        if not nonce or not isinstance(found, str):
            raise SignInError("id_token_nonce")
        if not hmac.compare_digest(found.encode("utf-8"), nonce.encode("utf-8")):
            raise SignInError("id_token_nonce")
        subject = claims["sub"]
        if not isinstance(subject, str) or not 0 < len(subject) <= MAX_SUBJECT_CHARS:
            raise SignInError("id_token_subject")
        return subject

    def subject(self, code: str, attempt: Attempt, redirect_uri: str) -> str:
        """The whole return trip: code in, verified subject id out."""
        token = self.exchange_code(code, attempt.verifier, redirect_uri)
        return self.verify_id_token(token, attempt.nonce)
