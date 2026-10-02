"""Exact-quote verification for procedure sheet fields (spec section 8, step 5)."""

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from waive.atlas.schema import DiscountTier, Layer, ProcedureSheet

MIN_QUOTE_CHARS = 12

_REPLACEMENTS = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "−": "-",
    "­": "",
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    for old, new in _REPLACEMENTS.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip().lower()


def _squash(text: str) -> str:
    """Hyphen- and space-insensitive form, for PDFs that split words across lines."""
    return re.sub(r"[\s\-]+", "", normalize(text))


def quote_found(quote: str, document_text: str) -> bool:
    normalized_quote = normalize(quote)
    if len(normalized_quote) < MIN_QUOTE_CHARS:
        return False
    if normalized_quote in normalize(document_text):
        return True
    return _squash(quote) in _squash(document_text)


def _number_in(number: Decimal, quote: str) -> bool:
    digits = format(number.normalize(), "f")
    haystack = normalize(quote).replace(",", "")
    return re.search(rf"(?<![\d.]){re.escape(digits)}(?!\d)", haystack) is not None


def value_in_quote(value: Any, quote: str) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int | Decimal):
        return _number_in(Decimal(value), quote)
    if isinstance(value, list) and value and all(isinstance(v, DiscountTier) for v in value):
        return all(
            _number_in(tier.min_fpl_exclusive, quote)
            and _number_in(tier.max_fpl_inclusive, quote)
            and _number_in(Decimal(tier.discount_percent), quote)
            for tier in value
        )
    return True


@dataclass
class VerificationReport:
    accepted: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejected


def verify_sheet(sheet: ProcedureSheet, documents: dict[str, str]) -> VerificationReport:
    report = VerificationReport()
    for path, cited in sheet.field_paths():
        if cited.layer is Layer.REPORTED:
            report.accepted.append(path)
            continue
        text = documents.get(cited.source_id or "")
        if text is None:
            report.rejected.append((path, "source text missing"))
        elif not quote_found(cited.quote or "", text):
            report.rejected.append((path, "quote not found in source"))
        elif not value_in_quote(cited.value, cited.quote or ""):
            report.rejected.append((path, "value not in quote"))
        else:
            report.accepted.append(path)
    return report
