# Website check of held sheets — 2026-10-16 (task 7.15c)

Waive holds a sheet when its hospital's website was matched with low confidence (0.6, decision 7.9a-1) or when the stored documents never name the hospital's state or town (the place check). This list covers the **138 held sheets** in either situation that have a website set. For each, the loop judged from the domain, the stored documents' titles and places, and what is publicly known about the hospital's owner.

What the decisions do once approved:

- **Withdraw** (2, published today on waive.kindred-ai.dev): the sheet was built from another organization's site. `reject_domain` holds it at once and clears the site.
- **Confirm** (56): the website is the hospital's or its health system's. `confirm_domain` sets confidence 1.0 (an admin's confirmation); the sheet is re-decided from stored data. Most stay held for another reason (no income rule in the stored documents, document conflicts, patient-share tables); about four publish (Kings Daughters Ohio, Corpus Christi Medical Center, Hillcrest, UMass Memorial Medical Center), each already checked by eye against its quote.
- **Wrong** (68 held sheets, plus 23 hospitals without a sheet): the website belongs to another organization, a directory, a news site or a staffing firm. `reject_domain` detaches its documents, keeps the sheet held and makes discovery skip that website; the hospital is then rediscovered and re-scouted with the 7.15b discovery rules (about 10 Tavily credits each, ≈ 900 credits in all, run in daily portions next to the regular batches).
- **Unsure** (14): left as they are.

No sheet is published from a website that is not the hospital's own or its system's.

## Withdraw now (2 published sheets)

| CCN | State | Hospital | Website | Whose site it is |
|---|---|---|---|---|
| 390111 | PA | Hospital of the University of Pennsylvania | healthbillcentral.com | a billing directory (Penn Medicine is pennmedicine.org); the sheet's only rule, "Up to 200% FPL", is the directory's summary |
| 450229 | TX | Hendrick Medical Center, Abilene | continuecare.org | ContinueCare Hospital at Hendrick, a separate long-term acute care hospital (Hendrick Health is hendrickhealth.org) |

## Hospitals without a sheet whose discovered website is not a hospital's (23)

Cleared for rediscovery: castleconnolly.com (330259 Mercy Medical Center NY, 360355 Ohio Valley Surgical Hospital), fatf-gafi.org (140135 Decatur Memorial), hospitalcompare.io (450162 Grace Surgical Hospital), medibillsaver.com (360040 Knox Community Hospital), medicarelist.com (050137 Kaiser Panorama City, 310028 Newton Medical Center, 360072 Fairfield Medical Center), nbclosangeles.com (050779 Martin Luther King Jr. Community Hospital), news10.com (330191 Glens Falls Hospital), njha.com (310032 Inspira Vineland), oh.us (361303 Lodi Community Hospital), ohiodailydigital.com (360197 Mary Rutan Hospital), reddit.com (390217 Excela Frick), safehospitalsusa.com (330055 NewYork-Presbyterian/Queens), sitecorecontenthub.cloud (140113 OSF Heart of Mary), syracuse.com (330175 Guthrie Cortland), turquoise.health (390329 Jefferson Einstein Montgomery, 450211 CHI St. Luke's Lufkin, 451316 Madison St. Joseph, 451322 Grimes St. Joseph, 670059 CHI St. Luke's Lakeside), wiktionary.org (140046 Good Samaritan Regional Health Center).

## Confirm (56)

| CCN | State | Hospital | Website | Why |
|---|---|---|---|---|
| 050017 | CA | Mercy General Hospital, Sacramento | commonspirit.org | Dignity Health (CommonSpirit); Greater Sacramento page |
| 050036 | CA | Bakersfield Memorial Hospital | commonspirit.org | Dignity Health Central Valley page |
| 050043 | CA | Alta Bates Summit Medical Center, Oakland | sutterhealth.org | Sutter Health |
| 050122 | CA | Dameron Hospital, Stockton | dameronhospital.org | its own site |
| 050149 | CA | California Hospital Medical Center, Los Angeles | commonspirit.org | Dignity Health Southern California page |
| 050357 | CA | Goleta Valley Cottage Hospital | cottagehealth.org | Cottage Health |
| 050496 | CA | John Muir Medical Center, Concord | johnmuirhealth.com | John Muir Health |
| 050590 | CA | Methodist Hospital of Sacramento | commonspirit.org | Dignity Health Greater Sacramento page |
| 100006 | FL | Orlando Health | orlandohealth.com | its own site |
| 100032 | FL | Orlando Health Bayfront Hospital | orlandohealth.com | Orlando Health |
| 100045 | FL | AdventHealth DeLand | adventhealth.com | AdventHealth |
| 100069 | FL | AdventHealth Carrollwood | adventhealth.com | AdventHealth |
| 100072 | FL | AdventHealth Fish Memorial | adventhealth.com | AdventHealth |
| 100093 | FL | Baptist Hospital, Pensacola | ebaptisthealthcare.org | Baptist Health Care |
| 100217 | FL | Orlando Health Sebastian River Hospital | orlandohealth.com | Orlando Health |
| 100266 | FL | Gulf Breeze Hospital | ebaptisthealthcare.org | Baptist Health Care |
| 101300 | FL | AdventHealth Wauchula | adventhealth.com | AdventHealth |
| 140002 | IL | Alton Memorial Hospital | bjc.org | BJC HealthCare |
| 140010 | IL | NorthShore University HealthSystem, Evanston | northshore.org | its own system site |
| 140029 | IL | Copley Memorial Hospital, Aurora | rush.edu | Rush Copley |
| 140091 | IL | Carle Foundation Hospital, Urbana | carle.org | Carle Health |
| 140209 | IL | Carle Health Methodist Hospital, Peoria | carle.org | Carle Health |
| 140308 | IL | Mercyhealth Hospital, Crystal Lake | mercyhealthsystem.org | Mercyhealth |
| 141301 | IL | Kirby Medical Center, Monticello | kirbyhealth.org | its own site |
| 220001 | MA | UMass Memorial HealthAlliance Hospitals | ummhealth.org | UMass Memorial Health |
| 220012 | MA | Cape Cod Hospital | capecodhealth.org | Cape Cod Healthcare |
| 220163 | MA | UMass Memorial Medical Center | ummhealth.org | UMass Memorial Health |
| 220175 | MA | MetroWest Medical Center | mwmc.com | its own site |
| 310005 | NJ | Hunterdon Medical Center | hunterdonhealth.org | Hunterdon Health |
| 310045 | NJ | Englewood Hospital | englewoodhealth.org | Englewood Health |
| 310064 | NJ | AtlantiCare Regional Medical Center, City Campus | atlanticare.org | AtlantiCare |
| 310115 | NJ | AHS Hospital Corp (Hackettstown) | atlantichealth.org | Atlantic Health |
| 330049 | NY | Northern Dutchess Hospital, Rhinebeck | northwell.edu | Northwell Health (Nuvance joined Northwell) |
| 330074 | NY | F. F. Thompson Hospital, Canandaigua | thompsonhealth.com | its own site |
| 330101 | NY | NewYork-Presbyterian Hospital | nyp.org | its own site |
| 330153 | NY | Ellis Hospital, Schenectady | ellismedicine.org | Ellis Medicine |
| 330194 | NY | Maimonides Medical Center | maimo.org | its own site |
| 330224 | NY | HealthAlliance Hospital, Kingston | wmchealth.org | WMCHealth |
| 331303 | NY | Garnet Health Medical Center Catskills | garnethealth.org | Garnet Health |
| 360039 | OH | Genesis Hospital, Zanesville | genesishcs.org | Genesis HealthCare System |
| 360179 | OH | Bethesda North, Cincinnati | trihealth.com | TriHealth |
| 360230 | OH | Hillcrest Hospital, Mayfield Heights | clevelandclinic.org | Cleveland Clinic |
| 360361 | OH | Kings Daughters Medical Center Ohio, Portsmouth | ukkingsdaughters.com | UK King's Daughters operates it |
| 360365 | OH | Avita Ontario | avitahealth.org | Avita Health System |
| 361320 | OH | Holzer Medical Center Jackson | holzer.org | Holzer Health System |
| 390032 | PA | Allegheny Valley Hospital | ahn.org | Allegheny Health Network |
| 450051 | TX | Methodist Dallas Medical Center | methodisthealthsystem.org | Methodist Health System |
| 450068 | TX | Memorial Hermann – Texas Medical Center | memorialhermann.org | Memorial Hermann |
| 450610 | TX | Memorial Hermann Memorial City | memorialhermann.org | Memorial Hermann |
| 450684 | TX | Memorial Hermann Northeast | memorialhermann.org | Memorial Hermann |
| 450723 | TX | Methodist Charlton Medical Center | methodisthealthsystem.org | Methodist Health System |
| 450788 | TX | Corpus Christi Medical Center | ccmedicalcenter.com | its own site (HCA) |
| 451390 | TX | Dimmit Regional Hospital | dimmitregionalhospital.com | its own site |
| 670023 | TX | Methodist Mansfield Medical Center | methodisthealthsystem.org | Methodist Health System |
| 670300 | TX | Methodist Midlothian Medical Center | methodisthealthsystem.org | Methodist Health System |
| 670340 | TX | Methodist Celina Medical Center | methodisthealthsystem.org | Methodist Health System |

## Wrong website (68 held sheets)

| CCN | State | Hospital | Website | Whose site it is |
|---|---|---|---|---|
| 050040 | CA | LAC/Olive View-UCLA Medical Center | 1degree.org | a resource directory |
| 050089 | CA | Community Hospital of San Bernardino | barstowhospital.com | Barstow Community Hospital |
| 050107 | CA | Marian Regional Medical Center, Santa Maria | communitymedical.org | Community Medical Centers, Fresno |
| 050180 | CA | John Muir Medical Center, Walnut Creek | kaiserpermanente.org | Kaiser Permanente (John Muir Health is johnmuirhealth.com) |
| 050295 | CA | Mercy Hospital, Bakersfield | mercyhealthrehabhospital.com | a Mercy rehabilitation hospital elsewhere |
| 050528 | CA | Memorial Hospital Los Banos | vituity.com | a physician staffing firm |
| 050557 | CA | Memorial Medical Center, Modesto | mycmh.org | another Community Memorial organization |
| 050603 | CA | MemorialCare Saddleback Medical Center | prnewswire.com | a press-release site |
| 050696 | CA | Keck Hospital of USC | hospitalbills411.com | a billing directory |
| 050704 | CA | Mission Community Hospital, Panorama City | riversidecommunityhospital.com | Riverside Community Hospital |
| 051334 | CA | Community Memorial Hospital – Ojai | calhospital.org | California Hospital Association |
| 100019 | FL | Holmes Regional Medical Center, Melbourne | hcafloridahealthcare.com | HCA Florida (Holmes is Health First) |
| 100043 | FL | Mease Dunedin Hospital | hcafloridahealthcare.com | HCA Florida (Mease is BayCare) |
| 100050 | FL | Larkin Community Hospital Palm Springs | ama-assn.org | American Medical Association |
| 100063 | FL | Morton Plant North Bay Hospital | hcafloridahealthcare.com | HCA Florida (BayCare) |
| 100073 | FL | Holy Cross Hospital, Fort Lauderdale | holycrosshealth.org | Holy Cross Health, Maryland |
| 100105 | FL | Cleveland Clinic Indian River Hospital | sunshinesupportnetwork.com | a support-network site |
| 100127 | FL | Morton Plant Hospital, Clearwater | hcafloridahealthcare.com | HCA Florida (BayCare) |
| 101315 | FL | Jay Hospital | thefriary.org | The Friary (Baptist Health Care's site is ebaptisthealthcare.org) |
| 140012 | IL | OSF Saint Katharine Medical Center | lownhospitalsindex.org | Lown Institute hospital index |
| 140018 | IL | Mt. Sinai Hospital Medical Center, Chicago | mountsinai.org | Mount Sinai, New York |
| 140110 | IL | OSF Saint Elizabeth Medical Center, Ottawa | brightbillapp.com | a billing app |
| 140133 | IL | Holy Cross Hospital, Chicago | holycrossmedicalcenter.org | Holy Cross Medical Center, New Mexico |
| 140148 | IL | Memorial Medical Center, Springfield | wearememorial.com | Memorial Health System, Mississippi |
| 140185 | IL | Memorial Hospital, Belleville | wearememorial.com | Memorial Health System, Mississippi |
| 140160 | IL | FHN Memorial Hospital, Freeport | mhtlc.org | Memorial Hospital, Carthage, Illinois |
| 140197 | IL | Methodist Hospital of Chicago | methodisthealthsystem.org | Methodist Health System, Dallas |
| 141352 | IL | Jacksonville Memorial Hospital | mhtlc.org | Memorial Hospital, Carthage, Illinois |
| 141310 | IL | OSF Saint Paul Medical Center | lownhospitalsindex.org | Lown Institute hospital index |
| 141335 | IL | Mercy Harvard Hospital | cloudinary.com | an image host (Mercyhealth's site is mercyhealthsystem.org) |
| 141337 | IL | OSF Saint Clare Medical Center, Princeton | saintclares.com | Saint Clare's Health, New Jersey |
| 220066 | MA | Mercy Medical Center, Springfield | mercy.net | Mercy, St. Louis (documents name Missouri, Kansas, Oklahoma) |
| 310016 | NJ | Heights University Hospital | njspotlightnews.org | a news site |
| 310041 | NJ | Community Medical Center, Toms River | communitymedical.org | Community Medical Centers, Fresno |
| 330045 | NY | Huntington Hospital | cabellhuntington.org | Cabell Huntington, West Virginia |
| 330094 | NY | Columbia Memorial Hospital, Hudson | columbiamemorial.org | Columbia Memorial Hospital, Astoria, Oregon |
| 330102 | NY | Kenmore Mercy Hospital | mercy.com | Bon Secours Mercy Health, Ohio |
| 330106 | NY | North Shore University Hospital, Manhasset | northshore.org | NorthShore University HealthSystem, Illinois |
| 330135 | NY | Bon Secours Community Hospital, Port Jervis | bonsecours.com | Bon Secours Mercy Health |
| 330188 | NY | Mount St. Mary's Hospital, Lewiston | trinityhealthma.org | Trinity Health Mid-Atlantic |
| 330193 | NY | Flushing Hospital Medical Center | seattlechildrens.org | Seattle Children's |
| 330279 | NY | Mercy Hospital of Buffalo | ecmc.edu | Erie County Medical Center |
| 360070 | OH | Mercy Medical Center, Canton | mercymedcenter.com | another Mercy Medical Center |
| 360077 | OH | Fairview Hospital, Cleveland | cobijohealth.org | another organization (documents name Adventist Health Sonora) |
| 360080 | OH | East Ohio Regional Hospital | doxo.com | a bill-payment directory |
| 360082 | OH | Euclid Hospital | uhhospitals.org | University Hospitals (Euclid is Cleveland Clinic) |
| 360121 | OH | Community Hospitals and Wellness Centers, Bryan | parkview.com | Parkview Health, Indiana |
| 360134 | OH | Good Samaritan Hospital, Cincinnati | goodsamsanjose.com | Good Samaritan Hospital, San Jose |
| 360143 | OH | Marymount Hospital | medibillsaver.com | a closed billing site |
| 360152 | OH | Doctors Hospital, Columbus | doctors-hospital.net | another Doctors Hospital |
| 360156 | OH | Memorial Hospital, Fremont | memorialohio.com | Memorial Health, Marysville |
| 360172 | OH | Mercy Regional Medical Center, Lorain | mercymedcenter.com | another Mercy Medical Center |
| 361309 | OH | Henry County Hospital, Napoleon | greatriverhealth.org | Great River Health, Iowa |
| 361314 | OH | Magruder Hospital | seattlechildrens.org | Seattle Children's |
| 361316 | OH | Bucyrus Community Hospital | ohiohealth.com | OhioHealth (Bucyrus is Avita Health System) |
| 361318 | OH | Fostoria Community Hospital | nursa.com | a nurse staffing app |
| 390012 | PA | Jefferson Lansdale Hospital | jeffersonhosp.com | another Jefferson Hospital (documents name Georgia) |
| 390204 | PA | Nazareth Hospital, Philadelphia | saintmaryofnazarethhospital.com | Saint Mary of Nazareth, Chicago |
| 390265 | PA | Jefferson Hospital, Jefferson Hills | jeffersonhosp.com | another Jefferson Hospital (documents name Georgia) |
| 390266 | PA | Grove City Medical Center | cbsnews.com | a news site |
| 390267 | PA | Forbes Hospital | suburbanbhc.org | another organization (Forbes is Allegheny Health Network) |
| 391303 | PA | Fulton County Medical Center, McConnellsburg | fultoncountyhealthcenter.org | Fulton County Health Center, Ohio |
| 391309 | PA | Barnes-Kasson County Hospital | doxo.com | a bill-payment directory |
| 450046 | TX | CHRISTUS Spohn Hospital Corpus Christi | ccmedicalcenter.com | Corpus Christi Medical Center (HCA) |
| 450176 | TX | Mission Regional Medical Center, Mission | missionhealth.org | Mission Health, North Carolina |
| 450237 | TX | CHRISTUS Santa Rosa Medical Center | communityplatform.us | a county resource directory |
| 450395 | TX | CHI St. Luke's Health Memorial Livingston | saintlukeskc.org | Saint Luke's, Kansas City |
| 451395 | TX | Hill Regional Hospital | recovered.org | an addiction-treatment directory |

## Unsure — left as they are (14)

| CCN | State | Hospital | Website | Why unsure |
|---|---|---|---|---|
| 050073, 050076, 050541, 050765, 050777 | CA | Kaiser Foundation Hospitals (Vallejo, San Francisco, Redwood City, Moreno Valley, San Leandro) | kaiserpermanente.org | the right organization, but the stored policy never names California and names Oregon and Washington: likely another region's policy; needs a re-scout for the California one, not a confirmation |
| 101309 | FL | Hendry Regional Medical Center | hrmc.us | cannot tell whose site this is |
| 140008 | IL | Loyola Gottlieb Memorial Hospital | trinity-health.org | right system (Trinity Health), but the documents are other regions' |
| 310025 | NJ | CarePoint Health – Bayonne Medical Center | carepointhealth.org | the hospital changed operator; the old operator's site |
| 330019 | NY | New York Community Hospital of Brooklyn | nyulangone.org | not sure it is an NYU Langone hospital |
| 360358 | OH | Diley Ridge Medical Center | trinity-health.org | right system (Mount Carmel, Trinity Health), documents other regions' |
| 361304 | OH | Greenfield Area Medical Center | fmchealth.org | Adena's hospital on Fairfield Medical Center's site |
| 361321 | OH | Barnesville Hospital | wvumedicine.org | WVU Medicine likely operates it; the documents never name Ohio |
| 390156 | PA | Mercy Fitzgerald (Mercy Catholic Medical Center) | trinity-health.org | right system, documents other regions' |
| 451305 | TX | Burleson St. Joseph Health Center | commonspirit.org | CommonSpirit's "St. Joseph Health" pages may be the California St. Joseph |
