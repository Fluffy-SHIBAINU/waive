# owner-scope

## why it passed
No check asks whether a document covers the hospital. Every check looks at places or at quotes.

1. The UnityPoint files came in with Eve's draft. Waive's own scout would never have fetched them for clarkehosp.org. fetch.host_allowed_for (fetch.py:73) and scout.policy_links (scout.py:190) keep a hospital to its registered domain plus the asset hosts, and files.unitypoint.org is neither. The import and rebuild path stores and reads whatever Eve cited.

2. The place check passes on another document. pipeline._unplaced (pipeline.py:647) calls discover.place_unconfirmed(hospital, web_texts), which joins every web document into one blob (discover.py:294 `blob = " ".join(texts)`). Clarke's own cost-estimate page names "Osceola, IA" and "Clarke County Hospital", so the two UPH files pass with it. Run on the UPH policy alone, place_unconfirmed returns "the documents name neither Osceola nor the hospital".

3. The other-state check sees nothing. structure.document_states(title, url) returns an empty set for both UPH files: the title is empty and the URL is an opaque /api/public/content/<id>. pipeline._drop_other_state_documents (pipeline.py:176) therefore keeps them as documents that name no state. The policy text names Iowa anyway.

4. The set-aside check doesn't cover it. pipeline._documents_set_aside / _never_the_hospitals (pipeline.py:239-273) set aside only dental-program documents, other Kaiser regions and translations.

5. Every quote verifies. verify.verify_sheet (verify.py:1166) holds each field to its own quote: quote_found, value_in_quote, and the free-care, tier, asset and insured checks. "0-200% of FPIG : 100% discount off AGB" is verbatim in 1.BR.34. bands_under_another_state (verify.py:343) reacts only to "for <State> facilities" headings. So Schedule D, which lists 21 UPH hospitals and not Clarke, is never read as a scope.

The scan found 110129 (St. Francis Hospital-Emory Healthcare, Columbus GA, website mystfrancis.com), which passed the same way. It cites Emory Healthcare's policy, whose list "Emory Healthcare Hospitals include:" gives 11 hospitals and not St. Francis. The hospital's own page names Columbus. It found 330197 (Canton-Potsdam) as well: it cites "Financial Assistance Program for Rochester General Hospital...", while RRH's own page also links a separate "Canton-Potsdam Hospital Financial Assistance Policy" that was never stored.

## rule
This is a per-document owner check. It does not test quotes. A document it flags is set aside, so every field quoted from it is rejected. When the income rule goes too (free_care_max_fpl / discount_tiers), the sheet is held, the same way as with DENTAL_REASON and OTHER_REGION_REASON.

Clause A (the 161348 pattern), FOREIGN_OWNER_REASON. A HOSPITAL_WEB source of hospital H with a website_domain is set aside when all of these hold:
(1) host_of(source.url) != host_of(H.website_domain);
(2) not fetch.is_asset_host(source.url). Together, (1) and (2) mean the document sits outside fetch.host_allowed_for(H.website_domain).
(3) Its text names neither H's town (CMS city, as whole words, after discover._plain) nor H itself (discover._names_hospital: two evidence tokens, including every distinctive word when H's name has fewer than two).
(4) No stored HOSPITAL_WEB document on H's own registered domain contains a link (https?://…) whose host_of equals the source's host_of.

Clause B (the 330197 pattern), SIBLING_POLICY_REASON. A source is set aside when all of these hold:
(1) Some stored page of H links, under a label matching /financial assistance|charity|financial aid|policy|fap/, a URL whose label names H (its town or _names_hospital).
(2) The same pages link this source under labels none of which names H, and at least one of which names another registry hospital of H's state (_names_hospital on the label).
(3) The source's text never names H or its town.

Where to call it:
(a) pipeline._documents_set_aside: after the per-document _never_the_hospitals pass, add foreign_documents(hospital, rest) and sibling_policies(hospital, rest, state_registry). This covers recheck_sheets, withdraw_rejected and confirm_domain. Clause B needs the state's registry rows, so pass the session or the list. verify_sheet itself is unchanged, because its existing `reason := set_aside.get(source_id)` branch rejects these fields.
(b) The build path: in pipeline._documents_for_structurer, add a _drop_foreign_documents step after _drop_other_state_documents. It detaches the documents, files a `foreign_documents` review item with the ids, URLs and reason, and keeps them from the structurer, as _drop_other_state_documents does. An admin can re-link a document if the hospital really does use that system's policy.

