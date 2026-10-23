"""Build the human review queue from the verified Eve sheets: one row per sheet, ranked so the
reviewer sees the income rule, its quote and its source first.

    uv run python scripts/review_queue.py data/atlas-eve-verified data/atlas-eve-verified/_review-queue.csv
"""

import csv
import json
import sys
from pathlib import Path

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/atlas-eve-verified")
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else SRC / "_review-queue.csv"


def cited(section: dict, key: str):
    f = (section or {}).get(key)
    return f if isinstance(f, dict) else None


rows = []
for file in sorted(p for p in SRC.glob("*.json") if not p.name.startswith("_")):
    payload = json.loads(file.read_text())
    for s in payload["sheets"]:
        v = s.get("eve_verified", {})
        e = s.get("eligibility", {})
        free = cited(e, "free_care_max_fpl")
        tiers = cited(e, "discount_tiers")
        src_by_id = {d["id"]: d for d in s.get("sources", [])}
        income_field = free or tiers
        src = src_by_id.get((income_field or {}).get("source_id", ""), {})
        tier_text = (
            "; ".join(
                f"{t['min_fpl_exclusive']}-{t['max_fpl_inclusive']}% {t['discount_percent']}%"
                for t in (tiers or {}).get("value", [])
            )
            if tiers
            else ""
        )
        flags = []
        if not v.get("income_rule_verified"):
            flags.append("no income rule")
        if v.get("sources_fetched", 0) < v.get("sources_total", 0):
            flags.append(
                f"{v.get('sources_total', 0) - v.get('sources_fetched', 0)} source(s) unreachable"
            )
        if v.get("rejected"):
            flags.append(f"{len(v['rejected'])} field(s) rejected")
        if (v.get("core_completeness") or 0) < 0.5:
            flags.append("core completeness under 50%")
        if s.get("eve_review"):
            flags.append(f"{len(s['eve_review'])} note(s)")
        priority = (
            0
            if v.get("income_rule_verified") and not v.get("rejected")
            else 1
            if v.get("income_rule_verified")
            else 2
        )
        rows.append(
            {
                "priority": priority,
                "state": s["hospital"]["state"],
                "ccn": s["hospital"]["ccn"],
                "hospital": s["hospital"]["name"],
                "free_care_max_fpl": (free or {}).get("value", ""),
                "discount_tiers": tier_text,
                "income_quote": (income_field or {}).get("quote", ""),
                "income_source_url": src.get("url", ""),
                "core_completeness": v.get("core_completeness", ""),
                "flags": "; ".join(flags),
                "rejected": " | ".join(f"{p}: {r}" for p, r in v.get("rejected", [])),
                "review_notes": " | ".join(
                    f"{n.get('field', '')}: {n.get('note', '')}" if isinstance(n, dict) else str(n)
                    for n in s.get("eve_review", [])
                )[:2000],
            }
        )

rows.sort(key=lambda r: (r["priority"], r["state"], r["hospital"]))
with OUT.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
counts = {p: sum(1 for r in rows if r["priority"] == p) for p in (0, 1, 2)}
print(
    f"[queue] {len(rows)} rows -> {OUT}; priority 0 (verified, nothing rejected): {counts[0]}, 1 (verified with rejections): {counts[1]}, 2 (no income rule): {counts[2]}"
)
