# insured

## why it passed
verify_sheet runs two checks on eligibility.insured_patients_covered (src/waive/atlas/verify.py:1236-1243): quotes_only_the_agb_cap and quote_addresses_insured. value_in_quote passes every bool (verify.py:952-953).

(a) False values. quote_addresses_insured (304-311) asks for one _INSURED_WORDS hit (291) and one _INSURED_EXCLUDED hit (295-302) anywhere in the quote. Nothing ties the exclusion to insured patients.
- 010007: 'insurance' plus '\bineligible\b'. The exclusion is conditional and covers only people whose insurance is enough: "sufficient insurance coverage or assets ... may be deemed ineligible".
- 181322: 'underinsured', which names insured people as helped, plus '\bnot (?:be )?eligible\b' taken from "not eligible for a government program".
- 231306: 'insured' comes only from the price comparison "than insured individuals". The exclusion '(?:do|does) not qualify' is about uninsured patients who fail the FAP.

_INSURED_EXCLUDED has two more gaps. It counts descriptions of the uninsured ('no insurance', 'do not have health insurance', 'without insurance') as exclusions of the insured. It also never looks for underinsured / limited / inadequate / "with or without insurance" wording that names insured people as helped. In the other direction, its own 'only (to|for) uninsured' and 'must be uninsured' alternatives never fire: such quotes have no _INSURED_WORDS hit, because the lookbehind skips 'uninsured', so they return False at line 309.

(b) True values. Line 311 returns `value` as soon as any insured word appears, so any sentence that mentions insurance passes:
- 040004, a billing statement ('insured', 'out-of-pocket', 'insurance').
- 210027, payment plans ('insurance status').
- 241323, the bare heading 'Underinsured:' (matches under-?insured).

240187 got past quotes_only_the_agb_cap because _AGB_CLAUSE (line 225) matches only 'amounts generally billed' spelled out, not the acronym 'AGB'. Line 236 also tests the substring 'insur', which 'uninsured' contains, so a cap sentence about uninsured patients would slip through too.

The structurer prompt (structure.py SYSTEM_PROMPT) never defines insured_patients_covered, so the model cites any sentence that mentions insurance.

## rule
Field: eligibility.insured_patients_covered only. Where: in verify_sheet (verify.py:1236-1243), replace the two insured branches with one: `elif path == "eligibility.insured_patients_covered" and (reason := insured_field_problem(cited.quote or "", bool(cited.value))): report.rejected.append((path, reason))`. It runs after the shared checks (quote found, set-aside, value_in_quote). Outcome: reject the field. This is not an income rule, so the sheet is never held; withdraw_rejected (pipeline.py) republishes the affected sheets without the field.

All matching is on normalize(quote). Reject when any of these holds:

(1) Old AGB rule: quotes_only_the_agb_cap still applies, so nothing it rejects today gets through.

(2) Price cap, either value: delete every clause matching
`\b(charged|billed|pay|paid|owe|responsible to pay)\b[^.;]{0,15}?\b(more|greater|higher)\b[^.;]{0,80}?\bthan\b[^.;]*` or `amounts? generally billed[^.;]*`. If one matched and no _INSURED_WORDS hit is left in the rest, reject. This catches 'charged more than AGB to insured', 'charged more ... than patients covered by ... insurance' and 'pay more for care than insured individuals'.

(3) Bare label, either value: the whole quote matches `^[^.;:]{1,40}:$`, as in 'Underinsured:'.