Outcome: reject the fields (with the reason above) and hold the sheet if its income rule is rejected.

## challenge (unsafe)
My rerun of the engineer's predicate over published-fields.jsonl matches their report: 110129 and 161348 through clause A, 330197 through clause B, and no correct sheet among those 605. That claim only holds for that snapshot and for published sheets.

(1) The database now has 644 published sheets. CHI Oakes (351315), a correct CommonSpirit G-003 sheet published after the snapshot, loses all 7 fields and its income rule under clause A. Its own page says the CommonSpirit policy applies, but the stored text has no commonspirit.org link.

(2) Clause A is also meant to run in the build path and in recheck. Across the database it fires on 154 documents at about 110 hospitals, almost all of them a system's own documents on CDNs or parent domains: Corewell (contentstack.io), Piedmont, Sentara, Franciscan Alliance, UNC Health, CommonSpirit on brand domains, the Advocate enterprise FAP for Aurora, BJC, HCA Virginia and Concord via archive.org. Under it, 70 draft or held sheets get new field rejections and 35 lose their income rule. 'Off the hospital's domain, unlinked and unnamed' does not show another owner.

The fix is to require positive scope evidence: the document carries its own covered-hospital list that names another same-state registry hospital, and never names H, its town or its website name. That keeps both true catches (Emory's 'Hospitals include:' and UPH's Schedule D) and drops every system false positive above.

The same check on the 1.BR.34 'attached Schedule D' scope also works on the hospital's own domain. It catches an unflagged income-rule miss: Carle Hoopeston (141316) quotes Carle West Region's policy, whose Schedule D lists only Methodist, Pekin and Proctor.

A second unflagged income-rule miss of the same kind is 450042 (Ascension Providence, Waco TX). It publishes 250% free care from a Maryland page, because all its stored documents are other states' and recheck never applies the other-state check. Clause C (states named by the URL or the title, with the site name dropped) catches it and fires on no correct sheet.

