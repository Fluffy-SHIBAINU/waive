# Publication Audit Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the verifier reject the field readings the 2026-10-24 publication audit found wrong, so no published sheet carries them, without rejecting any correct published field.

**Architecture:** Each rule is a small predicate in one of three places:
- `src/waive/atlas/verify.py`: field-level, called from `verify_sheet`.
- `src/waive/atlas/publish.py`: sheet-level, in `sheet_inconsistencies`.
- `src/waive/atlas/pipeline.py`: document-level set-asides, in `_documents_set_aside` and the build's `_documents_for_structurer`.

Rules only reject fields, set documents aside or hold sheets; they never add or change a value. A diagnosis workflow designed each rule, tested it on real quotes and ran it over all 605 published sheets. A skeptic then challenged it and reran it. The tested candidate code and cases are in the appendix.

**Tech Stack:** Python 3.12, uv, pytest (with pytest-socket: no network), ruff.

**Spec:** `docs/reports/publication-audit-2026-10-24.md`. Appendix: one folder per pattern, `docs/superpowers/plans/2026-10-24-waive-audit-rules/<pattern>/`, holding:
- `rule.md`: why the reading passed, the rule, the skeptic's challenge and **revised rule**, test cases and scan hits.
- `*.py.txt`: tested candidate code. It is scratch code that imports scratch paths, so port the logic and do not copy the imports.

Where the revised rule and the candidate differ, the revised rule wins.

## Global Constraints

- Gate after every task: `uv run ruff format . && uv run ruff check . && uv run pytest` passes.
  - One failure is expected under `.claude/worktrees/`: `tests/unit/test_log_hygiene.py::test_filter_redacts_a_traceback_that_carries_personal_values`. The worktree folder name contains a run of 7 digits, which trips that test. It is not caused by the task.
- Work on a worktree branch.
- Never open or write `var/waive.db` from a test or a task step.
- No Tavily or Token Factory calls.
- Owner decision B (2026-10-23) stays as it is:
  - Only quotes of the form "may … up to 100%" stop counting as free care (`quotes_assistance_ceiling`).
  - "may be eligible for free care" and CommonSpirit's "up to a 100% discount" stay free care.
  - No rule here may reject a free-care field for hedged wording alone.
- A rule rejects a field, sets a document aside or holds a sheet. It never writes a value.
- Each rejection reason is a module-level string constant with the exact text this plan gives.
- Docstrings and comments are plain prose in the repo's style. Name the hospital or policy that motivated a rule, with its CCN, as the existing rules do.
- Matching runs on `normalize(quote)` unless a step says original case.

## Review Focus

1. **Income bands that look like ranges.** These are not discount ranges; Task 1 pins them:
   - "201-300% discount 75%"
   - "Patients 201-300% FPL receive a 75% discount"
   - "0-100% of federal poverty level 100% discount"
2. **The poverty level's own publisher.** "of the Federal Poverty Guidelines published by the Department of Health and Human Services" is the poverty level, not another benchmark. Task 2 pins it.
3. **Place names that contain a state.** Task 3 pins these:
   - "Washington County", "Kansas City" and "Virginia Beach" must neither satisfy nor fail residency wrongly.
   - "West Virginia" is not "Virginia".
   - "North and South Carolina" names both states.
4. **A genuine exclusion of insured patients.** "Patients with insurance are not eligible for financial assistance" must still support `insured_patients_covered = false` after the insured rewrite. Task 4 pins it.
5. **Hedged ceilings under decision B.**
   - "You may qualify for a discount of up to 100% if your income is at or below 400%" stays a valid assistance ceiling (Task 7, R4′).
   - An insured patient's co-pay waiver does not count as a free band above the limit (Task 6, R8′).

---

### Task 1: Discount ranges written with one percent sign or a scale phrase

