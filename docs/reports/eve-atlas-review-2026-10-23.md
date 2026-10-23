# Review of the Eve atlas drafts

Reviewer: the Eve session, 2026-10-23, offline (no model calls). Scope: the PR code, systematic checks over every verified sheet, and a hand read of a random sample of income rules against the stored documents.

## 1. Code

- `verify.quote_found`: multi-span quotes joined with `" || "` are verified span by span. `trim_quotes` now leaves such quotes whole (it would otherwise keep one span and fail the value check). No sheet in the final set uses multi-span quotes, so the data does not exercise this; the change is a capability for the next run.
- `verify._lower_bound_in`: a tier's exclusive lower bound is also accepted as the written inclusive start (201%, 200.1%, 200.01%). The upper bound and the percent must still appear. Risk considered: a quote naming 201 for an unrelated band could satisfy the lower bound of a 200 tier; the upper bound check keeps that from passing unless both numbers coincide.
- `eve_import`: two rules added after this review. A tier whose quote says "up to N%" is a cap, not a fixed discount, and is skipped with a note (23 sheets). A free-care line below 100% FPL is held for review instead of recorded (4 sheets).
- `cli.import-eve --sources-dir`: stores the refetched document text with each sheet, so `verify` in Waive runs against the same bytes the quotes were checked on.
- Tests: ruff clean, the atlas, verify and publish suites pass.

## 2. Systematic checks (1,340 verified sheets)

| Check | Result |
|---|---|
| First discount tier starts away from the free-care line | 0 |
| Judge objected to an income rule the verifier accepted | 0 |
| Same policy document, different free-care values | Banner Health: Arizona and other states 200%, Colorado 250%. State-specific rows in one document; both correct. Centura and CommonSpirit Colorado: see 4 |
| Free-care line outside 100% to 400% | 3, now held for review by the import rule |
| Free-care quote is a copay or screening table row | 9 |
| Free-care quote is a table header with no stated benefit | 12 |
| Free-care quote hedges with "may be eligible" or "will qualify for assistance" and never says free | 13 |
| Tier quote says "up to N%" | 11 in the verified set, 23 in the reconverted set; now skipped |
| Income rule verified on a document whose bytes changed since the agent read it | 60; the verifier re-ran on the new text, so these stand |

## 3. Hand read, twelve random verified income rules

Ten correct against the document. One wrong: Family Health West (CO 061302), where "HDC 0 to 250%, screen for HDC eligibility" is Colorado's Hospital Discounted Care screening row, not free care; the hospital's own charity starts at 251%. One borderline: Mobridge Regional (SD 431325), "up to a 50% discount" recorded as a fixed 50% tier; now skipped by the cap rule.

## 4. Needs your eyes

### Colorado Hospital Discounted Care and copay tables

Decision (owner, 2026-10-23): Colorado HDC rows are recorded as a state program, not as the free-care line. The import now moves a Colorado free-care line whose quote is an HDC screening row, a "$0 copay" row, or a sentence about the legislation into `programs.state_programs` as "Colorado Hospital Discounted Care (income at or below 250% FPL)", citing the same quote and source. Six sheets moved: Mercy Regional 060013, Centura St Anthony North 060104, St Anthony Summit 060118, St Francis Interquest 060133, Family Health West 061302, St Thomas More 061344. A copay row with a dollar amount is held for review in any state; St. Catherine KS 170023 is held.

### Table headers quoted without the benefit

The quote names the income band but not what the band gets. Usually the table's first row is "100% discount"; the quote should include it. Read each document before publishing.

- GA 110003 Memorial Satilla Health: recorded 200% | quote: "In most cases, this will apply to patients who fall between 0 - 200% of the Federal Poverty Level"
- GA 110036 SAVANNAH HEALTH SERVICES LLC DBA M: recorded 200% | quote: "In most cases, this will apply to patients who fall between 0 - 200% of the Federal Poverty Level"
- NC 340020 CENTRAL CAROLINA HOSPITAL: recorded 200% | quote: "Poverty Level 0-200% 201-225% 226%-250% 251%-275% 276%-300%"
- NC 340126 WILSON MEDICAL CENTER: recorded 200% | quote: "Poverty Level 0-200% 201-250% 251-300%"
- NC 341305 DLP SWAIN COUNTY HOSPITAL LLC: recorded 200% | quote: "& DC Family size Poverty Level 0-200% 201-250% 251-300%"
- NE 281339 MEMORIAL HEALTH CARE SYSTEMS: recorded 175% | quote: "FPL 0% to 175% 176% to 189%"
- NH 300034 CATHOLIC MEDICAL CENTER: recorded 200% | quote: "In most cases, this will apply to patients who fall between 0 - 200% of the Federal Poverty Level"
- TN 440006 TRISTAR SKYLINE MEDICAL CENTER: recorded 200% | quote: "this will apply to patients who fall between 0 - 200% of the Federal Poverty Level"
- UT 460042 LAKEVIEW HOSPITAL: recorded 200% | quote: "this will apply to patients who fall between 0 - 200% of the Federal Poverty Level"
- VA 490107 RESTON HOSPITAL CENTER: recorded 200% | quote: "In most cases, this will apply to patients who fall between 0 - 200% of the Federal Poverty Level"
- VA 490118 HENRICO DOCTORS' HOSPITAL: recorded 200% | quote: "In most cases, this will apply to patients who fall between 0 - 200% of the Federal Poverty Level"
- WI 520089 UNITYPOINT HEALTH - MERITER: recorded 300% | quote: "≤ 300% FPG ≤ 400% FPG ≤ 600% FPG"

