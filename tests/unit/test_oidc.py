"""Phase 9, task 9.7: the Google side of sign-in. OpenID Connect authorization-code flow with
PKCE; the ID token is checked against Google's published keys. Every call to Google goes through
respx and the keys are made here: nothing in this file reaches the network."""

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from waive.auth.oidc import (
    AUTHORIZATION_ENDPOINT,
    ISSUERS,
    JWKS_TTL_SECONDS,
    JWKS_URL,
    TOKEN_ENDPOINT,
    Attempt,
    GoogleSignIn,
    SignInError,
    code_challenge,
    new_attempt,
)

CLIENT_ID = "waive-test.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret-value"
REDIRECT_URI = "http://testserver/auth/google/callback"
SUBJECT = "108204268033311374519"
KID = "key-1"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
URL_SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def jwks(*keys):
    """Google's key set as its certs endpoint serves it; by default the one test key."""
    keys = keys or ((KID, PRIVATE_KEY),)
    return {
        "keys": [
            {
                **jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True),
                "kid": kid,
                "alg": "RS256",
                "use": "sig",
            }
            for kid, key in keys
        ]
    }


def claims(nonce, **changes):
    now = int(time.time())
    found = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "azp": CLIENT_ID,
        "sub": SUBJECT,
        "iat": now,
        "exp": now + 3600,
        "nonce": nonce,
    }
    found.update(changes)
    return {name: value for name, value in found.items() if value is not None}


def id_token(nonce, *, key=PRIVATE_KEY, kid=KID, **changes):
    return jwt.encode(claims(nonce, **changes), key, algorithm="RS256", headers={"kid": kid})


def mock_google(token=None, *, keys=None, token_status=200):
    """Answer the token endpoint and the key set. Returns the two routes."""
    body = {"access_token": "at", "expires_in": 3599, "scope": "openid", "token_type": "Bearer"}
    if token is not None:
        body["id_token"] = token
    if token_status != 200:
        body = {"error": "invalid_grant", "error_description": "Bad Request"}
    exchange = respx.post(TOKEN_ENDPOINT).mock(return_value=httpx.Response(token_status, json=body))
    certs = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=keys or jwks()))
    return exchange, certs


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def google(clock=None):
    return GoogleSignIn(CLIENT_ID, CLIENT_SECRET, httpx.Client(), clock=clock or Clock())


def reason(call):
    with pytest.raises(SignInError) as caught:
        call()
    return caught.value.reason


def test_each_attempt_has_its_own_random_state_nonce_and_verifier():
    first, second = new_attempt(), new_attempt()
    values = [first.state, first.nonce, first.verifier, second.state, second.nonce, second.verifier]
    assert len(set(values)) == 6
    for value in values:
        assert len(value) >= 43 and set(value) <= URL_SAFE
    assert 43 <= len(first.verifier) <= 128  # RFC 7636
    digest = hashlib.sha256(first.verifier.encode("ascii")).digest()
    assert code_challenge(first.verifier) == base64.urlsafe_b64encode(digest).decode().rstrip("=")


def test_the_authorization_url_asks_google_for_an_openid_code_and_nothing_about_the_person():
    attempt = new_attempt()
    url = google().authorization_url(attempt, REDIRECT_URI)
    assert url.startswith(AUTHORIZATION_ENDPOINT + "?")
    assert AUTHORIZATION_ENDPOINT == "https://accounts.google.com/o/oauth2/v2/auth"
    query = {name: values for name, values in parse_qs(urlsplit(url).query).items()}
    assert query == {
        "client_id": [CLIENT_ID],
        "redirect_uri": [REDIRECT_URI],
        "response_type": ["code"],
        "scope": ["openid"],  # no email, no profile
        "state": [attempt.state],
        "nonce": [attempt.nonce],
        "code_challenge": [code_challenge(attempt.verifier)],
        "code_challenge_method": ["S256"],
    }
    assert attempt.verifier not in url and CLIENT_SECRET not in url


