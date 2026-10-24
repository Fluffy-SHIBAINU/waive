# non-fpl-basis

## why it passed
The four UMMS sheets passed because the free-care check never asks what the percentage is a percentage of. In verify_sheet (verify.py ~L1206), eligibility.free_care_max_fpl is held only to _free_limit_stated (L1093), which is states_income_limit(quote, value) (L888). That runs _income_limit_at (L863) on each "200" in the quote. Its last test, `_ends_within(_AT_MOST, before, INCOME_WORDS_BEFORE)` (L882, _AT_MOST at L777 = up to|less than|under|below), is met because "up to" sits within three words before "200%". Any "up to N%" passes that test, whatever follows. I ran it on the quote: _FPL_WORDS finds nothing, names_percent_of_poverty_level(q,200) is False, poverty_after/or_less/header are all False, at_most is True, so states_income_limit is True. Decision 7.15a chose on purpose not to require "% of the poverty level" for free care, because doing so would have dropped 69 correct table-style sheets. names_percent_of_poverty_level and _FPL_WORDS are used only for assistance_ceiling_fpl (ceiling_problem) and for the document-level helpers (free_care_limits_stated, widened_span, anchored_span, assistance_limits_stated). None of them is ever applied to a free-care quote that already passes states_income_limit. Nothing nearby mentions poverty either. The quoted sentence and its neighbours ("...request such assistance. I. Free Care - ..." and "II. Reduced Cost Care - Those between 200% and 300% of the income eligibility limits established by the Maryland Department of Health...") have no poverty words. Poverty words appear only in the policy's Definitions block ("Federal Poverty Level ... HHS", and "MDH Limits ... vs the Federal Poverty Levels ... UMMS adopted these new limits") and in a revision-history line. Because the free-care sentence has no _FPL_WORDS, free_care_limits_stated also names no limit for this document, so publish.document_conflicts had nothing to compare against. The structurer prompt (structure.py rule 10) asks for "the upper income bound of a 100% discount band" and never says to leave it null when the policy uses a basis other than FPL.

## rule
New check percent_of_other_basis(quote, value) in verify.py. Call it from verify_sheet as a new elif for path in INCOME_LIMIT_PATHS (free_care_max_fpl, assistance_ceiling_fpl, discount_tiers), placed right after the bands_under_another_state elif. The value to check is cited.value for the two scalar fields; for discount_tiers, any tier's max_fpl_inclusive or min_fpl_exclusive. Condition: the quote is read through _poverty_text with commas removed. Some occurrence of the value (same number pattern and (?<![\d.$]) guard as states_income_limit) must be followed by a percent sign and then "of". In the next 10 words, stopping before the next digit, there must be a named non-poverty income benchmark: "income eligibility limits/levels/standards/thresholds", "Medicaid / Medical Assistance / MDH / state [income] [eligibility] limits/levels/standards", "median [family|household] income", AMI, SMI, "Department of Health" (but not "...and Human Services"), MDH, "self-sufficiency standard" or "living wage". No poverty words (_POVERTY_AFTER) may come before that benchmark. Outcome: reject the field with a new reason, e.g. OTHER_BASIS_REASON = "quote measures income against another benchmark than the poverty level". For the UMMS sheets the free-care field is their only income rule (no tiers, no ceiling), so the sheet is held, not published. Optionally also add to structure.py rule 10: leave free_care_max_fpl null when the documents give the limit as a percentage of a measure other than the federal poverty level. This rule does not touch decision B: it never looks at "may ... up to 100%" or free-care wording.

## challenge (needs_change)
I re-ran the proposed predicate myself. On published-fields.jsonl it hits exactly 210003, 210043, 210049 and 210058, all free_care_max_fpl. I read each one and all four quote the UMMS sentence "200% of the income eligibility limits established by the Maryland Department of Health", so all four are correct catches. Across the 761 published income-field values it raised no false positive, the nine scouted-states sheets included. It also raised none across all 3,771 income fields in every sheet version in the DB (drafts and held too). In the corpus I checked every percentage in the 4,587 documents and found only the UMMS policy. A wider search found no other non-FPL basis that could feed an income field: no AMI, SMI, HUD, ALICE or living wage. The DSH-guideline hits (Norton, Carroll County) concern assets. "300% of the federal benefit rate" appears only in a MassHealth state document. "Local poverty line", "cost of living", Medicaid-rate and NJ DOH sentences all resolve to FPL or are not limits. Misses: none among published fields.

