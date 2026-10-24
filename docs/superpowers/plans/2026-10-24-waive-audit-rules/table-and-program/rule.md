# table-and-program

## why it passed
Each sheet passed because no current check reads the thing that was wrong. Line numbers are in src/waive/atlas/verify.py unless publish.py is named.

(1) MedStar 210018/210062, wrong column. value_in_quote (L951-963) only asks whether each tier's numbers appear somewhere in the quote, and 80 appears in the row "201% to 250% 75% % 80%". Nothing reads table columns. bands_under_another_state (L343) relies on _STATE_HEADING (L324-327), which needs "for|in|at <full state name> facilities/hospitals". The header "Washington Hospitals..." has no for/in/at in front of it, and it would be read as WA, not DC.

(2) 210032, hardship tiers. All the numbers are present (401, 450, 40, 451, 500, 35). No tier check reads conditions: quotes_patient_share (L131), tiers_rise_with_income (L171) and discount_range_in_quote (L155) never look at "with a financial hardship".

(3) Henry Ford 230047/230053. The quote "Household Income Discount Off of Balance Due ..." does match _PATIENT_SHARE ("balance due"), but _DISCOUNT_WORDS ("discount") cancels it, so quotes_patient_share returns False. The heading "Insured Patients: For qualifying insured patients ... table below" sits just before the quote in the source. verify_sheet (L1166-1250) uses the source only for quote_found and never reads the text around the quote.

(4) 171350, ceiling 70. ceiling_problem (L686-706) passes "fall below Level 1, 70% of FPL, will receive a 100% discount": 70 is tied to the FPL, there is no floor, the 100 is not tied to the poverty level so it is no "higher limit", and "discount" counts as an assistance word. Nothing asks whether a ceiling quote is really a free-care line. publish.sheet_inconsistencies (L144-175) compares only the free limit with the first tier (L164) and checks free > 400 when there is no table; it never compares the ceiling with the sheet's own tiers (which go to 200) or with its free limit. Because the sheet has tiers, _ceiling_alone is False, so the ceiling branch of document_conflicts is skipped too.

(5) 110069 Emory. The stored quote, clause IV.C.2.b (200%), passes every free-care check. The uninsured 201-350% "100% financial assistance" clause (IV.C.2.c) is in the same document. publish.document_conflicts skips the free limit's own document (publish.py L215, `if free is not None and source.id == free.source_id: continue`). Its free_care_limits_stated would also be too broad to apply there: 52 of 578 own documents name a higher number in some free-care sentence. withdraw_rejected never calls document_conflicts in any case.

(6) 260102, free care. _free_limit_stated passes ("0-100% FPL" ends a range). free_limit_not_free (L739-743) knows only sliding|prorat|denied. Nothing reads "patient service fee", "$5" or "$10". On the tiers: 301/400/25 are present. discount_range_in_quote treats "an additional 25% discount" as a fixed 25%. sheet_inconsistencies has no gap check, so the hole from 100 to 300 passes.

(7) 160104. "An IL UPH Hospital shall provide ..." names the state by postal code, with an article and the system's acronym. _STATE_HEADING matches only full state names after for/in/at, so _state_segments finds no IL segment and bands_under_another_state returns False. names_other_states_program (L746) knows only NJ Charity Care.

## rule
Ten predicates (R1-R10). Every flagged sheet in this pattern is caught by at least one. A scan of all 605 sheets hits 17 fields on 16 sheets, all correct catches, with no false positives. Where to call them:

verify.verify_sheet, path == "eligibility.discount_tiers" (new elif branches after RANGE_REASON; each rejects the field):
- R1 tiers_need_hardship: a sentence of the quote that states a tier's discount_percent also says "financial|medical hardship", "medical debt" or "catastrophic". Reason: the band needs a hardship, so it is not an income band.
- R2 tier_is_additional: the quote says "an additional N% discount/reduction/off/adjustment" and N is a stored tier percent. Reason: the discount stacks on another one.
- R9 insured_only_table: needs documents[cited.source_id]. Look at the 250 normalized characters of source before the quote. Reject when the last insured-word in that text is a bare "insured" introducing the table ("Insured Patients:" heading, or "for (qualifying|eligible|all) insured patients"), and the quote itself never says uninsured, underinsured or self-pay. Not counted: "uninsured and insured", or the AGB clause's "billed to commercially insured patients". Reason: the table is for insured patients only.
- R10 wrong_regulated_column: only when the quote contains both "hscrc-regulated" and "non-hscrc". For each tier, read its band row ("201% to 250% 75% % 80%"). If the row has two or more distinct percentages, a Maryland hospital must store the first (HSCRC) column and any other state's hospital the last column. Single-value rows are skipped. Reason: wrong table column for this hospital. This one is specific to the MedStar system.

