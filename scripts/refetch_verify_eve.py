"""Refetch every source of the converted Eve drafts with Waive's own downloader and run the real
verifier: trim quotes to the spans the documents contain, prune list fields to the items their
quotes name, reject what fails, and write the grounded sheets.

    uv run python scripts/refetch_verify_eve.py data/atlas-eve data/atlas-eve-verified
"""

import json
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import httpx

from waive.atlas.fetch import download_text
from waive.atlas.pipeline import _ground
from waive.atlas.schema import ProcedureSheet
from waive.atlas.verify import verify_sheet

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/atlas-eve")
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("data/atlas-eve-verified")
WORKERS = int(sys.argv[3]) if len(sys.argv) > 3 else 16
OUT.mkdir(parents=True, exist_ok=True)

_local = threading.local()


def client() -> httpx.Client:
    if not hasattr(_local, "http"):
        _local.http = httpx.Client(follow_redirects=False, timeout=15.0)
    return _local.http


def fetch_one(url: str) -> str | None:
    return download_text(
        url, client(), allowed=lambda _host: True, allow_html=True, timeout=15.0, total_timeout=60.0
    )


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
    for src in sheet.sources:
        text = fetch_one(src.url) if src.url else None
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
