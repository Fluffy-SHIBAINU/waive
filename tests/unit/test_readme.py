"""The README must stay honest about models and licenses (Phase 8.1)."""

import re
from pathlib import Path

import typer

from waive.cli import app
from waive.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def section(text: str, heading: str) -> str:
    """The body of one `## heading`, up to the next `## `."""
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start : end if end != -1 else len(text)]


def resolves(words: list[str]) -> bool:
    """True when `waive <words>` names a command in the CLI tree (groups have `.commands`)."""
    cmd = typer.main.get_command(app)
    for word in words:
        subcommands = getattr(cmd, "commands", {})
        if word not in subcommands:
            return False
        cmd = subcommands[word]
    return True


def test_setup_section_only_runs_commands_that_exist():
    # The Commands table may list commands that later tasks of the phase add; the Setup
    # walkthrough must work on the tree as committed.
    setup = section(readme(), "## Setup")
    invocations = re.findall(r"uv run waive ([a-z][a-z-]*(?: [a-z][a-z-]*)?)", setup)
    assert invocations
    missing = [words for words in invocations if not resolves(words.split())]
    assert missing == [], missing


def test_project_layout_lists_only_files_that_exist():
    layout = section(readme(), "## Project layout")
    block = layout.split("```")[1]
    entries = re.findall(r"^ {2}([a-z_]+(?:\.py|/))\s", block, flags=re.MULTILINE)
    assert "cli.py" in entries
    missing = [e for e in entries if not (ROOT / "src" / "waive" / e).exists()]
    assert missing == [], missing


def test_readme_names_the_configured_models_and_is_honest_about_vision():
    text = readme()
    settings = Settings(_env_file=None)
    for model in (
        settings.model_reason,
        settings.model_fast,
        settings.model_tiebreak,
        settings.model_vision,
    ):
        assert f"`{model}`" in text, model
    # Token Factory offers no NVIDIA vision model; the README says so instead of implying it.
    assert "no NVIDIA vision model" in text


def test_readme_has_the_required_sections_and_no_placeholders():
    text = readme()
    for heading in (
        "## Setup",
        "## Architecture",
        "## How Nebius, NVIDIA and Tavily are used at runtime",
        "## Privacy and zero data retention",
        "## Costs",
        "## Licenses",
    ):
        assert heading in text, heading
    assert "```mermaid" in text
    assert "Apache-2.0" in text and "CC BY 4.0" in text
    assert "Deployment status" in text  # the one line to update when Phase 6 task 6.8 closes
    assert "TBD" not in text


def test_license_is_the_canonical_apache_2_text():
    raw = (ROOT / "LICENSE").read_bytes()
    assert len(raw) == 11358
    text = raw.decode("utf-8")
    assert text.lstrip().startswith("Apache License")
    assert "Version 2.0, January 2004" in text


def test_atlas_data_license_note_exists():
    note = (ROOT / "data" / "atlas" / "LICENSE.md").read_text(encoding="utf-8")
    assert "CC BY 4.0" in note
    assert "https://creativecommons.org/licenses/by/4.0/" in note