verify_sheet, path == "eligibility.free_care_max_fpl" (reject the field):
- R3 free_band_charges_patient: the quote still charges the patient. Matches "patient|service|visit|flat fee(s)", "nominal payment/fee/amount/charge" (not followed by waived/collected/due), "$N per|/" (not "per person/member/family/..."), or "visit|department|room|clinic $N". Negated forms ("no …", "without …", "waived …") do not count.
- R8 full_help_band_above(value, documents[cited.source_id]): the field's own document has a sentence giving a band whose start is above value and at most value+1, whose end is above value, and which is followed within 120 characters, with no other number between, by "100% financial assistance / charity / discount / adjustment / write-off / reduction" or "free care / free of charge / no cost / at no charge". Sentences matching _HEDGED_UP_TO_FULL are skipped, per decision B. All 4 current hits have no other income rule, so rejecting the field holds the sheet. I recommend verify_sheet because withdraw_rejected re-runs it but never runs document_conflicts. The equivalent at build time is an own-document check added to publish.document_conflicts.

verify_sheet, path == "eligibility.assistance_ceiling_fpl" (extend ceiling_problem with a new reason; rejects the field):
- R4 ceiling_quotes_free_band: the quote matches _FREE_SENTENCE and matches none of _HEDGED_UP_TO_FULL, _UP_TO_FULL or _CEILING_WORDING. Reason: the quote states the free-care band, not the ceiling. Decision B is preserved: a ceiling quoted as "may … up to 100%" passes.

verify_sheet, INCOME_LIMIT_PATHS branch (reject with OTHER_STATE_REASON):
- R7 names_other_state_facility_by_code: run on the original-case quote, not the normalized one. Matches a US postal code in capitals, then up to two capitalized words, then hospital(s) / facility/facilities / entity/entities / location(s), where the code differs from sheet.hospital.state ("An IL UPH Hospital"). Lower-cased codes would collide with ordinary words (in, or, me, oh, pa).

publish.sheet_inconsistencies (holds the sheet; reached both by builds and by the withdraw_rejected recheck through decide_status):
- R5 ceiling_below_own_rules: assistance_ceiling_fpl is below the sheet's own free_care_max_fpl or below the max_fpl_inclusive of any of its own tiers.
- R6 income_bands_gap: the first tier's min_fpl_exclusive is more than free_care_max_fpl + 1, or one tier starts more than 1 point above the previous tier's max. The 1-point allowance covers band labels like "171%".

## challenge (needs_change)
I re-ran the engineer's scan and got the same 19 firings. Of the 6 hits outside the flags, four hold up: 230092 and 230302 (Henry Ford, the same insured-only table), 110129 (Emory, the same clause) and 361309 (the source's own table shows 101-200% at 100%; the sheet starts its tiers at 200). 330307 is right too: nominal payments apply at ≤100% FPL. The other two are false positives.

1. R8 false positives. Enloe 050039 and Lima 360009 are held for reasons that are not true. Enloe's 151-300% band is a $100-per-visit co-pay band. Lima's sentence waives an insured patient's co-pays after insurance. Both published values are correct for what they quote. Skipping cost-sharing, insured-only and fee sentences fixes this.

2. Latent false positives in two rules. They fire on nothing in today's quotes, but they would on a rebuild.
   - R7's postal-code pattern collides with stored documents: Northwestern's 'NM … Hospitals' (IL sheets), Northern Light's 'CA Dean Hospital' and 'AR Gould Hospital' (ME sheets), 'a VA facility' (Veterans Affairs), and a capitalised 'OR'. Requiring a determiner and an all-caps acronym leaves only UnityPoint's Schedule B.
   - R4 rejects valid ceilings that state the free band and the ceiling in one sentence. I found 58 such sentences in the corpus, for example 'a discount if ≤400% …, and free care if <150%' and '100% discounts up to 600% and lower discounts up to 800%'. R4' adds a no-partial-help condition and still catches 171350.
   - Smaller fixes: R3 fires on 'patient fees are waived', and R1 would fire on MassHealth's 'Medical Hardship Family Countable Income'.

3. Dropping a field makes two sheets worse. The engineer says R5 is the safety net for 171350, but R5 runs on the sheet after R4 has dropped the ceiling, so it never sees it. 171350 would then publish tiers 70-200 with no band below 70%, where the document gives 100%. R7 on 160104 has the same effect: tiers from 200% with nothing below it, though the published 200 was the right value. Both rejections need to hold the sheet.

4. Misses in this same pattern.
   - 220116 Tufts and 220046 Berkshire store one column of an inpatient/outpatient × hospital table. The new R11 is narrow and catches only these two tables in all 833 documents.
   - Not caught by any rule: Lima's uninsured band, which is free care to 200% under decision B; Henry Mayo's bill-size × assets grid; the Ascension Seton and North Texas discretionary nominal fees, which need an owner call.
   - Kaiser CA tiers quoted from the dental (DFA) policy (9 sheets): is_dental_program already returns True for that document, so the existing DENTAL_REASON set-aside rejects them on the next recheck.

5. The nine scouted states. No rule, old or revised, fires wrongly on their 12 sheets. 090011 DC correctly passes R10 with the Washington column. 291314's 170 to 171 step passes R6.
   - Two other-pattern defects there, for the hedged/ceiling owner: 351328 ND stores free care 200 from 'Discounts apply through 200%', where the policy describes a sliding scale up to 100%. 301309 NH stores free care 400 from an eligibility line that introduces 'free or discounted care'.

