"""Case lifecycle: start, read bill, confirm, household, evaluate, approve, delete (spec §9)."""

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from waive.ai.client import AIClient
from waive.atlas import repo
from waive.atlas.schema import DocType
from waive.cases.extract import BillExtract, extract_bill, extract_income
from waive.cases.images import prepare_image
from waive.cases.match import MatchCandidate, is_confident, match_hospital
from waive.cases.vault import FieldCipher, Scope, TokenSigner
from waive.db import CaseRow
from waive.rules.deadlines import Deadlines, deadlines_for
from waive.rules.eligibility import EligibilityResult, Household, Tier, evaluate_eligibility
from waive.rules.explain import caregiver_summary, senior_message

SENIOR_TTL = timedelta(days=90)
CAREGIVER_TTL = timedelta(days=365)
DEFAULT_DOCUMENTS = [DocType.PHOTO_ID, DocType.PROOF_OF_INCOME]
# Paid reads one case may ask for per UTC day, by photo kind. A real household never needs more,
# and one senior link must not be a free meter on the vision model (spec §11).
READ_LIMITS = {"bill": 5, "income": 5, "paper": 10}


class TooManyReads(RuntimeError):
    """A case asked the paid reader more often today than READ_LIMITS allows."""


def count_read(sealed: dict[str, Any], kind: str, today: date) -> None:
    """Record one paid read of `kind` for today in the sealed blob, or refuse it. Callers do this
    before the model is asked and save the blob at once, so a failed read still counts."""
    reads = sealed.setdefault("reads", {})
    day = today.isoformat()
    today_reads = [stamp for stamp in reads.get(kind, []) if stamp == day]
    if len(today_reads) >= READ_LIMITS[kind]:
        raise TooManyReads(f"{READ_LIMITS[kind]} {kind} photos were already read today")
    reads[kind] = [*today_reads, day]


@dataclass
class CaseContext:
    session: Session
    ai: AIClient
    cipher: FieldCipher
    signer: TokenSigner
    today: date


@dataclass(frozen=True)
class CaseLinks:
    case_id: str
    senior_token: str
    caregiver_token: str


@dataclass
class CaseView:
    case_id: str
    status: str
    hospital_name: str | None = None
    ccn: str | None = None
    candidates: list[MatchCandidate] = field(default_factory=list)
    bill: BillExtract | None = None
    household_size: int | None = None
    annual_income: Decimal | None = None
    tier: Tier | None = None
    senior_text: str = ""
    caregiver_text: str = ""
    deadlines: Deadlines | None = None
    missing: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    needs_scouting: bool = False
    result: EligibilityResult | None = None


def _load(ctx: CaseContext, row: CaseRow) -> dict[str, Any]:
    return ctx.cipher.decrypt(row.sealed) if row.sealed else {}


def _save(ctx: CaseContext, row: CaseRow, sealed: dict[str, Any]) -> None:
    row.sealed = ctx.cipher.encrypt(sealed)
    ctx.session.flush()


def _row(ctx: CaseContext, case_id: str) -> CaseRow:
    row = ctx.session.get(CaseRow, case_id)
    if row is None:
        raise KeyError(case_id)
    return row


def _unlocked(row: CaseRow, allow_approved: bool) -> None:
    """An approved case is the caregiver's signed deliverable (spec §9 step 9). The senior link
    only adds photos and sees the result, so a change after approval needs the caregiver's own
    correction form (`allow_approved`); papers from the hospital are not affected."""
    if row.status == "approved" and not allow_approved:
        raise PermissionError("this case is approved; only the caregiver can change it")


def start_case(ctx: CaseContext, state: str) -> CaseLinks:
    row = CaseRow(id=uuid.uuid4().hex, state=state.upper(), status="new", token_generation=1)
    ctx.session.add(row)
    ctx.session.flush()
    return CaseLinks(
        row.id,
        ctx.signer.mint(row.id, "senior", 1, SENIOR_TTL),
        ctx.signer.mint(row.id, "caregiver", 1, CAREGIVER_TTL),
    )


