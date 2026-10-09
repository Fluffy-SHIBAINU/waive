import base64
import tempfile

from fastapi.testclient import TestClient
from pydantic import SecretStr

from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.config import Settings
from waive.db import init_db, make_engine
from waive.web.app import create_app


def make_client(ai=None, **overrides):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    settings = Settings(_env_file=None, nebius_api_key=SecretStr("k"), **overrides)
    app = create_app(
        settings,
        engine=engine,
        ai=ai,
        cipher=FieldCipher(base64.b64decode(new_key())),
        signer=TokenSigner("x" * 40),
    )
    return TestClient(app), engine


def test_home_and_health():
    client, _ = make_client()
    assert client.get("/healthz").json() == {"ok": True}
    page = client.get("/")
    assert page.status_code == 200
    assert "Start" in page.text and "never asks for money" in page.text
    assert 'lang="en"' in page.text


def test_static_css_and_bad_token_page():
    client, _ = make_client()
    assert client.get("/static/waive.css").status_code == 200
    missing = client.get("/s/not-a-real-token")
    assert missing.status_code == 403
    assert "not valid" in missing.text


MIB = 1024 * 1024


def upload(size: int):
    return {"photo": ("bill.jpg", b"x" * size, "image/jpeg")}


def test_oversized_uploads_get_a_413_page_before_any_work():
    """FastAPI parses the whole multipart body before the token check runs, so the cap sits in
    front of the app: declared length first, then the bytes actually received."""
    client, _ = make_client()
    cap = client.app.state.settings.max_upload_bytes
    assert cap == 10 * MIB
    declared = client.post("/s/not-a-real-token/bill", files=upload(cap + 1))
    assert declared.status_code == 413 and "too large" in declared.text.lower()

    def chunks():
        for _ in range(cap // MIB + 1):
            yield b"x" * MIB

    chunked = client.post(
        "/s/not-a-real-token/bill",
        content=chunks(),
        headers={"content-type": "multipart/form-data; boundary=waive"},
    )
    assert chunked.status_code == 413
    assert client.post("/s/not-a-real-token/bill", files=upload(2 * MIB)).status_code == 403
    assert client.post("/c/not-a-real-token/paper", files=upload(cap + 1)).status_code == 413
    assert client.get("/healthz").status_code == 200


def test_case_creation_is_rate_limited():
    """POST /cases needs no sign-in and mints two capability links per call (spec §11: requests
    are rate-limited). The window is per process: one replica, in memory."""
    client, _ = make_client(cases_per_minute=3)
    assert client.app.state.settings.cases_per_minute == 3
    assert client.app.state.settings.cases_per_client_per_minute == 10
    statuses = [client.post("/cases", data={"state": "MA"}).status_code for _ in range(4)]
    assert statuses == [200, 200, 200, 429]
    refused = client.post("/cases", data={"state": "MA"})
    assert "too many" in refused.text.lower() and refused.status_code == 429
    assert client.get("/healthz").status_code == 200  # only case creation is counted
    limiter = client.app.state.limiter
    limiter.advance(61)  # the window passes
    assert client.post("/cases", data={"state": "MA"}).status_code == 200


def test_token_pages_are_never_cached_indexed_or_leaked_as_referrer():
    """Pages and files under a capability link carry personal data; the public atlas and the
    home page stay cacheable and indexable."""
    from tests.unit.test_web_senior import seeded_client

    client, links = seeded_client()
    private = (
        f"/s/{links.senior_token}",
        f"/c/{links.caregiver_token}",
        f"/c/{links.caregiver_token}/reminders.ics",
        "/admin/login",
        "/s/not-a-real-token",  # the 403 page too
    )
    for path in private:
        headers = client.get(path).headers
        assert headers["cache-control"] == "no-store", path
        assert headers["referrer-policy"] == "no-referrer", path
        assert "noindex" in headers["x-robots-tag"], path
    created = client.post("/cases", data={"state": "MA"})
    assert created.headers["cache-control"] == "no-store"
    for path in ("/", "/healthz", "/static/waive.css", "/atlas", "/atlas/229999"):
        headers = client.get(path).headers
        assert "x-robots-tag" not in headers and headers.get("cache-control") != "no-store", path
    gone = client.post(f"/c/{links.caregiver_token}/delete")
    assert gone.headers["clear-site-data"] == '"cache"'


def test_openapi_schema_and_docs_are_not_served():
    """`docs_url`/`redoc_url` were already off; `openapi_url` left at its default published the
    whole route map, every `/admin/*` path included (security audit finding secrets-config-8)."""
    client, _ = make_client()
    assert client.app.openapi_url is None
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code == 404, path
    assert client.get("/healthz").status_code == 200


def test_multipart_uploads_never_spool_to_disk(monkeypatch):
    rollovers = []
    monkeypatch.setattr(
        tempfile.SpooledTemporaryFile, "rollover", lambda self: rollovers.append(self)
    )
    client, _ = make_client()
    assert client.post("/s/not-a-real-token/bill", files=upload(2 * MIB)).status_code == 403
    assert rollovers == []