### Hedged wording, no free-care word in the quote

"May be eligible for full assistance" (Baptist Health system in AR, KY, IN) and "will qualify for financial assistance" (Baptist in AL). The policies may well give 100% at that line; the quote does not say so. Waive's verifier accepts them; your call.

- AL 010016 SHELBY BAPTIST MEDICAL CENTER: recorded 300% | quote: "Uninsured individuals or family/households who meet three hundred percent (300%) or below of FPG will qualify for financ"
- AL 010101 CITIZENS BAPTIST MEDICAL CENTER: recorded 225% | quote: "If a validated financial statement application reflects an income at or below 225% of the most current Federal Poverty L"
- AL 010103 PRINCETON BAPTIST MEDICAL CENTER: recorded 300% | quote: "Uninsured individuals or family/households who meet three hundred percent (300%) or below of FPG will qualify for financ"
- AR 040076 BAPTIST HEALTH MEDICAL CENTER-HOT : recorded 300% | quote: "If your family income is less than or equal to 300% of the federal poverty level, then you may be eligible for full assi"
- IN 150044 BAPTIST HEALTH FLOYD: recorded 300% | quote: "If your family income is less than or equal to 300% of the federal poverty level, then you may be eligible for full assi"
- KS 171315 NEMAHA VALLEY COMMUNITY HOSPITAL: recorded 200% | quote: "Financial Assistance is available to all patients up to 200% Federal Poverty Guidelines."
- KY 180103 BAPTIST HEALTH LEXINGTON: recorded 300% | quote: "If your family income is less than or equal to 300% of the federal poverty level, then you may be eligible for full assi"
- KY 180104 BAPTIST HEALTH PADUCAH: recorded 300% | quote: "If your family income is less than or equal to 300% of the federal poverty level, then you may be eligible for full assi"
- KY 180130 BAPTIST HEALTH LOUISVILLE: recorded 300% | quote: "If your family income is less than or equal to 300% of the federal poverty level, then you may be eligible for full assi"
- MI 230046 UNIVERSITY OF MICHIGAN HEALTH SYST: recorded 300% | quote: "A patient may qualify for MSupport financial assistance if the patient's household income does not exceed 300% of the es"
- MN 241372 MADISON HOSPITAL: recorded 200% | quote: "Income at or below 200% of Federal Poverty Guidelines (FPG) will qualify for Financial Assistance."
- OK 370019 GREAT PLAINS REGIONAL MEDICAL CENT: recorded 200% | quote: "Parameters have been established to allow for an assistance adjustment if the gross household income is at or below 200%"
- OR 380050 SKY LAKES MEDICAL CENTER: recorded 300% | quote: "if your yearly income is less than or equal to 300% of the current Federal Poverty Guideline, you may not have to pay yo"

### Values outside the plausible range, now held

- CO 060006 MONTROSE REGIONAL HEALTH: recorded 40.9% | quote: "Patients whose family income is not more than 40.9% of the FPL are eligible to receive a 100% discount off billed charge"
- CO 060107 NATIONAL JEWISH HEALTH: recorded 40% | quote: "Z 0 - 40% and Homeless"
- KS 171350 RUSSELL REGIONAL HOSPITAL: recorded 70% | quote: "Patient/guarantors that fall below Level 1, 70% of FPL, will receive a 100% discount."

### Capped tiers, now skipped

- CO 060054 COMMUNITY HOSPITAL: tiers [['100', '150', 50]]
- MN 240166 MAYO CLINIC HEALTH SYSTEM - FAIRMO: tiers [['200', '400', 50]]
- MN 241345 MAYO CLINIC HEALTH SYSTEM - WASECA: tiers [['200', '400', 50]]
- MN 241346 MAYO CLINIC HEALTH SYSTEM - CANNON: tiers [['200', '400', 50]]
- MN 241361 MAYO CLINIC HEALTH SYSTEM NEW PRAG: tiers [['200', '400', 50]]
- ND 351302 MCKENZIE COUNTY HEALTHCARE SYSTEMS: tiers [['200', '250', 50]]
- SD 431325 MOBRIDGE REGIONAL HOSPITAL - CAH: tiers [['100', '200', 50]]
- SD 431334 WINNER REGIONAL HEALTHCARE CENTER : tiers [['100', '150', 50]]
- VT 471305 COPLEY HOSPITAL: tiers [['300', '350', 75], ['350', '400', 50]]
- WI 521302 MAYO CLINIC HEALTH SYSTEM OAKRIDGE: tiers [['200', '400', 50]]
- WI 521340 MAYO CLINIC HEALTH SYSTEM-RED CEDA: tiers [['200', '400', 52]]

