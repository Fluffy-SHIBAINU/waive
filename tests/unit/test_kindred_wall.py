"""The Kindred ID wall: waive.kindred-ai.dev answers its reading pages to anyone and everything
else only with the family's session cookie (design: eventhorizon/.claude/features/kindred-id.md
§9.5 and the Kindred connect design §5.5; verifier copied verbatim into web/kindred_auth.py,
Waive's two rules on top in web/wall.py). Tokens are signed with a key generated here and the
verifier's key lookup is stood in for, so no socket opens: the suite runs with --disable-socket."""

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import quote

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.config import Settings
from waive.db import session_scope
from waive.web import kindred_auth
from waive.web.kindred_auth import KindredWallMiddleware
from waive.web.wall import Wall

from tests.unit.test_web_app import make_client
from tests.unit.test_web_senior import HOSPITAL

pytestmark = pytest.mark.kindred_wall

KID = "test-key"
PRIVATE = ec.generate_private_key(ec.SECP256R1())
PUBLIC = PRIVATE.public_key()
PUBLIC_PEM = PUBLIC.public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)
LOGIN = "https://id.kindred-ai.dev/login?return_to="
NAV = {"sec-fetch-mode": "navigate", "accept": "text/html"}
EMAIL = "reader@example.com"
# The module is imported at collection, before any fixture patches the verifier.
REAL_SIGNING_KEY = kindred_auth.signing_key


@pytest.fixture(autouse=True)
def issuer_key(monkeypatch):
    """The Issuer's key set as the verifier holds it after one fetch."""
    monkeypatch.setattr(
        kindred_auth, "signing_key", lambda kid, now=None: PUBLIC if kid == KID else None
    )


def token(headers=None, **claims) -> str:
    now = int(time.time())
    payload = {
        "iss": kindred_auth.ISSUER,
        "aud": kindred_auth.AUDIENCE,
        "sub": "google-sub-1",
        "email": EMAIL,
        "iat": now,
        "exp": now + 6 * 86400,
        **claims,
    }
    return jwt.encode(payload, PRIVATE, algorithm="ES256", headers={"kid": KID, **(headers or {})})


def cookie(value: str) -> dict[str, str]:
    return {"cookie": f"other=1; {kindred_auth.COOKIE}={value}"}


def client(**overrides) -> TestClient:
    overrides.setdefault("kindred_auth_bypass", None)
    c, _ = make_client(**overrides)
    return c


def seeded_client(**overrides) -> TestClient:
    """The real wall in front of an app that knows the example hospital and its sheet."""
    c = client(**overrides)
    with session_scope(c.app.state.deps.engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, st_example_sheet())
    return c


def get(c: TestClient, path: str, **headers):
    return c.get(path, headers=headers, follow_redirects=False)


# ── without a session ───────────────────────────────────────────────────────


def test_a_page_load_without_a_session_goes_to_the_issuer_with_the_request_url():
    c = client()
    page = get(c, "/account?tab=cases", **NAV)
    assert page.status_code == 303
    assert page.headers["location"] == LOGIN + quote("http://testserver/account?tab=cases", safe="")
    # Behind Caddy the public name and scheme arrive in the forwarded headers.
    forwarded = get(
        c,
        "/account",
        **NAV,
        **{"x-forwarded-host": "waive.kindred-ai.dev", "x-forwarded-proto": "https"},
    )
    assert forwarded.status_code == 303
    assert forwarded.headers["location"] == LOGIN + "https%3A%2F%2Fwaive.kindred-ai.dev%2Faccount"
    # Accept: text/html counts as a page load when the browser sends no Sec-Fetch-Mode.
    assert get(c, "/admin", accept="text/html,*/*").status_code == 303


def test_a_fetch_without_a_session_gets_401_json_that_is_never_cached():
    c = client()
    for headers in (
        {},
        {"sec-fetch-mode": "cors", "accept": "text/html"},
        {"accept": "application/json"},
    ):
        answer = get(c, "/metrics.json", **headers)
        assert answer.status_code == 401, headers
        assert answer.headers["cache-control"] == "no-store"
        assert answer.json() == {
            "error": "unauthenticated",
            "login": LOGIN + quote("http://testserver/metrics.json", safe=""),
        }