**Files:**
- Modify: `src/waive/atlas/verify.py` (`discount_range_in_quote`, around lines 142-168)
- Test: `tests/unit/test_verify.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `discount_range_in_quote(quote: str) -> set[Decimal]` keeps its signature and return meaning (range ends and floors). The `verify_sheet` branch that rejects with `RANGE_REASON` is unchanged.

- [ ] **Step 1: Write the failing tests.** Add `test_discount_range_in_quote_reads_one_sign_ranges_and_scale_phrases`.
  - Parametrize it over every case in `range-flat/rtests.py.txt` and in the "## tests" section of `range-flat/rule.md`.
  - Assert `discount_range_in_quote(quote) & stored == expected`. Here `stored` is the case's tier percents, and `expected` is non-empty exactly when the case says reject.
  - The cases must include:
    - Hartford: "will qualify for a 15-75% discount" rejects 75 and 15.
    - Holton (171319): "may qualify for a discount from a scale of 10% to 75%" rejects 75 and 10.
    - "a discount on a sliding scale from 25% to 75%" and "a discount based on a sliding scale of 20%-80%" are ranges.
    - The non-ranges of Review Focus 1 and "$1,000 - 20% discount" are not read as ranges.
- [ ] **Step 2: Run them and see them fail.** Run `uv run pytest tests/unit/test_verify.py -k one_sign_ranges -v`. Expected: the Hartford and Holton cases FAIL.
- [ ] **Step 3: Implement.** Extend `discount_range_in_quote` with the three patterns of the **revised** rule in `range-flat/rule.md`. The existing patterns stay.
  - The three patterns:
    - one sign, with the discount word after the range;
    - the discount noun first, with a required connective (of, from or between);
    - a scale phrase.
  - Use the revised lookbehind `(?<![\d.,$])`, and `P = %|percent|per cent`.
  - A pair counts only when `low < high <= 100`.
- [ ] **Step 4: Run the tests and see them pass.** Run `uv run pytest tests/unit/test_verify.py -v`. Expected: PASS, with every existing range test still green.
- [ ] **Step 5: Commit.** `git commit -m "fix(atlas): read discount ranges with one percent sign or a scale phrase (Hartford HealthCare, Holton)"`

### Task 2: Income limits measured against another benchmark

**Files:**
- Modify: `src/waive/atlas/verify.py`. Add a new predicate near `names_percent_of_poverty_level`, and a new `elif` in `verify_sheet` right after the `bands_under_another_state` branch.
- Test: `tests/unit/test_verify.py`

**Interfaces:**
- Produces:
  - `percent_of_other_basis(quote: str, value: Decimal) -> bool`
  - `OTHER_BASIS_REASON = "quote measures income against another benchmark than the poverty level"`

- [ ] **Step 1: Write the failing tests.**
  - `test_percent_of_other_basis`, parametrized over `non-fpl-basis/test_revised.py.txt`. The cases must include:
    - The UMMS sentence "200% of the income eligibility limits established by the Maryland Department of Health": True for 200.
    - The sentence in Review Focus 2: False.
    - "200% of the Federal Poverty Level": False.
  - `test_verify_sheet_rejects_an_income_limit_on_another_benchmark`:
    - A sheet with `free_care_max_fpl` 200 and the UMMS quote is rejected with `OTHER_BASIS_REASON`.
    - A tier with `max_fpl_inclusive` 200 under the same quote is rejected the same way.
- [ ] **Step 2: Run them and see them fail.** Run `uv run pytest tests/unit/test_verify.py -k other_basis -v`. Expected: FAIL (name not defined).
- [ ] **Step 3: Implement** `percent_of_other_basis` by the **revised** rule.
  - Read the text after the value's percent sign.
  - Cut it at the next digit or the next sentence end, whichever comes first.
  - Cap what is left at 20 words.
  - Run the benchmark regexes from `non-fpl-basis/revised.py.txt` on that text.
  - In `verify_sheet`, apply the check when `path in INCOME_LIMIT_PATHS`:
    - for the two scalar fields, check `cited.value`;
    - for `discount_tiers`, check every tier's `min_fpl_exclusive` and `max_fpl_inclusive`.
- [ ] **Step 4: Run the tests and see them pass.** Run `uv run pytest tests/unit/test_verify.py -v`. Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "fix(atlas): reject income limits measured against another benchmark (UMMS)"`

