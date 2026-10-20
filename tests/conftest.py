import pytest

from waive.atlas import fetch
from waive.web import kindred_auth

PUBLIC_ADDRESS = "93.184.216.34"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Unit tests never resolve names (pytest-socket blocks sockets, not getaddrinfo): every
    host the fetcher is allowed to reach looks like a public address. Tests of the private-address
    guard pass their own `resolver`."""
    monkeypatch.setattr(
        fetch, "resolve", lambda host, port=None: [(None, None, None, "", (PUBLIC_ADDRESS, 0))]
    )


SIGNED_IN = {"sub": "test", "email": "reader@kindred-ai.dev", "exp": 2_000_000_000}


@pytest.fixture(autouse=True)
def kindred_signed_in(request, monkeypatch):
    """Every web test runs as a reader signed in to Kindred. The wall's local bypass wants a
    localhost Host and the test client says `testserver`, so the bypass is stood in for here.
    Tests marked `kindred_wall` (tests/unit/test_kindred_wall.py) get the real wall."""
    if request.node.get_closest_marker("kindred_wall"):
        return
    monkeypatch.setattr(kindred_auth, "bypass_user", lambda env=None, host=None: dict(SIGNED_IN))
