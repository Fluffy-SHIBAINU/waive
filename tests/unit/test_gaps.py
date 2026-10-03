import base64
from datetime import date
from decimal import Decimal

import pytest

from waive.atlas import repo
from waive.atlas.publish import drop_fields, publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.cases.service import CaseContext, confirm_bill, set_household, start_case
from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.db import init_db, make_engine, session_scope
from waive.learning.classify import PhotoClass
from waive.learning.gaps import (
    INCOMPLETE,
    MISSING_FORM,
    NO_SHEET,
    answer_gap_ask,
    gap_ask,
    pending_gap_ask,
)
from waive.learning.intake import ingest_paper

from tests.unit.test_intake import ClassifyAI
from tests.unit.test_web_senior import HOSPITAL, photo

TODAY = date(2026, 10, 2)


def test_gap_ask_rules():
    sample = st_example_sheet()
    assert gap_ask(None) is NO_SHEET
    assert gap_ask(sample) is None
    assert gap_ask(drop_fields(sample, ["apply.documents_required"])) is MISSING_FORM
    assert gap_ask(drop_fields(sample, ["apply.submit_methods"])) is MISSING_FORM
    bare = drop_fields(
        sample,
        ["eligibility.free_care_max_fpl", "eligibility.discount_tiers", "apply.submit_methods"],
    )
    assert bare.completeness() < 0.5 and gap_ask(bare) is INCOMPLETE
    assert gap_ask(drop_fields(sample, ["eligibility.free_care_max_fpl"])) is INCOMPLETE
    assert PhotoClass.APPLICATION_FORM in MISSING_FORM.wanted
    assert "Take a photo" in NO_SHEET.question


@pytest.fixture
def ctx():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, drop_fields(st_example_sheet(), ["apply.documents_required"]))
        yield CaseContext(
            session=session,
            ai=ClassifyAI(PhotoClass.APPLICATION_FORM),
            cipher=FieldCipher(base64.b64decode(new_key())),
            signer=TokenSigner("x" * 40),
            today=TODAY,
        )


def confirmed(ctx):
    links = start_case(ctx, "MA")
    assert (
        pending_gap_ask(ctx, links.case_id) is None
    )  # nothing is asked before the bill is confirmed
    confirm_bill(
        ctx, links.case_id, {"statement_date": "2026-09-03", "amount_due": "1850.00"}, ccn="229999"
    )
    return links.case_id


def test_one_ask_per_case_skippable_or_answered_by_a_photo(ctx):
    case_id = confirmed(ctx)
    set_household(ctx, case_id, 1, Decimal("22800"), ())
    assert pending_gap_ask(ctx, case_id) is MISSING_FORM
    answer_gap_ask(ctx, case_id, "skipped")
    assert pending_gap_ask(ctx, case_id) is None

    other = confirmed(ctx)
    assert pending_gap_ask(ctx, other) is MISSING_FORM
    ingest_paper(ctx, other, photo()["photo"][1])
    assert pending_gap_ask(ctx, other) is None


def test_unknown_hospital_asks_for_any_paper(ctx):
    links = start_case(ctx, "MA")
    set_household(ctx, links.case_id, 1, Decimal("22800"), ())  # confirmed, but no hospital matched
    assert pending_gap_ask(ctx, links.case_id) is NO_SHEET
