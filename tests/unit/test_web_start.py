"""The start from another Kindred product's hospital page (Kindred connect design §5.5):
`GET /start?ccn=` names the hospital and creates nothing; its one button posts to `/start`, which
starts the case with the hospital chosen and counts it under `ccn_link` on the public `/stats`;
`robots.txt` keeps crawlers off the confirm page. Every request here is signed in by
tests/conftest.py; the wall's own behaviour on these paths is in test_kindred_wall.py."""

import re

from sqlalchemy import select

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.atlas.schema import SheetStatus
from waive.cases.extract import BillExtract
from waive.cases.service import view
from waive.db import CaseRow, ReviewItemRow, session_scope

from tests.unit.test_web_app import make_client
from tests.unit.test_web_atlas import REAL_HOSPITAL, real_sheet
from tests.unit.test_web_senior import HOSPITAL, FakeAI, photo

# A Washington public hospital district: in the registry, not scouted yet.
DISTRICT = {
    **HOSPITAL,
    "ccn": "500002",
    "name": "EVERGREEN DISTRICT HOSPITAL",
    "city": "KIRKLAND",
    "state": "WA",
    "zip": "98034",
    "phone": "425-555-0100",
    "ownership": "Government - Hospital District or Authority",
    "website_domain": None,
    "domain_confidence": None,
}


class UnsureAI(FakeAI):
    """Reads a bill whose letterhead names no hospital in the registry."""

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        read = super().complete_json(
            role, messages, schema, phi=phi, purpose=purpose, max_tokens=max_tokens
        )
        if isinstance(read, BillExtract):
            return read.model_copy(
                update={"hospital_name": "Nowhere Community Clinic", "hospital_phone": None}
            )
        return read


def seeded(ai=None, **overrides):
    """The example hospital with its sheet, a real one with a sheet, and the district without."""
    client, engine = make_client(ai=ai or FakeAI(), **overrides)
    with session_scope(engine) as session:
        for hospital in (HOSPITAL, REAL_HOSPITAL, DISTRICT):
            repo.upsert_hospital(session, hospital)
        publish_sheet(session, st_example_sheet())
        publish_sheet(session, real_sheet())
    return client, engine


def cases(engine) -> list[CaseRow]:
    with session_scope(engine) as session:
        return list(session.scalars(select(CaseRow).order_by(CaseRow.created_at)))


def start(client, ccn, **data):
    return client.post(
        "/start", data={"ccn": ccn, "source": "ccn_link", **data}, follow_redirects=False
    )


def test_the_confirm_page_names_the_hospital_and_creates_nothing():
    client, engine = seeded()
    page = client.get("/start?ccn=220031")
    assert page.status_code == 200
    assert "Check a bill from REAL GENERAL HOSPITAL" in page.text and "WORCESTER, MA" in page.text
    assert "policy on file, checked on" in page.text and 'href="/atlas/220031"' in page.text
    assert '<form method="post" action="/start" class="stack">' in page.text
    assert '<input type="hidden" name="ccn" value="220031">' in page.text
    assert '<input type="hidden" name="source" value="ccn_link">' in page.text
    assert "never asks for money" in page.text
    # A hospital Waive is still checking can be started too, and the page says so.
    district = client.get("/start?ccn=500002")
    assert district.status_code == 200 and "still checking" in district.text
    assert 'href="/atlas/500002"' not in district.text
    # The page is never cached or indexed: the button behind it mints a private link.
    assert page.headers["cache-control"] == "no-store" and "noindex" in page.headers["x-robots-tag"]
    assert cases(engine) == []  # looking starts nothing, however many pages link here


def test_an_unknown_or_missing_ccn_is_the_404_page_and_starts_nothing():
    client, engine = seeded()
    for ccn in ("999999", "", " ", "220031 "):
        looked = client.get("/start", params={"ccn": ccn}) if ccn else client.get("/start")
        if ccn.strip() == "220031":
            assert looked.status_code == 200, repr(ccn)  # a stray space is forgiven
            continue
        assert looked.status_code == 404 and "could not find" in looked.text, repr(ccn)
    for ccn in ("999999", "", " "):
        assert start(client, ccn).status_code == 404, repr(ccn)
    assert client.post("/start", data={}, follow_redirects=False).status_code == 404
    assert cases(engine) == []


