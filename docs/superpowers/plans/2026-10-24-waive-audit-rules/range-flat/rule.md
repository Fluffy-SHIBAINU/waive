# range-flat

## why it passed
All five sheets got through the same check: verify_sheet at verify.py:1232 rejects eligibility.discount_tiers only when discount_range_in_quote(quote) (verify.py:155-168) shares a number with a stored discount_percent. For all five quotes that function returns set(), so there is nothing to intersect. I confirmed this by running it on the published quotes.

(1) Hartford HealthCare (070011, 070017, 070021, 070035). The quote says "will qualify for a 15-75% discount", with the percent sign written once, after the second number. _PERCENT is r"(\d+(?:\.\d+)?)\s*%", so it needs a % after each number. That means the range branch of _RANGE_AFTER (verify.py:149-152, `{_PERCENT}(?:\s*(?:to|-)\s*{_PERCENT})?\s*(?:discount|...)`) can never match "15-75%". The regex instead matches only "75% discount", with group(1) (bound) and group(3) (second) both None. Lines 163-167 add nothing for a plain fixed percent, so the range is invisible. _RANGE_BEFORE does not apply because the discount word comes after the numbers.

(2) Holton (171319). The quote says "may qualify for a discount from a scale of 10% to 75%". _RANGE_BEFORE (verify.py:142-147) allows only `discount\s+(of\s+)?((that\s+)?(ranging|ranges?)\s+)?(from\s+)?(at least|up to|between\s+)?` before the first percent. The words "a scale of" between "from" and "10%" break that match. The only match left is the earlier fixed "discount of 100%". _RANGE_AFTER fails too because no discount word follows "75%".

Upstream checks that also let these through:
- value_in_quote (verify.py:956-962) passes, because _number_in finds both 75 and 15 inside "15-75%".
- The structurer's _discount caster (structure.py:569-573) rejects a range only when the model writes the range string. Here the model wrote a single endpoint (75, or 15 for MidState).
- Prompt rule 5 (structure.py:43) says nothing about a band whose discount is a range.

## rule
Field: eligibility.discount_tiers. Where: verify.py. Extend discount_range_in_quote, which verify_sheet already calls in its discount_tiers branch (line 1232), so verify_sheet, withdraw_rejected, recheck and the build path all pick it up. Keep the intersection rule exactly as it is: the field is rejected only when a range endpoint equals a stored discount_percent.

Add three patterns, applied to normalize(quote). Each one keeps (low, high) only when low < high <= 100, so an income band such as "201-300%" never counts as a discount range.

(a) One percent sign, discount word after: (?<![\d.])N\s*(?:-|to)\s*N\s*%\s*(?:discount|reduction|off\b|write). Example: "a 15-75% discount".

(b) One percent sign, discount word before, with a required connective: \b(?:discounts?|reductions?|reduc\w+|write[- ]?offs?)\s+(?:(?:that\s+)?(?:ranging|ranges?)\s+)?(?:of|from|between)\s+(?:between\s+)?N\s*(?:-|to|and)\s*N\s*%. Example: "a discount of 15-75%". The connective is required so a table header such as "charity discount 0 - 100% 100%" (Atrium, 360076) is not read as a range.

(c) A scale phrase between the discount word and the range: \b<discount noun>\s+(?:(?:from|on|based on|according to)\s+)?(?:a|the)\s+(?:sliding[- ]?)?scale\s+(?:(?:ranging\s+)?from\s+|of\s+)?N\s*%?\s*(?:to|-|through)\s*N\s*%. Example: "a discount from a scale of 10% to 75%".

Outcome: reject the eligibility.discount_tiers field with the existing RANGE_REASON. withdraw_rejected / recheck drop the field. The sheet stays published while its free-care band verifies, as it does for all five sheets here. A sheet left with no income rule would be held by decide_status. The rule never touches free_care_max_fpl, so decision B is unaffected.

## challenge (sound)
The proposed rule holds up. I re-checked it myself, independently of the other engineer's scripts.

