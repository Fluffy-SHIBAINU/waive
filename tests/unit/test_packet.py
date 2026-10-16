import io
from dataclasses import replace
from datetime import date
from decimal import Decimal

from pypdf import PdfReader

from waive.atlas.samples import st_example_sheet
from waive.cases.extract import BillExtract
from waive.cases.packet import PacketData, build_packet, packet_data
from waive.cases.service import CaseView
from waive.rules.deadlines import compute_deadlines
from waive.rules.eligibility import Household, evaluate_eligibility

TODAY = date(2026, 10, 2)


def test_packet_data_pulls_submit_methods_and_documents():
    sheet = st_example_sheet()
    result = evaluate_eligibility(sheet, Household(1, Decimal("22800"), "MA"))
    shown = CaseView(
        case_id="c",
        status="approved",
        hospital_name="St. Example Medical Center",
        ccn="229999",
        bill=BillExtract(
            patient_name="Rosa Alvarez",
            account_reference="ACCT-1",
            amount_due=Decimal("1850"),
            statement_date=date(2026, 9, 3),
        ),
        household_size=1,
        annual_income=Decimal("22800"),
        tier=result.tier,
        result=result,
        deadlines=compute_deadlines(date(2026, 9, 3), TODAY),
    )
    data = packet_data(shown, sheet, TODAY)
    assert data.submit_lines == [
        "Mail: Patient Financial Services, 1 Example Way, Boston, MA 02118",
        "Fax: 617-555-0199",
    ]
    assert data.documents == ["Photo ID", "Proof of income"]
    assert data.reasons[0][1].startswith("household income at or below 250%")
    assert data.tier_text.startswith("Likely free care")


def test_provisional_dates_are_labelled_and_never_claim_an_early_notice():
    sheet = st_example_sheet()
    result = evaluate_eligibility(sheet, Household(1, Decimal("22800"), "MA"))
    provisional = compute_deadlines(
        date(2026, 9, 3), TODAY, collection_notice=date(2026, 11, 15), anchor_confirmed=False
    )
    shown = CaseView(
        case_id="c",
        status="approved",
        hospital_name="St. Example Medical Center",
        ccn="229999",
        bill=BillExtract(amount_due=Decimal("1850"), statement_date=date(2026, 9, 3)),
        household_size=1,
        annual_income=Decimal("22800"),
        tier=result.tier,
        result=result,
        deadlines=provisional,
    )
    data = packet_data(shown, sheet, TODAY)
    assert data.collection_too_early is False
    text = "".join(page.extract_text() for page in PdfReader(io.BytesIO(build_packet(data))).pages)
    assert "first bill" in text and "501(r)-6" not in text
    sure = compute_deadlines(date(2026, 9, 3), TODAY, collection_notice=date(2026, 11, 15))
    data = packet_data(replace(shown, deadlines=sure), sheet, TODAY)
    assert data.collection_too_early is True
    text = "".join(page.extract_text() for page in PdfReader(io.BytesIO(build_packet(data))).pages)
    assert "501(r)-6" in text and "may be earlier" not in text


def test_build_packet_returns_pdf():
    data = PacketData(
        hospital_name="St. Example Medical Center",
        submit_lines=["Fax: 617-555-0199"],
        form_url=None,
        patient_name="Rosa Alvarez",
        account_reference="ACCT-1",
        amount_due=Decimal("1850"),
        statement_date=date(2026, 9, 3),
        household_size=1,
        annual_income=Decimal("22800"),
        tier_text="Likely free care",
        reasons=[("Income is 143% of the poverty line", "household income at or below 250%")],
        documents=["Photo ID"],
        deadlines=compute_deadlines(date(2026, 9, 3), TODAY),
        collection_too_early=True,
        today=TODAY,
    )
    pdf = build_packet(data)
    assert pdf[:5] == b"%PDF-" and len(pdf) > 2000