def test_the_wall_is_outermost_so_a_body_is_never_read_before_the_session_check():
    c = client()
    cap = c.app.state.settings.max_upload_bytes
    oversized = {"photo": ("bill.jpg", b"x" * (cap + 1), "image/jpeg")}
    assert c.post("/account/delete", files=oversized).status_code == 401
    assert c.post("/admin/login", data={"token": "x"}).status_code == 401


def test_health_and_static_files_stay_open():
    c = client()
    assert get(c, "/healthz", **NAV).json() == {"ok": True}
    assert get(c, "/static/waive.css").status_code == 200


def test_the_exempt_prefixes_come_from_the_setting():
    assert Settings(_env_file=None).wall_exempt == (
        "/",
        "/atlas",
        "/start",
        "/cases",
        "/s/",
        "/c/",
        "/stats",
        "/healthz",
        "/robots.txt",
        "/favicon.ico",
        "/static/",
    )
    opened = client(wall_exempt_prefixes=" /healthz, /static/ ,/atlas,, ")
    assert opened.app.state.settings.wall_exempt == ("/healthz", "/static/", "/atlas")
    assert get(opened, "/atlas", **NAV).status_code == 200
    assert get(opened, "/metrics.json").status_code == 401
    closed = client(wall_exempt_prefixes="/static/")
    assert get(closed, "/healthz").status_code == 401


def test_the_exempt_setting_reads_the_environment(monkeypatch):
    monkeypatch.setenv("WAIVE_WALL_EXEMPT_PREFIXES", "/healthz,/static/,/s/,/c/")
    assert Settings(_env_file=None).wall_exempt == ("/healthz", "/static/", "/s/", "/c/")


# ── open for reading (design §5.5) ──────────────────────────────────────────


def test_reading_needs_no_session_but_the_account_pages_and_the_console_do():
    """The home page, the atlas, the confirm page and the start behind it, the case's own pages
    under its link, the public counts, health, robots and static files answer without a
    session; the account pages, the console and the metrics still send a page load to the
    Issuer and a fetch to 401."""
    c = seeded_client()
    for path in (
        "/",
        "/atlas",
        "/atlas/229999",
        "/atlas/229999.json",
        "/start?ccn=229999",
        "/stats",
        "/healthz",
        "/robots.txt",
        "/static/waive.css",
    ):
        assert get(c, path, **NAV).status_code == 200, path
        assert get(c, path).status_code == 200, path
    started = c.post("/start", data={"ccn": "229999", "source": "ccn_link"}, follow_redirects=False)
    assert started.status_code == 303
    assert get(c, started.headers["location"], **NAV).status_code == 200
    assert (
        get(c, "/s/not-a-real-token", **NAV).status_code == 403
    )  # the link is checked, not a session
    assert c.post("/cases", data={"state": "MA"}, follow_redirects=False).status_code == 303
    for path in ("/account", "/admin", "/admin/login", "/metrics", "/metrics.json"):
        assert get(c, path, **NAV).status_code == 303, path
        assert get(c, path).status_code == 401, path
    assert c.post("/account/delete").status_code == 401


def test_an_open_page_still_knows_a_signed_in_reader():
    """The wall stands down on an open path, but a valid session still names the reader: the
    home page lists their cases. Without a session, or with a bad one, the same page answers
    with nobody's."""
    c = seeded_client()
    mine = {**cookie(token()), "sec-fetch-site": "same-origin"}
    started = c.post(
        "/start", data={"ccn": "229999", "source": "ccn_link"}, headers=mine, follow_redirects=False
    )
    assert started.status_code == 303
    home = get(c, "/", **NAV, **cookie(token()))
    assert home.status_code == 200 and "Your cases" in home.text
    assert home.headers["cache-control"] == "no-store"
    for headers in ({}, cookie(token(exp=int(time.time()) - 3600)), cookie("not-a-token")):
        page = get(c, "/", **NAV, **headers)
        assert page.status_code == 200 and "Your cases" not in page.text, headers
    # The case page under its link knows the reader too: started from this site by a signed-in
    # reader, the case is in their history already; without a session the page makes no offer.
    link = started.headers["location"].rsplit("/", 1)[0]
    assert "Saved to your account" in get(c, link, **cookie(token())).text
    anonymous = get(c, link).text
    assert "Saved to your account" not in anonymous and "Save this case" not in anonymous


