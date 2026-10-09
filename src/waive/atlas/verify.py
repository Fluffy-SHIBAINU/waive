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


_WORD_CHAR = re.compile(r"[^\W_]")  # a letter or digit in any script
_SQUASHED = re.compile(r"[\s\-]")


def _at_word_start(text: str, index: int) -> bool:
    return index == 0 or _WORD_CHAR.match(text, index - 1) is None


def _occurs_at_word_start(
    needle: str, haystack: str, text: str, offsets: list[int] | None = None
) -> bool:
    """Whether `needle` occurs in `haystack` where a word starts in `text`; `offsets` maps each
    index of a squashed `haystack` back to `text` (without it the two are the same string)."""
    start = haystack.find(needle)
    while start != -1:
        if _at_word_start(text, offsets[start] if offsets else start):
            return True
        start = haystack.find(needle, start + 1)
    return False


def quote_found(quote: str, document_text: str) -> bool:
    """Whether the document contains the quote, starting at a word boundary: "phone: 508-334-9300"
    is not in "telephone: 508-334-9300" (UMass Memorial, 220163), although it is a substring."""
    needle = normalize(quote)
    if len(needle) < MIN_QUOTE_CHARS:
        return False
    haystack = normalize(document_text)
    if _occurs_at_word_start(needle, haystack, haystack):
        return True
    # Hyphen- and space-insensitive form, for PDFs that split words across lines. Each squashed
    # character keeps its index in the normalized text, where the word boundary is judged.
    squashed_needle = _SQUASHED.sub("", needle)
    squashed_haystack = _SQUASHED.sub("", haystack)
    if not squashed_needle or squashed_needle not in squashed_haystack:
        return False
    offsets = [index for index, char in enumerate(haystack) if _SQUASHED.match(char) is None]
    return _occurs_at_word_start(squashed_needle, squashed_haystack, haystack, offsets)


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


AGB_CAP_REASON = "quote states the amounts-generally-billed cap, not who may apply"
_AGB_CLAUSE = re.compile(r"amounts? generally billed[^.;]*")


def quotes_only_the_agb_cap(quote: str) -> bool:
    """A 501(r) cap sentence ("you will not be billed more than the amount generally billed to
    patients with insurance coverage") whose only mention of insurance is that clause. It prices
    care for whoever qualifies and says nothing about whether insured patients do; UMass Memorial
    (220163) read it as true in one run and false in the next."""
    text = normalize(quote)
    if _AGB_CLAUSE.search(text) is None:
        return False
    return "insur" not in _AGB_CLAUSE.sub("", text)


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
        elif path == "eligibility.insured_patients_covered" and quotes_only_the_agb_cap(
            cited.quote or ""
        ):
            report.rejected.append((path, AGB_CAP_REASON))
        else:
            report.accepted.append(path)
    return report