### Task 3: Residency names every stored state as a whole state

**Files:**
- Modify: `src/waive/atlas/verify.py`. Add a new predicate and a new `elif path == "eligibility.residency"` in `verify_sheet`. Use `waive.atlas.states` for state names.
- Test: `tests/unit/test_verify.py`

**Interfaces:**
- Produces:
  - `residency_problem(value: list[str], quote: str) -> str | None`
  - `RESIDENCY_REASON = "quote does not name every listed state as a whole state"`
  - `RESIDENCY_BOTH_REASON = "quote says residents of other states qualify too"`

- [ ] **Step 1: Write the failing tests.** Add `test_residency_problem`, parametrized over `residency/test_revised.py.txt`. The cases must include:
  - MedStar §3.1.1, "proof of residency within Maryland, northern Virginia, or within the District of Columbia":
    - with `["MD","VA","DC"]` it gives `RESIDENCY_REASON`;
    - with `["MD","DC"]` it gives None.
  - A quote naming no state, with `["MD"]`, gives `RESIDENCY_REASON`.
  - The place-name cases of Review Focus 3.
  - "Wisconsin1" counts as Wisconsin.
  - "Minnesota residents and non-Minnesota residents" gives `RESIDENCY_BOTH_REASON`.
- [ ] **Step 2: Run them and see them fail.** Run `uv run pytest tests/unit/test_verify.py -k residency_problem -v`. Expected: FAIL.
- [ ] **Step 3: Implement** by the **revised** rule in `residency/rule.md`:
  - letter-only boundaries, longest state name first;
  - a region word before the name;
  - a place word before or after the name;
  - a parenthesised postal code;
  - paired North/South;
  - D.C.

  The field is rejected, not pruned. Residency is not an income rule, so the sheet is not held.
