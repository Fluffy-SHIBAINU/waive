from waive.atlas import repo
from waive.atlas.discover import (
    DomainResult,
    _evidence,
    host_of,
    is_directory,
    name_tokens,
    pick_domain,
    run_discovery,
)
from waive.atlas.samples import st_example_sheet
from waive.atlas.tavily_gateway import SearchHit
from waive.db import init_db, make_engine, session_scope

HOSPITAL = st_example_sheet().hospital.model_copy(update={"website_domain": None})


def hit(url, title="", content="", score=0.5):
    return SearchHit(url=url, title=title, content=content, score=score)


def test_host_and_directory_detection():
    assert host_of("https://www.Example.org/path?x=1") == "example.org"
    assert host_of("https://jobs.bilh.org/x") == "bilh.org"
    assert host_of("https://planmygift.baystatehealth.org/") == "baystatehealth.org"
    assert is_directory("en.wikipedia.org")
    assert is_directory("healthgrades.com")
    assert is_directory("findhelp.org") and is_directory("www.networkofcare.org")
    assert is_directory("addictions.com")
    # Picked as Harrington Hospital's (220019) official site; its pages describe other hospitals.
    assert is_directory("billfairly.com") and is_directory("www.billfairly.com")
    assert not is_directory("stexample.org")


def test_name_tokens_drop_generic_words():
    assert name_tokens("St. Example Medical Center") == {"example"}
    assert name_tokens("UMASS MEMORIAL HEALTHALLIANCE HOSPITALS") == {"umass", "healthalliance"}


def test_pick_domain_prefers_confirmed_official_site():
    hits = [
        hit("https://en.wikipedia.org/wiki/St_Example", "St. Example Medical Center", score=0.95),
        hit(
            "https://www.stexample.org/",
            "St. Example Medical Center | Boston",
            "Call 617-555-0100",
            score=0.7,
        ),
        hit("https://www.healthgrades.com/hospital/st-example", score=0.9),
    ]
    result = pick_domain(HOSPITAL, hits)
    assert result == DomainResult("stexample.org", 0.9, "https://www.stexample.org/")


def test_body_mentions_lose_to_title_matches():
    hits = [
        hit(
            "https://seacoastortho.example.com/referrals",
            "Referral partners",
            "We work with St. Example Medical Center and others",
            score=0.9,
        ),
        hit(
            "https://stexample.org/financial-assistance",
            "Financial Assistance | St. Example Medical Center",
            score=0.5,
        ),
    ]
    result = pick_domain(HOSPITAL, hits)
    # The title wins, but names neither Boston nor Massachusetts, and carries no phone: Good
    # Samaritan Hospital in San Jose has the same title as the one in Cincinnati (task 7.15a), so
    # such a site waits for an admin to confirm it (decision 7.9a-1).
    assert (result.domain, result.confidence) == ("stexample.org", 0.6)
    placed = hit(
        hits[1].url, "Financial Assistance | St. Example Medical Center, Boston", score=0.5
    )
    assert pick_domain(HOSPITAL, [hits[0], placed]).confidence == 0.9


def test_pick_domain_without_evidence_has_low_confidence():
    result = pick_domain(HOSPITAL, [hit("https://somewhere.com/page", "Unrelated", score=0.8)])
    assert result.domain == "somewhere.com"
    assert result.confidence == 0.6


def test_pick_domain_returns_none_when_only_directories():
    assert pick_domain(HOSPITAL, [hit("https://www.yelp.com/biz/st-example")]) is None


def test_pick_domain_never_returns_a_website_an_admin_rejected():
    # 7.11a: rediscovery would likely find the same wrong site again (360107, 330332, 450395).
    hits = [
        hit("https://www.stexample.org/", "St. Example Medical Center | Boston", score=0.9),
        hit("https://www.other.org/", "Paying your bill", score=0.5),
    ]
    assert pick_domain(HOSPITAL, hits).domain == "stexample.org"
    assert pick_domain(HOSPITAL, hits, rejected={"stexample.org"}).domain == "other.org"
    assert pick_domain(HOSPITAL, hits[:1], rejected={"stexample.org"}) is None


class FakeGateway:
    def __init__(self, hits):
        self.hits = hits
        self.queries = []

    def search(self, query, **kwargs):
        self.queries.append(query)
        return self.hits