def test_the_root_entry_opens_the_home_page_alone():
    """ "/" in the exempt list is the home page, never a prefix; a prefix stays a prefix."""
    seen = {}

    async def inner(scope, receive, send):
        seen["user"] = scope.get("state", {}).get("kindred_user")
        await PlainTextResponse("ok")(scope, receive, send)

    walled = TestClient(
        Wall(inner, exempt_prefixes=("/", "/open"), env={"WAIVE_ENV": "development"})
    )
    assert walled.get("/").status_code == 200 and seen["user"] is None
    assert walled.get("/open/anything").status_code == 200
    assert walled.get("/closed", headers=NAV, follow_redirects=False).status_code == 303
    assert walled.get("/closed").status_code == 401
    assert walled.get("/", headers=cookie(token())).status_code == 200
    assert seen["user"]["email"] == EMAIL
    assert walled.get("/", headers=cookie("garbage")).status_code == 200 and seen["user"] is None
    bare = TestClient(Wall(inner, exempt_prefixes=("/open",), env={"WAIVE_ENV": "development"}))
    assert bare.get("/", headers=NAV, follow_redirects=False).status_code == 303


# ── with a session ──────────────────────────────────────────────────────────


def test_a_valid_session_reaches_the_atlas_and_the_sign_out_line():
    c = client()
    page = get(c, "/atlas", **NAV, **cookie(token()))
    assert page.status_code == 200
    assert 'action="https://id.kindred-ai.dev/logout"' in page.text
    assert '<input type="hidden" name="return_to" value="https://www.kindred-ai.dev/">' in page.text
    assert "Signed in to Kindred" in page.text and "Sign out" in page.text
    # Every Kindred product links back to the family's site from its header.
    assert '<a href="https://kindred-ai.dev/" class="muted">Kindred AI</a>' in page.text
    assert EMAIL not in page.text
    assert get(c, "/metrics.json", **cookie(token())).status_code == 200


def test_an_expired_session_is_refused():
    c = client()
    old = token(exp=int(time.time()) - 3600)
    assert get(c, "/account", **NAV, **cookie(old)).status_code == 303
    assert get(c, "/metrics.json", **cookie(old)).status_code == 401


def test_a_session_about_to_expire_renews_on_a_page_load_but_still_serves_fetches():
    c = client()
    soon = token(exp=int(time.time()) + 3600)
    assert get(c, "/account", **NAV, **cookie(soon)).status_code == 303
    assert get(c, "/metrics.json", **cookie(soon)).status_code == 200


def test_the_issuers_oauth_state_cookie_is_never_a_session():
    c = client()
    state = token(headers={"typ": "kindred-oauth"})
    assert get(c, "/metrics.json", **cookie(state)).status_code == 401
    assert get(c, "/metrics.json", **cookie(token(headers={"typ": "JWT"}))).status_code == 200


def test_hs256_with_the_public_key_as_the_secret_is_refused():
    """Algorithm confusion: a token HMAC-signed with the Issuer's public key must not verify.
    PyJWT refuses to HMAC with a public key, so the token is assembled by hand."""

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
    now = int(time.time())
    payload = b64(
        json.dumps(
            {
                "iss": kindred_auth.ISSUER,
                "aud": kindred_auth.AUDIENCE,
                "sub": "google-sub-1",
                "email": EMAIL,
                "iat": now,
                "exp": now + 86400,
            }
        ).encode()
    )
    signature = b64(hmac.new(PUBLIC_PEM, f"{header}.{payload}".encode(), hashlib.sha256).digest())
    forged = f"{header}.{payload}.{signature}"
    assert kindred_auth.verify_session(forged) is None
    assert get(client(), "/metrics.json", **cookie(forged)).status_code == 401


def test_wrong_audience_issuer_or_key_is_refused():
    other = ec.generate_private_key(ec.SECP256R1())
    now = int(time.time())
    claims = {
        "iss": kindred_auth.ISSUER,
        "aud": kindred_auth.AUDIENCE,
        "sub": "s",
        "email": EMAIL,
        "iat": now,
        "exp": now + 86400,
    }
    foreign = jwt.encode(claims, other, algorithm="ES256", headers={"kid": KID})
    assert kindred_auth.verify_session(foreign) is None
    assert kindred_auth.verify_session(token(aud="tender.example")) is None
    assert kindred_auth.verify_session(token(iss="https://id.example.com")) is None
    assert kindred_auth.verify_session(token(email="")) is None
    assert kindred_auth.verify_session(token(headers={"kid": "unknown"})) is None
    good = token()
    assert kindred_auth.verify_session(good) == {
        "sub": "google-sub-1",
        "email": EMAIL,
        "exp": jwt.decode(good, options={"verify_signature": False})["exp"],
    }


