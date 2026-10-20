# Copied verbatim from the Kindred ID repository (eventhorizon), id/verifiers/kindred_auth.py; that file is the
# source of truth. Do not edit here: copy the newer version over this one.
"""Kindred session verifier for Python products (FastAPI / Starlette). Copy this file next to the app.

The Issuer (https://id.kindred-ai.dev) signs an ES256 JWT into the ``kindred_session`` cookie on
``Domain=kindred-ai.dev``. This module checks it against the Issuer's published keys; nothing here calls the Issuer
per request (PyJWKClient caches the keys and fetches again on an unknown key id).

Needs ``pyjwt[crypto]``. Tests sign tokens with a key they generate and monkeypatch ``jwk_client`` so no socket opens.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Iterable, Mapping, Optional, TypedDict
from urllib.parse import quote

import jwt
from jwt import PyJWKClient

COOKIE = "kindred_session"
ISSUER = "https://id.kindred-ai.dev"
AUDIENCE = "kindred-ai.dev"
RENEW_SECONDS = 86400
STATE_TYP = "kindred-oauth"  # the Issuer's ten-minute OAuth state cookie; never a session
LEEWAY_SECONDS = 60
PRODUCTION_FLAGS = (("WAIVE_ENV", "production"), ("APP_ENV", "production"), ("ENV", "production"), ("NODE_ENV", "production"))


class KindredUser(TypedDict):
    sub: str
    email: str
    exp: int


JWKS_TIMEOUT_SECONDS = 5
# An unknown key id triggers one refetch per cooldown, never one per request (a forged kid must not become a flood).
JWKS_REFETCH_COOLDOWN_SECONDS = 30

_client: Optional[PyJWKClient] = None
_keys: dict[str, Any] = {}
_fetched_at: float = 0.0


def jwk_client() -> PyJWKClient:
    global _client
    if _client is None:
        _client = PyJWKClient(f"{ISSUER}/.well-known/jwks.json", cache_keys=False, cache_jwk_set=False, timeout=JWKS_TIMEOUT_SECONDS)
    return _client


def signing_key(kid: str, now: Optional[float] = None) -> Optional[Any]:
    """The Issuer's public key for `kid` from the local cache, refetching at most once per cooldown."""
    global _keys, _fetched_at
    t = now if now is not None else time.time()
    if kid in _keys:
        return _keys[kid]
    if t - _fetched_at < JWKS_REFETCH_COOLDOWN_SECONDS:
        return None
    _fetched_at = t
    try:
        _keys = {k.key_id: k.key for k in jwk_client().get_jwk_set(refresh=True).keys if k.key_id}
    except Exception:
        return None
    return _keys.get(kid)


def verify_session(token: Optional[str]) -> Optional[KindredUser]:
    """The user in a session token, or None for anything that is not a valid Kindred session."""
    if not token:
        return None
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "ES256" or header.get("typ") == STATE_TYP or not isinstance(header.get("kid"), str):
            return None
        key = signing_key(header["kid"])
        if key is None:
            return None
        payload = jwt.decode(
            token, key, algorithms=["ES256"], audience=AUDIENCE, issuer=ISSUER,
            leeway=LEEWAY_SECONDS, options={"require": ["exp", "sub", "iss", "aud"]},
        )
    except Exception:
        return None
    sub, email, exp = payload.get("sub"), payload.get("email"), payload.get("exp")
    if not isinstance(sub, str) or not sub or not isinstance(email, str) or not email or not isinstance(exp, int):
        return None
    return {"sub": sub, "email": email, "exp": exp}


def is_local_host(host: Optional[str]) -> bool:
    if not host:
        return False
    h = host.lower()
    if h.startswith("["):
        h = h.split("]")[0] + "]"
    elif h.count(":") == 1:
        h = h.split(":")[0]
    return h in ("localhost", "127.0.0.1", "[::1]")


