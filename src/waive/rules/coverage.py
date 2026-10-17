"""Two checks before the hospital's income rules (task 9.12, research 2026-10-16 pains 5, 12, 13):
does federal law forbid this bill at all, and does the hospital's policy cover this payee."""

import re
from dataclasses import dataclass
from enum import StrEnum

from rapidfuzz import fuzz

from waive.atlas.schema import ProcedureSheet
from waive.cases.extract import BillExtract, PayeeKind
from waive.rules.eligibility import EligibilityResult, Reason, Tier

# State names for Medicaid where the card does not say "Medicaid". Everything else: Medicaid.
MEDICAID_NAMES = {
    "MA": "MassHealth",
    "CA": "Medi-Cal",
    "NJ": "NJ FamilyCare",
    "PA": "Medical Assistance",
}


def medicaid_name(state: str) -> str:
    return MEDICAID_NAMES.get(state.upper(), "Medicaid")


# What a senior may answer, lower-cased, that puts the bill under the cost-sharing ban. SNAP is
# not here: it is a presumptive-eligibility program for some FAPs, not a billing protection.
COST_SHARING_PROTECTED = frozenset(
    {"qmb", "medicaid", *(name.lower() for name in MEDICAID_NAMES.values())}
)


def cost_sharing_protected(programs: tuple[str, ...]) -> bool:
    return any(program.lower() in COST_SHARING_PROTECTED for program in programs)


@dataclass(frozen=True)
class CostSharingRule:
    tier: Tier
    text: str
    quote: str


COST_SHARING_RULE = CostSharingRule(
    tier=Tier.NOT_OWED,
    text=(
        "If this balance is Medicare or Medicaid cost-sharing for a covered visit, federal law "
        "does not allow billing it to someone enrolled in the Qualified Medicare Beneficiary "
        "program or Medicaid: the provider must accept the Medicare and Medicaid payments as "
        "payment in full."
    ),
    quote=(
        "Social Security Act §1902(n)(3)(B), 42 U.S.C. 1396a(n)(3)(B) (QMB balance-billing "
        "prohibition); 42 CFR 447.15 (Medicaid payment is payment in full); CMS and CFPB joint "
        "statement of October 4, 2024 on improper billing of QMB beneficiaries."
    ),
)


def cost_sharing_result() -> EligibilityResult:
    rule = COST_SHARING_RULE
    reason = Reason(
        text=rule.text, field_path="federal.cost_sharing", quote=rule.quote, source_id=None
    )
    return EligibilityResult(rule.tier, discount_percent=100, reasons=(reason,))


class Route(StrEnum):
    COVERED = "covered"
    # Ask the payee for its own financial assistance.
    PAYEE_REQUEST = "payee_request"
    # Ambulance: a Medicare appeal through the State Health Insurance Assistance Program.
    SHIP = "ship"


@dataclass(frozen=True)
class PayeeRoute:
    kind: Route
    # The answer when the policy does not cover the bill; None when it does.
    result: EligibilityResult | None = None
    # Why a non-hospital payee counts as covered: the sheet's covered-provider list.
    reason: Reason | None = None


OUTSIDE_FAP = frozenset(
    {
        PayeeKind.PHYSICIAN_GROUP,
        PayeeKind.ANESTHESIA_RADIOLOGY_PATHOLOGY,
        PayeeKind.AMBULANCE,
        PayeeKind.OTHER,
    }
)
COVERED_PATH = "coverage.covered_providers"
MATCH_THRESHOLD = 85
# Corporate suffixes and punctuation differ between a bill and a policy list; the name does not.
_SUFFIXES = re.compile(
    r"\b(?:p\.?a\.?|p\.?c\.?|l\.?l\.?c\.?|l\.?l\.?p\.?|inc\.?|ltd\.?|corp\.?|co\.?)\b"
)
_NOISE = re.compile(r"[^a-z0-9 ]+")


def _name_key(name: str) -> str:
    text = _SUFFIXES.sub(" ", name.lower())
    return " ".join(_NOISE.sub(" ", text).split())


def _listed(sheet: ProcedureSheet, payee_name: str | None) -> Reason | None:
    """The covered-provider entry the payee's name matches, as a cited reason. Whole-name
    similarity, never token containment: "Mercy" must not match "Mercy Ambulance Service"
    (review of 9.12)."""
    cited = sheet.coverage.covered_providers
    if cited is None or not payee_name:
        return None
    key = _name_key(payee_name)
    for listed in cited.value:
        if key and fuzz.ratio(_name_key(listed), key) >= MATCH_THRESHOLD:
            text = f"{payee_name} is on the policy's list of covered providers ({listed})."
            return Reason(text, COVERED_PATH, cited.quote, cited.source_id)
    return None


def payee_route(sheet: ProcedureSheet, bill: BillExtract) -> PayeeRoute | None:
    """None when the bill is the hospital's own (or the payee was not read). Otherwise where it
    goes: covered by name, a request to the payee, or SHIP for an ambulance."""
    if bill.payee_kind not in OUTSIDE_FAP:
        return None
    listed = _listed(sheet, bill.payee_name)
    if listed is not None:
        return PayeeRoute(Route.COVERED, reason=listed)
    # Hedged: the payee kind is read from a photo, and most sheets carry no provider list yet.
    who = bill.payee_name or "the group that sent it"
    if bill.payee_kind is PayeeKind.AMBULANCE:
        text = (
            f"This bill is from an ambulance service ({who}), not the hospital, so the hospital's "
            "financial assistance policy likely does not cover it."
        )
        kind = Route.SHIP
    else:
        text = (
            f"This bill is from {who}, not the hospital, so the hospital's financial assistance "
            "policy likely does not cover it; its covered-provider list is the final word."
        )
        kind = Route.PAYEE_REQUEST
    reason = Reason(text, "bill.payee_kind", None, None)
    return PayeeRoute(kind, result=EligibilityResult(Tier.NOT_COVERED, reasons=(reason,)))
