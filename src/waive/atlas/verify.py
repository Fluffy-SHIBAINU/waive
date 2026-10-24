"""Exact-quote verification for procedure sheet fields (spec section 8, step 5)."""

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from functools import partial
from typing import Any

from waive.atlas.schema import DiscountTier, DocType, Layer, ProcedureSheet, SubmitMethod
from waive.atlas.states import CODE_OF_NAME, STATE_CODE, STATE_NAME, code_tokens

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


SPAN_SEPARATOR = " || "


def quote_found(quote: str, document_text: str) -> bool:
    """A quote is found when it occurs in the document at a word boundary. A quote made of
    several verbatim spans joined with `" || "` (one value stated across table rows or
    sentences) is found when every span is."""
    if SPAN_SEPARATOR in quote:
        spans = [span.strip() for span in quote.split(SPAN_SEPARATOR) if span.strip()]
        return bool(spans) and all(_single_quote_found(span, document_text) for span in spans)
    return _single_quote_found(quote, document_text)


def _single_quote_found(quote: str, document_text: str) -> bool:
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


def _number_pattern(number: Decimal) -> str:
    """`number` as written, trailing zeros allowed ("$25.00" is 25, "200.00%" is 200, "63.20%" is
    63.2), never inside a longer number nor as the whole part of a decimal."""
    digits = format(number.normalize(), "f")
    zeros = r"0*" if "." in digits else r"(?:\.0+)?"
    return rf"{re.escape(digits)}{zeros}(?!\.?\d)"


def _number_in(number: Decimal, quote: str) -> bool:
    """Whether the quote states `number` itself: never inside a longer number, and never as the
    whole part of a decimal. MelroseWakefield (220070) published a 65% discount band read from the
    Tufts table's "Outpatient Discount 65.91%", Lowell General (220063) 63 from "63.28%" (7.15a)."""
    haystack = normalize(quote).replace(",", "")
    return re.search(rf"(?<![\d.]){_number_pattern(number)}", haystack) is not None


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
# "50% of the Amount Generally Billed" is what the patient pays under a 501(r) cap (Adventist
# Health's "Patient Responsibility" column, 050013 and five siblings, task 7.9).
_PATIENT_SHARE = re.compile(
    r"\b(?:patient )?responsib\w*|\bpatients? (?:pays?|portion|share)\b|\bco-?pay\w*"
    r"|\bof (?:total )?charges\b|\bbalance due\b|\bof (?:the )?(?:agb|amounts? generally billed)\b"
)
_DISCOUNT_WORDS = re.compile(r"\b(?:discount\w*|write[- ]?offs?|reduc\w*|free|waiv\w*|adjust\w*)\b")


def quotes_patient_share(quote: str) -> bool:
    """A sliding-scale quote that describes what the patient pays (co-pay, X% of charges,
    patient responsibility) and never mentions a discount or write-off. Its numbers are the
    patient's share, so a DiscountTier built from them would be upside down (Mercy, 15% vs 85%)."""
    text = normalize(quote)
    return _PATIENT_SHARE.search(text) is not None and _DISCOUNT_WORDS.search(text) is None


RANGE_REASON = "quote gives the discount as a range or a floor, not a fixed percentage"
_PERCENT = r"(\d+(?:\.\d+)?)\s*%"
# "a reduction of 55% to 75%", "a discount of at least 25%" (discount word first) ...
_RANGE_BEFORE = re.compile(
    rf"\b(?:discounts?|reductions?|reduc\w+|write[- ]?offs?)\s+(?:of\s+)?"
    # "a sliding scale discount ranging from 20% to 80%" (Mercyhealth Crystal Lake, 140308, 7.15)
    r"(?:(?:that\s+)?(?:ranging|ranges?)\s+)?(?:from\s+)?"
    rf"(?:(at least|up to|between)\s+)?{_PERCENT}(?:\s*(?:to|-|and)\s*{_PERCENT})?"
)
# ... and "a 30% to 50% discount", "up to 75% off" (discount word last).
_RANGE_AFTER = re.compile(
    rf"(?:(at least|up to)\s+)?{_PERCENT}(?:\s*(?:to|-)\s*{_PERCENT})?"
    r"\s*(?:discount|reduction|off\b|write)"
)
# The two patterns above need a percent sign after each number. A range is also written with one
# sign, or with "percent" spelled out. Never inside "$1,000" or "2023": "$1,000 - 20% discount" is
# no range from 0 to 20.
_RANGE_NUMBER = r"(?<![\d.,$])(\d+(?:\.\d+)?)"
_RANGE_SIGN = r"(?:\s*%|\s*percent\b|\s*per cent\b)"
_RANGE_NOUN = r"(?:discounts?|reductions?|reduc\w+|write[- ]?offs?|adjustments?)"
# A number right after a label or an income band starts no discount range: "tier 2 - 75%
# discount" and "section 4.2 - 50% discount" name one discount, and "0-50% fpl 51-75% discount"
# gives the next band's income (review of task 7.17).
_RANGE_NOT_AFTER = "".join(
    rf"(?<!{word} )"
    for word in (
        "level",
        "tier",
        "category",
        "step",
        "class",
        "group",
        "section",
        "band",
        "fpl",
        "fpg",
        "poverty",
        "%",
    )
)
# "and" joins a range only after "between": "when 90% and 80% discounts apply" lists two discounts.
_RANGE_SPAN = (
    rf"(?:between\s+{_RANGE_NUMBER}{_RANGE_SIGN}?\s*(?:-|to|and)\s*{_RANGE_NUMBER}{_RANGE_SIGN}"
    rf"|{_RANGE_NOT_AFTER}{_RANGE_NUMBER}{_RANGE_SIGN}?\s*(?:-|to|through)\s*"
    rf"{_RANGE_NUMBER}{_RANGE_SIGN})"
)
# The range first: "will qualify for a 15-75% discount" (Hartford HealthCare, 070011, 070017,
# 070021, 070035, task 7.17), "(35%-75%) adjustment" (Johns Hopkins Medicine).
_RANGE_SPAN_FIRST = re.compile(rf"{_RANGE_SPAN}\)?\s*(?:discount|reduction|adjustment|off\b|write)")
# The discount word first, with a connective: "a discount of 15-75%", "discounts ranging from 40
# to 90 percent", "the discount will be between 25% and 75%", "adjustments ranging from 35-100%"
# (Freeman). Between the word and its connective only a verb or what is discounted ("discounts off
# charges from 60 percent to 96 percent"). Atrium's table header "charity discount 0 - 100% 100%"
# (360076) has no connective: an income band follows it, not the discount.
_RANGE_NOUN_FIRST = re.compile(
    rf"\b{_RANGE_NOUN}\s+"
    r"(?:(?:will|can|may|shall)\s+(?:be\s+)?|is\s+|are\s+|var(?:y|ies)\s+"
    r"|off\s+(?:of\s+)?(?:the\s+)?(?:gross\s+|total\s+)?(?:charges|balance)\s+)?"
    r"(?P<ranging>(?:that\s+)?(?:ranging|ranges?)\s+)?(?:of\s+|from\s+|(?=between))"
    rf"{_RANGE_SPAN}"
)
# A scale phrase between the discount word and the range: "may qualify for a discount from a scale
# of 10% to 75%" (Holton, 171319, task 7.17), "a discount on a sliding scale from 25% to 75%".
_RANGE_ON_SCALE = re.compile(
    rf"\b{_RANGE_NOUN}\s+(?:(?:from|on|based on|according to)\s+)?(?:a|the)\s+"
    r"(?:sliding[- ]?(?:fee\s+)?)?scale\s+(?:(?:ranging\s+)?from\s+|of\s+)?"
    rf"{_RANGE_NUMBER}{_RANGE_SIGN}?\s*(?:-|to|through)\s*{_RANGE_NUMBER}{_RANGE_SIGN}"
)


def _range_ends(match: re.Match[str]) -> tuple[Decimal, Decimal] | None:
    """The two numbers of a range one of the patterns above matched, when they are a discount
    range: never past 100 ("201-300%" is an income band), and running upward unless the policy
    says "ranging" ("a sliding scale discount ranging from 100 % to 60 %")."""
    first, second = (
        Decimal(group)
        for group in match.groups()
        if group is not None and re.fullmatch(r"\d+(?:\.\d+)?", group)
    )
    ranging = "ranging" in match.re.groupindex and match.group("ranging") is not None
    if max(first, second) <= 100 and (first < second or (ranging and first > second)):
        return first, second
    return None


def discount_range_in_quote(quote: str) -> set[Decimal]:
    """The endpoints of a discount the quote gives as a range ("55% to 75%") or a bound ("at
    least 25%", "up to 75%"). A fixed "60% discount" contributes nothing. Alliance (360131) and
    ACMH (390163) published one endpoint as the whole band's discount (task 7.9), Hartford
    HealthCare (070011 and three siblings) and Holton (171319) one end of "a 15-75% discount" and
    of "a discount from a scale of 10% to 75%" (task 7.17)."""
    text = normalize(quote)
    found: set[Decimal] = set()
    for pattern in (_RANGE_BEFORE, _RANGE_AFTER):
        for match in pattern.finditer(text):
            bound, first, second = match.group(1), match.group(2), match.group(3)
            if second is not None:
                found |= {Decimal(first), Decimal(second)}
            elif bound is not None:
                found.add(Decimal(first))
    for pattern in (_RANGE_SPAN_FIRST, _RANGE_NOUN_FIRST, _RANGE_ON_SCALE):
        for match in pattern.finditer(text):
            ends = _range_ends(match)
            if ends is not None:
                found |= set(ends)
    return found


def tiers_rise_with_income(tiers: list[DiscountTier]) -> bool:
    """A later (richer) band with a larger discount than an earlier one. No sliding scale works
    that way: such a table is the patient's share read as a discount (Adventist Health's
    50/75/75, which gave a 75% "discount" to a household paying 75% of the amount generally
    billed), whatever words the quote happens to carry."""
    ordered = sorted(tiers, key=lambda tier: tier.min_fpl_exclusive)
    return any(
        later.discount_percent > earlier.discount_percent
        for earlier, later in zip(ordered, ordered[1:], strict=False)
    )


# Discount tables the publication audit (2026-10-24) found misread. Each reason rejects the tiers
# alone, and INSURED_TABLE_REASON a free-care limit quoted from inside the table too
# (_insured_table): none holds the sheet or writes a value.
HARDSHIP_TIER_REASON = "the band needs a financial hardship, so it is not an income band"
ADDITIONAL_TIER_REASON = "the discount is added to another one"
INSURED_TABLE_REASON = "the table is for insured patients only"
REGULATED_COLUMN_REASON = "the tier is read from the wrong column of the table for this hospital"
TIERS_SPLIT_REASON = "the table gives different discounts for inpatient and outpatient care"
# A condition besides income. MassHealth's "Medical Hardship Family Countable Income" is how the
# Health Safety Net measures income, never a condition ("MAGI Household Income or Medical Hardship
# Family Countable Income is between 150.1% and 300%").
_HARDSHIP = re.compile(
    r"\b(?:financial|medical) hardship\b(?! family countable)|\bmedical debt\b|\bcatastrophic\b"
)
# The quote's sentences, split before normalizing, which would join a table's rows into one line.
# A span-joined quote's spans are rows of their own: UCHealth Grandview's (060130) "Catastrophic"
# column header, in the span after the discounts, conditions none of its income tiers.
_QUOTE_SENTENCES = re.compile(r"(?<=[.;])\s+|\n+|" + re.escape(SPAN_SEPARATOR))


def tiers_need_hardship(quote: str, tiers: list[DiscountTier]) -> bool:
    """Whether a sentence of the quote that states a tier's discount also asks for a financial or
    medical hardship, medical debt or catastrophic expenses. Such a band is not open to every
    household whose income falls in it: Union Hospital of Cecil County (210032) published 40% for
    401-450% from "between 401-450% of FPL and with a financial hardship will receive a 40%
    adjustment", Maryland's reduced-cost care for medical debt."""
    for sentence in _QUOTE_SENTENCES.split(quote):
        text = normalize(sentence)
        if _HARDSHIP.search(text) and any(
            _number_in(tier.discount_percent, text) for tier in tiers
        ):
            return True
    return False


# "with an additional 25% discount on the remaining balance": a percentage taken off what another
# discount leaves. "Add $5,680 for each additional person" is no discount.
_ADDITIONAL = re.compile(
    r"\badditional (\d+(?:\.\d+)?) ?(?:%|percent) (?:discount|reduction|off\b|adjustment|write)"
)
# An added discount followed by its own base is the band's discount after all: UVM Health
# (470001, 470003) bases every uninsured award on the amount generally billed and every insured
# one on the patient's out-of-pocket costs, so "an additional 40% discount from the amount
# generally billed" reads like UnityPoint's "65% discount off AGB".
_OWN_BASE = re.compile(
    r"\S* (?:(?:from|off|of|on) )?(?:the )?"
    r"(?:amounts? generally billed|agb\b|patient'?s? out ?-?of ?-?pocket)"
)


def tier_is_additional(quote: str, tiers: list[DiscountTier]) -> bool:
    """Whether a stored tier's discount is one the quote adds to another discount. The band's
    discount is then both together, which the one percentage misstates: University Health
    Lakewood (260102) published 25% for 301-400% from "discounted by the current Uninsured
    Discount amount, with an additional 25% discount on the remaining balance"."""
    text = normalize(quote)
    added = {
        Decimal(match.group(1))
        for match in _ADDITIONAL.finditer(text)
        if not _OWN_BASE.match(text, match.end())
    }
    return bool(added & {tier.discount_percent for tier in tiers})


# Who a table is for: "insured" as its own word, or inside "uninsured" or "underinsured" (group 1).
_POPULATION = re.compile(r"\b(un-?|under-?)?insured\b")
# Insured patients named as the table's own lead-in: an "Insured Patients:" heading, or "for
# qualifying insured patients".
_INSURED_LEAD = re.compile(
    r"(?:^|[.:;] |\bfor (?:qualifying |eligible |all )?)insured "
    r"(?:patients?|individuals?|persons?)\b"
)
_BOTH_POPULATIONS = re.compile(r"\b(?:un-?|under-?)insured (?:and|or|&|/) ?$")
_OTHER_POPULATION = re.compile(r"\b(?:un-?|under-?)insured\b|\bself[- ]pay\b")
_LEAD_CHARS = 250


