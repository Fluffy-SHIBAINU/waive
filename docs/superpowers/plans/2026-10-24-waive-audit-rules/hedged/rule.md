# hedged

## why it passed
The existing rule should have caught none of the four. None of their quotes has decision B's form, so verify.quotes_assistance_ceiling (verify.py:213-221) returns False and verify_sheet accepts the line at verify.py:1224. I re-ran verify_sheet on each published v2 body with its stored documents. All four come back with rejected=[] and document_conflicts=[]. The rule was already in place when they were published: commit 2cd05d2 is 2026-10-23 22:42Z, and they went out 2026-10-24 00:37-01:27.

Per sheet:
- 161367 (Pella). Quote: "A discount up to 100% will be extended to patients ... equal to or less than 200%". It has "up to 100%" but a firm "will be extended" and no "may", so _HEDGED_UP_TO_FULL (verify.py:208) does not match. free_limit_not_free (verify.py:739) needs "sliding" inside the quote, and the earlier sentence "available on a sliding scale of up to 100% of charges" was not quoted. This is the same shape as CommonSpirit's sentence (firm verb plus "up to 100%"), so it STAYS free care under B.
- 171358 (Lindsborg). Quote: "may be eligible for a 100% bill reduction". It has "may" but no "up to", which is the "may be eligible for free care" style. STAYS free care under B.
- 241301 (LakeWood). The quote is CommonSpirit G-003's "is eligible to receive Financial Assistance up to a 100% discount", which B names as staying free care (comment at verify.py:204-206; the test at tests/unit/test_verify.py ~1583 asserts it passes). STAYS free care.
- 241339 (Hendricks). Quote: "may be eligible to receive Charity Care". There is no "up to 100%" in the quote. STAYS free care under B.

So there is no bug for these four.

I did find a latent order bug while checking. _HEDGED_UP_TO_FULL = `\bmay\b[^.;]*\bup to (?:a |an )?(?:100 ?(?:%|percent)|one hundred percent)` only matches when "may" comes first. Pella's own linked page (web-bdb5c6f0cff67061) says "A discount up to 100% may be offered to patients whose family income is equal to or less than 200%", which is the hedged case B is about, yet quotes_assistance_ceiling returns False on it. Had the structurer quoted that page instead of the policy page (web-766d7f11e6271935), B would not have fired. document_conflicts would not have caught it either: free_care_limits_stated(web-bdb5c6f0cff67061) is empty, because _FREE_SENTENCE needs "100% discount", not "discount up to 100%".

The same regex also reads an income figure "up to 100%" as the size of the help whenever a "may" comes earlier in the clause (for example the sbhny table in fap-e903006629fff7e4, "...trust income may be used ... federal poverty level up to 100% 101% to 150%").

For 241339, the same document's §VI.2 sentence "does not exceed 125% ... may be granted up to 100% forgiveness" IS rejected by the current rule (tested True). The structurer quoted §V.2 instead, and the verifier judges only the quoted sentence, which is how B is written.

## rule


## challenge (no_rule_proposed)
The core conclusion holds. Under decision B, all four sheets stay free care, and quotes_assistance_ceiling has no bug that should have caught them. I replayed the current rule on all 578 published free-care quotes. It is true only for 050145 (CA) and 100090 (FL). Both are B's named examples, both are correct catches, and both are stale copies waiting for the VM recheck and rebuild. It is false on all four flagged quotes. I read all 65 published free-care quotes that say "may" (or can, could, might). None other has B's form, so B as written misses nothing on the published set.

Corrections and challenges:

1. **171358 and 241301 are not open questions; B already decides "keep".**
   - The audit is inconsistent. It upheld 171358 (Lindsborg) but refuted 170012 (Salina) on the identical sentence ("may be eligible for a 100% bill reduction").
   - It also refuted 6 other "may ... 100%" flags: 161336, 171319, 201307, 231314, 230236 and 241364.
   - So 171358's upheld verdict contradicts B and should be closed under B, not sent to the owner. 241301 is B's named CommonSpirit exception; 8 other published sheets carry the same sentence.

