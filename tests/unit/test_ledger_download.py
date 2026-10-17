"""Cached downloads of public files and the CMS catalogue (Phase 10). No sockets: respx."""

import json

import httpx
import pytest
import respx

from waive.ledger.download import (
    CMS_CATALOG_URL,
    cms_api_rows,
    cms_catalog,
    datasets_named,
    fetch_file,
    latest_dataset,
    vintage_of,
)

CATALOG = {
    "dataset": [
        {
            "title": "Hospital Provider Cost Report : 2023-01-01",
            "modified": "2026-09-01",
            "temporal": [{"startDate": "2023-01-01", "endDate": "2023-12-31"}],
            "landingPage": "https://data.cms.gov/x/2023",
            "distribution": [
                {"accessURL": "https://data.cms.gov/data-api/v1/dataset/aaa/data"},
                {"mediaType": "text/csv", "downloadURL": "https://data.cms.gov/f/2023.csv"},
            ],
        },
        {
            "title": "Hospital Provider Cost Report : 2024-01-02",
            "modified": "2026-10-05",
            "temporal": [{"startDate": "2024-01-01", "endDate": "2024-12-31"}],
            "landingPage": "https://data.cms.gov/x/2024",
            "distribution": [
                {"accessURL": "https://data.cms.gov/data-api/v1/dataset/bbb/data"},
                {"mediaType": "text/csv", "downloadURL": "https://data.cms.gov/f/2024.csv"},
            ],
        },
        {"title": "Something else", "modified": "2026-10-06", "distribution": []},
    ]
}


@respx.mock
def test_fetch_file_streams_once_and_then_serves_the_cache(tmp_path):
    route = respx.get("https://example.org/files/a.csv").mock(
        return_value=httpx.Response(200, content=b"a,b\n1,2\n")
    )
    with httpx.Client() as http:
        path = fetch_file(http, "https://example.org/files/a.csv", tmp_path / "src")
        assert path == tmp_path / "src" / "a.csv" and path.read_bytes() == b"a,b\n1,2\n"
        assert fetch_file(http, "https://example.org/files/a.csv", tmp_path / "src") == path
        assert route.call_count == 1
        fetch_file(http, "https://example.org/files/a.csv", tmp_path / "src", refresh=True)
        assert route.call_count == 2
        named = fetch_file(http, "https://example.org/files/a.csv", tmp_path, name="MA.json")
        assert named == tmp_path / "MA.json"
    assert not list((tmp_path / "src").glob("*.part"))


@respx.mock
def test_fetch_file_leaves_no_file_behind_when_the_download_fails(tmp_path):
    respx.get("https://example.org/missing.csv").mock(return_value=httpx.Response(404))
    with httpx.Client() as http:
        try:
            fetch_file(http, "https://example.org/missing.csv", tmp_path)
        except httpx.HTTPStatusError:
            pass
        else:  # pragma: no cover
            raise AssertionError("a 404 must raise")
    assert list(tmp_path.iterdir()) == []


# The catalogue that names a URL is itself a download: a URL on another scheme, or one that
# names no plain file, is refused before any request or any write. No route is mocked, so a
# request that slipped through would fail as unmocked.
@respx.mock
@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://example.org/files/a.csv", "is not an https URL"),
        ("ftp://example.org/files/a.csv", "is not an https URL"),
        ("https://example.org/files/", "'' is not a plain file name"),
        ("https://example.org", "'' is not a plain file name"),
        ("https://example.org/files/..", "'..' is not a plain file name"),
        ("https://example.org/files/%2e%2e", "'..' is not a plain file name"),
        ("https://example.org/files/.", "'.' is not a plain file name"),
        ("https://example.org/a%2F..%2Fb.csv", "'a/../b.csv' is not a plain file name"),
        ("https://example.org/a%5C..%5Cb.csv", "is not a plain file name"),
    ],
)
def test_fetch_file_refuses_a_url_that_is_not_https_or_names_no_plain_file(tmp_path, url, message):
    with httpx.Client() as http:
        with pytest.raises(ValueError, match=message):
            fetch_file(http, url, tmp_path / "src")
    assert not (tmp_path / "src").exists()


