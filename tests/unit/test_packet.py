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
