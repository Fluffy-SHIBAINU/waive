"""Eve hospitals' websites from their stored documents, and the owner's rulings kept (task 7.12c).

The Eve import takes registry rows from the CMS seed, which has no website, so a rebuild from
stored documents would first run a paid Tavily search for each hospital. `eve_website` and
`set_eve_websites` score the draft's domain with discovery's own rule on the documents already
stored. `keep_owner_decision` holds a model's reading to the owner's ruling, as `import-eve` does
when it converts a draft; `record_owner_rulings` and `stored_ruling` keep the rulings in the
database, where every rebuild finds them. Every test builds its own hospitals, sheets and
documents in memory."""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import pytest

from waive.atlas import repo
from waive.atlas.discover import DomainResult
from waive.atlas.eve_import import (
    EveWebsites,
    eve_website,
    keep_owner_decision,
    record_owner_rulings,
    set_eve_websites,
    stored_ruling,
)
from waive.atlas.publish import drop_fields
from waive.atlas.samples import SAMPLE_SOURCE_ID, st_example_sheet
from waive.atlas.schema import (
    Cited,
    HospitalRef,
    ProcedureSheet,
    SheetStatus,
    SourceDoc,
    SourceKind,
)
from waive.db import init_db, make_engine, session_scope

TODAY = date(2026, 10, 23)
OWNERSHIP = "Voluntary non-profit - Private"
REAL_GENERAL = HospitalRef(
    ccn="220031",
    name="REAL GENERAL HOSPITAL",
    city="BOSTON",
    state="MA",
    zip="02118",
    ownership=OWNERSHIP,
)
POLICY_PAGE = "Real General Hospital, 1 Main Street, Boston, MA 02118. Financial assistance policy."
POLICY_URL = "https://pay.realgeneral.org/fap"
POLICY_TITLE = "Financial Assistance | Real General Hospital"


def stored(number: int, url: str | None, title: str, text: str) -> tuple[SourceDoc, str]:
    """A hospital web document as the Eve import stores it: the source and its text."""
    source = SourceDoc(
        id=f"fap-{number:03d}",
        kind=SourceKind.HOSPITAL_WEB,
        url=url,
        title=title,
        fetched_on=TODAY,
        sha256=f"{number:064d}",
    )
    return source, text


# --- eve_website ---------------------------------------------------------------------------


def test_a_document_titled_with_the_hospital_and_naming_its_place_makes_the_site_sure():
    sources = [stored(1, POLICY_URL, POLICY_TITLE, POLICY_PAGE)]
    assert eve_website(REAL_GENERAL, "realgeneral.org", sources) == DomainResult(
        "realgeneral.org", 0.9, POLICY_URL
    )


def test_a_document_that_only_mentions_the_hospital_leaves_the_site_a_guess():
    url = "https://www.realgeneral.org/fap"
    mention = "Real General Hospital offers financial assistance to patients in Boston, MA."
    sources = [stored(1, url, "Financial Assistance Policy", mention)]
    assert eve_website(REAL_GENERAL, "realgeneral.org", sources) == DomainResult(
        "realgeneral.org", 0.6, url
    )


def test_a_title_that_names_the_hospital_but_no_place_is_a_guess_too():
    # Discovery's rule: a title alone is not enough (7.15a), the page must name the place as well.
    sources = [stored(1, POLICY_URL, POLICY_TITLE, "Financial assistance policy. Apply early.")]
    assert eve_website(REAL_GENERAL, "realgeneral.org", sources) == DomainResult(
        "realgeneral.org", 0.6, POLICY_URL
    )


def test_documents_on_other_sites_cannot_vouch_for_the_domain():
    elsewhere = [
        stored(1, "https://www.otherhealth.org/fap", POLICY_TITLE, POLICY_PAGE),
        stored(2, None, POLICY_TITLE, POLICY_PAGE),  # a document with no address at all
    ]
    guess = DomainResult("realgeneral.org", 0.6, "")
    assert eve_website(REAL_GENERAL, "realgeneral.org", elsewhere) == guess
    assert eve_website(REAL_GENERAL, "realgeneral.org", []) == guess


