"""Dry recheck of the verifier's rules on a copy of the atlas: what `waive atlas recheck` would
list and what `waive atlas recheck --withdraw` would drop and hold, published sheets only unless
--all is given. The atlas is opened read-only and copied with SQLite's backup API into a
temporary file; WAIVE_DATABASE_URL points at the copy while the real recheck and withdraw run
there, and the copy is deleted at the end. No model call, no network.

One line per finding, by state and CCN: `ST CCN path: reason` for each field the rules reject
(`ST CCN sheet: problem` for a sheet-level one), `ST CCN held` when the withdraw would hold the
sheet, and `ST CCN kept as the owner ruled: ...` for each reading an owner's ruling is kept over;
then a summary line. With --compare-to REF the same runs on a second copy with the code of that
git ref (its `src` exported into a temporary folder, run in a subprocess), and only the lines
that differ are printed, `+` for this code and `-` for REF: `--compare-to main` lists what a
branch's rules change.

    uv run python scripts/audit_rules_check.py --db var/waive.db --compare-to main > var/audit-rules-check.txt
"""

import argparse
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB = Path("var/waive.db")
ROOT = Path(__file__).resolve().parents[1]
KEPT = "kept as the owner ruled"
# How withdraw_rejected notes a reading the owner's ruling was kept over (pipeline
# _record_ruling_conflicts): `<path>: the owner's ruling (70%) is kept over "<reason>"; ...`.
KEPT_NOTE = " is kept over "

Line = tuple[str, str, str]  # state, CCN, what the rules do to the sheet


@dataclass
class Check:
    lines: list[Line]
    fields: int
    held: int
    kept: int

    @property
    def sheets(self) -> int:
        return len({(state, ccn) for state, ccn, _ in self.lines})


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def summary(check: Check) -> str:
    return (
        f"{_count(check.sheets, 'sheet')} listed, {_count(check.fields, 'field')} dropped, "
        f"{_count(check.held, 'sheet')} held, {check.kept} {KEPT}"
    )


def render(check: Check) -> list[str]:
    return [f"{state} {ccn} {text}" for state, ccn, text in check.lines] + [summary(check)]


def copy_atlas(source: Path, target: Path) -> None:
    """`source` into `target` with SQLite's backup API, from a read-only connection: a writer may
    be at work on the atlas, and the backup reads one consistent state of it (its WAL included)."""
    reader = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    try:
        writer = sqlite3.connect(target)
        try:
            reader.backup(writer)
        finally:
            writer.close()
    finally:
        reader.close()


@contextmanager
def _database_url(url: str) -> Iterator[None]:
    """WAIVE_DATABASE_URL set to `url` for the run, as `waive atlas recheck` would read it, and
    put back afterwards."""
    previous = os.environ.get("WAIVE_DATABASE_URL")
    os.environ["WAIVE_DATABASE_URL"] = url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("WAIVE_DATABASE_URL", None)
        else:
            os.environ["WAIVE_DATABASE_URL"] = previous


def check_copy(copy: Path, include_all: bool) -> Check:
    """recheck_sheets, then withdraw_rejected, on the copy at `copy` (the withdraw writes new
    versions into it). Imported here so that a subprocess run with another ref's `src` on
    PYTHONPATH runs that code."""
    from waive.atlas import repo
    from waive.atlas.pipeline import recheck_sheets, withdraw_rejected
    from waive.atlas.schema import SheetStatus
    from waive.config import Settings
    from waive.db import init_db, make_engine, session_scope

    with _database_url(f"sqlite:///{copy}"):
        engine = make_engine(Settings().database_url)
        try:
            init_db(engine)
            found: dict[tuple[str, str], list[str]] = {}
            with session_scope(engine) as session:
                for entry in recheck_sheets(session):
                    if include_all or entry.status is SheetStatus.PUBLISHED:
                        found.setdefault((entry.state, entry.ccn), []).extend(
                            f"{path}: {reason}" for path, reason in entry.rejected
                        )
            with session_scope(engine) as session:
                before = {}
                for row in repo.list_hospitals(session):
                    latest = repo.latest_sheet(session, row.ccn)
                    if latest and (include_all or latest[0].status is SheetStatus.PUBLISHED):
                        paths = {path for path, _ in latest[0].field_paths()}
                        before[row.ccn] = (row.state, latest[0].status, paths)
                results = withdraw_rejected(session, published_only=not include_all)
                fields = held = kept = 0
                for result in results:
                    state, status, paths = before[result.ccn]
                    lines = found.setdefault((state, result.ccn), [])
                    if result.outcome in ("published", "held"):
                        after, _ = repo.latest_sheet(session, result.ccn)
                        fields += len(paths - {path for path, _ in after.field_paths()})
                    if result.outcome == "held" and status is not SheetStatus.HELD:
                        lines.append("held")
                        held += 1
                    conflicts = [note for note in result.notes if KEPT_NOTE in note]
                    lines.extend(f"{KEPT}: {note}" for note in conflicts)
                    kept += bool(conflicts)
        finally:
            engine.dispose()
    ordered = [
        (state, ccn, text) for (state, ccn), texts in sorted(found.items()) for text in texts
    ]
    return Check(ordered, fields, held, kept)