def authorize(ctx: CaseContext, token: str, required: Scope) -> CaseRow:
    claims = ctx.signer.verify(token)
    if claims is None:
        raise PermissionError("link is invalid or expired")
    row = ctx.session.get(CaseRow, claims.case_id)
    if row is None or row.token_generation != claims.generation:
        raise PermissionError("link was revoked")
    if required == "caregiver" and claims.scope != "caregiver":
        raise PermissionError("this link cannot approve or delete")
    return row


def _fpl_band(percent: Decimal | None) -> str | None:
    if percent is None:
        return None
    for limit, label in ((100, "<=100"), (200, "101-200"), (300, "201-300"), (400, "301-400")):
        if percent <= limit:
            return label
    return ">400"


def deadline_anchor(bill: BillExtract) -> tuple[date, bool]:
    """(the date the 501(r) clocks start from, whether it is known to be the first bill's).
    A first-bill date entered by the caregiver wins; otherwise the photographed statement's date,
    confirmed only when the reader marked it as the first statement and it carries no collection
    language (a final notice is almost never the first bill)."""
    if bill.first_statement_date:
        return bill.first_statement_date, True
    assert bill.statement_date is not None
    return bill.statement_date, bill.is_first_statement is True and not bill.collection_notice


def _evaluate(ctx: CaseContext, row: CaseRow, sealed: dict[str, Any]) -> CaseView:
    bill = BillExtract.model_validate(sealed["bill"]) if "bill" in sealed else None
    household = sealed.get("household") or {}
    shown = CaseView(
        case_id=row.id,
        status=row.status,
        ccn=row.ccn,
        bill=bill,
        candidates=[MatchCandidate(**c) for c in sealed.get("candidates", [])],
        household_size=household.get("size"),
        annual_income=Decimal(household["annual_income"])
        if household.get("annual_income")
        else None,
        warnings=tuple(sealed.get("image_warnings", [])),
        needs_scouting=bool(sealed.get("needs_scouting")),
    )
    if row.ccn:
        hospital = repo.get_hospital(ctx.session, row.ccn)
        shown.hospital_name = hospital.name if hospital else None
    if row.ccn is None or row.status in {"new", "bill_read"}:
        return shown
    found = repo.latest_sheet(ctx.session, row.ccn)
    if found is None:
        shown.needs_scouting = True
        return shown
    sheet, _ = found
    result = evaluate_eligibility(
        sheet,
        Household(
            size=int(household.get("size") or 1),
            annual_income=shown.annual_income,
            state=row.state,
            programs=tuple(household.get("programs", [])),
        ),
    )
    shown.result, shown.tier, shown.missing = result, result.tier, result.missing
    name = shown.hospital_name or sheet.hospital.name
    shown.senior_text = senior_message(result, name)
    shown.caregiver_text = caregiver_summary(result, name)
    if bill and (bill.first_statement_date or bill.statement_date):
        anchor, confirmed = deadline_anchor(bill)
        shown.deadlines = deadlines_for(
            sheet, anchor, ctx.today, bill.collection_notice_date, anchor_confirmed=confirmed
        )
    if result.tier in {Tier.FREE, Tier.DISCOUNT, Tier.NOT_ELIGIBLE} and row.prediction is None:
        documents = (
            sheet.apply.documents_required.value
            if sheet.apply.documents_required
            else DEFAULT_DOCUMENTS
        )
        row.prediction = {
            "sheet_version": sheet.version,
            "tier": result.tier.value,
            "fpl_band": _fpl_band(result.fpl_percent),
            "predicted_documents": [doc.value for doc in documents],
            "created_on": ctx.today.isoformat(),
        }
        row.status = "evaluated"
        shown.status = row.status
        ctx.session.flush()
    return shown


def view(ctx: CaseContext, case_id: str) -> CaseView:
    row = _row(ctx, case_id)
    return _evaluate(ctx, row, _load(ctx, row))


