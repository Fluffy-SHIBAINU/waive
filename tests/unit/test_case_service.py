import random
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.cases.extract import BillExtract, IncomeExtract
from waive.cases.service import (
    MAX_OPEN_SCOUT_REQUESTS,
    READ_LIMITS,
    CaseContext,
    TooManyReads,
    approve,
    authorize,
    confirm_bill,
    delete_case,
    purge_cases,
    relink,
    set_household,
    start_case,
    submit_bill,
    submit_income_letter,
    view,
)
from waive.cases.synth import make_truth, render_bill
from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.db import CaseRow, init_db, make_engine, session_scope
from waive.learning.hashing import case_hash
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
    assert not shown.deadlines.anchor_confirmed  # the reader did not say it was the first bill

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


def test_an_approved_case_only_changes_when_the_caregiver_allows_it(ctx):
    links = start_case(ctx, "MA")
    submit_bill(ctx, links.case_id, bill_image())
    set_household(ctx, links.case_id, 1, Decimal("22800"), ())
    assert approve(ctx, links.case_id).status == "approved"
    attempts = (
        lambda: submit_bill(ctx, links.case_id, bill_image()),
        lambda: confirm_bill(ctx, links.case_id, {}),
        lambda: set_household(ctx, links.case_id, 4, Decimal("1"), ()),
        lambda: submit_income_letter(ctx, links.case_id, bill_image()),
    )
    for attempt in attempts:
        with pytest.raises(PermissionError):
            attempt()
    shown = view(ctx, links.case_id)
    assert (shown.status, shown.household_size, shown.annual_income) == (
        "approved",
        1,
        Decimal("22800"),
    )
    # The caregiver's own correction form may still re-correct an approved case.
    shown = confirm_bill(ctx, links.case_id, {"amount_due": "900"}, allow_approved=True)
    shown = set_household(ctx, links.case_id, 2, Decimal("22800"), (), allow_approved=True)
    assert shown.household_size == 2 and shown.bill.amount_due == Decimal("900")


def test_a_case_may_only_ask_the_paid_reader_a_few_times_a_day(ctx):
    """One senior link must not be a free meter on the vision model (spec §11)."""
    assert READ_LIMITS == {"bill": 5, "income": 5, "paper": 10}
    links = start_case(ctx, "MA")
    for _ in range(5):
        submit_bill(ctx, links.case_id, bill_image())
    with pytest.raises(TooManyReads):
        submit_bill(ctx, links.case_id, bill_image())
    assert len(ctx.ai.calls) == 5  # the refused read never reached the model
    submit_income_letter(ctx, links.case_id, bill_image())  # letters have their own count
    tomorrow = replace(ctx, today=TODAY + timedelta(days=1))
    assert submit_bill(tomorrow, links.case_id, bill_image()).status == "bill_read"


def unknown_hospital(role, messages, schema, *, phi, purpose, max_tokens=2000):
    return BillExtract(
        hospital_name="Somewhere Else Hospital",
        fap_url="https://pay.somewhere-else.org/acct/ACCT-20260903?patient=rosa",
        amount_due=Decimal("100"),
        statement_date=TODAY,
    )


def scout_requests(session):
    return [item for item in repo.open_review_items(session) if item.kind == "scout_request"]


def test_unknown_hospital_requests_scouting(ctx):
    links = start_case(ctx, "MA")
    ctx.ai.complete_json = unknown_hospital
    shown = submit_bill(ctx, links.case_id, bill_image())
    assert shown.ccn is None and shown.needs_scouting
    [request] = scout_requests(ctx.session)
    # Only what the scheduler's matcher reads, tied to the case by its one-way hash: the model's
    # reading of a personal document must be deletable with the case and carry no account URL.
    assert request.detail == {
        "hospital_name": "Somewhere Else Hospital",
        "fap_url": "pay.somewhere-else.org",
        "state": "MA",
        "cases": [case_hash(links.case_id)],
        "count": 1,
    }
    assert view(ctx, links.case_id).tier is None
    delete_case(ctx, links.case_id)
    assert scout_requests(ctx.session) == []