Why it still needs a change: it is fragile in a way the corpus already shows. It cuts the text to 10 words before the "Department of Health and Human Services" exclusion runs. It also does not handle "&", and it ignores poverty words that come after the benchmark. The proposed version rejects all of these:
- "200% of the guidelines set by the Department of Health and Human Services"
- "...U.S. Department of Health & Human Services"
- "200% of the income eligibility limits (Federal Poverty Guidelines)"
- the California HCAI and NJ "Health Care for the Uninsured" phrasings
It already fires on real text in web-2a0e92e93ba3b101 (UK King's Daughters, 180009): "write off as much as 100% of your bill, based on the U.S. Department of Health and Human Services poverty guidelines". That is harmless today only because 100 there is a discount, which states_income_limit already refuses. The revised version passes all 20 original unit cases plus 11 adversarial ones, matches the proposed version's hits on every real field (22 identical hits across all sheet versions), and in the corpus fires only on the two UMMS income sentences. The proposed version also fired on "exceeds 25% of that family's annual income. MDH Limits refers to...", across a sentence end; the revised one does not.

End to end, verify_sheet today accepts the UMMS free-care field on 10 sheets: the 4 published ones plus drafts 210002, 210030, 210035, 210037, 210038 and 210063 from the running rebuild. The UMMS ceiling of 300 is already rejected (CEILING_UNTIED), so with this rule all 10 are held. The rule must land before the rebuild publishes, or 6 more sheets go out with the same defect.

Remaining risks, none of them seen in stored text: "N% of the income eligibility levels shown in Attachment A" where the attachment is in fact FPL would be a false positive. A benchmark followed by poverty words ("MDH limits, which exceed the federal poverty level") would be missed. So would "MassHealth income limits".

Unrelated to this pattern: published-fields.jsonl contains a fixture sheet, 229999 "ST. EXAMPLE MEDICAL CENTER" (example.org). It is marked published in the sheets table, yet its source src-st-example-fap is not in source_docs, so verify_sheet would reject every field as "source text missing".

## revised rule
Keep the design: a new check percent_of_other_basis(quote, value) in verify.py, called from verify_sheet as a new elif for INCOME_LIMIT_PATHS placed right after bands_under_another_state. It rejects with OTHER_BASIS_REASON = "quote measures income against another benchmark than the poverty level". The values checked are cited.value for the two scalar fields, and every tier's min_fpl_exclusive and max_fpl_inclusive. The structure.py rule-10 note is optional. Change how the basis is read:
(1) Read the text after the value's percent sign. Cut it at the next digit or the next sentence end ((?<=[.;])\s), whichever comes first, then cap it at 20 words. Run every regex on that capped text, not on a 10-word window. The 10-word window cuts "Department of Health and Human Services" after "and", so the (?! and human services) lookahead never sees "human" and the check fires on ordinary FPL sentences.
(2) Exclude HHS as (?! ?(?:and|&) ?human\b). normalize keeps "&", and some policies write "Human Service" in the singular. Also exclude "department of health care" (California's HCAI, New Jersey's "Health Care for the Uninsured").
(3) Don't fire when poverty words (_POVERTY_AFTER) appear anywhere in that text, including after the benchmark, as in "200% of the income eligibility limits (Federal Poverty Guidelines)". At present only words before the benchmark count.
(4) Fire when the benchmark starts within 10 words after "of" ("200% of the income eligibility limits established by the Maryland Department of Health"), or right after the percent sign with only "the" allowed between ("200% MDH limits", "80% AMI"). The second form is not in the current corpus. It costs nothing and fixes the stated "needs 'of'" limitation.
The benchmark list is unchanged: income eligibility limits/levels/standards/thresholds; Medicaid/medical assistance/MDH/state [income] [eligibility] limits/levels/standards/thresholds; median [family|household] income; AMI; SMI; department of health (with the exclusions in 2); MDH; self-sufficiency standard; living wage. Scratch implementation: /private/tmp/claude-501/-Users-grey-Desktop-dev-eventhorizon/b529e6d4-a357-482a-ba81-d6830b9a174d/scratchpad/waive/audit-rest/scratch-nonfpl-challenge/revised.py. Ship it before the running rebuild publishes. Six more UMMS drafts carry the same free-care quote, and today verify_sheet accepts it. This leaves decision B alone: it never reads free-care or "may ... up to 100%" wording.

## candidate code
```python
import re
from decimal import Decimal
from waive.atlas.verify import _NEXT_NUMBER, _PERCENT_SIGN, _POVERTY_AFTER, _number_pattern, _poverty_text

_OTHER_BASIS = re.compile(
    r"\bincome eligibility (?:limits?|levels?|standards?|thresholds?)\b"
    r"|\b(?:medicaid|medical assistance|mdh|state)(?:'s)?(?: income)?(?: eligibility)?"
    r" (?:income )?(?:limits?|levels?|standards?|thresholds?)\b"
    r"|\bmedian (?:family |household )?income\b|\b(?:ami|smi)\b"
    r"|\bdepartment of health\b(?! and human services)|\bmdh\b"
    r"|\bself[- ]sufficiency standard\b|\bliving wage\b"
)
OTHER_BASIS_WORDS = 10

def percent_of_other_basis(quote: str, value: Decimal) -> bool:
    text = _poverty_text(quote).replace(",", "")
    for match in re.finditer(rf"(?<![\d.$]){_number_pattern(Decimal(value))}", text):
        after = text[match.end() : match.end() + 200]
        sign = _PERCENT_SIGN.match(after)
        if sign is None:
            continue
        rest = after[sign.end() :]
        if not re.match(r" ?of\b", rest):
            continue
        window = " ".join(_NEXT_NUMBER.split(rest, maxsplit=1)[0].split()[:OTHER_BASIS_WORDS])
        basis = _OTHER_BASIS.search(window)
        if basis is not None and _POVERTY_AFTER.search(window[: basis.start()]) is None:
            return True
    return False

# verify_sheet, after the bands_under_another_state elif:
#   elif path in INCOME_LIMIT_PATHS and any(percent_of_other_basis(cited.quote or "", x) for x in _limits(cited.value)):
#       report.rejected.append((path, OTHER_BASIS_REASON))
# where _limits(v) = [v] for scalars, [t.max_fpl_inclusive, t.min_fpl_exclusive for t in v] for tiers.
```

## tests
- reject=True value='200' quote='Those with income up to 200% of the income eligibility limits established by the Maryland Department of Health are eligible for free care.'
- reject=True value='200' quote='Free Care - Those with income up to 200% of the income eligibility limits established by the Maryland Department of Health are eligible for free care.'
- reject=True value='300' quote='Reduced Cost Care - Those between 200% and 300% of the income eligibility limits established by the Maryland Department of Health are eligible for discounts on a sliding scale'
- reject=True value='80' quote='Free care is available to households with income at or below 80% of the Area Median Income (AMI).'
- reject=True value='250' quote='Patients with income up to 250% of the State Median Income qualify for a full write-off.'
- reject=True value='150' quote='Free care for income at or below 150% of the Medicaid income limit.'
- reject=True value='200' quote='Free care for incomes up to 200% of MDH limits.'
- reject=False value='100' quote='Household income must be at or below one-hundred percent (100%) of the poverty income guidelines established by the Department of Health and Human Services.'
- reject=False value='200' quote='Household income is less than or equal to 200% of the Federal Poverty Guidelines ("FPG") established by the U.S. Department of Health and Human Services.'
- reject=False value='200' quote='at or below 200 percent of the current Federal Poverty Guidelines as established by the United States Department of Health and Human Services (USDHHS).'
- reject=False value='200' quote='A 100% discount is available to individuals and families with annual incomes at or below 200% of the FPG.'
- reject=False value='200' quote='| less than or equal to 200% | 0%'
- reject=False value='200' quote='Up to 200% Eligible for Medicaid Rates'
- reject=False value='133' quote='the Medicaid income limit of 133% of the federal poverty guidelines'
- reject=False value='200' quote='income up to 200% of the poverty guidelines; patients eligible under the Maryland Department of Health programs'
- reject=False value='100' quote='Patients may be eligible for free care with income up to 100% of the income limits shown in Attachment A.'
- reject=False value='250' quote="free care at or below 250% of the State's poverty guidelines"
- reject=False value='100' quote='Uninsured patients pay 100% of the Medicaid rate.'
- reject=False value='400' quote='at or below 400% of the FPL; patients must first apply for Medicaid'
- reject=False value='200' quote='Those between 200% and 300% of the income eligibility limits established by the Maryland Department of Health are eligible for discounts'

## scan hits
- 210003 eligibility.free_care_max_fpl correct=True: UM Capital Region. The quote says 200% "of the income eligibility limits established by the Maryland Department of Health". The flag is upheld.
- 210043 eligibility.free_care_max_fpl correct=True: UM Baltimore Washington. Same UMMS sentence. The flag is upheld.
- 210049 eligibility.free_care_max_fpl correct=True: UM Upper Chesapeake. Same UMMS sentence. The flag is upheld.
- 210058 eligibility.free_care_max_fpl correct=True: UM Rehabilitation & Orthopaedic Institute. Same UMMS sentence. The flag is upheld.

## challenge false positives

## challenge misses

## one-offs

## notes
The rule fires on exactly the four flagged sheets and on no other field. I ran it on the value of every income field (761: 578 free_care_max_fpl, 7 assistance_ceiling_fpl, 176 discount_tiers, each tier's min and max). I also ran a version that ignores the stored value, checking every percentage in every quote, over all 3,184 published fields. Both gave the same 4 hits. All 20 unit cases pass (scripts are in /private/tmp/claude-501/-Users-grey-Desktop-dev-eventhorizon/b529e6d4-a357-482a-ba81-d6830b9a174d/scratchpad/waive/audit-rest/scratch-nonfpl/).

Searching all 4,587 stored source documents (read-only) found only one with a non-FPL percentage basis: the UMMS policy fap-2aede17aa8d8b543. Its two sentences are free care up to 200% and reduced cost 200–300% "of the income eligibility limits established by the Maryland Department of Health". So a future build that pulls a 200–300 tier from the UMMS policy is caught too, through its 300 max. No document anywhere uses area or state median income, AMI, SMI, a self-sufficiency standard or a living wage as the basis.

Other hits from the wider search were all false leads, and I checked each source:
- Holy Cross 210004/210065 and GBMC 210044 mention "Maryland Department of Health" only in their presumptive-eligibility lists. Their limits are FPL.
- Blanchard Valley's "ami" is a doctor's first name (Ami Orr).
- Lake Regional and Carle use "self-sufficiency" in a patient-rights line and a shelter listing.
- Geisinger, Albany Med and McKenzie mention "Medicaid income limits" as screening (McKenzie's is 133% of the federal poverty guidelines).
- MedStar's "Adjusted Percentage of Poverty Level" is still FPL.

Flat dollar figures cannot pass as FPL values (states_income_limit refuses a number right after "$"). The only stored free-care or ceiling values with no percent sign are 100026 and 100313 ("250 1%" footnote artifact, still FPL) and 360131 (the poverty line written without its number). All three have an FPL basis.

The 46 income quotes that contain no poverty words at all are table fragments or tier lists whose basis is FPL in the surrounding text, so the rule rightly leaves them alone.

Limitation: the rule needs the word "of" ("80% of AMI"). A bare "80% AMI" would get through, but no stored document is written that way.

Effect: the free-care field is the only income rule on all four UMMS sheets (no tiers, no ceiling), so rejecting it holds each sheet. trim_quotes cannot re-anchor these quotes, because widened_span and anchored_span both require poverty words in the passage. The rule is about the income basis only, so it leaves decision B (2026-10-23) untouched.
