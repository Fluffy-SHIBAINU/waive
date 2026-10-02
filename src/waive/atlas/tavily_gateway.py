"""Budget-aware wrapper around the Tavily API (spec section 8)."""

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol

from waive.config import Settings
from waive.governor import Governor

Depth = Literal["basic", "advanced"]


class TavilyLike(Protocol):
    def search(self, query: str, **kwargs: Any) -> dict[str, Any]: ...

    def extract(self, urls: list[str], **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str
    content: str
    score: float


@dataclass(frozen=True)
class ExtractedPage:
    url: str
    text: str


def search_cost(depth: Depth) -> Decimal:
    return Decimal(2 if depth == "advanced" else 1)


def extract_cost(successful_urls: int, depth: Depth) -> Decimal:
    per_five = 2 if depth == "advanced" else 1
    return Decimal(math.ceil(successful_urls / 5) * per_five)


class TavilyGateway:
    def __init__(self, client: TavilyLike, governor: Governor) -> None:
        self._client = client
        self._governor = governor

    def search(
        self,
        query: str,
        *,
        purpose: str,
        include_domains: list[str] | None = None,
        max_results: int = 5,
        depth: Depth = "basic",
        topic: str = "general",
    ) -> list[SearchHit]:
        cost = search_cost(depth)
        self._governor.ensure_tavily(cost)
        raw = self._client.search(
            query=query,
            search_depth=depth,
            topic=topic,
            max_results=max_results,
            include_domains=include_domains or [],
        )
        self._governor.record_tavily(cost, purpose)
        return [
            SearchHit(
                url=item["url"],
                title=item.get("title") or "",
                content=item.get("content") or "",
                score=float(item.get("score") or 0.0),
            )
            for item in raw.get("results", [])
        ]

    def extract(
        self, urls: list[str], *, purpose: str, depth: Depth = "basic"
    ) -> list[ExtractedPage]:
        if not urls:
            return []
        self._governor.ensure_tavily(extract_cost(len(urls), depth))
        raw = self._client.extract(urls=urls, extract_depth=depth)
        results = raw.get("results", [])
        self._governor.record_tavily(extract_cost(len(results), depth), purpose)
        return [
            ExtractedPage(url=item["url"], text=item.get("raw_content") or "") for item in results
        ]

    def map(
        self,
        url: str,
        *,
        purpose: str,
        select_paths: list[str] | None = None,
        limit: int = 30,
    ) -> list[str]:
        """Discover a site's URLs (Tavily Map), optionally limited to matching paths."""
        self._governor.ensure_tavily(map_cost(limit))
        raw = self._client.map(url=url, max_depth=2, limit=limit, select_paths=select_paths or [])
        results = raw.get("results", []) if isinstance(raw, dict) else []
        urls = [item if isinstance(item, str) else str(item.get("url", "")) for item in results]
        urls = [item for item in urls if item]
        self._governor.record_tavily(map_cost(len(urls)), purpose)
        return urls


def map_cost(pages: int) -> Decimal:
    """Tavily Map bills one credit per ten pages discovered (minimum one)."""
    return Decimal(max(1, math.ceil(pages / 10)))


def make_tavily_gateway(settings: Settings, governor: Governor) -> TavilyGateway:
    from tavily import TavilyClient

    if settings.tavily_api_key is None:
        raise RuntimeError("TAVILY_API_KEY is not set")
    client = TavilyClient(api_key=settings.tavily_api_key.get_secret_value())
    return TavilyGateway(client, governor)
