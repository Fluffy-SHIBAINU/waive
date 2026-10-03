"""Plain-language rendering of cited atlas values for the public sheet page.

The schema stores facts as Decimals, enums and small models. A senior reading the page should
see "250% of the federal poverty level" and "Photo ID", never Decimal('250') or a list repr.
Registered as the Jinja filter `plain` in `waive.web.app`; pure, so the unit tests call it
directly."""

from decimal import Decimal
from typing import Any

from waive.atlas.schema import DiscountTier, DocType, StateProgram, SubmitMethod

DOC_LABELS: dict[DocType, str] = {
    DocType.PHOTO_ID: "Photo ID",
    DocType.PROOF_OF_INCOME: "Proof of income",
    DocType.SOCIAL_SECURITY_LETTER: "Social Security letter",
    DocType.TAX_RETURN: "Tax return",
    DocType.PAY_STUBS: "Pay stubs",
    DocType.BANK_STATEMENTS: "Bank statements",
    DocType.PROOF_OF_RESIDENCY: "Proof of residency",
    DocType.INSURANCE_CARD: "Insurance card",
    DocType.MEDICAID_DENIAL: "Medicaid denial letter",
    DocType.OTHER: "Other documents",
}


def plain_lines(value: Any, path: str = "") -> list[str]:
    """Short lines of plain English for one cited value. `path` is the sheet field
    ("eligibility.free_care_max_fpl") and decides how a bare number reads: a percent of the
    poverty level, dollars, or a count of days. Lists give one line per item, repeats dropped."""
    if value is None:
        return []
    if isinstance(value, list):
        lines: list[str] = []
        for item in value:
            for line in plain_lines(item, path):
                if line not in lines:
                    lines.append(line)
        return lines
    return [_plain(value, path.rsplit(".", 1)[-1])]


def _plain(value: Any, field: str) -> str:
    # bool before int (a bool is an int); DocType before str (a StrEnum is a str).
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, DocType):
        return DOC_LABELS.get(value) or _humanise(value.value)
    if isinstance(value, str):
        return value
    if isinstance(value, Decimal):
        if field.endswith("_fpl"):
            return f"{_number(value)}% of the federal poverty level"
        return f"${value:,.2f}"
    if isinstance(value, int):
        if "days" in field:
            return "1 day" if value == 1 else f"{value} days"
        return str(value)
    if isinstance(value, DiscountTier):
        low, high = _number(value.min_fpl_exclusive), _number(value.max_fpl_inclusive)
        # A band starting at zero income is "Up to 400%", not "Above 0% up to 400%".
        band = f"Up to {high}%" if value.min_fpl_exclusive == 0 else f"Above {low}% up to {high}%"
        return f"{band} of FPL: {value.discount_percent}% discount"
    if isinstance(value, StateProgram):
        return f"{value.name} — {value.how_to_apply}"
    if isinstance(value, SubmitMethod):
        return f"{_humanise(value.kind)}: {value.detail}"
    return str(value)


def _number(value: Decimal) -> str:
    """250 and 137.5, never 250.00 or 2.5E+2."""
    return format(value.normalize(), "f")


def _humanise(token: str) -> str:
    return token.replace("_", " ").capitalize()