@respx.mock
def test_the_code_is_exchanged_with_the_verifier_and_the_client_secret():
    exchange, _ = mock_google("the-id-token")
    assert google().exchange_code("the-code", "the-verifier", REDIRECT_URI) == "the-id-token"
    assert TOKEN_ENDPOINT == "https://oauth2.googleapis.com/token"
    [call] = exchange.calls
    form = {name: values[0] for name, values in parse_qs(call.request.content.decode()).items()}
    assert form == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": "the-verifier",
    }
    assert call.request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert "the-code" not in str(call.request.url)  # in the body, never in a URL


@respx.mock
def test_a_token_endpoint_that_refuses_fails_or_answers_nonsense_is_a_sign_in_error():
    provider = google()
    respx.post(TOKEN_ENDPOINT).mock(
        side_effect=[
            httpx.Response(400, json={"error": "invalid_grant"}),
            httpx.Response(500, text="oops"),
            httpx.ConnectError("no route"),
            httpx.Response(200, text="<html>not json</html>"),
            httpx.Response(200, json={"access_token": "at"}),
            httpx.Response(200, json=["id_token"]),
            httpx.Response(200, json={"id_token": 7}),
        ]
    )
    reasons = [reason(lambda: provider.exchange_code("c", "v", REDIRECT_URI)) for _ in range(7)]
    assert reasons == [
        "token_endpoint_refused",
        "token_endpoint_refused",
        "token_endpoint_unreachable",
        "token_endpoint_unreadable",
        "no_id_token",
        "no_id_token",
        "no_id_token",
    ]


@respx.mock
def test_a_good_id_token_gives_the_subject_id():
    mock_google()
    provider = google()
    assert JWKS_URL == "https://www.googleapis.com/oauth2/v3/certs"
    assert ISSUERS == ("https://accounts.google.com", "accounts.google.com")
    assert provider.verify_id_token(id_token("n-1"), "n-1") == SUBJECT
    # Google signs with either spelling of its name.
    assert provider.verify_id_token(id_token("n-1", iss="accounts.google.com"), "n-1") == SUBJECT
    # Extra claims a wider scope would bring are simply not looked at.
    extra = id_token("n-1", email="rosa@example.org", name="Rosa", picture="https://x/y.png")
    assert provider.verify_id_token(extra, "n-1") == SUBJECT


@respx.mock
def test_the_whole_exchange_returns_only_the_subject():
    attempt = Attempt(state="s", nonce="n-1", verifier="v" * 43)
    exchange, certs = mock_google(id_token("n-1"))
    assert google().subject("the-code", attempt, REDIRECT_URI) == SUBJECT
    assert exchange.call_count == 1 and certs.call_count == 1


@respx.mock
def test_id_tokens_that_fail_a_check_are_refused():
    mock_google()
    provider = google()
    now = int(time.time())
    payload = base64.urlsafe_b64encode(
        json.dumps(claims("n-1", sub="someone-else")).encode()
    ).decode()
    good = id_token("n-1")
    header, _, signature = good.split(".")
    public_pem = PRIVATE_KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    unsigned = jwt.encode(claims("n-1"), None, algorithm="none", headers={"kid": KID})
    refused = {
        "id_token_nonce": id_token("another-nonce"),
        "id_token_audience": id_token("n-1", aud="someone-else.apps.googleusercontent.com"),
        "id_token_issuer": id_token("n-1", iss="https://accounts.example.com"),
        "id_token_expired": id_token("n-1", iat=now - 7200, exp=now - 3600),
        "id_token_signature": id_token("n-1", key=OTHER_KEY),
        "id_token_malformed": "not-a-token",
        "id_token_algorithm": unsigned,
        "id_token_key": id_token("n-1", kid="unknown-key"),
        "id_token_subject": id_token("n-1", sub=""),
        "id_token_party": id_token("n-1", azp="someone-else.apps.googleusercontent.com"),
    }
    for expected, token in refused.items():
        assert reason(lambda token=token: provider.verify_id_token(token, "n-1")) == expected
    # A payload swapped under a real signature.
    assert (
        reason(
            lambda: provider.verify_id_token(f"{header}.{payload.rstrip('=')}.{signature}", "n-1")
        )
        == "id_token_signature"
    )
    # More than our client as the audience, a missing nonce, a missing expiry: all refused.
    for token in (
        id_token("n-1", aud=[CLIENT_ID, "someone-else"]),
        id_token(None),  # no nonce in the token at all
        id_token("n-1", exp=None),
        id_token("n-1", sub=None),
        id_token("n-1", iat=now + 3600, exp=now + 7200),  # not valid yet
    ):
        assert reason(lambda token=token: provider.verify_id_token(token, "n-1")).startswith(
            "id_token_"
        )
    # A token "signed" with the public key as a shared secret (algorithm confusion).
    forged = jwt.api_jws.PyJWS().encode(
        json.dumps(claims("n-1")).encode(),
        hashlib.sha256(public_pem).digest(),
        algorithm="HS256",
        headers={"kid": KID},
    )
    assert reason(lambda: provider.verify_id_token(forged, "n-1")) == "id_token_algorithm"
    # An empty nonce never matches, even against a token without one.
    assert reason(lambda: provider.verify_id_token(id_token(None), "")) == "id_token_nonce"
    assert reason(lambda: provider.verify_id_token(id_token(""), "")) == "id_token_nonce"