- [ ] **Step 4: Run the tests and see them pass.** Run `uv run pytest tests/unit/test_verify.py -v`. Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "fix(atlas): residency must name each stored state as a whole state (MedStar northern Virginia)"`

### Task 4: One insured-patients check

**Files:**
- Modify: `src/waive/atlas/verify.py`.
  - In `verify_sheet`, replace the two `insured_patients_covered` branches (the `quotes_only_the_agb_cap` one and the `quote_addresses_insured` one) with one branch that calls the new function.
  - Keep the old helpers only if other code still uses them; check with `grep -rn` first.
- Test: `tests/unit/test_verify.py`

**Interfaces:**
- Produces: `insured_field_problem(quote: str, value: bool) -> str | None`. It returns one of these reasons, or None:
  - `AGB_CAP_REASON` or `INSURED_REASON` (both already exist);
  - a new constant from `insured/revised.py.txt`, with the exact text given there: `INSURED_LEFT_OUT_REASON`, `PRICE_CAP_REASON`, `BARE_LABEL_REASON`, `PAYMENT_PLAN_REASON` or `ABSENT_REASON`.

- [ ] **Step 1: Write the failing tests.** Add `test_insured_field_problem`, parametrized over `insured/tests.py.txt` plus the audit's cases:
  - Rejected with value False:
    - Mizell (010007): "may be deemed ineligible".
    - Caldwell (181322): "uninsured, underinsured".
    - Mackinac (231306): the uninsured self-pay discount.
  - Rejected with value True:
    - a billing-statement quote (040004);
    - a payment-plan quote (210027);
    - the AGB cap (240187);
    - the bare heading "Underinsured:" (241323).
  - Accepted:
    - the exclusion in Review Focus 4, with False;
    - "Insured patients who do not have the ability to pay for … portions not covered by insurance", with True.
- [ ] **Step 2: Run them and see them fail.** Run `uv run pytest tests/unit/test_verify.py -k insured_field_problem -v`. Expected: FAIL.
- [ ] **Step 3: Implement** by `insured/revised.py.txt` and the revised rule in `insured/rule.md`.
  - Use the narrowed price-cap tail from the challenge: `(?:(?!,\s*(?:and|but|while|however|although)\b)[^.;])*`.
  - Include the challenge's billing-statement addition, for 040004.
- [ ] **Step 4: Run the tests and see them pass.** Run `uv run pytest tests/unit/test_verify.py -v`. Expected: PASS. Every existing insured test passes too; if one must change, give a one-line reason in the commit body.
- [ ] **Step 5: Commit.** `git commit -m "fix(atlas): one insured-patients check (underinsured help, billing and payment-plan quotes)"`

### Task 5: Discount-tier readings (R1′, R2, R9, R10, R11)

**Files:**
- Modify: `src/waive/atlas/verify.py`. Add new predicates, and new `elif` branches in `verify_sheet` after the `RANGE_REASON` branch.
- Test: `tests/unit/test_verify.py`

**Interfaces:**
- Produces:

| Rule | Function | Reason constant | Reason text |
|---|---|---|---|
| R1′ | `tiers_need_hardship(quote: str, tiers: list[DiscountTier]) -> bool` | `HARDSHIP_TIER_REASON` | "the band needs a financial hardship, so it is not an income band" |
| R2 | `tier_is_additional(quote: str, tiers: list[DiscountTier]) -> bool` | `ADDITIONAL_TIER_REASON` | "the discount is added to another one" |
| R9 | `insured_only_table(quote: str, document_text: str) -> bool` | `INSURED_TABLE_REASON` | "the table is for insured patients only" |
| R10 | `wrong_regulated_column(quote: str, state: str, tiers: list[DiscountTier]) -> bool` | `REGULATED_COLUMN_REASON` | "the tier is read from the wrong column of the table for this hospital" |
| R11 | `tiers_split_by_service(quote: str) -> bool` | `TIERS_SPLIT_REASON` | "the table gives different discounts for inpatient and outpatient care" |

- [ ] **Step 1: Write the failing tests.** Add one parametrized test per function, over `table-and-program/tests.py.txt` and `tests_revised.py.txt`. The cases must include:
  - R1′:
    - Rejected: the hardship sentences of Union Hospital of Cecil County (210032), at 401–450% (40%) and 451–500% (35%).
    - Not rejected: the MassHealth "Medical Hardship Family Countable Income" sentence.
  - R9:
    - Rejected: Henry Ford's "Discount Off of Balance Due" table under an "Insured Patients:" lead-in (230047).
    - Not rejected: "uninsured and insured".
  - R10, on MedStar's "HSCRC-Regulated Services | Washington Hospitals…" rows:
    - For an MD hospital (210018, the 201–250% row), 75 is accepted and 80 is rejected.
    - For a DC hospital, the same row accepts 80.
  - R11:
    - Rejected: the Tufts and Berkshire service-column headers.
    - Not rejected: "Inpatient and outpatient services will be discounted by".
- [ ] **Step 2: Run them and see them fail.** Run `uv run pytest tests/unit/test_verify.py -k "hardship or additional or insured_only or regulated_column or split_by_service" -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - R2, R9 and R10: follow `table-and-program/candidate.py.txt`.
  - R1′ and R11: follow `revised.py.txt` and the revised rule in `table-and-program/rule.md`.
  - R9 reads the 250 normalized characters before the quote in `documents[cited.source_id]`. `verify_sheet` already holds that document as `text`.
  - R10 runs only when the quote contains both "hscrc-regulated" and "non-hscrc". A Maryland hospital must store the first column; any other state's hospital must store the last.
