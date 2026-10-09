"""The Devpost material must be complete, honest and short enough (Phase 8.6)."""

import re
from pathlib import Path

DEVPOST = Path(__file__).resolve().parents[2] / "docs" / "devpost"
REPORTS = DEVPOST.parent / "reports"
README = DEVPOST.parents[1] / "README.md"
SHOT_ROW = re.compile(r"^\| \d+ \| (\d):(\d\d)–(\d):(\d\d) \| (\d+) \|")
TAVILY_SCAN = re.compile(r'git grep -lE "(tvly-[^"]+)"')
ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
TENS = {
    2: "twenty",
    3: "thirty",
    4: "forty",
    5: "fifty",
    6: "sixty",
    7: "seventy",
    8: "eighty",
    9: "ninety",
}


def in_words(n: int) -> str:
    """0–99 as the narration spells them ("twenty-nine", "forty-six")."""
    if n < 20:
        return ONES[n]
    tens, ones = divmod(n, 10)
    return TENS[tens] + (f"-{ONES[ones]}" if ones else "")


def report_figure(name: str, label: str) -> str:
    """The value of a `Label: value` line at the top of a committed coverage report."""
    text = (REPORTS / name).read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(label)}: (.+)$", text, flags=re.MULTILINE)
    assert match, f"{name} has no '{label}' line"
    return match.group(1).strip()


def count_and_percent(figure: str) -> tuple[str, str]:
    match = re.fullmatch(r"(\d+) \((\d+)%\)", figure)
    assert match, figure
    return match.group(1), match.group(2)


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


def test_public_figures_match_the_committed_reports():
    # The README, the submission and the demo narration quote coverage figures that
    # `waive atlas report` writes to docs/reports/; a figures refresh must carry all three along
    # (8.9 review: the README lagged the national report by one review item, the narration by
    # two published hospitals).
    readme = README.read_text(encoding="utf-8")
    submission = (DEVPOST / "submission.md").read_text(encoding="utf-8")
    script = (DEVPOST / "demo-script.md").read_text(encoding="utf-8")

    ma_registry = report_figure("atlas-ma.md", "Hospitals in registry")
    ma_count, ma_percent = count_and_percent(report_figure("atlas-ma.md", "Published sheets"))
    assert f"**{ma_count} published sheets ({ma_percent} %)**" in readme
    assert f"{ma_count} published sheets ({ma_percent} %)" in submission
    narration = (
        f"{in_words(int(ma_count)).capitalize()} of {in_words(int(ma_registry))} "
        "Massachusetts hospitals are published"
    )
    assert narration in script
    assert f"({ma_count} of {ma_registry};" in script  # the honesty note's own figure

    hospitals = int(report_figure("atlas-national.md", "Hospitals in registry"))
    nat_count, nat_percent = count_and_percent(
        report_figure("atlas-national.md", "Published sheets")
    )
    core = report_figure("atlas-national.md", "Core fields documented (published sheets)")
    open_items = report_figure("atlas-national.md", "Open review items")
    assert f"**{hospitals:,}** nonprofit hospitals" in readme
    assert f"{nat_count} published nationally ({nat_percent} %)" in readme
    assert f"core fields documented on {core.rstrip('%')} % of published sheets" in readme
    assert f"{open_items} open review items" in readme
    assert f"{hospitals:,} hospitals seeded nationally" in submission
    assert f"{nat_count} published so far" in submission


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