def test_bills_naming_the_same_unknown_hospital_share_one_scout_request(ctx):
    """An anonymous loop must not flood the review queue (spec §11): bills for one hospital
    merge into one request that counts distinct cases, and open requests are capped."""
    first, second = start_case(ctx, "MA"), start_case(ctx, "MA")
    ctx.ai.complete_json = unknown_hospital
    submit_bill(ctx, first.case_id, bill_image())
    submit_bill(ctx, second.case_id, bill_image())
    submit_bill(ctx, second.case_id, bill_image())  # the same case again does not count twice
    [request] = scout_requests(ctx.session)
    assert request.detail["count"] == 2
    assert sorted(request.detail["cases"]) == sorted(
        case_hash(c) for c in (first.case_id, second.case_id)
    )
    delete_case(ctx, first.case_id)
    [request] = scout_requests(ctx.session)
    assert request.detail == {**request.detail, "cases": [case_hash(second.case_id)], "count": 1}
    assert MAX_OPEN_SCOUT_REQUESTS == 200


def test_open_scout_requests_are_capped(ctx, monkeypatch):
    monkeypatch.setattr("waive.cases.service.MAX_OPEN_SCOUT_REQUESTS", 1)
    ctx.ai.complete_json = unknown_hospital
    submit_bill(ctx, start_case(ctx, "MA").case_id, bill_image())
    ctx.ai.complete_json = lambda role, messages, schema, *, phi, purpose, max_tokens=2000: (
        BillExtract(hospital_name="Yet Another Hospital", amount_due=Decimal("100"))
    )
    shown = submit_bill(ctx, start_case(ctx, "MA").case_id, bill_image())
    assert shown.needs_scouting  # the caregiver can still pick the hospital by hand
    assert len(scout_requests(ctx.session)) == 1


def test_purge_removes_abandoned_and_expired_cases_only(ctx):
    abandoned = start_case(ctx, "MA")  # created, never photographed
    expired = start_case(ctx, "MA")
    ctx.ai.complete_json = unknown_hospital
    submit_bill(ctx, expired.case_id, bill_image())
    live_new = start_case(ctx, "MA")
    live = start_case(ctx, "MA")
    submit_bill(ctx, live.case_id, bill_image())
    ctx.session.get(CaseRow, abandoned.case_id).created_at = datetime(2026, 9, 29, tzinfo=UTC)
    ctx.session.get(CaseRow, expired.case_id).created_at = datetime(2025, 1, 1, tzinfo=UTC)
    ctx.session.get(CaseRow, live.case_id).created_at = datetime(2026, 9, 1, tzinfo=UTC)
    ctx.session.flush()
    assert purge_cases(ctx.session, TODAY) == {"abandoned": 1, "expired": 1}
    remaining = {row.id for row in ctx.session.scalars(select(CaseRow))}
    assert remaining == {live_new.case_id, live.case_id}
    [request] = scout_requests(ctx.session)  # the expired case left its scout request too
    assert request.detail["cases"] == [case_hash(live.case_id)]
    assert purge_cases(ctx.session, TODAY) == {"abandoned": 0, "expired": 0}


def test_choosing_a_hospital_or_reading_nothing_leaves_no_scout_request(ctx):
    links = start_case(ctx, "MA")
    ctx.ai.complete_json = unknown_hospital
    submit_bill(ctx, links.case_id, bill_image())
    assert len(scout_requests(ctx.session)) == 1
    confirm_bill(ctx, links.case_id, {}, ccn="229999")  # the caregiver knew the hospital
    assert scout_requests(ctx.session) == []
    blank = start_case(ctx, "MA")
    ctx.ai.complete_json = lambda role, messages, schema, *, phi, purpose, max_tokens=2000: (
        BillExtract(amount_due=Decimal("100"))
    )
    submit_bill(ctx, blank.case_id, bill_image())
    assert scout_requests(ctx.session) == []  # nothing a scout could match later


def later_statement(role, messages, schema, *, phi, purpose, max_tokens=2000):
    """A third statement, already a final notice: the first bill was months earlier."""
    return BillExtract(
        hospital_name="St. Example Medical Center",
        hospital_phone="617-555-0100",
        statement_date=date(2026, 9, 3),
        amount_due=Decimal("1850.00"),
        is_first_statement=False,
        collection_notice=True,
        collection_notice_date=date(2026, 11, 15),
    )