2. **"Impossible" is too strong. The right word is "not allowed under B as written."** A mechanical, document-level check is clean:
   - It holds a free-care limit when the sheet's own stored documents state that same income in B's hedged form.
   - Across all published sheets it hits exactly 050145, 100090, 161367 and 241339. Nothing else is touched, including no other sheet from the nine scouted states and neither 171358 nor 241301.
   - Pella (161367) needs the order fix for this, because its linked page says "A discount up to 100% may be offered".
   - It judges documents rather than the quote, so it is a new owner decision. Offer it as the owner's option on the 161367 and 241339 rows.

3. **The Pella reasoning has an error.** The other engineer implied that free_limit_not_free would have caught Pella if the "sliding scale of up to 100% of charges" sentence had been quoted. It would not.
   - _FREE_WORDING counts the "100%" in "up to 100%", so a two-sentence quote still passes both checks (tested).
   - This affects no published quote today. Whether "up to 100%" on a sliding scale is free wording is a question for the owner, not B.

4. **The optional hardening.**
   - Order-insensitivity is a real within-B gap. Across the 4,587 documents (364 sentences that contain both "up to" and "may"), it changes 8 sentences, all of them genuine: Pella's linked page, OSF, fap-8ec0309fdc15a258 twice, fap-7bc83f2dcede48e7, web-8ebebe3690ba65f4 and web-dc2b95849a45a83c are newly caught, and the sbhny income table is no longer a false positive.
   - On published sheets it hits only 050145 and 100090.
   - The submitted candidate has three latent defects. None occurs in the current data, but each is a real failure:
     - It rejects "income up to 100% of the 2025 Federal Poverty Guidelines may be eligible for Charity Care". That is the "may be eligible" wording B keeps, so this overrides B.
     - It rejects a firm "discount up to 100%, and patients above that may apply for a payment plan".
     - It stops catching "income up to 100% of the FPL may receive a discount of up to 100%", which the current rule catches. The cause is that it strips only the help's "up to 100%", so the income "100%" survives as a free marker.
   - The tightened cand2 fixes all three. It gives the same 2 published hits and the same 8 document differences. It passes all 40 cases (the other engineer's 14 plus 26 of mine). With it patched in, all 99 tests in tests/unit/test_verify.py and test_structure.py pass.

5. **Remaining latent gaps, for the record only.** No published sheet is affected.
   - An income figure written as "100%" ("between 100% and 200% FPL may receive up to 100%") still counts as a free marker in both versions.
   - "Up to 100% free medical care" keeps "free medical care" as a free marker, so Hendricks' full definition would pass.

**Report rows for the owner:**
- 161367 and 241339: keep as free care, or adopt the document-level option.
- 171358 and 241301: closed by B.
- 241339 also belongs in the "quote names no benefit" review (docs/reports/free-care-wording-2026-10-23.md), along with 241364 ("may provide uncompensated care", published after that report).
- 330065 ("may qualify for charity care" at 100%) slipped that report's "names no benefit" filter, because its income "100%" was read as a benefit.

The repo and database were not modified; only read-only queries were run. Scripts are in /private/tmp/claude-501/-Users-grey-Desktop-dev-eventhorizon/b529e6d4-a357-482a-ba81-d6830b9a174d/scratchpad/waive/audit-rest/scratch-hedged-challenge/: scan1-3.py, docscan.py, docscan2.py, probe.py, tests2.py, cand2.py, docrule.py, docrule_cur.py, runtests.py.

## revised rule
No rule for the four flagged sheets. Under decision B as written, which judges the quote, catching any of them would override B.

OPTIONAL within-B hardening of quotes_assistance_ceiling. It replaces the other engineer's candidate, which has three defects. Code: /private/tmp/claude-501/-Users-grey-Desktop-dev-eventhorizon/b529e6d4-a357-482a-ba81-d6830b9a174d/scratchpad/waive/audit-rest/scratch-hedged-challenge/cand2.py

_FULL = r"up to (?:a |an )?(?:100 ?(?:%|percent)|one hundred percent)"
_QUAL = r"(?:\d{4}|current|current year's|annual|published|hhs|u\.s\.|latest|most recent|applicable|federal income) "
_INCOME_AFTER = rf"(?! ?(?:of )?(?:the )?(?:{_QUAL})*(?:federal|poverty|fpl\b|fpg\b|fpig\b)| ?[-–]? ?\d)"
_HELP_FULL = rf"\b{_FULL}{_INCOME_AFTER}"
_HEDGED_EITHER = re.compile(rf"\bmay\b[^.;]*{_HELP_FULL}|{_HELP_FULL}(?:(?!,? (?:and|but|or|while|whereas|others?) )[^.;]){{0,80}}?\bmay\b")
def quotes_assistance_ceiling(quote):
    text = normalize(quote)
    if _HEDGED_EITHER.search(text) and _FREE_MARKERS.search(_UP_TO_FULL.sub(" ", text)) is None:
        return True
    return _CEILING_WORDING.search(text) is not None and _FREE_MARKERS.search(text) is None

Changes from the other candidate:
- (a) The income lookahead also accepts a year or other qualifier ("of the 2025 / current year's federal poverty"), so an income figure is not misread as the size of the help.
- (b) The reversed order ("up to 100% ... may") must sit in one short clause, not across ", and/but/or".
- (c) It strips every "up to 100%" before looking for free markers, as the current rule does.

OWNER OPTION ONLY. This goes beyond B, because it judges the documents, not the quote. Hold a published free-care limit X when any stored source of the sheet has a sentence on which the hardened predicate is true and whose poverty-tied percentages (_tied_percents(_poverty_text(s))) include X. Measured on every published sheet, it holds exactly 4:
- 050145 and 100090: already caught by the quote rule;
- 161367 and 241339: only with the order fix for 161367; the current forward-only rule gets 241339 alone.

It touches nothing else among the 578 published free-care fields, and not 171358 or 241301.

## candidate code
```python
# OPTIONAL hardening of the existing decision-B rule. It is not a rule for the four flagged sheets and catches none of them.
# Tested in scratch-hedged/candidate.py with the repo's own normalize, _UP_TO_FULL, _FREE_MARKERS and _CEILING_WORDING.
import re
from waive.atlas import verify as V

_FULL = r"up to (?:a |an )?(?:100 ?(?:%|percent)|one hundred percent)"
# An 'up to 100%' followed by poverty-level words or the next band is an income, not the size of the help.
_INCOME_AFTER = r"(?! ?(?:of (?:the )?)?(?:current |annual )?(?:federal poverty|fpl\b|fpg\b|poverty)| ?\d)"
_HELP_FULL = re.compile(rf"\b{_FULL}{_INCOME_AFTER}")
_HEDGED_EITHER = re.compile(
    rf"\bmay\b[^.;]*\b{_FULL}{_INCOME_AFTER}|\b{_FULL}{_INCOME_AFTER}[^.;]*\bmay\b"
)

def quotes_assistance_ceiling_b(quote: str) -> bool:
    text = V.normalize(quote)
    if _HEDGED_EITHER.search(text) and V._FREE_MARKERS.search(_HELP_FULL.sub(" ", text)) is None:
        return True
    return V._CEILING_WORDING.search(text) is not None and V._FREE_MARKERS.search(text) is None
```

## tests
- reject=False value='200' quote='A discount up to 100% will be extended to patients whose family income is equal to or less than 200% of federal poverty guidelines.'
- reject=False value='200' quote='Household income less than 200% of the Federal Poverty Guidelines may be eligible for a 100% bill reduction.'
- reject=False value='200' quote='Up to 200% of the FPL  – Any patient whose Family Income is at or below 200% of the FPL, including, without limitation, any Uninsured or Underinsured patient, is eligible to receive Financial Assistance up to a 100% discount from his or her account balance for eligible services provided to the patient after payment, if any, by any thirdparty(ies).'
- reject=False value='125' quote='Patients who are Uninsured or Underinsured for a medically necessary service who are ineligible for governmental or other coverage, and who have family incomes not in excess 125% of the Federal Poverty Guidelines may be eligible to receive Charity Care.'
- reject=True value='200' quote='A discount up to 100% may be offered to patients whose family income is equal to or less than 200% of federal poverty guidelines.'
- reject=True value='125' quote='Uninsured and underinsured patients whose income/family income does not exceed 125% of the current Federal Poverty Guidelines may be granted up to 100% forgiveness of their charges for emergent or medically necessary care.'
- reject=True value='400' quote='If your gross (total) family income is no higher than 400 percent of the [Federal Poverty Level (FPL)](https://syfphr.oshpd.ca.gov/), you may receive a discount of up to 100 percent on the services you received.'
- reject=True value='200' quote='If your family adjusted income is less than 200% of federal poverty guidelines, you may be eligible for up to a 100% discount.'
- reject=True value='240' quote="If the patient's income is between 200%-240% of the Federal Poverty Income Guidelines they may qualify for Financial Assistance up to 100% of their hospital and clinic bills."
- reject=False value='200' quote='Patients at or below 200% of the poverty guidelines may receive free care, a discount of up to 100% of charges.'
- reject=False value='150' quote='family incomes not in excess of 150% of the Federal Poverty Guidelines may be eligible to receive 100% Charity care based on their financial assistance application.'
- reject=False value='100' quote='Patients with household income up to 100% of the FPL may receive a full discount.'
- reject=False value='100' quote='Patients may qualify for a full discount if their income is up to 100% of the Federal Poverty Level.'
- reject=False value='200' quote='Patients at or below 200% FPL receive a discount up to 100%; others may apply for a payment plan.'

## scan hits
- 050145 eligibility.free_care_max_fpl correct=True: Community Hospital of the Monterey Peninsula, value 400: "you may receive a discount of up to 100 percent". This is decision B's own named example. The current rule and the hardened version both reject it. It is still published here only because the sheet dates from 2026-10-11, before 2cd05d2. docs/PROGRESS.md (7.12c-1) already lists it as a stale copy that needs `waive atlas recheck` and a rebuild with --reuse-sources (the user's step on the VM).
- 100090 eligibility.free_care_max_fpl correct=True: Flagler Hospital, value 200: "you may be eligible for up to a 100% discount". Also a named decision-B example. Both rule versions reject it. Published v3 on 2026-10-15, before the rule; listed in PROGRESS.md as awaiting recheck and rebuild. No other of the 578 published free-care fields is hit by either version. The predicate is only called on free_care_max_fpl, so the other 2,606 fields are never rejected by it.

