"""The Devpost material must be complete, honest and short enough (Phase 8.6)."""

import re
from pathlib import Path

DEVPOST = Path(__file__).resolve().parents[2] / "docs" / "devpost"
SHOT_ROW = re.compile(r"^\| \d+ \| (\d):(\d\d)–(\d):(\d\d) \| (\d+) \|")
TAVILY_SCAN = re.compile(r'git grep -lE "(tvly-[^"]+)"')


def test_submission_has_every_required_section():
    text = (DEVPOST / "submission.md").read_text(encoding="utf-8")
    for heading in (
        "## Inspiration",
        "## What it does",
        "## How we built it",
        "## Challenges we ran into",
        "## Accomplishments that we're proud of",
        "## What we learned",
        "## What's next for Waive",
        "## Built with",
        "## Feedback on Nebius and NVIDIA tools",
        "## Best Use of Tavily",
    ):
        assert heading in text, heading
    assert "not an NVIDIA model" in text  # the vision model, stated plainly
    checklist = (DEVPOST / "checklist.md").read_text(encoding="utf-8")
    for gate in ("U8.1", "U8.2", "U8.3", "U8.4"):
        assert gate in checklist


def test_devpost_documents_have_no_tbd():
    paths = sorted(DEVPOST.glob("*.md"))
    assert paths, "no Devpost documents found; the glob must not pass vacuously"
    names = {path.name for path in paths}
    assert {"submission.md", "demo-script.md"} <= names
    for path in paths:
        assert "TBD" not in path.read_text(encoding="utf-8"), path.name


def test_submission_names_the_open_zdr_gate():
    # While U0.4 is open nobody has confirmed zero data retention or checked the Token
    # Factory console, so the submission must point at the gate rather than assert a result.
    progress = (DEVPOST.parent / "PROGRESS.md").read_text(encoding="utf-8")
    text = (DEVPOST / "submission.md").read_text(encoding="utf-8")
    if "- [ ] **U0.4**" in progress:
        assert "gate U0.4" in text


def test_submission_says_zero_data_retention_is_confirmed_once_the_gate_closed():
    # Once U0.4 is ticked the project owner has confirmed zero data retention for the Token
    # Factory project (2026-10-09), so the submission must say so, with the date, and must drop
    # the open-gate caveats; the refusal while `WAIVE_ZDR_CONFIRMED` is false stays described.
    progress = (DEVPOST.parent / "PROGRESS.md").read_text(encoding="utf-8")
    text = (DEVPOST / "submission.md").read_text(encoding="utf-8")
    if "- [x] **U0.4**" in progress:
        assert "runs with zero data retention, confirmed 2026-10-09 by the project owner" in text
        assert "real bills are now accepted" in text
        assert "WAIVE_ZDR_CONFIRMED" in text
        assert "until zero data retention is confirmed" not in text
        assert "still open (gate U0.4)" not in text


def test_demo_script_shot_list_is_contiguous_and_under_three_minutes():
    text = (DEVPOST / "demo-script.md").read_text(encoding="utf-8")
    rows = [m for m in (SHOT_ROW.match(line) for line in text.splitlines()) if m]
    assert len(rows) >= 8
    clock = 0
    for row in rows:
        start = int(row.group(1)) * 60 + int(row.group(2))
        end = int(row.group(3)) * 60 + int(row.group(4))
        assert start == clock, f"shot starting at {start}s does not follow the previous one"
        assert end - start == int(row.group(5)), "the Seconds column disagrees with the times"
        clock = end
    assert clock <= 175, f"the video runs {clock}s; the limit is 180 with a margin"
    assert f"Total: **{clock // 60}:{clock % 60:02d}**" in text  # the stated total matches the rows


def test_checklist_tavily_key_scan_catches_prefixed_keys_and_passes_itself():
    # Tavily issues plain and prefixed keys (`tvly-…`, `tvly-dev-…`, `tvly-prod-…`); the gate
    # U8.1 scan must catch every shape and must not match the checklist's own text. Synthetic
    # shapes only: no string here is a key, and none of them matches the scan as source text.
    checklist = (DEVPOST / "checklist.md").read_text(encoding="utf-8")
    match = TAVILY_SCAN.search(checklist)
    assert match, "the checklist must carry the tvly- key scan"
    scan = re.compile(match.group(1))
    for shape in ("tvly-" + "a" * 32, "tvly-dev-" + "b" * 32, "tvly-prod-" + "C9_-" * 8):
        assert scan.search(shape), shape
    assert not scan.search(checklist), "the checklist must pass its own scan"