def check_ref(ref: str, source: Path, include_all: bool, scratch: Path) -> Check:
    """The same check with the code of git ref `ref`: its `src` exported into `scratch` and this
    script run on it in a subprocess, which makes its own copy of the atlas."""
    archive = subprocess.run(
        ["git", "archive", "--format=tar", ref, "src"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    code = scratch / "code"
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(code, filter="data")
    out = scratch / "ref.json"
    command = [sys.executable, str(Path(__file__).resolve()), "--db", str(source), "--json"]
    command += ["--out", str(out)] + (["--all"] if include_all else [])
    paths = [str(code / "src"), *filter(None, [os.environ.get("PYTHONPATH")])]
    subprocess.run(command, env={**os.environ, "PYTHONPATH": os.pathsep.join(paths)}, check=True)
    data = json.loads(out.read_text())
    if not Path(data["code"]).resolve().is_relative_to((code / "src").resolve()):
        raise RuntimeError(f"the run for {ref} imported waive from {data['code']}")
    lines = [(state, ccn, text) for state, ccn, text in data["lines"]]
    return Check(lines, data["fields"], data["held"], data["kept"])


def differences(ours: Check, theirs: Check, ref: str) -> list[str]:
    """The lines only one check has, by state and CCN, REF's (`-`) before this code's (`+`) for
    each sheet; then both summaries and a count."""
    only_ours = list((Counter(ours.lines) - Counter(theirs.lines)).elements())
    only_theirs = list((Counter(theirs.lines) - Counter(ours.lines)).elements())
    marked = [("-", line) for line in only_theirs] + [("+", line) for line in only_ours]
    marked.sort(key=lambda item: (item[1][0], item[1][1], item[0] == "+"))
    sheets = {(state, ccn) for _, (state, ccn, _) in marked}
    return [f"{sign} {state} {ccn} {text}" for sign, (state, ccn, text) in marked] + [
        f"this code: {summary(ours)}",
        f"{ref}: {summary(theirs)}",
        f"{_count(len(marked), 'line')} differ on {_count(len(sheets), 'sheet')} "
        f"({len(only_ours)} only with this code, {len(only_theirs)} only with {ref})",
    ]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="the atlas (read-only)")
    parser.add_argument("--all", action="store_true", help="every latest sheet, not only published")
    parser.add_argument("--out", type=Path, help="write the list here instead of stdout")
    parser.add_argument("--compare-to", metavar="REF", help="print only what differs from REF")
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)  # check_ref's run
    args = parser.parse_args(argv)
    source = args.db
    if not source.is_file():
        print(f"no atlas at {source}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="audit-rules-check-") as scratch:
        copy = Path(scratch) / "atlas.db"
        copy_atlas(source, copy)
        ours = check_copy(copy, args.all)
        if args.json:
            import waive

            payload = {"code": str(Path(waive.__file__).parent), **ours.__dict__}
            output = [json.dumps(payload)]
        elif args.compare_to:
            theirs = check_ref(args.compare_to, source.resolve(), args.all, Path(scratch))
            output = differences(ours, theirs, args.compare_to)
        else:
            output = render(ours)
    text = "".join(f"{line}\n" for line in output)
    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
