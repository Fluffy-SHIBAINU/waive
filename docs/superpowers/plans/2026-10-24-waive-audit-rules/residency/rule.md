# residency

## why it passed
Nothing in the verifier compares a residency value with its quote. In verify_sheet (src/waive/atlas/verify.py, around lines 1168-1250) the field `eligibility.residency` goes through only the generic checks: quote_found (is the quote verbatim) and looks_serialised. It then reaches the final `else: report.accepted`. value_in_quote (verify.py:951) handles only bool, number and list[DiscountTier]. For any other list, such as ["MD","VA","DC"], it reaches the closing `return True`. I confirmed it returns True for ["MD","VA","DC"] against "no states here at all". Residency is also not in LIST_SUPPORT (verify.py:527), so unsupported_list_reason and prune_lists never see it. The only residency code is the structurer's caster, structure._states (structure.py:281). It reads the model's value text, not the quote, and STATE_NAMES finds "virginia" as a whole word inside "northern Virginia". I confirmed _states(['Maryland','northern Virginia','District of Columbia']) returns ['MD','VA','DC']. So for 210018, 210024 and 210062 the regional qualifier was dropped at cast time, and nothing downstream reads the quote. For 210015 and 210034 the stored quote is the second and third sentences of §3.1.1 (kinds of proof, then apply for Medical Assistance) and names no state. It was still accepted because no check asks whether the quote names the states. Running today's verify_sheet on the latest published version of all five MedStar sheets, read-only from the DB with their stored source text, gives residency accepted and rejected=[] for each. This matters because src/waive/rules/eligibility.py:91-99 returns NOT_ELIGIBLE for any household state not in the list. A Richmond or Norfolk resident passes the residency step under a bare "VA".

## rule
Field: eligibility.residency only. Condition: every stored state code must be named in the cited quote as a whole state, otherwise reject the field. A state counts as named when:
(a) its full name appears as a whole word (states.STATE_NAME, longest first, so "west virginia" and "north carolina" stay one state each), with
- no region word directly before it: north/south(-east/-west)(-ern), east(ern), west(ern), central, upstate, downstate, upper, lower, coastal, mid, greater, rural, metro(politan), followed by a space or hyphen; and
- no place word directly after it: county, counties, city, beach (so not "Washington County", "Kansas City", "Virginia Beach", "certain Pennsylvania counties", "New York City");
(b) or its postal code appears as an upper-case token in mixed-case text, using structure._codes_in logic, so "the State of NJ" counts and an all-caps "IN" does not;
(c) or the quote says "D.C.";
(d) or the quote says "North and/or South Carolina|Dakota", which names both.
A state mentioned only with a regional qualifier ("northern Virginia", "Northeastern Montana") is therefore not named. One unqualified mention anywhere in the quote is enough.

Where to call it: a new branch in verify_sheet, `elif path == "eligibility.residency" and (reason := residency_problem(cited.value, cited.quote or "")): report.rejected.append((path, reason))`. It runs after trim_quotes and prune_lists in both _structure_and_verify and withdraw_rejected, so a recheck withdraws the bad fields from published sheets with no model call.

Outcome: reject the field, which drops it. Residency is not an income rule, so the sheet is not held and stays published. Do not add the rule to LIST_SUPPORT or prune_lists. Pruning would publish [MD, DC], and eligibility.py would then tell northern-Virginia residents NOT_ELIGIBLE, a new wrong answer.

Implementation note: verify.py cannot import structure.py, because structure already imports verify. Move _STATE_CODES, WORD_CODES, _CODE_LIST and _codes_in into states.py and import them from there in both modules. Optional and separate: give structure._states the same region and place test, so the caster stops turning "northern Virginia" into VA in the first place.

## challenge (needs_change)
On the 12 published residency fields, the proposed rule does what it claims, and so does my revision. Both fire on exactly 5: 210015, 210018, 210024, 210034 and the unflagged 210062. I read each one, and each is a correct catch. Both pass the other 7, and I found no false positive among them. I confirmed that residency is not in CRITICAL_PATHS and not an income rule, so rejecting it drops only the field and the sheet stays published.