@respx.mock
def test_fetch_file_checks_a_given_name_the_same_way(tmp_path):
    with httpx.Client() as http:
        with pytest.raises(ValueError, match="'../x.json' is not a plain file name"):
            fetch_file(http, "https://example.org/files/a.csv", tmp_path, name="../x.json")
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_fetch_file_saves_under_the_unquoted_basename(tmp_path):
    respx.get("https://example.org/files/a%20b.csv").mock(
        return_value=httpx.Response(200, content=b"x")
    )
    with httpx.Client() as http:
        assert fetch_file(http, "https://example.org/files/a%20b.csv", tmp_path) == tmp_path / (
            "a b.csv"
        )


@respx.mock
def test_catalog_picks_the_newest_dataset_by_title(tmp_path):
    respx.get(CMS_CATALOG_URL).mock(return_value=httpx.Response(200, json=CATALOG))
    with httpx.Client() as http:
        catalog = cms_catalog(http, tmp_path)
        assert (tmp_path / "data.json").exists()
        assert len(json.loads((tmp_path / "data.json").read_text())["dataset"]) == 3
    newest = latest_dataset(catalog, "Hospital Provider Cost Report")
    assert newest.csv_url == "https://data.cms.gov/f/2024.csv"
    assert newest.api_url == "https://data.cms.gov/data-api/v1/dataset/bbb/data"
    assert newest.modified == "2026-10-05" and newest.period_end == "2024-12-31"
    assert newest.landing_page == "https://data.cms.gov/x/2024"
    assert [d.period_end for d in datasets_named(catalog, "hospital provider cost report")] == [
        "2024-12-31",
        "2023-12-31",
    ]
    assert latest_dataset(catalog, "No such file") is None
    assert vintage_of(newest) == "2024-01-01 to 2024-12-31, file of 2026-10-05"


@respx.mock
def test_cms_api_rows_paginates_with_filters():
    api = "https://data.cms.gov/data-api/v1/dataset/bbb/data"
    pages = {
        "0": [{"CCN": "220001"}, {"CCN": "220002"}],
        "2": [{"CCN": "220003"}],
    }

    def answer(request):
        assert request.url.params["filter[STATE]"] == "MA"
        assert request.url.params["size"] == "2"
        return httpx.Response(200, json=pages[request.url.params["offset"]])

    respx.get(api).mock(side_effect=answer)
    with httpx.Client() as http:
        rows = cms_api_rows(http, api, {"STATE": "MA"}, page_size=2)
    assert [row["CCN"] for row in rows] == ["220001", "220002", "220003"]


def test_a_dataset_family_is_matched_whole_and_a_document_landing_page_is_dropped():
    catalog = [
        {
            "title": "Medicare Inpatient Hospitals - by Provider : 2024-01-01",
            "modified": "2026-04-23",
            "temporal": [{"startDate": "2024-01-01", "endDate": "2024-12-31"}],
            "landingPage": {"@type": "Document", "accessURL": "https://data.cms.gov/x"},
            "distribution": [],
        },
        {
            "title": "Medicare Inpatient Hospitals - by Provider and Service : 2024-01-01",
            "modified": "2026-04-23",
            "temporal": [{"startDate": "2024-01-01", "endDate": "2024-12-31"}],
            "distribution": [],
        },
    ]
    found = latest_dataset(catalog, "medicare inpatient hospitals - by provider")
    assert found.title.startswith("Medicare Inpatient Hospitals - by Provider :")
    assert found.landing_page is None
    assert len(datasets_named(catalog, "Medicare Inpatient Hospitals - by Provider")) == 1
    assert latest_dataset(catalog, "Medicare Inpatient Hospitals") is None