def insured_only_table(quote: str, document_text: str) -> bool:
    """Whether the document introduces the quoted table for insured patients only: the last
    population named in the 250 characters before the quote is insured patients, in an "Insured
    Patients:" heading or a "for qualifying insured patients" lead-in, and the quote itself names
    no uninsured, underinsured or self-pay patients. Henry Ford (230047, 230053, 230092, 230302)
    published as its discount scale the "Discount Off of Balance Due" table its policy gives
    insured patients; uninsured patients pay a share of the Medicare rate under another table.
    "Uninsured and insured patients" introduces a table for both, and the amounts-generally-billed
    clause's "billed to commercially insured patients" introduces none (Heart of Texas, 451348)."""
    if _OTHER_POPULATION.search(normalize(quote)):
        return False
    text = normalize(document_text)
    at = text.find(normalize(quote)[:80])
    if at < 0:
        return False
    # Positions in the whole text, never a slice: a look-back that starts inside "uninsured"
    # never reads its tail as an "insured patients" heading, and ^ matches only at the start.
    start = max(0, at - _LEAD_CHARS)
    mentions = list(_POPULATION.finditer(text, start, at))
    if not mentions or mentions[-1].group(1):
        return False
    last = mentions[-1]
    window = (max(start, last.start() - 30), min(at, last.end() + 12))
    return (
        _INSURED_LEAD.search(text, *window) is not None
        and _BOTH_POPULATIONS.search(text, start, last.start()) is None
    )


# A table row: its band's label, then each column's percentage up to the next band's label.
_ROW_PERCENT = re.compile(r"(?<![\d.$])(\d{1,3}(?:\.\d+)?) ?%")
_ROW_LABEL = r"(?<![\d.]){low} ?%? ?(?:-|to|through|thru) ?{high} ?%"
_NEXT_ROW_LABEL = re.compile(r"(?<![\d.])\d{1,3} ?%? ?(?:-|to|through|thru) ?\d{2,3} ?%")


def _row_percents(text: str, tier: DiscountTier) -> list[Decimal] | None:
    """The percentages in the tier's own row of a normalized table ("201% to 250% 75% % 80%" for
    a tier above 200 up to 250), or None when no row carries the tier's band as its label."""
    low, high = int(tier.min_fpl_exclusive), int(tier.max_fpl_inclusive)
    for start in (low + 1, low):
        label = re.search(_ROW_LABEL.format(low=start, high=high), text)
        if label:
            rest = text[label.end() :]
            following = _NEXT_ROW_LABEL.search(rest)
            row = rest[: following.start() if following else 60]
            return [Decimal(number) for number in _ROW_PERCENT.findall(row)]
    return None


# Where the document turns to uninsured patients, and where it turns back to insured ones. North
# Country (471304) writes "for unsured patients whose household income is".
_UNINSURED_LEAD = re.compile(
    r"\b(?:un-?insured|unsured|self[- ]pay) (?:patients?|individuals?|persons?)\b"
)
_BARE_INSURED = re.compile(r"(?<![\w-])insured (?:patients?|individuals?|persons?)\b")
_SEGMENT = re.compile(r"•|(?<=[.;]) ")
_UNINSURED_SPAN_CHARS = 900


def _band_stated(window: str, tier: DiscountTier) -> bool:
    """Whether the normalized `window` gives the tier's band its discount: in the band's table
    row, or else in one bullet or sentence that names the band's ends and the discount."""
    row = _row_percents(window, tier)
    if row is not None:
        return tier.discount_percent in row
    lows = (tier.min_fpl_exclusive + 1, tier.min_fpl_exclusive)
    return any(
        any(_number_in(low, segment) for low in lows)
        and _number_in(tier.max_fpl_inclusive, segment)
        and _number_in(tier.discount_percent, segment)
        for segment in _SEGMENT.split(window)
    )


def uninsured_get_same_tiers(document_text: str, tiers: list[DiscountTier]) -> bool:
    """Whether the document gives uninsured patients every one of these tiers too: some passage
    that starts at uninsured (or self-pay) patients, and runs up to 900 characters or to where
    insured patients come back, states each band with the same discount. The tiers of an
    insured-only table are then everyone's: Dartmouth Health (301304, 301305, 471302) quoted the
    insured paragraph, and its uninsured paragraph gives the same 75/50/40 for 251-300%, 301-350%
    and 351-400%. Henry Ford's uninsured patients pay a share of the Medicare rate instead."""
    if not tiers:
        return False
    text = normalize(document_text)
    for lead in _UNINSURED_LEAD.finditer(text):
        end = min(len(text), lead.end() + _UNINSURED_SPAN_CHARS)
        insured = _BARE_INSURED.search(text, lead.end(), end)
        window = text[lead.start() : insured.start() if insured else end]
        if all(_band_stated(window, tier) for tier in tiers):
            return True
    return False


def _insured_table(sheet: ProcedureSheet, documents: Mapping[str, str]) -> tuple[str, str] | None:
    """The source id and normalized quote of the sheet's discount table when its document gives
    it to insured patients only and not to uninsured patients too (INSURED_TABLE_REASON), or
    None. A free-care limit quoted from inside that table is the insured patients' as well:
    Novant Health Brunswick (340158) quoted free care to 300% from Novant's insured paragraph,
    where 300% tops a 50% band, and its siblings (340085, 340190, 340195) free care to 200% from
    it, while uninsured patients get a 100% adjustment up to 300%."""
    tiers = sheet.eligibility.discount_tiers
    if tiers is None or tiers.layer is Layer.REPORTED or not tiers.quote:
        return None
    text = documents.get(tiers.source_id or "")
    if text is None or not quote_found(tiers.quote, text):
        return None
    if not insured_only_table(tiers.quote, text) or uninsured_get_same_tiers(text, tiers.value):
        return None
    return tiers.source_id or "", normalize(tiers.quote)


def wrong_regulated_column(quote: str, state: str, tiers: list[DiscountTier]) -> bool:
    """Whether a tier is read from the wrong column of MedStar's two-column table, "HSCRC-Regulated
    Services | Washington Hospitals, Hospital-Based Physician Practices, and non-HSCRC Regulated
    Services": a Maryland hospital's discount is the first column, any other state's hospital's
    the last. MedStar Montgomery (210018) and MedStar Southern Maryland (210062) published 80% for
    201-250% from "201% to 250% 75% % 80%", where their own column gives 75%. A row with one
    percentage, or the same one in both columns, is skipped."""
    text = normalize(quote)
    if "hscrc-regulated" not in text or "non-hscrc" not in text:
        return False
    for tier in tiers:
        percents = _row_percents(text, tier)
        if not percents or len(set(percents)) < 2:
            continue
        expected = percents[0] if state.upper() == "MD" else percents[-1]
        if tier.discount_percent != expected:
            return True
    return False


# Columns or rows for each kind of care: an "Inpatient Outpatient" header (Berkshire, 220046), or
# an "Inpatient Discount" row with an "Outpatient Discount" row after it (Tufts, 220116).
_SERVICE_COLUMNS = re.compile(
    r"\bin-?patient:? out-?patient\b|\bin-?patient discount\b.{0,400}?\bout-?patient discount\b"
)


def tiers_split_by_service(quote: str) -> bool:
    """Whether the quoted table gives inpatient and outpatient care different discounts. One
    percentage per band holds only one of them, and either misleads, as with free-care limits
    (FREE_LIMIT_SPLIT_REASON): Tufts Medical Center (220116) published 30% for 150-300%, an
    inpatient column, where its outpatients get 69.50%, and Berkshire Medical Center (220046)
    46.07% for 0-400%, its inpatient column, where outpatients get 63.14%. "Inpatient and
    outpatient services will be discounted by" is one table for both."""
    return _SERVICE_COLUMNS.search(normalize(quote)) is not None


CEILING_REASON = "quote states an eligibility ceiling for any assistance, not a free-care band"
# "you may qualify for full or partial financial assistance if your household income is at or
# below 400%" (Arnot Ogden, 330090, task 7.9) bounds every kind of help; it is free care only
# when the same passage says so. A bare "partial" is not a ceiling: "full charity care; 201-400%
# receive partial assistance" states the free band and the next one (review of 7.9).
_CEILING_WORDING = re.compile(
    r"\bfull or partial\b|\bpartial(?:ly)? (?:financial )?assistance\b"
    r"|\bfree or (?:discounted|reduced|low[- ]?cost)\b"
    r"|\bmay qualify for (?:financial )?assistance\b|\beligible for (?:financial )?assistance\b"
)
_FREE_MARKERS = re.compile(
    r"\b100 ?(?:%|percent)|\bone hundred percent\b|\bfree (?:hospital |medical )?care\b"
    r"|\bfree of charge\b|\bno charge\b|\bat no cost\b|\bzero\b|\bwaiv\w*"
    r"|\bfull(?:y)? (?:write[- ]?off|covered|discounted|charity|financial assistance|cost)"
)


# Decision 7.12c-1 (owner, 2026-10-23, option B): "you may receive a discount of up to 100
# percent" (Community Hospital of the Monterey Peninsula, 050145, at 400%), "you may be eligible
# for up to a 100% discount" (Flagler, 100090) and "they may qualify for Financial Assistance up
# to 100%" (Wickenburg, 031300) promise at most full help, and only maybe: an assistance limit,
# not a free-care band. Their "100%" is the top of the help, so it is no free marker. CommonSpirit's
# "is eligible to receive Financial Assistance up to a 100% discount" says no "may" and stays
# free care, as CommonSpirit's own summaries say.
_UP_TO_FULL = re.compile(r"\bup to (?:a |an )?(?:100 ?(?:%|percent)|one hundred percent)")
_HEDGED_UP_TO_FULL = re.compile(
    r"\bmay\b[^.;]*\bup to (?:a |an )?(?:100 ?(?:%|percent)|one hundred percent)"
)


def quotes_assistance_ceiling(quote: str) -> bool:
    """A free-care quote that only promises some help ("full or partial", "may qualify for
    financial assistance", "may receive … up to 100%") up to an income, without saying that care
    is free there. Its number is the ceiling on all assistance, which the draft keeps in
    assistance_ceiling_fpl."""
    text = normalize(quote)
    if _HEDGED_UP_TO_FULL.search(text) and _FREE_MARKERS.search(_UP_TO_FULL.sub(" ", text)) is None:
        return True
    return _CEILING_WORDING.search(text) is not None and _FREE_MARKERS.search(text) is None


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


ASSET_REASON = "quote does not support the asset-test value"
_ASSET_WORDS = re.compile(r"\bassets?\b|\bresources\b|\bsavings\b|\bnet worth\b|\bmeans[- ]test")
# The quote says assets are left out of the eligibility decision.
_ASSET_NEGATED = re.compile(
    r"(?:assets?|resources|savings|net worth|employment status)\b[^.;]{0,40}\b(?:are |is |will be )?"
    r"not (?:considered|required|counted|used|reviewed|evaluated|a factor|part of|taken into "
    r"account|included)"
    r"|\bno (?:income or )?(?:asset|resource)s? (?:criteria|test|limit|requirement|review)"
    r"|\bdoes not (?:consider|count|use|review|evaluate|require)\b[^.;]{0,30}"
    r"\b(?:assets?|resources|savings)"
    r"|\bwithout regard to\b[^.;]{0,30}\bassets?\b"
    r"|\basset test(?:ing)? is not (?:required|performed|used|part)"
    r"|\bregardless of (?:their )?assets?\b"
    r"|\bbased (?:solely|only) on (?:household |family )?income\b"
)
# A partial exemption ("the first $10,000 ... shall not be counted", "will never include the
# primary residence", "may also be applied to Medicare recipients") means assets ARE tested.
_ASSET_PARTIAL = re.compile(
    r"\bshall not be counted\b|\bnot be counted\b|\bnor shall\b|\bwill never include\b"
    r"|\bdoes not include\b|\bthe first (?:ten thousand|\$)|\bover the first\b"
    r"|\bmay (?:also )?be applied\b|\bprimary residence\b|\bexempt\w*|\bdoes not apply to\b"
    r"|\bprotected assets?\b"
)
# The quote says assets are looked at.
_ASSET_TESTED = re.compile(
    r"\blimits?\b|\bmeans[- ]test|\bconsidered\b|\bcounted\b|\bverif\w+|\bavailable for payment\b"
    r"|\bused\b|\breview\w*|\bexhausted\b|\bevaluat\w+|\bproof of assets\b|\basset test\w*"
    r"|\bbe applied\b|\btaken into account\b|\bconsists? of\b|\binclud\w+|\bdocument\w*"
    r"|\bvalidate\b"
)


def quote_supports_asset_test(quote: str, value: bool) -> bool:
    """Whether the quote is about assets and says what the boolean says. Twenty-one sheets in
    the first national batch (task 7.9) carried asset_test under quotes that said the opposite
    (Adventist Health's $10,000 exemption read as "no asset test") or nothing about assets at
    all (a Colorado screening paragraph, an AGB sentence), because any boolean passed."""
    text = normalize(quote)
    if _ASSET_WORDS.search(text) is None:
        return False
    negated = _ASSET_NEGATED.search(text) is not None
    partial = _ASSET_PARTIAL.search(text) is not None
    if value:
        # "Assets are not considered" carries "considered", a tested word; the negation wins
        # unless the quote exempts some assets, which means the rest are looked at.
        return _ASSET_TESTED.search(text) is not None and (not negated or partial)
    return negated and not partial


