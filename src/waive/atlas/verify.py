"""Exact-quote verification for procedure sheet fields (spec section 8, step 5)."""

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from functools import partial
from typing import Any

from waive.atlas.schema import DiscountTier, DocType, Layer, ProcedureSheet, SubmitMethod
from waive.atlas.states import CODE_OF_NAME, STATE_NAME

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


def discount_range_in_quote(quote: str) -> set[Decimal]:
    """The endpoints of a discount the quote gives as a range ("55% to 75%") or a bound ("at
    least 25%", "up to 75%"). A fixed "60% discount" contributes nothing. Alliance (360131) and
    ACMH (390163) published one endpoint as the whole band's discount (task 7.9)."""
    text = normalize(quote)
    found: set[Decimal] = set()
    for pattern in (_RANGE_BEFORE, _RANGE_AFTER):
        for match in pattern.finditer(text):
            bound, first, second = match.group(1), match.group(2), match.group(3)
            if second is not None:
                found |= {Decimal(first), Decimal(second)}
            elif bound is not None:
                found.add(Decimal(first))
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


def quotes_assistance_ceiling(quote: str) -> bool:
    """A free-care quote that only promises some help ("full or partial", "may qualify for
    financial assistance") up to an income, without saying that care is free there. Its number
    is the ceiling on all assistance, which the draft keeps in assistance_ceiling_fpl."""
    text = normalize(quote)
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
_INSURED_EXCLUDED = re.compile(
    r"\bonly (?:to |for )?(?:the )?uninsured\b|\buninsured (?:patients |individuals )?only\b"
    r"|\bnot (?:available|offered|open) to\b|\bexclud\w+|\bineligible\b|\b(?:do|does) not qualify\b"
    r"|\bnot (?:be )?eligible\b|\b(?:do|does) not have (?:any (?:form of )?)?(?:health )?insurance\b"
    r"|\bwithout (?:health )?insurance\b|\bno (?:health )?insurance\b|\bmust be uninsured\b"
    r"|\bnot intended to\b|\bnot (?:cover|apply to|provide)\b"
)


def quote_addresses_insured(quote: str, value: bool) -> bool:
    """Whether the quote speaks of insured patients at all, and, for a False value, says they
    are left out. Sentences about "all uninsured patients", emergency care for everyone or
    physician fees were read both ways in the first national batch (task 7.9)."""
    text = normalize(quote)
    if _INSURED_WORDS.search(text) is None:
        return False
    return value or _INSURED_EXCLUDED.search(text) is not None


OTHER_STATE_REASON = "quote states the band for another state's facilities"
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


CEILING_UNTIED_REASON = "quote does not state the ceiling as a percentage of the poverty level"
CEILING_FLOOR_REASON = (
    "quote states the percentage as a floor (above, over, at least), not a ceiling"
)
CEILING_NOT_TOP_REASON = "quote names a higher income limit than the ceiling"
CEILING_NOT_ASSISTANCE_REASON = "quote does not say the hospital's financial assistance reaches it"
CEILING_OTHER_PROGRAM_REASON = "quote is about another program or a payment plan"
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


def ceiling_problem(quote: str, value: Decimal) -> str | None:
    """Why `quote` cannot carry an assistance ceiling of `value`, or None when it can: it must
    name the value as a percentage of the poverty level, as the top of the limits it names and not
    as a floor ("above 400%"), in a sentence about the hospital's own help. Review of 7.14: a state
    program's 300%, Medicaid screening at 138%, a payment plan, catastrophic help above 400% and
    the first band of a sliding scale all verified as ceilings before."""
    text = _poverty_text(quote)
    tied = _tied_percents(text)
    starts = [start for number, start in tied if number == Decimal(value)]
    if not starts:
        return CEILING_UNTIED_REASON
    if any(_FLOOR_BEFORE.search(text, 0, start) for start in starts):
        return CEILING_FLOOR_REASON
    if any(number > Decimal(value) for number, _ in tied):
        return CEILING_NOT_TOP_REASON
    stripped = _NEGATED_PROGRAM.sub(" ", text)
    if _ASSISTANCE_WORDS.search(stripped) is None:
        return CEILING_NOT_ASSISTANCE_REASON
    if _OTHER_PROGRAM.search(stripped) is not None:
        return CEILING_OTHER_PROGRAM_REASON
    return None


FREE_LIMIT_UNTIED_REASON = "quote does not state the free-care limit as an income limit"
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
            _number_in(tier.min_fpl_exclusive, quote)
            and _number_in(tier.max_fpl_inclusive, quote)
            and _number_in(Decimal(tier.discount_percent), quote)
            for tier in value
        )
    return True


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
        if text is None:
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
# pipeline names those documents (pipeline._translations_to_reject); verify_sheet rejects every
# field quoted from one, so a recheck withdraws it at no cost and a rebuild quotes the English one.
TRANSLATION_REASON = "quote is from a translation while an English document states the income rules"


@dataclass
class VerificationReport:
    accepted: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejected


def verify_sheet(
    sheet: ProcedureSheet, documents: dict[str, str], translations: frozenset[str] = frozenset()
) -> VerificationReport:
    """Each documented field held to its quote and its source text; `translations` names the
    documents no field may be quoted from (see TRANSLATION_REASON)."""
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
        elif cited.source_id in translations:
            report.rejected.append((path, TRANSLATION_REASON))
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
        elif path in INCOME_LIMIT_PATHS and (
            bands_under_another_state(cited.quote or "", sheet.hospital.state, cited.value)
        ):
            report.rejected.append((path, OTHER_STATE_REASON))
        elif path == "eligibility.free_care_max_fpl" and not _free_limit_stated(
            cited.value, cited.quote or ""
        ):
            report.rejected.append((path, FREE_LIMIT_UNTIED_REASON))
        elif path == "eligibility.free_care_max_fpl" and quotes_assistance_ceiling(
            cited.quote or ""
        ):
            report.rejected.append((path, CEILING_REASON))
        elif path == "eligibility.discount_tiers" and (
            quotes_patient_share(cited.quote or "") or tiers_rise_with_income(cited.value)
        ):
            report.rejected.append((path, PATIENT_SHARE_REASON))
        elif path == "eligibility.discount_tiers" and discount_range_in_quote(cited.quote or "") & {
            Decimal(tier.discount_percent) for tier in cited.value
        }:
            report.rejected.append((path, RANGE_REASON))
        elif path == "eligibility.insured_patients_covered" and quotes_only_the_agb_cap(
            cited.quote or ""
        ):
            report.rejected.append((path, AGB_CAP_REASON))
        elif path == "eligibility.insured_patients_covered" and not quote_addresses_insured(
            cited.quote or "", bool(cited.value)
        ):
            report.rejected.append((path, INSURED_REASON))
        elif path == "eligibility.asset_test" and not quote_supports_asset_test(
            cited.quote or "", bool(cited.value)
        ):
            report.rejected.append((path, ASSET_REASON))
        else:
            report.accepted.append(path)
    return report
