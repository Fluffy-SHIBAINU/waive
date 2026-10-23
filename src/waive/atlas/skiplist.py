"""The scout's skip list: hospitals the Eve waive-agent tried and could not build (task 7.12b).

`data/atlas-eve/_skip.json` names them with the reason each was given up on: the hospital's site
refuses server fetches, or the scout found no official policy. Waive never scouts a listed
hospital again. The scheduler and `waive atlas build` leave it out, each one gets an open
`eve-skip` review item carrying the reason, and the public atlas list shows it as not scouted
with that cause in plain words.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from waive.atlas import repo

EVE_SKIP = "eve-skip"
SKIP_LIST = Path("data/atlas-eve/_skip.json")
# The public atlas words Eve's two reasons for readers; any other reason is shown as written.
PLAIN_REASONS = {
    "site blocks server fetches (403)": "the hospital's website blocks automated reading",
    "scout found no official policy": "no official financial assistance policy found online",
}


@dataclass(frozen=True)
class SkipEntry:
    ccn: str
    state: str
    name: str
    reason: str
    urls_seen: tuple[str, ...] = ()
    last_tried: str | None = None


def load_skip_list(path: Path = SKIP_LIST) -> dict[str, SkipEntry]:
    """The listed hospitals by CCN. A missing file is an empty list: the container image carries
    no data/ folder, and there the scout has nothing to leave out. A file that is not a JSON list
    is an error rather than an empty list, because the list guards Tavily spend. Rows that are not
    objects, or that lack a ccn or a reason, are ignored."""
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a JSON list of hospitals")
    skip: dict[str, SkipEntry] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("ccn") or not row.get("reason"):
            continue
        urls = row.get("urls_seen")
        entry = SkipEntry(
            ccn=str(row["ccn"]),
            state=str(row.get("state") or ""),
            name=str(row.get("name") or ""),
            reason=str(row["reason"]),
            urls_seen=tuple(str(url) for url in urls) if isinstance(urls, list) else (),
            last_tried=str(row["last_tried"]) if row.get("last_tried") else None,
        )
        skip[entry.ccn] = entry
    return skip


def file_skip_items(session: Session, skip: Mapping[str, SkipEntry]) -> int:
    """File one open `eve-skip` review item for every listed hospital that has none open, so
    the admin queue and the public atlas list say why it was never scouted and a rerun files
    nothing new. The hospital need not be in the registry (24 Puerto Rico hospitals on the list
    are not). Returns how many items were filed."""
    already = {item.ccn for item in repo.open_review_items(session, kind=EVE_SKIP)}
    filed = 0
    for entry in skip.values():
        if entry.ccn in already:
            continue
        repo.add_review_item(
            session,
            entry.ccn,
            EVE_SKIP,
            {
                "reason": entry.reason,
                "name": entry.name,
                "state": entry.state,
                "urls_seen": list(entry.urls_seen),
                "last_tried": entry.last_tried,
            },
        )
        filed += 1
    return filed


def plain_reason(reason: str) -> str:
    """The cause as the public atlas says it."""
    return PLAIN_REASONS.get(reason, reason)