@pytest.mark.parametrize(
    "drafted",
    [
        "realgeneral.org",
        "www.realgeneral.org",
        "WWW.RealGeneral.org",
        "pay.realgeneral.org",
        "realgeneral.org/stalois",  # one real draft names a folder of its system's site
        "  realgeneral.org ",
    ],
)
def test_the_drafts_domain_is_read_as_the_registered_domain(drafted):
    sources = [stored(1, POLICY_URL, POLICY_TITLE, POLICY_PAGE)]
    assert eve_website(REAL_GENERAL, drafted, sources) == DomainResult(
        "realgeneral.org", 0.9, POLICY_URL
    )


@pytest.mark.parametrize(
    "drafted",
    [
        "",
        "   ",
        "facebook.com",
        "www.facebook.com",
        "mass.gov",
        "res.cloudinary.com",
        "d1234.cloudfront.net",
        "sitecorecontenthub.cloud",
    ],
)
def test_a_directory_or_a_file_host_is_never_a_hospitals_website(drafted):
    # Even a stored document on that very host, titled with the hospital, does not make it one.
    host = drafted.strip() or "realgeneral.org"
    sources = [stored(1, f"https://{host}/realgeneral", POLICY_TITLE, POLICY_PAGE)]
    assert eve_website(REAL_GENERAL, drafted, sources) is None


# --- set_eve_websites ----------------------------------------------------------------------

SURE_DOCS = [stored(1, POLICY_URL, POLICY_TITLE, POLICY_PAGE)]
GUESS_DOCS = [
    stored(
        2,
        "https://www.capeview.org/fap",
        "Financial Assistance Policy",
        "Cape View Medical Center serves Falmouth, MA.",
    )
]
RHODE_DOCS = [
    stored(
        3,
        "https://www.baysidehealth.org/fap",
        "Financial Assistance | Bayside Community Hospital",
        "Bayside Community Hospital, 5 Harbor Road, Newport, RI 02840.",
    )
]


def eve_hospital(
    session,
    ccn: str,
    name: str,
    *,
    drafted: str | None,
    city: str = "BOSTON",
    state: str = "MA",
    documents: Sequence[tuple[SourceDoc, str]] = (),
    website: str | None = None,
    confidence: float | None = None,
    item_status: str = "open",
    eve: bool = True,
) -> None:
    """A hospital as `import-eve --db` leaves it: a registry row from the CMS seed (no website
    unless `website` says otherwise), its stored documents, a version 1 draft whose hospital block
    carries the draft's domain, and an `eve-import` review item (none when `eve` is off)."""
    registry = {
        "ccn": ccn,
        "name": name,
        "city": city,
        "state": state,
        "zip": "02118",
        "hospital_type": "Acute Care Hospitals",
        "ownership": OWNERSHIP,
    }
    if website:
        registry |= {"website_domain": website, "domain_confidence": confidence}
    repo.upsert_hospital(session, registry)
    for source, text in documents:
        repo.save_source(session, source, text, ccn)
    draft = ProcedureSheet(
        hospital=HospitalRef(
            ccn=ccn,
            name=name,
            city=city,
            state=state,
            zip="02118",
            ownership=OWNERSHIP,
            website_domain=drafted,
        ),
        version=1,
        status=SheetStatus.DRAFT,
        sources=[source for source, _ in documents],
    )
    repo.add_sheet_version(session, draft, None)
    if eve:
        item = repo.add_review_item(
            session,
            ccn,
            "eve-import",
            {"notes": [], "source_text_stored": len(documents), "sources": len(documents)},
        )
        if item_status != "open":
            repo.set_review_status(session, item.id, item_status)


def websites(session) -> dict[str, tuple[str | None, float | None]]:
    return {
        row.ccn: (row.website_domain, row.domain_confidence) for row in repo.list_hospitals(session)
    }


def make_atlas():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return engine