## challenge false positives

## challenge misses
- 161367 eligibility.free_care_max_fpl: Not a miss under decision B as written. The quote "A discount up to 100% will be extended ... <=200%" has a firm verb and no "may", the same shape as CommonSpirit's sentence. It would only be caught if B were applied to the hospital's own documents: its linked page web-bdb5c6f0cff67061 says "A discount up to 100% may be offered to patients ... <=200%", which is B's form at the same income, but in reverse order, so even that needs the order-insensitive fix. That is an owner decision, not a bug.
- 241339 eligibility.free_care_max_fpl: Not a miss under decision B as written. The quote is §V.2's "may be eligible to receive Charity Care": it has no "up to 100%". Its own document says "does not exceed 125% ... may be granted up to 100% forgiveness" (§VI.2, which the current rule rejects) and defines Charity Care as "Up to 100% free medical care". So it would be caught only by a document-level reading, which is the owner's call. Even quoting the whole §V.2 item would pass, because "free medical care" survives as a free marker after "up to 100%" is stripped.

## one-offs
- 161367 eligibility.free_care_max_fpl: Report row for the owner; no rule. Under B this stays free care: the quote says "A discount up to 100% will be extended ... equal to or less than 200%", a firm verb with no "may", the same shape as CommonSpirit. For the owner to weigh: the sentence just before it says "available on a sliding scale of up to 100% of charges", and the hospital's own linked page web-bdb5c6f0cff67061 says "may be offered". If the owner rules that this is not free care, record a ruling and rebuild so that 200 becomes assistance_ceiling_fpl. If B is made order-insensitive, a rebuild that quotes the linked page would also be rejected.
- 171358 eligibility.free_care_max_fpl: Report row for the owner; no rule. Under B this stays free care: "may be eligible for a 100% bill reduction" is the "may be eligible for free care" style. Salina Regional (170012) publishes the identical sentence from the same template, so one ruling should cover both. About 30 other published lines are the same "may ... 100% discount, reduction or free care" shape that B keeps (loose count). Side note: "less than 200%" excludes 200 itself.
- 241301 eligibility.free_care_max_fpl: No action under B: CommonSpirit G-003's "is eligible to receive Financial Assistance up to a 100% discount" is the decision's named exception. Eight other published sheets carry the same sentence: 450011, 450072, 050444, 450193, 670059, 060003, 060015 and 171388. Only the owner reopening 7.12c-1 (option A, strict) would change all nine.
- 241339 eligibility.free_care_max_fpl: Report row for the owner; no rule. As quoted (§V.2, "may be eligible to receive Charity Care") this stays free care under B. The same document defines Charity Care as "Up to 100% free medical care", and §VI.2 says "does not exceed 125% ... may be granted up to 100% forgiveness", which is B's exact form; the current rule rejects that sentence (tested). Applying B to a document's other sentences rather than to the quote would be a new decision, so the owner should rule on this sheet directly: keep it, or record a ruling and rebuild with 125 as a hedged limit or ceiling.