def test_a_cached_key_verifies_without_a_socket_and_an_unknown_key_fails_closed(monkeypatch):
    """The real key lookup with the Issuer's key already cached: no fetch, so no socket (the
    suite forbids them). An unknown key id inside the refetch cooldown is refused without a
    fetch; outside it one fetch is attempted, and when the Issuer does not answer the verifier
    fails closed for that key id and keeps the keys it has."""
    monkeypatch.setattr(kindred_auth, "signing_key", REAL_SIGNING_KEY)
    monkeypatch.setattr(kindred_auth, "_keys", {KID: PUBLIC})
    monkeypatch.setattr(kindred_auth, "_fetched_at", time.time())
    fetches = []

    class Unreachable:
        def get_jwk_set(self, refresh=False):
            fetches.append(refresh)
            raise OSError("the Issuer did not answer")

    monkeypatch.setattr(kindred_auth, "jwk_client", lambda: Unreachable())
    c = client()
    assert get(c, "/metrics.json", **cookie(token())).status_code == 200
    rotated = cookie(token(headers={"kid": "rotated"}))
    assert get(c, "/metrics.json", **rotated).status_code == 401
    assert fetches == []
    monkeypatch.setattr(kindred_auth, "_fetched_at", 0.0)
    assert get(c, "/metrics.json", **rotated).status_code == 401
    assert get(c, "/metrics.json", **rotated).status_code == 401
    assert fetches == [True]  # one fetch per cooldown, however many unknown ids arrive
    assert get(c, "/metrics.json", **cookie(token())).status_code == 200


# ── the local bypass ────────────────────────────────────────────────────────


def test_the_bypass_counts_only_for_a_localhost_host_outside_production():
    dev = client(kindred_auth_bypass="Dev@Kindred-ai.dev")
    assert dev.app.state.settings.wall_env == {
        "WAIVE_ENV": "development",
        "KINDRED_AUTH_BYPASS": "Dev@Kindred-ai.dev",
    }
    assert get(dev, "/account", **NAV, host="localhost:8000").status_code == 200
    assert get(dev, "/account", **NAV, host="127.0.0.1:8000").status_code == 200
    assert get(dev, "/account", **NAV, host="waive.kindred-ai.dev").status_code == 303
    assert get(dev, "/account", **NAV).status_code == 303  # Host: testserver
    assert (
        get(client(), "/account", **NAV, host="localhost:8000").status_code == 303
    )  # no address set

    production = client(
        env="production",
        database_url="postgresql+psycopg://waive:pw@db.example.net:5432/waive",
        ledger_backend="db",
        kindred_auth_bypass="dev@kindred-ai.dev",
    )
    assert production.app.state.settings.wall_env["WAIVE_ENV"] == "production"
    assert get(production, "/account", **NAV, host="localhost:8000").status_code == 303
    assert get(production, "/metrics.json", host="localhost:8000").status_code == 401
    assert get(production, "/healthz", host="localhost:8000").status_code == 200


def test_the_middleware_alone_sets_the_user_on_the_request_state():
    seen = {}

    async def inner(scope, receive, send):
        seen["user"] = scope.get("state", {}).get("kindred_user")
        await PlainTextResponse("ok")(scope, receive, send)

    walled = TestClient(
        KindredWallMiddleware(
            inner,
            exempt_prefixes=("/open",),
            env={"WAIVE_ENV": "development", "KINDRED_AUTH_BYPASS": ""},
        )
    )
    assert walled.get("/open").status_code == 200 and seen["user"] is None
    assert walled.get("/closed", headers=cookie(token())).status_code == 200
    assert seen["user"] == {"sub": "google-sub-1", "email": EMAIL, "exp": seen["user"]["exp"]}
    assert walled.get("/closed", headers=NAV, follow_redirects=False).status_code == 303