**The diagnosis is right.** The current discount_range_in_quote returns an empty set for all five quotes:
- **Hartford HealthCare:** "15-75%" has one percent sign, so only "75% discount" is read, as a fixed discount.
- **Holton:** "a scale of" breaks _RANGE_BEFORE.

I confirmed in the stored text (fap-dc06d545589e6a88, lines 186 and 233) that Appendix A is only referenced, never included. Holton's page (web-3810d0fb508c726c, line 149) says "a discount from a scale of 10% to 75%".

**Published fields (all 605 sheets, 3,184 fields).**
- Run with my own script, the predicate rejects exactly 070011, 070017, 070021, 070035 and 171319, all flagged and upheld. No other field is touched.
- I read every one of the 176 discount_tiers quotes, in all states, including the small-state sheets (AK, AZ, DC, NV, SD, WI, ND, ID and others). No other quote gives a discount as a range, so there are no misses.
- Near-misses stay accepted: Atrium and Miami Valley ("charity discount 0 - 100% 100%"), Day Kimball, Kettering ("0% - 100% 100%"), Bluffton ("50, 75, or 100%"), Clarion ("in a range of 100% ... or 75%"), and the NJ patient-share tables.
- The schema enforces discount_percent between 1 and 99, so the endpoints of "0-100%" can never intersect a stored tier.

**Whole database (read-only).**
- **Latest sheet of every hospital (653 with tiers):** besides the five, the rule also catches drafts 070024 and 070025 (same Hartford HealthCare policy, stored 75) and 171362 (draft, "from a scale of 60% to 80%", stored 80). Those are the same defect. It catches nothing else.
- **End to end, as withdraw_rejected does it (trim_quotes, prune_lists, verify_sheet):** only the five sheets change, each losing discount_tiers. Their free-care values (250 for Hartford HealthCare, 100 for Holton) still verify.
- **Unit tests:** tests/unit/test_verify.py passes with the function swapped in.

**Whole source corpus (4,587 docs).** Every range the proposed patterns newly read is a genuine discount range. The `low < high <= 100` guard correctly drops income bands ("251-300% discount", "discount from 201-300% fpl").

**Gaps (no published or draft sheet is affected today).** These wordings appear in stored hospital documents and the proposed rule misses them:
- "percent" spelled out: "discounts ranging from 40 to 90 percent" (HCA application), "discounts off charges from 60 percent to 96 percent" (Anaconda FAP)
- "adjustment(s)": "(35%-75%) adjustment" (Johns Hopkins Medicine), "adjustments ranging from 35-100%" (Freeman)
- a verb between noun and range: "the discount will be between 25% and 75%", "varies between 25 and 100 percent"
- high-to-low ranges: "ranging from 100 % to 60 %", "ranging between 95% and 5%"

The optional hardening in revised_rule covers all of these. It reads nothing new on published fields and passes 37 positive and trap cases, including:
- "90% and 80% discounts apply" (a list, not a range)
- the NJ "20% to 80% sliding fee scale" (patient share)
- hardship tables ("adjustment 20 - 29% 15%")
- "patients that qualify for 0-20% will receive a 20% adjustment"
- "$1,000 - 20% discount"

The last one is a constructed trap: the proposal's lookbehind reads it as 0-20, but no stored document has that exact text.

**Adjacent case for the owner, not this defect.** Pioneer Memorial (431328, SD; not among the 153 audited sheets) stores tiers 75/50/24 from a table headed "Maximum Discount". That is an "up to" bound written as a column header. It is not covered by this rule, and "maximum discount" appears in 9 stored docs, some unrelated, so I would not fold it in mechanically.

**Housekeeping.** I wrote nothing to the database and edited no repo file. One slip: my first dump script wrote tiers_dump.txt into the repo root. I moved it to the scratch folder at once; afterwards git status showed only the pre-existing untracked docs.json. My scripts and outputs are in /private/tmp/claude-501/-Users-grey-Desktop-dev-eventhorizon/b529e6d4-a357-482a-ba81-d6830b9a174d/scratchpad/waive/audit-rest/scratch-range-flat-challenge/ (revised.py, rtests.py, revised_scan.py, e2e.py, live.py, corpus*.py, and the dump and corpus outputs).