def test_the_button_starts_the_case_with_the_hospital_chosen_and_counts_it():
    client, engine = seeded()
    created = start(client, "500002")
    assert created.status_code == 303 and re.fullmatch(
        r"/c/[^/]+/bill", created.headers["location"]
    )
    assert created.headers["cache-control"] == "no-store"
    [row] = cases(engine)
    assert (row.state, row.ccn, row.source, row.status) == ("WA", "500002", "ccn_link", "new")
    assert row.sealed is None  # nothing photographed: purged as abandoned like any other start
    photo_step = client.get(created.headers["location"])
    assert photo_step.status_code == 200
    assert "This case is for EVERGREEN DISTRICT HOSPITAL." in photo_step.text
    assert "Take a photo of the bill" in photo_step.text
    # The check page names the chosen hospital before any photo.
    token = created.headers["location"].split("/")[2]
    assert "EVERGREEN DISTRICT HOSPITAL" in client.get(f"/c/{token}").text
    # Only the confirm page's own word is stored; anything else is no source.
    assert start(client, "220031", source="spoofed").status_code == 303
    assert start(client, "220031").status_code == 303
    # The home page's own form names no source; the public counts tell the starts apart.
    assert client.post("/cases", data={"state": "MA"}, follow_redirects=False).status_code == 303
    assert [row.source for row in cases(engine)] == ["ccn_link", None, "ccn_link", None]
    assert client.get("/stats").json()["cases"] == {
        "total": 4,
        "by_source": {"ccn_link": 2, "other": 2},
    }


def test_a_bill_the_registry_cannot_match_keeps_the_chosen_hospital():
    # The reader came from the district's page; the bill's letterhead reads badly. The chosen
    # hospital stands and nobody is asked to scout it. A bill the registry reads with confidence
    # as another hospital's wins, as it always did.
    client, engine = seeded(ai=UnsureAI())
    created = start(client, "500002")
    readback = client.post(created.headers["location"], files=photo())
    assert readback.status_code == 200
    [row] = cases(engine)
    assert (row.ccn, row.status) == ("500002", "bill_read")
    with session_scope(engine) as session:
        shown = view(client.app.state.deps.context(session), row.id)
        assert (shown.ccn, shown.hospital_name) == ("500002", "EVERGREEN DISTRICT HOSPITAL")
        assert shown.needs_scouting is False
        assert list(session.scalars(select(ReviewItemRow))) == []  # no scout request opened

    confident, engine = seeded()  # reads St. Example Medical Center, 229999, confidently
    created = start(confident, "500002")
    assert confident.post(created.headers["location"], files=photo()).status_code == 200
    assert [row.ccn for row in cases(engine)] == ["229999"]


def test_the_public_counts_cover_the_sheets_by_state_without_the_demo_hospital():
    client, engine = seeded()
    assert client.get("/stats").json() == {
        "cases": {"total": 0, "by_source": {"ccn_link": 0, "other": 0}},
        "sheets": {"published": 1, "held": 0, "by_state": {"MA": {"published": 1, "held": 0}}},
    }
    with session_scope(engine) as session:
        published, _ = repo.latest_sheet(session, "220031")
        publish_sheet(session, published.model_copy(update={"status": SheetStatus.HELD}))
    counts = client.get("/stats").json()["sheets"]
    assert counts == {"published": 0, "held": 1, "by_state": {"MA": {"published": 0, "held": 1}}}
    assert "x-robots-tag" not in client.get("/stats").headers  # public, cacheable, findable


def test_robots_keeps_crawlers_off_the_confirm_page_and_the_private_addresses():
    client, _ = make_client()
    robots = client.get("/robots.txt")
    assert robots.status_code == 200 and robots.headers["content-type"].startswith("text/plain")
    lines = robots.text.splitlines()
    assert lines[0] == "User-agent: *"
    for path in ("/start", "/cases", "/s/", "/c/", "/account", "/auth/"):
        assert f"Disallow: {path}" in lines, path
    assert not any("atlas" in line or "Disallow: /\n" in line for line in lines)