- [ ] **Step 4: Run the tests and see them pass.** Run `uv run pytest tests/unit/test_verify.py -v`. Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "fix(atlas): reject hardship, additional, insured-only, wrong-column and service-split tiers"`

### Task 6: Free-care readings (R3′, R8′)

**Files:**
- Modify: `src/waive/atlas/verify.py`. Add new predicates, and new `elif` branches for `eligibility.free_care_max_fpl` placed before the `quotes_assistance_ceiling` branch.
- Test: `tests/unit/test_verify.py`

**Interfaces:**
- Produces:
  - `free_band_charges_patient(quote: str) -> bool`
  - `FREE_BAND_FEE_REASON = "quote charges the patient within the free band"`
  - `full_help_band_above(value: Decimal, document_text: str) -> bool`
  - `FREE_LIMIT_BELOW_FULL_HELP_REASON = "the document gives full assistance to a band above this limit"`

- [ ] **Step 1: Write the failing tests.** The cases must include:
  - R3′:
    - Rejected: University Health Lakewood (260102), "$5 per outpatient visit" in the 0–100% band.
    - Not rejected: "patient fees are waived" and "no patient fees".
  - R8′:
    - Rejected: Emory's policy text (110069), where uninsured patients at 201%–350% FPG get 100% financial assistance, rejects a limit of 200.
    - Not rejected:
      - Enloe's $100 co-pay band (050039);
      - Lima's insured co-pay waiver (360009);
      - a "may … up to 100%" sentence (decision B).
- [ ] **Step 2: Run them and see them fail.** Run `uv run pytest tests/unit/test_verify.py -k "charges_patient or full_help" -v`. Expected: FAIL.
- [ ] **Step 3: Implement** by the revised R3′ and R8′ in `table-and-program/rule.md` and `revised.py.txt`.
  - R3′ ignores a fee that is followed, within 3 words, by "waived" or by "not (be) charged|collected|required|apply".
  - R8′ skips any sentence that:
    - matches `_HEDGED_UP_TO_FULL`;
    - names cost sharing;
    - is about insured patients only;
    - contains an R3′ fee.
  - R8′ reads only the field's own document, `documents[cited.source_id]`.
- [ ] **Step 4: Run the tests and see them pass.** Run `uv run pytest tests/unit/test_verify.py -v`. Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "fix(atlas): free care that still charges a fee, and a free limit under a full-help band"`

### Task 7: Ceilings, other-state facilities and band consistency (R4′, R7′, R5, R6), with holds

**Files:**
- Modify:
  - `src/waive/atlas/verify.py`: R4′ inside `ceiling_problem`, R7′ in the `INCOME_LIMIT_PATHS` branch, and `HOLDING_REASONS`.
  - `src/waive/atlas/publish.py`: R5 and R6 in `sheet_inconsistencies`.
  - `src/waive/atlas/pipeline.py`: holds in `_primary` (near line 583) and in `withdraw_rejected` (near line 1262).
- Test:
  - `tests/unit/test_verify.py`
  - `tests/unit/test_publish.py`
  - the pipeline test that covers `withdraw_rejected` holds (find it with `grep -rn PATIENT_SHARE_REASON tests/`)

**Interfaces:**
- Produces:
  - `CEILING_FREE_BAND_REASON = "quote states the free-care band, not the ceiling"`, returned by `ceiling_problem`.
  - `names_other_state_facility(quote: str, state: str) -> bool`, which runs on the original case.
  - `OTHER_STATE_FACILITY_REASON = "quote states the band for another state's facilities, named by its postal code"`
  - `HOLDING_REASONS = (PATIENT_SHARE_REASON, CEILING_FREE_BAND_REASON, OTHER_STATE_FACILITY_REASON)`. A rejection with any of these reasons holds the sheet, both in `_primary` and in `withdraw_rejected`.
  - Two new `sheet_inconsistencies` problems, with exact texts:
    - R5: `"eligibility.assistance_ceiling_fpl: {c:.0f}% is below the sheet's own income rules"`
    - R6: `"eligibility.discount_tiers: no rule covers incomes from {a:.0f}% to {b:.0f}%"`