INSURED_REASON = "quote does not say whether insured patients are covered"
# "insured" as its own word (never the "insured" inside "uninsured"), or words only an insured
# patient has: a deductible, a co-pay, coinsurance.
_INSURED_WORDS = re.compile(
    r"(?<!un-)\binsured\b|\bunder-?insured\b|\binsurance\b|\bdeductibles?\b|\bco-?pay\w*"
    r"|\bco-?insurance\b|\bout-of-pocket\b"
)
# The 2026-10-24 publication audit found insured_patients_covered wrong both ways. False stood
# on quotes that help underinsured patients or only say a patient with enough insurance "may be
# deemed ineligible" (Mizell, 010007; Caldwell, 181322), and on the uninsured self-pay discount
# (Mackinac Straits, 231306). True stood on a billing statement (Washington Regional, 040004), a
# payment plan (Western Maryland, 210027), the AGB cap (Hutchinson Health, 240187) and the bare
# heading "Underinsured:" (Madelia, 241323): any sentence that mentioned insurance passed.
INSURED_LEFT_OUT_REASON = "quote does not say insured patients are left out of financial assistance"
PRICE_CAP_REASON = "quote states a price cap benchmarked on insured patients, not who may apply"
BARE_LABEL_REASON = "quote is only a heading"
PAYMENT_PLAN_REASON = "quote is about payment terms, not financial assistance"
ABSENT_REASON = "quote names insurance only to say the patient has none"
# Wording that leaves insured patients out: "available only to uninsured patients", "must be
# uninsured", "insured patients are not eligible", "does not apply to insured patients",
# "cannot be covered by private insurance" (Bacon County, 111327), "must be self-pay"
# (Rockcastle County, 180115), and Cabell Huntington's (510055) "not eligible for financial
# assistance ... if the patient is eligible to receive reimbursement for any portion of a
# patient's bill from any first or third party payor, including health insurance". McKenzie's
# (351302) "not eligible ... if the applicable health insurance lists McKenzie Health as
# out-of-network" leaves out only some insured patients. "Not only to the uninsured" is no
# exclusion.
_INSURED_LEFT_OUT = re.compile(
    r"(?<!not )\bonly (?:to |for )?(?:the )?uninsured\b"
    r"|\buninsured (?:patients |individuals |persons )?only\b"
    r"|\bmust (?:be|remain) uninsured\b"
    r"|\b(?:limited|restricted) to (?:the )?uninsured\b"
    r"|(?<!not )\bonly (?:to |for )?(?:patients|individuals|persons|people|those) "
    r"(?:who (?:do not|don't|does not) have|with no|without) (?:any )?(?:health )?"
    r"(?:insurance|coverage)\b"
    r"|(?<![\w-])insured (?:patients?|individuals?|persons?|people) (?:are|is|will|shall|do|does)"
    r"(?: be)? (?:not|never) (?:be )?(?:eligible|covered|qualify|considered for)\b"
    r"|\b(?:patients|individuals|persons|people|those) (?:with|who have) (?:health |private "
    r"|commercial |third[- ]party )?(?:insurance|coverage)(?: coverage)? "
    r"(?:are|is|will|shall|do|does)(?: be)? not (?:be )?(?:eligible|covered|qualify)\b"
    r"|\b(?:patients|individuals|persons|people) who are insured (?:are|is|will|shall|do|does)"
    r"(?: be)? not (?:be )?(?:eligible|covered|qualify)\b"
    r"|\bnot (?:available|offered|open|extended) to (?:insured|patients (?:with|who have) "
    r"(?:health )?insurance)\b"
    r"|\b(?:does|do|shall|will) not apply to (?:insured|patients (?:with|who have) "
    r"(?:health )?insurance)\b"
    r"|\bnot (?:be )?eligible for financial assis ?tance\b[^.;]{0,80}?\bif (?:the patient|they"
    r"|you) (?:is|are) eligible to receive (?:reimbursement|payment) for any portion\b"
    r"[^.;]{0,120}\binsurance\b"
    r"|\bexclud\w* (?:all )?(?:insured|patients (?:with|who have) (?:health )?insurance)\b"
    r"|\b(?:cannot|can not|may not|must not) be covered by (?:any |private |health |commercial "
    r"|third[- ]party )*insurance\b"
    r"|\bmust be self[- ]?pay\b"
)
# Wording that names insured people as helped: underinsured, limited or inadequate coverage,
# "with or without insurance", and the "sufficient insurance coverage or assets" conditional
# (Mizell, 010007), which turns away only those whose insurance is enough.
_INSURED_HELPED = re.compile(
    r"\bunder[- ]?insured\b"
    r"|\b(?:limited|inadequate|insufficient|partial)(?: or no)? (?:health )?(?:insurance|coverage)\b"
    r"|\bwith or without (?:an? )?(?:health )?insurance\b"
    r"|\b(?:sufficient|adequate|enough) (?:health )?insurance\b|\binsurance (?:coverage )?or assets\b"
)
# A rate that is not financial assistance: the self-pay, uninsured, cash or prompt-pay discount.
# Leaving insured patients out of one says nothing about the FAP (Mackinac Straits, 231306;
# Hillsboro, 141332; RWJBarnabas, 310041).
_NON_FAP_RATE = re.compile(
    r"\b(?:true )?(?:self[- ]?pay|uninsured|cash|prompt[- ]?pay(?:ment)?) "
    r"(?:discounts?|rates?|prices?|pricing)\b"
    r"|\bdiscounted cash price\b"
)
# A price cap ("not be charged more than AGB to insured patients", "pay more for care than
# insured individuals"), up to the end of its clause but not past ", and ..." or ", but ...", so
# "will pay no more than 10% of charges, and insured patients may also qualify" keeps its
# second clause.
_CAP_TAIL = r"(?:(?!,\s*(?:and|but|while|however|although)\b)[^.;])*"
_PRICE_CAP_CLAUSE = re.compile(
    r"\b(?:charged|billed|pay|paid|owe|responsible to pay)\b[^.;]{0,15}?\b(?:more|greater|higher)\b"
    r"[^.;]{0,80}?\bthan\b" + _CAP_TAIL + r"|amounts? generally billed" + _CAP_TAIL
)
_BARE_LABEL = re.compile(r"^[^.;:]{1,40}:$")
# Payment terms: a payment plan, a prompt-pay discount, payment in full, a (billing) statement.
_PAYMENT_ONLY = re.compile(
    r"\bpayment (?:plans?|arrangements?)\b|\bprompt[- ]?pay(?:ment)? discounts?\b"
    r"|\b(?:payment|paid) in full\b|\b(?:billing )?statements?\b"
)
_FAP_WORDS = re.compile(
    r"\bfinancial (?:assistance|aid)\b|\bcharity\b|\bfree\b|\bdiscount\w*|\bwrite[- ]?offs?\b"
    r"|\bwritten off\b|\bforgiv\w+|\breduc\w+|\bsliding\b|\bfap\b"
)
# Insurance named only to say the patient has none (Geisinger, 390006: "does not have insurance
# coverage and was denied Medicaid"), or to tell people to buy some (Houston Methodist, 450358).
_NOT_INSURED = re.compile(
    r"\b(?:(?:do|does|did) not|don't|doesn't) have (?:any )?(?:form of )?(?:health |medical )?"
    r"(?:insurance|coverage)(?: coverage)?\b"
    r"|\b(?:has|have|with) no (?:health |medical )?(?:insurance|coverage)(?: coverage)?\b"
    r"|\bwithout (?:any )?(?:health |medical )?(?:insurance|coverage)(?: coverage)?\b"
    r"|\bno (?:health |medical )?insurance(?: coverage)?\b"
    r"|\bnot insured\b"
    r"|\b(?:purchase|obtain|buy|enroll in|apply for) (?:health )?(?:insurance|coverage)\b"
)
# Another payer beside the missing insurance, as in Maine's "have no health insurance coverage
# or have coverage that pays only part of the bill" or "your portion of the bill".
_OTHER_PAYER = re.compile(
    r"\bcoverage\b|\bmedicare\b|\bthird[- ]party\b|\bpay[eo]rs?\b|\bcovered\b"
    r"|\bportion of (?:the|your|their) bill\b"
)


def _quote_excludes_insured(text: str) -> bool:
    """Whether the normalised quote leaves insured patients out of financial assistance, without
    naming insured people as helped or speaking of a self-pay rate instead."""
    return (
        _INSURED_LEFT_OUT.search(text) is not None
        and _INSURED_HELPED.search(text) is None
        and _NON_FAP_RATE.search(text) is None
    )


def _quotes_only_a_price_cap(text: str) -> bool:
    """Whether the quote's only insured words sit in a price cap, which benchmarks a price on
    insured patients and says nothing about who may apply."""
    if _PRICE_CAP_CLAUSE.search(text) is None:
        return False
    return _INSURED_WORDS.search(_PRICE_CAP_CLAUSE.sub(" ", text)) is None


def _quotes_only_payment_terms(text: str) -> bool:
    """Whether the quote is about payment terms and, once those are set aside, names no
    assistance; the prompt-pay discount is a payment term, not a discount under the policy."""
    if _PAYMENT_ONLY.search(text) is None:
        return False
    return _FAP_WORDS.search(_PAYMENT_ONLY.sub(" ", text)) is None


def _names_insurance_only_as_absent(text: str) -> bool:
    """Whether every mention of insurance says the patient has none, with no other payer or
    partial coverage beside it."""
    if _NOT_INSURED.search(text) is None or _INSURED_HELPED.search(text):
        return False
    rest = _NOT_INSURED.sub(" ", text)
    return _INSURED_WORDS.search(rest) is None and _OTHER_PAYER.search(rest) is None


def insured_field_problem(quote: str, value: bool) -> str | None:
    """Why an insured_patients_covered value does not stand on its quote, or None. Either value
    fails on the AGB cap, another price cap or a bare heading. True needs a quote that speaks of
    insured patients and is not only about payment terms or the lack of insurance; false needs a
    quote that leaves insured patients out of financial assistance."""
    text = normalize(quote)
    if quotes_only_the_agb_cap(quote):
        return AGB_CAP_REASON
    if _quotes_only_a_price_cap(text):
        return PRICE_CAP_REASON
    if _BARE_LABEL.match(text):
        return BARE_LABEL_REASON
    if value:
        if _INSURED_WORDS.search(text) is None:
            return INSURED_REASON
        if _quotes_only_payment_terms(text):
            return PAYMENT_PLAN_REASON
        if _names_insurance_only_as_absent(text):
            return ABSENT_REASON
        return None
    return None if _quote_excludes_insured(text) else INSURED_LEFT_OUT_REASON


OTHER_STATE_REASON = "quote states the band for another state's facilities"
OTHER_STATE_FACILITY_REASON = (
    "quote states the band for another state's facilities, named by its postal code"
)
# The fields that carry an income limit, any of which a system policy may state per state.
INCOME_LIMIT_PATHS = (
    "eligibility.free_care_max_fpl",
    "eligibility.discount_tiers",
    "eligibility.assistance_ceiling_fpl",
)
# "For Illinois facilities – Uninsured patients with household incomes between 401% and 600%
# ... 85% discount" inside AdventHealth's system policy (cw-f-50.1) was published for Wauchula,
# Florida (review of 7.9). Everything after such a heading, up to the next one, is that state's.
_STATE_HEADING = re.compile(
    rf"\b(?:for|in|at) (?:all |our |its )?(?:the )?({STATE_NAME})(?:[- ]based)? "
    r"(?:facilities|hospitals|entities|locations|providers|patients|residents)\b"
)


def _state_segments(quote: str) -> list[tuple[str | None, str]]:
    """The quote cut at each "for <State> facilities" heading: (state code or None, text)."""
    text = normalize(quote)
    segments: list[tuple[str | None, str]] = []
    state: str | None = None
    start = 0
    for match in _STATE_HEADING.finditer(text):
        segments.append((state, text[start : match.start()]))
        state, start = CODE_OF_NAME[match.group(1)], match.end()
    segments.append((state, text[start:]))
    return segments


def bands_under_another_state(quote: str, state: str, value: Any) -> bool:
    """Whether a free-care limit, an assistance ceiling or a discount table takes any of its
    numbers from a part of the quote headed "for <State> facilities" for a state other than the
    hospital's. A quote without such a heading never trips this; with one, every band must be
    stated in full in the parts that are the hospital's own state's or nobody's."""
    segments = _state_segments(quote)
    if all(segment_state in (None, state) for segment_state, _ in segments):
        return False
    own = " ; ".join(text for segment_state, text in segments if segment_state in (None, state))
    if isinstance(value, list):
        return not all(
            _number_in(tier.min_fpl_exclusive, own)
            and _number_in(tier.max_fpl_inclusive, own)
            and _number_in(Decimal(tier.discount_percent), own)
            for tier in value
        )
    return not _number_in(Decimal(value), own)


# UnityPoint's system policy (fap-ec276fb294e1a7f3) gives its Illinois hospitals a schedule of
# their own, "B. An IL UPH Hospital shall provide a charitable discount of 100% ... not more than
# 200%", and Trinity Bettendorf (160104), in Iowa, published free care to 200% from it. The state
# is a postal code, so _STATE_HEADING never sees it. Read as written, never lower-cased ("in",
# "or", "me"), and only after a determiner and before a system's all-caps acronym: Northwestern's
# "NM Woodstock Hospitals" (Illinois), Northern Light's "CA Dean Hospital" (Maine), "a VA
# facility" (Veterans Affairs) and Aultman's capital "OR Aultman Alliance Hospital" name no state.
_FACILITY_OF_STATE = re.compile(
    rf"\b(?:[Aa]n?|[Tt]he|[Ee]ach|[Aa]ny|[Aa]ll|[Ee]very|[Ss]uch) ({STATE_CODE}) [A-Z]{{2,6}} "
    r"(?:[Hh]ospitals?|[Ff]acilit(?:y|ies)|[Ee]ntit(?:y|ies)|[Ll]ocations?)\b"
)


def names_other_state_facility(quote: str, state: str) -> bool:
    """Whether the quote, read in its original case, states its rule for the facilities of a
    state other than `state`, named by postal code: "An IL UPH Hospital", "Each IL UPH Hospital"."""
    return any(
        match.group(1) != state for match in _FACILITY_OF_STATE.finditer(" ".join(quote.split()))
    )