def test_the_cover_letter_for_a_ceiling_only_policy_never_claims_free_care():
    # Decision 7.9a-2 (task 7.14): the policy states only the income up to which help is given,
    # so the letter asks under that limit and leaves the amount to the hospital.
    from tests.unit.test_eligibility import CEILING_QUOTE, ceiling_only_sheet

    sheet = ceiling_only_sheet()
    result = evaluate_eligibility(sheet, Household(1, Decimal("22800"), "MA"))
    shown = CaseView(
        case_id="c",
        status="approved",
        hospital_name="St. Example Medical Center",
        ccn="229999",
        bill=BillExtract(amount_due=Decimal("1850"), statement_date=date(2026, 9, 3)),
        household_size=1,
        annual_income=Decimal("22800"),
        tier=result.tier,
        result=result,
        deadlines=compute_deadlines(date(2026, 9, 3), TODAY),
    )
    data = packet_data(shown, sheet, TODAY)
    assert data.tier_text == "Likely eligible for assistance at St. Example Medical Center."
    assert data.reasons == [
        (
            "Income is 143% of the poverty line; assistance exists up to 400% of the poverty "
            "level, and the hospital decides the amount.",
            CEILING_QUOTE,
        )
    ]
    pages = PdfReader(io.BytesIO(build_packet(data))).pages
    text = " ".join(" ".join(page.extract_text() for page in pages).split())
    assert "I believe I qualify. Likely eligible for assistance at St. Example Medical Center." in (
        text
    )
    assert "and the hospital decides the amount." in text
    assert f'Your policy states: "{CEILING_QUOTE}"' in text
    assert "free" not in text.lower()


def shown_for(result, bill, **extra):
    return CaseView(
        case_id="c",
        status="approved",
        hospital_name="St. Example Medical Center",
        ccn="229999",
        bill=bill,
        household_size=1,
        tier=result.tier,
        result=result,
        **extra,
    )


def pdf_text(data):
    return "".join(page.extract_text() for page in PdfReader(io.BytesIO(build_packet(data))).pages)


def test_a_qmb_case_gets_a_cost_sharing_complaint_letter_not_an_application():
    from waive.rules.coverage import cost_sharing_result

    bill = BillExtract(
        patient_name="Rosa Alvarez",
        account_reference="ACCT-1",
        amount_due=Decimal("1850"),
        statement_date=date(2026, 9, 3),
    )
    shown = shown_for(cost_sharing_result(), bill, next_step="cost_sharing", state="MA")
    data = packet_data(shown, st_example_sheet(), TODAY)
    assert data.kind == "cost_sharing"
    assert data.submit_lines[0].startswith("Mail:")  # the hospital's own address still helps
    text = pdf_text(data).replace("\n", " ")
    assert "1902(n)(3)(B)" in text and "447.15" in text
    assert "cancel" in text.lower() and "refund" in text.lower()
    assert "1-800-MEDICARE" in text and "MassHealth" in text
    assert "apply for financial assistance" not in text
    assert "Copy of your MassHealth or QMB card" in text


def test_a_cost_sharing_letter_needs_no_sheet():
    from waive.rules.coverage import cost_sharing_result

    shown = shown_for(
        cost_sharing_result(), BillExtract(amount_due=Decimal("1")), next_step="cost_sharing"
    )
    data = packet_data(shown, None, TODAY)
    assert data.kind == "cost_sharing" and data.submit_lines == [] and data.documents == []
    assert "address on the bill" in pdf_text(data).replace("\n", " ")


def test_a_physician_group_bill_gets_a_request_to_the_group():
    from waive.cases.extract import PayeeKind
    from waive.rules.coverage import payee_route

    bill = BillExtract(
        amount_due=Decimal("640"),
        statement_date=date(2026, 9, 3),
        payee_kind=PayeeKind.PHYSICIAN_GROUP,
        payee_name="Example Emergency Physicians, P.A.",
    )
    route = payee_route(st_example_sheet(), bill)
    shown = shown_for(
        route.result,
        bill,
        next_step="payee_request",
        payee_name="Example Emergency Physicians, P.A.",
    )
    data = packet_data(shown, st_example_sheet(), TODAY)
    assert data.kind == "payee_request" and data.submit_lines == []
    text = pdf_text(data).replace("\n", " ")
    assert "To: Example Emergency Physicians, P.A." in text
    assert "financial assistance" in text and "St. Example Medical Center" not in text[:200]
    assert "Federal Poverty" not in text
