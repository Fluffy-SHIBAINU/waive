"""Phase 9, task 9.7: the sign-in routes. Google is played by respx with a key made in
test_oidc; nothing here reaches the network."""

import base64
import logging
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from waive.auth import accounts, sessions
from waive.auth.oidc import JWKS_URL, TOKEN_ENDPOINT, code_challenge
from waive.cases.service import delete_case, relink, start_case
from waive.cases.vault import FieldCipher, new_key
from waive.db import AccountCaseRow, AccountRow, CaseRow, SessionRow, session_scope
from waive.web import routes_auth
from waive.web.routes_auth import STATE_COOKIE, STATE_SECONDS
from waive.web.signin import SESSION_COOKIE

from tests.unit.test_oidc import (
    CLIENT_ID,
    CLIENT_SECRET,
    OTHER_KEY,
    SUBJECT,
    id_token,
    mock_google,
)
from tests.unit.test_web_app import make_client

CALLBACK = "http://testserver/auth/google/callback"
FAILED = "Sign-in did not work. Nothing was changed."


def signin_client(**overrides):
    """An app with sign-in switched on. Returns (client, engine)."""
    settings = {
        "google_client_id": CLIENT_ID,
        "google_client_secret": SecretStr(CLIENT_SECRET),
        **overrides,
    }
    return make_client(**settings)


def start(client, case=None):
    """Press "Sign in with Google": returns what the browser carries to Google."""
    params = {"case": case} if case else None
    response = client.get("/auth/google/start", params=params, follow_redirects=False)
    assert response.status_code == 302, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return {name: values[0] for name, values in query.items()}


def come_back(client, sent, **changes):
    """Google sends the browser back with the state and a code."""
    params = {"state": sent["state"], "code": "the-code", **changes}
    return client.get("/auth/google/callback", params=params, follow_redirects=False)


def sign_in(client, case=None, subject=SUBJECT):
    """The whole round trip with a Google that answers well. Call under `respx.mock`."""
    sent = start(client, case)
    mock_google(id_token(sent["nonce"], sub=subject))
    response = come_back(client, sent)
    assert response.status_code == 303, response.text
    return response


def rows(engine, model):
    with session_scope(engine) as session:
        return list(session.scalars(select(model)))


def set_cookies(response):
    return response.headers.get_list("set-cookie")


def cookie(response, name):
    found = [line for line in set_cookies(response) if line.startswith(f"{name}=")]
    assert len(found) == 1, set_cookies(response)
    return found[0]


def new_case(client, engine):
    with session_scope(engine) as session:
        return start_case(client.app.state.deps.context(session), "MA")


def failure_reasons(caplog):
    """The one fixed word each failed sign-in left in the log, oldest first."""
    return [r.args[0] for r in caplog.records if r.name == "waive.web.routes_auth"]


def assert_nothing_changed(response, engine):
    assert response.status_code == 400
    assert FAILED in response.text
    assert "Traceback" not in response.text
    assert rows(engine, AccountRow) == [] and rows(engine, SessionRow) == []
    assert not any(line.startswith(f"{SESSION_COOKIE}=") for line in set_cookies(response))
    assert response.headers["cache-control"] == "no-store"


