"""Import draft sheets built by the Eve waive-agent (data/atlas-drafts/<STATE>/<CCN>.json).

The agent's drafts carry the same cited-field shape as a procedure sheet, with a few differences
this module reconciles:

- source ``kind`` is ``pdf`` or ``hospital_web``; both are hospital web documents here.
- ``residency`` is one sentence, not a list.
- ``documents_required`` are free-text items, mapped onto ``DocType`` by keyword.
- ``submit_methods.kind`` may say ``online``; the sheet says ``portal``.
- a quote may hold up to three verbatim excerpts joined with ``" || "``; all are kept, and
  ``verify.quote_found`` checks each span on its own.
- discount tiers of 0% or 100% are not tiers; a 100% row is the free-care line.

Every imported sheet is version 1 with status ``draft``. Nothing is published here: a reviewer
runs the normal verify and publish path on the stored sources.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.registry import row_to_hospital
from waive.atlas.schema import DocType, ProcedureSheet, SheetStatus, SourceKind

DOC_KEYWORDS: tuple[tuple[DocType, tuple[str, ...]], ...] = (
    (
        DocType.PHOTO_ID,
        ("photo id", "photo identification", "driver", "state id", "passport", "identification"),
    ),
    (DocType.TAX_RETURN, ("tax return", "1040", "tax transcript", "income tax")),
    (DocType.PAY_STUBS, ("pay stub", "paystub", "pay check", "paycheck", "payroll", "wage")),
    (
        DocType.BANK_STATEMENTS,
        ("bank statement", "checking", "savings statement", "account statement"),
    ),
    (
        DocType.SOCIAL_SECURITY_LETTER,
        ("social security", "ssa", "ssi", "ssdi", "benefit letter", "award letter"),
    ),
    (
        DocType.PROOF_OF_RESIDENCY,
        ("residency", "lease", "utility bill", "proof of address", "mortgage"),
    ),
    (
        DocType.INSURANCE_CARD,
        ("insurance card", "insurance information", "explanation of benefits", "eob"),
    ),
    (
        DocType.MEDICAID_DENIAL,
        ("medicaid denial", "denial letter", "medicaid determination", "masshealth denial"),
    ),
    (
        DocType.PROOF_OF_INCOME,
        (
            "proof of income",
            "income verification",
            "unemployment",
            "pension",
            "child support",
            "alimony",
            "profit and loss",
            "self-employ",
            "disability",
            "income",
        ),
    ),
)

SUBMIT_KINDS = {"mail", "fax", "email", "portal", "in_person", "phone"}
SUBMIT_ALIASES = {
    "online": "portal",
    "web": "portal",
    "website": "portal",
    "mychart": "portal",
    "in person": "in_person",
    "in-person": "in_person",
    "walk-in": "in_person",
    "telephone": "phone",
    "call": "phone",
    "post": "mail",
}

# Colorado's Hospital Discounted Care (C.R.S. 25.5-3-501 and following) caps what a patient at or
# below 250% FPL pays. Policies show it as a screening row, a "$0 copay" row, or a sentence about
# the legislation. It is a state program, not the hospital's free-care line (review decision
# 2026-10-23). A copay row with a dollar amount is not free care anywhere.
_HDC_QUOTE = re.compile(
    r"\bHDC\b|hospital discounted care|discounted care program|\bCRS\b|25\.5-3-5"
    r"|(?:legislation|law) requires hospitals to provide (?:financial assistance|discounted care)"
    r"|\$\s?0 copay",
    re.I,
)
_COPAY_ROW = re.compile(r"\$\s?[1-9]\d*\s?copay|copay per visit", re.I)
HDC_PROGRAM = {
    "name": "Colorado Hospital Discounted Care (income at or below 250% FPL)",
    "how_to_apply": "Ask the hospital to screen you for Hospital Discounted Care; Colorado law "
    "requires the screening and caps what you pay.",
}


@dataclass
class ImportResult:
    ccn: str
    state: str
    name: str
    sheet: ProcedureSheet | None
    notes: list[str] = field(default_factory=list)
    error: str | None = None


def load_seed(seed_dir: Path, state: str) -> dict[str, dict[str, Any]]:
    """The newest CMS snapshot for a state, keyed by CCN. Empty when none exists."""
    files = sorted(seed_dir.glob(f"cms-hospitals-{state.upper()}-*.json"))
    if not files:
        return {}
    rows = json.loads(files[-1].read_text(encoding="utf-8"))
    return {row["facility_id"]: row for row in rows}


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip().rstrip("%"))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _int(value: Any) -> int | None:
    number = _decimal(value)
    if number is None:
        return None
    return int(number.to_integral_value())


def _split_quote(quote: str | None) -> tuple[str | None, list[str]]:
    if not quote:
        return None, []
    parts = [part.strip() for part in quote.split(" || ") if part.strip()]
    if not parts:
        return None, []
    return parts[0], parts[1:]


def _cited(
    raw: dict[str, Any] | None, value: Any, today: date, notes: list[str], path: str
) -> dict[str, Any] | None:
    if raw is None or value is None:
        return None
    quote, extra = _split_quote(raw.get("quote"))
    if not quote or not raw.get("source_id"):
        notes.append(f"{path}: no quote or source id; dropped")
        return None
    if extra:
        # Several verbatim spans back one value (a tier table split across rows, a document list
        # across sentences). Keep them all; `verify.quote_found` checks each span on its own.
        quote = " || ".join([quote, *extra])
        notes.append(f"{path}: value backed by {len(extra) + 1} verbatim spans")
    if raw.get("verification") == "normalized":
        notes.append(f"{path}: quote matched the source only after typography normalization")
    checked = raw.get("checked_on") or today.isoformat()
    return {
        "value": value,
        "layer": "documented",
        "quote": quote,
        "source_id": raw["source_id"],
        "checked_on": checked,
        "confidence": float(raw["confidence"])
        if isinstance(raw.get("confidence"), int | float)
        and not isinstance(raw.get("confidence"), bool)
        else 1.0,
        "support_count": 0,
    }


def _doc_types(items: Any) -> list[str]:
    out: list[str] = []
    for item in items or []:
        text = str(item).lower()
        kind = DocType.OTHER
        for doc_type, keys in DOC_KEYWORDS:
            if any(key in text for key in keys):
                kind = doc_type
                break
        if kind.value not in out:
            out.append(kind.value)
    return out


def _submit_methods(items: Any, notes: list[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip().lower()
        kind = SUBMIT_ALIASES.get(kind, kind)
        if kind not in SUBMIT_KINDS:
            notes.append(f"apply.submit_methods: unknown kind {item.get('kind')!r} skipped")
            continue
        out.append({"kind": kind, "detail": str(item.get("detail", ""))})
    return out


def _tiers(items: Any, notes: list[str], quote: str | None = None) -> list[dict[str, Any]] | None:
    tiers: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        lo = _decimal(item.get("min_fpl_exclusive"))
        hi = _decimal(item.get("max_fpl_inclusive"))
        pct = _decimal(item.get("discount_percent"))
        if lo is None or hi is None or pct is None:
            notes.append("eligibility.discount_tiers: non-numeric tier skipped")
            continue
        if pct >= 100:
            notes.append(
                f"eligibility.discount_tiers: {lo}-{hi}% at {pct}% is the free-care band, not a tier; skipped"
            )
            continue
        if pct < 1:
            notes.append(
                f"eligibility.discount_tiers: {lo}-{hi}% at {pct}% is no discount; skipped"
            )
            continue
        pct_text = f"{int(pct) if pct == pct.to_integral_value() else pct}"
        if quote:
            capped = re.search(rf"up to (?:a |an )?{pct_text}\s?%", quote, re.I) is not None
            fixed = (
                re.search(rf"(?<!up to )(?<!up to a )(?<!up to an )\b{pct_text}\s?%", quote, re.I)
                is not None
            )
            if capped and not fixed:
                notes.append(
                    f"eligibility.discount_tiers: {lo}-{hi}% says 'up to {pct}%', a cap rather "
                    "than a fixed discount; skipped"
                )
                continue
            if capped and fixed:
                notes.append(
                    f"eligibility.discount_tiers: the quote says both 'up to {pct}%' and a fixed "
                    f"{pct}%; {lo}-{hi}% kept, check which band is the cap"
                )
        tiers.append({"min_fpl_exclusive": lo, "max_fpl_inclusive": hi, "discount_percent": pct})
    tiers.sort(key=lambda t: t["min_fpl_exclusive"])
    return tiers or None


def _state_programs(
    p: dict[str, Any],
    free_raw: dict[str, Any] | None,
    hdc_program: dict[str, Any] | None,
    today: date,
    notes: list[str],
) -> dict[str, Any] | None:
    """The agent's state programs, plus Colorado Hospital Discounted Care when the free-care quote
    was really the HDC row: that quote and source back the program entry."""
    raw = p.get("state_programs") if isinstance(p.get("state_programs"), dict) else None
    items = [x for x in ((raw or {}).get("value") or []) if isinstance(x, dict) and x.get("name")]
    if hdc_program is not None and not any(
        "discounted care" in str(x.get("name", "")).lower() for x in items
    ):
        items = [*items, hdc_program]
        if raw is None and free_raw is not None:
            raw = {**free_raw}
    return _cited(raw, items or None, today, notes, "programs.state_programs")


def convert_draft(
    draft: dict[str, Any],
    seed_row: dict[str, Any] | None,
    today: date,
    decision: dict[str, Any] | None = None,
) -> ImportResult:
    hospital = draft.get("hospital") or {}
    ccn = str(hospital.get("ccn", ""))
    state = str(hospital.get("state", "")).upper()
    name = str(hospital.get("name", ""))
    notes: list[str] = []
    result = ImportResult(ccn=ccn, state=state, name=name, sheet=None, notes=notes)

    if seed_row is None:
        result.error = "no CMS seed row for this CCN; run `waive atlas seed --state` first"
        return result
    hospital_ref = row_to_hospital(seed_row)
    if hospital.get("website_domain") and not hospital_ref.get("website_domain"):
        hospital_ref["website_domain"] = hospital["website_domain"]

    sources = []
    for src in draft.get("sources") or []:
        if not isinstance(src, dict) or not src.get("id") or not src.get("sha256"):
            result.error = f"malformed source entry in draft: {str(src)[:120]}"
            return result
        kind = src.get("kind")
        sources.append(
            {
                "id": src["id"],
                "kind": SourceKind.HOSPITAL_WEB.value
                if kind in ("pdf", "hospital_web", None)
                else kind,
                "url": src.get("url"),
                "title": src.get("title") or "",
                "fetched_on": src.get("fetched_on") or today.isoformat(),
                "sha256": src["sha256"],
            }
        )

    e = draft.get("eligibility") or {}
    p = draft.get("programs") or {}
    a = draft.get("apply") or {}
    c = draft.get("collections") or {}
    k = draft.get("contacts") or {}

    def val(section: dict[str, Any], key: str) -> Any:
        raw = section.get(key)
        return raw.get("value") if isinstance(raw, dict) else None

    residency = val(e, "residency")
    if isinstance(residency, str):
        residency = [residency]
    free_value = _decimal(val(e, "free_care_max_fpl"))
    if free_value is not None and free_value < 100:
        notes.append(
            f"eligibility.free_care_max_fpl: {free_value}% FPL is below the poverty line; "
            "left for review, not recorded"
        )
        free_value = None
    free_raw = e.get("free_care_max_fpl") if isinstance(e.get("free_care_max_fpl"), dict) else None
    free_quote = (free_raw or {}).get("quote") or ""
    hdc_program: dict[str, Any] | None = None
    if free_value is not None and state == "CO" and _HDC_QUOTE.search(free_quote):
        notes.append(
            f"eligibility.free_care_max_fpl: {free_value}% is Colorado Hospital Discounted Care, "
            "recorded under programs.state_programs instead of the free-care line"
        )
        hdc_program = {**HDC_PROGRAM}
        free_value = None
    elif (
        free_value is not None
        and _COPAY_ROW.search(free_quote)
        and not re.search(r"\$\s?0\b", free_quote)
    ):
        notes.append(
            "eligibility.free_care_max_fpl: the quote is a copay row with a dollar copay, "
            "not free care; left for review, not recorded"
        )
        free_value = None
    ceiling_from_decision: Decimal | None = None
    if decision is not None:
        verdict = str(decision.get("decision") or "")
        note = str(decision.get("note") or "").strip()
        tail = f" Note: {note}" if note else ""
        if verdict == "discount":
            if free_value is not None:
                existing_ceiling = _decimal(val(e, "assistance_ceiling_fpl"))
                if existing_ceiling is None:
                    ceiling_from_decision = free_value
                    where = "recorded as assistance_ceiling_fpl"
                else:
                    where = f"dropped; the sheet already records a {existing_ceiling}% ceiling"
                notes.append(
                    f"eligibility.free_care_max_fpl: owner review says {free_value}% is a discount "
                    f"or assistance limit, not free care; {where}.{tail}"
                )
            else:
                notes.append(
                    f"eligibility.free_care_max_fpl: owner review: discount, not free care.{tail}"
                )
            free_value = None
        elif verdict in ("hold", "unsure"):
            label = "hold" if verdict == "hold" else "cannot tell from the document; rebuild"
            notes.append(f"eligibility.free_care_max_fpl: owner review: {label}.{tail}")
            free_value = None
        elif verdict == "accept":
            if free_value is not None:
                notes.append(
                    f"eligibility.free_care_max_fpl: owner review: accepted as free care.{tail}"
                )
            else:
                notes.append(
                    "eligibility.free_care_max_fpl: owner review said accept, but the extracted "
                    f"value is not a usable free-care line and stays unrecorded.{tail}"
                )

    body: dict[str, Any] = {
        "hospital": hospital_ref,
        "version": 1,
        "status": SheetStatus.DRAFT.value,
        "eligibility": {
            "free_care_max_fpl": _cited(
                e.get("free_care_max_fpl"),
                free_value,
                today,
                notes,
                "eligibility.free_care_max_fpl",
            ),
            "discount_tiers": _cited(
                e.get("discount_tiers"),
                _tiers(
                    val(e, "discount_tiers"),
                    notes,
                    e["discount_tiers"].get("quote")
                    if isinstance(e.get("discount_tiers"), dict)
                    else None,
                ),
                today,
                notes,
                "eligibility.discount_tiers",
            ),
            "assistance_ceiling_fpl": _cited(
                e.get("assistance_ceiling_fpl")
                or (free_raw if ceiling_from_decision is not None else None),
                _decimal(val(e, "assistance_ceiling_fpl"))
                if val(e, "assistance_ceiling_fpl") is not None
                else ceiling_from_decision,
                today,
                notes,
                "eligibility.assistance_ceiling_fpl",
            ),
            "asset_test": _cited(
                e.get("asset_test"),
                val(e, "asset_test") if isinstance(val(e, "asset_test"), bool) else None,
                today,
                notes,
                "eligibility.asset_test",
            ),
            "residency": _cited(
                e.get("residency"), residency, today, notes, "eligibility.residency"
            ),
            "insured_patients_covered": _cited(
                e.get("insured_patients_covered"),
                val(e, "insured_patients_covered")
                if isinstance(val(e, "insured_patients_covered"), bool)
                else None,
                today,
                notes,
                "eligibility.insured_patients_covered",
            ),
            "min_balance": _cited(
                e.get("min_balance"),
                _decimal(val(e, "min_balance")),
                today,
                notes,
                "eligibility.min_balance",
            ),
        },
        "programs": {
            "presumptive": _cited(
                p.get("presumptive"),
                [str(x) for x in (val(p, "presumptive") or [])] or None,
                today,
                notes,
                "programs.presumptive",
            ),
            "state_programs": _state_programs(p, free_raw, hdc_program, today, notes),
        },
        "apply": {
            "form_url": _cited(
                a.get("form_url"),
                val(a, "form_url")
                if isinstance(val(a, "form_url"), str) and val(a, "form_url").startswith("http")
                else None,
                today,
                notes,
                "apply.form_url",
            ),
            "documents_required": _cited(
                a.get("documents_required"),
                _doc_types(val(a, "documents_required")) or None,
                today,
                notes,
                "apply.documents_required",
            ),
            "submit_methods": _cited(
                a.get("submit_methods"),
                _submit_methods(val(a, "submit_methods"), notes) or None,
                today,
                notes,
                "apply.submit_methods",
            ),
            "window_days_from_first_bill": _cited(
                a.get("window_days_from_first_bill"),
                _int(val(a, "window_days_from_first_bill")),
                today,
                notes,
                "apply.window_days_from_first_bill",
            ),
            "decision_days": _cited(
                a.get("decision_days"),
                _int(val(a, "decision_days")),
                today,
                notes,
                "apply.decision_days",
            ),
        },
        "collections": {
            "eca_wait_days": _cited(
                c.get("eca_wait_days"),
                _int(val(c, "eca_wait_days")),
                today,
                notes,
                "collections.eca_wait_days",
            ),
        },
        "contacts": {
            "phone": _cited(
                k.get("phone"),
                val(k, "phone") if isinstance(val(k, "phone"), str) else None,
                today,
                notes,
                "contacts.phone",
            ),
            "hours": _cited(
                k.get("hours"),
                val(k, "hours") if isinstance(val(k, "hours"), str) else None,
                today,
                notes,
                "contacts.hours",
            ),
            "languages": _cited(
                k.get("languages"),
                [str(x) for x in (val(k, "languages") or [])] or None,
                today,
                notes,
                "contacts.languages",
            ),
        },
        "sources": sources,
    }
    if isinstance(val(a, "form_url"), str) and not val(a, "form_url").startswith("http"):
        notes.append(f"apply.form_url: not an absolute URL ({val(a, 'form_url')[:60]!r}); dropped")
    for item in draft.get("dropped") or []:
        notes.append(f"agent dropped {item.get('field')}: {item.get('reason')}")
    for item in draft.get("review_notes") or []:
        notes.append(f"agent judge on {item.get('field')}: {item.get('note')}")

    try:
        result.sheet = ProcedureSheet.model_validate(body)
    except ValidationError as error:
        # Retry without the tiers when they are what the schema rejects (overlap or order).
        if "discount tier" in str(error) or "discount_tiers" in str(error):
            body["eligibility"]["discount_tiers"] = None
            notes.append(
                f"eligibility.discount_tiers: rejected by schema ({str(error).splitlines()[0][:100]}); dropped"
            )
            try:
                result.sheet = ProcedureSheet.model_validate(body)
                return result
            except ValidationError as again:
                error = again
        result.error = str(error).replace("\n", " ")[:400]
    return result


def load_decisions(path: Path | None) -> dict[str, dict[str, Any]]:
    """Owner decisions from the review page, keyed by CCN: {decision: accept|hold|discount|unsure, note}."""
    if path is None or not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {str(r["ccn"]): r for r in rows if isinstance(r, dict) and r.get("ccn")}


def read_drafts(draft_dir: Path, states: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    drafts: list[dict[str, Any]] = []
    for state_dir in sorted(draft_dir.iterdir()):
        if not state_dir.is_dir() or not re.fullmatch(r"[A-Z]{2}", state_dir.name):
            continue
        if states and state_dir.name not in states:
            continue
        for file in sorted(state_dir.glob("*.json")):
            if file.name.startswith("_"):
                continue
            drafts.append(json.loads(file.read_text(encoding="utf-8")))
    return drafts


def write_exports(results: list[ImportResult], out_dir: Path, today: date) -> dict[str, int]:
    """One file per state in the public export shape, every sheet a draft, plus the review notes."""
    by_state: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        if r.sheet is None:
            continue
        by_state.setdefault(r.state, []).append(
            {**r.sheet.model_dump(mode="json"), "eve_review": r.notes}
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for state, sheets in sorted(by_state.items()):
        payload = {
            "generated_on": today.isoformat(),
            "license": "CC BY 4.0",
            "state": state,
            "status": "draft",
            "built_by": "eve waive-agent",
            "sheets": sheets,
        }
        (out_dir / f"{state.lower()}.json").write_text(
            json.dumps(payload, indent=1), encoding="utf-8"
        )
        counts[state] = len(sheets)
    return counts


def import_to_db(
    session: Session,
    results: list[ImportResult],
    seeds: dict[str, dict[str, dict[str, Any]]],
    sources_dir: Path | None = None,
) -> int:
    """Store each converted sheet as version 1 draft. Skips a CCN that already has any sheet.
    Source text comes from `sources_dir/<source id>.txt` when present (the refetch pass writes
    it); otherwise it is empty and the review item says so."""
    stored = 0
    for r in results:
        if r.sheet is None:
            continue
        if repo.latest_sheet(session, r.ccn) is not None:
            r.notes.append("db: a sheet already exists for this CCN; not imported")
            continue
        repo.upsert_hospital(session, row_to_hospital(seeds[r.state][r.ccn]))
        texts_stored = 0
        for doc in r.sheet.sources:
            text = ""
            if sources_dir is not None:
                candidate = sources_dir / f"{doc.id}.txt"
                if candidate.exists():
                    text = candidate.read_text(encoding="utf-8")
                    texts_stored += 1
            repo.save_source(session, doc, text, r.ccn)
        repo.add_sheet_version(session, r.sheet, None)
        repo.add_review_item(
            session,
            r.ccn,
            "eve-import",
            {
                "notes": r.notes,
                "source_text_stored": texts_stored,
                "sources": len(r.sheet.sources),
                "imported_on": datetime.now(UTC).isoformat(),
            },
        )
        stored += 1
    return stored


def write_report(results: list[ImportResult], out_dir: Path, today: date) -> Path:
    ok = [r for r in results if r.sheet is not None]
    failed = [r for r in results if r.sheet is None]
    lines = [
        "# Eve draft import report",
        "",
        f"{today.isoformat()}: {len(results)} drafts read, {len(ok)} converted to draft procedure sheets, {len(failed)} rejected by the schema.",
        "",
        "| State | Converted |",
        "|---|---|",
    ]
    by_state: dict[str, int] = {}
    for r in ok:
        by_state[r.state] = by_state.get(r.state, 0) + 1
    lines += [f"| {s} | {n} |" for s, n in sorted(by_state.items())]
    with_free = sum(1 for r in ok if r.sheet and r.sheet.eligibility.free_care_max_fpl)
    with_tiers = sum(1 for r in ok if r.sheet and r.sheet.eligibility.discount_tiers)
    lines += [
        "",
        f"Sheets with a free-care line: {with_free}. With discount tiers: {with_tiers}.",
        "",
        "## Rejected",
        "",
    ]
    lines += [f"- {r.state} {r.ccn} {r.name}: {r.error}" for r in failed] or ["(none)"]
    lines += [
        "",
        "## Review notes",
        "",
        f"{sum(len(r.notes) for r in ok)} notes across {sum(1 for r in ok if r.notes)} sheets. Each sheet's notes are in its `eve_review` list in the state file.",
    ]
    path = out_dir / "_import-report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