def test_run_discovery_updates_rows_and_flags_low_confidence():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        repo.upsert_hospital(
            session,
            {
                "ccn": "229999",
                "name": "ST. EXAMPLE MEDICAL CENTER",
                "city": "BOSTON",
                "state": "MA",
                "zip": "02118",
                "phone": "617-555-0100",
                "hospital_type": "Acute Care Hospitals",
                "ownership": "Voluntary non-profit - Private",
            },
        )
    gateway = FakeGateway([hit("https://somewhere.com/page", "Unrelated", score=0.8)])
    with session_scope(engine) as session:
        results = run_discovery(session, gateway, "MA")
    assert results[0][0] == "229999"
    assert results[0][1].domain == "somewhere.com"
    assert "ST. EXAMPLE MEDICAL CENTER" in gateway.queries[0]
    with session_scope(engine) as session:
        row = repo.get_hospital(session, "229999")
        assert (row.website_domain, row.domain_confidence) == ("somewhere.com", 0.6)
        [item] = repo.open_review_items(session, "229999")
        assert item.kind == "domain"
        # An admin found it is not the hospital's site: discovery does not pick it again.
        row.website_domain, row.domain_confidence = None, None
        item.detail = {"found": None, "cleared": "somewhere.com", "reason": "not the hospital"}
    with session_scope(engine) as session:
        assert run_discovery(session, gateway, "MA") == [("229999", None)]
        assert repo.get_hospital(session, "229999").website_domain is None


def test_guide_sites_directories_and_government_hosts_are_never_the_hospital():
    # 7.9: ten of 57 hospitals in the first national batch landed on another site: careroute.ai
    # and fairvisithealth.com (third-party guides about many hospitals), carelistings.com and
    # seniorhealthdatabase.com (directories), illinois.gov and paauditor.gov (state records).
    for host in (
        "careroute.ai",
        "www.careroute.ai",
        "fairvisithealth.com",
        "carelistings.com",
        "seniorhealthdatabase.com",
        "illinois.gov",
        "www.paauditor.gov",
        "dph.illinois.gov",
        # Batch 2 (2026-10-10): price and nonprofit directories whose page titles name the
        # hospital (seven hospitals at confidence 0.9), and a dictionary entry for "Christus".
        "payerprice.com",
        "www.causeiq.com",
        "merriam-webster.com",
    ):
        assert is_directory(host), host
    assert not is_directory("akrongeneral.org") and not is_directory("gov.example.org")
    assert (
        pick_domain(
            HOSPITAL,
            [
                hit(
                    "https://www.careroute.ai/hospital-financial-assistance/x",
                    "St. Example Medical Center Financial Assistance",
                    score=0.9,
                )
            ],
        )
        is None
    )


def test_a_single_distinctive_name_token_in_a_title_is_not_proof_of_the_official_site():
    # Akron General (360027, 7.9): name_tokens drops "general", "medical" and "center", so the
    # one token left, "akron", matched Akron Children's Hospital's page title at confidence 0.9.
    akron = HOSPITAL.model_copy(
        update={
            "name": "AKRON GENERAL MEDICAL CENTER",
            "city": "AKRON",
            "state": "OH",
            "phone": None,
        }
    )
    childrens = hit(
        "https://www.akronchildrens.org/pages/Financial-Assistance.html",
        "Financial Assistance | Akron Children's",
        "Akron Children's Hospital helps families in Akron pay for medical care at the center.",
        score=0.9,
    )
    result = pick_domain(akron, [childrens])
    assert (result.domain, result.confidence) == ("akronchildrens.org", 0.6)
    official = hit(
        "https://my.clevelandclinic.org/locations/akron-general",
        "Cleveland Clinic Akron General | Akron, Ohio",
        score=0.7,
    )
    result = pick_domain(akron, [childrens, official])
    assert (result.domain, result.confidence) == ("clevelandclinic.org", 0.9)
    # The CMS phone number on the page is still proof on its own.
    with_phone = akron.model_copy(update={"phone": "330-344-6000"})
    assert (
        pick_domain(
            with_phone,
            [hit("https://www.akrongeneral.org/", "Home", "Call 330-344-6000", score=0.5)],
        ).confidence
        == 0.9
    )


