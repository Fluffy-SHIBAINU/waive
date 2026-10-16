"""Cached downloads of public files (Phase 10): data.cms.gov files and catalogue, the IRS e-file
index and bulk ZIPs, Community Benefit Insight, the Census API. Every file lands once under
`var/ledger/<source>/` and is reused until `--refresh`; a failed or interrupted download never
leaves a half file behind under the real name. No keys, no credits."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "Waive/0.1 (hospital financial assistance atlas; public data)"
CMS_CATALOG_URL = "https://data.cms.gov/data.json"
CMS_API_PAGE = 5000
DOWNLOAD_TIMEOUT = httpx.Timeout(600.0, connect=30.0)


def fetch_file(
    http: httpx.Client,
    url: str,
    dest_dir: Path,
    *,
    name: str | None = None,
    refresh: bool = False,
) -> Path:
    """Download `url` into `dest_dir` as `name` (default: the URL's basename), streaming to a
    `.part` file and renaming at the end. Served from the cache unless `refresh`."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / (name or Path(urlparse(url).path).name)
    if target.exists() and not refresh:
        return target
    partial = target.with_name(target.name + ".part")
    try:
        with (
            http.stream(
                "GET",
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=DOWNLOAD_TIMEOUT,
                follow_redirects=True,
            ) as response,
            partial.open("wb") as out,
        ):
            response.raise_for_status()
            for chunk in response.iter_bytes():
                out.write(chunk)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    log.info("downloaded %s (%d bytes)", target.name, target.stat().st_size)
    return target


@dataclass(frozen=True)
class DatasetRef:
    """One data.cms.gov dataset version: where its API and CSV are and what period it covers."""

    title: str
    modified: str
    period_start: str | None
    period_end: str | None
    api_url: str | None
    csv_url: str | None
    landing_page: str | None


def cms_catalog(http: httpx.Client, cache_dir: Path, refresh: bool = False) -> list[dict[str, Any]]:
    """The datasets listed in data.cms.gov's catalogue (`data.json`, cached)."""
    path = fetch_file(http, CMS_CATALOG_URL, cache_dir, refresh=refresh)
    return list(json.loads(path.read_text(encoding="utf-8")).get("dataset", []))


def _ref(dataset: dict[str, Any]) -> DatasetRef:
    temporal = dataset.get("temporal") or []
    period = temporal[0] if isinstance(temporal, list) and temporal else {}
    api_url = csv_url = None
    for item in dataset.get("distribution") or []:
        access = str(item.get("accessURL") or "")
        if "/data-api/" in access and api_url is None:
            api_url = access
        if item.get("mediaType") == "text/csv" and csv_url is None:
            csv_url = item.get("downloadURL")
    return DatasetRef(
        title=str(dataset.get("title") or ""),
        modified=str(dataset.get("modified") or "")[:10],
        period_start=period.get("startDate") if isinstance(period, dict) else None,
        period_end=period.get("endDate") if isinstance(period, dict) else None,
        api_url=api_url,
        csv_url=csv_url,
        landing_page=dataset.get("landingPage"),
    )


def datasets_named(catalog: list[dict[str, Any]], title_prefix: str) -> list[DatasetRef]:
    """The versions of one dataset family (title prefix, any case), newest period first."""
    prefix = title_prefix.lower()
    refs = [_ref(d) for d in catalog if str(d.get("title") or "").lower().startswith(prefix)]
    return sorted(refs, key=lambda ref: (ref.period_end or "", ref.modified), reverse=True)


def latest_dataset(catalog: list[dict[str, Any]], title_prefix: str) -> DatasetRef | None:
    refs = datasets_named(catalog, title_prefix)
    return refs[0] if refs else None


def vintage_of(ref: DatasetRef) -> str:
    """'2024-01-01 to 2024-12-31, file of 2026-10-05': the period the file covers and the day
    CMS last changed it, as the pages show next to each number."""
    period = (
        f"{ref.period_start} to {ref.period_end}"
        if ref.period_start and ref.period_end
        else (ref.period_end or ref.period_start or "")
    )
    stamp = f"file of {ref.modified}" if ref.modified else ""
    return ", ".join(part for part in (period, stamp) if part)


def cms_api_rows(
    http: httpx.Client,
    api_url: str,
    filters: dict[str, str] | None = None,
    page_size: int = CMS_API_PAGE,
) -> list[dict[str, Any]]:
    """Every row of a data.cms.gov dataset through its data API, page by page."""
    rows: list[dict[str, Any]] = []
    offset = 0
    params: dict[str, Any] = {
        f"filter[{column}]": value for column, value in (filters or {}).items()
    }
    while True:
        response = http.get(
            api_url,
            params={**params, "size": page_size, "offset": offset},
            headers={"User-Agent": USER_AGENT},
            timeout=120,
        )
        response.raise_for_status()
        batch = response.json()
        rows.extend(batch)
        if len(batch) < page_size:
            return rows
        offset += page_size
