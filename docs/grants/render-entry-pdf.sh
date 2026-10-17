#!/usr/bin/env bash
# Render the AHRQ entry (Part B of the grants file) to PDF and check it against AHRQ's format rules:
# at most 15 single-spaced pages, Arial 11 pt or larger, margins of 1 inch or more.
# Usage: bash docs/grants/render-entry-pdf.sh [output.pdf]     (needs Google Chrome and uv)
set -euo pipefail

cd "$(dirname "$0")/../.."
SRC="docs/grants/2026-10-ahrq-choice-competition-entry.md"
OUT="${1:-${TMPDIR:-/tmp}/ahrq-entry.pdf}"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
BASE="$PWD/docs/grants"
WORK="$(mktemp -d)"

sed -n '/<!-- ENTRY START -->/,/<!-- ENTRY END -->/p' "$SRC" > "$WORK/entry.md"

uv run --no-project --with markdown python -I - "$WORK" "$BASE" <<'PY'
import pathlib
import sys

import markdown

work, base = pathlib.Path(sys.argv[1]), sys.argv[2]
body = markdown.markdown((work / "entry.md").read_text(encoding="utf-8"), extensions=["tables", "sane_lists"])
css = """
@page { size: Letter; margin: 1in; }
html { font-family: Arial, Helvetica, sans-serif; font-size: 11pt; }
body { margin: 0; line-height: 1.15; color: #000; }
h1 { font-size: 16pt; margin: 0 0 4pt; }
h2 { font-size: 12pt; margin: 9pt 0 3pt; }
p { margin: 0 0 5pt; }
ul, ol { margin: 0 0 5pt; padding-left: 18pt; }
li { margin: 0 0 2pt; }
code { font-family: Arial, Helvetica, sans-serif; font-size: 11pt; }
table { border-collapse: collapse; width: 100%; font-size: 11pt; margin: 4pt 0 6pt; }
th, td { border: 0.5pt solid #555; padding: 2pt 4pt; vertical-align: top; text-align: left; }
th { background: #eee; }
img { width: 100%; display: block; margin: 4pt 0 2pt; page-break-inside: avoid; }
a { color: #000; text-decoration: none; word-break: break-all; }
tr { page-break-inside: avoid; }
"""
html = (
    f'<!doctype html><html><head><meta charset="utf-8"><base href="file://{base}/">'
    f"<style>{css}</style></head><body>{body}</body></html>"
)
(work / "entry.html").write_text(html, encoding="utf-8")
PY

"$CHROME" --headless=new --disable-gpu --no-pdf-header-footer --virtual-time-budget=8000 \
  --print-to-pdf="$OUT" "file://$WORK/entry.html" > /dev/null 2>&1

uv run --no-project --with pypdf python -I - "$OUT" "$WORK/entry.md" <<'PY'
import re
import sys

from pypdf import PdfReader

reader = PdfReader(sys.argv[1])
fonts = set()
for page in reader.pages:
    for ref in ((page.get("/Resources") or {}).get("/Font") or {}).values():
        fonts.add(str(ref.get_object().get("/BaseFont")).split("+")[-1])
left = len(re.findall(r"\[USER (?:FILLS|CONFIRMS)", open(sys.argv[2], encoding="utf-8").read()))
print(f"pages: {len(reader.pages)} (AHRQ limit: 15)")
print("fonts:", ", ".join(sorted(fonts)))
print(f"placeholders left: {left}")
print("written:", sys.argv[1])
sys.exit(1 if len(reader.pages) > 15 else 0)
PY
