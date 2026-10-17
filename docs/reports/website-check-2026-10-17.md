# Website check 2 — held sheets after the 7.15 re-scouts (task 7.17)

The 7.15 re-scouts rediscovered 93 hospitals' websites with the stricter discovery rules. A match without the hospital's place in the search hit now gets confidence 0.6, so the sheet waits for an admin (decision 7.9a-1). This list covers the **63 held sheets** on such a website, or failing the place check, that check 1 (2026-10-16) did not decide. The 14 left unsure in check 1 are left out.

What the decisions do once approved (on the VM, the atlas master):

- **Confirm** (29): `confirm_domain` sets confidence 1.0 and re-decides the sheet from stored data. Every one that publishes is checked by eye against its quote.
- **Wrong, site known** (24): `reject_domain`, then the hospital's own system site is set by hand at confidence 0.95 (still place-checked, as in 7.11a) and the hospital is re-scouted. That is about 9 Tavily credits each.
- **Wrong, rediscover** (8): `reject_domain` and a fresh discovery with the 7.15 rules, then a scout. Same cost.
- **Unsure** (2): left as they are.

About 290 credits in all, within the 1,000-a-day budget.

## Confirm (29)

| CCN | State | Hospital | Website | Why |
|---|---|---|---|---|
| 050180 | CA | John Muir Medical Center, Walnut Creek | johnmuirhealth.com | John Muir Health |
| 050510 | CA | Kaiser Foundation Hospital, San Rafael | kaiserpermanente.org | Kaiser Permanente (documents handled by 7.16) |
| 050696 | CA | Keck Hospital of USC | keckmedicine.org | Keck Medicine of USC |
| 050710 | CA | Kaiser Foundation Hospital, Fresno | kaiserpermanente.org | Kaiser Permanente |
| 050779 | CA | Martin Luther King, Jr. Community Hospital | mlkch.org | its own site |
| 140046 | IL | Good Samaritan Regional Health Center, Mount Vernon | ssmhealth.com | SSM Health |
| 140110 | IL | OSF Saint Elizabeth Medical Center | osfhealthcare.org | OSF HealthCare |
| 140135 | IL | Decatur Memorial Hospital | memorial.health | Memorial Health (Springfield) |
| 141337 | IL | OSF Saint Clare Medical Center, Princeton | osfhealthcare.org | OSF HealthCare |
| 141352 | IL | Jacksonville Memorial Hospital | memorial.health | Memorial Health (Springfield) |
| 220066 | MA | Mercy Medical Center, Springfield | trinityhealthofne.org | Trinity Health Of New England |
| 310032 | NJ | Inspira Medical Center Vineland | inspirahealthnetwork.org | Inspira Health |
| 330045 | NY | Huntington Hospital | northwell.edu | Northwell Health |
| 330106 | NY | North Shore University Hospital | northwell.edu | Northwell Health |
| 330175 | NY | Guthrie Cortland Medical Center | guthrie.org | Guthrie |
| 330191 | NY | Glens Falls Hospital | albanymed.org | Albany Med Health System |
| 360070 | OH | Mercy Medical Center, Canton | clevelandclinic.org | Cleveland Clinic Mercy Hospital |
| 360077 | OH | Fairview Hospital, Cleveland | clevelandclinic.org | Cleveland Clinic |
| 360134 | OH | Good Samaritan Hospital, Cincinnati | trihealth.com | TriHealth |
| 360152 | OH | Doctors Hospital, Columbus | ohiohealth.com | OhioHealth |
| 360172 | OH | Mercy Regional Medical Center, Lorain | mercy.com | Mercy Health (Bon Secours Mercy Health) |
| 360197 | OH | Mary Rutan Hospital | maryrutan.org | its own site |
| 390012 | PA | Jefferson Lansdale Hospital | jeffersonhealth.org | Jefferson Health |
| 390217 | PA | Excela Health Frick Hospital | independence.health | Independence Health System |
| 390329 | PA | Jefferson Einstein Montgomery Hospital | jeffersonhealth.org | Jefferson Health |
| 450046 | TX | CHRISTUS Spohn Hospital Corpus Christi | christushealth.org | CHRISTUS Health |
| 450176 | TX | Mission Regional Medical Center, Mission | missionrmc.org | its own site |
| 450211 | TX | CHI St. Luke's Health Memorial Lufkin | commonspirit.org | CommonSpirit |
| 450229 | TX | Hendrick Medical Center, Abilene | hendrickhealth.org | Hendrick Health |