Scripts (read-only, nothing in the repo touched), all in /private/tmp/claude-501/-Users-grey-Desktop-dev-eventhorizon/b529e6d4-a357-482a-ba81-d6830b9a174d/scratchpad/waive/audit-rest/scratch-table-and-program-challenge/:
- revised.py: the predicates
- tests_revised.py: 32 tests, all pass
- final_scan.py: the revised scan
- r8.py, r4_test.py, r4_docs.py, scan_all.py, r1_docs.py, r3_docs.py, r11b_test.py, r9_leads.py: the corpus checks

## revised rule
Keep R2, R5, R6, R9 and R10 as proposed. Narrow R1, R3, R4, R7 and R8, add R11, and make the R4/R7 rejections hold the sheet.

- R1' tiers_need_hardship: as proposed, but use `\b(?:financial|medical) hardship\b(?! family countable)|\bmedical debt\b|\bcatastrophic\b`. Otherwise MassHealth's income measure 'Medical Hardship Family Countable Income' in an HSN partial-tier sentence would count as a hardship condition.

- R3' free_band_charges_patient: as proposed, but a fee match is also ignored when, within 3 words, it is followed by '(is|are|will be|shall be) waived' or 'not (be) charged|collected|required|apply'. Without this, 'patient fees are waived' fires.

- R4' ceiling_quotes_free_band (assistance_ceiling_fpl): as proposed, plus one more condition. After removing the _FREE_SENTENCE match, the quote must state no partial help: `\bsliding\b|\bpartial\w*|\breduced\b|\breduction in charges\b|\bdiscounted (care|charges?|fees?|rates?|services)\b|\bdiscounts?\b(?! of 100)` or a percentage below 100 followed by discount/reduction/adjustment/write/financial assistance/charity/of AGB/of charges. A sentence that gives both the free band and the ceiling stays a valid ceiling. Decision B is untouched.

- R7' names_other_state_facility_by_code (INCOME_LIMIT_PATHS, original case): `\b(?:[Aa]n?|[Tt]he|[Ee]ach|[Aa]ny|[Aa]ll|[Ee]very|[Ss]uch) (<postal codes>) [A-Z]{2,6} (?:[Hh]ospitals?|[Ff]acilit(?:y|ies)|[Ee]ntit(?:y|ies)|[Ll]ocations?)\b`, firing when the code differs from the hospital's state. It requires a determiner and an all-caps system acronym, as in 'An IL UPH Hospital' and 'Each IL UPH Hospital'. In all 833 stored documents it matches only UnityPoint's Schedule B. If you prefer no rule, treat 160104 as a one-off requote to §2.3.

- R8' full_help_band_above (free_care_max_fpl, the field's own document only): as proposed, but skip any sentence that:
  - matches _HEDGED_UP_TO_FULL;
  - names cost sharing: `\bco-?pays?\b|\bco-?payments?\b|\bco-?insurance\b|\bdeductibles?\b|\bout[- ]of[- ]pocket\b`;
  - is about insured patients only: `\b(?:have|has|with) (?:health )?insurance\b|(?<!un)(?<!under)\binsured patients?\b|\bafter insurance\b`;
  - contains an R3 fee match.
  It now hits only the two Emory sheets, 110069 and 110129.

- R11 tiers_split_by_service (new; discount_tiers): reject when the quote has service columns, meaning `\bin-?patient:? out-?patient\b` (a header) or `\bin-?patient discount\b.{0,400}?\bout-?patient discount\b`. Reason: 'the table gives different discounts for inpatient and outpatient care'. This mirrors FREE_LIMIT_SPLIT_REASON. It hits 220116 and 220046, the only two such tables in the corpus. 'Inpatient and outpatient services will be discounted by …' does not match.

- Hold, don't just drop: a rejection with the R4' or R7' reason must add a hold, both in pipeline._primary (as PATIENT_SHARE_REASON does) and in withdraw_rejected.
  - Dropping alone publishes a worse sheet. 171350 would publish tiers 70-200 with nothing below 70%, where the document gives a 100% discount. R5 cannot fire, because sheet_inconsistencies runs on the sheet after the drop. 160104 would publish tiers from 200% with nothing below, while the same tier quote says '0-200% of FPIG: 100% discount off AGB'.
  - Do not add a general 'no free limit and the first tier starts above 0' hold. It would also hold 19 other published sheets, among them 070003, which the audit accepted.

