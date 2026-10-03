"""The README must stay honest about models and licenses (Phase 8.1)."""

from pathlib import Path

from waive.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


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
