from pathlib import Path

import httpx
import respx

from waive.atlas import repo
from waive.atlas.registry import (
    CMS_DATASTORE_URL,
    fetch_cms_rows,
    is_eligible,
    normalize_phone,
    row_to_hospital,
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