It still needs a change, because two rebuilds are running and will feed it new quotes. I cast every residency field in every sheet version in the DB (510 rows, 362 distinct pairs, mostly drafts imported from Eve) with structure._states and ran the rule on them. It got one pattern wrong: the \b boundary misses the footnote-glued "Wisconsin1" in fap-d9c5de9c9fa6dd89. That would reject correct residency on 11 sheets, 9 of them Wisconsin hospitals losing their own state. It also passed about 20 drafts with the exact flagged defect:
- county- or city-qualified names
- codes with qualifiers (the code path has no region or place guard)
- "Middle Tennessee"
- "Washington (NY)" or "Washington, D.C." turned into WA
- facility-scope sentences

The revision fixes all of these:
- 58/58 of my tests pass, including all 24 of the engineer's.
- The published result is unchanged (the same 5 hits).
- In the DB simulation, 23 new hits, every one of which I checked is a part of a state or not a residency rule, and the Wisconsin1 false positive is gone.
- Across all 4,505 source documents, every residency sentence where the new guards drop a mention is a genuine part of a state or a place: Kansas City MO, Cecil County MD, "Hudson County, NJ", Clark County WA, City of New York, Southern NH, Louisiana Region 5, "certain counties of Pennsylvania" and the like.
- Phoebe's "regardless of which county in Georgia" still passes, because the same sentence names Georgia unqualified.

Two published sheets the audit never read are wrong in a direction no quote-naming rule can see. 140125 Gateway and 240214 Maple Grove store IL and MN, taken from the state uninsured-discount sentence, while their charity care has no residency rule at all. So eligibility.py tells out-of-state patients NOT_ELIGIBLE. The obvious mechanical test, a sentence with both uninsured and discount, also fires on genuine whole-policy rules (New York ×8, Northwell, Rhode Island, Vermont), so these need a person.

Also for a person: on the NJ sheets (310015, 310075, 310041), the value NJ is right per the document, but the document says emergency care is an exception. The schema cannot express that, so out-of-state emergency patients get NOT_ELIGIBLE. The RWJBarnabas quote names NJ only as "non-New Jersey residents". A better quote in the same document is "Patients may be eligible for Charity Care if they are New Jersey residents who".

Scratch files, all read-only work, under /private/tmp/claude-501/-Users-grey-Desktop-dev-eventhorizon/b529e6d4-a357-482a-ba81-d6830b9a174d/scratchpad/waive/audit-rest/scratch-residency-challenge/:
- revised_rule.py, test_revised.py
- sim2.py and sim2.out (DB simulation)
- doc_scan2.py and doc_scan2.out (document scan)
- scope_probe.py, scope_docs.py (the unsafe uninsured-discount probe)
- allres.json (residency rows from the DB)

No repo file or DB was written.

## revised rule
Field: eligibility.residency only. Reject the field, don't prune it: the sheet is not held, the branch sits in verify_sheet before the final else, and withdraw_rejected applies it with no model call. All of that stays as the engineer proposed. Condition: every stored state code must be named in the quote as a whole state.

(a) Names. The full name appears in normalize(quote) with LETTER-ONLY boundaries, (?<![a-z])(STATE_NAME)(?![a-z]), so "Wisconsin1" still counts. Longest name first. The mention does not count when any of these holds:
- REGION BEFORE: a region word directly before it, then a space or hyphen. The words: (north|south)(east|west)?(ern)?, east(ern)?, west(ern)?, central, middle, upstate, downstate, upper, lower, coastal, mid, greater, rural, metro(politan).
- PLACE BEFORE: (county|counties|city|parish(es)|borough|township) followed by "," or by "[located] in|of", then optionally "the state of". Also area(s) followed by "[located] in|of". This covers "Thomas County, Georgia", "Kansas City, Missouri", "Oxford County in Maine", "county in the state of Washington" and "service areas in Delaware".
- PLACE AFTER: county, counties, city, beach, parish(es), region, metro(politan), panhandle, campus, clinic(s), hospital(s) or facility/facilities directly after; or ", D.C." / " DC" after it ("Washington, D.C." is not WA); or a parenthesised two-letter code that is a different state ("Washington (NY)").
- "North and/or/& South Carolina|Dakota" names both. "D.C." with dots names DC.

