import pytest

from waive.atlas import fetch

PUBLIC_ADDRESS = "93.184.216.34"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Unit tests never resolve names (pytest-socket blocks sockets, not getaddrinfo): every
    host the fetcher is allowed to reach looks like a public address. Tests of the private-address
    guard pass their own `resolver`."""
    monkeypatch.setattr(
        fetch, "resolve", lambda host, port=None: [(None, None, None, "", (PUBLIC_ADDRESS, 0))]
    )