# Task 7.17: MedStar's policy covers residents of "Maryland, northern Virginia, or within the
# District of Columbia", and five MedStar sheets (210015, 210018, 210024, 210034, 210062) stored
# [MD, VA, DC], so a Richmond or Norfolk resident passed the residency step under a bare VA; two
# of them quoted sentences that name no state at all. Each stored state must be named in its
# quote as a whole state: not after a region word ("northern Virginia", "Northeastern Montana"),
# and not as the state of a place ("Washington County", "Kansas City, Missouri", "Virginia
# Beach", "Oxford County in Maine", "Washington, D.C."). The field is rejected, never pruned:
# [MD, DC] would turn northern Virginia away.
RESIDENCY_REASON = "quote does not name every listed state as a whole state"
# "North Carolina and non-North Carolina residents" (drafts for ECU Health North, 340151, and
# Bertie, 341304; Novant Health Forsyth, 340014): a list of states would turn away the patients
# from other states the policy also covers.
RESIDENCY_BOTH_REASON = "quote says residents of other states qualify too"
# A list of no states would turn every household away, whatever the quote says.
RESIDENCY_EMPTY_REASON = "the residency list names no state"
# Letters only as boundaries, so a footnote mark glued to a name still names the state
# ("Wisconsin1", fap-d9c5de9c9fa6dd89); longest name first, so "west virginia" is one state.
_WHOLE_STATE = re.compile(rf"(?<![a-z])({STATE_NAME})(?![a-z])")
# The guards ignore case: they read the normalised quote for names and the quote as written for
# postal codes ("Cecil County, MD", "Southern NH Health", "the following PA counties").
_REGION_WORD = (
    r"(?:north|south)(?:east|west)?(?:ern)?|east(?:ern)?|west(?:ern)?|central|middle|upstate"
    r"|downstate|upper|lower|coastal|mid|greater|rural|metro(?:politan)?"
)
_REGION_BEFORE = re.compile(rf"(?<![a-z])(?:{_REGION_WORD})[- ]+$", re.IGNORECASE)
# "Thomas County, Georgia", "Kansas City, Missouri", "Oxford County in Maine", "bordering county
# in the state of Washington", "primary service areas in Delaware", and the state of a town or a
# part of it: "Virginia Beach, Virginia", "Palm Beach, Florida", "the northern panhandle of West
# Virginia".
_PLACE_BEFORE = re.compile(
    r"(?<![a-z])(?:(?:count(?:y|ies)|city|beach|regions?|panhandle|parish(?:es)?|borough|township)"
    r"(?:\s*,|\s+(?:located\s+)?(?:in|of))|areas?\s+(?:located\s+)?(?:in|of))"
    r"\s+(?:the\s+state\s+of\s+)?$",
    re.IGNORECASE,
)
# "Washington County", "Kansas City", "Virginia Beach", "Louisiana Region 5", "a California
# Hospital Facility", "the Washington campus".
_PLACE_AFTER = re.compile(
    r"[- ]+(?:count(?:y|ies)|city|beach|parish(?:es)?|region|metro(?:politan)?|panhandle|campus"
    r"|clinics?|hospitals?|facilit(?:y|ies))(?![a-z])",
    re.IGNORECASE,
)
# "Washington, D.C." and "Washington DC" are the District, not the state.
_DC_AFTER = re.compile(r"\s*,?\s*d\.?\s?c(?![a-z])", re.IGNORECASE)
# "Rensselaer (NY), Washington (NY) and Berkshire (MA)": a county followed by its own state.
_PAREN_CODE = re.compile(r"\s*\(([a-z]{2})\)", re.IGNORECASE)
_COUNTY_WORD = re.compile(r"(?<![a-z])count(?:y|ies)(?![a-z])")
_NORTH_AND_SOUTH = re.compile(r"(?<![a-z])north (?:and|or|&) south (carolina|dakota)(?![a-z])")
_DC_DOTTED = re.compile(r"(?<![a-z])d\. ?c\.")
_STATE_AND_NON_STATE = re.compile(
    rf"(?<![a-z])({STATE_NAME})(?: residents?)? (?:and|or) non-? ?\1(?![a-z])"
)


def _part_of_a_state(text: str, start: int, end: int, code: str | None = None) -> bool:
    """Whether the state named at text[start:end] is only a region of it or the state of a place
    in it. `code` is the state a written-out name stands for; a different state's code in
    parentheses after it makes the name a county's ("Washington (NY)")."""
    paren = _PAREN_CODE.match(text, end)
    return bool(
        _REGION_BEFORE.search(text, 0, start)
        or _PLACE_BEFORE.search(text, 0, start)
        or _PLACE_AFTER.match(text, end)
        or (code == "WA" and _DC_AFTER.match(text, end))
        or (code and paren and paren.group(1).upper() != code)
    )


def _whole_states_named(quote: str) -> set[str]:
    """The codes of the states the quote names as whole states, by name or by postal code."""
    text = normalize(quote)
    found: set[str] = set()
    for match in _WHOLE_STATE.finditer(text):
        code = CODE_OF_NAME[match.group(1)]
        if not _part_of_a_state(text, match.start(), match.end(), code):
            found.add(code)
    for match in _NORTH_AND_SOUTH.finditer(text):
        found.update(CODE_OF_NAME[f"{side} {match.group(1)}"] for side in ("north", "south"))
    if _DC_DOTTED.search(text):
        found.add("DC")
    written = " ".join(quote.split())
    if written != written.upper():  # an all-caps quote's "IN" or "ME" is a word, not a state
        counties = _COUNTY_WORD.search(text) is not None
        for match in code_tokens(written):
            start, end = match.start(), match.end()
            in_parentheses = written[start - 1 : start] == "(" and written[end : end + 1] == ")"
            if not (in_parentheses and counties) and not _part_of_a_state(written, start, end):
                found.add(match.group(1))
    return found


def residency_problem(value: list[str], quote: str) -> str | None:
    """Why a residency list does not stand on its quote, or None: the list must hold a state, the
    quote must name each one as a whole state, and must not say residents of other states
    qualify too."""
    codes = {code.upper() for code in value}
    if not codes:
        return RESIDENCY_EMPTY_REASON
    for match in _STATE_AND_NON_STATE.finditer(normalize(quote)):
        if CODE_OF_NAME[match.group(1)] in codes:
            return RESIDENCY_BOTH_REASON
    return RESIDENCY_REASON if codes - _whole_states_named(quote) else None


# Lists whose every item must be named by the quote (task 7.9 and its review: the structurer
# prunes them from the model's quote, then trim_quotes keeps the sentence the source contains,
# so the pipeline prunes again after trimming and verify_sheet checks the stored pair).
DOC_MARKERS: dict[DocType, re.Pattern[str]] = {
    DocType.PHOTO_ID: re.compile(
        r"photo[- _]?id|\bid\b|identification|identity|driver|passport|government[- ]issued"
        r"|military id|state[- ]issued"
    ),
    DocType.PROOF_OF_INCOME: re.compile(
        r"income|\bw-?2\b|\b1099\b|\bwages?\b|earnings|salary|pay ?check"
    ),
    DocType.SOCIAL_SECURITY_LETTER: re.compile(
        r"social security|\bssa\b|benefit (?:letter|statement|award)|award letter"
    ),
    DocType.TAX_RETURN: re.compile(
        r"tax (?:return|form|transcript|filing)|\b1040\b|income tax|tax_return|\btaxes\b"
    ),
    DocType.PAY_STUBS: re.compile(
        r"pay ?stubs?|paystubs?|check stubs?|payroll|pay_stubs|wage statement"
    ),
    DocType.BANK_STATEMENTS: re.compile(
        r"\bbank\b|checking|savings account|investment (?:account|statement)"
    ),
    DocType.PROOF_OF_RESIDENCY: re.compile(r"residen\w*|proof of address|\blease\b|utility"),
    DocType.INSURANCE_CARD: re.compile(r"insurance"),
    DocType.MEDICAID_DENIAL: re.compile(r"denial|denied|turned down"),
}


def supported_doc_types(value: list[DocType], quote: str) -> list[DocType]:
    """The listed kinds the quote names, de-duplicated; "other" survives once as long as the
    quote names something and the list is not the whole enum echoed back."""
    text = normalize(quote)
    kept = [
        doc_type
        for doc_type in dict.fromkeys(value)
        if doc_type is not DocType.OTHER and DOC_MARKERS[doc_type].search(text)
    ]
    if not kept:
        raise ValueError("none of the listed documents is named in the quote")
    echoed = set(value) >= set(DocType) - {DocType.OTHER}
    if DocType.OTHER in value and not echoed:
        kept.append(DocType.OTHER)
    return kept


# What a quote must say for each channel to stay on the list (task 7.9: Adventist sheets listed
# mail, fax, email, portal and in person under "paper copies ... from any hospital registration
# area or by phone"; the packet then told seniors to fax a number that does not exist).
CHANNEL_MARKERS: dict[str, re.Pattern[str]] = {
    "mail": re.compile(
        r"(?<![e-])\bmail\w*|\baddress\b|p\.? ?o\.? box|\bbox \d|\bwrite to\b|\bpostal\b|\bsend\b"
        r"|\breturn\b|\bstreet\b|\bst\.|\bsuite\b|\bste\b|\bave\b|avenue|\broad\b|\brd\b|\bblvd\b"
        r"|\bdrive\b|\b[a-z]{2} \d{5}(?:-\d{4})?\b"
    ),
    "fax": re.compile(r"\bfax"),
    "email": re.compile(r"e-?mail|@|electronic mail"),
    "portal": re.compile(
        r"online|portal|website|\bweb\b|\bapp\b|www\.|https?://|download|\.org\b|\.com\b"
        r"|electronically|mychart"
    ),
    "in_person": re.compile(
        r"in[- ]person|\bvisit|registration|\boffice|stop (?:at|by|in)|lobby|counsel|\bbring\b"
        r"|department|\blocation|campus|\bdesk\b|\bwindow\b|admitting|admissions|\bat any\b"
    ),
    "phone": re.compile(
        r"\bphone|\bcall|\btel\b|telephone|\(\d{3}\) ?\d{3}|\b\d{3}[-. ]\d{3}[-. ]\d{4}\b"
    ),
}


def supported_submit_methods(value: list[SubmitMethod], quote: str) -> list[SubmitMethod]:
    text = normalize(quote)
    kept = [method for method in value if CHANNEL_MARKERS[method.kind].search(text)]
    if not kept:
        raise ValueError("none of the listed channels is named in the quote")
    return kept


# A quote about presumptive (automatic) eligibility, as distinct from a screening requirement
# ("may be asked to cooperate with screening for Medicaid") or an accounting rule.
_PRESUMPTIVE_MARKERS = re.compile(
    r"presumptiv|automatic|\bdeem|qualify for (?:free|100)|enrolled in|enrollment in"
    r"|eligibility in|means[- ]tested|without further|participat\w+ in|active (?:medicaid|medi-cal"
    r"|masshealth)|\bhsn (?:full|partial)\b"
)
# Medi-Cal needs its hyphen: "medi-?cal" matched "medical", so "unpaid medical bills" named
# Medicaid (review of 7.9).
PROGRAM_SYNONYMS: dict[str, re.Pattern[str]] = {
    "medicaid": re.compile(
        r"\bmedicaid\b|\bmedi-cal\b|masshealth|mass health|\bmedical assistance\b|apple health"
    ),
    "snap": re.compile(r"\bsnap\b|calfresh|food stamps?|supplemental nutrition"),
    "wic": re.compile(r"\bwic\b|women,? infants"),
    "liheap": re.compile(r"liheap|low[- ]income home energy"),
    "tanf": re.compile(r"\btanf\b|temporary assistance"),
    "ssi": re.compile(r"\bssi\b|supplemental security"),
    "health safety net": re.compile(r"health safety net|\bhsn\b"),
    "medicare": re.compile(r"medicare"),
    "chip": re.compile(r"\bchip\b|children's health insurance"),
    "section 8": re.compile(r"section 8|housing choice voucher"),
    "head start": re.compile(r"head start"),
    "connector": re.compile(r"connector"),
}
PROGRAM_GENERIC_WORDS = frozenset(
    {
        "program", "programs", "plan", "the", "and", "for", "of", "with", "assistance", "health",
        "care", "medical", "state", "patients", "patient", "eligible", "eligibility", "coverage",
        "services", "county", "local", "other",
    }
)  # fmt: skip
MAX_PROGRAM_CHARS = 60
MAX_PROVIDER_CHARS = 120
# A criterion copied as a program ("Individual is self-identified as homeless.").
_SENTENCE_SHAPED = re.compile(r"\b(?:is|are|was|were|has|have|will|may|must)\b|\.$")


def program_named(item: str, text: str) -> bool:
    name = normalize(item)
    key = next((key for key, pattern in PROGRAM_SYNONYMS.items() if pattern.search(name)), None)
    if key is not None and PROGRAM_SYNONYMS[key].search(text):
        return True
    if name in text:
        return True
    words = [
        word
        for word in re.findall(r"[a-z0-9]+", name)
        if len(word) >= 4 and word not in PROGRAM_GENERIC_WORDS
    ]
    return bool(words) and all(word in text for word in words)


def supported_programs(value: list[str], quote: str) -> list[str]:
    """The listed programs the quote names, from a quote that is about automatic eligibility.
    Task 7.9: ACMH listed Medicare from a bad-debt posting rule, Advocate sheets Medicaid from a
    screening sentence, and AdventHealth sheets whole criteria sentences."""
    text = normalize(quote)
    if _PRESUMPTIVE_MARKERS.search(text) is None:
        raise ValueError("quote does not describe presumptive (automatic) eligibility")
    kept = [
        item
        for item in dict.fromkeys(value)
        if len(item) <= MAX_PROGRAM_CHARS
        and _SENTENCE_SHAPED.search(item.lower()) is None
        and program_named(item, text)
    ]
    if not kept:
        raise ValueError("none of the listed programs is named in the quote")
    return kept


def supported_providers(value: list[str], quote: str) -> list[str]:
    """The listed covered providers the quote names (task 9.12): a provider the passage does
    not mention would otherwise become a cited "covered" answer for a physician group."""
    text = normalize(quote)
    kept = [
        item
        for item in dict.fromkeys(value)
        if len(item) <= MAX_PROVIDER_CHARS and normalize(item) in text
    ]
    if not kept:
        raise ValueError("none of the listed providers is named in the quote")
    return kept