def test_the_drafts_domain_becomes_the_registry_website_with_the_confidence_its_documents_give():
    with session_scope(make_atlas()) as session:
        # Any status of the `eve-import` item counts: an admin may have resolved it long ago.
        eve_hospital(
            session,
            "220031",
            "REAL GENERAL HOSPITAL",
            drafted="www.realgeneral.org",
            documents=SURE_DOCS,
            item_status="resolved",
        )
        eve_hospital(
            session,
            "220010",
            "CAPE VIEW MEDICAL CENTER",
            city="FALMOUTH",
            drafted="capeview.org",
            documents=GUESS_DOCS,
        )
        eve_hospital(
            session,
            "220045",
            "ALREADY HOSPITAL",
            drafted="elsewhere.org",
            website="already.org",
            confidence=1.0,
        )
        eve_hospital(session, "220050", "NO DOMAIN HOSPITAL", drafted=None)
        eve_hospital(session, "220060", "DIRECTORY HOSPITAL", drafted="facebook.com")
        # Not an Eve hospital: its sheet names a domain, but nothing here is Eve's to set.
        eve_hospital(session, "220070", "OTHER HOSPITAL", drafted="other.org", eve=False)

        first = set_eve_websites(session)
        assert first == EveWebsites(sure=1, unsure=1, already_set=1, no_domain=1, refused=1)
        after = websites(session)
        assert after == {
            "220031": ("realgeneral.org", 0.9),
            "220010": ("capeview.org", 0.6),
            "220045": ("already.org", 1.0),  # left alone, the draft's other domain ignored
            "220050": (None, None),
            "220060": (None, None),
            "220070": (None, None),
        }

        # A second run changes nothing: what was set is now a website the registry already has.
        second = set_eve_websites(session)
        assert second == EveWebsites(already_set=3, no_domain=1, refused=1)
        assert websites(session) == after


def test_a_state_limits_the_hospitals_that_are_set():
    with session_scope(make_atlas()) as session:
        eve_hospital(
            session,
            "220031",
            "REAL GENERAL HOSPITAL",
            drafted="realgeneral.org",
            documents=SURE_DOCS,
        )
        eve_hospital(
            session,
            "410001",
            "BAYSIDE COMMUNITY HOSPITAL",
            city="NEWPORT",
            state="RI",
            drafted="baysidehealth.org",
            documents=RHODE_DOCS,
        )
        assert set_eve_websites(session, "ri") == EveWebsites(sure=1)
        assert websites(session) == {
            "220031": (None, None),
            "410001": ("baysidehealth.org", 0.9),
        }
        assert set_eve_websites(session, "MA") == EveWebsites(sure=1)
        assert set_eve_websites(session) == EveWebsites(already_set=2)


# --- keep_owner_decision -------------------------------------------------------------------

CEILING_QUOTE = "household income up to 300% of the Federal Poverty Guidelines may qualify"
DISCOUNT_NOTE = (
    "eligibility.free_care_max_fpl: owner review says 250% is a discount or assistance limit, "
    "not free care; "
)
UNSURE_NOTE = (
    "eligibility.free_care_max_fpl: owner review could not tell whether 250% is free care; "
    "not recorded"
)
HOLD_NOTE = "eligibility.free_care_max_fpl: owner review holds 250%; not recorded"


def ruling(decision: str | None, value: object = "250", **more: object) -> dict[str, object]:
    """One entry of data/atlas-eve/_decisions.json, as the review page writes it."""
    return {
        "ccn": "229999",
        "decision": decision,
        "value": value,
        "note": "",
        "at": "2026-10-23T17:31:54.394Z",
        "hospital": "St. Example Medical Center",
        "state": "MA",
        "bucket": "hedged",
        **more,
    }


def with_ceiling(sheet: ProcedureSheet, value: str, quote: str) -> ProcedureSheet:
    cited = Cited[Decimal](
        value=Decimal(value), quote=quote, source_id=SAMPLE_SOURCE_ID, checked_on=TODAY
    )
    eligibility = sheet.eligibility.model_copy(update={"assistance_ceiling_fpl": cited})
    return sheet.model_copy(update={"eligibility": eligibility})