def hospital(name, city, state, phone=None):
    return HOSPITAL.model_copy(update={"name": name, "city": city, "state": state, "phone": phone})


def test_directories_a_newsroom_a_staffing_firm_and_asset_hosts_are_never_the_hospital():
    # Task 7.15a: hosts discovery took for hospital sites in the national batches.
    for host in (
        "lownhospitalsindex.org",  # three OSF hospitals, 140012, 140179, 141310
        "www.hospitalbills411.com",  # Keck Hospital of USC, 050696
        "healthbillcentral.com",  # Loyola, 140276; the Hospital of the University of Pennsylvania
        "brightbillapp.com",  # OSF Saint Elizabeth, 140110
        "calhospital.org",  # the California Hospital Association, for Ojai, 051334
        "njspotlightnews.org",  # a newsroom, for Heights University Hospital, 310016
        "communityplatform.us",  # a county resource directory, for CHRISTUS Santa Rosa, 450237
        "vituity.com",  # a physician staffing firm, for Memorial Hospital Los Banos, 050528
    ):
        assert is_directory(host), host
    # Mercy Harvard (141335) got cloudinary.com, where one account holds the policies of Truman,
    # Southeast Health, Mercy Health and Lake Region: an asset host is nobody's website.
    mercy = hospital("MERCY HARVARD HOSPITAL", "HARVARD", "IL", "815-943-5431")
    assets = [
        hit(
            "https://res.cloudinary.com/dpmykpsih/image/upload/mercyhealth-site-398/media/"
            "bd5f019a1dc14a9e8b06989107db282f/mercy-health-financial-assistance-policy.pdf",
            "Hospital Financial Assistance | Mercy Harvard Hospital, Harvard, Illinois",
            "Call 815-943-5431",
            score=0.9,
        ),
        hit(
            "https://baystatehealth.canto.com/direct/document/x",
            "Mercy Harvard Hospital",
            score=0.8,
        ),
    ]
    assert pick_domain(mercy, assets) is None


def test_a_hit_that_names_other_states_but_not_the_hospitals_is_skipped():
    # North Shore University Hospital, Manhasset, New York (330106) was matched to NorthShore
    # University HealthSystem in Illinois, whose page names Illinois and not New York (7.15a).
    north_shore = hospital("NORTH SHORE UNIVERSITY HOSPITAL", "MANHASSET", "NY", "516-562-0100")
    illinois = hit(
        "https://www.northshore.org/globalassets/about-us/billing/plain-language-summary.pdf",
        "NorthShore University HealthSystem Financial Assistance ...",
        "NorthShore University HealthSystem Financial Assistance Plain Language Summary. "
        "NorthShore University HealthSystem, Evanston, Illinois.",
        score=0.826,
    )
    northwell = hit(
        "https://www.northwell.edu/billing-and-insurance/financial-assistance-programs-policies/"
        "financial-assistance-policy",
        "Financial assistance policy - Financial assistance programs & policies | Northwell Health",
        "Cohen Children's Medical Center Glen Cove Hospital Huntington Hospital Lenox Hill Hospital "
        "Mather Hospital North Shore University Hospital",
        score=0.668,
    )
    assert pick_domain(north_shore, [illinois]) is None
    assert pick_domain(north_shore, [illinois, northwell]).domain == "northwell.edu"
    # A page that names the hospital's state beside others stays, and so does one whose only
    # state is in the hospital's own name or town, or is a street ("Kansas City", "Sierra Nevada",
    # "Washington Street").
    hillcrest = hospital("HILLCREST HOSPITAL", "MAYFIELD HEIGHTS", "OH")
    summary = hit(
        "https://my.clevelandclinic.org/-/scassets/files/org/patients-visitors/billing/x.pdf",
        "Summary of Financial Assistance",
        "Cleveland Clinic Hillcrest Hospital, Cleveland, OH 44193; Florida; Nevada",
    )
    assert pick_domain(hillcrest, [summary]).domain == "clevelandclinic.org"
    kansas_city = hospital("SAINT LUKES HOSPITAL OF KANSAS CITY", "KANSAS CITY", "MO")
    assert pick_domain(
        kansas_city, [hit("https://www.saintlukeskc.org/", "Saint Luke's Kansas City")]
    )
    sierra = hospital("SIERRA NEVADA MEMORIAL HOSPITAL", "GRASS VALLEY", "CA")
    assert pick_domain(sierra, [hit("https://www.example.org/", "Sierra Nevada Memorial Hospital")])
    beverly = hospital("BEVERLY HOSPITAL", "BEVERLY", "MA")
    street = hit("https://www.beverlyhospital.org/", "Beverly Hospital", "85 Washington Street")
    assert pick_domain(beverly, [street]).domain == "beverlyhospital.org"


