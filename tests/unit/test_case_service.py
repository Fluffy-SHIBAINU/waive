import random
from datetime import date
from decimal import Decimal

import pytest

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.cases.extract import BillExtract, IncomeExtract
from waive.cases.service import (
    CaseContext,
    approve,
    authorize,
    confirm_bill,
    delete_case,
    set_household,
    start_case,
    submit_bill,
    submit_income_letter,
    view,
)
from waive.cases.synth import make_truth, render_bill
from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.db import CaseRow, init_db, make_engine, session_scope
from waive.rules.eligibility import Tier

TODAY = date(2026, 10, 2)
HOSPITAL = {
    "ccn": "229999",
    "name": "ST. EXAMPLE MEDICAL CENTER",
    "city": "BOSTON",
    "state": "MA",
    "zip": "02118",
    "phone": "617-555-0100",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
    "website_domain": "example.org",
}


class FakeAI:
    def __init__(self):
        self.calls = []

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        self.calls.append((purpose, phi))
        if schema is BillExtract:
            return BillExtract(
                hospital_name="St. Example Medical Center",
                hospital_phone="617-555-0100",
                statement_date=date(2026, 9, 3),
                amount_due=Decimal("1850.00"),
                patient_name="Rosa Alvarez",
                account_reference="ACCT-1",
                fap_url="www.example.org/financial-assistance",
                confidence=0.9,
            )
        if schema is IncomeExtract:
            return IncomeExtract(monthly_benefit=Decimal("1900"))
        raise AssertionError(schema)


@pytest.fixture
def ctx():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, st_example_sheet())
        import base64

        yield CaseContext(
            session=session,
            ai=FakeAI(),
            cipher=FieldCipher(base64.b64decode(new_key())),
            signer=TokenSigner("x" * 40),
            today=TODAY,
        )


def bill_image():
    return render_bill(
        make_truth(random.Random(1)), random.Random(1), layout=0, rotate_deg=0.0, blur=0.0
    )


def test_full_flow_to_free_care(ctx):
    links = start_case(ctx, "MA")
    assert authorize(ctx, links.senior_token, "senior").id == links.case_id
    assert authorize(ctx, links.caregiver_token, "senior").id == links.case_id
    with pytest.raises(PermissionError):
        authorize(ctx, links.senior_token, "caregiver")

    shown = submit_bill(ctx, links.case_id, bill_image())
    assert shown.status == "bill_read"
    assert shown.hospital_name == "ST. EXAMPLE MEDICAL CENTER" and shown.ccn == "229999"
    assert shown.bill.amount_due == Decimal("1850.00")
    assert ctx.ai.calls[0] == ("case.bill", True)

    shown = confirm_bill(ctx, links.case_id, {"amount_due": "1850.00"})
    assert shown.status == "confirmed" and shown.tier is Tier.NEEDS_INFO

    shown = submit_income_letter(ctx, links.case_id, bill_image())
    assert shown.annual_income == Decimal("22800")
    shown = set_household(ctx, links.case_id, 1, shown.annual_income, ())
    assert (shown.status, shown.tier) == ("evaluated", Tier.FREE)
    assert "likely do not have to pay" in shown.senior_text
    assert "Policy says" in shown.caregiver_text
    assert shown.deadlines.application_deadline == date(2027, 5, 1)

    row = ctx.session.get(CaseRow, links.case_id)
    assert row.prediction == {
        "sheet_version": 1,
        "tier": "free",
        "fpl_band": "101-200",
        "predicted_documents": ["photo_id", "proof_of_income"],
        "created_on": "2026-10-02",
    }
    assert "Rosa" not in (row.sealed or "") and "Rosa" not in str(row.prediction)

    assert approve(ctx, links.case_id).status == "approved"
    delete_case(ctx, links.case_id)
    assert ctx.session.get(CaseRow, links.case_id) is None


def test_unknown_hospital_requests_scouting(ctx):
    links = start_case(ctx, "MA")
    ctx.ai.complete_json = lambda role, messages, schema, *, phi, purpose, max_tokens=2000: (
        BillExtract(
            hospital_name="Somewhere Else Hospital", amount_due=Decimal("100"), statement_date=TODAY
        )
    )
    shown = submit_bill(ctx, links.case_id, bill_image())
    assert shown.ccn is None and shown.needs_scouting
    assert any(item.kind == "scout_request" for item in repo.open_review_items(ctx.session))
    assert view(ctx, links.case_id).tier is None


def test_a_hospital_outside_the_registry_cannot_be_chosen(ctx):
    links = start_case(ctx, "MA")
    submit_bill(ctx, links.case_id, bill_image())
    with pytest.raises(KeyError):
        confirm_bill(ctx, links.case_id, {}, ccn="999999")
    assert ctx.session.get(CaseRow, links.case_id).ccn == "229999"


def test_corrections_override_extraction(ctx):
    links = start_case(ctx, "MA")
    submit_bill(ctx, links.case_id, bill_image())
    shown = confirm_bill(
        ctx, links.case_id, {"statement_date": "2026-08-01", "amount_due": "900"}, ccn="229999"
    )
    assert shown.bill.statement_date == date(2026, 8, 1) and shown.bill.amount_due == Decimal("900")