def test_a_discount_ruling_moves_the_free_care_value_to_the_assistance_ceiling():
    sheet = st_example_sheet()
    free = sheet.eligibility.free_care_max_fpl
    kept, notes = keep_owner_decision(sheet, ruling("discount"))
    assert kept.eligibility.free_care_max_fpl is None
    # The same cited field: value, quote, source, layer and checked date.
    assert kept.eligibility.assistance_ceiling_fpl == free
    assert notes == [DISCOUNT_NOTE + "recorded as assistance_ceiling_fpl"]
    # Nothing else changes, and the sheet that was passed in is left as it was.
    assert drop_fields(kept, ["eligibility.assistance_ceiling_fpl"]) == drop_fields(
        sheet, ["eligibility.free_care_max_fpl"]
    )
    assert sheet.eligibility.free_care_max_fpl is free
    assert sheet.eligibility.assistance_ceiling_fpl is None


def test_a_discount_ruling_drops_the_value_when_the_sheet_already_records_a_ceiling():
    sheet = with_ceiling(st_example_sheet(), "300", CEILING_QUOTE)
    kept, notes = keep_owner_decision(sheet, ruling("discount"))
    assert kept.eligibility.free_care_max_fpl is None
    assert kept.eligibility.assistance_ceiling_fpl == sheet.eligibility.assistance_ceiling_fpl
    assert kept.eligibility.assistance_ceiling_fpl.value == 300
    assert notes == [DISCOUNT_NOTE + "dropped, the sheet already records a ceiling"]


@pytest.mark.parametrize(
    ("verdict", "note"), [("unsure", UNSURE_NOTE), ("hold", HOLD_NOTE)], ids=["unsure", "hold"]
)
def test_an_unsure_or_hold_ruling_drops_the_value_and_records_no_ceiling(verdict, note):
    sheet = st_example_sheet()
    kept, notes = keep_owner_decision(sheet, ruling(verdict))
    assert kept.eligibility.free_care_max_fpl is None
    assert kept.eligibility.assistance_ceiling_fpl is None
    assert notes == [note]
    assert kept == drop_fields(sheet, ["eligibility.free_care_max_fpl"])


@pytest.mark.parametrize(
    "value",
    ["250", "250.0", 250, "250%", " 250 "],
    ids=["text", "text with a decimal", "number", "percent sign", "padded"],
)
def test_the_ruled_value_is_compared_as_a_number_not_as_text(value):
    kept, notes = keep_owner_decision(st_example_sheet(), ruling("unsure", value))
    assert kept.eligibility.free_care_max_fpl is None
    assert notes == [UNSURE_NOTE]


def test_a_fractional_value_is_named_as_the_owner_wrote_it():
    free = Cited[Decimal](
        value=Decimal("40.9"), quote="up to 40.9%", source_id=SAMPLE_SOURCE_ID, checked_on=TODAY
    )
    sheet = st_example_sheet()
    sheet = sheet.model_copy(
        update={"eligibility": sheet.eligibility.model_copy(update={"free_care_max_fpl": free})}
    )
    kept, notes = keep_owner_decision(sheet, ruling("hold", "40.9"))
    assert kept.eligibility.free_care_max_fpl is None
    assert notes == ["eligibility.free_care_max_fpl: owner review holds 40.9%; not recorded"]


@pytest.mark.parametrize(
    "decision",
    [
        None,
        ruling("accept"),  # the owner accepted the reading as free care
        ruling("maybe"),  # a verdict nobody defined
        ruling(None),
        {"ccn": "229999", "value": "250"},  # no verdict at all
        ruling("discount", "300"),  # another reading is another claim: the verifier judges it
        ruling("discount", "n/a"),
        ruling("discount", ""),
        ruling("discount", None),
        ruling("discount", True),
    ],
    ids=[
        "no ruling",
        "accept",
        "unknown verdict",
        "null verdict",
        "missing verdict",
        "different value",
        "unparseable value",
        "empty value",
        "no value",
        "boolean value",
    ],
)
def test_a_ruling_that_does_not_apply_changes_nothing(decision):
    sheet = st_example_sheet()
    assert keep_owner_decision(sheet, decision) == (sheet, [])