def bypass_user(env: Mapping[str, str] = os.environ, host: Optional[str] = None) -> Optional[KindredUser]:
    """Local development only: KINDRED_AUTH_BYPASS=<email> stands in for a session, and only for a request that
    arrived at localhost (`host` is the request's Host header). Ignored in production and on every public host."""
    email = env.get("KINDRED_AUTH_BYPASS")
    if not email or not is_local_host(host) or any(env.get(k) == v for k, v in PRODUCTION_FLAGS):
        return None
    return {"sub": "bypass", "email": email.lower(), "exp": int(time.time()) + 365 * 86400}


def needs_renewal(user: KindredUser, now: Optional[float] = None) -> bool:
    return user["exp"] - (now if now is not None else time.time()) < RENEW_SECONDS


def is_navigation(headers: Mapping[str, str]) -> bool:
    """A top-level page load (redirect to sign-in) as opposed to a fetch (401 JSON)."""
    mode = headers.get("sec-fetch-mode")
    if mode:
        return mode == "navigate"
    return "text/html" in (headers.get("accept") or "")


def read_cookie(header: Optional[str], name: str = COOKIE) -> Optional[str]:
    if not header:
        return None
    for part in header.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() == name:
            return v.strip() or None
    return None


def login_url(return_to: str) -> str:
    return f"{ISSUER}/login?return_to={quote(return_to, safe='')}"


def unauthenticated(return_to: str) -> dict[str, str]:
    return {"error": "unauthenticated", "login": login_url(return_to)}


# ── FastAPI dependency ──────────────────────────────────────────────────────

def current_user(request: Any) -> KindredUser:
    """``Depends(current_user)``: the signed-in reader, else HTTP 401 with the login address."""
    from fastapi import HTTPException

    user = bypass_user(host=request.headers.get("host")) or verify_session(request.cookies.get(COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail=unauthenticated(str(request.url)), headers={"Cache-Control": "no-store"})
    return user


# ── ASGI middleware: the Wall ───────────────────────────────────────────────

class KindredWallMiddleware:
    """Pure ASGI middleware. Every HTTP request needs a session except paths under ``exempt_prefixes``.

        app.add_middleware(KindredWallMiddleware, exempt_prefixes=("/healthz", "/static/"))
    """

    def __init__(
        self,
        app: Any,
        exempt_prefixes: Iterable[str] = ("/healthz",),
        verify: Callable[[Optional[str]], Optional[KindredUser]] = verify_session,
        env: Optional[Mapping[str, str]] = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.app = app
        self.exempt = tuple(exempt_prefixes)
        self.verify = verify
        self.env = env if env is not None else os.environ
        self.now = now

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or any(scope["path"].startswith(p) for p in self.exempt):
            await self.app(scope, receive, send)
            return
        from anyio import to_thread
        from starlette.datastructures import Headers
        from starlette.responses import JSONResponse, RedirectResponse

        headers = Headers(scope=scope)
        # Verification may fetch the key set (blocking urllib); keep it off the event loop.
        user = bypass_user(self.env, headers.get("host")) or await to_thread.run_sync(self.verify, read_cookie(headers.get("cookie")))
        nav = is_navigation(headers)
        if user is not None and not (nav and needs_renewal(user, self.now())):
            scope.setdefault("state", {})["kindred_user"] = user
            await self.app(scope, receive, send)
            return
        url = _request_url(scope, headers)
        if nav:
            response: Any = RedirectResponse(login_url(url), status_code=303)
        else:
            response = JSONResponse(unauthenticated(url), status_code=401, headers={"Cache-Control": "no-store"})
        await response(scope, receive, send)


def _request_url(scope: dict, headers: Mapping[str, str]) -> str:
    scheme = headers.get("x-forwarded-proto") or scope.get("scheme", "https")
    host = headers.get("x-forwarded-host") or headers.get("host") or ""
    query = scope.get("query_string", b"").decode()
    return f"{scheme}://{host}{scope['path']}" + (f"?{query}" if query else "")