def submit_bill(
    ctx: CaseContext,
    case_id: str,
    image_bytes: bytes,
    *,
    synthetic: bool = False,
    allow_approved: bool = False,
) -> CaseView:
    row = _row(ctx, case_id)
    _unlocked(row, allow_approved)
    prepared = prepare_image(image_bytes)
    sealed = _load(ctx, row)
    count_read(sealed, "bill", ctx.today)
    _save(ctx, row, sealed)
    extract = extract_bill(ctx.ai, prepared.jpeg, synthetic=synthetic)
    hospitals = [repo.hospital_ref(h) for h in repo.list_hospitals(ctx.session)]
    candidates = match_hospital(extract, hospitals)
    sealed["bill"] = extract.model_dump(mode="json")
    sealed["candidates"] = [c.__dict__ for c in candidates]
    sealed["image_warnings"] = list(prepared.warnings)
    if candidates and is_confident(candidates):
        row.ccn = candidates[0].ccn
        sealed["needs_scouting"] = False
    else:
        row.ccn = None
        sealed["needs_scouting"] = True
        repo.add_review_item(
            ctx.session,
            None,
            "scout_request",
            {
                "hospital_name": extract.hospital_name,
                "fap_url": extract.fap_url,
                "state": row.state,
            },
        )
    row.status = "bill_read"
    _save(ctx, row, sealed)
    return _evaluate(ctx, row, sealed)


def confirm_bill(
    ctx: CaseContext,
    case_id: str,
    corrections: dict[str, Any],
    ccn: str | None = None,
    *,
    allow_approved: bool = False,
) -> CaseView:
    row = _row(ctx, case_id)
    _unlocked(row, allow_approved)
    sealed = _load(ctx, row)
    bill = BillExtract.model_validate({**sealed.get("bill", {}), **corrections})
    if corrections.get("is_first_statement"):
        # The caregiver says the photographed statement is the first bill: pin the clocks to it.
        bill = bill.model_copy(update={"first_statement_date": bill.statement_date})
    sealed["bill"] = bill.model_dump(mode="json")
    if ccn:
        if repo.get_hospital(ctx.session, ccn) is None:
            raise KeyError(ccn)  # only registry hospitals: a case never points at a made-up CCN
        row.ccn = ccn
        sealed["needs_scouting"] = False
    row.status = "confirmed"
    _save(ctx, row, sealed)
    return _evaluate(ctx, row, sealed)


def set_household(
    ctx: CaseContext,
    case_id: str,
    size: int,
    annual_income: Decimal | None,
    programs: tuple[str, ...],
    *,
    allow_approved: bool = False,
) -> CaseView:
    row = _row(ctx, case_id)
    _unlocked(row, allow_approved)
    sealed = _load(ctx, row)
    sealed["household"] = {
        "size": size,
        "annual_income": None if annual_income is None else str(annual_income),
        "programs": list(programs),
    }
    if row.status in {"new", "bill_read"}:
        row.status = "confirmed"
    _save(ctx, row, sealed)
    return _evaluate(ctx, row, sealed)


def submit_income_letter(
    ctx: CaseContext,
    case_id: str,
    image_bytes: bytes,
    *,
    synthetic: bool = False,
    allow_approved: bool = False,
) -> CaseView:
    row = _row(ctx, case_id)
    _unlocked(row, allow_approved)
    prepared = prepare_image(image_bytes)
    sealed = _load(ctx, row)
    count_read(sealed, "income", ctx.today)
    _save(ctx, row, sealed)
    income = extract_income(ctx.ai, prepared.jpeg, synthetic=synthetic)
    household = sealed.get("household") or {"size": 1, "programs": []}
    annual = income.annual()
    household["annual_income"] = None if annual is None else str(annual)
    sealed["household"] = household
    _save(ctx, row, sealed)
    return _evaluate(ctx, row, sealed)


def approve(ctx: CaseContext, case_id: str) -> CaseView:
    row = _row(ctx, case_id)
    row.status = "approved"
    ctx.session.flush()
    return view(ctx, case_id)


def delete_case(ctx: CaseContext, case_id: str) -> None:
    row = _row(ctx, case_id)
    ctx.session.delete(row)
    ctx.session.flush()


# Public access to the sealed blob for the learning loop (spec §10). The blob stays encrypted at
# rest; callers must keep personal values out of clear columns and logs.
get_row = _row
load_sealed = _load
save_sealed = _save
