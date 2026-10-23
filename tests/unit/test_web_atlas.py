import html

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.atlas.skiplist import EVE_SKIP, SkipEntry, file_skip_items
from waive.db import session_scope

from tests.unit.test_web_app import make_client
from tests.unit.test_web_senior import HOSPITAL

REAL_HOSPITAL = {**HOSPITAL, "ccn": "220031", "name": "REAL GENERAL HOSPITAL", "city": "WORCESTER"}


def real_sheet():
    sample = st_example_sheet()
    hospital = sample.hospital.model_copy(
        update={"ccn": "220031", "name": "Real General Hospital", "city": "Worcester"}
    )
    return sample.model_copy(update={"hospital": hospital})


def seed(engine):
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        repo.upsert_hospital(session, REAL_HOSPITAL)
        publish_sheet(session, st_example_sheet())
        publish_sheet(session, real_sheet())


def test_atlas_pages_and_json():
    client, engine = make_client()
    seed(engine)
    listing = client.get("/atlas?q=real")
    assert listing.status_code == 200
    assert "REAL GENERAL HOSPITAL" in listing.text and "published" in listing.text
    page = client.get("/atlas/220031")
    assert "free_care_max_fpl" in page.text and "250" in page.text
    assert "household income at or below 250%" in page.text
    assert "example.org" in page.text
    data = client.get("/atlas/220031.json").json()
    assert data["version"] == 1 and data["eligibility"]["free_care_max_fpl"]["value"] == "250"
    assert client.get("/atlas/000000").status_code == 404


def test_atlas_sheet_reads_in_plain_language():
    client, engine = make_client()
    seed(engine)
    page = client.get("/atlas/220031")
    assert page.status_code == 200
    # No Python reprs anywhere on the page: no list brackets, no model or enum names.
    assert "[" not in page.text and "]" not in page.text
    assert "StateProgram" not in page.text and "DocType" not in page.text
    assert "250% of the federal poverty level" in page.text
    assert "Above 250% up to 400% of FPL: 60% discount" in page.text
    # Lists are one item per line.
    assert "MassHealth<br>SNAP" in page.text
    assert "Photo ID<br>Proof of income" in page.text
    assert (
        "Mail: Patient Financial Services, 1 Example Way, Boston, MA 02118<br>Fax: 617-555-0199"
        in page.text
    )
    assert "240 days" in page.text and "120 days" in page.text
    # The quote-and-source display is unchanged.
    assert "household income at or below 250%" in page.text
    assert "documented · checked 2026-10-02" in page.text
    assert "Financial Assistance Policy" in page.text


def test_atlas_list_hides_the_demo_hospital_unless_asked():
    client, engine = make_client()
    seed(engine)
    public = client.get("/atlas")
    assert public.status_code == 200
    assert "REAL GENERAL HOSPITAL" in public.text
    assert "ST. EXAMPLE" not in public.text and "229999" not in public.text
    # A search that would match the demo hospital still keeps it out.
    assert "ST. EXAMPLE" not in client.get("/atlas?q=example").text
    with_demo = client.get("/atlas?demo=1")
    assert "ST. EXAMPLE MEDICAL CENTER" in with_demo.text
    assert "REAL GENERAL HOSPITAL" in with_demo.text
    # The phone demo's links keep working: the sheet page and its JSON stay reachable.
    assert client.get("/atlas/229999").status_code == 200
    assert client.get("/atlas/229999.json").json()["hospital"]["ccn"] == "229999"


def test_a_hospital_whose_sheet_is_held_is_under_review_with_no_figures():
    # Audit rules-correctness-9: the public page and its JSON served held sheets. A held version
    # after a published one withdrew it (a wrong website, a failed check), so neither is shown.
    from waive.atlas.schema import SheetStatus

    client, engine = make_client()
    seed(engine)
    with session_scope(engine) as session:
        published, _ = repo.latest_sheet(session, "220031")
        publish_sheet(session, published.model_copy(update={"status": SheetStatus.HELD}))
    page = client.get("/atlas/220031")
    assert page.status_code == 200
    assert "REAL GENERAL HOSPITAL" in page.text and "Under review" in page.text
    assert "household income at or below 250%" not in page.text
    assert "free_care_max_fpl" not in page.text and "version" not in page.text
    data = client.get("/atlas/220031.json").json()
    assert data == {
        "ccn": "220031",
        "name": "REAL GENERAL HOSPITAL",
        "city": "WORCESTER",
        "state": "MA",
        "status": "under_review",
        "ledger": None,
    }
    listing = client.get("/atlas?q=real").text
    assert "under review" in listing and "published" not in listing
    # Published again: the figures are back.
    with session_scope(engine) as session:
        publish_sheet(session, published.model_copy(update={"status": SheetStatus.PUBLISHED}))
    assert "household income at or below 250%" in client.get("/atlas/220031").text
    assert client.get("/atlas/220031.json").json()["status"] == "published"
    assert "published" in client.get("/atlas?q=real").text
    assert client.get("/atlas/000000.json").status_code == 404