(b) Codes. Only in mixed-case text, using the WORD_CODES and _CODE_LIST logic from structure._codes_in, but iterated by position. A code does not count when the same REGION BEFORE, PLACE BEFORE or PLACE AFTER test fires on the lower-cased text ("Cecil County, MD", "York County, SC", "Southern NH Health", "the following PA counties"). It also does not count when it sits in parentheses and the quote mentions count(y|ies) ("Rensselaer (NY) ... counties").

One unqualified mention anywhere in the quote is enough. "non-New Jersey" still counts as naming NJ, so the two correct RWJBarnabas sheets (310075, 310041) pass.

(c) Optional, separate reason: reject when the quote says "<State> [residents] and|or non-<State>" for a stored state, because residents of other states qualify too. This catches the ECU "North Carolina and non-North Carolina residents" and Novant drafts, where [NC] would wrongly exclude out-of-state patients. It fires on no published field.

Implementation: move _STATE_CODES, WORD_CODES and _CODE_LIST into states.py, as the engineer noted. verify.py cannot import structure.py.

Recommended, not part of the rule: give structure._states the same guards. It turns "Washington County", "Washington (NY)", "Washington, D.C.", "Kansas City" and "Virginia Beach" into the wrong state, and the verifier only catches that while the quote carries the qualifier.

Not mechanically fixable, so flag for a person or the structurer prompt: residency taken from a state uninsured-discount sentence (140125 Gateway, 240214 Maple Grove). I tested the obvious check, "the enclosing sentence has both uninsured and discount". It also fires on real policy-wide rules, such as NY's "uninsured and underinsured patients who reside in New York State ... discount" (8 docs), Northwell, South County RI and Vermont, so it is unsafe.

Candidate code (tested): /private/tmp/claude-501/-Users-grey-Desktop-dev-eventhorizon/b529e6d4-a357-482a-ba81-d6830b9a174d/scratchpad/waive/audit-rest/scratch-residency-challenge/revised_rule.py. Core of it:
_REGION_WORD = r"(?:north|south)(?:east|west)?(?:ern)?|east(?:ern)?|west(?:ern)?|central|middle|upstate|downstate|upper|lower|coastal|mid|greater|rural|metro(?:politan)?"
_NAMES = re.compile(rf"(?<![a-z])({STATE_NAME})(?![a-z])")
_REGION_BEFORE = re.compile(rf"(?<![a-z])(?:{_REGION_WORD})[- ]+$")
_PLACE_BEFORE = re.compile(r"(?<![a-z])(?:(?:count(?:y|ies)|city|parish(?:es)?|borough|township)(?:\s*,|\s+(?:located\s+)?(?:in|of))|areas?\s+(?:located\s+)?(?:in|of))\s+(?:the\s+state\s+of\s+)?$")
_PLACE_AFTER = re.compile(r"^(?:[- ]+(?:count(?:y|ies)|city|beach|parish(?:es)?|region|metro(?:politan)?|panhandle|campus|clinics?|hospitals?|facilit(?:y|ies))(?![a-z])|\s*,?\s*d\.?\s?c(?![a-z]))")
_PAREN_CODE = re.compile(r"^\s*\(([a-z]{2})\)")  # skip the name when the code differs from its own
_BOTH = re.compile(rf"(?<![a-z])({STATE_NAME})(?: residents?)? (?:and|or) non-? ?\1(?![a-z])")