## revised rule
Adopt the proposed rule. It extends discount_range_in_quote in verify.py, keeps the endpoint-intersection rule, and rejects with RANGE_REASON on eligibility.discount_tiers only.

Endpoint-only matching is enough. A stored percent that is not an endpoint (say 40 from "15-75%") is already rejected by value_in_quote, unless that number happens to appear elsewhere in the quote.

Optional hardening, tested. On today's data it reads exactly the same numbers as the proposed rule (5 published hits, 0 extra reads on any of the 3,184 fields). It also covers range wordings found in stored hospital documents that the proposed rule misses. Apply each pattern to normalize(quote), with:
- N = (\d+(?:\.\d+)?)
- lookbehind (?<![\d.,$]) before each N (the proposal's (?<![\d.]) lets "$1,000 - 20% discount" read as 0-20)
- P = (?:\s*%|\s*percent\b|\s*per cent\b)
- NOUN = (?:discounts?|reductions?|reduc\w+|write[- ]?offs?|adjustments?)
- SPAN = between\s+N P? \s*(?:-|to|and)\s* N P | N P? \s*(?:-|to|through)\s* N P. "and" counts only after "between", because "when 90% and 80% discounts apply" is a list of two discounts, not a range.

Patterns:
(a) SPAN\)?\s*(?:discount|reduction|adjustment|off\b|write). Catches "a 15-75% discount", "a 15 to 75 percent discount", "(35%-75%) adjustment" (Johns Hopkins Medicine).
(b) \bNOUN\s+FILLER(?P<ranging>(?:that\s+)?(?:ranging|ranges?)\s+)?(?:of\s+|from\s+|(?=between))SPAN, where FILLER = optional (?:(?:will|can|may|shall)\s+(?:be\s+)?|is\s+|are\s+|var(?:y|ies)\s+|off\s+(?:of\s+)?(?:the\s+)?(?:gross\s+|total\s+)?(?:charges|balance)\s+). Catches "a discount of 15-75%", "discounts ranging from 40 to 90 percent", "the discount will be between 25% and 75%", "the discount varies between 25 and 100 percent", "discounts off charges from 60 percent to 96 percent", "adjustments ranging from 35-100%", "the adjustment can be from 20% to 100%".
(c) the proposal's scale pattern, with P and an optional "fee " after "sliding". Catches "a discount from a scale of 10% to 75%".

Keep the pair only when max <= 100, and when low < high. Allow high-to-low order only when the "ranging" group matched: "discount ranging from 100 % to 60 %", "discounts ranging between 95% and 5%".

Outcome is unchanged: the discount_tiers field is dropped with RANGE_REASON, the sheet stays published while its free-care band verifies, and free_care_max_fpl is never touched, so decision B is unaffected.