## 5. Not in question

- The 1,003 review-ready sheets outside the buckets above read cleanly in the sample.
- Phone fields: Waive stores "A or B" text on purpose; no change.
- 301 sheets without an income rule stay as contact and application records.

## 6. Owner decisions, applied 2026-10-23

Decided on the review page and applied by `import-eve --decisions` (defaults to `data/atlas-eve/_decisions.json`). A "discount" decision removes the free-care line and records the same income limit as `assistance_ceiling_fpl` with the same quote and source, unless the sheet already records a ceiling. "Cannot tell" removes the line and adds the hospital to the rebuild list. Every decision is written into the sheet's `eve_review` notes.

Totals: 27 discount or assistance limit, 2 cannot tell (National Jewish Health CO 060107, Savannah Health Services GA 110036), 1 accept (Cavalier County Memorial ND 351323; its extracted value was 0%, which stays unrecorded).

| State | CCN | Hospital | Bucket | Extracted | Decision |
|---|---|---|---|---|---|
| AL | 010101 | Citizens Baptist Medical Center | hedged | 225% | discount or assistance limit, not free care |
| AL | 010103 | Princeton Baptist Medical Center | hedged | 300% | discount or assistance limit, not free care |
| AL | 010016 | Shelby Baptist Medical Center | hedged | 300% | discount or assistance limit, not free care |
| AR | 040076 | Baptist Health Medical Center-Hot Springs County | hedged | 300% | discount or assistance limit, not free care |
| CO | 060006 | Montrose Regional Health | held | 40.9% | discount or assistance limit, not free care |
| CO | 060107 | National Jewish Health | held | 40% | cannot tell, rebuild |
| GA | 110003 | Memorial Satilla Health | header | 200% | discount or assistance limit, not free care |
| GA | 110036 | Savannah Health Services Llc Dba Memorial Health University Medical Center | header | 200% | cannot tell, rebuild |
| IN | 150044 | Baptist Health Floyd | hedged | 300% | discount or assistance limit, not free care |
| KS | 171315 | Nemaha Valley Community Hospital | hedged | 200% | discount or assistance limit, not free care |
| KS | 171350 | Russell Regional Hospital | held | 70% | discount or assistance limit, not free care |
| KS | 170023 | St. Catherine Hospital Garden City | held | 150% | discount or assistance limit, not free care |
| KY | 180103 | Baptist Health Lexington | hedged | 300% | discount or assistance limit, not free care |
| KY | 180130 | Baptist Health Louisville | hedged | 300% | discount or assistance limit, not free care |
| KY | 180104 | Baptist Health Paducah | hedged | 300% | discount or assistance limit, not free care |
| MI | 230046 | University of Michigan Health System | hedged | 300% | discount or assistance limit, not free care |
| MN | 241372 | Madison Hospital | hedged | 200% | discount or assistance limit, not free care |
| NC | 340020 | Central Carolina Hospital | header | 200% | discount or assistance limit, not free care |
| NC | 341305 | Dlp Swain County Hospital Llc | header | 200% | discount or assistance limit, not free care |
| NC | 340126 | Wilson Medical Center | header | 200% | discount or assistance limit, not free care |
| ND | 351323 | Cavalier County Memorial Hospital Association | held | 0% | accept as free care |
| NE | 281339 | Memorial Health Care Systems | header | 175% | discount or assistance limit, not free care |
| NH | 300034 | Catholic Medical Center | header | 200% | discount or assistance limit, not free care |
| OK | 370019 | Great Plains Regional Medical Center | hedged | 200% | discount or assistance limit, not free care |
| OR | 380050 | Sky Lakes Medical Center | hedged | 300% | discount or assistance limit, not free care |
| TN | 440006 | Tristar Skyline Medical Center | header | 200% | discount or assistance limit, not free care |
| UT | 460042 | Lakeview Hospital | header | 200% | discount or assistance limit, not free care |
| VA | 490118 | Henrico Doctors' Hospital | header | 200% | discount or assistance limit, not free care |
| VA | 490107 | Reston Hospital Center | header | 200% | discount or assistance limit, not free care |
| WI | 520089 | Unitypoint Health Meriter | header | 300% | discount or assistance limit, not free care |

## 7. After the review fixes

Redirects are now followed and each source is fetched once per run. Final verification on refetched documents: 1013 of 1,340 sheets keep an income rule. Verified data, queue, SQLite copy and bundle were regenerated from this state before merge.
