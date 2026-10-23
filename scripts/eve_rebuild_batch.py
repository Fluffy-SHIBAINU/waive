"""Task 7.12c batch runner: rebuild Eve hospitals from stored documents, with no Tavily.

Usage (from the repository root; caps are the Mac ledger's totals, see `docs/PROGRESS.md` 7.12c):

    python3 scripts/eve_rebuild_batch.py --list
    python3 scripts/eve_rebuild_batch.py --after 170086 --limit 50 \
        --tavily-cap 5297 --tf-cap 21.935470270 --out var/eve-rebuild.jsonl


Runs `uv run waive atlas build --state XX --ccn NNNNNN --reuse-sources` one hospital at a time,
with the Tavily cap pinned at the credits already used (any search is refused before it happens)
and the Token Factory cap at the block's budget. Order: _rebuild.json sorted by (state, ccn),
minus the skip list and the hospitals with no stored text. Resumes after --after CCN.
Writes one JSON line per hospital to --out and prints a summary.
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
DB = f"{REPO}/var/waive.db"


def ro():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def latest(ccn):
    with ro() as con:
        row = con.execute(
            "select version, status from sheets where ccn=? order by version desc limit 1", (ccn,)
        ).fetchone()
    return list(row) if row else None


def has_text(ccn):
    with ro() as con:
        n = con.execute(
            "select count(*) from hospital_sources hs join source_docs sd on sd.id=hs.source_id "
            "where hs.ccn=? and length(sd.text)>0",
            (ccn,),
        ).fetchone()[0]
    return n > 0


def confidence(ccn):
    with ro() as con:
        row = con.execute("select domain_confidence from hospitals where ccn=?", (ccn,)).fetchone()
    return (row[0] if row else None) or 0.0


def order():
    """Sure websites (0.9) first, so a batch's sheets can publish; unconfirmed (0.6) ones are held
    for a website check whatever the rebuild finds. Within each, by state and CCN."""
    rebuild = json.load(open(f"{REPO}/data/atlas-eve/_rebuild.json"))
    skip = {r["ccn"] for r in json.load(open(f"{REPO}/data/atlas-eve/_skip.json"))}
    rows = sorted(
        (r for r in rebuild if r["ccn"] not in skip),
        key=lambda r: (-confidence(r["ccn"]), r["state"], r["ccn"]),
    )
    return [r for r in rows if has_text(r["ccn"])], [r for r in rows if not has_text(r["ccn"])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--after", default="", help="resume after this CCN in the order")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--tavily-cap", default="")
    ap.add_argument("--tf-cap", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--list", action="store_true", help="print the order and exit")
    a = ap.parse_args()
    eligible, no_text = order()
    if a.list:
        print(f"eligible {len(eligible)}; without stored text {len(no_text)}")
        print("no text:", " ".join(r["ccn"] for r in no_text))
        return
    if not (a.tavily_cap and a.tf_cap and a.out):
        sys.exit("--tavily-cap, --tf-cap and --out are required for a run")
    start = 0
    if a.after:
        ccns = [r["ccn"] for r in eligible]
        start = ccns.index(a.after) + 1
    batch = eligible[start : start + a.limit]
    env = {
        **os.environ,
        "WAIVE_TAVILY_CREDIT_CAP": a.tavily_cap,
        "WAIVE_TOKEN_FACTORY_USD_CAP": a.tf_cap,
        "WAIVE_SCHEDULER": "off",
    }
    counts = {}
    last = None
    with open(a.out, "a", encoding="utf-8") as out:
        for i, r in enumerate(batch, 1):
            ccn, st = r["ccn"], r["state"]
            before = latest(ccn)
            t0 = time.time()
            try:
                p = subprocess.run(
                    [
                        "uv",
                        "run",
                        "waive",
                        "atlas",
                        "build",
                        "--state",
                        st,
                        "--ccn",
                        ccn,
                        "--reuse-sources",
                    ],
                    cwd=REPO,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=900,
                )
                text, rc = p.stdout + p.stderr, p.returncode
            except subprocess.TimeoutExpired as e:
                text, rc = f"timeout {e}", -9
            after = latest(ccn)
            flat = " ".join(text.split())
            m = re.search(rf"{ccn}\s*│\s*[^│]*│\s*(\w+)\s*│", text)
            outcome = m.group(1) if m else ("failed" if rc else "unknown")
            if after and (before is None or after[0] > before[0]):
                # The database is the truth: a new version carries the build's verdict.
                outcome = after[1]
            if rc and "BudgetExceeded" in text:
                outcome = "refused-budget"
            spend = re.search(
                r"Spend so far: ([\d.]+) Tavily credits, \$([\d.]+) Token Factory", flat
            )
            rec = {
                "ccn": ccn,
                "state": st,
                "before": before,
                "after": after,
                "outcome": outcome,
                "rc": rc,
                "secs": round(time.time() - t0, 1),
                "spend": spend.groups() if spend else None,
                "tail": flat[-600:],
            }
            out.write(json.dumps(rec) + "\n")
            out.flush()
            counts[outcome] = counts.get(outcome, 0) + 1
            last = ccn
            print(
                f"[{i}/{len(batch)}] {st} {ccn} {outcome} v{before}->{after} {rec['secs']}s spend={rec['spend']}",
                flush=True,
            )
            if outcome == "refused-budget" and "Token Factory cap" in flat:
                print("STOP: Token Factory block budget reached", flush=True)
                break
    print("SUMMARY", json.dumps(counts), "last", last, flush=True)


if __name__ == "__main__":
    sys.exit(main())
