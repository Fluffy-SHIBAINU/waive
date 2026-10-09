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


_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _MARKDOWN_LINK.sub(r"\1", text).replace("**", "").replace("__", "")
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


def looks_serialised(value: Any) -> bool:
    """True for a text value that is really a list or object written out, such as
    "[{'kind': 'main', 'number': '978-463-1123'}]", or a list of strings with such an item: a
    shape the model got wrong and the structurer stringified. Quotes do not catch it (strings are
    never checked against the quote), and the sheet page would print the brackets."""
    if isinstance(value, str):
        return value.lstrip().startswith(("[", "{"))
    if isinstance(value, list):
        return any(isinstance(item, str) and looks_serialised(item) for item in value)
    return False


PATIENT_SHARE_REASON = "quote states the patient's share, not a discount"
# Word-bounded on purpose: "Lowell" contains "owe" and "Inpatient Discount" contains "patient".
_PATIENT_SHARE = re.compile(
    r"\b(?:patient )?responsib\w*|\bpatients? (?:pays?|portion|share)\b|\bco-?pay\w*"
    r"|\bof (?:total )?charges\b|\bbalance due\b"
)
_DISCOUNT_WORDS = re.compile(r"\b(?:discount\w*|write[- ]?offs?|reduc\w*|free|waiv\w*|adjust\w*)\b")


def quotes_patient_share(quote: str) -> bool:
    """A sliding-scale quote that describes what the patient pays (co-pay, X% of charges,
    patient responsibility) and never mentions a discount or write-off. Its numbers are the
    patient's share, so a DiscountTier built from them would be upside down (Mercy, 15% vs 85%)."""
    text = normalize(quote)
    return _PATIENT_SHARE.search(text) is not None and _DISCOUNT_WORDS.search(text) is None


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


def matched_span(quote: str, document_text: str) -> str | None:
    """The quote itself if the source contains it; otherwise its longest sentence that does."""
    if quote_found(quote, document_text):
        return quote.strip()
    sentences = [s.strip() for s in re.split(r"(?<=[.;:])\s+|\n+", quote) if s.strip()]
    candidates = [
        s for s in sorted(sentences, key=len, reverse=True) if quote_found(s, document_text)
    ]
    return candidates[0] if candidates else None


def trim_quotes(sheet: ProcedureSheet, documents: dict[str, str]) -> ProcedureSheet:
    """Replace each documented quote with the span of it that the source actually contains.

    Models sometimes stitch two passages or paraphrase an edge; keeping the verified sentence
    preserves grounding and lets verify_sheet apply its exact-quote rule to real text."""
    updates: dict[str, Any] = {}
    for path, cited in sheet.field_paths():
        if cited.layer is Layer.REPORTED or not cited.quote:
            continue
        text = documents.get(cited.source_id or "")
        if text is None:
            continue
        span = matched_span(cited.quote, text)
        if span is None or span == cited.quote:
            continue
        section_name, field_name = path.split(".")
        section = updates.get(section_name, getattr(sheet, section_name))
        updates[section_name] = section.model_copy(
            update={field_name: cited.model_copy(update={"quote": span})}
        )
    return sheet.model_copy(update=updates) if updates else sheet


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
        elif looks_serialised(cited.value):
            report.rejected.append((path, "value is a list or object written as text"))
        elif not value_in_quote(cited.value, cited.quote or ""):
            report.rejected.append((path, "value not in quote"))
        elif path == "eligibility.discount_tiers" and quotes_patient_share(cited.quote or ""):
            report.rejected.append((path, PATIENT_SHARE_REASON))
        else:
            report.accepted.append(path)
    return report