LIST_SUPPORT: dict[str, Callable[[Any, str], list[Any]]] = {
    "apply.documents_required": supported_doc_types,
    "apply.submit_methods": supported_submit_methods,
    "programs.presumptive": supported_programs,
    "coverage.covered_providers": supported_providers,
}
LIST_REASON = "quote does not name every listed item"


def unsupported_list_reason(path: str, value: list[Any], quote: str) -> str | None:
    """Why a list field does not stand on its quote: nothing on it is named (the filter's own
    words) or only part of it is. None when the quote names every item."""
    try:
        kept = LIST_SUPPORT[path](value, quote)
    except ValueError as error:
        return str(error)
    return None if len(kept) == len(value) else LIST_REASON


def prune_lists(
    sheet: ProcedureSheet, documents: dict[str, str]
) -> tuple[ProcedureSheet, list[str]]:
    """Each list field cut down to the items its (trimmed) quote names, with a note per change;
    a list the quote names nothing of is dropped. Run after trim_quotes: the structurer filtered
    against the model's whole quote, and the trimmed sentence may name fewer items (140202 kept
    photo_id and bank_statements under a sentence about W-2s and pay stubs, review of 7.9)."""
    updates: dict[str, Any] = {}
    notes: list[str] = []
    for path, cited in sheet.field_paths():
        if path not in LIST_SUPPORT or cited.layer is Layer.REPORTED or not cited.quote:
            continue
        if cited.source_id not in documents:
            continue
        section_name, field_name = path.split(".")
        section = updates.get(section_name, getattr(sheet, section_name))
        try:
            kept = LIST_SUPPORT[path](cited.value, cited.quote)
        except ValueError as error:
            updates[section_name] = section.model_copy(update={field_name: None})
            notes.append(f"{path}: {error} (after trimming)")
            continue
        if len(kept) == len(cited.value):
            continue
        updates[section_name] = section.model_copy(
            update={field_name: cited.model_copy(update={"value": kept})}
        )
        gone = len(cited.value) - len(kept)
        notes.append(f"{path}: {gone} item{'s' if gone > 1 else ''} not named in the trimmed quote")
    return (sheet.model_copy(update=updates) if updates else sheet), notes


# A sentence that promises free care names the income it stops at: "free care ... at or below
# 200% of the Federal Poverty Level", "100 percent write-off ... 200 percent FPL".
_FREE_SENTENCE = re.compile(
    r"\bfree (?:hospital |medical )?care\b|\bfree of charge\b|\bno[- ]cost\b|\bat no charge\b"
    r"|\b100 ?(?:%|percent) (?:discount|reduction|write[- ]?offs?|financial assistance|charity"
    r"|adjustment|off\b)|\b(?:discount|reduction|write[- ]?off|adjustment) of 100 ?(?:%|percent)"
    r"|\bone hundred percent\b|\bfull(?:y)? (?:write[- ]?off|covered|discounted|charity)"
)
_FPL_WORDS = re.compile(r"federal poverty|\bfpl\b|\bfpg\b|poverty (?:level|guideline|income)")
# A percentage read whole, decimals and all: "137.5%" is not a limit of 137 (as in _number_in).
_FPL_PERCENT = re.compile(r"(?<![\d.])(\d{2,3}(?:\.\d+)?) ?(?:%|percent)")
# The same, tied to the poverty level by its own words ("200% of the Federal Poverty Level",
# "250% FPL"): in "allows from 80% to 100% discounting" the 80 is a discount, not an income limit
# (Adirondack Health, 330079, held on a conflict that was not one).
_FPL_TIED_PERCENT = re.compile(
    r"(?<![\d.])(\d{2,3})(?:\.\d+)? ?(?:%|percent)"
    r"(?= ?(?:or (?:less|below|under) )?(?:of |above |below |at |under |x |times )?"
    r"(?:the |current |latest |applicable |annual |published )*(?:federal )?"
    r"(?:poverty|fpl|fpg|fpig))"
)


# Policies write "four hundred percent (400%) of the Federal Poverty Guidelines" and date or
# attribute the guidelines ("of the 2026 HHS Poverty Guidelines"); neither unties the number.
_BRACKETED_PERCENT = re.compile(r"\((\d{2,3}(?:\.\d+)? ?(?:%|percent))\)")
# One qualifier at a time, right before the poverty words: a repeated group here would backtrack
# through a long run of year-like numbers in a flattened table (seconds per page).
_GUIDELINE_QUALIFIER = re.compile(
    r"\b(?:(?:19|20)\d\d|hhs|u\.s\.|us|united states) (?=(?:federal )?(?:poverty|fpl|fpg))"
)
MAX_GUIDELINE_QUALIFIERS = 3  # "of the 2026 U.S. HHS Poverty Guidelines"
# A ceiling written as a negated floor: "does not exceed 400%", "no more than 400%".
_NOT_ABOVE = re.compile(
    r"\b(?:does not|do not|cannot|can't|doesn't|don't|not|no|never)(?: to)? "
    r"(?:above|over|more than|greater than|higher than|in excess of|exceeds?|exceeding|beyond)\b"
)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_SENTENCES = re.compile(r"(?<=[.;])\s+|\n{2,}|•|▪")


def _poverty_text(text: str) -> str:
    """`text` normalized for reading percentages of the poverty level: bracketed figures
    unwrapped, the guidelines' year or publisher dropped, and a negated floor read as the limit
    it is ("does not exceed 400%" as "up to 400%")."""
    text = _BRACKETED_PERCENT.sub(r"\1", normalize(text))
    for _ in range(MAX_GUIDELINE_QUALIFIERS):
        text = _GUIDELINE_QUALIFIER.sub("", text)
    return _NOT_ABOVE.sub("up to", text)


def _tied_percents(text: str) -> list[tuple[Decimal, int]]:
    """Each percentage of the poverty level in `text` (read by _poverty_text), and where it starts."""
    found: list[tuple[Decimal, int]] = []
    for match in _FPL_TIED_PERCENT.finditer(text):
        number = _NUMBER.match(match.group(0))
        if number is not None:
            found.append((Decimal(number.group(0)), match.start()))
    return found


def names_percent_of_poverty_level(quote: str, value: Decimal) -> bool:
    """Whether the quote names `value` as a percentage of the poverty level ("up to 300% of the
    Federal Poverty Guidelines", "400% FPL"), read the way free-care sentences are: a bare "up to
    400%" may be a discount or a balance, and "four times the poverty level" has no number to
    check. An assistance ceiling publishes only on such a quote (decision 7.9a-2)."""
    return any(number == Decimal(value) for number, _ in _tied_percents(_poverty_text(quote)))


# The University of Maryland Medical System's policy gives free care "up to 200% of the income
# eligibility limits established by the Maryland Department of Health": Maryland's Medicaid limits,
# not the poverty level. UM Capital Region (210003), UM Baltimore Washington (210043), UM Upper
# Chesapeake (210049) and UM Rehabilitation & Orthopaedic Institute (210058) published it as free
# care up to 200% FPL (task 7.17).
OTHER_BASIS_REASON = "quote measures income against another benchmark than the poverty level"
_OTHER_BASIS = re.compile(
    r"\bincome eligibility (?:limits?|levels?|standards?|thresholds?)\b"
    r"|\b(?:medicaid|medical assistance|mdh|state)(?:'s)?(?: income)?(?: eligibility)?"
    r" (?:income )?(?:limits?|levels?|standards?|thresholds?)\b"
    r"|\bmedian (?:family |household )?income\b|\b(?:ami|smi)\b"
    # HHS publishes the poverty guidelines ("Department of Health & Human Services" too), and
    # California's HCAI and New Jersey's "Health Care for the Uninsured" are no income measure.
    r"|\bdepartment of health\b(?! ?(?:and|&) ?human\b)(?! care\b)|\bmdh\b"
    r"|\bself[- ]sufficiency standard\b|\bliving wage\b"
)
_CLAUSE_END = re.compile(r"(?<=[.;])\s")
# Read far enough that "... the Department of Health and Human Services" is whole, ...
OTHER_BASIS_WORDS = 20
# ... and let the measure start that far after "of": "of the income eligibility limits
# established by the Maryland Department of Health".
OTHER_BASIS_STARTS_WITHIN = 10
# How much text after each occurrence is read: more than OTHER_BASIS_WORDS words of agency names.
OTHER_BASIS_CHARS = 300
# _PERCENT_SIGN, _NEXT_NUMBER and _POVERTY_AFTER are defined with states_income_limit below.


def percent_of_other_basis(quote: str, value: Decimal) -> bool:
    """Whether the quote gives `value` as a percentage of an income measure other than the poverty
    level. Some occurrence of it (a whole number, never a decimal's whole part or a dollar figure)
    is followed by a percent sign and then, before the next number and the end of the sentence,
    by the measure's name: within ten words after "of" ("200% of the income eligibility limits
    established by the Maryland Department of Health", "80% of the Area Median Income") or right
    after the sign ("80% AMI"). The poverty level's words anywhere before the next number keep it
    the poverty level ("of the income eligibility limits (Federal Poverty Guidelines)", "of the
    guidelines set by the Department of Health and Human Services"), even past a period that only
    ends an abbreviation ("set by the U.S. Department of Health and Human Services poverty
    guidelines", "limits, i.e. the Federal Poverty Guidelines")."""
    text = _poverty_text(quote).replace(",", "")
    for match in re.finditer(rf"(?<![\d.$]){_number_pattern(Decimal(value))}", text):
        after = text[match.end() : match.end() + OTHER_BASIS_CHARS]
        sign = _PERCENT_SIGN.match(after)
        if sign is None:
            continue
        until_next = _NEXT_NUMBER.split(after[sign.end() :], maxsplit=1)[0]
        words = " ".join(until_next.split()[:OTHER_BASIS_WORDS])
        if _POVERTY_AFTER.search(words) is not None:
            continue
        # The measure itself must be named in the same sentence: "exceeds 25% of that family's
        # annual income. MDH Limits refers to ..." gives no limit on MDH's.
        segment = _CLAUSE_END.split(words, maxsplit=1)[0]
        basis = _OTHER_BASIS.search(segment)
        if basis is None:
            continue
        lead = segment[: basis.start()].split()  # "of" and the words up to the measure
        if lead in ([], ["the"]) or (lead[0] == "of" and len(lead) <= OTHER_BASIS_STARTS_WITHIN):
            return True
    return False


def _income_limits(value: Any) -> list[Decimal]:
    """The income limits an income field carries: its value, or each tier's two bounds."""
    if isinstance(value, list):
        return [
            bound for tier in value for bound in (tier.min_fpl_exclusive, tier.max_fpl_inclusive)
        ]
    return [Decimal(value)]


def income_limit_problem(quote: str, state: str, value: Any) -> str | None:
    """Why an income field's quote (INCOME_LIMIT_PATHS) gives another state's or another
    measure's limit, or None: a band under another state's heading, a band for another state's
    facilities named by postal code, or a percentage of another benchmark than the poverty
    level. The pipeline also runs it on an owner-ruled ceiling, which no ruling excuses from it."""
    if bands_under_another_state(quote, state, value):
        return OTHER_STATE_REASON
    if names_other_state_facility(quote, state):
        return OTHER_STATE_FACILITY_REASON
    if any(percent_of_other_basis(quote, limit) for limit in _income_limits(value)):
        return OTHER_BASIS_REASON
    return None


CEILING_UNTIED_REASON = "quote does not state the ceiling as a percentage of the poverty level"
CEILING_FLOOR_REASON = (
    "quote states the percentage as a floor (above, over, at least), not a ceiling"
)
CEILING_NOT_TOP_REASON = "quote names a higher income limit than the ceiling"
CEILING_NOT_ASSISTANCE_REASON = "quote does not say the hospital's financial assistance reaches it"
CEILING_OTHER_PROGRAM_REASON = "quote is about another program or a payment plan"
CEILING_FREE_BAND_REASON = "quote states the free-care band, not the ceiling"
# A floor just before the percentage, the number perhaps spelled out first ("more than four
# hundred percent (400%)").
_FLOOR_BEFORE = re.compile(
    r"\b(?:above|over|more than|greater than|higher than|in excess of|exceeds?|exceeding"
    r"|at least|beyond|a minimum of)(?: \S+){0,3} ?$"
)
# Words that make a sentence about help with the bill ...
_ASSISTANCE_WORDS = re.compile(
    r"\bassist\w*|\bdiscount\w*|\bcharit\w*|\bqualif\w*|\beligib\w*|\bsliding\b|\breduc\w*"
    r"|\bwrite[- ]?offs?\b|\bfinancial aid\b"
)
# ... and the programs and plans that make it someone else's help, or no help at all.
_PROGRAMS = (
    r"health safety net|\bhsn\b|\bmedicaid\b|\bmedi-cal\b|masshealth|mass health|marketplace"
    r"|health connector|insurance exchange|\bchip\b|\b(?:state|county)(?:'s)? (?:program|plan)"
)
_OTHER_PROGRAM = re.compile(rf"{_PROGRAMS}|payment plans?|installment|extended payment")
# "who are not eligible for Medicaid", "denied MassHealth": still the hospital's own help.
_NEGATED_PROGRAM = re.compile(
    rf"\b(?:not|ineligible|denied|no|without)\b(?: \S+){{0,4}}? (?:{_PROGRAMS})"
)


def _about_own_assistance(text: str) -> bool:
    """Whether a sentence (read by _poverty_text) is about the hospital's own help with the bill:
    it speaks of assistance, a discount or charity, and not of another program or a payment plan
    (a program it rules out, "not eligible for Medicaid", does not count)."""
    stripped = _NEGATED_PROGRAM.sub(" ", text)
    return (
        _ASSISTANCE_WORDS.search(stripped) is not None and _OTHER_PROGRAM.search(stripped) is None
    )