def test_a_later_statement_keeps_the_dates_provisional_until_the_first_bill_is_known(ctx):
    links = start_case(ctx, "MA")
    ctx.ai.complete_json = later_statement
    submit_bill(ctx, links.case_id, bill_image())
    shown = set_household(ctx, links.case_id, 1, Decimal("22800"), ())
    assert shown.deadlines.application_deadline == date(2027, 5, 1)
    assert shown.deadlines.anchor_confirmed is False
    assert shown.deadlines.collection_notice_too_early is None  # not asserted from a guess
    # The caregiver enters the first bill's date: the clocks move and the notice is judged.
    shown = confirm_bill(ctx, links.case_id, {"first_statement_date": "2026-07-05"})
    assert shown.deadlines.application_deadline == date(2027, 3, 2)
    assert shown.deadlines.collections_allowed_from == date(2026, 11, 2)
    assert shown.deadlines.anchor_confirmed is True
    assert shown.deadlines.collection_notice_too_early is False
    assert shown.bill.statement_date == date(2026, 9, 3)  # the cover letter still cites this one


def test_the_caregiver_can_mark_the_photographed_statement_as_the_first(ctx):
    links = start_case(ctx, "MA")
    submit_bill(ctx, links.case_id, bill_image())
    shown = confirm_bill(ctx, links.case_id, {"is_first_statement": True})
    shown = set_household(ctx, links.case_id, 1, Decimal("22800"), ())
    assert shown.bill.first_statement_date == date(2026, 9, 3)
    assert shown.deadlines.anchor_confirmed is True
    assert shown.deadlines.application_deadline == date(2027, 5, 1)


def test_a_hospital_outside_the_registry_cannot_be_chosen(ctx):
    links = start_case(ctx, "MA")
    submit_bill(ctx, links.case_id, bill_image())
    with pytest.raises(KeyError):
        confirm_bill(ctx, links.case_id, {}, ccn="999999")
    assert ctx.session.get(CaseRow, links.case_id).ccn == "229999"


def test_relink_revokes_both_old_links_and_keeps_the_case(ctx):
    """README: links are revocable. A senior link forwarded over SMS stays valid for 90 days
    unless the caregiver can cut it off without deleting the case."""
    links = start_case(ctx, "MA")
    submit_bill(ctx, links.case_id, bill_image())
    before = set_household(ctx, links.case_id, 1, Decimal("22800"), ())
    fresh = relink(ctx, links.case_id)
    assert fresh.case_id == links.case_id
    assert fresh.senior_token != links.senior_token
    for old in (links.senior_token, links.caregiver_token):
        with pytest.raises(PermissionError, match="revoked"):
            authorize(ctx, old, "senior")
    assert authorize(ctx, fresh.senior_token, "senior").id == links.case_id
    assert authorize(ctx, fresh.caregiver_token, "caregiver").id == links.case_id
    with pytest.raises(PermissionError):
        authorize(ctx, fresh.senior_token, "caregiver")
    row = ctx.session.get(CaseRow, links.case_id)
    assert row.token_generation == 2
    after = view(ctx, links.case_id)
    assert (after.status, after.tier, after.annual_income) == (
        before.status,
        before.tier,
        before.annual_income,
    )
    assert row.prediction == ctx.session.get(CaseRow, links.case_id).prediction
    again = relink(ctx, links.case_id)
    assert row.token_generation == 3
    with pytest.raises(PermissionError):
        authorize(ctx, fresh.caregiver_token, "caregiver")
    assert authorize(ctx, again.caregiver_token, "caregiver").id == links.case_id


def test_corrections_override_extraction(ctx):
    links = start_case(ctx, "MA")
    submit_bill(ctx, links.case_id, bill_image())
    shown = confirm_bill(
        ctx, links.case_id, {"statement_date": "2026-08-01", "amount_due": "900"}, ccn="229999"
    )
    assert shown.bill.statement_date == date(2026, 8, 1) and shown.bill.amount_due == Decimal("900")
