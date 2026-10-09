"""Exact-quote verification for procedure sheet fields (spec section 8, step 5)."""

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
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
    """Whether a free-care limit or a discount table takes any of its numbers from a part of the
    quote headed "for <State> facilities" for a state other than the hospital's. A quote without
    such a heading never trips this; with one, every band must be stated in full in the parts
    that are the hospital's own state's or nobody's."""
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


LIST_SUPPORT: dict[str, Callable[[Any, str], list[Any]]] = {
    "apply.documents_required": supported_doc_types,
    "apply.submit_methods": supported_submit_methods,
    "programs.presumptive": supported_programs,
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
_FPL_PERCENT = re.compile(r"(?<![\d.])(\d{2,3})(?:\.\d+)? ?(?:%|percent)")


def free_care_limits_stated(document_text: str) -> set[Decimal]:
    """The poverty-level percentages a document's free-care sentences name: every percentage in a
    sentence that promises free care and mentions the poverty level. Two stored documents of one
    hospital naming different limits is a conflict to hold on (AHMC Anaheim, 050226: a web page
    said 250%, the newer PDF 200%, and the sheet published 250; review of 7.9)."""
    found: set[Decimal] = set()
    for sentence in re.split(r"(?<=[.;])\s+|\n{2,}|•|▪", normalize(document_text)):
        if _FREE_SENTENCE.search(sentence) is None or _FPL_WORDS.search(sentence) is None:
            continue
        found.update(
            Decimal(match.group(1))
            for match in _FPL_PERCENT.finditer(sentence)
            if match.group(1) != "100"
        )
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
        elif path in LIST_SUPPORT and (
            reason := unsupported_list_reason(path, cited.value, cited.quote or "")
        ):
            report.rejected.append((path, reason))
        elif path in ("eligibility.free_care_max_fpl", "eligibility.discount_tiers") and (
            bands_under_another_state(cited.quote or "", sheet.hospital.state, cited.value)
        ):
            report.rejected.append((path, OTHER_STATE_REASON))
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