# Russell Regional (171350) published an assistance ceiling of 70 from "Patient/guarantors that
# fall below Level 1, 70% of FPL, will receive a 100% discount": its free-care line, while its
# table runs on to 200%, the policy's ceiling. A ceiling quote that promises free care and states
# no partial help is that free band. Decision B stands ("you may receive a discount of up to 100
# percent ... at or below 400%" is a ceiling), and so does a sentence that gives the free band and
# partial help ("a discount if ... at or below 400% ..., and free care if ... below 150%", Dameron,
# 050122; "100% discounts ... up to 600% FPL and lower discounts ... up to 800% FPL", NYU, 330214).
# "Discounted" in any form is partial help ("free care up to 200% FPL, discounted to 250% FPL",
# "Discounted or free care is available up to 400%"); "fully discounted" is free-care wording and
# goes with it. So is a price at the amounts generally billed ("100% write-off AGB Percentage times
# remaining self-pay balance").
_PARTIAL_HELP = re.compile(
    r"\bsliding\b|\bpartial\w*|\breduced\b|\breduction in charges\b"
    r"|\bdiscounted\b|\bdiscounts?\b(?! of 100)(?! ?\(100)|\bagb\b|\bamounts? generally billed\b"
    r"|(?<![\d.])[1-9]\d?(?:\.\d+)? ?(?:%|percent) (?:discount|reduction|adjustment|write"
    r"|financial assistance|charity|of (?:agb|charges))"
)


def _quotes_only_the_free_band(text: str) -> bool:
    """Whether a normalized ceiling quote promises free care and nothing less: no hedged "up to
    100%", no ceiling wording ("full or partial"), and no partial help once its free-care words
    are taken out."""
    if _UP_TO_FULL.search(text) or _HEDGED_UP_TO_FULL.search(text):
        return False
    if _CEILING_WORDING.search(text) or _FREE_SENTENCE.search(text) is None:
        return False
    return _PARTIAL_HELP.search(_FREE_SENTENCE.sub(" ", text)) is None


def ceiling_problem(quote: str, value: Decimal) -> str | None:
    """Why `quote` cannot carry an assistance ceiling of `value`, or None when it can: it must
    name the value as a percentage of the poverty level, as the top of the limits it names and not
    as a floor ("above 400%"), in a sentence that is more than its free-care band and is about the
    hospital's own help (ceiling_assistance_problem). Review of 7.14: a state program's 300%,
    Medicaid screening at 138%, a payment plan, catastrophic help above 400% and the first band of
    a sliding scale all verified as ceilings before. The free-band reading comes before the
    sentence's subject: Russell Regional's misread (171350) worded "will receive free care" names
    no assistance, and dropped as not about it, it would hold nothing (CEILING_FREE_BAND_REASON
    holds the sheet)."""
    text = _poverty_text(quote)
    tied = _tied_percents(text)
    starts = [start for number, start in tied if number == Decimal(value)]
    if not starts:
        return CEILING_UNTIED_REASON
    if any(_FLOOR_BEFORE.search(text, 0, start) for start in starts):
        return CEILING_FLOOR_REASON
    if any(number > Decimal(value) for number, _ in tied):
        return CEILING_NOT_TOP_REASON
    # Only a quote whose one poverty-level percentage is the ceiling: "up to 300% ..., with free
    # care below 200%" states the ceiling beside the free band.
    if {number for number, _ in tied} <= {Decimal(value)} and _quotes_only_the_free_band(
        normalize(quote)
    ):
        return CEILING_FREE_BAND_REASON
    return ceiling_assistance_problem(quote)


def ceiling_assistance_problem(quote: str) -> str | None:
    """Why a ceiling quote is not about the hospital's own help, or None: it must speak of
    assistance, a discount or charity, and not of another program or a payment plan. These are
    the tests ceiling_problem runs after the free-band reading, so the pipeline runs them again
    on a ruled ceiling the owner's ruling spares from that reading
    (pipeline._keep_owner_ruled_ceiling)."""
    stripped = _NEGATED_PROGRAM.sub(" ", _poverty_text(quote))
    if _ASSISTANCE_WORDS.search(stripped) is None:
        return CEILING_NOT_ASSISTANCE_REASON
    if _OTHER_PROGRAM.search(stripped) is not None:
        return CEILING_OTHER_PROGRAM_REASON
    return None


# A rejection for one of these reasons holds the sheet rather than only dropping the field: the
# rest of the sheet would then publish a worse answer. A patient-share table hides the co-pay
# band; without Russell's misread ceiling its table starts at 70% with nothing below; without
# Trinity Bettendorf's Illinois free-care band its table starts at 200% with nothing below. A
# ceiling an owner's ruling made is the owner's: the pipeline keeps it and records the conflict
# (pipeline.OWNER_RULING_CONFLICT), as it does with Russell's own "discount 70" ruling.
HOLDING_REASONS = (PATIENT_SHARE_REASON, CEILING_FREE_BAND_REASON, OTHER_STATE_FACILITY_REASON)


FREE_LIMIT_UNTIED_REASON = "quote does not state the free-care limit as an income limit"
FREE_LIMIT_SPLIT_REASON = "quote gives different free-care limits for inpatient and outpatient care"
FREE_LIMIT_NOT_FREE_REASON = "quote describes a sliding scale or a denial, not free care"
FREE_LIMIT_LATER_BAND_REASON = "quote gives the limit as the top of a later band, not the free band"
_RANGE = re.compile(r"(?<![\d.])(\d{2,3})\s?%?\s*[-\u2013]\s*(\d{2,3})\s?%")


def free_limit_tops_later_band(quote: str, value: Any) -> bool:
    """Whether every range in the quote that ends at `value` starts one above another percentage
    the quote names: "200% 201% - 300%" is a second band (AHN Saint Vincent, 390009, website
    check 4: free care to 200%, 75% off to 300%, published as free care to 300%)."""
    text = normalize(quote)
    numbers = {int(n) for n in re.findall(r"(?<![\d.$])(\d{2,3})\s?%", text)}
    ends = [(int(a), int(b)) for a, b in _RANGE.findall(text) if Decimal(b) == Decimal(value)]
    return bool(ends) and all(start - 1 in numbers for start, _ in ends)


# Website check 3: "granted on a sliding scale … up to 200%" (UF Health Jacksonville, 100001),
# "prorated on a sliding scale" (Summa Western Reserve, 360150) and "denied Financial Assistance,
# due to being over the 200%" (UF Health Shands, 100113) were each published as free care to 200%.
_NOT_FREE_HINT = re.compile(r"\bsliding\b|\bprorat|\bdenied\b")
_FREE_WORDING = re.compile(
    r"\bfree\b|\bno[- ]cost\b|\bno charge\b|\b100 ?(?:%|percent)|\bfull\b|\bforgiv|\bwaive"
    r"|\bwrite[- ]?off\b|\bwritten off\b|\bno patient responsibility\b|\bnot be responsible\b"
)
# "100% NJ Charity Care" is New Jersey's program (Thomas Jefferson University Hospital, 390174,
# Philadelphia, quoted Jefferson's New Jersey section; website check 3).
_STATE_PROGRAM = re.compile(r"\b(nj|new jersey) charity care\b", re.IGNORECASE)


def free_limit_not_free(quote: str) -> bool:
    """Whether a free-care quote describes a sliding scale, a prorated scale or a denial without
    promising free care anywhere in it."""
    text = normalize(quote).lower()
    return _NOT_FREE_HINT.search(text) is not None and _FREE_WORDING.search(text) is None


def names_other_states_program(quote: str, state: str) -> bool:
    """Whether the quote ties the limit to another state's charity care program."""
    return state != "NJ" and _STATE_PROGRAM.search(normalize(quote)) is not None


_INPATIENT = re.compile(r"\bin-?patient\b")
_OUTPATIENT = re.compile(r"\bout-?patient\b")


def free_limit_split_by_service(quote: str) -> bool:
    """Whether the quote gives free care up to different poverty-level limits for inpatient and
    outpatient care. The schema holds one free-care limit, and either one misleads: OhioHealth
    Doctors Hospital (360152, website check 2) published 150% from "up to 100% of the FPG for
    inpatient services or 150% of the FPG for outpatient services", promising free inpatient care
    between 100% and 150% that the policy does not give."""
    text = normalize(quote).lower()
    if not (_INPATIENT.search(text) and _OUTPATIENT.search(text)):
        return False
    return len({number for number, _ in _tied_percents(_poverty_text(quote))}) >= 2


# Free-care limits the publication audit (2026-10-24) found misread. Each reason rejects the limit
# alone and writes no value; neither holds the sheet unless its discount table stays.
FREE_BAND_FEE_REASON = "quote charges the patient within the free band"
FREE_LIMIT_BELOW_FULL_HELP_REASON = "the document gives full assistance to a band above this limit"
# A free-care limit rejected for one of these holds the sheet while its discount table stays
# (pipeline._holding_reasons): the table would publish with nothing below its first band, where
# the policy's free band is, and the band-gap check (publish.sheet_inconsistencies) sees only the
# fields that stay. Henry County's shape (361309): free care to 100%, a line that gives 101-200% a
# 100% discount, bands from 200%; University Health Lakewood's (260102): a 0-100% band that still
# charges a fee, a table from 300%.
HOLDING_BESIDE_A_TABLE_REASONS = (FREE_BAND_FEE_REASON, FREE_LIMIT_BELOW_FULL_HELP_REASON)
# What a free band still charges: a patient, service, visit or flat fee, a nominal or minimal
# payment (Lake Charles Memorial, 190060: "subject to a minimal fee"), a dollar amount per visit,
# day or 30-day period ("$100 per inpatient day"), or one after a kind of care ("Emergency
# Department $10"). "$5,680 per person" sizes a household and "$0" charges nothing. An amount per
# year or month is an income, as in Advocate's (140223, 140288) "at or below 300% FPL (~$47,880/yr
# single)", unless a fee follows it: "a $10 per month fee".
_CHARGES_PATIENT = re.compile(
    r"\b(?:patient|service|visit|flat) fees?\b"
    r"|\b(?:nominal|minimal) (?:payments?|fees?|amounts?|charges?)\b"
    r"(?! (?:will be |is |are )?(?:waived|collected|due))"
    r"|\$ ?[1-9][\d,]*(?:\.\d\d)? ?(?:/|per\b)"
    r"(?! ?(?:additional |each |extra )?(?:person|member|family|individual|dependent|household)"
    r"| ?(?:calendar )?(?:yr|year|annum|mo|month|wk|week|hr|hour)s?\b"
    r"(?! (?:fees?|charges?|co-?pays?)\b))"
    r"|\b(?:visit|department|room|clinic)s?\b:? ?\$ ?[1-9]"
)
# A fee the quote says is not charged: up to three words before it "no patient fees", "will not
# be charged a nominal fee" (Linton Regional, 351328: "Patients will not be required a nominal
# fee"), or within three words after it "patient fees are waived", "service fees will not be
# charged". "Patient fees are not waived" still charges, and so does "at no charge or at the
# nominal payment level" (North Shore University, 330106), whose "no" is four words back.
_FEE_NEGATED_BEFORE = re.compile(r"\b(?:no|without|waives?|waived|not)\b(?: \S+){0,3} ?$")
_FEE_WAIVED_AFTER = re.compile(
    r"\W{0,3}(?:(?!not )\S+ ){0,3}?(?:is |are |will be |shall be )?"
    r"(?:waived|not (?:be )?(?:charged|collected|required|apply))"
)


def free_band_charges_patient(quote: str) -> bool:
    """Whether a free-care quote still charges the patient something within its band: a fee, a
    nominal payment or an amount per visit that the quote does not waive. University Health
    Lakewood (260102) published free care to 100% from "0-100% FPL - patient service fee 1.
    Outpatient office visit $5 2. Emergency Department $10", and Cayuga Medical Center (330307)
    from a band that "qualifies for a nominal payment limit". A clinic's copay named without an
    amount is not caught (Mizell, 010007, where hospital care is free)."""
    text = normalize(quote)
    return any(
        _FEE_NEGATED_BEFORE.search(text, 0, match.start()) is None
        and _FEE_WAIVED_AFTER.match(text, match.end()) is None
        for match in _CHARGES_PATIENT.finditer(text)
    )


# A band of the poverty level written with both ends: "151% and 300%", "101-200%", "201 % but less
# than or equal to 350%".
_BAND_END = r"(\d{2,3}(?:\.\d+)?) ?(?:%|percent)?"
_FPL_BAND = re.compile(
    rf"(?<![\d.$]){_BAND_END} ?(?:-|to|through|thru|and|but (?:less than or equal to"
    rf"|not (?:more|greater) than|no more than|up to)|and (?:up to|less than or equal to)) ?"
    rf"{_BAND_END} ?(?:%|percent)"
)
# Full help within 120 characters after the band, with no other number between.
_FULL_HELP_AFTER = re.compile(
    r"[^%\d]{0,120}?(?:\b100 ?(?:%|percent) (?:financial assistance|charity|discount|adjustment"
    r"|write[- ]?off|reduction|of (?:the )?(?:charges|balance)|free)|\bfree care\b"
    r"|\bfree of charge\b|\bno cost\b|\bat no charge\b)"
)
# Sentences whose full help is not a free band: cost sharing waived (Lima Memorial, 360009: "a 100%
# discount on any co-pay, co-insurance, and deductible after insurance"), or insured patients only.
_COST_SHARING = re.compile(
    r"\bco-?pays?\b|\bco-?payments?\b|\bco-?insurance\b|\bdeductibles?\b|\bout[- ]of[- ]pocket\b"
)
# "Un-insured", "non-insured" and "under-insured" patients are deliberately not insured-only.
_INSURED_ONLY = re.compile(
    r"\b(?:have|has|with) (?:health )?insurance\b|(?<![\w-])insured patients?\b"
    r"|\bafter insurance\b"
)


