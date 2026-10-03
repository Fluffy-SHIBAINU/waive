"""The Devpost material must be complete, honest and short enough (Phase 8.6)."""

import re
from pathlib import Path

DEVPOST = Path(__file__).resolve().parents[2] / "docs" / "devpost"
SHOT_ROW = re.compile(r"^\| \d+ \| (\d):(\d\d)–(\d):(\d\d) \| (\d+) \|")


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
    assert f"{clock // 60}:{clock % 60:02d}" in text  # the stated total matches the rows
