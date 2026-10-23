"""Run Waive's own quote-level verifier and consistency rules over the converted Eve drafts.

Source text is not stored, so each source document is stood in by the quotes that cite it: the
substring check passes by construction (the agent already verified quotes against the fetched
text) and every other rule in verify_sheet runs as it would at publish time.
"""

import glob
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from waive.atlas.publish import sheet_inconsistencies
from waive.atlas.schema import ProcedureSheet
from waive.atlas.verify import verify_sheet

EVE_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/atlas-eve")
DRAFT_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.home() / "waive-atlas/atlas-drafts"

generation = {}
for f in glob.glob(str(DRAFT_DIR / "[A-Z][A-Z]" / "[0-9]*.json")):
    d = json.load(open(f))
    generation[d["hospital"]["ccn"]] = (d.get("models") or {}).get("structuring", "opus-era")

field_acc = Counter()
field_rej = Counter()
reasons = defaultdict(Counter)
per_gen = defaultdict(lambda: Counter())
per_state = defaultdict(lambda: Counter())
inconsistent = 0
rows = []
for f in sorted(p for p in glob.glob(str(EVE_DIR / "*.json")) if not Path(p).name.startswith("_")):
    payload = json.load(open(f))
    for raw in payload["sheets"]:
        notes = raw.pop("eve_review", [])
        sheet = ProcedureSheet.model_validate(raw)
        docs = defaultdict(list)
        for _, cited in sheet.field_paths():
            if cited.source_id and cited.quote:
                docs[cited.source_id].append(cited.quote)
        report = verify_sheet(sheet, {k: "\n".join(v) for k, v in docs.items()})
        problems = sheet_inconsistencies(sheet)
        gen = generation.get(sheet.hospital.ccn, "opus-era")
        st = sheet.hospital.state
        rejected = {p: r for p, r in report.rejected}
        for p in report.accepted:
            field_acc[p] += 1
        for p, r in report.rejected:
            field_rej[p] += 1
            reasons[p][r] += 1
        free_ok = "eligibility.free_care_max_fpl" in report.accepted
        tiers_ok = "eligibility.discount_tiers" in report.accepted
        income_ok = free_ok or tiers_ok
        core = sheet.completeness()
        ready = income_ok and not problems and core >= 0.5
        if problems:
            inconsistent += 1
        per_gen[gen]["sheets"] += 1
        per_gen[gen]["income_rule_verified"] += income_ok
        per_gen[gen]["free_care_verified"] += free_ok
        per_gen[gen]["tiers_verified"] += tiers_ok
        per_gen[gen]["inconsistent"] += bool(problems)
        per_gen[gen]["review_ready"] += ready
        per_gen[gen]["rejected_fields"] += len(report.rejected)
        per_state[st]["sheets"] += 1
        per_state[st]["review_ready"] += ready
        per_state[st]["income_rule_verified"] += income_ok
        rows.append(
            {
                "ccn": sheet.hospital.ccn,
                "state": st,
                "name": sheet.hospital.name,
                "generation": gen,
                "completeness": round(core, 2),
                "accepted": len(report.accepted),
                "rejected": rejected,
                "inconsistencies": problems,
                "income_rule_verified": income_ok,
                "review_ready": ready,
                "judge_notes": len(notes),
            }
        )

total = len(rows)
ready = sum(r["review_ready"] for r in rows)
income = sum(r["income_rule_verified"] for r in rows)
out = EVE_DIR / "_waive-verify-review.json"
out.write_text(json.dumps(rows, indent=1))
lines = [
    "# Waive verifier over Eve drafts",
    "",
    f"{total} sheets. Each source document is stood in by its own quotes, so every rule except substring-in-document runs as at publish time.",
    "",
    "| Metric | Count | Share |",
    "|---|---|---|",
    f"| Income rule (free-care line or tiers) survives Waive's verifier | {income} | {income * 100 // total}% |",
    f"| Review-ready: income rule verified, no inconsistency, core completeness at least 50% | {ready} | {ready * 100 // total}% |",
    f"| Sheets with a publish-time inconsistency | {inconsistent} | {inconsistent * 100 // total}% |",
    "",
    "## By generation",
    "",
    "| Generation | Sheets | Income rule verified | Free care verified | Tiers verified | Inconsistent | Review-ready | Rejected fields |",
    "|---|---|---|---|---|---|---|---|",
]
for g, c in sorted(per_gen.items()):
    lines.append(
        f"| {g} | {c['sheets']} | {c['income_rule_verified']} | {c['free_care_verified']} | {c['tiers_verified']} | {c['inconsistent']} | {c['review_ready']} | {c['rejected_fields']} |"
    )
lines += [
    "",
    "## By field",
    "",
    "| Field | Accepted | Rejected | Top rejection reasons |",
    "|---|---|---|---|",
]
for p in sorted(set(field_acc) | set(field_rej), key=lambda p: -(field_acc[p] + field_rej[p])):
    top = "; ".join(f"{r} ({n})" for r, n in reasons[p].most_common(3))
    lines.append(f"| {p} | {field_acc[p]} | {field_rej[p]} | {top} |")
lines += [
    "",
    "## By state",
    "",
    "| State | Sheets | Income rule verified | Review-ready |",
    "|---|---|---|---|",
]
for st, c in sorted(per_state.items()):
    lines.append(f"| {st} | {c['sheets']} | {c['income_rule_verified']} | {c['review_ready']} |")
(EVE_DIR / "_waive-verify-review.md").write_text("\n".join(lines) + "\n")
print("\n".join(lines[:22]))
print("...")
print(f"[review] wrote {out} and {EVE_DIR / '_waive-verify-review.md'}")
