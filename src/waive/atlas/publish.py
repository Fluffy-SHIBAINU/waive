"""Verification outcomes, cross-checks, versioning and export (spec §8 steps 5–7)."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.schema import Layer, ProcedureSheet, SheetStatus
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


def copy_fields(target: ProcedureSheet, source: ProcedureSheet, paths: list[str]) -> ProcedureSheet:
    """`target` with the cited fields at `paths` taken from `source` (None copies as None)."""
    updates: dict[str, Any] = {}
    for path in paths:
        section_name, field_name = path.split(".")
        section = updates.get(section_name, getattr(target, section_name))
        cited = getattr(getattr(source, section_name), field_name)
        updates[section_name] = section.model_copy(update={field_name: cited})
    return target.model_copy(update=updates)


def carry_over_reported(sheet: ProcedureSheet, previous: ProcedureSheet) -> ProcedureSheet:
    """Rebuilds re-read documents only; patient-reported fields have no quote or source and must
    survive a rebuild. A reported field never replaces a documented one at the same path."""
    updates: dict[str, Any] = {}
    for path, cited in previous.field_paths():
        if cited.layer is not Layer.REPORTED:
            continue
        section_name, field_name = path.split(".")
        section = updates.get(section_name, getattr(sheet, section_name))
        if getattr(section, field_name) is None:
            updates[section_name] = section.model_copy(update={field_name: cited})
    return sheet.model_copy(update=updates) if updates else sheet


def critical_conflicts(primary: ProcedureSheet, secondary: ProcedureSheet) -> list[str]:
    conflicts = []
    for path in CRITICAL_PATHS:
        first, second = _json_value(primary, path), _json_value(secondary, path)
        if first is not None and second is not None and first != second:
            conflicts.append(path)
    return conflicts


def resolve_conflicts(
    primary: ProcedureSheet,
    secondary: ProcedureSheet,
    tiebreak: ProcedureSheet,
    conflicts: list[str],
) -> tuple[ProcedureSheet, list[str], dict[str, dict[str, Any]]]:
    """Let a third model's draft settle each disputed path: siding with the primary keeps it,
    siding with the secondary takes the secondary's cited field (the caller still has to verify
    its quote), anything else leaves the conflict open.

    Returns the merged sheet, the conflicts still open, and per path the three JSON-normalised
    values plus the verdict ("primary", "secondary" or "unsettled") for the review item."""
    detail: dict[str, dict[str, Any]] = {}
    remaining: list[str] = []
    from_secondary: list[str] = []
    for path in conflicts:
        first, second, third = (
            _json_value(sheet, path) for sheet in (primary, secondary, tiebreak)
        )
        if third is not None and third == first:
            verdict = "primary"
        elif third is not None and third == second:
            verdict = "secondary"
            from_secondary.append(path)
        else:
            verdict = "unsettled"
            remaining.append(path)
        detail[path] = {
            "primary": first,
            "secondary": second,
            "tiebreak": third,
            "verdict": verdict,
        }
    merged = copy_fields(primary, secondary, from_secondary) if from_secondary else primary
    return merged, remaining, detail


# A free-care limit this far above the poverty line with no discount table has, in practice, been
# the policy's overall eligibility ceiling misread as free care (UMass Memorial: "less than 600%").
FREE_CARE_REVIEW_FPL = Decimal(400)


def sheet_inconsistencies(sheet: ProcedureSheet) -> list[str]:
    """Income rules that contradict each other or read like a misread ceiling. Checked at publish
    time rather than as a schema rule, so stored versions stay loadable and the structurer's
    fallback never drops a correct table to satisfy a wrong limit."""
    free = sheet.eligibility.free_care_max_fpl
    if free is None:
        return []
    tiers = sheet.eligibility.discount_tiers
    if tiers is not None and tiers.value:
        lowest = min(tier.min_fpl_exclusive for tier in tiers.value)
        if free.value > lowest:
            return [
                f"eligibility.free_care_max_fpl: {free.value:.0f}% is above the first discount "
                f"band, which starts at {lowest:.0f}%; every band under the limit would be dead"
            ]
        return []
    if free.value > FREE_CARE_REVIEW_FPL:
        return [
            f"eligibility.free_care_max_fpl: {free.value:.0f}% with no discount table reads like "
            "an eligibility ceiling, not free care"
        ]
    return []


def decide_status(sheet: ProcedureSheet, conflicts: list[str]) -> SheetStatus:
    if conflicts:
        return SheetStatus.HELD
    if sheet.eligibility.free_care_max_fpl is None and sheet.eligibility.discount_tiers is None:
        return SheetStatus.HELD
    if sheet_inconsistencies(sheet):
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
    """Write the public JSON export; the fictional demo hospital stays out of it."""
    sheets = [
        sheet
        for sheet in repo.list_latest_sheets(session, state)
        if not repo.is_demo(sheet.hospital.ccn)
    ]
    payload = {
        "generated_on": datetime.now(UTC).date().isoformat(),
        "license": "CC BY 4.0",
        "state": state.upper(),
        "sheets": [sheet.model_dump(mode="json") for sheet in sheets],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return len(sheets)
