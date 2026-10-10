from waive.atlas import repo
from waive.atlas.discover import (
    DomainResult,
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
    assert (result.domain, result.confidence) == ("stexample.org", 0.9)


def test_pick_domain_without_evidence_has_low_confidence():
    result = pick_domain(HOSPITAL, [hit("https://somewhere.com/page", "Unrelated", score=0.8)])
    assert result.domain == "somewhere.com"
    assert result.confidence == 0.6


def test_pick_domain_returns_none_when_only_directories():
    assert pick_domain(HOSPITAL, [hit("https://www.yelp.com/biz/st-example")]) is None


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
