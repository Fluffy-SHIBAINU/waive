"""The Devpost material must be complete, honest and short enough (Phase 8.6)."""

import re
from pathlib import Path

DEVPOST = Path(__file__).resolve().parents[2] / "docs" / "devpost"
SHOT_ROW = re.compile(r"^\| \d+ \| (\d):(\d\d)–(\d):(\d\d) \| (\d+) \|")


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
    # Task 8.6 adds the checklist.md gate assertions (U8.1–U8.4) here.


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
