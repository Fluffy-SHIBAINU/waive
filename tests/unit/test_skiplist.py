"""The scout's skip list (task 7.12b): hospitals the Eve agent tried and could not build."""

import dataclasses
import json
from pathlib import Path

import pytest

from waive.atlas import repo
from waive.atlas.skiplist import (
    EVE_SKIP,
    SKIP_LIST,
    SkipEntry,
    file_skip_items,
    load_skip_list,
    plain_reason,
)
from waive.config import Settings
from waive.db import init_db, make_engine, session_scope

BLOCKED = "site blocks server fetches (403)"
NO_POLICY = "scout found no official policy"
KETCHIKAN_URL = (
    "https://www.peacehealth.org/patient-financial-services/request-financial-assistance"
)

FAIRBANKS = {
    "ccn": "020012",
    "state": "AK",
    "name": "FAIRBANKS MEMORIAL HOSPITAL",
    "reason": BLOCKED,
    "urls_seen": [],
    "last_tried": "2026-10-23",
}
KETCHIKAN = {
    "ccn": "021311",
    "state": "AK",
    "name": "PEACEHEALTH KETCHIKAN MEDICAL CENTER",
    "reason": NO_POLICY,
    "urls_seen": [KETCHIKAN_URL],
    "last_tried": "2026-10-23",
}


def write_list(tmp_path: Path, rows) -> Path:
    path = tmp_path / "_skip.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def entries(*rows) -> dict[str, SkipEntry]:
    return {
        row["ccn"]: SkipEntry(
            row["ccn"],
            row["state"],
            row["name"],
            row["reason"],
            tuple(row["urls_seen"]),
            row["last_tried"],
        )
        for row in rows
    }


def test_load_skip_list_reads_the_listed_hospitals_and_ignores_unusable_rows(tmp_path):
    path = write_list(
        tmp_path,
        [
            FAIRBANKS,
            KETCHIKAN,
            {"state": "AK", "name": "NO CCN HOSPITAL", "reason": BLOCKED},
            {"ccn": "020099", "state": "AK", "name": "NO REASON HOSPITAL"},
            "020098",  # not an object
        ],
    )
    skip = load_skip_list(path)
    assert sorted(skip) == ["020012", "021311"]
    assert skip["020012"] == SkipEntry(
        "020012", "AK", "FAIRBANKS MEMORIAL HOSPITAL", BLOCKED, (), "2026-10-23"
    )
    assert skip["021311"].reason == NO_POLICY
    assert skip["021311"].urls_seen == (KETCHIKAN_URL,)
    with pytest.raises(dataclasses.FrozenInstanceError):
        skip["020012"].reason = "changed"  # type: ignore[misc]


def test_a_row_may_leave_out_everything_but_the_ccn_and_the_reason(tmp_path):
    skip = load_skip_list(write_list(tmp_path, [{"ccn": "020012", "reason": BLOCKED}]))
    assert skip == {"020012": SkipEntry("020012", "", "", BLOCKED, (), None)}


def test_a_missing_skip_list_is_an_empty_one(tmp_path):
    # The container image carries no data/ folder: there the scout simply has nothing to leave out.
    assert load_skip_list(tmp_path / "no-such-file.json") == {}
    assert SKIP_LIST == Path("data/atlas-eve/_skip.json")


def test_a_skip_list_that_cannot_be_read_stops_the_run_instead_of_listing_nobody(tmp_path):
    # The list guards Tavily spend: a damaged file must not quietly mean "skip nothing".
    broken = tmp_path / "broken.json"
    broken.write_text('[{"ccn": "020012"', encoding="utf-8")
    with pytest.raises(ValueError):
        load_skip_list(broken)
    with pytest.raises(ValueError, match="list of hospitals"):
        load_skip_list(write_list(tmp_path, {"020012": BLOCKED}))


def test_the_skip_list_path_is_a_setting(monkeypatch, tmp_path):
    monkeypatch.delenv("WAIVE_SCOUT_SKIP_LIST", raising=False)
    assert Settings(_env_file=None).scout_skip_list == SKIP_LIST
    monkeypatch.setenv("WAIVE_SCOUT_SKIP_LIST", str(tmp_path / "elsewhere.json"))
    assert Settings(_env_file=None).scout_skip_list == tmp_path / "elsewhere.json"


def test_file_skip_items_files_one_open_item_per_hospital_and_a_rerun_files_none():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    skip = entries(FAIRBANKS, KETCHIKAN)
    with session_scope(engine) as session:
        # Fairbanks is in the registry; Ketchikan is not (24 Puerto Rico hospitals on the real
        # list are not either), and neither needs to be.
        repo.upsert_hospital(
            session,
            {
                "ccn": "020012",
                "name": "FAIRBANKS MEMORIAL HOSPITAL",
                "city": "FAIRBANKS",
                "state": "AK",
                "hospital_type": "Acute Care Hospitals",
                "ownership": "Voluntary non-profit - Private",
            },
        )
        assert file_skip_items(session, skip) == 2
        items = repo.open_review_items(session, kind=EVE_SKIP)
        assert [(item.ccn, item.kind, item.status) for item in items] == [
            ("020012", "eve-skip", "open"),
            ("021311", "eve-skip", "open"),
        ]
        assert items[0].detail == {
            "reason": BLOCKED,
            "name": "FAIRBANKS MEMORIAL HOSPITAL",
            "state": "AK",
            "urls_seen": [],
            "last_tried": "2026-10-23",
        }
        assert items[1].detail["urls_seen"] == [KETCHIKAN_URL]
        assert items[1].detail["reason"] == NO_POLICY
        # A rerun files nothing new.
        assert file_skip_items(session, skip) == 0
        assert len(repo.open_review_items(session, kind=EVE_SKIP)) == 2


def test_only_an_open_eve_skip_item_stops_a_hospital_from_getting_another():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    skip = entries(FAIRBANKS)
    with session_scope(engine) as session:
        # An open item of another kind says nothing about the skip list.
        repo.add_review_item(session, "020012", "verification", {"problems": []})
        assert file_skip_items(session, skip) == 1
        [item] = repo.open_review_items(session, kind=EVE_SKIP)
        # A resolved one no longer counts as the hospital's open skip item.
        repo.set_review_status(session, item.id, "resolved")
        assert file_skip_items(session, skip) == 1
        assert file_skip_items(session, skip) == 0
        assert len(repo.open_review_items(session, kind=EVE_SKIP)) == 1
        assert len(repo.open_review_items(session, "020012")) == 2  # verification + eve-skip


def test_plain_reason_translates_the_two_known_causes_and_leaves_others_alone():
    assert plain_reason(BLOCKED) == "the hospital's website blocks automated reading"
    assert plain_reason(NO_POLICY) == "no official financial assistance policy found online"
    assert plain_reason("a reason nobody has seen yet") == "a reason nobody has seen yet"