(4) value True:
- no _INSURED_WORDS hit (today's rule); or
- the quote matches `\bpayment (plans?|arrangements?)\b` and has none of: financial assistance/aid, charity, free, discount*, write-off, written off, forgiv*, reduc*, sliding, FAP.

(5) value False: accept only when the quote says insured patients are left out, in a clause about them, and does not name insured people as helped.
- Required, _INSURED_LEFT_OUT: 'only (to|for) (the) uninsured' | 'uninsured (patients) only' | 'must (be|remain) uninsured' | '(limited|restricted) to uninsured' | 'only (to|for) patients (who do not have|with no|without) (health) insurance/coverage' | 'insured patients (are|will|do) (not|never) (be) (eligible|covered|qualify|considered)' | 'patients (with|who have) (health|private|commercial) insurance/coverage (are|will|do) not (be) (eligible|covered|qualify)' (these two not followed by 'for (the) uninsured/self-pay/true self') | 'not (available|offered|open|extended) to (insured|patients with insurance)' | 'exclud* (insured|patients with insurance)'.
- Forbidden, _INSURED_HELPED: 'under[- ]?insured' | '(limited|inadequate|insufficient|partial)( or no) (health) (insurance|coverage)' | 'with or without (an) (health) insurance' | '(sufficient|adequate|enough) insurance' | 'insurance (coverage) or assets'.
- This False branch drops today's _INSURED_WORDS precondition, which makes the 'only to uninsured' wording usable.

Effect on the published set: 26 of 3,184 fields are rejected, all on this path, and all 26 are correct catches. That is 18 False values (every False the set holds; each one is wrong against its source) and 8 True values with off-topic quotes. Six of the 7 flagged sheets are caught. Owner decision B is untouched: free_care_max_fpl is not involved.

## challenge (needs_change)
I reran the proposed predicate on published-fields.jsonl and got the same result: 26 of 3,184 fields rejected, 18 False and 8 True. 6 of the 7 flagged sheets are caught.

**Hits (all 26 are right).** I read every hit and checked the doubtful ones against their sources: Tufts, Cabell, Holyoke, Rawlins, Geisinger, Houston Methodist, Jackson Park, Montefiore, Hillsboro.
- None of the 18 False quotes says insured patients are left out of financial assistance. They are underinsured lists, 'may be deemed ineligible' conditionals, uninsured or self-pay definitions, the RWJBarnabas self-pay-rate line, Tufts' 'primarily', and MGB's public-programs sentence.
- All 8 True quotes are off-topic: AGB or price caps, payment plans, and the bare heading 'Underinsured:'.
- The published set has no false positives, including the nine scouted states.

**What still needs changing:**

1. **Same-defect misses in the published set.**
   - 040004 can be caught safely after all. Treating 'statement' as a payment term fires on this sheet and nothing else among all 1,648 insured fields stored in the DB. The proposal's reason for leaving it as a one-off does not hold.
   - 390006 (Geisinger quotes the uninsured bullet; the right bullet sits two lines later) and 450358 (an 'encouraged to purchase health insurance' sentence) are True values whose only insured word sits inside a no-insurance phrase. They are catchable once Maine's 'coverage that pays only part', 'limited or no', 'with or without' and 'portion of your bill' wording is excluded. That check also catches three draft sheets (261321, 250094, 521332).

2. **The False branch loosens in an unsafe way.** It drops the insured-word precondition, and its self-pay guard only looks at the words straight after 'not eligible'. These quotes would all pass as False:
   - 'Self-pay discounts are offered only to uninsured patients'
   - 'The uninsured discount is available only to uninsured patients'
   - 'not eligible for the 40% self-pay discount'
   - 'the prompt pay discount'
   - 'not only to the uninsured but also to insured patients'
   
   The first four are the wrong-program defect behind 231306, 141332 and 310041, reopened for future rebuilds. No stored quote is accepted this way today, but nothing in the rule stops it.

3. **The 'makes good False wording usable' claim is empty.** Among all 1,648 stored insured fields, the proposal newly accepts none. Real exclusions in drafts (111327, 180115) stay rejected. The revision accepts those two and nothing else.

4. **The price-cap deletion runs past ', and ...'.** That rejects good True quotes that add an insured clause after the cap.

5. **Wider-DB loss (draft, not published).** Cabell 510055 is a real False ('not eligible ... if ... reimbursable by any payor, including health insurance'). Today's code accepts it; the proposal and my revision both reject it. A pattern that accepted it would also accept McKenzie 351302's out-of-network-only exclusion. So I left it rejected, which drops only the field.

**Not caught by either version:** prompt-pay and 'contact your insurance provider' quotes (170200, 170185, 171316) and Aspirus's 'not a health insurance plan' (521300). These need a re-cite.

**Files.** I wrote everything only under /private/tmp/claude-501/-Users-grey-Desktop-dev-eventhorizon/b529e6d4-a357-482a-ba81-d6830b9a174d/scratchpad/waive/audit-rest/scratch-insured-challenge/:
- cand.py (the proposal as given)
- revised.py
- scan.py, allsheets.py, compare.py, accept_check.py, tests.py, probe_unins.py, ctx.py, ctx2.py
- scan_out.json, allsheets_out.json

I edited nothing in the repo, made no network calls, and opened the DB only through the read-only URI.

## revised rule
Same placement and outcome as proposed. In verify_sheet (verify.py:1236-1243), replace both insured branches with: `elif path == "eligibility.insured_patients_covered" and (reason := insured_field_problem(cited.quote or "", bool(cited.value))): report.rejected.append((path, reason))`. The field is rejected, the sheet is never held, and withdraw_rejected republishes without the field. All matching is on normalize(quote). Runnable code: /private/tmp/claude-501/-Users-grey-Desktop-dev-eventhorizon/b529e6d4-a357-482a-ba81-d6830b9a174d/scratchpad/waive/audit-rest/scratch-insured-challenge/revised.py

The proposal is kept except for these changes:

(A) Price cap, either value. The deleted clause stops before ', and / , but / , while / , however / , although':
_TAIL = `(?:(?!,\s*(?:and|but|while|however|although)\b)[^.;])*`
_PRICE_CAP_CLAUSE = `\b(?:charged|billed|pay|paid|owe|responsible to pay)\b[^.;]{0,15}?\b(?:more|greater|higher)\b[^.;]{0,80}?\bthan\b` + _TAIL + `|amounts? generally billed` + _TAIL
Reject if a clause matched and no _INSURED_WORDS hit is left after removing it. The old quotes_only_the_agb_cap still runs first. Why: the proposal's `than\b[^.;]*` swallows the rest of the sentence. It would reject a good True like 'Patients who qualify will pay no more than 10% of charges, and insured patients may also qualify.'

(B) Bare label, either value: `^[^.;:]{1,40}:$`. Unchanged.

(C) value True:
1. No _INSURED_WORDS hit: reject (as today).
2. Payment terms. _PAYMENT_ONLY = `\bpayment (?:plans?|arrangements?)\b|\bprompt[- ]?pay(?:ment)? discounts?\b|\b(?:payment|paid) in full\b|\b(?:billing )?statements?\b`. Reject if it matches and, after removing those phrases, none of the proposal's _ASSISTANCE_WORDS remain (financial assistance/aid, charity, free, discount*, write-off, written off, forgiv*, reduc*, sliding, fap). Removing the phrases first is what lets 'prompt pay discount' count as payment terms rather than assistance.
3. NEW: insurance named only as absent. _NOT_INSURED = `(?:do|does|did) not|don't|doesn't) have (any)(form of)(health|medical) (insurance|coverage)( coverage)` | `(has|have|with) no (health|medical) (insurance|coverage)( coverage)` | `without (any)(health|medical) (insurance|coverage)( coverage)` | `no (health|medical) insurance( coverage)` | `not insured` | `(purchase|obtain|buy|enroll in|apply for) (health) (insurance|coverage)`. Reject when _NOT_INSURED matches, _INSURED_HELPED does not match the whole quote, and after removing _NOT_INSURED there is no _INSURED_WORDS hit and no _OTHER_PAYER hit. _OTHER_PAYER = `\bcoverage\b|\bmedicare\b|\bthird[- ]party\b|\bpay[eo]rs?\b|\bcovered\b|\bportion of (the|your|their) bill\b`. This keeps Maine 'have no health insurance coverage or have coverage that pays only part of the bill', 'limited or no health insurance', 'with or without insurance', 'not insured or insured for only part' and 'do not have insurance and/or ... your portion of the bill'.

(D) value False: accept only when quote_excludes_insured holds, i.e. _INSURED_LEFT_OUT matches, _INSURED_HELPED does not, and NEW _NON_FAP_RATE does not. Otherwise reject.
- _NON_FAP_RATE = `\b(?:true )?(?:self[- ]?pay|uninsured|cash|prompt[- ]?pay(?:ment)?) (?:discounts?|rates?|prices?|pricing)\b|\bdiscounted cash price\b`. It replaces the proposal's _NOT_FOR_SELF_PAY lookahead, which only checks the words straight after 'not eligible' and so lets 'not eligible for the 40% self-pay discount', 'the prompt pay discount' and 'Self-pay discounts are offered only to uninsured patients' through.
- Changes to _INSURED_LEFT_OUT: add `(?<!not )` before both 'only ...' alternatives, so 'not only to the uninsured but also to insured patients' does not count. Change bare 'considered' to 'considered for'. Add `\b(?:cannot|can not|may not|must not) be covered by (?:any |private |health |commercial |third[- ]party )*insurance\b` and `\bmust be self[- ]?pay\b`.
- _INSURED_HELPED is unchanged.

Effect:
- Published set: 29 of 288 insured fields rejected, 0 false positives. That is all 18 False values plus 11 True: the proposal's 8, plus 040004, 390006 and 450358. All 7 flagged sheets are caught, 040004 included.
- Nine scouted states (CA FL IL MA NJ NY OH PA TX): 18 hits, each read against its quote and source. All are correct catches.
- All 1,648 distinct insured fields in every stored sheet version: newly accepted versus today are only two real exclusions, 111327 'Applicant cannot be covered by private insurance' and 180115 '*Must be self-pay'.
- 43 tests pass: the engineer's 26 plus 17 adversarial. The proposal passes 34 of 43.
- Owner decision B is untouched; free_care_max_fpl is never read.

## candidate code
```python
import re
from waive.atlas.verify import (_INSURED_WORDS, normalize, AGB_CAP_REASON, INSURED_REASON,
                                quotes_only_the_agb_cap)

_NOT_FOR_SELF_PAY = r"(?! for (?:the )?(?:uninsured|self[- ]?pay|true self))"
_INSURED_LEFT_OUT = re.compile(
    r"\bonly (?:to |for )?(?:the )?uninsured\b"
    r"|\buninsured (?:patients |individuals |persons )?only\b"
    r"|\bmust (?:be|remain) uninsured\b"
    r"|\b(?:limited|restricted) to (?:the )?uninsured\b"
    r"|\bonly (?:to |for )?(?:patients|individuals|persons|people|those) (?:who (?:do not|don't|does not) have"
    r"|with no|without) (?:any )?(?:health )?(?:insurance|coverage)\b"
    r"|(?<![\w-])insured (?:patients?|individuals?|persons?|people) (?:are|is|will|shall|do|does)(?: be)?"
    r" (?:not|never) (?:be )?(?:eligible|covered|qualify|considered)\b" + _NOT_FOR_SELF_PAY +
    r"|\b(?:patients|individuals|persons|people|those) (?:with|who have) (?:health |private |commercial "
    r"|third[- ]party )?(?:insurance|coverage) (?:are|is|will|shall|do|does)(?: be)? not (?:be )?"
    r"(?:eligible|covered|qualify)\b" + _NOT_FOR_SELF_PAY +
    r"|\bnot (?:available|offered|open|extended) to (?:insured|patients (?:with|who have) (?:health )?insurance)\b"
    r"|\bexclud\w* (?:all )?(?:insured|patients (?:with|who have) (?:health )?insurance)\b"
)
_INSURED_HELPED = re.compile(
    r"\bunder[- ]?insured\b"
    r"|\b(?:limited|inadequate|insufficient|partial)(?: or no)? (?:health )?(?:insurance|coverage)\b"
    r"|\bwith or without (?:an? )?(?:health )?insurance\b"
    r"|\b(?:sufficient|adequate|enough) (?:health )?insurance\b|\binsurance (?:coverage )?or assets\b"
)

def quote_excludes_insured(quote: str) -> bool:
    text = normalize(quote)
    return _INSURED_LEFT_OUT.search(text) is not None and _INSURED_HELPED.search(text) is None

_PRICE_CAP_CLAUSE = re.compile(
    r"\b(?:charged|billed|pay|paid|owe|responsible to pay)\b[^.;]{0,15}?\b(?:more|greater|higher)\b"
    r"[^.;]{0,80}?\bthan\b[^.;]*"
    r"|amounts? generally billed[^.;]*"
)

def quotes_only_a_price_cap(quote: str) -> bool:
    text = normalize(quote)
    if _PRICE_CAP_CLAUSE.search(text) is None:
        return False
    return _INSURED_WORDS.search(_PRICE_CAP_CLAUSE.sub(" ", text)) is None

_BARE_LABEL = re.compile(r"^[^.;:]{1,40}:$")

def quote_is_bare_label(quote: str) -> bool:
    return _BARE_LABEL.match(normalize(quote)) is not None

_PAYMENT_PLAN = re.compile(r"\bpayment (?:plans?|arrangements?)\b")
_ASSISTANCE_WORDS = re.compile(
    r"\bfinancial (?:assistance|aid)\b|\bcharity\b|\bfree\b|\bdiscount\w*|\bwrite[- ]?offs?\b|\bwritten off\b"
    r"|\bforgiv\w+|\breduc\w+|\bsliding\b|\bfap\b"
)

def quotes_only_payment_plans(quote: str) -> bool:
    text = normalize(quote)
    return _PAYMENT_PLAN.search(text) is not None and _ASSISTANCE_WORDS.search(text) is None

INSURED_LEFT_OUT_REASON = "quote does not say insured patients are left out"
PRICE_CAP_REASON = "quote states a price cap benchmarked on insured patients, not who may apply"
BARE_LABEL_REASON = "quote is only a heading"
PAYMENT_PLAN_REASON = "quote is about payment plans, not financial assistance"

def insured_field_problem(quote: str, value: bool) -> str | None:
    """Replacement for the two insured branches of verify_sheet: None to accept."""
    text = normalize(quote)
    if quotes_only_the_agb_cap(quote):
        return AGB_CAP_REASON
    if quotes_only_a_price_cap(quote):
        return PRICE_CAP_REASON
    if quote_is_bare_label(quote):
        return BARE_LABEL_REASON
    if value:
        if _INSURED_WORDS.search(text) is None:
            return INSURED_REASON
        if quotes_only_payment_plans(quote):
            return PAYMENT_PLAN_REASON
        return None
    if not quote_excludes_insured(quote):
        return INSURED_LEFT_OUT_REASON
    return None

# verify_sheet, replacing verify.py:1236-1243:
#   elif path == "eligibility.insured_patients_covered" and (
#       reason := insured_field_problem(cited.quote or "", bool(cited.value))
#   ):
#       report.rejected.append((path, reason))
```

## tests
- reject=True value='false' quote='If an individual has sufficient insurance coverage or assets available to pay for care, he/she may be deemed ineligible for financial assistance.'
- reject=True value='false' quote='Patients shall be able to get financial assistance if uninsured, underinsured, not eligible for a government program, or not qualify for governmental assistance (for example, Medicare or Medicaid).'
- reject=True value='false' quote="Uninsured patients who do not qualify for Mackinac Straits Health System's financial assistance program (e.g., due to income and family size) will receive a discount of 30% on gross charges for medically necessary services to ensure they do not pay more for care than insured individuals."
- reject=True value='true' quote='Payment plans are available to patients with family income between 200 and 500 percent of the Federal Poverty Level for those patients who request assistance, regardless of their insurance status.'
- reject=True value='true' quote='After a patient’s eligibility for financial assistance under this policy is determined, a financial assistance-eligible patient will not be charged more than AGB to insured patients by the HealthPartners care delivery location for emergency or other medically necessary care.'
- reject=True value='true' quote='Underinsured:'
- reject=True value='false' quote='Insured patients are not eligible for uninsured/true self pay rates.'
- reject=True value='false' quote='If you have insurance or assets to pay for your care, you may not be eligible for financial assistance.'
- reject=True value='false' quote='Have no insurance or inadequate insurance coverage (high deductible plan)'
- reject=True value='false' quote='You may qualify if: * You do not qualify for public insurance programs in your home state, and'
- reject=True value='true' quote='If you qualify for fnancial assistance, you will not be charged more for emergency or other medically necessary care than patients who are covered by Medicare or other insurance.'
- reject=True value='true' quote='Montefiore Medical Center also provides payment arrangements to patients that have insurance coverage but have an out-of-pocket expense that they cannot afford or deem a hardship.'
- reject=False value='false' quote='Financial assistance is available only to uninsured patients.'
- reject=False value='false' quote='This policy applies only to uninsured patients who meet the income criteria.'
- reject=False value='false' quote='Insured patients are not eligible for financial assistance under this policy.'
- reject=False value='false' quote='Patients with health insurance are not eligible for the charity care program.'
- reject=False value='false' quote='The program is not available to patients with insurance.'
- reject=False value='false' quote='Applicants must be uninsured.'
- reject=False value='true' quote='Eligible patients with insurance coverage are not personally responsible to pay more than AGB after all payments by the health insurer have been applied.'
- reject=False value='true' quote='The Financial Assistance Adjustment based on the AGB percentage will also be applied to the outstanding balance, after payments by insurance, if any, of patients with Catastrophic Medical Expenses, as defined above, who are not eligible for financial assistance based on the other provisions of this policy.'
- reject=False value='true' quote='Insured patients with family income less than 450% of current Federal Poverty Level and excessive medical costs, may qualify for partially discounted care and extended payment plans.'
- reject=False value='true' quote='Underinsured: The patient has some level of insurance or third-party assistance but still has out-of-pocket expenses that exceed his/her financial abilities.'
- reject=False value='true' quote='Insured Patient Free And Discounted Care (set forth on Appendix B)'
- reject=False value='true' quote='Insured patients will not have the AGB applied 2. The FA discount will be applied directly to the balance'
- reject=False value='true' quote='Underinsured patients will not be charged more than amounts generally billed to patients with insurance.'
- reject=False value='true' quote='Only patients with active insurance coverage are eligible for financial assistance.'

## scan hits
- 010007 eligibility.insured_patients_covered correct=True: FLAGGED. False; reason: left-out. The ineligibility is conditional and applies only to people whose insurance is enough ('sufficient insurance coverage or assets'). The policy covers 'underinsured'. Correct value: true.
- 181322 eligibility.insured_patients_covered correct=True: FLAGGED. False; reason: left-out. The quote itself lists 'underinsured' among those who can get assistance. Correct value: true.
- 231306 eligibility.insured_patients_covered correct=True: FLAGGED. False; reason: price cap. The quote is the uninsured 30% self-pay discount, and its only insured mention is 'than insured individuals'. The policy covers '(or underinsured)'. Should be unset or true.
- 210027 eligibility.insured_patients_covered correct=True: FLAGGED. True; reason: payment plan. The quote is the payment-plan clause, not FA. The value is right; re-cite §V.2 'uninsured, under insured ... may be eligible'.
- 240187 eligibility.insured_patients_covered correct=True: FLAGGED. True; reason: price cap. The quote is the 'not charged more than AGB to insured patients' cap. The value is right; re-cite 'all uninsured and underinsured patients ... 400% FPL'.
- 241323 eligibility.insured_patients_covered correct=True: FLAGGED. True; reason: bare heading 'Underinsured:'. The value is right; re-cite 'Uninsured and underinsured patients whose household income does not exceed 200%...'.
- 140101 eligibility.insured_patients_covered correct=True: Morris Hospital IL. False, but the quote grants FA where 'there is limited or no health insurance', so limited insurance is covered. Should be true.
- 141332 eligibility.insured_patients_covered correct=True: Hillsboro IL. False from the web-page 'Uninsured Discount' (HUPDA) sentence. The policy's separate Financial Assistance Program (% of balance written off) has no insurance exclusion, so the quote does not say insured patients are left out of FA.
- 151329 eligibility.insured_patients_covered correct=True: Margaret Mary IN. False from the definition of 'Self-Pay or Uninsured'. The policy purpose covers 'uninsured and underinsured individuals'. Should be true.
- 161355 eligibility.insured_patients_covered correct=True: Knoxville IA. False, but the quoted criterion 'no insurance or inadequate insurance coverage (high deductible plan)' admits underinsured patients. Should be true.
- 220074 eligibility.insured_patients_covered correct=True: Southcoast MA. False from the conditional 'If you have insurance or assets to pay for your care, you may not be eligible' (same shape as 010007). The summary says 'uninsured, underinsured'. Should be true.
- 220075 eligibility.insured_patients_covered correct=True: Mass Eye and Ear (MGB policy). False from 'do not qualify for government sponsored insurance programs', which is about public programs. The MGB policy assists 'uninsured patients and underinsured patients'. Should be true.
- 220110 eligibility.insured_patients_covered correct=True: Brigham and Women's. Same MGB quote and verdict as 220075.
- 220116 eligibility.insured_patients_covered correct=True: Tufts MA. False from 'intended to primarily serve patients who do not have health insurance'; 'primarily' is not an exclusion. The policy defines and helps 'Uninsured and Underinsured Patients' (it excludes only co-pays and deductibles). Should be true.
- 220119 eligibility.insured_patients_covered correct=True: BW Faulkner MA. False from 'do not qualify for public insurance programs', which is about public programs. The MGB policy covers underinsured patients. Should be true.
- 271329 eligibility.insured_patients_covered correct=True: Madison Valley MT. False, but the quote says FA 'can be used with or without an insurance program'. Should be true.
- 310041 eligibility.insured_patients_covered correct=True: RWJBarnabas NJ. False from 'Insured patients are not eligible for uninsured/true self pay rates', which concerns the self-pay rate, not FA. NJ Charity Care in the same policy covers 'coverage that pays only part of the hospital bill (uninsured or underinsured)'. Should be true.
- 310074 eligibility.insured_patients_covered correct=True: Jersey City MC (RWJBarnabas). Same self-pay-rate quote and verdict as 310041.
- 310075 eligibility.insured_patients_covered correct=True: Monmouth MC (RWJBarnabas). Same self-pay-rate quote and verdict as 310041.
- 310092 eligibility.insured_patients_covered correct=True: Capital Health NJ. False from the definition of an uninsured patient. The policy also has an 'Underinsured Discount' (100% at or below 400% FPG). Should be true.
- 360364 eligibility.insured_patients_covered correct=True: Cleveland Clinic Avon OH. False, but the quote itself defines 'Under-Insured Qualified Patients', and the sliding scale is 'Uninsured/Under-Insured'. Only the charity write-off is self-pay-only. The quote does not support false.
- 140187 eligibility.insured_patients_covered correct=True: HSHS St Elizabeth's IL. True from the brochure's AGB cap ('not be charged more ... than patients who are covered by Medicare or other insurance'), the same shape as 240187. The value is right (hshs.org/fap: 'are underinsured'); re-cite.
- 521310 eligibility.insured_patients_covered correct=True: HSHS St Clare WI. Same brochure AGB-cap sentence as 140187. The value is likely right; re-cite.
- 140177 eligibility.insured_patients_covered correct=True: Jackson Park IL. True from 'Payment Plans ... available to both insured and uninsured patients', which is a payment plan. The value is right (purpose: 'uninsured or underinsured individuals'); re-cite.
- 330059 eligibility.insured_patients_covered correct=True: Montefiore NY. True from 'also provides payment arrangements to patients that have insurance coverage', which is about payment arrangements, not FA. The value is right (same summary: 'Underinsured patients: people with insurance and high medical expenses'); re-cite.
- 330086 eligibility.insured_patients_covered correct=True: Montefiore NY. Same payment-arrangements quote and verdict as 330059.

## challenge false positives
- 510055 eligibility.insured_patients_covered: Draft sheet (Cabell Huntington WV), not published. False is cited to 'Patients are not eligible for financial assistance, as provided for in this Policy, if the patient is eligible to receive reimbursement for any portion of a patient's bill from any first or third party payor, including health insurance'. That is a real exclusion, and the policy's main beneficiaries are 'uninsured patients'. Today's code accepts it; the proposed False branch rejects it because no _INSURED_LEFT_OUT phrase matches. My revision also rejects it. No safe pattern exists: the same 'not eligible ... if ... health insurance' shape is McKenzie 351302's out-of-network-only exclusion, which must stay rejected. The loss is safe in direction: the field is dropped and the sheet is not held.
- 220024 eligibility.insured_patients_covered: Held sheet (Holyoke MA), debatable. The quote 'Does not have any form of insurance to cover services rendered that are medically necessary' sits under 'Who can use this application?', so for HMC's own program it is an uninsured-only requirement. The bare line reads like a definition, though, and MA HSN-Secondary covers insured patients. Accepted today, rejected by both the proposal and the revision. Only the field is dropped.

## challenge misses
- 040004 eligibility.insured_patients_covered: FLAGGED, and the proposal leaves it as a one-off. It can be caught safely: add '\b(billing )?statements?\b' to the payment-terms phrases (removed before the assistance-word test). Across all 1,648 distinct insured fields stored in every sheet version in the DB, this fires only on 040004 ('Insured patients will receive a statement for their applicable out-of-pocket responsibility...'). It leaves alone 310017/310111/310115 ('...billing statement ... to submit a completed application for financial assistance') and 140091 ('Statement of Policy ... Carle Financial Assistance Program').
- 390006 eligibility.insured_patients_covered: Published, PA, Geisinger. True is cited to the uninsured bullet 'Patient does not have insurance coverage and was denied Medicaid...'. Its only insured word sits inside 'does not have insurance'. The bullet the model should have cited comes two lines later in the source: 'patient has insurance that has rendered payment, but balance after insurance is a financial hardship'. Same defect as the flagged off-topic True quotes. The proposal does not catch it.
- 450358 eligibility.insured_patients_covered: Published, TX, Houston Methodist. True is cited to 'Individuals with the financial capacity to purchase health insurance will be encouraged to do so', which is about buying insurance, not who gets FA. The value is right (the plain-language summary says 'uninsured, underinsured'). The proposal does not catch it.
- 261321 eligibility.insured_patients_covered: Draft. True is cited to 'Patients without insurance will automatically receive a 40 percent discount ... and will be considered for assistance': uninsured discount only.
- 250094 eligibility.insured_patients_covered: Draft. True is cited to 'Payment in full is required at the time of service from all patients who owe co-pays, coinsurance...', which is a payment rule and if anything points the other way.
- 521332 eligibility.insured_patients_covered: Draft. True is cited to '... you will receive a 10% prompt pay discount'. Prompt pay is not FA, and the word 'discount' lets it pass the proposal's assistance-word test.
- 170200 / 170185 / 171316 / 521300 (held/draft) eligibility.insured_patients_covered: Same defect; neither version catches these safely, so leave them to re-cite/rebuild. 170200: 'Uninsured and insured Patients who are not eligible for financial assistance may receive a prompt pay discount'. 170185/171316: 'If you are insured ... please contact your insurance provider. Patients without insurance will automatically receive a 40 percent discount'. 521300 v2: 'Aspirus Financial Aid ... is not a health insurance plan ... arrange a payment plan'.

## one-offs
- 040004 eligibility.insured_patients_covered: Re-cite by hand or on the next rebuild. Keep the value true and change the quote (fap-e1d2078a8ef0ccad) to 'Individuals who may be eligible for financial assistance include the uninsured and underinsured.' The current billing-statement quote cannot be caught safely: requiring an eligibility word in every True quote would also reject quotes the audit accepted (definitions 060001, 110256, 190005; 'Medical Resources includes...' 161341; 'secondary to ... insurance' 131313, 171350), and a rule aimed at 'will receive a statement' would catch only this one sheet.

## notes
Throwaway scripts and outputs are in /private/tmp/claude-501/-Users-grey-Desktop-dev-eventhorizon/b529e6d4-a357-482a-ba81-d6830b9a174d/scratchpad/waive/audit-rest/scratch-insured/: candidate.py, tests.py, tests_out.json, scan.py, scan2.py, all_insured.txt. I made no edits to the repo and wrote nothing to the DB (read-only URI only).

**Scan results**
- 3,184 fields scanned; 26 rejected, all on eligibility.insured_patients_covered (288 fields: 270 True, 18 False).
- 18 rejections are False values, i.e. every False in the published set. I read each one against its source and none is supported; in each case the policy covers underinsured patients or the quote is about something else. Only 3 of those sheets were among the 153 audited, and all 3 were flagged.
- 8 rejections are True values with off-topic quotes. Their values look right, so rejecting drops a correct value with a bad citation. Restoring them needs a re-cite or rebuild; the better quotes are in each scan_hit note.
- 6 of the 7 flagged sheets are caught. 040004 is the one-off.
- The 26 tests pass (no unexpected mismatches).

**Side effect of the False rule:** today's code rejects good False quotes like 'available only to uninsured patients' and 'must be uninsured'. The _INSURED_WORDS precondition (line 309) leaves _INSURED_EXCLUDED's own 'only uninsured' alternatives dead. The new False branch accepts them.

**Ordering:** the price-cap test keeps the old quotes_only_the_agb_cap in front of it, so it only adds rejections. It leaves alone 'Eligible patients with insurance coverage are not personally responsible to pay more than AGB...' (271330) and 'Adjustment based on the AGB percentage ... after payments by insurance' (100157). That is why the cap pattern needs the 'charged/billed/pay ... more ... than' comparison form instead of simply adding 'agb' to _AGB_CLAUSE: the plain addition would wrongly reject 100157.

**Bare-label test on other paths:** run as an aside, it also matches 220071 asset_test 'Assets will also be used when:', 361310 submit_methods 'Willard, OH 44890 Dept:' and 200008 submit_methods 'Application Mailing Addresses:'. The proposed rule is limited to the insured path, so these are not rejected, but they may be worth their own check.

**Probes I rejected as unsafe:**
- An 'eligibility word required for True' rule hits audit-accepted definitions.
- An 'insured words only in uninsured descriptions' rule hits correct Maine quotes ('have coverage that pays only part of the bill').

**Probable unflagged True errors no safe rule catches (outside the audit, worth a look):**
- 390006 Geisinger: 'Patient does not have insurance coverage and was denied Medicaid' (an uninsured criterion).
- 450358 Houston Methodist: 'Individuals with the financial capacity to purchase health insurance will be encouraged to do so'.
- 241355: 'All insurance options ... must be used before assistance is approved'.

**Owner decision B** (free-care 'may ... up to 100%') is untouched; the rule never looks at free_care_max_fpl.