## candidate code
```python
import re
from decimal import Decimal

from waive.atlas.verify import discount_range_in_quote, normalize

_NUM = r"(\d+(?:\.\d+)?)"
_DISCOUNT_NOUN = r"(?:discounts?|reductions?|reduc\w+|write[- ]?offs?)"
# "a 15-75% discount" (Hartford HealthCare, 070011, 070017, 070021, 070035): the percent sign is
# written once, after the second number, so _RANGE_AFTER's first _PERCENT never matches and only
# "75% discount" is read, as a fixed discount.
_RANGE_ONE_SIGN_AFTER = re.compile(
    rf"(?<![\d.]){_NUM}\s*(?:-|to)\s*{_NUM}\s*%\s*(?:discount|reduction|off\b|write)"
)
# "a discount of 15-75%". A connective is required: a table header "charity discount 0 - 100% 100%"
# (Atrium, 360076) is followed by an income band, not by the discount.
_RANGE_ONE_SIGN_BEFORE = re.compile(
    rf"\b{_DISCOUNT_NOUN}\s+(?:(?:that\s+)?(?:ranging|ranges?)\s+)?(?:of|from|between)\s+"
    rf"(?:between\s+)?(?<![\d.]){_NUM}\s*(?:-|to|and)\s*{_NUM}\s*%"
)
# "a discount from a scale of 10% to 75%" (Holton, 171319): a scale phrase between the discount
# word and the range, which _RANGE_BEFORE does not allow.
_RANGE_ON_SCALE = re.compile(
    rf"\b{_DISCOUNT_NOUN}\s+(?:(?:from|on|based on|according to)\s+)?(?:a|the)\s+"
    rf"(?:sliding[- ]?)?scale\s+(?:(?:ranging\s+)?from\s+|of\s+)?"
    rf"{_NUM}\s*%?\s*(?:to|-|through)\s*{_NUM}\s*%"
)


def candidate_range_in_quote(quote: str) -> set[Decimal]:
    found = set(discount_range_in_quote(quote))
    text = normalize(quote)
    for pattern in (_RANGE_ONE_SIGN_AFTER, _RANGE_ONE_SIGN_BEFORE, _RANGE_ON_SCALE):
        for match in pattern.finditer(text):
            low, high = Decimal(match.group(1)), Decimal(match.group(2))
            # A discount range runs upward and never past 100; "201-300%" is an income band.
            if low < high <= 100:
                found |= {low, high}
    return found


def rejects(path: str, value, quote: str) -> bool:
    """verify_sheet line 1232 with the candidate in place of discount_range_in_quote."""
    if path != "eligibility.discount_tiers" or not isinstance(value, list):
        return False
    percents = {Decimal(str(t["discount_percent"] if isinstance(t, dict) else t.discount_percent)) for t in value}
    return bool(candidate_range_in_quote(quote) & percents)
```

## tests
- reject=True value='tiers [250-550: 75] (070011/070021/070035)' quote='(ii) If family income is verified between 250% and 550% of the Federal\nPoverty Level Guidelines, the patient will qualify for a 15-75%\ndiscount against the patient’s account balance.'
- reject=True value='tiers [250-550: 15] (070017)' quote='(ii) If family income is verified between 250% and 550% of the Federal Poverty Level Guidelines, the patient will qualify for a 15-75% discount against the patient’s account balance.'
- reject=True value='tiers [100-200: 75] (171319)' quote='Patients with a household income of 100% – 200% of the FPL may qualify for a discount from a scale of 10% to 75%.'
- reject=True value='tiers [100-200: 10]' quote='Patients with a household income of 100% – 200% of the FPL may qualify for a discount from a scale of 10% to 75%.'
- reject=True value='tiers [15]' quote='a discount of 15-75% off the balance'
- reject=True value='tiers [25]' quote='a discount on a sliding scale from 25% to 75%'
- reject=True value='tiers [80]' quote='a discount based on a sliding scale of 20%-80%'
- reject=False value='tiers [40] (not an endpoint; existing intersection semantics)' quote='will qualify for a 15-75% discount'
- reject=False value='tiers [75]' quote='Patients 201-300% FPL receive a 75% discount'
- reject=False value='tiers [75]' quote='201-300% discount 75%'
- reject=False value='tiers [75] (Day Kimball shape)' quote='0-100% of federal poverty level 100% discount ii. 101% - 200% of federal poverty level 75% discount'
- reject=False value='tiers [95] (HealthSource Saginaw shape)' quote='- 100% financial assistance for income levels at or below 250% - 95% financial assistance for income levels between 251% and 275%'
- reject=False value='tiers [60]' quote='receive a 60% discount'
- reject=False value='tiers [90,80,75] (Buchanan County table)' quote='FPL 0-100% 100% 101-140% 90% 141-160% 80% 161-180% 75%'
- reject=False value='tiers [24]' quote='a discount of 2023-24% applies'
- reject=True value='tiers [55] (existing Alliance case kept)' quote='may qualify for a reduction of 55% to 75% of total charges.'
- reject=True value='tiers [25] (existing ACMH case kept)' quote='may be eligible for a discount of at least 25%.'
- reject=True value='tiers [30] (existing case kept)' quote='a 30% to 50% discount off charges'