def test_a_name_with_one_distinctive_word_needs_that_word_on_the_page():
    # Community Hospital of San Bernardino (050089, 7.15a): "community" and "hospital" stood in
    # for "bernardino", so Mountains Community Hospital's title was proof at confidence 0.9.
    san_bernardino = hospital("COMMUNITY HOSPITAL OF SAN BERNARDINO", "SAN BERNARDINO", "CA")
    mountains = hit(
        "https://www.mchcares.com/helppayingyourbill",
        "Financial Help Available | Mountains Community Hospital",
        "The Hospital Bill Complaint Program is a state program that reviews hospital decisions.",
        score=0.37,
    )
    dignity = hit(
        "https://www.commonspirit.org/patient-resources/dignity-health-socal-financial-assistance",
        "Dignity Health Southern California Financial Assistance | CommonSpirit Health",
        "To view the list of providers who are covered or not by the financial assistance policy, "
        "please select a facility below. #### Covered Community Hospital San Bernardino California "
        "Hospital Medical Center St. Bernardine Medical Center",
        score=0.736,
    )
    assert _evidence(san_bernardino, mountains) == 0.0
    assert _evidence(san_bernardino, dignity) == 0.3
    result = pick_domain(san_bernardino, [mountains, dignity])
    assert (result.domain, result.confidence) == ("commonspirit.org", 0.6)
    # With no distinctive word at all, a title is proof only beside the hospital's town:
    # Community Medical Center in Toms River, New Jersey (310041) is not Fresno's Community
    # Medical Centers.
    toms_river = hospital("COMMUNITY MEDICAL CENTER", "TOMS RIVER", "NJ")
    fresno = hit(
        "https://www.communitymedical.org/for-patients-families/billing-insurance/"
        "help-paying-your-bill",
        "Community Medical Centers - Help Paying Your Bill",
        "Community Medical Centers has a long history of giving back to our community.",
        score=0.57,
    )
    rwj = hit(
        "https://www.rwjbh.org/community-medical-center/billing/charges",
        "Charges | Community Medical Center",
        "Community Medical Center, 99 Route 37 West, Toms River, NJ 08755. Financial assistance "
        "(partial and in full).",
        score=0.5,
    )
    assert _evidence(toms_river, fresno) == 0.3
    assert pick_domain(toms_river, [fresno, rwj]) == DomainResult("rwjbh.org", 0.9, rwj.url)