## notes
Under decision B all four flagged sheets stay free care, and the existing B rule correctly passes each of them. No new rule is proposed: catching any of the four would override B.

Optional hardening, still within B (the candidate_code above):
- What it changes:
  - the hedge can come after the "up to 100%" in the same clause (", ; ." still bound it);
  - an "up to 100%" followed by poverty-level words or the next band is treated as an income, not the size of the help.
- How it was tested: all 14 test cases pass. Across all 4,587 stored documents, only 3 sentences that combine poverty-level words, "up to 100%" and "may" read differently from the current rule:
  - Pella web-bdb5c6f0cff67061 "A discount up to 100% may be offered ... 200%" is now caught;
  - OSF billing-8c88d7c924d508a7 "what level of discount (up to 100%) you may receive" is now caught (it has no income figure, so the free-limit check rejects it anyway);
  - the sbhny table fap-e903006629fff7e4 is no longer a false positive.
- What it touches: on the 605 published sheets it hits exactly what the current rule hits (050145 and 100090). It changes no published sheet and would not withdraw any of the four flagged ones.

The real live problem in this pattern is that two of B's own named examples, Monterey 050145 (400) and Flagler 100090 (200), are still published in this database. They predate 2cd05d2 and are waiting for the recheck and rebuild already listed in PROGRESS.md 7.12c-1.

None of the four CCNs appears in docs/reports/free-care-wording-2026-10-23.md or data/atlas-eve/_decisions.json, so all four report rows are new.

Scripts and outputs are in /private/tmp/claude-501/-Users-grey-Desktop-dev-eventhorizon/b529e6d4-a357-482a-ba81-d6830b9a174d/scratchpad/waive/audit-rest/scratch-hedged/: replay.py, candidate.py, tests.py, tests.json, scan_final.py, docscan.py, docscan2.py, cohorts.py. The database was read through the read-only URI only, and no repo files were edited.
