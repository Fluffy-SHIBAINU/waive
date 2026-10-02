"""Verification outcomes, cross-checks, versioning and export (spec §8 steps 5–7)."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.schema import ProcedureSheet, SheetStatus
from waive.db import SheetRow

CRITICAL_PATHS = (
    "eligibility.free_care_max_fpl",
    "eligibility.discount_tiers",
    "apply.window_days_from_first_bill",
    "programs.presumptive",
)


def _json_value(sheet: ProcedureSheet, path: str) -> Any:
    section_name, field_name = path.split(".")
    cited = getattr(getattr(sheet, section_name), field_name)
    if cited is None:
        return None
    return json.loads(json.dumps(cited.model_dump(mode="json")["value"], sort_keys=True))


def drop_fields(sheet: ProcedureSheet, paths: list[str]) -> ProcedureSheet:
    updates: dict[str, Any] = {}
    for path in paths:
        section_name, field_name = path.split(".")
        section = updates.get(section_name, getattr(sheet, section_name))
        updates[section_name] = section.model_copy(update={field_name: None})
    return sheet.model_copy(update=updates)


def critical_conflicts(primary: ProcedureSheet, secondary: ProcedureSheet) -> list[str]:
    conflicts = []
    for path in CRITICAL_PATHS:
        first, second = _json_value(primary, path), _json_value(secondary, path)
        if first is not None and second is not None and first != second:
            conflicts.append(path)
    return conflicts


def decide_status(sheet: ProcedureSheet, conflicts: list[str]) -> SheetStatus:
    if conflicts:
        return SheetStatus.HELD
    if sheet.eligibility.free_care_max_fpl is None and sheet.eligibility.discount_tiers is None:
        return SheetStatus.HELD
    return SheetStatus.PUBLISHED


def diff_sheets(old: ProcedureSheet | None, new: ProcedureSheet) -> dict[str, dict[str, Any]]:
    paths = {path for path, _ in new.field_paths()}
    if old is not None:
        paths |= {path for path, _ in old.field_paths()}
    diff: dict[str, dict[str, Any]] = {}
    for path in sorted(paths):
        before = None if old is None else _json_value(old, path)
        after = _json_value(new, path)
        if before != after:
            diff[path] = {"old": before, "new": after}
    if old is not None and old.status != new.status:
        diff["status"] = {"old": old.status.value, "new": new.status.value}
    return diff


def publish_sheet(session: Session, sheet: ProcedureSheet) -> SheetRow | None:
    latest = repo.latest_sheet(session, sheet.hospital.ccn)
    previous = latest[0] if latest else None
    diff = diff_sheets(previous, sheet)
    if previous is not None and not diff:
        return None
    version = 1 if previous is None else previous.version + 1
    return repo.add_sheet_version(session, sheet.model_copy(update={"version": version}), diff)


def export_state(session: Session, state: str, path: Path) -> int:
    sheets = repo.list_latest_sheets(session, state)
    payload = {
        "generated_on": datetime.now(UTC).date().isoformat(),
        "license": "CC BY 4.0",
        "state": state.upper(),
        "sheets": [sheet.model_dump(mode="json") for sheet in sheets],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return len(sheets)