def test_a_hit_that_names_the_hospitals_place_beats_a_same_named_hospital_elsewhere():
    # Task 7.15a: each of these was matched to the other hospital's site at confidence 0.9.
    good_samaritan = hospital("GOOD SAMARITAN HOSPITAL", "CINCINNATI", "OH", "513-862-2601")
    trihealth = hit(
        "https://www.trihealth.com/patients-and-visitors/billing-and-insurance/financial-assistance",
        "Financial Assistance | TriHealth",
        "Good Samaritan Hospital 375 Dixmyth Ave. Cincinnati, OH 45220 1st Floor Dixmyth Lobby",
        score=0.723,
    )
    san_jose = hit(
        "https://www.goodsamsanjose.com/patient-resources/patient-financial-resources/"
        "financial-assistance",
        "Financial Assistance | Good Samaritan Hospital",
        "Good Samaritan Hospital ##### Specialties ##### Patients # Financial assistance",
        score=0.675,
    )
    result = pick_domain(good_samaritan, [trihealth, san_jose])
    assert result == DomainResult("trihealth.com", 0.6, trihealth.url)
    # Mission, Texas (450176): the town is the hospital's own name, so "Mission Health" in
    # Asheville names no place; the Texas address does.
    mission = hospital("MISSION REGIONAL MEDICAL CENTER", "MISSION", "TX", "956-323-9000")
    texas = hit(
        "https://missionrmc.org/wp-content/uploads/2024/11/MRTX_FAP-Application-Instructions.pdf",
        "Financial Assistance Application Form Instructions",
        "900 South Bryan Road, Mission, TX 78572 Rev. 06/2024-AR Mission Regional Medical Center "
        "Financial Assistance Application Form Instructions",
        score=0.883,
    )
    asheville = hit(
        "https://www.missionhealth.org/patient-resources/patient-financial-resources/"
        "financial-assistance",
        "Charity Financial Assistance (Medical Debt Mitigation) Policy | Mission Health",
        "Financial relief and payment plans may be available to patients whose income falls "
        "between 0 - 400% of the Federal Poverty Level (FPL).",
        score=0.647,
    )
    assert pick_domain(mission, [texas, asheville]).domain == "missionrmc.org"
    # Huntington, New York (330045), against Cabell Huntington in West Virginia.
    huntington = hospital("NS/LIJ HS HUNTINGTON HOSPITAL", "HUNTINGTON", "NY", "631-351-2000")
    northwell = hit(
        "https://www.northwell.edu/billing-and-insurance/financial-assistance-programs-policies/"
        "contact-us",
        "Contact the Financial Assistance Unit | Northwell Health",
        "| Glen Cove Hospital 101 St. Andrews Lane Glen Cove, NY 11542 Phone: (516) 674-7335 "
        "Huntington Hospital 270 Park Avenue Huntington, NY 11743 Phone: (631) 351-7944",
        score=0.634,
    )
    cabell = hit(
        "https://cabellhuntington.org/customer-service/financial-assistance-policy",
        "Financial Assistance Policy - Marshall Health Network - Cabell Huntington Hospital",
        "Financial counselors are on hand to address your concerns. They may be reached by phone "
        "at 304.526.6099 or 304.526.6344.",
        score=0.535,
    )
    assert pick_domain(huntington, [northwell, cabell]).domain == "northwell.edu"
    # Its title alone, with neither the place nor the phone, is a guess an admin confirms.
    assert pick_domain(huntington, [cabell]) == DomainResult(
        "cabellhuntington.org", 0.6, cabell.url
    )


def test_the_right_site_stays_sure_when_its_page_names_the_place_or_carries_the_phone():
    # Fulton County Medical Center, McConnellsburg, Pennsylvania (391303): the probe's five hits.
    fulton = hospital("FULTON COUNTY MEDICAL CENTER", "MCCONNELLSBURG", "PA", "717-485-3155")
    hits = [
        hit(
            "https://www.fcmcpa.org/getpage.php?name=Financial_Assistance",
            "Financial Assistance",
            "Patient Financial Advocates at Fulton County Medical Center can navigate the Financial "
            "Assistance the hospital offers that best suits you.",
            score=0.865,
        ),
        hit(
            "https://www.fcmcpa.org",
            "Pennsylvania Hospital | 24 Hour Emergency Care Fulton County",
            "skip navigation 214 Peach Orchard Road, McConnellsburg, PA 17233 Fulton County Medical "
            "Center logo Patient Portal",
            score=0.726,
        ),
        hit("https://www.fcmcpa.org/getpage.php?name=Registration_Process", "Registration Process",
            "FCMC has a financial assistance program.", score=0.704),
        hit(
            "https://www.fcmcpa.org/community-resources",
            "Community Resources",
            "## Fulton County Medical Center 214 Peach Orchard Road McConnellsburg, PA (717) "
            "485-3155",
            score=0.671,
        ),
    ]  # fmt: skip
    assert pick_domain(fulton, hits) == DomainResult("fcmcpa.org", 0.9, "https://www.fcmcpa.org")
    # Methodist Richardson Medical Center, Texas (450537): the town is in its name, and its
    # billing page carries the hospital's phone.
    richardson = hospital("METHODIST RICHARDSON MEDICAL CENTER", "RICHARDSON", "TX", "469-204-1000")
    billing = hit(
        "https://www.methodisthealthsystem.org/methodist-richardson-medical-center/billing-estimates",
        "Billing & Estimates | Methodist Health System",
        'Dismiss Modal (469) 204-1000 204-1000") MyChart Pay My Bill Contact Us Giving 1. Home '
        "2. Methodist Richardson Medical Center 3. Billing & Estimates",
        score=0.678,
    )
    hits = [
        hit(
            "https://dollarfor.org/medical-debt-help-richardson-methodist-richardson-medical-center",
            "Medical Bill Help from Methodist Richardson Medical Center – Dollar For",
            score=0.763,
        ),
        hit(
            "https://www.methodisthealthsystem.org/patients-visitors/patient-tools-support/"
            "financial-assistance",
            "Financial Assistance | Methodist Health System",
            "Methodist Health System has a Financial Assistance Policy that provides free or "
            "reduced-cost hospital care.",
            score=0.731,
        ),
        billing,
    ]
    assert pick_domain(richardson, hits) == DomainResult(
        "methodisthealthsystem.org", 0.9, billing.url
    )