Revised scan: 19 firings on 17 fields across 16 sheets, with no known false positives:
  - Flagged (11 fields): 210018, 210062, 210032, 230047, 230053, 171350 (R4' and R5), 110069, 260102 (free care and tiers), 160104.
  - Unflagged, but the same error under an upheld flag: 230092, 230302, 110129.
  - Not audited, checked by hand: 330307 (nominal payment at ≤100%), 361309 (the 101-200% band at 100% is missing), 220116, 220046.
  - 050039 and 360009 are gone.

Still one-offs for a person: 360009 (uninsured free care to 200 under decision B), 050624 (the grid), 210018/210062 if you would rather fix them by hand than keep the MedStar-only R10 (set the 200-250 tier to 75), and the Ascension and North Texas discretionary nominal fee, which needs an owner call.

## candidate code
```python
"""Candidate predicates for the table-and-program misreads (scratch; not repo code)."""
import re
from decimal import Decimal

from waive.atlas.states import US_STATES
from waive.atlas.verify import (
    _CEILING_WORDING, _FREE_SENTENCE, _HEDGED_UP_TO_FULL, _SENTENCES, _UP_TO_FULL,
    _number_in, _poverty_text, normalize,
)

# R1 (discount_tiers): hardship-conditioned band
_HARDSHIP = re.compile(r"\b(?:financial|medical) hardship\b|\bmedical debt\b|\bcatastrophic\b")
_QSENT = re.compile(r"(?<=[.;])\s+|\n+")

def tiers_need_hardship(quote, tiers):
    for sentence in _QSENT.split(quote):
        text = normalize(sentence)
        if _HARDSHIP.search(text) and any(_number_in(Decimal(t.discount_percent), text) for t in tiers):
            return True
    return False

# R2 (discount_tiers): stacked "additional N%" discount
_ADDITIONAL = re.compile(
    r"\b(?:an? )?additional (\d+(?:\.\d+)?) ?(?:%|percent) (?:discount|reduction|off\b|adjustment|write)"
)

def tier_is_additional(quote, tiers):
    stated = {Decimal(m.group(1)) for m in _ADDITIONAL.finditer(normalize(quote))}
    return bool(stated & {Decimal(t.discount_percent) for t in tiers})

# R3 (free_care_max_fpl): free band still charges the patient
_CHARGES_PATIENT = re.compile(
    r"\b(?:patient|service|visit|flat) fees?\b"
    r"|\bnominal (?:payments?|fees?|amounts?|charges?)\b(?! (?:will be |is |are )?(?:waived|collected|due))"
    r"|\$ ?[1-9][\d,]*(?:\.\d\d)? ?(?:/|per\b)(?! ?(?:additional |each |extra )?(?:person|member|family|individual|dependent|household))"
    r"|\b(?:visit|department|room|clinic)s?\b:? ?\$ ?[1-9]"
)
_NEGATION_BEFORE = re.compile(r"\b(?:no|without|waives?|waived|not)\b(?: \S+){0,2} ?$")

def free_band_charges_patient(quote):
    text = normalize(quote)
    return any(
        _NEGATION_BEFORE.search(text[: m.start()]) is None for m in _CHARGES_PATIENT.finditer(text)
    )

# R4 (assistance_ceiling_fpl): ceiling quote is a definite free-care line; decision B hedged form passes
def ceiling_quotes_free_band(quote):
    text = normalize(quote)
    if _HEDGED_UP_TO_FULL.search(text) or _UP_TO_FULL.search(text) or _CEILING_WORDING.search(text):
        return False
    return _FREE_SENTENCE.search(text) is not None

# R5 (sheet_inconsistencies)
def ceiling_below_own_rules(ceiling, free, tiers):
    if ceiling is None:
        return False
    tops = [Decimal(t.max_fpl_inclusive) for t in (tiers or [])]
    if free is not None:
        tops.append(Decimal(free))
    return any(Decimal(ceiling) < top for top in tops)

# R6 (sheet_inconsistencies)
def income_bands_gap(free, tiers):
    if not tiers:
        return False
    ordered = sorted(tiers, key=lambda t: t.min_fpl_exclusive)
    if free is not None and ordered[0].min_fpl_exclusive > Decimal(free) + 1:
        return True
    return any(b.min_fpl_exclusive > a.max_fpl_inclusive + 1 for a, b in zip(ordered, ordered[1:]))

# R7 (INCOME_LIMIT_PATHS): other state's postal code heading a facility, ORIGINAL case
_CODES = "|".join(US_STATES)
_CODE_FACILITY = re.compile(
    rf"\b({_CODES})\b(?: [A-Z][A-Za-z]{{1,10}}){{0,2}} (?:[Hh]ospitals?|[Ff]acilit(?:y|ies)|[Ee]ntit(?:y|ies)|[Ll]ocations?)\b"
)

def names_other_state_facility_by_code(quote, state):
    return any(m.group(1) != state for m in _CODE_FACILITY.finditer(quote))

# R8 (free_care_max_fpl + its own document): a 100%/free band starts just above the published limit
_NUM = r"(\d{2,3}(?:\.\d+)?) ?(?:%|percent)?"
_BAND = re.compile(
    rf"(?<![\d.$]){_NUM} ?(?:-|to|through|thru|and|but (?:less than or equal to|not (?:more|greater) than"
    rf"|no more than|up to)|and (?:up to|less than or equal to)) ?{_NUM} ?(?:%|percent)"
)
_FULL_AFTER = re.compile(
    r"^[^%\d]{0,120}?(?:\b100 ?(?:%|percent) (?:financial assistance|charity|discount|adjustment"
    r"|write[- ]?off|reduction|of (?:the )?(?:charges|balance)|free)|\bfree care\b|\bfree of charge\b"
    r"|\bno cost\b|\bat no charge\b)"
)

def full_help_band_above(value, document_text):
    v = Decimal(value)
    found = []
    for sentence in _SENTENCES.split(_poverty_text(document_text)):
        for m in _BAND.finditer(sentence):
            low, high = Decimal(m.group(1)), Decimal(m.group(2))
            if _HEDGED_UP_TO_FULL.search(sentence):
                continue  # decision B: "may ... up to 100%" is no free band
            if v < low <= v + 1 and high > v and _FULL_AFTER.search(sentence[m.end():]):
                found.append(sentence[max(0, m.start() - 100): m.end() + 140])
    return found

# R9 (discount_tiers + document): table introduced for insured patients only
_POPULATION = re.compile(r"\b(un-?|under-?)?insured\b")
_INSURED_LEAD = re.compile(
    r"(?:^|[.:;] |\bfor (?:qualifying |eligible |all )?)insured (?:patients?|individuals?|persons?)\b"
)
LEAD_CHARS = 250

def _locate(quote, document_text):
    nq, nt = normalize(quote), normalize(document_text)
    return nt, nt.find(nq[:80])

def insured_only_table(quote, document_text):
    if re.search(r"\b(?:un-?|under-?)insured\b|\bself[- ]pay\b", normalize(quote)):
        return False
    nt, at = _locate(quote, document_text)
    if at < 0:
        return False
    lead = nt[max(0, at - LEAD_CHARS): at]
    mentions = list(_POPULATION.finditer(lead))
    if not mentions or mentions[-1].group(1):
        return False
    last = mentions[-1]
    return _INSURED_LEAD.search(lead[max(0, last.start() - 30): last.end() + 12]) is not None and (
        re.search(r"\b(?:un-?|under-?)insured (?:and|or|&|/) ?$", lead[: last.start()]) is None
    )

# R10 (discount_tiers): MedStar HSCRC / non-HSCRC two-column rows
_PCT = re.compile(r"(?<![\d.$])(\d{1,3}(?:\.\d+)?) ?%")
_BAND_LABEL = r"(?<![\d.]){lo} ?%? ?(?:-|to|through|thru) ?{hi} ?%"
_NEXT_LABEL = re.compile(r"(?<![\d.])\d{1,3} ?%? ?(?:-|to|through|thru) ?\d{2,3} ?%")

def _row_values(text, tier):
    lo, hi = int(tier.min_fpl_exclusive), int(tier.max_fpl_inclusive)
    for start in (lo + 1, lo):
        m = re.search(_BAND_LABEL.format(lo=start, hi=hi), text)
        if m:
            rest = text[m.end():]
            nxt = _NEXT_LABEL.search(rest)
            return [Decimal(x) for x in _PCT.findall(rest[: nxt.start() if nxt else 60])]
    return None

def wrong_regulated_column(quote, state, tiers):
    text = normalize(quote)
    if "hscrc-regulated" not in text or "non-hscrc" not in text:
        return False
    for tier in tiers:
        values = _row_values(text, tier)
        if not values or len(set(values)) < 2:
            continue
        expected = values[0] if state == "MD" else values[-1]
        if Decimal(tier.discount_percent) != expected:
            return True
    return False
```

## tests
- reject=True value='R10, MD hospital, tier 200-250 at 80' quote='MedStar table: HSCRC-Regulated Services | Washington Hospitals, Hospital-Based Physician Practices, and non-HSCRC Regulated Services ... 201% to 250% 75% % 80% ...'
- reject=False value='R10, MD hospital, tier 200-250 at 75 (Franklin Square / Union Memorial / Harbor)' quote='same MedStar table'
- reject=False value='R10, DC hospital (090011), tier 200-250 at 80' quote='same MedStar table'
- reject=True value='R1, tiers 400-450 at 40, 450-500 at 35' quote='Patients with household income between 401-450% of FPL and with a financial hardship will receive a 40% adjustment. Patients with household income between 451-500% will receive a 35% adjustment.'
- reject=False value='R1, UnityPoint tiers 65/45/25/5' quote='2.3 Hospital bills will be further reduced by the following amounts ... 201-225% of FPIG : 65% discount off AGB ... 301-400% of FPIG : 5% discount off AGB'
- reject=True value='R2, tier 300-400 at 25' quote='D. 301-400% FPL: Services will be discounted by the current Uninsured Discount amount, with an additional 25% discount on the remaining balance'
- reject=False value='R2, tier 200-300 at 50' quote='Patients between 201% and 300% of FPL receive a 50% discount. Add $5,680 for each additional person.'
- reject=True value='R3, free_care_max_fpl 100' quote='0-100% FPL – patient service fee 1. Outpatient office visit $5 2. Emergency Department $10 3. Inpatient 100% financial assistance'
- reject=True value='R3, Cayuga free_care_max_fpl 100' quote='... equal to or less than 100% of the most recent Federal Poverty Guidelines qualifies for a nominal payment limit as designated by major service category, the rest will be written off.'
- reject=False value='R3, free 200' quote='For patients with incomes below 200% of the FPL, BronxCare will waive all charges and no nominal payment will be collected.'
- reject=False value='R3, free 200' quote='For eligible individuals with incomes up to 200% of the FPL: charges waived, no nominal payments due.'
- reject=False value='R3, free 200' quote='Family gross income at 0 to 200% of FPL Full Financial Assistance; $0 is billable to the patient.'
- reject=False value='R3, free 200' quote='HSHS FPL table ... add $5,680 per person for families larger than 8 people ... receives 100% discount'
- reject=False value='R3, Mizell free 200 (clinic copays only; deliberately not caught)' quote="If the patient's income is under 200% of the FPG ... approved for financial assistance at 100% with no patient financial responsibility except for any applicable clinic visit copays."
- reject=True value='R4, assistance_ceiling_fpl 70' quote='Patient/guarantors that fall below Level 1, 70% of FPL, will receive a 100% discount.'
- reject=False value='R4, ceiling 400 (decision B: stays a ceiling)' quote='You may receive a discount of up to 100 percent if your household income is at or below 400% of the Federal Poverty Level.'
- reject=False value='R4, ceiling 400' quote='Whether you are uninsured or insured, you may qualify for full or partial financial assistance if your household income is at or below 400% of the federal poverty level'
- reject=False value='R4, ceiling 400' quote='Generally, patients with family income at or below 400% of the federal poverty level will be eligible for charity (free) care or discounted (reduced) care.'
- reject=True value='R5, ceiling below top tier' quote='(structural) ceiling 70; tiers 70-100 at 95 ... 175-200 at 25'
- reject=False value='R5' quote='(structural) ceiling 400; free 200; tier 200-400'
- reject=True value='R6, gap 100 to 300' quote='(structural) free 100; tiers 300-400'
- reject=False value='R6, 1-point rounding' quote='(structural) tiers 100-170 at 75, 171-250 at 55'
- reject=True value='R7, Iowa hospital, free 200' quote='B. An IL UPH Hospital shall provide a charitable discount of 100% of its charges ... family income of not more than 200% of the federal poverty income guidelines.'
- reject=False value='R7, Illinois hospital' quote='same IL UPH quote'
- reject=False value='R7, OH hospital (IN/OR in capitals)' quote='CARE PROVIDED IN HOSPITAL FACILITIES OR CLINICS is covered for patients at or below 200% of FPL.'
- reject=True value='R8, free 200 against own document' quote='Emory FAP: ... uninsured is at least 201 % but less than or equal to 350% of the Federal Poverty Guidelines, the patient will receive 100% financial assistance ...'
- reject=False value='R8, free 200' quote='Income 0-200% of FPL: 100% discount. Income 201-300% of FPL: 75% discount. Income 301-400% of FPL: 50% discount.'
- reject=False value='R8, free 200 (decision B hedged band)' quote='Patients at or below 200% of the FPL receive free care. Patients between 201% and 300% of the FPL may receive a discount of up to 100%.'
- reject=True value='R9, Henry Ford tiers 75/50/25' quote="Household Income Discount Off of Balance Due 351% - 400% of the FPL 25% ... (source lead-in: 'Insured Patients: For qualifying insured patients ... discounted based on the table below')"
- reject=False value='R9, Heart of Texas tiers' quote="Family Income as % of FPG Discount Patient Liability ... (lead-in: 'amounts generally billed to ... commercially insured [or medicare] patients')"
- reject=False value='R9, Alexian Brothers tiers' quote='Uninsured Patient Sliding Scale % Insured Patient Sliding Scale % 0% - 250% FPL 100% ...'

## scan hits
- 210018 eligibility.discount_tiers correct=True: R10: a Maryland hospital stored 80 from the Washington/non-HSCRC column; the HSCRC column gives 75. Flagged.
- 210062 eligibility.discount_tiers correct=True: R10: same MedStar column error (80 instead of 75). Flagged.
- 210032 eligibility.discount_tiers correct=True: R1: the 401-450% band at 40% is 'with a financial hardship' (Maryland reduced-cost care). Flagged.
- 230047 eligibility.discount_tiers correct=True: R9: the source introduces the table with 'For qualifying insured patients'; uninsured patients have a separate Medicare-rate table. Flagged.
- 230053 eligibility.discount_tiers correct=True: R9: same Henry Ford insured-only table. Flagged.
- 230092 eligibility.discount_tiers correct=True: R9: Henry Ford Allegiance, not flagged: same policy, same insured-only 'Discount Off of Balance Due' table, same lead-in.
- 230302 eligibility.discount_tiers correct=True: R9: Henry Ford West Bloomfield, not flagged: same policy and the same error.
- 171350 eligibility.assistance_ceiling_fpl correct=True: R4 (quote is the '<70% … will receive a 100% discount' free line) and R5 (ceiling 70 is below the sheet's own top tier of 200). Flagged.
- 110069 eligibility.free_care_max_fpl correct=True: R8: the own document (IV.C.2.c) gives uninsured patients at 201-350% '100% financial assistance'. The sheet's only income rule, so it is held. Flagged.
- 110129 eligibility.free_care_max_fpl correct=True: R8: St Francis–Emory, not flagged: same Emory FAP clause, same omission of the uninsured 350% free band.
- 050039 eligibility.free_care_max_fpl correct=True: R8, a hold rather than a wrong value. The published 150 is the owes-nothing limit, but the page says 151-300% FPL is 'eligible for free care after a $100 per visit co-pay', and the sheet has no tiers, so a reader at 151-300% is told nothing applies.
- 360009 eligibility.free_care_max_fpl correct=True: R8, a hold. The sheet publishes only HCAP free care to 100%. The same policy gives insured patients at 101-200% a 100% discount on co-pays and deductibles, uninsured patients at ≤200% up to 100%, and 200-400% a 95% discount, and none of that is on the sheet.
- 260102 eligibility.free_care_max_fpl correct=True: R3: 'patient service fee … Outpatient office visit $5, Emergency Department $10' in the free band. Flagged.
- 330307 eligibility.free_care_max_fpl correct=True: R3, Cayuga, not flagged: at ≤100% FPL the patient 'qualifies for a nominal payment limit'. The source's nominal fee schedule is $150 per inpatient discharge and $15 per ED/clinic visit, so the free band is not $0, which is the 260102 problem again.
- 260102 eligibility.discount_tiers correct=True: R2 ('an additional 25% discount on the remaining balance') and R6 (gap from free limit 100 to first tier 300: the 101-300% band is missing). Flagged.
- 361309 eligibility.discount_tiers correct=True: R6, sheet hold, Henry County OH, not flagged: free stored as 100 and tiers start at 200, but the table says '101% - 200% 100%' discount from patient liability. The policy text says 101-400% is a sliding scale, so the document contradicts itself; an admin should decide (free care likely to 200).
- 160104 eligibility.free_care_max_fpl correct=True: R7: 'An IL UPH Hospital shall provide…' (Illinois-only Schedule B) quoted for an Iowa hospital. Flagged; requote §2.3.

## challenge false positives
- 050039 eligibility.free_care_max_fpl: R8 fires on the sentence 'families with incomes between 151% and 300% ... are eligible for free care after a $100 per visit co-pay'. That is a co-pay band, not full help. The published 150 is the correct owes-nothing limit ('less than 150% ... may be eligible for free care'). Rejecting it drops the sheet's only income rule and holds the sheet for a false reason. The fee in that sentence is what R3 itself treats as 'still charges the patient'.
- 360009 eligibility.free_care_max_fpl: R8 fires on 'For those patients who have insurance and do not qualify for HCAP but fall within 101-200% ..., they will be eligible for a 100% discount on any co-pay, co-insurance, and deductible after insurance'. That waives an insured patient's cost sharing only; it is not a free band. The published HCAP 100 is correct for the sentence it quotes. The sheet does have a real problem (see misses), but R8 does not detect it and would hold the sheet for a wrong reason.
- 140116 eligibility.free_care_max_fpl: Latent R7 false positive. No current quote triggers it, but a rebuild could. Northwestern Medicine's IL policy (fap-8a852ef2e6b5a7fd, also cited by 140130, 140211 and 140281) lists 'NM Huntley, NM McHenry and NM Woodstock Hospitals'. R7's pattern reads NM as New Mexico and would reject an Illinois income quote that names those hospitals as another state's.
- 200008 eligibility.free_care_max_fpl: Latent R7 false positive. Northern Light's ME policy (fap-67a499e0f8d95d53, also cited by 200018, 201300 and 201313) lists 'CA Dean Hospital' and 'AR Gould Hospital'. R7 matches both as other states' hospitals.
- 110129 eligibility.free_care_max_fpl: Latent R7 false positive. The Emory policy (fap-0e1bb40253c56386) and 260137's document (fap-34639576e4fc9c81) say 'a VA facility' and 'VA facilities', meaning Veterans Affairs. R7 reads them as Virginia facilities. Other collisions in stored documents: 'OR Aultman Alliance Hospital' (OR as a capitalised 'or', 361323), 'MD Hospital Medicine' (a doctor's credential) and 'IA Illinois Blessing Hospital' (an address line).
- 050122 eligibility.assistance_ceiling_fpl: Latent R4 false positive. R4 would reject the valid ceiling sentence 'You may qualify for a discount if your family income is at or below 400% ..., and for free care if your family income is below 150% ...' (billing-383525ccf5ae9d31), because it contains 'free care'. Similar valid ceilings R4 rejects: 330214 NYU, '100% discounts ... up to 600% FPL and lower discounts ... up to 800% FPL'; 201304 MDI, 'free care ... 200% or less ... reduced care to those at 201-250%'; 050352, 'sliding scale ... up to 400% of the FPL, as follows: full charity care ... 249%'; 140101, 'discounted charges ... under 600% ..., while ... free care ... under 200%'. In the corpus, 58 of the 251 ceiling-shaped sentences R4 calls free bands also state partial help.

## challenge misses
- 220116 eligibility.discount_tiers: Tufts Medical Center. The quote is a hospital × service table ('TM Tufts Medical Center Hospital Inpatient Discount 100% 49.50% 30% ... Outpatient Discount 69.50%'), and the sheet stores a single tier, 150-300% at 30%. 30% is at best one column, and outpatient care gets 69.50%. This is the same wrong-column defect as MedStar, and R10 (HSCRC only) misses it. The new R11 catches it.
- 220046 eligibility.discount_tiers: Berkshire Medical Center. The table header reads 'Berkshire Medical Center | Fairview Hospital | Inpatient Outpatient Inpatient Outpatient', with the row '0 – 400 46.07% 63.14% 48.58% 53.17%'. The sheet stores one tier, 0-400% at 46.07%, which is BMC's inpatient column only; outpatient is 63.14%. R11 catches it.
- 360009 eligibility.free_care_max_fpl: Lima Memorial. The same document says 'For patients without insurance, a discount of up to 100% will be extended to those whose family income is at or below 200% of the FPG', with 200-400% at a 95% discount. There is no 'may', so under decision B this is free care for uninsured patients up to 200%. The sheet publishes HCAP 100 and no tiers, which is Emory's problem again. R8 is not a band match here, so neither version catches it; send it to a person.
- 050624 eligibility.discount_tiers: Henry Mayo. Appendix C is a grid by bill size (<$1,000 … >$100,000) and net assets. The 201-400% rows read 75/100/50%, and even 0-200% is 85% or 75% for some asset bands. The sheet stores a flat 200-400% at 75 and free care to 200. Rows in this table are too irregular for a safe rule; treat it as a one-off.
- 670043 eligibility.free_care_max_fpl: Ascension Seton. The own document says 'patients that are eligible for financial assistance may be charged a nominal flat fee of up to $30 for services', but the free-care quote ('100% charity care ... ≤250%') does not mention it. R3 reads only the quote, so it misses this. The same document backs 451371, 451365, 450056, 450867, 671304, 670041, 450124, 450808, 100040 and 100361. North Texas MC (450090) 'reserves the right to bill and collect a reasonable copayment or nominal fee'. Both are discretionary ('may'), so the owner has to decide; this is not a mechanical reject.

## one-offs

## notes
Every flagged sheet in this pattern is caught by at least one tested rule. The scan of all 605 sheets / 3,184 fields gives 19 rule firings on 17 fields across 16 sheets. Of those, 11 fields are flagged and 6 are unflagged siblings or the same error elsewhere: 230092, 230302, 110129, 330307, 361309, and 050039/360009 as omission holds. No false positives remain. The first draft of R9 fired on Heart of Texas 451348 because of the AGB clause "billed to commercially insured patients"; requiring an insured lead-in ("Insured Patients:" or "for qualifying insured patients") fixed that.

All 31 tests pass, on real quotes and documents. Scripts are in /private/tmp/claude-501/-Users-grey-Desktop-dev-eventhorizon/b529e6d4-a357-482a-ba81-d6830b9a174d/scratchpad/waive/audit-rest/scratch-table-and-program/: candidate.py, tests.py, scan.py, and sources.json (a read-only dump of the 833 cited docs; one source_id has no row).

Caveats and decisions:
1. R10 is mechanical and safe, but it encodes one system's table (MedStar's HSCRC vs non-HSCRC columns; Maryland hospitals use the HSCRC column). If the owner prefers no system-specific rule, treat 210018/210062 as one-offs: set the 200-250 tier to 75. The 301-350 (40%) and 351-400 (20%) rows show a single value whose column is unclear, so R10 skips them; someone should check the PDF for all six MedStar sheets.
2. R8 keeps decision B: sentences of the form "may … up to 100%" never count as a full band. Rejecting the field holds all 4 current hits, because none has another income rule. Where a sheet does have tiers, a document_conflicts-style hold would be gentler. I put R8 in verify_sheet because withdraw_rejected never calls document_conflicts.
3. R3 deliberately does not fire on Mizell 010007 ("except for any applicable clinic visit copays"): hospital inpatient and outpatient care is 100% there, and only clinic copays apply. R3 needs a fee, a nominal payment, or a dollar amount per visit or day.
4. R4 and R5 overlap on 171350. R4 drops the ceiling and the sheet stays published with its tiers; R5 is the sheet-level safety net. Fix by hand: free_care_max_fpl should be 70 and assistance_ceiling_fpl 200.
5. After the rules run, these still need a person or a rebuild: 260102 needs the 101-300% band added (100% assistance minus flat fees); Henry Ford sheets cannot hold both the insured and uninsured tables in one discount_tiers field; Emory needs a separate 350% limit for uninsured patients; 160104 should be requoted to §2.3.
6. Outside this pattern: the 210018/210024 residency flags (northern Virginia stored as all of VA) are not covered here.
