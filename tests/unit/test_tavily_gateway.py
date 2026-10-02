from decimal import Decimal

import pytest

from waive.atlas.tavily_gateway import TavilyGateway, extract_cost, map_cost, search_cost
from waive.governor import BudgetExceeded, Governor, Ledger


class FakeTavily:
    def __init__(self):
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append(("search", query, kwargs))
        return {
            "results": [
                {
                    "url": "https://www.example.org/fap.pdf",
                    "title": "Financial Assistance Policy",
                    "content": "free care",
                    "score": 0.9,
                }
            ]
        }

    def map(self, url, **kwargs):
        self.calls.append(("map", url, kwargs))
        return {
            "base_url": url,
            "results": [
                "https://www.example.org/financial-assistance",
                "https://www.example.org/billing/financial-assistance-policy.pdf",
            ],
        }

    def extract(self, urls, **kwargs):
        self.calls.append(("extract", urls, kwargs))
        ok = [{"url": url, "raw_content": f"text of {url}"} for url in urls[:-1]]
        return {"results": ok, "failed_results": [{"url": urls[-1], "error": "timeout"}]}


def make(tmp_path, cap=10):
    governor = Governor(Ledger(tmp_path / "usage.jsonl"), cap, Decimal("15"))
    fake = FakeTavily()
    return TavilyGateway(fake, governor), fake, governor


def test_search_maps_results_and_spends_one_credit(tmp_path):
    gateway, fake, governor = make(tmp_path)
    hits = gateway.search(
        "St. Example financial assistance", purpose="test", include_domains=["example.org"]
    )
    assert hits[0].url == "https://www.example.org/fap.pdf"
    assert hits[0].score == 0.9
    assert fake.calls[0][2]["include_domains"] == ["example.org"]
    assert fake.calls[0][2]["search_depth"] == "basic"
    assert governor.summary()["tavily"][0] == Decimal("1")


def test_extract_charges_only_successful_urls(tmp_path):
    gateway, _, governor = make(tmp_path)
    urls = [f"https://www.example.org/doc{i}.pdf" for i in range(6)]
    pages = gateway.extract(urls, purpose="test")
    assert len(pages) == 5
    assert pages[0].text == "text of https://www.example.org/doc0.pdf"
    assert governor.summary()["tavily"][0] == Decimal("1")


def test_budget_blocks_before_calling_tavily(tmp_path):
    gateway, fake, governor = make(tmp_path, cap=1)
    governor.record_tavily(Decimal("1"), "earlier")
    with pytest.raises(BudgetExceeded):
        gateway.search("anything", purpose="test")
    assert fake.calls == []


def test_extract_with_no_urls_is_free(tmp_path):
    gateway, fake, _ = make(tmp_path)
    assert gateway.extract([], purpose="test") == []
    assert fake.calls == []


def test_map_returns_urls_and_charges_per_ten_pages(tmp_path):
    gateway, fake, governor = make(tmp_path)
    urls = gateway.map("https://www.example.org", purpose="test", select_paths=[".*financial.*"])
    assert urls == [
        "https://www.example.org/financial-assistance",
        "https://www.example.org/billing/financial-assistance-policy.pdf",
    ]
    assert fake.calls[-1][0] == "map" and fake.calls[-1][2]["select_paths"] == [".*financial.*"]
    assert governor.summary()["tavily"][0] == Decimal("1")
    assert map_cost(0) == Decimal("1") and map_cost(25) == Decimal("3")


def test_cost_helpers():
    assert search_cost("basic") == Decimal("1")
    assert search_cost("advanced") == Decimal("2")
    assert extract_cost(6, "basic") == Decimal("2")
    assert extract_cost(5, "advanced") == Decimal("2")
    assert extract_cost(0, "basic") == Decimal("0")
