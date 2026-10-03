"""Compare a case's prediction with the hospital's real decision (spec §10, deterministic rules)."""

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.schema import DocType, ProcedureSheet
from waive.cases.service import CaseContext, get_row
from waive.db import CaseRow, ReviewItemRow
from waive.learning.evidence import DOCUMENTS_PATH, SLIP_PATH, add_evidence, raise_flags
from waive.learning.hashing import case_hash
from waive.learning.outcomes import (
    Decision,
    DenialReason,
    OutcomeExtract,
    load_outcome,
    save_outcome,
)


class Triage(StrEnum):
    MATCHED = "matched"
    CASE_ISSUE = "case_issue"
    SHEET_MISSING = "sheet_missing"
    SHEET_WRONG = "sheet_wrong"
    HOSPITAL_SLIP = "hospital_slip"
    NO_PREDICTION = "no_prediction"


# Reasons that point at the application, not at the sheet: help the person resubmit (spec §10).
CASE_ISSUE_REASONS = frozenset(
    {
        DenialReason.INCOMPLETE,
        DenialReason.LATE,
        DenialReason.UNSIGNED,
        DenialReason.INCOME_TOO_HIGH,
        DenialReason.MISSING_DOCUMENTS,
        DenialReason.ASSETS_TOO_HIGH,
    }
)

HELP_TEXT: dict[DenialReason, str] = {
    DenialReason.INCOMPLETE: (
        "The hospital says the application was incomplete. Fill in every line, sign it, and send "
        "it again with the documents listed."
    ),
    DenialReason.LATE: (
        "The hospital says the application came too late. Ask in writing for an exception and "
        "mention the date of the first bill; the packet's deadline page helps."
    ),
    DenialReason.UNSIGNED: "The hospital says the application was not signed. Sign it and send it again.",
    DenialReason.INCOME_TOO_HIGH: (
        "The hospital counted a higher income than we used. Check the income on the application "
        "against the benefit letter and send proof."
    ),
    DenialReason.MISSING_DOCUMENTS: (
        "The hospital needs documents that were not included. Send the documents it listed."
    ),
    DenialReason.ASSETS_TOO_HIGH: (
        "The hospital counted savings or other assets. Ask what it counted and whether an "
        "exception exists."
    ),
}

APPEAL_TEXT = (
    "Appeal draft. To the Financial Assistance Office of {hospital}: on {decision_date} you denied "
    "or reduced the attached application. Your published financial assistance policy (version "
    '{version}, checked {checked_on}) states: "{quote}". Our household income is within that '
    "limit (about {fpl_band}% of the federal poverty guideline for our household size). Please "
    "review the application again under your policy and send a written answer."
)


@dataclass(frozen=True)
class TriageResult:
    kind: Triage
    matched: bool | None
    new_documents: tuple[DocType, ...] = ()
    slip_value: str | None = None
    rescout: bool = False
    help_text: str | None = None
    appeal_text: str | None = None


def triage(
    prediction: dict[str, Any], outcome: OutcomeExtract, latest_version: int
) -> TriageResult:
    """Deterministic comparison of what the sheet predicted with what the hospital did.
    `matched` is None when the outcome does not test the prediction (more information asked,
    or the application itself was at fault)."""
    predicted = prediction["tier"]
    expected = set(prediction.get("predicted_documents", []))
    new_docs = tuple(
        doc
        for doc in outcome.documents_requested
        if doc is not DocType.OTHER and doc.value not in expected
    )
    reasons = set(outcome.reasons)
    sheet_changed = latest_version > int(prediction["sheet_version"])
    decision = outcome.decision

    if decision is Decision.MORE_INFO:
        kind = Triage.SHEET_MISSING if new_docs else Triage.MATCHED
        return TriageResult(kind, matched=None, new_documents=new_docs)
    if predicted == "not_eligible":
        if decision is Decision.DENIED:
            return TriageResult(Triage.MATCHED, matched=True)
        # The hospital helped someone the sheet said it would not: the sheet is too strict.
        return TriageResult(Triage.SHEET_WRONG, matched=False, rescout=not sheet_changed)
    if decision is Decision.APPROVED or (decision is Decision.PARTIAL and predicted == "discount"):
        return TriageResult(Triage.MATCHED, matched=True, new_documents=new_docs)
    if decision is Decision.DENIED and reasons and reasons <= CASE_ISSUE_REASONS:
        ordered = sorted(reasons, key=list(DenialReason).index)
        help_text = " ".join(HELP_TEXT[reason] for reason in ordered)
        return TriageResult(
            Triage.CASE_ISSUE, matched=None, new_documents=new_docs, help_text=help_text
        )
    if sheet_changed:
        return TriageResult(Triage.SHEET_WRONG, matched=False, new_documents=new_docs)
    slip_value = "partial_despite_free" if decision is Decision.PARTIAL else "denied_despite_policy"
    return TriageResult(
        Triage.HOSPITAL_SLIP,
        matched=False,
        new_documents=new_docs,
        slip_value=slip_value,
        rescout=True,
    )


