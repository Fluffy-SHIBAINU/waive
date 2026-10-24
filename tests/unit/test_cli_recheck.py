"""`waive atlas recheck` on the command line: what to do about each sheet it lists."""

import pytest
from typer.testing import CliRunner

from waive.atlas import repo
from waive.atlas.pipeline import build_hospital
from waive.cli import app
from waive.db import init_db, make_engine, session_scope

from tests.unit.test_pipeline import REAL_HOSPITAL, TODAY, FakeAI, FakeGateway


@pytest.fixture
def atlas(monkeypatch, tmp_path):
    """A local database with Real General's sheet published from a site discovery was sure of;
    no .env."""
    monkeypatch.chdir(tmp_path)
    url = f"sqlite:///{tmp_path / 'local.db'}"
    monkeypatch.setenv("WAIVE_DATABASE_URL", url)
    engine = make_engine(url)
    init_db(engine)
    with session_scope(engine) as session:
        repo.upsert_hospital(session, REAL_HOSPITAL)
        result = build_hospital(session, FakeGateway(), FakeAI(), "220031", TODAY)
        assert result.outcome == "published"
    yield engine
    engine.dispose()


def recheck() -> str:
    result = CliRunner().invoke(app, ["atlas", "recheck"])
    assert result.exit_code == 0, result.output
    return " ".join(result.output.split())  # Rich wraps long lines at 80 columns


def test_a_sheet_waiting_only_on_its_website_gets_withdraw_and_confirm_not_a_rebuild(atlas):
    # A rebuild would be held again for the same website and spend tokens for nothing.
    with session_scope(atlas) as session:
        repo.get_hospital(session, "220031").domain_confidence = 0.6
    text = recheck()
    assert "1 sheet with fields the current rules reject" in text
    assert "Rebuild:" not in text
    assert (
        "1 sheet waits only on an unconfirmed website (a rebuild would be held again): "
        "`uv run waive atlas recheck --withdraw` holds it, then Confirm website or Wrong "
        "website in the admin console (/admin/review)"
    ) in text


def test_a_sheet_with_a_rejected_field_keeps_its_rebuild_command(atlas):
    with session_scope(atlas) as session:
        sheet, _ = repo.latest_sheet(session, "220031")
        phone = sheet.contacts.phone.model_copy(update={"quote": "Call us any day: 617-555-0100"})
        stale = sheet.model_copy(
            update={"contacts": sheet.contacts.model_copy(update={"phone": phone}), "version": 2}
        )
        repo.add_sheet_version(session, stale, {"contacts.phone": {"old": None}})
    text = recheck()
    assert "Rebuild: uv run waive atlas build --state MA --ccn 220031 --reuse-sources" in text
    assert "unconfirmed website" not in text


def test_a_sheet_level_problem_on_an_unconfirmed_website_gets_the_website_advice(atlas):
    # A rebuild would be held again for the website; withdraw holds the gap with it, and Confirm
    # website re-checks it in place.
    from waive.atlas.schema import DiscountTier

    with session_scope(atlas) as session:
        repo.get_hospital(session, "220031").domain_confidence = 0.6
        sheet, _ = repo.latest_sheet(session, "220031")
        tier = DiscountTier(min_fpl_exclusive=300, max_fpl_inclusive=400, discount_percent=60)
        # Two spans of the stored policy: the 60% band, read from 300% (the 250-300% band lost).
        quote = (
            "Patients with household income above 250% and at or below 400% of the Federal "
            "Poverty Guidelines receive a 60% discount. || Patients with household income at or "
            "below 300% of the Federal Poverty Guidelines"
        )
        tiers = sheet.eligibility.free_care_max_fpl.model_copy(
            update={"value": [tier], "quote": quote}
        )
        stale = sheet.model_copy(
            update={
                "eligibility": sheet.eligibility.model_copy(update={"discount_tiers": tiers}),
                "version": 2,
            }
        )
        repo.add_sheet_version(session, stale, {"eligibility.discount_tiers": {"old": None}})
    text = recheck()
    assert "1 sheet with fields the current rules reject" in text  # the gap, listed
    assert "Rebuild:" not in text
    assert (
        "1 sheet waits only on an unconfirmed website (a rebuild would be held again): "
        "`uv run waive atlas recheck --withdraw` holds it, then Confirm website or Wrong "
        "website in the admin console (/admin/review)"
    ) in text
