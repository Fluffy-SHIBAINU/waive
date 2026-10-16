"""Who may be billed at all, and which bills the hospital's policy covers (task 9.12)."""

from datetime import date

from waive.atlas.samples import SAMPLE_SOURCE_ID, st_example_sheet
from waive.atlas.schema import Cited, Coverage
from waive.cases.extract import BillExtract, PayeeKind
from waive.rules.coverage import (
    COST_SHARING_RULE,
    Route,
    cost_sharing_protected,
    medicaid_name,
    payee_route,
)
from waive.rules.eligibility import Tier


def test_qmb_and_medicaid_cards_protect_against_cost_sharing_bills():
    assert cost_sharing_protected(("QMB",))
    assert cost_sharing_protected(("MassHealth",))
    assert cost_sharing_protected(("medi-cal",))
    assert cost_sharing_protected(("SNAP", "Medicaid"))


def test_snap_alone_or_nothing_does_not():
    assert not cost_sharing_protected(("SNAP",))
    assert not cost_sharing_protected(())


def test_medicaid_name_follows_the_state():
    assert medicaid_name("MA") == "MassHealth"
    assert medicaid_name("ca") == "Medi-Cal"
    assert medicaid_name("TX") == "Medicaid"


def test_cost_sharing_rule_cites_the_statute_and_the_joint_statement():
    assert "1902(n)(3)(B)" in COST_SHARING_RULE.quote
    assert "447.15" in COST_SHARING_RULE.quote
    assert COST_SHARING_RULE.tier is Tier.NOT_OWED


def with_covered(names: list[str]):
    sheet = st_example_sheet()
    cited = Cited(
        value=names,
        quote="The following providers are covered by this policy: " + ", ".join(names),
        source_id=SAMPLE_SOURCE_ID,
        checked_on=date(2026, 10, 2),
    )
    return sheet.model_copy(update={"coverage": Coverage(covered_providers=cited)})


def test_a_hospital_facility_bill_is_covered():
    bill = BillExtract(payee_kind=PayeeKind.HOSPITAL_FACILITY, payee_name="St. Example")
    assert payee_route(st_example_sheet(), bill) is None


def test_an_unread_payee_is_treated_as_the_hospital():
    assert payee_route(st_example_sheet(), BillExtract()) is None


def test_a_physician_group_bill_is_not_covered_and_routes_to_the_group():
    bill = BillExtract(
        payee_kind=PayeeKind.PHYSICIAN_GROUP, payee_name="Example Emergency Physicians PA"
    )
    route = payee_route(st_example_sheet(), bill)
    assert route is not None
    assert route.kind is Route.PAYEE_REQUEST
    assert route.result.tier is Tier.NOT_COVERED
    assert "Example Emergency Physicians PA" in route.result.reasons[0].text
    assert route.result.reasons[0].quote is None


def test_anesthesia_and_other_payees_route_to_the_payee_too():
    for kind in (PayeeKind.ANESTHESIA_RADIOLOGY_PATHOLOGY, PayeeKind.OTHER):
        route = payee_route(st_example_sheet(), BillExtract(payee_kind=kind, payee_name="X LLC"))
        assert route is not None and route.kind is Route.PAYEE_REQUEST


def test_an_ambulance_bill_routes_to_ship():
    bill = BillExtract(payee_kind=PayeeKind.AMBULANCE, payee_name="Boston EMS")
    route = payee_route(st_example_sheet(), bill)
    assert route is not None
    assert route.kind is Route.SHIP
    assert route.result.tier is Tier.NOT_COVERED


def test_a_physician_group_named_on_the_covered_provider_list_is_covered_with_a_citation():
    sheet = with_covered(["Example Emergency Physicians, P.A.", "Example Anesthesia Associates"])
    bill = BillExtract(
        payee_kind=PayeeKind.PHYSICIAN_GROUP, payee_name="EXAMPLE EMERGENCY PHYSICIANS PA"
    )
    route = payee_route(sheet, bill)
    assert route is not None
    assert route.kind is Route.COVERED
    assert route.result is None
    assert route.reason is not None
    assert route.reason.field_path == "coverage.covered_providers"
    assert "covered by this policy" in route.reason.quote


def test_a_physician_group_not_on_the_list_is_still_not_covered():
    sheet = with_covered(["Example Anesthesia Associates"])
    bill = BillExtract(payee_kind=PayeeKind.PHYSICIAN_GROUP, payee_name="Harbor Hospitalists")
    route = payee_route(sheet, bill)
    assert route is not None and route.kind is Route.PAYEE_REQUEST


def test_a_payee_without_a_name_cannot_match_the_list():
    sheet = with_covered(["Example Emergency Physicians"])
    bill = BillExtract(payee_kind=PayeeKind.PHYSICIAN_GROUP)
    route = payee_route(sheet, bill)
    assert route is not None and route.kind is Route.PAYEE_REQUEST
    assert "the group that sent it" in route.result.reasons[0].text