Remaining misses are on the hospital's registered domain or come from a wrong registry website: 330267 (NYP Hudson Valley published from Northwell), 451322 (a Kentucky CHI summary on a Texas sheet), 100226 and 100131 (the UCF Lake Nona page), and the engineer's list. I tested four further mechanical forms, and each also fires on correct sheets:
- covered-hospital list on the own domain (Southside's rename to South Shore University Hospital);
- self-declared owner in a policy title or 'applies to' line (published 141346 Fayette under Sarah Bush Lincoln);
- title names another hospital (Carle Foundation Hospital, Mercy regional pages);
- other-state address in the text (central billing PO boxes in other states).
These stay one-offs.

A further root cause: _share_sibling_sources lends documents across states (siblings_on_domain has no state filter). That is how Dignity Sacramento's application reached Colorado and Kansas sheets.

Residual gaps of the revision:
- held sheet 450395 (BJC FAP on a CHI Texas hospital) and the generic CHS template forms (250094, 320063, 371326, 440033), which the original clause A caught at build time;
- a renamed hospital listed under its new name in an off-domain system list would be wrongly set aside. The town and website-name exemptions reduce that risk, and none occurs today.

Scripts are under /private/tmp/claude-501/-Users-grey-Desktop-dev-eventhorizon/b529e6d4-a357-482a-ba81-d6830b9a174d/scratchpad/waive/audit-rest/scratch-owner-challenge/: revised.py, tests_revised.py, sim.py, run_revised_jsonl.py, build_all_rev.py, all_hosp_A.txt and sim_eng_dh.txt. The database was opened read-only only, no repo file was edited, and no network calls were made.

## revised rule
Keep clause B as the engineer wrote it. Replace clause A with A2, and add clauses S and C. Each clause sets a document aside, which rejects every field quoted from it; if the income rule goes, the sheet is held.

A2, FOREIGN_OWNER_REASON. H is a hospital with a website_domain; home = host_of(www.<domain>), and label = the first DNS label of home. A HOSPITAL_WEB document is set aside when all of these hold:
(1) host_of(url) != home and not fetch.is_asset_host(url);
(2) no stored HOSPITAL_WEB page on home contains an absolute link with the same host_of;
(3) the text names neither H (discover._names_hospital) nor H's town (whole words after _plain) nor H's website name (label found in the text after removing everything but [a-z0-9]; for example 'UNC Health Appalachian' matches unchealthappalachian);
(4) the text carries its own covered-hospital list: ROSTER = hospitals\s+include\s*: | facilities\s+include\s*: | \(listed\s+below\) | applies\s+to\s+the\s+following\s+(hospitals|facilities|entities) | the\s+following\b.{0,120}?\b(hospitals|facilities)\b.{0,60}?\bare\s+covered\b | included\s+in\s+(the\s+)?attached\s+schedule\s+[a-z]\b, and no appl(y|ies)\s+to\s*:?\s*all\b;
(5) that list names at least one other registry hospital of H's state, by _names_hospital and that hospital's town.
Then every other document on the same host that meets (1) to (3) is set aside with it. This covers UnityPoint's application next to 1.BR.34.

S, the Schedule clause, on any host including home. A document is set aside when it contains included\s+in\s+(the\s+)?attached\s+schedule\s+[a-z]\b, has no 'applies to all', either sits on home or meets (2), never names H or its town, and names at least one other same-state registry hospital by name and town. This catches the 1.BR.34 template on the hospital's own domain (Carle Hoopeston).

C, OTHER_STATE_REASON. A HOSPITAL_WEB document is set aside when the states named in its URL path plus the first segment of its title name at least one state, none of them H's. The title is split on ' | ', ' – ', ' - ', which drops the site name, so 'North Mississippi Health Services' does not count; states_named is used, minus the states in H's name and city. C applies in _documents_set_aside even when every stored document is another state's. That extends the Kaiser-only branch of the 7.9 'nothing to prefer' fallback to every host, so it needs the owner's sign-off. Also apply C in _share_sibling_sources, so no sibling lends another state's document.

Do not keep the engineer's case 'unlinked, unnamed system policy with no own page, therefore reject' (070011). Without a covered-hospital list the document stays. CHI Oakes and the Corewell, Piedmont, Sentara and Franciscan sheets are why.

Where to call it: pipeline._documents_set_aside, after _never_the_hospitals (A2+S and B need the state's registry rows; C needs none). This reaches recheck_sheets, withdraw_rejected, confirm_domain and the build's verify. In _documents_for_structurer, detach the same documents with a foreign_documents or other_state_documents review item. verify_sheet itself is unchanged.

Results:
- published-fields.jsonl (605 sheets): 20 fields on 5 sheets. 110129 (6, Emory), 161348 (6, UPH), 141316 (3, Carle West), 330197 (3, via B; free care was already rejected) and 450042 (2, MD and FL pages).
- The 644 sheets published now: the same 5. Drafts and held: only 331315 (B, Rochester General's policy) and 051301 (C, Adventist '(HI)' policy), both correct.
- Build time over 1,970 hospitals: A2+S set aside 5 documents at 3 hospitals, against 154 for the original clause A; C sets aside 15 documents, all other states' versions.
- No sheet in any state, the nine scouted ones included, is wrongly hit. Decision B wording is untouched.
- Tests: 37 cases in scratch-owner-challenge/tests_revised.py, all passing.

## candidate code
```python
# scratch-owner-scope/candidate_final.py (tested with uv run --frozen against var/waive.db read-only)
import re
from collections.abc import Iterable, Sequence
from urllib.parse import unquote, urljoin
from waive.atlas.discover import _names_hospital, _plain, host_of
from waive.atlas.fetch import is_asset_host
from waive.atlas.schema import HospitalRef, SourceDoc, SourceKind

FOREIGN_OWNER_REASON = ("quote is from another organisation's document that never names the hospital and that the "
                        "hospital's own website does not link to")
SIBLING_POLICY_REASON = "quote is from another hospital's policy; the hospital's site links its own"
_LINK = re.compile(r"https?://[^\s)\]\"'<>|]+", re.IGNORECASE)
_MARKDOWN_LINK = re.compile(r"\[([^\]]{3,200})\]\(\s*([^)\s]+)")
_POLICY_LABEL = re.compile(r"financial assistance|charity|financial aid|\bpolicy\b|\bfap\b", re.I)

def names_hospital_or_town(hospital: HospitalRef, text: str) -> bool:
    plain = " ".join(_plain(text).split())
    town = " ".join(_plain(hospital.city).split())
    if town and re.search(rf"(?<![a-z0-9]){re.escape(town)}(?![a-z0-9])", plain):
        return True
    return _names_hospital(hospital, plain)

def _web(sources):
    return [(s, t) for s, t in sources if s.kind is SourceKind.HOSPITAL_WEB and s.url]

def foreign_documents(hospital: HospitalRef, sources: Iterable[tuple[SourceDoc, str]]) -> set[str]:
    if not hospital.website_domain:
        return set()
    home = host_of(f"https://www.{hospital.website_domain}")
    docs = _web(sources)
    linked = {host_of(link) for source, text in docs if host_of(source.url) == home
              for link in _LINK.findall(text)}
    return {source.id for source, text in docs
            if host_of(source.url) != home and not is_asset_host(source.url)
            and host_of(source.url) not in linked and not names_hospital_or_town(hospital, text)}

def _same_url(url: str) -> str:
    return unquote(url).rstrip("/").lower().replace("http://", "https://").replace("://www.", "://")

def sibling_policies(hospital: HospitalRef, sources, state_registry: Sequence[HospitalRef]) -> set[str]:
    docs = _web(sources)
    labels: dict[str, list[str]] = {}
    for source, text in docs:
        for label, href in _MARKDOWN_LINK.findall(text):
            label = " ".join(label.split())
            if _POLICY_LABEL.search(label):
                labels.setdefault(_same_url(urljoin(source.url, href)), []).append(label)
    own = {u for u, found in labels.items() if any(names_hospital_or_town(hospital, l) for l in found)}
    if not own:
        return set()
    others = [h for h in state_registry if h.ccn != hospital.ccn]
    out = set()
    for source, text in docs:
        url = _same_url(source.url)
        found = labels.get(url, [])
        if url in own or not found or names_hospital_or_town(hospital, text):
            continue
        if any(_names_hospital(o, " ".join(_plain(l).split())) for o in others for l in found):
            out.add(source.id)
    return out

# call site, pipeline._documents_set_aside (after the _never_the_hospitals pass):
#   rest = [(s, t) for s, t in sources_with_text if s.id not in set_aside]
#   set_aside.update((sid, FOREIGN_OWNER_REASON) for sid in foreign_documents(hospital, rest))
#   set_aside.update((sid, SIBLING_POLICY_REASON) for sid in sibling_policies(hospital, rest, state_registry))
#   ... and the same two sets detached in _documents_for_structurer with a `foreign_documents` review item.
```

## tests
- reject=True value='161348 free_care_max_fpl=200, discount_tiers, asset_test, presumptive, documents_required' quote="[A] fap-ec276fb294e1a7f3 files.unitypoint.org UPH 1.BR.34: 'UPH Hospitals ... in Illinois and Iowa, and included in attached Schedule D' (no Clarke/Osceola); own clarkehosp.org page links only chart.myunitypoint.org"
- reject=True value='161348 apply.submit_methods' quote="[A] fap-8cb546853b4d5aee files.unitypoint.org UPH IA/IL application: 'UnityPoint Health Financial Assistance PO Box 809330 Chicago, IL'"
- reject=False value='161348 any field from own page' quote="[A] web-b931b400861284c2 clarkehosp.org own page 'Clarke County Hospital ... Osceola, IA 50213'"
- reject=False value='161348 fields from UPH policy' quote="[A] synthetic: Clarke's own page with '[FAP](https://files.unitypoint.org/x.pdf)'"
- reject=False value='161348 fields from UPH policy' quote="[A] synthetic: UPH policy text + ' Clarke County Hospital, Osceola'"
- reject=True value='110129 free_care_max_fpl=200 etc.' quote="[A] fap-0e1bb40253c56386 emoryhealthcare.org: 'Emory Healthcare Hospitals include: • Emory University Hospital ... • Houston Hospital - Perry' (no St. Francis/Columbus); mystfrancis.com page has no Emory link"
- reject=False value='070011 free_care_max_fpl etc.' quote="[A] fap-dc06d545589e6a88 hartfordhealthcare.org HHC policy ('entity as listed in Appendix D', no hospital names); charlottehungerford.org page links https://hartfordhealthcare.org/financial-assistance"
- reject=True value='070011 fields' quote='[A] same HHC policy with no own-domain page stored'
- reject=False value='171337 fields' quote='[A] fap-02f0838acdca19c6 healthcare.ascension.org Kansas FAP naming Wamego'
- reject=False value='171337 apply.documents_required' quote='[A] fap-da8116e972384e4d Ascension Kansas application (no Wamego); own page links healthcare.ascension.org'
- reject=False value='331312 apply.submit_methods' quote='[A] application-8ca1290907d3da45 nyuhs.widen.net (asset host) NYUHS form'
- reject=False value='171363 free_care_max_fpl' quote='[A] fap-64194b6aa1d4fa57 cmhcare.org own-domain policy that never names the hospital'
- reject=False value='161348 (no website)' quote='[A] UPH policy for a hospital with website_domain=None'
- reject=True value='330197 free_care_max_fpl=400, insured_patients_covered, decision_days=30' quote="[B] billing-e49ebb7a9a323126 'Financial Assistance Program for Rochester General Hospital and all Rochester Regional Health Employed Physicians'; RRH page links it as 'Rochester General Hospital Financial Assistance Policy' and also links 'Canton-Potsdam Hospital Financial Assistance Policy'"
- reject=False value='330197 documents_required, submit_methods' quote="[B] application-6bdaefe8ec940f91 'NYS Uniform Hospital Financial Assistance Application' linked as 'Download the Financial Assistance Application - English'"
- reject=False value='330125 fields' quote="[B] Rochester General's own policy cited by Rochester General (330125)"
- reject=False value='330197 fields' quote="[B] synthetic: RGH policy text + ' Canton-Potsdam Hospital'"
- reject=False value='330197 fields' quote='[B] synthetic: RRH page without the Canton-Potsdam policy link'
- reject=False value='390179 free_care_max_fpl=300' quote="[B] summary-26d8ffa01a616326 Penn Medicine summary naming 'The Chester County Hospital' (also linked as '#goto-LGH-version')"

## scan hits
- 161348 eligibility.free_care_max_fpl correct=True: A: UPH 1.BR.34 (files.unitypoint.org). Schedule D lists 21 UPH hospitals, not Clarke; the flagged sheet.
- 161348 eligibility.discount_tiers correct=True: A: same UPH policy, 65/45/25/5% tiers are UPH's
- 161348 eligibility.asset_test correct=True: A: same UPH policy
- 161348 programs.presumptive correct=True: A: same UPH policy
- 161348 apply.documents_required correct=True: A: same UPH policy
- 161348 apply.submit_methods correct=True: A: UPH IA/IL application (fap-8cb546853b4d5aee) sends Clarke patients to the UnityPoint PO Box in Chicago. The audit verdict says this form never names Clarke either.
- 110129 eligibility.free_care_max_fpl correct=True: A, unflagged (not audited): Emory Healthcare policy on emoryhealthcare.org lists its 11 hospitals, not St. Francis (Columbus). mystfrancis.com is 'Empowered by ScionHealth', describes 'our Financial Assistance Program' with its own application and phone 706.596.4412, and never links Emory.
- 110129 programs.presumptive correct=True: A: same Emory policy
- 110129 programs.state_programs correct=True: A: same Emory policy (Georgia ICTF)
- 110129 apply.documents_required correct=True: A: same Emory policy
- 110129 apply.submit_methods correct=True: A: 'through the patient's MyChart account' is Emory's MyChart, not St. Francis's
- 110129 apply.window_days_from_first_bill correct=True: A: same Emory policy (contacts.languages from it is rejected too; that field isn't in published-fields.jsonl)
- 330197 eligibility.free_care_max_fpl correct=True: B, unflagged: Rochester General Hospital's policy; RRH's page links a separate Canton-Potsdam Hospital policy that was never stored. The current verifier already rejects this field (later-band rule).
- 330197 eligibility.insured_patients_covered correct=True: B: same RGH policy. The value is probably identical in RRH's templated Canton-Potsdam policy, but the quote is another hospital's.
- 330197 apply.decision_days correct=True: B: same RGH policy

## challenge false positives
- 351315 eligibility.free_care_max_fpl, eligibility.insured_patients_covered, programs.presumptive, apply.documents_required, apply.submit_methods, apply.decision_days, contacts.phone: CHI Oakes Hospital (ND) is published (v2) and correct, but it went up after published-fields.jsonl was cut, so the 605-sheet check never saw it. Every field quotes CommonSpirit G-003 (commonspirit.org, fap-174bfaef7b79efbc) or CHI's ND/MN application (chilakewoodhealth.com, fap-e4142b1248b8adbd). Its own page (web-8eaa6ecdc46d9e20) says 'The CommonSpirit Health Financial Assistance Policy ... applies', but the stored text has no commonspirit.org link; it only links commonspirit.myfa.app. Clause A rejects all 7 fields, the income rule goes, and the sheet is held. The real verify_sheet over the 644 currently published sheets gives 4 hits: 110129, 161348, 330197 and this one.
- 230003,230020,230021,230035,230078,230089,230110,230130,230142,230151,230176,230269,230270,231323,231338,231339 apply.* (documents_required, submit_methods, etc.): These are draft Corewell Health sheets. They cite Corewell's own application on assets.contentstack.io (its CMS asset host), which names no hospital and is linked from no stored corewellhealth.org page. Clause A rejects those fields. The build path would also detach the document, so the structurer never sees it.
- 150015,150022,150057,150090,150109,150126,150162,150165,150193,151324 insured_patients_covered, contacts.*, documents_required (151324: the income rule too): Franciscan Health drafts. The documents sit on statements.franciscanalliance.org, the system's own statement site; Franciscan Alliance is Franciscan Health's legal name. Clause A sets them aside. 151324 loses free_care_max_fpl and discount_tiers and would be held.
- 030012,030036,160028,161309,241370,241377,280023,500011,501335,060012 free_care_max_fpl, insured_patients_covered, presumptive and others: CommonSpirit, CHI and Dignity hospitals on their own brand domains (yrmc.org, dignityhealth.org, chihealth.com, chistgabriels.com, chistfrancishealth.org, vmfh.org, stmarycorwin.org). They cite G-003 or mountain.commonspirit.org, which covers every CommonSpirit facility. Clause A rejects the income rule on each, as it does on 351315.
- 340010,340027,340041,340050,340060,341311,341323,520059,520097,520102,520139,520189,520198,520206,260032,260162,260180,260219,490093,490107,490118,110018,210028,090005,041321,300005,301306 various, including the income rule on 341323, the Aurora sheets, the BJC sheets, 490107/490118 and 210028: Each cites a legitimate system document: UNC Health's policy and application (unchealth.org); the Advocate Health enterprise FAP for Aurora (cdn.atriumhealth.org); BJC (bjc.org, saintlukeskc.org 3259250); Sentara (sitecorecontenthub.cloud); HCA Virginia (hcadam.com); Piedmont (merge-digital.com); MedStar (medstarhealth.org for medstarstmarys.org); Johns Hopkins (hpo-docs.jh.edu for Sibley); Baptist Health (optimizely.com); Concord Hospital's own pages through web.archive.org. Across the whole DB (1,970 hospitals with documents), clause A fires on 154 documents. 70 draft or held sheets get new field rejections (52 drafts, 18 held), and 35 lose their income rule. Only a few are true catches: the CHS template forms on 250094, 320063, 371326 and 440033, Banner on 061336, and Gouverneur through clause B.
- 390179 (uncited) fap-be423d642f6422b0: At build time clause B detaches Penn Medicine's general 'Financial assistance state resources' page from Chester County Hospital. Some page labels the URL with Lancaster General's name. No field is lost, but the structurer stops seeing a Penn-wide page.

## challenge misses
- 450042 eligibility.free_care_max_fpl (250), apply.documents_required: Ascension Providence is in Waco, TX. Free care is quoted from 'Financial assistance Ascension Saint Agnes in Maryland' (fap-bfa27011b572dca1) and documents_required from 'Ascension St. Vincent's in FL' (fap-03526e8a56a74887). All 8 stored documents are other states' (FL, OK, MD, IL). _drop_other_state_documents therefore keeps them under its 'nothing to prefer' fallback, and _documents_set_aside, which recheck uses, has no other-state check at all. The documents are on the registered domain ascension.org, so clause A never looks. The MD page also names 'Ascension Providence' in its location menu, so a names-the-hospital test passes it. The value may match Ascension Texas, but the quote is Maryland's. Revised clause C catches it.
- 141316 eligibility.free_care_max_fpl, eligibility.discount_tiers, eligibility.insured_patients_covered: Carle Hoopeston (IL) quotes Carle Health West Region's 1.BR.34 (carle.org .../PeoriaFinancialAssistancePolicyEnglish-FINAL.pdf). This is the same UnityPoint template as 161348. Its SCOPE covers the hospitals 'included in attached Schedule D', and Schedule D lists only Methodist, Pekin and Proctor. Hoopeston is never named, and the tiers (65/45/25/5) are the West Region's. The policy is on the hospital's registered domain, so clause A cannot see it. The Carle Rural Health policy that probably covers it (fap-88cc9b03d3916937) is stored as garbled glyph IDs. Caught by the revised Schedule clause.
- 330267 all 7 fields (free_care_max_fpl, insured_patients_covered, documents_required, submit_methods, window_days, decision_days, eca_wait_days): Hudson Valley Hospital Center (1980 Crompond Rd, Cortlandt Manor) is NewYork-Presbyterian Hudson Valley Hospital. Its registry website is northwell.edu, and the whole sheet is Northwell's 300.20 and 2018 policies. Northwell's roster does not list it. The place check passed on the phrase 'Hudson Valley & Connecticut patients (formerly Nuvance)'. This is a wrong website. I found no safe rule: an on-domain roster rule also fires on Southside (330043), which Northwell lists as South Shore University Hospital. One-off: mark the website wrong and rebuild.
- 451322 eligibility.free_care_max_fpl: Grimes St. Joseph (Navasota, TX) quotes the CommonSpirit summary for Saint Joseph East (summary-5424c3b004a1933e: '150 N. Eagle Creek Drive, Lexington, KY 40509'). Every stored document is CHI Saint Joseph Health Kentucky's, and neither the URL ('chisj/soky') nor the title names a state. A text or address-based other-state rule is unsafe: it would hit the Carle summaries (Des Moines PO box), the HSHS application (Springfield, IL), Methodist Olive Branch (Memphis) and others. One-off: re-cite G-003 or hold the sheet.
- 100226 eligibility.free_care_max_fpl, eligibility.insured_patients_covered, apply.form_url: HCA Florida Orange Park quotes application-3f5881ad62e4f6fd, titled 'Financial Assistance Policy and Application | UCF Lake Nona Hospital', which says 'For UCF Lake Nona Hospital ...' (100131 Aventura's submit_methods uses the same page). A rule on 'the title names another registry hospital' is noisy: it hits Carle Foundation Hospital pages on 141309/141355, Mercy regional pages and NMHS. One-off: re-cite HCA Florida's policy (fap-9bb7eefce601684c, already on the sheet).
- 100296,101313,330096,310084,360230,361311,330141,330195,050488,060003,060015,171388,450162 as listed in the engineer's one-offs: I confirm all of these. I tried a 'self-declared owner' clause ('This policy applies to X', 'Applicability X', 'Financial Assistance Program for X' naming another registry hospital and never H). It catches 100296, 101313, 330096 and 330197, but it wrongly fires on published 141346 (Fayette County Hospital under the Sarah Bush Lincoln policy) and on 151324, 310011, 340071 and 341323, so I did not adopt it. Root cause for 060003, 060015 and 171388: _share_sibling_sources lent Dignity Sacramento's CA application across states, from 050444 to CO and KS hospitals on commonspirit.org. siblings_on_domain has no state filter.

## one-offs
- 161348 all (owner): Caught by clause A, so not a one-off. The store has no Clarke policy (only a cost-estimate page with no thresholds). Hold the sheet and scout clarkehosp.org for Clarke County Hospital's own FAP.
- 361311 eligibility.free_care_max_fpl: UNFLAGGED, found by this scan: quoted from WVU Medicine Weirton Medical Center's plain-language summary (wvumedicine.org/weirton/..., summary-07064a8ec4bf3a33), which names only Weirton. Drop the field or hold the sheet, and store Harrison Community Hospital's own FAP.
- 310084 free_care_max_fpl, discount_tiers, asset_test, insured_patients_covered, submit_methods: UNFLAGGED: every field is from Saint Barnabas Behavioral Health Center's FAP (rwjbh.org/documents/bh/sbbhc-fap-english.pdf). Hold the sheet and store Monmouth Medical Center Southern Campus's RWJBarnabas policy.
- 330141 discount_tiers, insured_patients_covered: UNFLAGGED: Catholic Health LI 2026 eligibility percentages list Good Samaritan, Mercy, St. Catherine, St. Charles, St. Francis and St. Joseph, not Long Island Community Hospital. Check the registry website (catholichealthli.org looks wrong) and use 'Wrong website' if so.
- 330195 eligibility.discount_tiers: UNFLAGGED: Long Island Jewish Medical Center (Northwell) has website catholichealthli.org and publishes Catholic Health's tiers. Mark the website wrong (northwell.edu), clear the sheet and rebuild.
- 330096 programs.presumptive: UNFLAGGED: quoted from Strong Memorial Hospital policy 3.09 ('Applicability University of Rochester - Strong Memorial Hospital'). Drop the field and store Jones Memorial's own policy.
- 100296 apply.decision_days, collections.eca_wait_days: UNFLAGGED: Bethesda Hospital Inc. policy, which 'applies to Bethesda Hospital East and Bethesda Hospital West'. Re-cite the Baptist Health South Florida policy that covers Doctors Hospital, or drop both fields.
- 101313 apply.decision_days, collections.eca_wait_days: UNFLAGGED: same Bethesda-only policy as 100296. Re-cite the BHSF policy covering Mariners Hospital, or drop.
- 360230 free_care_max_fpl, insured_patients_covered, documents_required: UNFLAGGED: quoted from the Cleveland Clinic Rehabilitation Hospital (CCRH) policy and application. Cleveland Clinic's own summary says its policy is 'not applicable to Cleveland Clinic Rehabilitation Hospitals'. Hold and re-cite Cleveland Clinic's hospital FAP, as Medina (360091) does.
- 050488 free_care_max_fpl, documents_required, submit_methods: UNFLAGGED: quoted from Sutter's Surgery Center Division page ('request a copy of the application form from your surgery center'). Re-cite Sutter's hospital FA page, which is already linked (billing-e43ea01319174ab6).
- 060003 apply.decision_days: UNFLAGGED, low stakes: quoted from Mercy San Juan Medical Center's (CA) CommonSpirit application. Re-cite Longmont United's own application or drop. The same applies to 060015 and 171388.
- 450162 apply.documents_required: UNFLAGGED: quoted from Providence's generic application under website providence.org. Check whether Grace Surgical Hospital (Lubbock) is a Providence facility; if not, mark the website wrong.

## notes
**Rule safety on the 605 published sheets (3,184 fields):**
- I ran the real verify_sheet over all 605 sheets, read-only, with pipeline._documents_set_aside plus the two clauses as extra set-asides.
- New rejections fall only on 161348 (6 fields), 110129 (7 fields, including contacts.languages) and 330197 (2 new; free care was already rejected).
- No sheet the audit judged correct is hit:
  - The 4 Hartford HealthCare sheets cite an off-domain HHC policy that names no hospital, but each hospital's own page links hartfordhealthcare.org, so they pass.
  - Lakewood (241301, CommonSpirit G-003) and Wamego (171337, Ascension) also pass on that link.
  - Southside (330043, listed by Northwell as "South Shore University Hospital") and Flagler (100090, listed by UF Health as "UF Health St Johns") are on their system's own domain, so clause A never judges them, and clause B needs an own-labelled link.
- At build time, clause A also flags 7 uncited documents on 6 sheets. None is the hospital's:
  - a Banner CO uniform application (061302)
  - a PayerPrice page about Sisters of Charity (220063)
  - chiamass.gov pages (221300)
  - findhelp.org pages (221302)
  - an 849-character HSHS policy-manager shell (520075, 521310)
- Tests: 19 cases, all pass, using real stored texts (scratch-owner-scope/tests_final.py).

**Variants rejected:**
- Matching the hospital name on any single word fires only on 161348; it lets 110129 through because "emory" is in its CMS name.
- Requiring the exact URL to be linked (instead of the domain) wrongly fires on the 4 HHC sheets, Lakewood and Wamego.
- Counting how many registry hospitals a document names is too noisy ("York" in "New York", "Mission", "Washington").
- Clause B without the "document never names H" guard wrongly fires on Chester County (Penn summary with an LGH anchor link); with "Hospital" as the facility marker it fires on Flushing's NYS uniform form.

**Limits:**
- Clause A treats any link from the hospital's own site to the system's domain (even a privacy-notice link) as proof the document is the hospital's. That errs toward not firing.
- If a hospital genuinely uses a system policy but only the system PDF is stored, its sheet is held with a foreign_documents item for an admin to re-link.
- On-domain cases where the policy states its own owner ("applies to Bethesda Hospital East and West", "CCRH's policy", "It is the policy of Saint Barnabas Behavioral Health Center", Catholic Health's facility list) need entity extraction. Brand renames would make that unsafe, so they are listed as one-offs.

**Outside my pattern:** 160104 (Bettendorf) uses the same UPH policy but quotes its Illinois-only Schedule B. That is an other-state section: _STATE_HEADING in bands_under_another_state needs "for Illinois facilities" and misses "SCHEDULE B - ILLINOIS LAWS ... Illinois Hospital".

Scratch files are in /private/tmp/claude-501/-Users-grey-Desktop-dev-eventhorizon/b529e6d4-a357-482a-ba81-d6830b9a174d/scratchpad/waive/audit-rest/scratch-owner-scope/ (candidate_final.py, tests_final.py, recheck_sim2.py, hits_jsonl.py, scan_b2.py). No repo file was edited and the database was opened only with mode=ro.