- [ ] **Step 1: Write the failing tests.**
  - R4′ (`ceiling_problem`):
    - Russell (171350): its 100%-discount line stored as the ceiling, value 70, gives `CEILING_FREE_BAND_REASON`.
    - The hedged ceiling in Review Focus 5 gives None.
    - A sentence that gives both the free band and a sliding scale gives None.
  - R7′ (`names_other_state_facility`):
    - "An IL UPH Hospital" for an IA hospital (160104) is True.
    - The same quote for an IL hospital is False.
    - "in", "or" and "me" in lower case are never read as state codes.
  - R5: a ceiling of 70 with tiers up to 200 is a problem.
  - R6:
    - Free care to 100 with a first tier from 300 is a problem (260102: the 101–300 band is missing).
    - A tier starting at 171 after one ending at 170 is not a problem.
  - Holds:
    - In a pipeline test, `verify_sheet` rejects with `CEILING_FREE_BAND_REASON`; assert the sheet comes back held.
    - Assert the same through `withdraw_rejected`.
- [ ] **Step 2: Run them and see them fail.** Run `uv run pytest tests/unit/test_verify.py tests/unit/test_publish.py -k "ceiling or other_state_facility or inconsisten" -v`. Expected: FAIL.
- [ ] **Step 3: Implement** by the revised R4′ and R7′ and the R5 and R6 as first proposed (all in `table-and-program/rule.md`).
  - R4′: after the free sentence is removed, the quote must state no partial help.
  - R7′: the match needs a determiner plus an all-caps system acronym.
  - Do not add a general hold for "no free limit and the first tier starts above 0". It would also hold 19 other published sheets (see rule.md).
- [ ] **Step 4: Run the tests and see them pass.** Run `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "fix(atlas): ceilings read from the free band, other-state facility bands and band gaps hold the sheet"`

### Task 8: Documents that belong to another hospital or another state

**Files:**
- Modify:
  - `src/waive/atlas/pipeline.py`:
    - `_documents_set_aside` (line 254): apply the new clauses after `_never_the_hospitals`.
    - `_documents_for_structurer`: detach the same documents, filing a `foreign_documents` or `other_state_documents` review item, as `_drop_other_state_documents` does.
    - `_share_sibling_sources` (line 405): never lend a document that clause C sets aside.
  - `src/waive/atlas/verify.py`: the reason constants, beside `DENTAL_REASON`.
  - `_documents_set_aside` needs the state's registry rows for clauses A2, S and B. Pass them in from its callers; `repo` already lists hospitals by state.
- Test: the `tests/unit/test_pipeline*.py` file that tests `_documents_set_aside` (find it with `grep -rn _documents_set_aside tests/`).

**Interfaces:**
- Produces, in `pipeline.py`:
  - `foreign_documents(hospital: HospitalRef, sources: list[tuple[SourceDoc, str]], registry: list[HospitalRow]) -> set[str]` (clauses A2 and S)
  - `sibling_policies(hospital: HospitalRef, sources: list[tuple[SourceDoc, str]], registry: list[HospitalRow]) -> set[str]` (clause B)
  - `other_state_documents(hospital: HospitalRef, sources: list[tuple[SourceDoc, str]]) -> set[str]` (clause C)
- Produces, in `verify.py`:
  - `FOREIGN_OWNER_REASON`, with the text from `owner-scope/revised.py.txt`
  - `SIBLING_POLICY_REASON = "quote is from another hospital's policy; the hospital's site links its own"`
  - `OTHER_STATE_DOCUMENT_REASON = "quote is from another state's version of the system policy"`

