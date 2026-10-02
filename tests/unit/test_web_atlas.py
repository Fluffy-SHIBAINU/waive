from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.db import session_scope

from tests.unit.test_web_app import make_client
from tests.unit.test_web_senior import HOSPITAL


def test_atlas_pages_and_json():
    client, engine = make_client()
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, st_example_sheet())
    listing = client.get("/atlas?q=example")
    assert listing.status_code == 200
    assert "ST. EXAMPLE MEDICAL CENTER" in listing.text and "published" in listing.text
    page = client.get("/atlas/229999")
    assert "free_care_max_fpl" in page.text and "250" in page.text
    assert "household income at or below 250%" in page.text
    assert "example.org" in page.text
    data = client.get("/atlas/229999.json").json()
    assert data["version"] == 1 and data["eligibility"]["free_care_max_fpl"]["value"] == "250"
    assert client.get("/atlas/000000").status_code == 404