## Wrong website, with the hospital's own site known (24)

| CCN | State | Hospital | Wrong website | Set by hand |
|---|---|---|---|---|
| 050089 | CA | Community Hospital of San Bernardino | montclair-hospital.org | commonspirit.org (Dignity Health) |
| 050557 | CA | Memorial Medical Center, Modesto | wearememorial.com | sutterhealth.org |
| 051334 | CA | Community Memorial Hospital – Ojai | cmhsumner.org | mycmh.org (Community Memorial Healthcare) |
| 100019 | FL | Holmes Regional Medical Center | orlandohealth.com | hf.org (Health First) |
| 100043 | FL | Mease Dunedin Hospital | mercy.com | baycare.org |
| 101315 | FL | Jay Hospital | hcafloridahealthcare.com | ebaptisthealthcare.org (Baptist Health Care) |
| 140133 | IL | Holy Cross Hospital, Chicago | holycrosshealth.org | sinaichicago.org |
| 140148 | IL | Memorial Medical Center, Springfield | mhs.net | memorial.health |
| 140185 | IL | Memorial Hospital, Belleville | memorial.health | bjc.org |
| 141335 | IL | Mercy Harvard Hospital | elderlifefinancial.com | mercyhealthsystem.org (Mercyhealth) |
| 310028 | NJ | Newton Medical Center | mynmchealth.org | atlantichealth.org |
| 330094 | NY | Columbia Memorial Hospital, Hudson | communitymemorial.org | columbiamemorialhealth.org |
| 330102 | NY | Kenmore Mercy Hospital | mercy.net | chsbuffalo.org (Catholic Health) |
| 330188 | NY | Mount St. Mary's Hospital, Lewiston | yahoo.com | chsbuffalo.org (Catholic Health) |
| 330259 | NY | Mercy Medical Center, Rockville Centre | mercymedcenter.com | catholichealthli.org |
| 330279 | NY | Mercy Hospital of Buffalo | catholichealthli.org | chsbuffalo.org (Catholic Health) |
| 360082 | OH | Euclid Hospital | ohiohealth.com | clevelandclinic.org |
| 360121 | OH | Community Hospitals and Wellness Centers, Bryan | ohiohealth.com | chwchospital.org |
| 361316 | OH | Bucyrus Community Hospital | bonsecours.com | avitahealth.org |
| 361318 | OH | Fostoria Community Hospital | bonsecours.com | promedica.org |
| 390111 | PA | Hospital of the University of Pennsylvania | spotlightpa.org | pennmedicine.org |
| 390265 | PA | Jefferson Hospital, Jefferson Hills | jeffersonhealth.org | ahn.org (Allegheny Health Network) |
| 451316 | TX | Madison St. Joseph Health Center | usacs.com | chisjh.org (CHI St. Joseph Health) |
| 050704 | CA | Mission Community Hospital, Panorama City | missionhealth.org | missioncommunityhospital.org |

## Wrong website, rediscover (8)

| CCN | State | Hospital | Wrong website | Whose site it is |
|---|---|---|---|---|
| 050040 | CA | LAC/Olive View-UCLA Medical Center | uclahealth.org | UCLA Health; Olive View is a Los Angeles County hospital, whose own .gov site discovery never picks |
| 100050 | FL | Larkin Community Hospital Palm Springs | goodbill.com | a billing advocacy service |
| 140160 | IL | FHN Memorial Hospital, Freeport | breezhealth.com | a financial-assistance software vendor |
| 140197 | IL | Methodist Hospital of Chicago | sahealth.com | Methodist Healthcare, San Antonio |
| 310016 | NJ | Heights University Hospital | uhnj.org | University Hospital, Newark |
| 360080 | OH | East Ohio Regional Hospital | bluebrix.health | a billing software vendor |
| 360156 | OH | Memorial Hospital, Fremont | ohiohealth.com | OhioHealth |
| 361303 | OH | Lodi Community Hospital | adventisthealth.org | Adventist Health Lodi Memorial, California |

## Unsure — left as they are (2)

| CCN | State | Hospital | Website | Why unsure |
|---|---|---|---|---|
| 390204 | PA | Nazareth Hospital, Philadelphia | trinity-health.org | right system (Trinity Health Mid-Atlantic); the stored documents never name the hospital or Philadelphia |
| 450395 | TX | CHI St. Luke's Health Memorial Livingston | chisjh.org | CHI St. Joseph Health's site; Livingston belongs to CHI St. Luke's Health (Houston) |
