"""Hospital registry seeded from the CMS Hospital General Information dataset (spec §8 step 1)."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from waive.atlas import repo

CMS_DATASTORE_URL = "https://data.cms.gov/provider-data/api/1/datastore/query/xubh-q36u/0"
ELIGIBLE_TYPES = {"Acute Care Hospitals", "Critical Access Hospitals"}
NONPROFIT_PREFIX = "Voluntary non-profit"

# The 50 states and the District of Columbia, as CMS spells them in the `state` column.
STATES: tuple[str, ...] = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL",
    "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME",
    "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI",
    "WY",
)  # fmt: skip


def fetch_cms_rows(state: str, http: httpx.Client, page_size: int = 500) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = {
            "limit": page_size,
            "offset": offset,
            "conditions[0][property]": "state",
            "conditions[0][value]": state.upper(),
        }
        response = http.get(CMS_DATASTORE_URL, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        batch = payload.get("results", [])
        rows.extend(batch)
        count = payload.get("count")
        if len(batch) < page_size or (count is not None and len(rows) >= int(count)):
            return rows
        offset += page_size


def is_eligible(row: dict[str, Any]) -> bool:
    return row.get("hospital_type") in ELIGIBLE_TYPES and str(
        row.get("hospital_ownership", "")
    ).startswith(NONPROFIT_PREFIX)


def normalize_phone(raw: str | None) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"


def row_to_hospital(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ccn": row["facility_id"],
        "name": row["facility_name"].strip(),
        "address": row.get("address", "").strip(),
        "city": row["citytown"].strip(),
        "state": row["state"].upper(),
        "zip": str(row.get("zip_code", "")).strip()[:10],
        "phone": normalize_phone(row.get("telephone_number")),
        "hospital_type": row["hospital_type"],
        "ownership": row["hospital_ownership"],
    }


@dataclass(frozen=True)
class SeedReport:
    fetched: int
    kept: int
    snapshot: Path | None
    state: str = ""
    error: str | None = None


def seed_state(
    session: Session, state: str, http: httpx.Client, snapshot_dir: Path = Path("data/seed")
) -> SeedReport:
    rows = fetch_cms_rows(state, http)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).date().isoformat()
    snapshot = snapshot_dir / f"cms-hospitals-{state.upper()}-{stamp}.json"
    snapshot.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    kept = 0
    for row in rows:
        if is_eligible(row):
            repo.upsert_hospital(session, row_to_hospital(row))
            kept += 1
    return SeedReport(fetched=len(rows), kept=kept, snapshot=snapshot, state=state.upper())


def seed_all_states(
    session: Session,
    http: httpx.Client,
    snapshot_dir: Path = Path("data/seed"),
    states: tuple[str, ...] = STATES,
    progress: Callable[[SeedReport], None] | None = None,
) -> list[SeedReport]:
    """Seed every state in turn (free: the CMS datastore needs no key). A state whose request
    fails is reported with `error` and skipped; the others still land. Commits after each state
    so a crash mid-way keeps what was seeded."""
    reports: list[SeedReport] = []
    for state in states:
        try:
            report = seed_state(session, state, http, snapshot_dir)
        except (httpx.HTTPError, ValueError, KeyError) as error:
            report = SeedReport(
                fetched=0,
                kept=0,
                snapshot=None,
                state=state.upper(),
                error=f"{type(error).__name__}: {error}"[:200],
            )
        session.commit()
        reports.append(report)
        if progress is not None:
            progress(report)
    return reports
