from datetime import date
from decimal import Decimal

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