def test_start_sends_the_browser_to_google_with_a_sealed_short_lived_cookie():
    client, engine = signin_client()
    response = client.get("/auth/google/start", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    sent = {name: values[0] for name, values in parse_qs(urlsplit(location).query).items()}
    assert sent["client_id"] == CLIENT_ID and sent["response_type"] == "code"
    assert sent["scope"] == "openid" and sent["code_challenge_method"] == "S256"
    assert sent["redirect_uri"] == CALLBACK
    assert len(sent["state"]) >= 43 and len(sent["nonce"]) >= 43
    assert CLIENT_SECRET not in location

    state_cookie = cookie(response, STATE_COOKIE)
    flags = state_cookie.lower()
    assert "httponly" in flags and "samesite=lax" in flags
    assert f"max-age={STATE_SECONDS}" in flags and STATE_SECONDS == 600
    assert "path=/auth/google" in flags
    assert "secure" not in flags  # plain http on a laptop; https adds it (see below)
    # Sealed: neither the state, the nonce nor anything else in it can be read or swapped.
    value = state_cookie.split(";")[0].split("=", 1)[1]
    assert sent["state"] not in value and sent["nonce"] not in value
    for part in (sent["state"], sent["nonce"]):
        assert base64.b64encode(part.encode()).decode().rstrip("=") not in value
    # Nothing is stored until Google answers.
    assert rows(engine, AccountRow) == [] and rows(engine, SessionRow) == []
    for name, expected in (
        ("cache-control", "no-store"),
        ("referrer-policy", "no-referrer"),
        ("x-robots-tag", "noindex, nofollow"),
    ):
        assert response.headers[name] == expected
    # Every press is a new attempt.
    assert start(client)["state"] != sent["state"]


@respx.mock
def test_coming_back_from_google_creates_the_account_and_a_session():
    client, engine = signin_client()
    sent = start(client)
    exchange, _ = mock_google(id_token(sent["nonce"]))
    response = come_back(client, sent)
    assert response.status_code == 303 and response.headers["location"] == "/account"

    [account] = rows(engine, AccountRow)
    assert account.provider == "google"
    assert account.subject_hash == accounts.subject_hash("x" * 40, "google", SUBJECT)
    assert SUBJECT not in account.subject_hash
    [session_row] = rows(engine, SessionRow)
    assert session_row.account_id == account.id

    session_cookie = cookie(response, SESSION_COOKIE)
    flags = session_cookie.lower()
    assert "httponly" in flags and "samesite=lax" in flags and "path=/" in flags
    assert f"max-age={14 * 24 * 3600}" in flags
    assert "secure" not in flags
    # The cookie is the random session id and nothing else.
    session_id = session_cookie.split(";")[0].split("=", 1)[1]
    assert len(session_id) >= 43 and SUBJECT not in session_id
    with session_scope(engine) as session:
        assert sessions.read(session, session_id) == account.id
    # The attempt is over: its cookie is cleared.
    cleared = cookie(response, STATE_COOKIE).lower()
    assert "max-age=0" in cleared and "path=/auth/google" in cleared
    assert STATE_COOKIE not in client.cookies and SESSION_COOKIE in client.cookies

    # Google got the code with the verifier behind the challenge, at the same redirect address.
    [call] = exchange.calls
    form = {name: values[0] for name, values in parse_qs(call.request.content.decode()).items()}
    assert form["code"] == "the-code" and form["redirect_uri"] == CALLBACK
    assert code_challenge(form["code_verifier"]) == sent["code_challenge"]
    assert form["client_secret"] == CLIENT_SECRET


@respx.mock
def test_the_same_person_signing_in_again_gets_the_same_account_and_a_new_session():
    client, engine = signin_client()
    sign_in(client)
    other_browser = TestClient(client.app)
    sign_in(other_browser)
    assert len(rows(engine, AccountRow)) == 1
    assert len(rows(engine, SessionRow)) == 2
    sign_in(TestClient(client.app), subject="someone-else")
    assert len(rows(engine, AccountRow)) == 2


@respx.mock
def test_signing_in_from_a_case_keeps_that_case_and_returns_to_it():
    client, engine = signin_client()
    links = new_case(client, engine)
    sent = start(client, case=links.caregiver_token)
    assert links.caregiver_token not in str(sent)  # the link does not travel to Google
    mock_google(id_token(sent["nonce"]))
    response = come_back(client, sent)
    assert response.status_code == 303
    assert response.headers["location"] == f"/c/{links.caregiver_token}"
    [account] = rows(engine, AccountRow)
    [link] = rows(engine, AccountCaseRow)
    assert (link.account_id, link.case_id) == (account.id, links.case_id)
    # A case that already has a result goes back to the result page.
    with session_scope(engine) as session:
        session.get(CaseRow, links.case_id).status = "evaluated"
    back = sign_in(client, case=links.caregiver_token)
    assert back.headers["location"] == f"/c/{links.caregiver_token}/result"
    assert len(rows(engine, AccountCaseRow)) == 1  # kept once
    # The browser signed in a second time: its first session ended with the new one.
    assert len(rows(engine, SessionRow)) == 1 and len(rows(engine, AccountRow)) == 1


@respx.mock
def test_only_a_full_link_to_a_case_that_still_exists_is_kept():
    client, engine = signin_client()
    links = new_case(client, engine)
    # The photo-only link cannot put a case into an account; nor can a made-up token.
    for carried in (links.senior_token, "not-a-token", "x" * 5000):
        response = sign_in(TestClient(client.app), case=carried)
        assert response.headers["location"] == "/account"
    assert rows(engine, AccountCaseRow) == []
    # Links revoked or a case deleted while the person was at Google: signed in, nothing kept.
    for undo in (relink, delete_case):
        browser = TestClient(client.app)
        sent = start(browser, case=links.caregiver_token)
        with session_scope(engine) as session:
            fresh = undo(client.app.state.deps.context(session), links.case_id)
        mock_google(id_token(sent["nonce"]))
        response = come_back(browser, sent)
        assert response.status_code == 303 and response.headers["location"] == "/account"
        assert rows(engine, AccountCaseRow) == []
        links = fresh or links
    assert len(rows(engine, AccountRow)) == 1 and len(rows(engine, SessionRow)) == 5


@respx.mock
def test_a_wrong_state_is_refused(caplog):
    caplog.set_level(logging.INFO)
    client, engine = signin_client()
    sent = start(client)
    exchange, _ = mock_google(id_token(sent["nonce"]))
    for wrong in ("", "another-state", sent["state"][:-1], sent["state"] + "x", "x" * 5000):
        response = come_back(TestClient(client.app, cookies=client.cookies), sent, state=wrong)
        assert_nothing_changed(response, engine)
    assert failure_reasons(caplog) == ["state_mismatch"] * 5  # the cookie was there and good
    assert exchange.call_count == 0  # Google is never asked with a state that does not match


@respx.mock
def test_a_missing_or_forged_state_cookie_is_refused(caplog):
    caplog.set_level(logging.INFO)
    client, engine = signin_client()
    sent = start(client)
    exchange, _ = mock_google(id_token(sent["nonce"]))
    # Another browser: the attacker has the state and a code, but not the victim's cookie.
    assert_nothing_changed(come_back(TestClient(client.app), sent), engine)
    # A cookie sealed with some other key, or not sealed at all.
    stranger = FieldCipher(base64.b64decode(new_key())).encrypt(
        {"k": "signin", "s": sent["state"], "n": sent["nonce"], "v": "v" * 43, "c": "", "e": 2**40}
    )
    for forged in (stranger, stranger.replace("+", "-").replace("/", "_"), "x", "e30", ""):
        browser = TestClient(client.app, cookies={STATE_COOKIE: forged})
        assert_nothing_changed(come_back(browser, sent), engine)
    assert failure_reasons(caplog) == ["no_attempt", *["attempt_unreadable"] * 4, "no_attempt"]
    assert exchange.call_count == 0


@respx.mock
def test_sealed_case_data_cannot_stand_in_for_the_state_cookie(caplog):
    """The cookie is sealed with the vault key, like case data: a blob of another kind, even a
    well sealed one, is not an attempt."""
    caplog.set_level(logging.INFO)
    client, engine = signin_client()
    sent = start(client)
    exchange, _ = mock_google(id_token(sent["nonce"]))
    cipher = client.app.state.deps.cipher
    other = cipher.encrypt({"s": sent["state"], "n": sent["nonce"], "v": "v" * 43, "c": ""})
    cookie_value = other.replace("+", "-").replace("/", "_").rstrip("=")
    browser = TestClient(client.app, cookies={STATE_COOKIE: cookie_value})
    assert_nothing_changed(come_back(browser, sent), engine)
    assert failure_reasons(caplog) == ["attempt_unreadable"]
    assert exchange.call_count == 0


@respx.mock
def test_an_attempt_older_than_ten_minutes_is_refused(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    client, engine = signin_client()
    sent = start(client)
    exchange, _ = mock_google(id_token(sent["nonce"]))
    kept = dict(client.cookies)
    real = routes_auth.now
    monkeypatch.setattr(routes_auth, "now", lambda: real() + STATE_SECONDS + 1)
    # The browser would have dropped the cookie; one that kept it gains nothing.
    assert_nothing_changed(come_back(TestClient(client.app, cookies=kept), sent), engine)
    assert failure_reasons(caplog) == ["attempt_expired"]
    assert exchange.call_count == 0
    monkeypatch.setattr(routes_auth, "now", lambda: real() + STATE_SECONDS - 5)
    assert come_back(TestClient(client.app, cookies=kept), sent).status_code == 303


@pytest.mark.parametrize(
    ("token_changes", "reason"),
    [
        ({"nonce": "another-nonce"}, "id_token_nonce"),
        ({"aud": "someone-else.apps.googleusercontent.com"}, "id_token_audience"),
        ({"iss": "https://accounts.example.com"}, "id_token_issuer"),
        ({"iat": 1_700_000_000, "exp": 1_700_003_600}, "id_token_expired"),
        ({"key": OTHER_KEY}, "id_token_signature"),
    ],
    ids=["wrong nonce", "wrong audience", "wrong issuer", "expired", "tampered signature"],
)
@respx.mock
def test_an_id_token_that_fails_a_check_signs_nobody_in(token_changes, reason, caplog):
    caplog.set_level(logging.INFO)
    client, engine = signin_client()
    sent = start(client)
    changes = {"nonce": sent["nonce"], **token_changes}
    nonce = changes.pop("nonce")
    exchange, _ = mock_google(id_token(nonce, **changes))
    response = come_back(client, sent)
    assert_nothing_changed(response, engine)
    assert failure_reasons(caplog) == [reason]
    assert exchange.call_count == 1
    # The attempt is spent either way.
    assert "max-age=0" in cookie(response, STATE_COOKIE).lower()
    assert STATE_COOKIE not in client.cookies


@respx.mock
def test_a_token_endpoint_error_or_a_refusal_at_google_signs_nobody_in(caplog):
    caplog.set_level(logging.INFO)
    client, engine = signin_client()
    sent = start(client)
    exchange, certs = mock_google(token_status=400)
    assert_nothing_changed(come_back(client, sent), engine)
    assert exchange.call_count == 1 and certs.call_count == 0

    # The person pressed "Cancel" at Google: it sends an error and no code.
    sent = start(client)
    params = {"state": sent["state"], "error": "access_denied"}
    response = client.get("/auth/google/callback", params=params, follow_redirects=False)
    assert_nothing_changed(response, engine)
    assert exchange.call_count == 1

    # Google cannot be reached at all.
    sent = start(client)
    respx.post(TOKEN_ENDPOINT).mock(side_effect=httpx.ConnectError("no route"))
    assert_nothing_changed(come_back(client, sent), engine)
    sent = start(client)
    mock_google(id_token(sent["nonce"]))
    respx.get(JWKS_URL).mock(return_value=httpx.Response(503))
    assert_nothing_changed(come_back(client, sent), engine)
    assert failure_reasons(caplog) == [
        "token_endpoint_refused",
        "refused_at_provider",
        "token_endpoint_unreachable",
        "keys_unavailable",
    ]
    # And the page stays usable: there is a way on.
    assert 'href="/"' in come_back(client, sent).text


@respx.mock
def test_a_replayed_callback_is_refused_and_starts_no_second_session(caplog):
    caplog.set_level(logging.INFO)
    client, engine = signin_client()
    sent = start(client)
    exchange, _ = mock_google(id_token(sent["nonce"]))
    assert come_back(client, sent).status_code == 303
    assert len(rows(engine, SessionRow)) == 1
    replay = come_back(client, sent)  # same state, same code; the cookie is gone
    assert replay.status_code == 400 and FAILED in replay.text
    assert len(rows(engine, SessionRow)) == 1 and len(rows(engine, AccountRow)) == 1
    assert not any(line.startswith(f"{SESSION_COOKIE}=") for line in set_cookies(replay))
    assert failure_reasons(caplog) == ["no_attempt"]
    assert exchange.call_count == 1


def test_without_google_settings_the_sign_in_routes_do_not_exist():
    for overrides in (
        {},
        {"google_client_id": CLIENT_ID},
        {"google_client_secret": SecretStr(CLIENT_SECRET)},
        {"google_client_id": "", "google_client_secret": SecretStr("")},
    ):
        client, engine = make_client(**overrides)
        assert client.app.state.settings.sign_in_configured is False
        for path in ("/auth/google/start", "/auth/google/callback?state=s&code=c"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 404, (overrides, path)
            assert set_cookies(response) == []
            assert "google" not in response.headers.get("location", "")
        assert rows(engine, SessionRow) == []
    client, _ = signin_client()
    assert client.app.state.settings.sign_in_configured is True


@respx.mock
def test_sign_out_ends_the_session_and_clears_the_cookie():
    client, engine = signin_client()
    sign_in(client)
    other_browser = TestClient(client.app)
    sign_in(other_browser)
    assert len(rows(engine, SessionRow)) == 2
    session_id = client.cookies[SESSION_COOKIE]

    response = client.post("/auth/signout", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    cleared = cookie(response, SESSION_COOKIE).lower()
    assert "max-age=0" in cleared and "httponly" in cleared and "samesite=lax" in cleared
    assert SESSION_COOKIE not in client.cookies
    assert response.headers["cache-control"] == "no-store"
    # The id is dead on the server too: a copy of the cookie signs nobody in.
    with session_scope(engine) as session:
        assert sessions.read(session, session_id) is None
    assert len(rows(engine, SessionRow)) == 1  # the other browser stays signed in
    assert len(rows(engine, AccountRow)) == 1  # signing out deletes nothing else
    # Signing out twice, or without ever signing in, is just a way home.
    assert client.post("/auth/signout", follow_redirects=False).status_code == 303
    assert client.get("/auth/signout").status_code == 405  # a link cannot sign anyone out


@respx.mock
def test_another_site_cannot_sign_the_person_out():
    client, engine = signin_client()
    sign_in(client)
    for site in ("cross-site", "same-site"):
        response = client.post(
            "/auth/signout", headers={"sec-fetch-site": site}, follow_redirects=False
        )
        assert response.status_code == 303 and set_cookies(response) == []
        assert len(rows(engine, SessionRow)) == 1
    same = client.post(
        "/auth/signout", headers={"sec-fetch-site": "same-origin"}, follow_redirects=False
    )
    assert same.status_code == 303 and rows(engine, SessionRow) == []


def test_over_https_and_in_production_the_cookies_are_secure_and_the_return_address_is_https():
    client, _ = signin_client()
    https = TestClient(client.app, base_url="https://testserver")
    response = https.get("/auth/google/start", follow_redirects=False)
    assert "secure" in cookie(response, STATE_COOKIE).lower()
    sent = parse_qs(urlsplit(response.headers["location"]).query)
    assert sent["redirect_uri"] == ["https://testserver/auth/google/callback"]

    # Production sits behind a front end that ends TLS: the cookies are Secure and Google is
    # given the https address even when the request reached the app as plain http.
    production, _ = signin_client(
        env="production",
        database_url="postgresql+psycopg://waive:pw@db.example.net:5432/waive",
        ledger_backend="db",
    )
    response = production.get("/auth/google/start", follow_redirects=False)
    assert "secure" in cookie(response, STATE_COOKIE).lower()
    sent = parse_qs(urlsplit(response.headers["location"]).query)
    assert sent["redirect_uri"] == ["https://testserver/auth/google/callback"]


@respx.mock
def test_the_session_cookie_is_secure_over_https():
    client, _ = signin_client()
    https = TestClient(client.app, base_url="https://testserver")
    sent = start(https)
    mock_google(id_token(sent["nonce"]))
    response = come_back(https, sent)
    assert response.status_code == 303
    flags = cookie(response, SESSION_COOKIE).lower()
    assert "secure" in flags and "httponly" in flags and "samesite=lax" in flags
    signed_out = https.post("/auth/signout", follow_redirects=False)
    assert "secure" in cookie(signed_out, SESSION_COOKIE).lower()


def test_starting_and_returning_are_rate_limited():
    client, _ = signin_client(sign_ins_per_minute=3)
    statuses = [
        client.get("/auth/google/start", follow_redirects=False).status_code for _ in range(4)
    ]
    assert statuses == [302, 302, 302, 429]
    refused = client.get("/auth/google/start", follow_redirects=False)
    assert refused.status_code == 429 and "too many" in refused.text.lower()
    assert set_cookies(refused) == []
    # The return trip has its own count, and the limit is hit before anything is checked.
    statuses = [client.get("/auth/google/callback?state=s&code=c").status_code for _ in range(4)]
    assert statuses == [400, 400, 400, 429]
    assert client.get("/healthz").status_code == 200
    client.app.state.limiter.advance(61)
    assert client.get("/auth/google/start", follow_redirects=False).status_code == 302

    per_client, _ = signin_client(sign_ins_per_client_per_minute=1)
    assert per_client.app.state.settings.sign_ins_per_minute == 60
    assert per_client.get("/auth/google/start", follow_redirects=False).status_code == 302
    assert per_client.get("/auth/google/start", follow_redirects=False).status_code == 429


@respx.mock
def test_nothing_secret_reaches_the_log(caplog):
    client, engine = signin_client()
    links = new_case(client, engine)
    with caplog.at_level(logging.DEBUG):
        sent = start(client, case=links.caregiver_token)
        mock_google(id_token(sent["nonce"]))
        response = come_back(client, sent)
        session_id = client.cookies[SESSION_COOKIE]
        failed = start(client)
        mock_google(id_token("another-nonce"))
        assert come_back(client, failed, code="second-code").status_code == 400
        client.post("/auth/signout")
    assert response.status_code == 303
    # The test's own browser logs the addresses it asks for; a real one does not log here, and
    # the server runs without an access log (`waive serve`, the Dockerfile). Everything else is
    # the app's: its routes and its own calls to Google.
    records = [record for record in caplog.records if "://testserver" not in record.getMessage()]
    logged = "\n".join(
        [record.getMessage() for record in records] + [str(record.args) for record in records]
    )
    assert "https://oauth2.googleapis.com/token" in logged  # the app's calls are in there
    secrets_seen = (
        sent["state"],
        sent["nonce"],
        failed["state"],
        failed["nonce"],
        "the-code",
        "second-code",
        CLIENT_SECRET,
        SUBJECT,
        session_id,
        links.caregiver_token,
    )
    for secret in secrets_seen:
        assert secret not in logged
    # A failure is still visible to whoever runs the site, as one fixed word.
    assert "id_token_nonce" in logged
