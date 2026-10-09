"""Public-repo hygiene files from the security audit (task 8.11): a security policy, Dependabot,
a CI workflow that runs what `README.md` tells contributors to run, and the license notice for
the skill vendored under `.claude/skills/nebius-starter`."""

import json
import re
from pathlib import Path

from tests.unit.test_readme import readme, section

ROOT = Path(__file__).resolve().parents[2]
REPO_URL = "https://github.com/Fluffy-SHIBAINU/waive"
SKILL_UPSTREAM = "https://github.com/antongisli/nebius-starter-skill"


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_security_policy_says_how_to_report_privately_and_what_is_in_scope():
    text = read("SECURITY.md")
    for heading in (
        "## Reporting a vulnerability",
        "## Supported versions",
        "## Scope",
        "## No bounty",
    ):
        assert heading in text, heading
    assert f"{REPO_URL}/security/advisories/new" in text
    assert "privately" in text.lower() and "public issue" in text.lower()
    assert "`main`" in text  # the only supported version
    assert "bug bounty" in text.lower()
    assert "TBD" not in text and "[USER FILLS" not in text


def test_dependabot_watches_the_uv_lock_and_the_workflow_actions_weekly():
    text = read(".github/dependabot.yml")
    assert text.startswith("version: 2")
    ecosystems = re.findall(r'package-ecosystem:\s*"([a-z-]+)"', text)
    assert sorted(ecosystems) == ["github-actions", "uv"]
    assert text.count('directory: "/"') == 2
    assert text.count('interval: "weekly"') == 2


def test_ci_runs_the_readme_check_commands_on_push_and_pull_request_without_secrets():
    text = read(".github/workflows/ci.yml")
    assert re.search(r"^on:\n(?:  .*\n)*?  push:", text, flags=re.MULTILINE)
    assert re.search(r"^on:\n(?:  .*\n)*?  pull_request:", text, flags=re.MULTILINE)
    assert "uses: actions/checkout@" in text
    assert "uses: astral-sh/setup-uv@" in text
    commands = re.findall(r"run: (uv .*)$", text, flags=re.MULTILINE)
    assert commands == [
        "uv sync --frozen",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run pytest",
    ]
    assert "secrets." not in text and "${{ secrets" not in text
    # The README tells contributors to run the same three checks.
    tests_section = section(readme(), "## Tests")
    assert "uv run ruff format . && uv run ruff check . && uv run pytest" in tests_section
    assert ".github/workflows/ci.yml" in tests_section


def test_third_party_notice_carries_the_mit_text_for_the_vendored_skill():
    text = read("THIRD_PARTY_NOTICES.md")
    lock = json.loads(read("skills-lock.json"))["skills"]["nebius-starter"]
    assert lock["source"] == "antongisli/nebius-starter-skill"
    assert f"https://github.com/{lock['source']}" == SKILL_UPSTREAM
    assert SKILL_UPSTREAM in text
    assert ".claude/skills/nebius-starter" in text
    assert "MIT License" in text
    assert "Copyright (c) 2026 Anton Smith" in text
    assert "Permission is hereby granted, free of charge" in text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in text
    # The vendored files are the ones the notice covers.
    vendored = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / ".claude/skills").rglob("*"))
    assert vendored and all(p.startswith(".claude/skills/nebius-starter") for p in vendored)


def test_readme_licenses_section_points_at_the_third_party_notice():
    licenses = section(readme(), "## Licenses")
    assert "THIRD_PARTY_NOTICES.md" in licenses
    assert ".claude/skills/nebius-starter" in licenses
    assert "MIT" in licenses
    assert SKILL_UPSTREAM in licenses
