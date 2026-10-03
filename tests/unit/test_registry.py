from pathlib import Path

import httpx
import respx

from waive.atlas import repo
from waive.atlas.registry import (
    CMS_DATASTORE_URL,
    STATES,
    fetch_cms_rows,
    is_eligible,
    normalize_phone,
    row_to_hospital,
    seed_all_states,
    seed_state,
)
from waive.db import init_db, make_engine, session_scope

ROWS = [
    {
        "facility_id": "220001",
        "facility_name": "UMASS MEMORIAL HEALTHALLIANCE HOSPITALS",
        "address": "60 HOSPITAL ROAD",
        "citytown": "LEOMINSTER",
        "state": "MA",
        "zip_code": "01453",
        "telephone_number": "(978) 466-2000",
        "hospital_type": "Acute Care Hospitals",
        "hospital_ownership": "Voluntary non-profit - Other",
    },
    {
        "facility_id": "220002",
        "facility_name": "PROFIT GENERAL",
        "address": "1 MAIN ST",
        "citytown": "BOSTON",
        "state": "MA",
        "zip_code": "02110",
        "telephone_number": "6175550111",
        "hospital_type": "Acute Care Hospitals",
        "hospital_ownership": "Proprietary",
    },
    {
        "facility_id": "224003",
        "facility_name": "QUIET PSYCHIATRIC",
        "address": "2 MAIN ST",
        "citytown": "BOSTON",
        "state": "MA",
        "zip_code": "02110",
        "telephone_number": "",
        "hospital_type": "Psychiatric",
        "hospital_ownership": "Voluntary non-profit - Private",
    },
    {
        "facility_id": "221300",
        "facility_name": "TINY CRITICAL ACCESS",
        "address": "3 MAIN ST",
        "citytown": "ATHOL",
        "state": "MA",
        "zip_code": "01331",
        "telephone_number": "1-978-555-0122",
        "hospital_type": "Critical Access Hospitals",
        "hospital_ownership": "Voluntary non-profit - Church",
    },
]


def test_eligibility_filter():
    assert [is_eligible(row) for row in ROWS] == [True, False, False, True]


def test_phone_normalization():
    assert normalize_phone("(978) 466-2000") == "978-466-2000"
    assert normalize_phone("1-978-555-0122") == "978-555-0122"
    assert normalize_phone("") is None


def test_row_to_hospital_maps_columns():
    hospital = row_to_hospital(ROWS[0])
    assert hospital == {
        "ccn": "220001",
        "name": "UMASS MEMORIAL HEALTHALLIANCE HOSPITALS",
        "address": "60 HOSPITAL ROAD",
        "city": "LEOMINSTER",
        "state": "MA",
        "zip": "01453",
        "phone": "978-466-2000",
        "hospital_type": "Acute Care Hospitals",
        "ownership": "Voluntary non-profit - Other",
    }


@respx.mock
def test_fetch_pages_until_short_page():
    route = respx.get(CMS_DATASTORE_URL).mock(
        side_effect=[
            httpx.Response(200, json={"results": ROWS[:2], "count": 4}),
            httpx.Response(200, json={"results": ROWS[2:], "count": 4}),
        ]
    )
    with httpx.Client() as http:
        rows = fetch_cms_rows("MA", http, page_size=2)
    assert [row["facility_id"] for row in rows] == ["220001", "220002", "224003", "221300"]
    first = route.calls[0].request.url
    assert first.params["conditions[0][property]"] == "state"
    assert first.params["conditions[0][value]"] == "MA"
    assert first.params["limit"] == "2"


@respx.mock
def test_seed_state_keeps_eligible_and_writes_snapshot(tmp_path):
    respx.get(CMS_DATASTORE_URL).mock(
        return_value=httpx.Response(200, json={"results": ROWS, "count": 4})
    )
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session, httpx.Client() as http:
        report = seed_state(session, "MA", http, snapshot_dir=tmp_path)
    assert (report.fetched, report.kept) == (4, 2)
    assert report.snapshot.exists() and report.snapshot.suffix == ".json"
    with session_scope(engine) as session:
        assert [h.ccn for h in repo.list_hospitals(session, "MA")] == ["221300", "220001"]
    assert isinstance(report.snapshot, Path)


VT_ROWS = [
    {
        "facility_id": "470003",
        "facility_name": "RUTLAND REGIONAL MEDICAL CENTER",
        "address": "160 ALLEN STREET",
        "citytown": "RUTLAND",
        "state": "VT",
        "zip_code": "05701",
        "telephone_number": "(802) 775-7111",
        "hospital_type": "Acute Care Hospitals",
        "hospital_ownership": "Voluntary non-profit - Private",
    }
]


def test_states_cover_fifty_states_and_dc():
    assert len(STATES) == 51 and len(set(STATES)) == 51
    assert {"MA", "CA", "WA", "DC", "WY"} <= set(STATES)
    assert all(len(code) == 2 and code.isupper() for code in STATES)


@respx.mock
def test_seed_all_states_continues_past_a_failing_state(tmp_path):
    def by_state(request):
        state = request.url.params["conditions[0][value]"]
        if state == "RI":
            return httpx.Response(500)
        rows = {"MA": ROWS, "VT": VT_ROWS}[state]
        return httpx.Response(200, json={"results": rows, "count": len(rows)})

    respx.get(CMS_DATASTORE_URL).mock(side_effect=by_state)
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    seen = []
    with session_scope(engine) as session, httpx.Client() as http:
        reports = seed_all_states(
            session, http, snapshot_dir=tmp_path, states=("MA", "RI", "VT"), progress=seen.append
        )
    assert [(r.state, r.fetched, r.kept) for r in reports] == [
        ("MA", 4, 2),
        ("RI", 0, 0),
        ("VT", 1, 1),
    ]
    assert reports[1].error is not None and "500" in reports[1].error
    assert reports[1].snapshot is None
    assert reports[0].snapshot.exists() and reports[2].snapshot.exists()
    assert len(seen) == 3
    with session_scope(engine) as session:
        assert [h.ccn for h in repo.list_hospitals(session, "VT")] == ["470003"]
        assert len(repo.list_hospitals(session)) == 3