def test_a_sheet_without_a_free_care_value_is_left_alone():
    sheet = drop_fields(st_example_sheet(), ["eligibility.free_care_max_fpl"])
    assert keep_owner_decision(sheet, ruling("discount")) == (sheet, [])


# --- the rulings kept with the hospitals ---------------------------------------------------

RULING_FIELDS = ("decision", "value", "note", "at", "hospital", "state", "bucket")


def recorded(entry: dict[str, object]) -> dict[str, object]:
    """What an `owner_ruling` item holds of an entry of `_decisions.json`: its own fields."""
    return {key: entry[key] for key in RULING_FIELDS}


def test_rulings_are_recorded_as_resolved_items_and_a_rerun_records_none():
    first = ruling("discount", "300", ccn="220010")
    second = ruling(
        "unsure", "200", ccn="220045", at="2026-10-23T17:35:00.000Z", note="cannot tell"
    )
    rulings = {"220010": first, "220045": second}
    with session_scope(make_atlas()) as session:
        # Neither hospital has a sheet, or even a registry row: its ruling is recorded all the same.
        assert record_owner_rulings(session, rulings) == 2
        for ccn, entry in rulings.items():
            [item] = repo.review_items_of_kind(session, ccn, "owner_ruling")
            assert item.status == "resolved"
            assert item.detail == recorded(entry)
        # A settled decision is not work for the admin queue, so nothing is open.
        assert repo.open_review_items(session) == []
        assert repo.open_review_items(session, kind="owner_ruling") == []
        # Rerunning records nothing: each CCN already holds an item with that ruling's time.
        assert record_owner_rulings(session, rulings) == 0
        assert len(repo.review_items_of_kind(session, "220010", "owner_ruling")) == 1
        assert len(repo.review_items_of_kind(session, "220045", "owner_ruling")) == 1


def test_a_ruling_with_a_new_time_is_a_new_item_and_the_latest_is_the_stored_ruling():
    older = ruling("discount", "300", ccn="220010")
    newer = ruling("accept", "300", ccn="220010", at="2026-10-24T09:00:00.000Z")
    with session_scope(make_atlas()) as session:
        assert stored_ruling(session, "220010") is None
        assert record_owner_rulings(session, {"220010": older}) == 1
        assert stored_ruling(session, "220010") == recorded(older)
        # The owner changed their mind: the new time adds an item, and it is the one that counts.
        assert record_owner_rulings(session, {"220010": newer}) == 1
        items = repo.review_items_of_kind(session, "220010", "owner_ruling")
        assert [item.detail["decision"] for item in items] == ["discount", "accept"]
        assert [item.status for item in items] == ["resolved", "resolved"]
        assert stored_ruling(session, "220010") == recorded(newer)
        # Reading the older file again brings nothing back: its time is already recorded.
        assert record_owner_rulings(session, {"220010": older}) == 0
        assert stored_ruling(session, "220010") == recorded(newer)


def test_a_stored_ruling_is_its_hospitals_whatever_the_status_of_its_item():
    with session_scope(make_atlas()) as session:
        record_owner_rulings(session, {"220010": ruling("discount", ccn="220010")})
        [item] = repo.review_items_of_kind(session, "220010", "owner_ruling")
        for status in ("resolved", "open", "dismissed"):
            repo.set_review_status(session, item.id, status)
            assert stored_ruling(session, "220010")["decision"] == "discount"
        # Another hospital has no ruling of its own, and an item of another kind is no ruling.
        repo.add_review_item(
            session, "220045", "owner_decision", {"decision": "discount", "value": "250"}
        )
        assert stored_ruling(session, "220045") is None
        # What comes back is a copy: changing it does not change the stored item.
        stored_ruling(session, "220010")["decision"] = "accept"
        assert stored_ruling(session, "220010")["decision"] == "discount"