@respx.mock
def test_the_key_set_is_cached_for_a_short_time_and_fetched_again_when_google_rotates():
    clock = Clock()
    provider = google(clock)
    certs = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks()))
    for _ in range(3):
        assert provider.verify_id_token(id_token("n"), "n") == SUBJECT
    assert certs.call_count == 1
    assert 60 <= JWKS_TTL_SECONDS <= 3600
    clock.now += JWKS_TTL_SECONDS + 1
    assert provider.verify_id_token(id_token("n"), "n") == SUBJECT
    assert certs.call_count == 2
    # A key id the cache does not know: one fresh look, which finds the rotated key.
    certs.mock(return_value=httpx.Response(200, json=jwks(("key-2", OTHER_KEY))))
    rotated = id_token("n", key=OTHER_KEY, kid="key-2")
    assert provider.verify_id_token(rotated, "n") == SUBJECT
    assert certs.call_count == 3
    # The old key is gone with the rotation; an unknown key costs one look, not a loop.
    assert reason(lambda: provider.verify_id_token(id_token("n"), "n")) == "id_token_key"
    assert certs.call_count == 4


@respx.mock
def test_a_key_set_that_cannot_be_fetched_or_read_is_a_sign_in_error():
    provider = google()
    respx.get(JWKS_URL).mock(
        side_effect=[
            httpx.Response(503, text="unavailable"),
            httpx.ConnectError("no route"),
            httpx.Response(200, text="not json"),
            httpx.Response(200, json={"keys": []}),
            httpx.Response(200, json={"nothing": "here"}),
        ]
    )
    for _ in range(5):
        assert reason(lambda: provider.verify_id_token(id_token("n"), "n")) == "keys_unavailable"


@respx.mock
def test_only_rsa_signing_keys_with_an_id_are_used():
    encryption_only = jwks()
    encryption_only["keys"][0]["use"] = "enc"
    nameless = jwks()
    del nameless["keys"][0]["kid"]
    respx.get(JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json=encryption_only),
            httpx.Response(200, json=nameless),
            httpx.Response(200, json=jwks()),
        ]
    )
    provider = google()
    assert reason(lambda: provider.verify_id_token(id_token("n"), "n")) == "keys_unavailable"
    assert reason(lambda: provider.verify_id_token(id_token("n"), "n")) == "keys_unavailable"
    # A token that does not say which key signed it is not tried against every key.
    no_key_id = jwt.encode(claims("n"), PRIVATE_KEY, algorithm="RS256")
    assert reason(lambda: provider.verify_id_token(no_key_id, "n")) == "id_token_key"
    assert provider.verify_id_token(id_token("n"), "n") == SUBJECT


def test_closing_the_provider_closes_its_http_client():
    http = httpx.Client()
    provider = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, http)
    provider.close()
    assert http.is_closed


def test_a_sign_in_error_carries_a_fixed_word_and_nothing_from_the_exchange():
    error = SignInError("id_token_nonce")
    assert error.reason == "id_token_nonce" and str(error) == "id_token_nonce"
