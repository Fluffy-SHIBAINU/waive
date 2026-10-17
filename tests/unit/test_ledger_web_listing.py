"""The ranked list at /ledger costs the same however long it is: the joined hospital and
metrics rows in one statement, every hospital's newest sheet status in another, and the three
words per row from `explain` straight off the metrics row, never a per-hospital view.

The words are compared with `explain`'s own output and with the atlas page's cards, so the
wording may change without touching this file; nothing here pins a sentence."""

from datetime import date
from decimal import Decimal

from sqlalchemy import event

from waive.atlas import repo as atlas_repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.atlas.schema import SheetStatus
from waive.db import session_scope
from waive.ledger import explain, repo
from waive.ledger.models import ExposureLabel, GapLabel
from waive.ledger.view import ledger_view

from tests.unit.test_web_app import make_client
from tests.unit.test_web_senior import HOSPITAL as DEMO

TODAY = date(2026, 10, 16)
STATES = ("MA", "NJ", "NY")
BASE = {
    "zip": "02118",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
}
# The metrics rows the list can meet, cycled over the seeded hospitals: every input known; a
# stated limit alone; a limit the hospital out-spends with bad debt but no flags; limit and
# spending in line with every flag answered no; spending alone; a row with nothing to say.
SHAPES: tuple[dict, ...] = (
    {
        "stated_fpl": 300.0, "stated_source": "sheet", "charity_cost_pct": 0.9, "gap": 1.0,
        "gap_label": GapLabel.PROMISES_MORE.value, "exposure_rank": 1.0, "exposure_label": "high",
        "credit_reporting": True, "sells_debt": False, "legal_action": True,
        "flag_line": "engaged", "bad_debt_pct": 3.6,
    },
    {
        "stated_fpl": 200.0, "stated_source": "irs990", "exposure_rank": 0.0,
        "exposure_label": "low", "credit_reporting": False, "sells_debt": False,
        "legal_action": False, "flag_line": "engaged",
    },
    {
        "stated_fpl": 200.0, "stated_source": "sheet", "charity_cost_pct": 5.0, "gap": -1.0,
        "gap_label": GapLabel.PROMISES_LESS.value, "bad_debt_pct": 1.0,
    },
    {
        "stated_fpl": 250.0, "stated_source": "irs990", "charity_cost_pct": 3.0, "gap": 0.0,
        "gap_label": GapLabel.DELIVERS.value, "exposure_rank": 0.5, "exposure_label": "medium",
        "lawsuits": False, "liens": False, "credit_reporting": False, "sells_debt": False,
        "legal_action": False, "flag_line": "engaged", "bad_debt_pct": 2.0,
    },
    {"charity_cost_pct": 2.5, "gap": None},
    {"behaviour_computed_on": TODAY},
)  # fmt: skip


def ccn_of(i: int) -> str:
    return f"22{i:04d}"


def hospital_of(i: int) -> dict:
    return {
        **BASE,
        "ccn": ccn_of(i),
        "name": f"HOSPITAL {i:02d}",
        "city": f"TOWN {i:02d}",
        "state": STATES[i % len(STATES)],
    }


def shape_of(i: int) -> dict:
    return SHAPES[i % len(SHAPES)]


def sheet_for(hospital: dict, fpl: str, status: SheetStatus = SheetStatus.PUBLISHED):
    sample = st_example_sheet()
    ref = sample.hospital.model_copy(
        update={
            "ccn": hospital["ccn"],
            "name": hospital["name"].title(),
            "city": hospital["city"].title(),
            "state": hospital["state"],
        }
    )
    limit = sample.eligibility.free_care_max_fpl.model_copy(update={"value": Decimal(fpl)})
    eligibility = sample.eligibility.model_copy(update={"free_care_max_fpl": limit})
    return sample.model_copy(update={"hospital": ref, "eligibility": eligibility, "status": status})


def seed(engine, count: int) -> None:
    """`count` hospitals with metrics rows. Every third has a published sheet, the next a held
    one, the next none; hospital 0 publishes first and is then held, so its newest version is
    the held one. The demo hospital gets a metrics row too and must stay off the list."""
    with session_scope(engine) as session:
        for i in range(count):
            hospital = hospital_of(i)
            atlas_repo.upsert_hospital(session, hospital)
            if i % 3 == 0:
                publish_sheet(session, sheet_for(hospital, "300"))
            elif i % 3 == 1:
                publish_sheet(session, sheet_for(hospital, "250", SheetStatus.HELD))
            repo.upsert_metrics(session, hospital["ccn"], **shape_of(i))
        if count:
            publish_sheet(session, sheet_for(hospital_of(0), "250", SheetStatus.HELD))
        atlas_repo.upsert_hospital(session, DEMO)
        publish_sheet(session, st_example_sheet())
        repo.upsert_metrics(session, DEMO["ccn"], exposure_rank=0.9, exposure_label="high")