## scan hits
- 070011 eligibility.discount_tiers correct=True: Charlotte Hungerford (Hartford HealthCare FAP fap-dc06d545589e6a88). The quote says '15-75% discount' and the sheet stores a flat 75 for 250-550% FPL. Flagged and upheld. Candidate reads {15, 75}.
- 070017 eligibility.discount_tiers correct=True: MidState (same Hartford HealthCare FAP). The quote says '15-75% discount' and the sheet stores a flat 15 for 250-550% FPL. Flagged and upheld. Candidate reads {15, 75}.
- 070021 eligibility.discount_tiers correct=True: Windham (same Hartford HealthCare FAP). The sheet stores a flat 75 from '15-75% discount'. Flagged and upheld.
- 070035 eligibility.discount_tiers correct=True: Hospital of Central Connecticut (same Hartford HealthCare FAP). The sheet stores a flat 75 from '15-75% discount'. Flagged and upheld.
- 171319 eligibility.discount_tiers correct=True: Holton (page web-3810d0fb508c726c). The quote says 'a discount from a scale of 10% to 75%' and the sheet stores a flat 75 for 100-200% FPL. Flagged and upheld. Candidate reads {10, 75}.

## challenge false positives

## challenge misses

## one-offs

## notes
The rule catches all five flagged sheets and nothing else, so one_offs is empty.

How it was tested:
- **Published fields:** scanned all 3,184 fields across the 605 sheets in published-fields.jsonl. The rule rejects exactly 5 fields, the five flagged discount_tiers. No other field is touched; the rule applies to the tiers path only, and no non-tier quote contains a newly read range anyway.
- **End to end on the database (read-only):** loaded the latest sheet for all 605 CCNs. Each was grounded the way withdraw_rejected does it (trim_quotes + prune_lists), then run through verify_sheet twice, once with the current function and once with the candidate. Only those 5 sheets change, each losing eligibility.discount_tiers with RANGE_REASON. decide_status after drop_fields stays PUBLISHED with no sheet_inconsistencies: the Hartford HealthCare sheets keep free_care_max_fpl 250 and Holton keeps 100.
- **Existing unit tests:** tests/unit/test_verify.py passes 52/52 with the candidate swapped in through a scratch pytest plugin, and 52/52 without it. A control test confirmed the swap was active.
- **Near misses that stay accepted:** Atrium (360076) and Miami Valley (360051) have a table header "charity discount 0 - 100% 100%"; the required connective in pattern (b) keeps it out. Day Kimball (070003) has "0 - 100% ... 100% discount" and HealthSource Saginaw (230275) has bulleted "- 95%". The current patterns already read income bands (301-350, 151-300) for Ascension, Wamego, Memorial (281339) and Jackson (010024). That is harmless because those numbers never equal a stored discount percent. The new <=100 guard covers only the new patterns.

Follow-ups for a person; the rule only removes the wrong number:
- **Hartford HealthCare (070011, 070017, 070021, 070035):** the exact percentages are in FAP Appendix A, which is missing from the stored 32.7k-character text (it is only referenced, at lines 89, 163, 171 and 377). Find and store Appendix A, then rebuild. Until then these sheets publish without a discount table.
- **Holton (171319):** the hospital's stored application form (web-5b563d576d9d1bea, FINANCIAL-ASSISTANCE-APP-2026-1.doc) has the stepped table: 100% FPL gets 100%, 125% gets 75%, 150% gets 50%, 175% gets 25%, 200% gets 10%. A rebuild that quotes that table instead of the summary page should publish the correct tiers.
- **Optional prompt change (not mechanical):** structure.py rule 5 could say that a band whose discount is given as a range gets no number. Today the model picks an endpoint, and the caster only catches range strings.

Housekeeping: I edited no repo file and wrote nothing to the database. I ran with PYTHONDONTWRITEBYTECODE=1 and pytest's cache off. Scratch scripts are in /private/tmp/claude-501/-Users-grey-Desktop-dev-eventhorizon/b529e6d4-a357-482a-ba81-d6830b9a174d/scratchpad/waive/audit-rest/scratch-range-flat/ (candidate.py, tests.py, scan.py, e2e.py, status.py). An untracked file, docs.json (9.9 MB, created 21:49), appeared in the repo root during this run. I did not create it; another process probably did.
