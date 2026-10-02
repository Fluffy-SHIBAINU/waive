from waive.atlas.schema import HospitalRef
from waive.cases.extract import BillExtract
from waive.cases.match import is_confident, match_hospital


def hospital(ccn, name, phone=None, domain=None, city="BOSTON", zip="02118"):
    return HospitalRef(
        ccn=ccn,
        name=name,
        city=city,
        state="MA",
        zip=zip,
        phone=phone,
        ownership="Voluntary non-profit - Private",
        website_domain=domain,
    )


HOSPITALS = [
    hospital("1", "ST. EXAMPLE MEDICAL CENTER", "617-555-0100", "example.org"),
    hospital(
        "2",
        "ST. EXAMPLE NORTH SHORE HOSPITAL",
        "617-555-0199",
        "northshore.example.org",
        city="SALEM",
        zip="01970",
    ),
    hospital("3", "HARBOR GENERAL HOSPITAL", "617-555-0120", "harbor.example.net"),
]


def test_name_and_phone_pick_the_right_hospital():
    extract = BillExtract(hospital_name="St Example Medical Ctr", hospital_phone="(617) 555-0100")
    candidates = match_hospital(extract, HOSPITALS)
    assert candidates[0].ccn == "1"
    assert is_confident(candidates)


def test_fap_url_domain_breaks_ties():
    extract = BillExtract(
        hospital_name="St. Example", fap_url="https://northshore.example.org/financial-assistance"
    )
    candidates = match_hospital(extract, HOSPITALS)
    assert candidates[0].ccn == "2"


def test_ambiguous_name_is_not_confident():
    candidates = match_hospital(BillExtract(hospital_name="St. Example"), HOSPITALS)
    assert {c.ccn for c in candidates[:2]} == {"1", "2"}
    assert not is_confident(candidates)


def test_no_name_returns_nothing_confident():
    candidates = match_hospital(BillExtract(), HOSPITALS)
    assert candidates == [] or not is_confident(candidates)