def expected_words(shape: dict) -> str:
    """The row's three words as `explain` gives them and the template's fallbacks shape them:
    the exposure label as a headline, the gap headline with its first letter lowered, and the
    collections headline, which always has something to say."""
    label = shape.get("exposure_label")
    exposure = explain.exposure_headline(ExposureLabel(label)) if label else "not enough data"
    stated, charity = shape.get("stated_fpl"), shape.get("charity_cost_pct")
    if stated is None and charity is None:
        gap = "not enough data"
    else:
        gap_label = GapLabel(shape["gap_label"]) if shape.get("gap_label") else None
        headline = explain.gap_headline(gap_label, stated, charity)
        gap = headline[0].lower() + headline[1:]
    collections = explain.collections_headline(
        shape.get("lawsuits"),
        shape.get("liens"),
        shape.get("credit_reporting"),
        shape.get("sells_debt"),
        shape.get("legal_action"),
        shape.get("bad_debt_pct"),
    )
    return f"Seniors at risk: {exposure} · Free care: {gap} · Collections: {collections}"


def get_counting(client, engine, path: str) -> tuple[str, list[str]]:
    """The page at `path` and every SQL statement the engine ran to build it."""
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        page = client.get(path)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert page.status_code == 200
    return page.text, seen


def test_the_list_runs_the_same_two_statements_whatever_its_length():
    counts = {}
    for count in (3, 30):
        client, engine = make_client()
        seed(engine, count)
        text, statements = get_counting(client, engine, "/ledger")
        assert len(statements) < 10, statements
        assert all(s.lstrip().upper().startswith("SELECT") for s in statements), statements
        counts[count] = len(statements)
        for i in range(count):
            name = hospital_of(i)["name"]
            assert name in text, name
        # A state filter and another sort change nothing: the filter runs in Python.
        _, filtered = get_counting(client, engine, "/ledger?state=ma&sort=gap")
        assert len(filtered) == len(statements)
    assert counts[3] == counts[30] == 2


def test_every_row_says_what_explain_says_and_what_the_atlas_card_says():
    client, engine = make_client()
    seed(engine, 30)
    text = client.get("/ledger").text
    for i in range(30):
        shape = shape_of(i)
        assert f'<p class="muted">{expected_words(shape)}</p>' in text, (i, shape)
    # Each shape's words are the headlines the atlas page's cards show for the same row.
    with session_scope(engine) as session:
        for i in range(len(SHAPES)):
            view = ledger_view(session, ccn_of(i))
            assert view is not None
            words = expected_words(shape_of(i))
            assert f"Collections: {view.collections.headline}" in words
            if view.gap.status.value != "unknown":
                head = view.gap.headline
                assert f"Free care: {head[0].lower()}{head[1:]}" in words
            else:
                assert "Free care: not enough data" in words
    # The six shapes give six different lines; none of them is an enum name or a blank.
    lines = {expected_words(shape) for shape in SHAPES}
    assert len(lines) == len(SHAPES)
    for ugly in ("promises_more", "None", "True", "False"):
        assert ugly not in text, ugly
    assert "Seniors at risk: not enough data" in text and "Free care: not enough data" in text


def test_the_sheet_status_comes_from_the_newest_version_and_links_only_when_one_exists():
    client, engine = make_client()
    seed(engine, 30)
    text = client.get("/ledger").text
    # Hospital 0 was published and then held: the held version is the newest one.
    assert '<a href="/atlas/220000"><strong>HOSPITAL 00</strong></a>' in text
    assert "<br>TOWN 00, MA — under review" in text
    assert '<a href="/atlas/220003"><strong>HOSPITAL 03</strong></a>' in text
    assert "<br>TOWN 03, MA — published" in text
    assert '<a href="/atlas/220001"><strong>HOSPITAL 01</strong></a>' in text
    assert "<br>TOWN 01, NJ — under review" in text
    assert "<strong>HOSPITAL 02</strong><br>TOWN 02, NY — no policy sheet yet" in text
    assert 'href="/atlas/220002"' not in text
    assert text.count("— published") == 9 and text.count("— under review") == 11
    assert text.count("— no policy sheet yet") == 10
    # The demo hospital has a metrics row and a sheet and still never appears.
    assert DEMO["name"] not in text and DEMO["ccn"] not in text


def names_in_order(text: str, count: int) -> list[int]:
    """The seeded hospitals the page shows, top to bottom, by number."""
    found = [(text.find(f"<strong>HOSPITAL {i:02d}</strong>"), i) for i in range(count)]
    return [i for at, i in sorted(found) if at >= 0]


def test_the_sorts_and_the_state_filter_still_hold():
    client, engine = make_client()
    seed(engine, 12)
    by_exposure = names_in_order(client.get("/ledger").text, 12)
    ranks = [shape_of(i).get("exposure_rank") for i in by_exposure]
    known = [rank for rank in ranks if rank is not None]
    assert known == sorted(known, reverse=True)
    assert ranks[len(known) :] == [None] * (12 - len(known))
    assert by_exposure[:2] == [0, 6]  # rank 1.0, ties in CCN order
    by_gap = names_in_order(client.get("/ledger?sort=gap").text, 12)
    gaps = [shape_of(i).get("gap") for i in by_gap]
    assert gaps[:4] == [1.0, 1.0, 0.0, 0.0] and gaps[-4:] == [None] * 4
    assert names_in_order(client.get("/ledger?sort=name").text, 12) == list(range(12))
    nj = client.get("/ledger?state=nj").text
    assert names_in_order(nj, 12) == [i for i in by_exposure if i % 3 == 1]
    assert '<option value="NJ" selected>NJ</option>' in nj
    assert '<option value="MA">MA</option>' in nj and '<option value="NY">NY</option>' in nj
    empty = client.get("/ledger?state=WY").text
    assert "No hospitals have ledger data yet." in empty
