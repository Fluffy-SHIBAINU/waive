"""One targeted, skippable question per case when the hospital's sheet has gaps (spec §10)."""

from dataclasses import dataclass
from typing import Literal

from waive.atlas import repo
from waive.atlas.schema import ProcedureSheet
from waive.cases.service import CaseContext, get_row, load_sealed, save_sealed
from waive.learning.classify import PhotoClass

COMPLETENESS_FLOOR = 0.5
# What a person needs in order to apply; a sheet missing any of these earns one question.
GAP_FIELDS = ("eligibility.free_care_max_fpl", "apply.documents_required", "apply.submit_methods")
GapAnswer = Literal["photo", "skipped"]


@dataclass(frozen=True)
class GapAsk:
    question: str
    wanted: tuple[PhotoClass, ...]
    reason: str


NO_SHEET = GapAsk(
    "Did the hospital give you any papers about help paying the bill? Take a photo of them.",
    (PhotoClass.FAP, PhotoClass.PLAIN_LANGUAGE_SUMMARY, PhotoClass.APPLICATION_FORM),
    "no_sheet",
)
INCOMPLETE = GapAsk(
    "Did the hospital give you a paper that explains who can get help? Take a photo of it.",
    (PhotoClass.FAP, PhotoClass.PLAIN_LANGUAGE_SUMMARY),
    "incomplete",
)
MISSING_FORM = GapAsk(
    "Did the hospital give you a form to apply for help? Take a photo of the blank form.",
    (PhotoClass.APPLICATION_FORM,),
    "missing_apply_fields",
)


def gap_ask(sheet: ProcedureSheet | None) -> GapAsk | None:
    if sheet is None:
        return NO_SHEET
    present = {path for path, _ in sheet.field_paths()}
    if sheet.completeness() < COMPLETENESS_FLOOR or "eligibility.free_care_max_fpl" not in present:
        return INCOMPLETE
    if any(path not in present for path in GAP_FIELDS):
        return MISSING_FORM
    return None


def pending_gap_ask(ctx: CaseContext, case_id: str) -> GapAsk | None:
    row = get_row(ctx, case_id)
    if row.status in {"new", "bill_read"} or "gap_ask" in load_sealed(ctx, row):
        return None
    found = repo.latest_sheet(ctx.session, row.ccn) if row.ccn else None
    return gap_ask(found[0] if found else None)


def answer_gap_ask(ctx: CaseContext, case_id: str, answer: GapAnswer) -> None:
    row = get_row(ctx, case_id)
    sealed = load_sealed(ctx, row)
    sealed["gap_ask"] = {"answer": answer, "on": ctx.today.isoformat()}
    save_sealed(ctx, row, sealed)