def full_help_band_above(value: Decimal, document_text: str) -> bool:
    """Whether the field's own document gives full help, free care or 100% assistance, to a band
    that starts just above the free-care limit `value` and ends above it. The limit then stops
    short of the free band: Emory Houston (110069) and St Francis-Emory (110129) published free care
    to 200% from clause b of Emory's policy, whose clause c gives uninsured patients "at least 201 %
    but less than or equal to 350%" of the guidelines "100% financial assistance". A sentence of
    the form "may ... up to 100%" is no free band (decision B), and neither is one that waives
    cost sharing, is about insured patients only, or still charges a fee: Enloe (050039) gives
    151%-300% "free care after a $100 per visit co-pay"."""
    limit = Decimal(value)
    for sentence in _SENTENCES.split(_poverty_text(document_text)):
        if not any(
            limit < Decimal(band.group(1)) <= limit + 1
            and Decimal(band.group(2)) > limit
            and _FULL_HELP_AFTER.match(sentence, band.end())
            for band in _FPL_BAND.finditer(sentence)
        ):
            continue
        if not (
            _HEDGED_UP_TO_FULL.search(sentence)
            or _COST_SHARING.search(sentence)
            or _INSURED_ONLY.search(sentence)
            or free_band_charges_patient(sentence)
        ):
            return True
    return False


# Task 7.15a: Oneida (330115) published free care up to 100% of the poverty level from "Sliding
# Scale 100% 100% 89% 89% ...", whose 100s are discounts (its table gives free care to about 133%),
# and Holzer (360054) and Grady (360210) read 100 from a discount column too. Asking for "% of the
# poverty level" in so many words would have dropped 69 published sheets, most of them right
# ("0% -200% 100% Discount (Full)", "| less than or equal to 200% |", "FPL LEVEL 200% 300%"), so
# the words around the number decide. The poverty level's words, a little wider than _FPL_WORDS
# ("the local poverty line", Carle's "FPIG"):
_POVERTY_WORDS = re.compile(rf"{_FPL_WORDS.pattern}|\bfpig\b|\bpoverty line\b")
# An at-most comparison; _poverty_text has already read "does not exceed" and "no more than" as
# "up to", and "at or below", "falls below" and "equal to or less than" end in one of these.
_AT_MOST = re.compile(r"\bup to\b|\bless than\b|\bunder\b|\bbelow\b")
_AT_MOST_SIGN = re.compile(r"(?:≤|<=?) ?$")
_OR_LESS = re.compile(r"\b(?:or|and) (?:less|below|lower|under)\b")
# The end of a range ("0% - 350%", "0 to 150%", "between 0 and 200%") is a limit ...
_RANGE_END = re.compile(
    r"(?:\d+(?:\.\d+)? ?(?:%|percent)? ?(?:-|to|through|thru)"
    r"|\bbetween \d+(?:\.\d+)? ?(?:%|percent)? and) ?$"
)
# ... its start is a floor: "201 - 300%" limits incomes at 300, never at 201.
_RANGE_GOES_ON = re.compile(r" ?(?:-|to|through|thru) ?\d")
_BETWEEN_BEFORE = re.compile(r"\bbetween ?$")
_AND_A_NUMBER = re.compile(r" ?and ?\d")
# A percentage followed by the help it sizes is how much is written off, never an income: Memorial
# Hospital, Carthage (141305), published free care up to 100% from "FPL Guidelines 100%
# Assistance", the column whose incomes are 150% of the guidelines.
_SIZES_HELP = re.compile(
    r" ?(?:discount|off\b|write[- ]?off|reduction|adjustment|assistance\b|award"
    r"|of (?:the |total |all )?(?:charges|bills?|balances?)\b)"
)
# The same with up to two qualifiers between and more words for the help ("100% charity care",
# "100% financial assistance", "a 100% charity discount", "100% (Charity Care)", "100% Charity
# Write Off"), where the number does not end a range: "0-200% Free Care" is an income band. Review
# of 7.15b: 173 passages of stored documents verified such a 100 as a free-care limit of 100.
_SIZES_QUALIFIED_HELP = re.compile(
    r" ?\(?(?:(?:charity|financial|full|free|hospital|sliding[- ]?scale)\)? ){0,2}\(?"
    r"(?:discount|off\b|write[- ]?off|reduction|adjustment|assistance\b|award|charity\b|free\b"
    r"|coverage\b|covered\b|forgiv|waiv|of (?:the |total |all |their |your |eligible )*"
    r"(?:patient'?s? )?(?:charges|bills?|balances?|accounts?)\b)"
)
# Poverty words before the number head its column only when nothing between them reads as a
# sentence, a benefit or an equation ("FPL will receive 100%", "FPL Discount 100%", "FPL = 100%"),
# and when they do not close an earlier band ("0-250% FPL 100% 251-400% FPL 98%").
_NOT_A_HEADER = re.compile(
    r"\b(?:receives?|qualif\w*|eligible|gets?|will|are|is|discount\w*|charity|assist\w*|free"
    r"|write|adjust\w*|cover\w*)\b|="
)
_AFTER_A_PERCENTAGE = re.compile(r"\d ?(?:%|percent)? ?(?:of (?:the )?)?$")
# After the number, the poverty level's words ("of poverty" too) and "or less" count only before
# the next number: in "cover 100% at 200% FPL" the poverty level is 200's.
_POVERTY_AFTER = re.compile(rf"{_POVERTY_WORDS.pattern}|\bof poverty\b")
_NEXT_NUMBER = re.compile(r"\d")
# A band's top followed by the next band's start ("150% 100% 151 - 199% 90%", Grady's own table
# with its free-care limit of 150) is that band's limit.
_BAND_START = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?) ?(?:%|percent)? ?(?:-|to\b|through\b|thru\b)")
_PERCENT_SIGN = re.compile(r" ?(?:%|percent\b)")
INCOME_WORDS_BEFORE = 3
INCOME_WORDS_AFTER = 6
# How much text around each occurrence is read: a flattened table can be long, and the rule only
# ever looks a few words either side.
INCOME_CONTEXT_CHARS = 200


def _ends_within(pattern: re.Pattern[str], before: str, words: int) -> bool:
    """Whether `pattern` matches in `before` with at most `words` words after it."""
    return any(len(before[match.end() :].split()) <= words for match in pattern.finditer(before))


def _starts_within(pattern: re.Pattern[str], after: str, words: int) -> bool:
    """Whether `pattern` matches in `after` starting within its first `words` words."""
    tokens = after.split()
    match = pattern.search(" ".join(tokens[: words + 1]))
    return match is not None and match.start() < len(" ".join(tokens[:words]))


def _poverty_header_before(before: str) -> bool:
    """Whether poverty words head the number's column within three words before it."""
    for match in _POVERTY_WORDS.finditer(before):
        between = before[match.end() :]
        if len(between.split()) > INCOME_WORDS_BEFORE or _NOT_A_HEADER.search(between):
            continue
        if _AFTER_A_PERCENTAGE.search(before[: match.start()]) is None:
            return True
    return False


def _next_band_follows(value: Decimal, rest: str) -> bool:
    """Whether a band starting just above `value` ("151 -", "150.1 to") opens within the next
    three words."""
    tokens = rest.split()
    head = len(" ".join(tokens[:INCOME_WORDS_BEFORE]))
    for match in _BAND_START.finditer(" ".join(tokens[: INCOME_WORDS_BEFORE + 2])):
        if match.start() < head and value < Decimal(match.group(1)) <= value + 1:
            return True
    return False


def _income_limit_at(text: str, start: int, end: int) -> bool:
    """Whether the number at text[start:end] reads as a limit on household income."""
    before = text[max(0, start - INCOME_CONTEXT_CHARS) : start]
    after = text[end : end + INCOME_CONTEXT_CHARS]
    sign = _PERCENT_SIGN.match(after)
    rest = after[sign.end() :] if sign else after
    if _SIZES_HELP.match(rest) or _RANGE_GOES_ON.match(rest):
        return False
    if _BETWEEN_BEFORE.search(before) and _AND_A_NUMBER.match(rest):
        return False
    if _RANGE_END.search(before) is not None:
        return True
    if _SIZES_QUALIFIED_HELP.match(rest):
        return False
    until_next = _NEXT_NUMBER.split(rest, maxsplit=1)[0]
    return (
        _starts_within(_POVERTY_AFTER, until_next, INCOME_WORDS_AFTER)
        or _starts_within(_OR_LESS, until_next, INCOME_WORDS_AFTER)
        or _poverty_header_before(before)
        or _ends_within(_AT_MOST, before, INCOME_WORDS_BEFORE)
        or _AT_MOST_SIGN.search(before) is not None
        or _next_band_follows(Decimal(text[start:end]), rest)
    )


def states_income_limit(quote: str, value: Decimal) -> bool:
    """Whether the quote states `value` as a limit on household income: some occurrence of it (a
    whole number, never a decimal's whole part or a dollar figure) ends a range ("0% - 350%"), or,
    when no help it sizes follows it ("100% charity care", "100% financial assistance"), has the
    poverty level's words after it before the next number ("200% of the current year's federal
    poverty guidelines", "200% of poverty"), heads a column under them ("FPL LEVEL 200%"), has an
    at-most comparison within three words before it ("less than or equal to 200%", "Federal
    Poverty Level ≤ 200%") or "or less" after it, or tops a band the next one starts just above
    ("150% 100% 151 - 199%"). The start of a range never counts."""
    text = _poverty_text(quote).replace(",", "")
    return any(
        _income_limit_at(text, match.start(), match.end())
        for match in re.finditer(rf"(?<![\d.$]){_number_pattern(Decimal(value))}", text)
    )


# Free care in plain words ("are not billed for services", "will not be charged", "Charity Care at
# 100%", "100% of charges forgiven", "completely free"); never the amounts-generally-billed cap
# ("will not be charged more than ...") or a payment plan's terms ("not charged interest").
_FREE_IN_PLAIN_WORDS = re.compile(
    r"\bnot (?:be )?(?:billed|charged)\b(?! (?:for )?(?:more|any more|in excess|interest|a fee)\b)"
    r"|\bcharity care at 100 ?(?:%|percent)"
    r"|\b100 ?(?:%|percent) of (?:all |the |total |their )?(?:charges|bills?|balances?)\b"
    r"(?: \S+){0,3}? (?:forgiven|waived|written off)\b"
    r"|\b(?:completely|entirely|totally) free\b"
)


def free_care_limits_stated(document_text: str) -> set[Decimal]:
    """The poverty-level percentages a document's free-care sentences name: every percentage in a
    sentence that promises free care and mentions the poverty level. Two stored documents of one
    hospital naming different limits is a conflict to hold on (AHMC Anaheim, 050226: a web page
    said 250%, the newer PDF 200%, and the sheet published 250; review of 7.9). Free care up to
    the poverty line itself ("at or below 100% of the FPL") counts: the percentage is tied to the
    poverty level, so it is no "100% discount"."""
    found: set[Decimal] = set()
    for sentence in _SENTENCES.split(normalize(document_text)):
        if _FPL_WORDS.search(sentence) is None:
            continue
        if (
            _FREE_SENTENCE.search(sentence) is None
            and _FREE_IN_PLAIN_WORDS.search(sentence) is None
        ):
            continue
        found.update(number for number, _ in _tied_percents(_poverty_text(sentence)))
    return found


def assistance_limits_stated(document_text: str) -> set[Decimal]:
    """The poverty-level percentages a document's sentences about the hospital's own help name
    (assistance, discounts, a sliding scale; never another program or a payment plan). A sheet
    that publishes only a ceiling is held when one of them is above it (publish.document_conflicts):
    "at or below 200% ... eligible for financial assistance" was the first band of a sliding scale
    that went on to 400% (review of 7.14)."""
    found: set[Decimal] = set()
    for sentence in _SENTENCES.split(normalize(document_text)):
        text = _poverty_text(sentence)
        if _FPL_WORDS.search(text) is None or not _about_own_assistance(text):
            continue
        found.update(number for number, _ in _tied_percents(text))
    return found


def value_in_quote(value: Any, quote: str) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int | Decimal):
        return _number_in(Decimal(value), quote)
    if isinstance(value, list) and value and all(isinstance(v, DiscountTier) for v in value):
        return all(
            _lower_bound_in(tier.min_fpl_exclusive, quote)
            and _number_in(tier.max_fpl_inclusive, quote)
            and _number_in(Decimal(tier.discount_percent), quote)
            for tier in value
        )
    return True


def _lower_bound_in(bound: Decimal, quote: str) -> bool:
    """A tier's exclusive lower bound is stated when the quote names the bound itself ("above
    200%", "0 to 200% ... 200.1 to 400%") or the first value above it as policies write an
    inclusive start: the next whole percent ("201% to 400%") or a tenth or hundredth past it
    ("200.1%", "200.01%")."""
    return any(
        _number_in(bound + step, quote)
        for step in (Decimal(0), Decimal(1), Decimal("0.1"), Decimal("0.01"))
    )


_SENTENCE_SPLIT = re.compile(r"(?<=[.;:])\s+|\n+")
# A widened quote stays a quote: two or three source sentences, not a page.
MAX_WIDENED_CHARS = 700


def matched_span(
    quote: str,
    document_text: str,
    value: Any = None,
    usable: Callable[[str], bool] | None = None,
) -> str | None:
    """The quote itself if the source contains it; otherwise the sentence of it that does. Among
    several, the longest that is `usable` is preferred (a free-care limit's sentence that states
    it as an income limit, review of 7.15b), then the longest that carries `value` (a stitched
    quote whose longer sentence is about something else must not cost the field its number), then
    simply the longest."""
    if quote_found(quote, document_text):
        return quote.strip()
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(quote) if s.strip()]
    candidates = [
        s for s in sorted(sentences, key=len, reverse=True) if quote_found(s, document_text)
    ]
    if usable is not None and (preferred := [s for s in candidates if usable(s)]):
        return preferred[0]
    if isinstance(value, int | Decimal | list) and not isinstance(value, bool):
        carrying = [s for s in candidates if value_in_quote(value, s)]
        if carrying:
            return carrying[0]
    return candidates[0] if candidates else None


def _states_percent_of_poverty_level(text: str, value: Decimal) -> bool:
    plain = normalize(text)
    return _FPL_WORDS.search(plain) is not None and any(
        Decimal(found) == value for found in _FPL_PERCENT.findall(plain)
    )


