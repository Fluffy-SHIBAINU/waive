import base64

from fastapi.testclient import TestClient
from pydantic import SecretStr

from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.config import Settings
from waive.db import init_db, make_engine
from waive.web.app import create_app


def make_client(ai=None):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    settings = Settings(_env_file=None, nebius_api_key=SecretStr("k"))
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