## candidate code
```python
import re
from waive.atlas.states import CODE_OF_NAME, STATE_NAME
from waive.atlas.structure import _codes_in   # move to states.py in the real change (circular import)
from waive.atlas.verify import normalize

RESIDENCY_REASON = "quote does not name every listed state as a whole state"
_REGION_WORD = (
    r"(?:north|south)(?:east|west)?(?:ern)?|east(?:ern)?|west(?:ern)?|central|upstate|downstate"
    r"|upper|lower|coastal|mid|greater|rural|metro(?:politan)?"
)
_STATE_NAMES = re.compile(rf"\b({STATE_NAME})\b")  # longest first: "west virginia" is one state
_REGION_BEFORE = re.compile(rf"\b(?:{_REGION_WORD})[- ]+$")
_PLACE_AFTER = re.compile(r"^[- ]+(?:county|counties|city|beach)\b")
_PAIRED = re.compile(r"\bnorth (?:and|or|&) south (carolina|dakota)\b")
_DC = re.compile(r"\bd\. ?c\.")


def whole_states_named(quote: str) -> set[str]:
    text = normalize(quote)
    found: set[str] = set()
    for match in _STATE_NAMES.finditer(text):
        if _REGION_BEFORE.search(text, 0, match.start()) or _PLACE_AFTER.match(text[match.end():]):
            continue
        found.add(CODE_OF_NAME[match.group(1)])
    for match in _PAIRED.finditer(text):
        found |= {CODE_OF_NAME[f"north {match.group(1)}"], CODE_OF_NAME[f"south {match.group(1)}"]}
    if _DC.search(text):
        found.add("DC")
    raw = " ".join(quote.split())
    if raw != raw.upper():
        found.update(_codes_in(raw))
    return found


def residency_problem(value: list[str], quote: str) -> str | None:
    missing = {code.upper() for code in value} - whole_states_named(quote)
    return f"{RESIDENCY_REASON}: {', '.join(sorted(missing))}" if missing else None

# in verify_sheet, before the final else:
#     elif path == "eligibility.residency" and (
#         reason := residency_problem(cited.value, cited.quote or "")
#     ):
#         report.rejected.append((path, reason))
```

## tests
- reject=True value='["MD","VA","DC"]' quote='Proof of residency documentation would include gas and electric bills, pay stubs, bank statements, rent statements, etc. Patient must first apply for Medical Assistance, Medical Assistance Emergency Services, and other coverage program(s) eligibility.'
- reject=True value='["MD","VA","DC"]' quote='All patients must provide proof of residency within Maryland, northern Virginia, or within the District of Columbia.'
- reject=True value='["MD","DC","VA"]' quote='for patients who reside within Maryland, the District of \nColumbia, or northern Virginia.'
- reject=False value='["MD","DC"]' quote='All patients must provide proof of residency within Maryland, northern Virginia, or within the District of Columbia.'
- reject=True value='["ND","MT"]' quote='residing in North Dakota or Northeastern Montana and have received care at a Trinity Health facility'
- reject=False value='["NC","SC","GA","AL"]' quote='Uninsured patients who reside in North Carolina, South Carolina, Georgia, or Alabama'
- reject=False value='["WV"]' quote='Patients who reside in West Virginia are eligible.'
- reject=True value='["WV"]' quote='Patients who reside in southern West Virginia are eligible.'
- reject=True value='["NC"]' quote='residents of western North Carolina'
- reject=False value='["NC","SC"]' quote='residents of North and South Carolina'
- reject=True value='["NY"]' quote='residents of Western New York'
- reject=False value='["NY"]' quote='residents of New York State'
- reject=False value='["NJ"]' quote='Must be a resident of the State of NJ or the crime had to occur in this State.'
- reject=False value='["NJ"]' quote='Residency Criteria Charity Care may be available to non-New Jersey residents, requiring immediate medical attention for an emergency medical condition.'
- reject=True value='["WA"]' quote='Patients who live in Washington County are eligible for assistance.'
- reject=True value='["VA"]' quote='residents of the City of Virginia Beach'
- reject=True value='["TX"]' quote='residents of Central Texas counties'
- reject=True value='["IN"]' quote='Patients must live IN the service area.'
- reject=False value='["DC"]' quote='residents of Washington, D.C. and its suburbs'
- reject=False value='["OR","WA"]' quote='Financial assistance is offered to eligible residents of Oregon or Washington.'
- reject=False value='["IL"]' quote='Illinois Resident means a Patient who lives in Illinois and intends to continue living in Illinois indefinitely.'
- reject=False value='["MN"]' quote='who are residents of Minnesota'

