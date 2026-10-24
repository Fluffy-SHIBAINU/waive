"""Verification outcomes, cross-checks, versioning and export (spec §8 steps 5–7)."""

import csv
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.schema import (
    Cited,
    DiscountTier,
    Layer,
    ProcedureSheet,
    SheetStatus,
    SourceDoc,
    SourceKind,
)
from waive.atlas.verify import (
    assistance_limits_stated,
    free_care_limits_stated,
    tiers_rise_with_income,
)
from waive.db import HospitalRow, SheetRow

CRITICAL_PATHS = (
    "eligibility.free_care_max_fpl",
    "eligibility.discount_tiers",
    "eligibility.assistance_ceiling_fpl",
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


CEILING_PATH = "eligibility.assistance_ceiling_fpl"


def _ceiling_alone(sheet: ProcedureSheet) -> bool:
    """The sheet's one income rule is the assistance ceiling (decision 7.9a-2)."""
    eligibility = sheet.eligibility
    return (
        eligibility.assistance_ceiling_fpl is not None
        and eligibility.free_care_max_fpl is None
        and eligibility.discount_tiers is None
    )


def critical_conflicts(primary: ProcedureSheet, secondary: ProcedureSheet) -> list[str]:
    """The critical paths the two models' (verified) sheets disagree on. A primary that would
    publish only a ceiling is disputed by a secondary that reads a free-care band or a table: the
    documents state more than the ceiling (review of 7.14). The other way round nothing new would
    publish, as before 7.14: the primary's band or table is the answer and carries no ceiling."""
    conflicts = []
    for path in CRITICAL_PATHS:
        first, second = _json_value(primary, path), _json_value(secondary, path)
        if first is not None and second is not None and first != second:
            conflicts.append(path)
        elif path == CEILING_PATH and _ceiling_alone(primary) and second is None:
            eligibility = secondary.eligibility
            if eligibility.free_care_max_fpl is not None or eligibility.discount_tiers is not None:
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


# How far above the last income covered a band may start: band labels give whole percents ("0-170%",
# "171-250%"), so a band from 171 after one to 170 leaves no gap (291314).
BAND_LABEL_STEP = Decimal(1)


def _below_own_rules(
    ceiling: Decimal, free: Cited[Decimal] | None, tiers: Cited[list[DiscountTier]] | None
) -> bool:
    """Whether an assistance ceiling is below the free-care limit or the top of a band of the same
    sheet. Russell Regional (171350) published a ceiling of 70 under a table that runs to 200%:
    the ceiling was its free-care line."""
    tops = [tier.max_fpl_inclusive for tier in tiers.value] if tiers is not None else []
    if free is not None:
        tops.append(free.value)
    return any(ceiling < top for top in tops)


def _income_gaps(
    free: Cited[Decimal] | None, tiers: Cited[list[DiscountTier]] | None
) -> list[tuple[Decimal, Decimal]]:
    """The incomes (from, to) that fall between the free-care limit and the first band, or between
    two bands, more than a band label's step apart. University Health Lakewood (260102) published
    free care to 100% and its first band from 300%, the 101-300% band missing; Henry County (361309)
    free care to 100% and bands from 200%, while its table gives 101-200% a 100% discount. A sheet
    with no free-care limit is not checked below its first band: its documents may state no free
    band, and a sheet whose free-care limit verification dropped publishes from its first band
    (Day Kimball, 070003, from 200%: its limit was quoted as the top of a later band). A limit
    dropped because the free band itself is misstated holds a sheet that keeps its table instead
    (verify.HOLDING_BESIDE_A_TABLE_REASONS)."""
    if tiers is None or not tiers.value:
        return []
    gaps: list[tuple[Decimal, Decimal]] = []
    covered = free.value if free is not None else None
    for tier in sorted(tiers.value, key=lambda tier: tier.min_fpl_exclusive):
        if covered is not None and tier.min_fpl_exclusive > covered + BAND_LABEL_STEP:
            gaps.append((covered, tier.min_fpl_exclusive))
        top = tier.max_fpl_inclusive
        covered = top if covered is None else max(covered, top)
    return gaps


def sheet_inconsistencies(sheet: ProcedureSheet, ruled_ceiling: Decimal | None = None) -> list[str]:
    """Income rules that contradict each other or read like a misread ceiling. Checked at publish
    time rather than as a schema rule, so stored versions stay loadable and the structurer's
    fallback never drops a correct table to satisfy a wrong limit. An assistance ceiling equal to
    `ruled_ceiling`, the value of the owner's `discount` ruling (eve_import.ruled_ceiling), is the
    owner's and is not held below the sheet's own rules: owner rulings win, and the pipeline
    records the conflict for the owner instead."""
    problems: list[str] = []
    tiers = sheet.eligibility.discount_tiers
    if tiers is not None and tiers.value and tiers_rise_with_income(tiers.value):
        # The safety net behind verify's patient-share rule (Adventist Health, 50/75/75): a table
        # that gives richer households a larger discount is the patient's share, not a discount.
        ordered = sorted(tiers.value, key=lambda tier: tier.min_fpl_exclusive)
        percents = ", ".join(f"{tier.discount_percent}%" for tier in ordered)
        problems.append(
            f"eligibility.discount_tiers: the discount rises with income ({percents}), which "
            "reads as the patient's share, not a sliding scale"
        )
    free = sheet.eligibility.free_care_max_fpl
    ceiling = sheet.eligibility.assistance_ceiling_fpl
    if (
        ceiling is not None
        and ceiling.value != ruled_ceiling
        and _below_own_rules(ceiling.value, free, tiers)
    ):
        problems.append(
            f"eligibility.assistance_ceiling_fpl: {ceiling.value:.0f}% is below the sheet's own "
            "income rules"
        )
    for start, end in _income_gaps(free, tiers):
        problems.append(
            f"eligibility.discount_tiers: no rule covers incomes from {start:.0f}% to {end:.0f}%"
        )
    if free is None:
        return problems
    if tiers is not None and tiers.value:
        lowest = min(tier.min_fpl_exclusive for tier in tiers.value)
        if free.value > lowest:
            problems.append(
                f"eligibility.free_care_max_fpl: {free.value:.0f}% is above the first discount "
                f"band, which starts at {lowest:.0f}%; every band under the limit would be dead"
            )
        return problems
    if free.value > FREE_CARE_REVIEW_FPL:
        problems.append(
            f"eligibility.free_care_max_fpl: {free.value:.0f}% with no discount table reads like "
            "an eligibility ceiling, not free care"
        )
    return problems


DOCUMENT_CONFLICT_REASON = "the hospital's documents state different free-care limits"
CEILING_CONFLICT_REASON = (
    "the hospital's documents state a free-care band or a higher limit than the assistance ceiling"
)


def conflict_reason(sheet: ProcedureSheet) -> str:
    """How a hold on `document_conflicts` reads for this sheet."""
    return CEILING_CONFLICT_REASON if _ceiling_alone(sheet) else DOCUMENT_CONFLICT_REASON


def document_conflicts(sheet: ProcedureSheet, documents: dict[str, str]) -> list[dict[str, Any]]:
    """The hospital's other documents whose free-care sentences name a limit, none of which is
    the one the sheet publishes: one entry per document, with the limits it states. AHMC Anaheim
    (050226, review of 7.9): the web page said 0-250% is no-cost care, the newer PDF 200%, and
    the sheet published 250 because no discount table tripped sheet_inconsistencies. The model's
    pick between two documents is an admin's decision, so the sheet is held. Only the hospital's
    web documents count: a patient's photo of a newer policy was approved by an admin and is
    meant to supersede the stored page (the learning loop), not to be held against it.

    A sheet that publishes only an assistance ceiling says the documents name no free-care band
    and no help above the ceiling (task 7.14), so any document, the ceiling's own included, that
    names a free-care limit, or help with the bill at a higher percentage of the poverty level
    (a sliding scale on to 400% after "at or below 200% ... eligible"; review of 7.14), conflicts."""
    free = sheet.eligibility.free_care_max_fpl
    ceiling = sheet.eligibility.assistance_ceiling_fpl if _ceiling_alone(sheet) else None
    # The income rule the documents are checked against: the free-care band, or the ceiling.
    rule = free if free is not None else ceiling
    if rule is None or rule.layer is Layer.REPORTED:
        return []
    kinds = {source.id: source.kind for source in sheet.sources}
    if kinds.get(rule.source_id or "") is not SourceKind.HOSPITAL_WEB:
        return []
    conflicts: list[dict[str, Any]] = []
    for source in sheet.sources:
        if source.kind is not SourceKind.HOSPITAL_WEB or source.id not in documents:
            continue
        if free is not None and source.id == free.source_id:
            continue
        text = documents[source.id]
        limits = free_care_limits_stated(text)
        if ceiling is not None:
            limits |= {limit for limit in assistance_limits_stated(text) if limit > ceiling.value}
        if limits and (free is None or Decimal(free.value) not in limits):
            conflicts.append({"id": source.id, "limits": [str(limit) for limit in sorted(limits)]})
    return conflicts


def decide_status(
    sheet: ProcedureSheet,
    conflicts: list[str],
    holds: list[str] | None = None,
    ruled_ceiling: Decimal | None = None,
) -> SheetStatus:
    """`holds` are reasons an admin must look first that are not model disagreements, such as
    a discount table that turned out to describe the patient's share. A sheet needs an income
    rule to publish: a free-care band, a table, or, when the documents give neither, a verified
    assistance ceiling (decision 7.9a-2, task 7.14). `ruled_ceiling` goes to
    sheet_inconsistencies: the owner's ruled ceiling is not held below the sheet's own rules."""
    if conflicts or holds:
        return SheetStatus.HELD
    eligibility = sheet.eligibility
    if (
        eligibility.free_care_max_fpl is None
        and eligibility.discount_tiers is None
        and eligibility.assistance_ceiling_fpl is None
    ):
        return SheetStatus.HELD
    if sheet_inconsistencies(sheet, ruled_ceiling):
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


def _latest_by_hospital(
    session: Session, state: str
) -> list[tuple[HospitalRow, tuple[ProcedureSheet, SheetRow] | None]]:
    """Every registry hospital of a state, by name, with its newest sheet version (None when it
    was never scouted). The fictional demo hospital stays out of every export."""
    return [
        (hospital, repo.latest_sheet(session, hospital.ccn))
        for hospital in repo.list_hospitals(session, state=state)
        if not repo.is_demo(hospital.ccn)
    ]


def export_state(session: Session, state: str, path: Path) -> int:
    """Write the public JSON export: each hospital's sheet when its newest version is published
    (a held one is still being checked and withdrew any version before it; audit
    rules-correctness-9), each with the stored `diff` of that version. The fictional demo
    hospital stays out of it."""
    sheets: list[dict[str, Any]] = []
    for _hospital, found in _latest_by_hospital(session, state):
        if found is None or found[0].status is not SheetStatus.PUBLISHED:
            continue
        sheet, row = found
        sheets.append({**sheet.model_dump(mode="json"), "diff": row.diff})
    payload = {
        "generated_on": datetime.now(UTC).date().isoformat(),
        "license": "CC BY 4.0",
        "state": state.upper(),
        "sheets": sheets,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return len(sheets)


# The snapshot Tender imports (Kindred connect, spec 5.3): this header, in this order.
TENDER_COLUMNS: tuple[str, ...] = (
    "ccn",
    "name",
    "status",
    "website",
    "fap_url",
    "source_kind",
    "source_title",
    "free_pct",
    "quote",
    "discount_pct",
    "discount_quote",
    "retrieved_at",
    "system_name",
    "system_size",
    "sheet_version",
    "source_sha256",
)
# Tender stores a website only when discovery matched the domain at least this surely; below it
# the column stays empty and Tender keeps the website it holds (an admin's entry included).
TENDER_WEBSITE_CONFIDENCE = 0.9
UNSCOUTED = "unscouted"


def _source_of(sheet: ProcedureSheet, cited: Cited[Any] | None) -> SourceDoc | None:
    if cited is None or cited.source_id is None:
        return None
    return next((source for source in sheet.sources if source.id == cited.source_id), None)


def tender_row(
    hospital: HospitalRow, found: tuple[ProcedureSheet, SheetRow] | None
) -> dict[str, str]:
    """One snapshot row. `status` follows the hospital's newest sheet version (`unscouted` with
    none); only a published row carries the policy and website columns, so a held, draft or
    unscouted row tells Tender to clear what it imported before. The policy columns come from
    the free-care field, or from the discount table when the sheet has no free-care field; a
    sheet with neither, or whose cited source has no URL (a patient's photo, a Form 990), is
    exported as held: Tender shows a quote only with the URL it was read from. `system_size` is
    Tender's to fill."""
    row = dict.fromkeys(TENDER_COLUMNS, "")
    row["ccn"], row["name"] = hospital.ccn, hospital.name
    row["system_name"] = hospital.system or ""
    if found is None:
        row["status"] = UNSCOUTED
        return row
    sheet, _ = found
    if sheet.status is not SheetStatus.PUBLISHED:
        row["status"] = sheet.status.value
        return row
    free = sheet.eligibility.free_care_max_fpl
    tiers = sheet.eligibility.discount_tiers
    if tiers is not None and not tiers.value:
        tiers = None
    rule = free if free is not None else tiers
    source = _source_of(sheet, rule)
    if rule is None or source is None or not source.url:
        row["status"] = SheetStatus.HELD.value
        return row
    row["status"] = SheetStatus.PUBLISHED.value
    confidence = hospital.domain_confidence
    if (
        hospital.website_domain
        and confidence is not None
        and confidence >= TENDER_WEBSITE_CONFIDENCE
    ):
        row["website"] = hospital.website_domain
    row["fap_url"] = source.url
    row["source_kind"] = source.kind.value
    row["source_title"] = source.title
    if free is not None:
        row["free_pct"] = str(free.value)
        row["quote"] = free.quote or ""
    if tiers is not None:
        row["discount_pct"] = str(max(tier.max_fpl_inclusive for tier in tiers.value))
        row["discount_quote"] = tiers.quote or ""
    row["retrieved_at"] = rule.checked_on.isoformat()
    row["sheet_version"] = str(sheet.version)
    row["source_sha256"] = source.sha256
    return row


def export_tender(session: Session, state: str, path: Path) -> int:
    """Write the per-state snapshot Tender imports: one row per registry hospital of the state
    under TENDER_COLUMNS (see `tender_row`). A snapshot is complete, so a hospital whose sheet
    was withdrawn appears with an empty policy, and Tender clears it. Returns the row count."""
    rows = [tender_row(hospital, found) for hospital, found in _latest_by_hospital(session, state)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TENDER_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