def test_a_ceiling_only_sheet_reads_as_the_ceiling_and_never_as_free_care():
    # Decision 7.9a-2 (task 7.14): the policy states only the income up to which help is given.
    from tests.unit.test_eligibility import CEILING_QUOTE, ceiling_only_sheet

    client, engine = make_client()
    sheet = ceiling_only_sheet()
    with session_scope(engine) as session:
        repo.upsert_hospital(session, REAL_HOSPITAL)
        publish_sheet(session, sheet.model_copy(update={"hospital": real_sheet().hospital}))
    page = client.get("/atlas/220031")
    assert page.status_code == 200
    assert "eligibility.assistance_ceiling_fpl" in page.text
    assert (
        "Assistance up to 400% of the federal poverty level; the hospital decides the amount"
        in page.text
    )
    assert CEILING_QUOTE in page.text
    assert "free" not in page.text.lower()
    data = client.get("/atlas/220031.json").json()
    assert data["status"] == "published"
    ceiling = data["eligibility"]["assistance_ceiling_fpl"]
    assert (ceiling["value"], ceiling["quote"]) == ("400", CEILING_QUOTE)
    assert ceiling["source_id"] == sheet.sources[0].id and ceiling["layer"] == "documented"
    assert data["eligibility"]["free_care_max_fpl"] is None
    assert data["eligibility"]["discount_tiers"] is None


def test_atlas_list_says_why_a_hospital_without_a_sheet_was_not_scouted():
    # 7.12b: Eve could not build these hospitals. The list says so in plain words instead of the
    # bare "none"; a hospital nobody tried keeps "none", and one with a sheet keeps its status.
    blocked = "site blocks server fetches (403)"
    no_policy = "scout found no official policy"
    fairbanks = {
        **REAL_HOSPITAL,
        "ccn": "020012",
        "name": "FAIRBANKS MEMORIAL HOSPITAL",
        "city": "FAIRBANKS",
        "state": "AK",
    }
    kodiak = {**fairbanks, "ccn": "020013", "name": "KODIAK ISLAND HOSPITAL", "city": "KODIAK"}
    nome = {**fairbanks, "ccn": "020014", "name": "NORTON SOUND HOSPITAL", "city": "NOME"}
    sitka = {**fairbanks, "ccn": "020015", "name": "SITKA GENERAL HOSPITAL", "city": "SITKA"}
    client, engine = make_client()
    seed(engine)
    with session_scope(engine) as session:
        for row in (fairbanks, kodiak, nome, sitka):
            repo.upsert_hospital(session, row)
        skip = {
            ccn: SkipEntry(ccn, "AK", name, reason)
            for ccn, name, reason in (
                ("020012", "FAIRBANKS MEMORIAL HOSPITAL", blocked),
                ("020013", "KODIAK ISLAND HOSPITAL", no_policy),
                ("020015", "SITKA GENERAL HOSPITAL", blocked),
                ("220031", "REAL GENERAL HOSPITAL", blocked),  # has a published sheet
            )
        }
        assert file_skip_items(session, skip) == 4
        # Sitka's item was dealt with: the hospital is back to "none".
        [item] = [i for i in repo.open_review_items(session, kind=EVE_SKIP) if i.ccn == "020015"]
        repo.set_review_status(session, item.id, "resolved")
    page = html.unescape(client.get("/atlas").text)
    assert "FAIRBANKS, AK — not scouted (the hospital's website blocks automated reading)" in page
    assert "KODIAK, AK — not scouted (no official financial assistance policy found online)" in page
    assert "NOME, AK — none" in page and "SITKA, AK — none" in page
    # Real General has a skip item too, but its sheet decides what the list says.
    assert "WORCESTER, MA — published (version 1)" in page
    assert page.count("not scouted") == 2
    # The search narrows the list as before.
    assert "not scouted" not in client.get("/atlas?q=real").text
    assert "not scouted" in client.get("/atlas?q=fairbanks").text


def test_atlas_list_shows_an_unfamiliar_reason_as_written_and_escaped():
    # Only Eve's two reasons are reworded; any other is shown as written, as text and not markup.
    # An item with no reason at all reads "not scouted" with no cause.
    kodiak = {
        **REAL_HOSPITAL,
        "ccn": "020013",
        "name": "KODIAK ISLAND HOSPITAL",
        "city": "KODIAK",
        "state": "AK",
    }
    nome = {**kodiak, "ccn": "020014", "name": "NORTON SOUND HOSPITAL", "city": "NOME"}
    client, engine = make_client()
    with session_scope(engine) as session:
        repo.upsert_hospital(session, kodiak)
        repo.upsert_hospital(session, nome)
        repo.add_review_item(session, "020013", EVE_SKIP, {"reason": "timed out <b>twice</b>"})
        repo.add_review_item(session, "020014", EVE_SKIP, {})
    page = client.get("/atlas").text
    assert "KODIAK, AK — not scouted (timed out &lt;b&gt;twice&lt;/b&gt;)</div>" in page
    assert "<b>twice</b>" not in page
    assert "NOME, AK — not scouted</div>" in page
