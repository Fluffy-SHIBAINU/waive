"""Refetch every source of the converted Eve drafts with Waive's own downloader and run the real
verifier: trim quotes to the spans the documents contain, prune list fields to the items their
quotes name, reject what fails, and write the grounded sheets.

    uv run python scripts/refetch_verify_eve.py data/atlas-eve data/atlas-eve-verified
"""

import hashlib
import json
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import httpx

from waive.atlas.fetch import html_text, pdf_text
from waive.atlas.pipeline import _ground
from waive.atlas.schema import ProcedureSheet
from waive.atlas.verify import verify_sheet

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/atlas-eve")
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("data/atlas-eve-verified")
WORKERS = int(sys.argv[3]) if len(sys.argv) > 3 else 16
# Every fetched document is kept here as text, keyed by source id, with an index that records the
# SHA-256 of the bytes fetched now next to the SHA-256 the agent recorded when it read the document.
SOURCES = Path(sys.argv[4]) if len(sys.argv) > 4 else Path.home() / "waive-atlas" / "sources"
OUT.mkdir(parents=True, exist_ok=True)
SOURCES.mkdir(parents=True, exist_ok=True)
_index_lock = threading.Lock()

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

_local = threading.local()


def client() -> httpx.Client:
    if not hasattr(_local, "http"):
        _local.http = httpx.Client(follow_redirects=False, timeout=15.0)
    return _local.http


def fetch_one(source_id: str, url: str, recorded_sha256: str) -> tuple[str | None, dict]:
    """The document's text through Waive's own extractors, saved under SOURCES with an index row.
    Returns the text (None when unreachable or unreadable) and the index row."""
    row = {
        "id": source_id,
        "url": url,
        "recorded_sha256": recorded_sha256,
        "fetched_on": datetime.now(UTC).date().isoformat(),
    }
    try:
        response = client().get(
            url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,*/*"}
        )
        row["http"] = response.status_code
        if response.status_code != 200:
            row["error"] = f"HTTP {response.status_code}"
            return None, row
        body = response.content
        row["fetched_sha256"] = hashlib.sha256(body).hexdigest()
        row["sha256_match"] = row["fetched_sha256"] == recorded_sha256
        row["bytes"] = len(body)
        content_type = response.headers.get("content-type", "").lower()
        if "pdf" in content_type or body[:5] == b"%PDF-":
            text = pdf_text(body)
            row["kind"] = "pdf"
        else:
            text = html_text(
                body.decode(response.encoding or "utf-8", errors="replace"), str(response.url)
            )
            row["kind"] = "html"
        if not text or len(text) < 200:
            row["error"] = "no usable text"
            return None, row
        row["chars"] = len(text)
        (SOURCES / f"{source_id}.txt").write_text(text, encoding="utf-8")
        return text, row
    except Exception as error:  # noqa: BLE001 - one bad source must not stop the pass
        row["error"] = f"{type(error).__name__}: {error}"[:200]
        return None, row
    finally:
        with _index_lock, (SOURCES / "_index.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")


def strip_rejected(sheet: ProcedureSheet, rejected: list[tuple[str, str]]) -> ProcedureSheet:
    updates: dict[str, dict] = {}
    for path, _ in rejected:
        section, field = path.split(".")
        updates.setdefault(section, {})[field] = None
    data = sheet.model_dump()
    for section, fields in updates.items():
        data[section].update(fields)
    return ProcedureSheet.model_validate(data)


def process(raw: dict) -> dict:
    notes = raw.pop("eve_review", [])
    sheet = ProcedureSheet.model_validate(raw)
    texts: dict[str, str] = {}
    missing: list[str] = []
    hash_matches: dict[str, bool | None] = {}
    for src in sheet.sources:
        text, row = fetch_one(src.id, src.url, src.sha256) if src.url else (None, {})
        hash_matches[src.id] = row.get("sha256_match")
        if text:
            texts[src.id] = text
        else:
            missing.append(src.url or src.id)
    grounded, pruned = _ground(sheet, texts)
    report = verify_sheet(grounded, texts)
    final = strip_rejected(grounded, report.rejected)
    income = (
        final.eligibility.free_care_max_fpl is not None
        or final.eligibility.discount_tiers is not None
    )
    return {
        **final.model_dump(mode="json"),
        "eve_review": notes,
        "eve_verified": {
            "on": datetime.now(UTC).date().isoformat(),
            "sources_fetched": len(texts),
            "sources_total": len(sheet.sources),
            "sources_missing": missing,
            "sha256_match": hash_matches,
            "sources_dir": str(SOURCES),
            "accepted": report.accepted,
            "rejected": report.rejected,
            "pruned": pruned,
            "income_rule_verified": income,
            "core_completeness": round(final.completeness(), 2),
        },
    }


def main() -> None:
    files = sorted(p for p in SRC.glob("*.json") if not p.name.startswith("_"))
    totals = Counter()
    reasons = Counter()
    for file in files:
        payload = json.loads(file.read_text())
        with ThreadPoolExecutor(WORKERS) as pool:
            sheets = list(pool.map(process, payload["sheets"]))
        for s in sheets:
            v = s["eve_verified"]
            totals["sheets"] += 1
            totals["income_rule_verified"] += v["income_rule_verified"]
            totals["all_sources_fetched"] += v["sources_fetched"] == v["sources_total"]
            totals["no_source_fetched"] += v["sources_fetched"] == 0
            totals["sources_hash_match"] += sum(1 for m in v["sha256_match"].values() if m)
            totals["sources_hash_changed"] += sum(
                1 for m in v["sha256_match"].values() if m is False
            )
            totals["rejected_fields"] += len(v["rejected"])
            totals["accepted_fields"] += len(v["accepted"])
            for _, r in v["rejected"]:
                reasons[r] += 1
        out = {
            **{k: v for k, v in payload.items() if k != "sheets"},
            "verified_on": datetime.now(UTC).date().isoformat(),
            "sheets": sheets,
        }
        (OUT / file.name).write_text(json.dumps(out, indent=1))
        print(
            f"[verified] {file.stem.upper()}: {len(sheets)} sheets, income rule {sum(s['eve_verified']['income_rule_verified'] for s in sheets)}",
            flush=True,
        )
    lines = [
        "# Refetch and verify",
        "",
        f"{totals['sheets']} sheets. Sources refetched with Waive's downloader and the full verifier run.",
        "",
        "| Metric | Count |",
        "|---|---|",
    ]
    for k in (
        "income_rule_verified",
        "all_sources_fetched",
        "no_source_fetched",
        "sources_hash_match",
        "sources_hash_changed",
        "accepted_fields",
        "rejected_fields",
    ):
        lines.append(f"| {k} | {totals[k]} |")
    lines += ["", "## Rejection reasons", "", "| Reason | Fields |", "|---|---|"]
    lines += [f"| {r} | {n} |" for r, n in reasons.most_common(20)]
    (OUT / "_refetch-report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:12]))
    print(f"[done] {OUT / '_refetch-report.md'}")


if __name__ == "__main__":
    main()