## scan hits
- 210015 eligibility.residency correct=True: Flagged. The quote (§3.1.1, sentences 2 and 3) names no state, so MD, VA and DC are all missing. The source fap-b95c0699bae1032f limits Virginia to northern Virginia in every mention.
- 210018 eligibility.residency correct=True: Flagged. The quote says "Maryland, northern Virginia, or within the District of Columbia", so VA appears only with a regional qualifier. MD and DC are named.
- 210024 eligibility.residency correct=True: Flagged. The Scope quote says "Maryland, the District of Columbia, or northern Virginia", so VA appears only with a regional qualifier.
- 210034 eligibility.residency correct=True: Flagged. The quote is the same proof-of-residency and Medical Assistance sentences as 210015 and names no state.
- 210062 eligibility.residency correct=True: Not in flags.json, but the same error: MedStar Southern Maryland stores [MD, VA, DC] from "proof of residency within Maryland, northern Virginia, or within the District of Columbia." I grepped the source: all five Virginia mentions in fap-b95c0699bae1032f say "northern Virginia". The audit readers of group-06 flagged only its discount_tiers.

## challenge false positives
- 340096, 340119, 520038, 520059, 520102, 520113, 520139, 520189, 520198, 520206, 520207 eligibility.residency: These are draft versions on the rebuild path, not published. All 11 quote fap-d9c5de9c9fa6dd89: "Uninsured patients who reside in Illinois, Wisconsin1, North Carolina, South Carolina, Georgia or Alabama." The PDF footnote mark is glued to the name, so \bwisconsin\b does not match. When the caster reads [IL, WI, NC, SC, GA, AL] from the model's value text, the proposed rule rejects WI and drops the whole residency field, although every state is named. Nine of these are Wisconsin hospitals, so the rule would reject their own home state. The fix is letter-only boundaries, (?<![a-z])name(?![a-z]), in place of \b. The full doc scan shows this is the only place that boundary change alters a result.

## challenge misses
- 140125 eligibility.residency: Published, unaudited, value [IL]. This is a scope variant of the same field. The quote "Care discounted at to 135% of hospital cost for uninsured Illinois residents..." is one bullet under Partial Charity Care, and it describes the state uninsured-discount law. In fap-8d03992ad69d5282, Full Charity Care (200% FPL or below) has no residency condition, and the policy even counts out-of-state Medicaid non-covered services as financial assistance. So eligibility.py:91-99 wrongly tells a Missouri resident NOT_ELIGIBLE at Gateway Regional, which is in the St. Louis metro. The rule passes it because IL is named as a whole state.
- 240214 eligibility.residency: Published, unaudited, value [MN]. Same scope variant. The quote "who are residents of Minnesota" comes from "PROCEDURE I. NMH Uninsured Discount: NMH offers discounts to patients who are residents of Minnesota and who are uninsured". Section II, NMH Charity Care (free care at 200% FPL or below, 300% FPL band), has no residency requirement anywhere in fap-1be2f2e7d1491110. Out-of-state patients are wrongly told NOT_ELIGIBLE.
- 141302 eligibility.residency: Published, plausible, needs a person. The quote is the statutory definition of "Illinois Resident" used for the IL Hospital Uninsured Patient Discount ("does not apply to patients who reside outside of IL"). It is not a restriction on MMC Straight Charity, which offers 100% at 150% FPG or below. The application does ask for "Residency Verification documents" for the MMC Charity program as well, so the IL value may be right, but the quote does not show that.
- 260048, 260102, 470012, 471306, 380021, 301303, 110038, 110121, 281352, 300020, 440151, 441300, 340015, 030012, 110236, 160028, 190060, 080001, 521348, 501332 eligibility.residency: Draft versions on the rebuild path, not published. Each is the exact flagged defect: a state named only as part of the state, or not as a residency rule at all. Cast by structure._states, each passes the proposed rule. Examples: "Kansas City, Missouri, or Jackson County, Missouri" stored as [MO]; "Rensselaer (NY), Washington (NY) and Berkshire (MA)", where the caster turns the county "Washington" into WA; "Sullivan or Cheshire Counties in New Hampshire"; "bordering county in the state of Washington"; "Oxford County in Maine"; "Thomas County, Georgia"; "Richardson County, Nebraska"; "Southern NH Health's ... service areas" (an NH code inside an organisation name); "Counties of Middle Tennessee" ("middle" is missing from the region words); "York County, SC" (the code path has no place guard); CommonSpirit's "occurs within a California Hospital Facility" stored as CA for AZ, GA and IA hospitals; "Louisiana Region 5"; "primary service areas in Delaware..."; "Vernon and Crawford Counties, Wisconsin"; "the Washington campus, and Idaho Clinics". The proposed rule already counts "Washington, D.C." as naming WA, so a WA cast from it would pass too.

