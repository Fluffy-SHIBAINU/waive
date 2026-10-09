"""The production image: two stages, uv-built, non-root, app factory on port 8000 (spec §14)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def dockerfile_lines():
    return [line.strip() for line in (ROOT / "Dockerfile").read_text().splitlines() if line.strip()]


def test_two_stages_built_with_uv_without_dev_dependencies():
    lines = dockerfile_lines()
    froms = [line for line in lines if line.startswith("FROM ")]
    assert len(froms) == 2
    # Both stages come from Docker Hub: ghcr.io pulls are blocked on the build machine (gate U6.0),
    # so the builder installs a pinned uv with pip instead of using the astral-sh/uv image.
    assert all("python:3.12-slim-bookworm" in line for line in froms)
    instructions = [line for line in lines if not line.startswith("#")]
    assert not any("ghcr.io" in line for line in instructions)
    text = "\n".join(lines)
    assert re.search(r"pip install [^\n]*\buv==\d+\.\d+\.\d+\b", text)
    assert "uv sync --frozen --no-dev --no-install-project" in text
    assert "uv sync --frozen --no-dev --no-editable" in text
    assert "COPY . " not in text and "COPY ./ " not in text  # the context is copied selectively


def test_runs_the_app_factory_as_non_root_on_port_8000():
    lines = dockerfile_lines()
    cmd = next(line for line in lines if line.startswith("CMD "))
    assert "USER waive" in lines and "EXPOSE 8000" in lines
    assert lines.index("USER waive") < lines.index(cmd)
    for token in (
        '"uvicorn"',
        '"waive.web.app:create_app"',
        '"--factory"',
        '"--port", "8000"',
        '"--no-access-log"',  # capability tokens travel in URLs; access logs must not keep them
    ):
        assert token in cmd


def test_build_context_excludes_secrets_tests_and_local_data():
    ignored = {line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()}
    assert {".env", ".env.*", "!.env.example", "var/", "tests/", "docs/", ".git", "*.db"} <= ignored