- [ ] **Step 1: Write the failing tests.** Port the 37 cases of `owner-scope/tests_revised.py.txt` (built on `tests_final.py.txt`) to synthetic `SourceDoc` and registry rows. The cases must include:
  - Set aside:
    - UnityPoint 1.BR.34 for Clarke County (161348): the attached Schedule lists other Iowa UPH hospitals and not Clarke County.
    - Carle West Region's Schedule D (Methodist, Pekin, Proctor) for Carle Hoopeston (141316), even though it sits on the hospital's own domain.
    - Emory's "Hospitals include:" list, which leaves out Emory Houston, for 110129.
  - Not set aside:
    - A CommonSpirit G-003 policy with no covered-hospital list (CHI Oakes, 351315).
    - Corewell's application on assets.contentstack.io.
    - A UNC Health page that says "UNC Health Appalachian", for unchealthappalachian.org.
  - Clause C:
    - Set aside: Ascension Providence (TX), with "Financial assistance Ascension Saint Agnes in Maryland" in the title.
    - Not set aside: a document whose site name is "North Mississippi Health Services".
  - Lending: a Colorado hospital never borrows Dignity Sacramento's California application from a sibling.
- [ ] **Step 2: Run them and see them fail.** Run `uv run pytest tests/unit -k "foreign or sibling_polic or other_state_doc" -v`. Expected: FAIL.
- [ ] **Step 3: Implement** by the revised clauses A2, S and C, and clause B as first proposed. Sources: `owner-scope/rule.md`, `revised.py.txt` and `candidate_final.py.txt`.
  - Ruling (record it in the ledger): clause C applies in `_documents_set_aside` even when every stored document is another state's.
  - This extends 7.9's "nothing to prefer" fallback beyond Kaiser. It only holds sheets that quote another state's policy, and no reader should be shown that as their hospital's policy.
- [ ] **Step 4: Run the tests and see them pass.** Run `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "fix(atlas): set aside other hospitals' and other states' policies; siblings lend only within the state"`

### Task 9: Dry recheck against a copy of the atlas (acceptance)

**Files:**
- Create: `scripts/audit_rules_check.py`. It is read-only:
  - It copies `var/waive.db` to a temporary file with SQLite's backup API (`sqlite3.connect(src).backup(dst)`).
  - It points `WAIVE_DATABASE_URL` at the copy and runs `waive atlas recheck` there.
  - It prints the published sheets whose fields the new rules reject or would hold.

**Interfaces:**
- Consumes: Tasks 1–8.

- [ ] **Step 1: Write the script.**
  - Signature: `main(argv: list[str]) -> int`. The source defaults to `var/waive.db`; the `--db` option changes it.
  - Output: one line per affected published sheet, `ST CCN path: reason`, with `held` added when the sheet would be held.
  - It opens the source read-only (`?mode=ro`) and never writes to it.
- [ ] **Step 2: Run it once the rebuild runners have finished.** They have finished when `pgrep -f eve_rebuild_batch` prints nothing. Run `uv run python scripts/audit_rules_check.py > var/audit-rules-check.txt`.
  - Expected: at least these published CCNs appear:
    - Range: 070011, 070017, 070021, 070035, 171319.
    - Basis: 210003, 210043, 210049, 210058.
    - Residency: 210015, 210018, 210024, 210034, 210062.
    - Tables: 210018, 210062, 210032, 230047, 230053, 230092, 230302, 171350, 110069, 110129, 260102, 160104, 330307, 361309, 220116, 220046.
    - Owner: 161348, 141316, 110129, 330197, 450042.
  - Read every other listed sheet by hand against its quote.
  - If the rules hit a correct sheet, that is a defect in the task that owns the rule. Fix it there before the real recheck.
- [ ] **Step 3: Commit.** `git commit -m "chore(scripts): dry recheck of the audit rules on a copy of the atlas"`