def widened_span(span: str, document_text: str, value: Decimal) -> str | None:
    """`span` together with the source sentence before or after it (or both), when the wider
    passage states `value` as a percentage of the poverty level and as an income limit (the rule
    verify_sheet holds a free-care quote to); None otherwise. Models often quote the sentence that
    grants the discount and leave the limit in its neighbour ("... who fall between 0 - 200% of
    the Federal Poverty Level. [They] will have a 100% Charity discount processed.", CHRISTUS
    Spohn). The result is verbatim source text, so the exact-quote rule still applies to it; a
    bare number in the neighbour (a street address) is not enough."""
    needle = normalize(span)
    if not needle:
        return None
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(document_text) if s.strip()]
    for index, sentence in enumerate(sentences):
        if needle not in normalize(sentence):
            continue
        for low, high in ((index - 1, index), (index, index + 1), (index - 1, index + 1)):
            if low < 0 or high >= len(sentences):
                continue
            candidate = " ".join(sentences[low : high + 1])
            if len(candidate) > MAX_WIDENED_CHARS:
                continue
            if (
                _states_percent_of_poverty_level(candidate, value)
                and states_income_limit(candidate, value)
                and quote_found(candidate, document_text)
            ):
                return candidate
    return None


# What makes a passage a free-care statement when a quote is re-anchored on it: the strict
# free-care wording of `_FREE_SENTENCE`, or a "Full Financial Assistance" heading.
_FULL_ASSISTANCE = re.compile(r"\bfull (?:financial )?assistance\b|\bfull charity care\b")


def anchored_span(document_text: str, value: Decimal) -> str | None:
    """The one passage of the source (a sentence, or two adjacent ones: a heading and its line)
    that promises free care, mentions the poverty level and names `value` as its only percentage
    of it, as an income limit; None when there is none. For a model that gives the right limit but
    quotes a sentence elsewhere in the document (Blessing Hospital, 140015: the definition of
    "financially indigent" quoted, the 275% limit under "Full Financial Assistance" two paragraphs
    on). A passage naming several percentages (a flattened table row: 200%, 300%, 400%) is
    ambiguous and never chosen; the shortest qualifying passage wins."""
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(document_text) if s.strip()]
    best: str | None = None
    for index in range(len(sentences)):
        for width in (1, 2):
            window = sentences[index : index + width]
            candidate = " ".join(window)
            if len(window) < width or len(candidate) > MAX_WIDENED_CHARS:
                continue
            plain = normalize(candidate)
            if _FPL_WORDS.search(plain) is None:
                continue
            if _FREE_SENTENCE.search(plain) is None and _FULL_ASSISTANCE.search(plain) is None:
                continue
            named = {Decimal(found) for found in _FPL_PERCENT.findall(plain)} - {Decimal(100)}
            if named != {value} or not states_income_limit(candidate, value):
                continue
            if (best is None or len(candidate) < len(best)) and quote_found(
                candidate, document_text
            ):
                best = candidate
    return best


_POVERTY_LINE = re.compile(
    r"(?:at or below|at or under|not exceed|no more than|up to|within|less than or equal to"
    r"|equal to or less than)\s+(?:100 ?(?:%|percent) of )?(?:the\s+)?"
    r"(?:current\s+|applicable\s+)?(?:federal\s+)?poverty\s+(?:income\s+)?"
    r"(?:level|guidelines?|line|limits?)"
)


def _is_poverty_line(value: Any, quote: str) -> bool:
    return value == 100 and states_the_poverty_line(quote)


def _free_limit_stated(value: Any, quote: str) -> bool:
    """Whether `quote` can carry a free-care limit of `value`: it states the value as an income
    limit, or it is the poverty line written without its number. verify_sheet holds the stored
    quote to this, and trim_quotes looks for a span that meets it, never one that falls short."""
    return states_income_limit(quote, Decimal(value)) or _is_poverty_line(value, quote)


def states_the_poverty_line(quote: str) -> bool:
    """Free care for incomes "at or below the poverty guidelines" is a 100% limit written without
    its number (Alliance Community Hospital, 360131: "at no cost if their family does not exceed
    the Federal Poverty Income Guidelines"). The quote must promise free care in so many words."""
    plain = normalize(quote)
    return _POVERTY_LINE.search(plain) is not None and _FREE_SENTENCE.search(plain) is not None


def trim_quotes(sheet: ProcedureSheet, documents: dict[str, str]) -> ProcedureSheet:
    """Replace each documented quote with the span of it that the source actually contains.

    Models sometimes stitch two passages or paraphrase an edge; keeping the verified sentence
    preserves grounding and lets verify_sheet apply its exact-quote rule to real text."""
    updates: dict[str, Any] = {}
    for path, cited in sheet.field_paths():
        if cited.layer is Layer.REPORTED or not cited.quote:
            continue
        text = documents.get(cited.source_id or "")
        if text is None or SPAN_SEPARATOR in cited.quote:
            # A multi-span quote is verified span by span by quote_found; trimming would keep one.
            continue
        free_care = path == "eligibility.free_care_max_fpl"
        usable = partial(_free_limit_stated, cited.value) if free_care else None
        span = matched_span(cited.quote, text, cited.value, usable)
        if free_care and not (span is not None and _free_limit_stated(cited.value, span)):
            # The limit is right more often than the quote: first the neighbouring sentence,
            # then the one passage of this document that states exactly this limit.
            limit = Decimal(cited.value)
            span = (
                (widened_span(span, text, limit) if span is not None else None)
                or anchored_span(text, limit)
                or span
            )
        if span is None or span == cited.quote:
            continue
        section_name, field_name = path.split(".")
        section = updates.get(section_name, getattr(sheet, section_name))
        updates[section_name] = section.model_copy(
            update={field_name: cited.model_copy(update={"quote": span})}
        )
    return sheet.model_copy(update=updates) if updates else sheet


# Review of 7.15b: 16 published sheets cite a translation while an English document that states the
# income rules is stored (Anna Jaques, 220029, four fields in Italian; Edward, 140231, in Japanese;
# Jefferson Northeast, 390115, in Vietnamese), and seniors read the quotes on the public atlas. The
# pipeline names those documents (pipeline._documents_set_aside); verify_sheet rejects every field
# quoted from one, so a recheck withdraws it at no cost and a rebuild quotes the English one.
TRANSLATION_REASON = "quote is from a translation while an English document states the income rules"
# Task 7.16: nine published Kaiser California sheets quoted Kaiser Northwest's dental policy, and
# others Kaiser's Mid-Atlantic, Northwest, Georgia or Washington documents. A build sets those
# aside before structuring; a sheet built before still quotes them while they stay linked.
DENTAL_REASON = "quote is from a dental assistance program's document, not the hospital's policy"
OTHER_REGION_REASON = "quote is from another Kaiser region's policy, not the hospital's state's"
# The 2026-10-24 publication audit: published sheets quoted a policy whose own list of covered
# hospitals leaves the hospital out (UnityPoint's 1.BR.34 for Clarke County, 161348; Carle Health
# West Region's Schedule D for Carle Hoopeston, 141316; Emory Healthcare's "Hospitals include:" for
# St. Francis-Emory, 110129), another hospital's policy beside the one the hospital's site links as
# its own (Rochester General's for Canton-Potsdam, 330197), and another state's version of a system
# policy (Ascension Saint Agnes, Maryland, for Ascension Providence in Waco, 450042). The pipeline
# names those documents too (pipeline.foreign_documents, sibling_policies, other_state_documents).
# The schedule clause has its own reason: Carle West's policy sits on Carle Hoopeston's own website.
# Each set-aside reason fits the withdraw table's notes after the longest field path
# (cli.NOTES_CHARS). The document is the policy or one that goes with it, such as UnityPoint's
# application for Clarke County; the policy's list of covered hospitals leaves the hospital out.
FOREIGN_OWNER_REASON = (
    "quote is from another organisation's document; its policy leaves the hospital out and the "
    "hospital's site does not link it"
)
SCHEDULE_OWNER_REASON = (
    "quote is from a policy whose own list of covered hospitals leaves this hospital out"
)
SIBLING_POLICY_REASON = "quote is from another hospital's policy; the hospital's site links its own"
OTHER_STATE_DOCUMENT_REASON = "quote is from another state's version of the system policy"
# Mercy Merced (050444, California) lent Mercy San Juan's application to Longmont United (060003,
# Colorado) before siblings lent only within the state; nothing in its address names California.
LENT_DOCUMENT_REASON = "quote is from a document lent by another state's hospital"


@dataclass
class VerificationReport:
    accepted: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejected


def verify_sheet(
    sheet: ProcedureSheet,
    documents: dict[str, str],
    set_aside: Mapping[str, str] | None = None,
) -> VerificationReport:
    """Each documented field held to its quote and its source text; `set_aside` names the
    documents no field may be quoted from, each with the reason (TRANSLATION_REASON,
    DENTAL_REASON, OTHER_REGION_REASON, OTHER_STATE_DOCUMENT_REASON, LENT_DOCUMENT_REASON,
    FOREIGN_OWNER_REASON, SCHEDULE_OWNER_REASON, SIBLING_POLICY_REASON)."""
    set_aside = set_aside or {}
    report = VerificationReport()
    insured_table = _insured_table(sheet, documents)
    for path, cited in sheet.field_paths():
        if cited.layer is Layer.REPORTED:
            report.accepted.append(path)
            continue
        text = documents.get(cited.source_id or "")
        if text is None:
            report.rejected.append((path, "source text missing"))
        elif not quote_found(cited.quote or "", text):
            report.rejected.append((path, "quote not found in source"))
        elif reason := set_aside.get(cited.source_id or ""):
            report.rejected.append((path, reason))
        elif looks_serialised(cited.value):
            report.rejected.append((path, "value is a list or object written as text"))
        elif not value_in_quote(cited.value, cited.quote or "") and not (
            path == "eligibility.free_care_max_fpl"
            and _is_poverty_line(cited.value, cited.quote or "")
        ):
            report.rejected.append((path, "value not in quote"))
        elif path in LIST_SUPPORT and (
            reason := unsupported_list_reason(path, cited.value, cited.quote or "")
        ):
            report.rejected.append((path, reason))
        elif path == "eligibility.assistance_ceiling_fpl" and (
            reason := ceiling_problem(cited.quote or "", cited.value)
        ):
            report.rejected.append((path, reason))
        elif path == "eligibility.discount_tiers" and (
            quotes_patient_share(cited.quote or "") or tiers_rise_with_income(cited.value)
        ):
            # Before the income-limit tests: the patient's share holds the sheet, and a table
            # measured on another benchmark, or under another state's heading, only goes.
            report.rejected.append((path, PATIENT_SHARE_REASON))
        elif path in INCOME_LIMIT_PATHS and (
            reason := income_limit_problem(cited.quote or "", sheet.hospital.state, cited.value)
        ):
            report.rejected.append((path, reason))
        elif path == "eligibility.free_care_max_fpl" and not _free_limit_stated(
            cited.value, cited.quote or ""
        ):
            report.rejected.append((path, FREE_LIMIT_UNTIED_REASON))
        elif path == "eligibility.free_care_max_fpl" and free_limit_split_by_service(
            cited.quote or ""
        ):
            report.rejected.append((path, FREE_LIMIT_SPLIT_REASON))
        elif path == "eligibility.free_care_max_fpl" and free_limit_tops_later_band(
            cited.quote or "", cited.value
        ):
            report.rejected.append((path, FREE_LIMIT_LATER_BAND_REASON))
        elif path == "eligibility.free_care_max_fpl" and free_limit_not_free(cited.quote or ""):
            report.rejected.append((path, FREE_LIMIT_NOT_FREE_REASON))
        elif path == "eligibility.free_care_max_fpl" and names_other_states_program(
            cited.quote or "", sheet.hospital.state
        ):
            report.rejected.append((path, OTHER_STATE_REASON))
        elif path == "eligibility.free_care_max_fpl" and free_band_charges_patient(
            cited.quote or ""
        ):
            report.rejected.append((path, FREE_BAND_FEE_REASON))
        elif path == "eligibility.free_care_max_fpl" and full_help_band_above(cited.value, text):
            report.rejected.append((path, FREE_LIMIT_BELOW_FULL_HELP_REASON))
        elif path == "eligibility.free_care_max_fpl" and quotes_assistance_ceiling(
            cited.quote or ""
        ):
            report.rejected.append((path, CEILING_REASON))
        elif (
            path == "eligibility.free_care_max_fpl"
            and insured_table is not None
            and cited.source_id == insured_table[0]
            and normalize(cited.quote or "") in insured_table[1]
        ):
            report.rejected.append((path, INSURED_TABLE_REASON))
        elif path == "eligibility.discount_tiers" and discount_range_in_quote(cited.quote or "") & {
            Decimal(tier.discount_percent) for tier in cited.value
        }:
            report.rejected.append((path, RANGE_REASON))
        elif path == "eligibility.discount_tiers" and tiers_need_hardship(
            cited.quote or "", cited.value
        ):
            report.rejected.append((path, HARDSHIP_TIER_REASON))
        elif path == "eligibility.discount_tiers" and tier_is_additional(
            cited.quote or "", cited.value
        ):
            report.rejected.append((path, ADDITIONAL_TIER_REASON))
        elif (
            path == "eligibility.discount_tiers"
            and insured_only_table(cited.quote or "", text)
            and not uninsured_get_same_tiers(text, cited.value)
        ):
            report.rejected.append((path, INSURED_TABLE_REASON))
        elif path == "eligibility.discount_tiers" and wrong_regulated_column(
            cited.quote or "", sheet.hospital.state, cited.value
        ):
            report.rejected.append((path, REGULATED_COLUMN_REASON))
        elif path == "eligibility.discount_tiers" and tiers_split_by_service(cited.quote or ""):
            report.rejected.append((path, TIERS_SPLIT_REASON))
        elif path == "eligibility.insured_patients_covered" and (
            reason := insured_field_problem(cited.quote or "", bool(cited.value))
        ):
            report.rejected.append((path, reason))
        elif path == "eligibility.asset_test" and not quote_supports_asset_test(
            cited.quote or "", bool(cited.value)
        ):
            report.rejected.append((path, ASSET_REASON))
        elif path == "eligibility.residency" and (
            reason := residency_problem(cited.value, cited.quote or "")
        ):
            report.rejected.append((path, reason))
        else:
            report.accepted.append(path)
    return report