def test_documents_must_name_the_hospitals_state_and_the_hospital_or_its_town():
    # National batch 2: Bellevue Hospital (Ohio) was published from Overlake Medical Center in
    # Bellevue, Washington, and Chilton Medical Center from another New Jersey hospital's site.
    from waive.atlas.discover import place_unconfirmed

    bellevue = HOSPITAL.model_copy(
        update={"name": "BELLEVUE HOSPITAL", "city": "BELLEVUE", "state": "OH", "zip": "44811"}
    )
    overlake = (
        "Overlake Medical Center & Clinics, 1035 116th Ave NE, Bellevue, WA 98004. Patients at or "
        "below 300% of the federal poverty level receive free care."
    )
    assert place_unconfirmed(bellevue, [overlake]) == "the documents never name Ohio"
    assert (
        place_unconfirmed(
            bellevue, ["The Bellevue Hospital, 1400 West Main Street, Bellevue, OH 44811"]
        )
        is None
    )
    assert place_unconfirmed(bellevue, ["Financial help for Ohio residents at Bellevue"]) is None
    # "Oh" as an exclamation, or the letters inside another word, are not the state.
    assert place_unconfirmed(bellevue, ["Oh, and Bellevue patients in JOHANNESBURG"]) is not None
    chilton = HOSPITAL.model_copy(
        update={"name": "CHILTON MEDICAL CENTER", "city": "POMPTON PLAINS", "state": "NJ"}
    )
    mountainside = "Mountainside Medical Center, 1 Bay Avenue, Montclair, NJ 07042. Charity care."
    assert (
        place_unconfirmed(chilton, [mountainside])
        == "the documents name neither Pompton Plains nor the hospital"
    )
    system_policy = (
        "Atlantic Health System, New Jersey. Covered facilities: Chilton Medical Center."
    )
    assert place_unconfirmed(chilton, [system_policy]) is None
    assert (
        place_unconfirmed(chilton, ["Billing office: 97 West Parkway, Pompton Plains, NJ"]) is None
    )
    # Nothing to judge (no web documents): not this check's call.
    assert place_unconfirmed(chilton, []) is None


def test_a_website_matched_with_low_confidence_waits_for_an_admin_to_confirm_it():
    # Decision 7.9a-1 (task 7.13): of about 20 sheets published from a 0.6 match, three were
    # another hospital's website on day one (7.11a).
    from waive.atlas.discover import CONFIRMED_CONFIDENCE, MIN_CONFIDENCE, domain_unconfirmed

    assert domain_unconfirmed("example.org", 0.6)
    assert domain_unconfirmed("example.org", 0.79)
    assert domain_unconfirmed("example.org", None)  # set with no confidence recorded
    assert not domain_unconfirmed("example.org", MIN_CONFIDENCE)
    assert not domain_unconfirmed("example.org", 0.9)
    assert not domain_unconfirmed("example.org", 0.95)  # entered by hand in the loop, still checked
    assert not domain_unconfirmed("example.org", CONFIRMED_CONFIDENCE)
    assert CONFIRMED_CONFIDENCE == 1.0
    # No website: nothing to confirm (the build discovers one first, or stops).
    assert not domain_unconfirmed(None, 0.6) and not domain_unconfirmed(None, None)