## one-offs

## notes
Scan scope: the rule applies only to eligibility.residency. That is 12 of the 3,184 published fields across 605 sheets. It fires on 5: the 4 flagged MedStar sheets plus MedStar Southern Maryland 210062, the same error the audit missed. It passes the other 7:
- 140125 and 141302 (IL, "Illinois residents")
- 310015 (NJ, "Proof of New Jersey residence")
- 310075 and 310041 (NJ, "non-New Jersey residents")
- 240214 (MN, "residents of Minnesota")
- 111310 (Atrium Health, NC/SC/GA/AL, all four named)
None of those 7 was flagged, and their values match the quotes. No rule fires on any non-residency field, so there are 0 hits outside these 12.

The 310075 and 310041 quote only implies the NJ rule through an emergency exception for non-residents. The value NJ is right: the same document says patients may be eligible for Charity Care if they are New Jersey residents. The rule counts "non-New Jersey" as naming NJ on purpose, so these correct sheets pass. Someone may still want a stronger quote for them.

Further safety checks:
1. Every residency field in every sheet version in the DB (read-only): 14 distinct state-valued (value, quote) pairs. 6 fire: the 5 MedStar fields plus held sheet 350006 v2 (Trinity Health ND, [ND, MT] from "North Dakota or Northeastern Montana", also a correct catch). 8 pass.
2. Across all 4,587 stored source documents, every place the region or place test fires inside a residency sentence is a genuine part of a state or a place: northern Virginia, northeastern Montana, Northeast Ohio, West Alabama, Northwest Kansas, Southern Nevada, Central New York, South Texas, New York City, Kansas City, Washington County, and "certain Pennsylvania / South Carolina / Ohio counties". The rest sit in organisation names (Central Maine Healthcare, Baptist Health South Florida). The one residency sentence among them also names the state unqualified ("a Maine resident"), so it still passes.

Known limits:
- Written-out abbreviations ("Mass.", "Calif.") are not recognised and would reject. That errs safe, and no published quote uses them.
- A state-named town other than city/beach/county (e.g. "Indiana, PA") would over-count. That makes the rule permissive, not wrong.

Reject, don't prune. Dropping VA to leave [MD, DC] would make eligibility.py:91-99 return NOT_ELIGIBLE for northern-Virginia residents, whom the policy covers. Dropping the whole field avoids that. Residency is not an income rule, so the 5 sheets stay published without residency, and withdraw_rejected removes the fields at no model cost.

The schema stores only whole-state codes, so a person may want to add a "northern Virginia only" note on the MedStar sheets later. That needs a person, but no flagged sheet is left uncaught, so one_offs is empty.

The 210018 and 210062 discount_tiers 80-vs-75 column error is a different pattern and outside this rule.

Scratch files, all under /private/tmp/claude-501/-Users-grey-Desktop-dev-eventhorizon/b529e6d4-a357-482a-ba81-d6830b9a174d/scratchpad/waive/audit-rest/scratch-residency/:
- residency_rule.py (predicate, 24 tests all passing, published scan)
- db_scan.py (all sheet versions)
- doc_scan.py (qualifier firings in source documents)
- current_verify.py (shows today's verify_sheet accepts all 5)

No repo file or DB was written.