def appeal_draft(sheet: ProcedureSheet, prediction: dict[str, Any], outcome: OutcomeExtract) -> str:
    eligibility = sheet.eligibility
    cited = (
        eligibility.free_care_max_fpl
        if prediction["tier"] == "free"
        else eligibility.discount_tiers
    )
    if cited is None:
        cited = eligibility.free_care_max_fpl or eligibility.discount_tiers
    quote = cited.quote if cited is not None and cited.quote else "the published income limits"
    checked_on = cited.checked_on.isoformat() if cited is not None else "recently"
    return APPEAL_TEXT.format(
        hospital=sheet.hospital.name,
        decision_date=outcome.decision_date.isoformat() if outcome.decision_date else "recently",
        version=sheet.version,
        checked_on=checked_on,
        quote=quote,
        fpl_band=prediction.get("fpl_band") or "an eligible",
    )


def request_rescout(session: Session, ccn: str, case_id: str) -> ReviewItemRow:
    """One open re-scout request per hospital; each contradicting case is counted by its hash.
    It queues work for an admin (and, from Phase 7, the scheduler); no Tavily credit is spent."""
    digest = case_hash(case_id)
    for item in repo.open_review_items(session, ccn):
        if item.kind == "rescout_request":
            cases = list(item.detail.get("cases", []))
            if digest not in cases:
                cases.append(digest)
                item.detail = {**item.detail, "cases": cases, "count": len(cases)}
                session.flush()
            return item
    detail = {"reason": "an outcome contradicts the sheet", "cases": [digest], "count": 1}
    return repo.add_review_item(session, ccn, "rescout_request", detail)


def _latest(ctx: CaseContext, row: CaseRow) -> tuple[ProcedureSheet | None, int]:
    found = repo.latest_sheet(ctx.session, row.ccn) if row.ccn else None
    sheet = found[0] if found else None
    return sheet, sheet.version if sheet else int(row.prediction["sheet_version"])


def record_outcome(ctx: CaseContext, case_id: str, outcome: OutcomeExtract) -> TriageResult:
    """Store the outcome (encrypted), compare it with the prediction and act: evidence rows for
    missing documents and slips, one re-scout request per hospital, help or appeal text back to
    the caregiver. Only the triage class and matched flag land in the clear outcome summary."""
    row = get_row(ctx, case_id)
    save_outcome(ctx, case_id, outcome)
    if row.prediction is None or row.ccn is None:
        result = TriageResult(Triage.NO_PREDICTION, matched=None)
    else:
        sheet, latest_version = _latest(ctx, row)
        result = triage(row.prediction, outcome, latest_version)
        for doc in result.new_documents:
            add_evidence(ctx.session, row.ccn, DOCUMENTS_PATH, doc.value, row.id, ctx.today)
        if result.slip_value:
            add_evidence(ctx.session, row.ccn, SLIP_PATH, result.slip_value, row.id, ctx.today)
            raise_flags(ctx.session, row.ccn)
        if result.rescout:
            request_rescout(ctx.session, row.ccn, row.id)
        if result.kind is Triage.HOSPITAL_SLIP and sheet is not None:
            result = replace(result, appeal_text=appeal_draft(sheet, row.prediction, outcome))
    row.outcome = {**(row.outcome or {}), "triage": result.kind.value, "matched": result.matched}
    ctx.session.flush()
    return result


def triage_for(ctx: CaseContext, case_id: str) -> TriageResult | None:
    """Recompute help and appeal texts for the caregiver page from the encrypted outcome.
    Nothing is stored: the texts are derived, so they never need to live in the database."""
    row = get_row(ctx, case_id)
    outcome = load_outcome(ctx, case_id)
    if outcome is None or row.prediction is None or row.ccn is None:
        return None
    sheet, latest_version = _latest(ctx, row)
    result = triage(row.prediction, outcome, latest_version)
    if result.kind is Triage.HOSPITAL_SLIP and sheet is not None:
        result = replace(result, appeal_text=appeal_draft(sheet, row.prediction, outcome))
    return result
