"""`waive serve` must not keep capability tokens: request paths carry them (README privacy
notes), so the access log stays off here as it does in the container (test_dockerfile)."""

import uvicorn
from typer.testing import CliRunner

from waive.cli import app


def test_serve_runs_uvicorn_with_the_access_log_off(monkeypatch):
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    result = CliRunner().invoke(app, ["serve", "--host", "127.0.0.1", "--port", "0"])
    assert result.exit_code == 0, result.output
    [(args, kwargs)] = calls
    assert args == ("waive.web.app:create_app",)
    assert kwargs["factory"] is True and kwargs["host"] == "127.0.0.1" and kwargs["port"] == 0
    assert kwargs["access_log"] is False
