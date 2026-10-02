"""The printable application packet: cover letter, data sheet, checklist (spec §9 step 8)."""

import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

from waive.atlas.schema import ProcedureSheet
from waive.cases.service import CaseView
from waive.rules.deadlines import Deadlines
from waive.rules.explain import caregiver_summary

DOC_LABELS = {
    "photo_id": "Photo ID",
    "proof_of_income": "Proof of income",
    "social_security_letter": "Social Security benefit letter",
    "tax_return": "Most recent tax return",
    "pay_stubs": "Recent pay stubs",
    "bank_statements": "Bank statements",
    "proof_of_residency": "Proof of address",
    "insurance_card": "Insurance card",
    "medicaid_denial": "Medicaid (MassHealth) denial letter",
    "other": "Other documents the hospital asks for",
}
METHOD_LABELS = {
    "mail": "Mail",
    "fax": "Fax",
    "email": "Email",
    "portal": "Online",
    "in_person": "In person",
    "phone": "Phone",
}


@dataclass
class PacketData:
    hospital_name: str
    submit_lines: list[str]
    form_url: str | None
    patient_name: str | None
    account_reference: str | None
    amount_due: Decimal | None
    statement_date: date | None
    household_size: int | None
    annual_income: Decimal | None
    tier_text: str
    reasons: list[tuple[str, str]]
    documents: list[str]
    deadlines: Deadlines | None
    collection_too_early: bool
    today: date


def packet_data(shown: CaseView, sheet: ProcedureSheet, today: date) -> PacketData:
    methods = sheet.apply.submit_methods.value if sheet.apply.submit_methods else []
    documents = sheet.apply.documents_required.value if sheet.apply.documents_required else []
    result = shown.result
    bill = shown.bill
    name = shown.hospital_name or sheet.hospital.name
    return PacketData(
        hospital_name=name,
        submit_lines=[f"{METHOD_LABELS[m.kind]}: {m.detail}" for m in methods],
        form_url=sheet.apply.form_url.value if sheet.apply.form_url else None,
        patient_name=bill.patient_name if bill else None,
        account_reference=bill.account_reference if bill else None,
        amount_due=bill.amount_due if bill else None,
        statement_date=bill.statement_date if bill else None,
        household_size=shown.household_size,
        annual_income=shown.annual_income,
        tier_text=caregiver_summary(result, name).split("\n")[0] if result else "",
        reasons=[(r.text, r.quote or "") for r in (result.reasons if result else [])],
        documents=[DOC_LABELS.get(d.value, d.value) for d in documents],
        deadlines=shown.deadlines,
        collection_too_early=bool(shown.deadlines and shown.deadlines.collection_notice_too_early),
        today=today,
    )


def build_packet(data: PacketData) -> bytes:
    styles = getSampleStyleSheet()
    body, title, heading = styles["BodyText"], styles["Title"], styles["Heading2"]
    body.fontSize, body.leading = 11, 15
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=inch,
        rightMargin=inch,
        topMargin=inch,
        bottomMargin=inch,
    )
    flow = [Paragraph("Request for financial assistance", title), Spacer(1, 12)]
    flow.append(Paragraph(f"Date: {data.today:%B %d, %Y}", body))
    flow.append(Paragraph(f"To: Patient Financial Services, {data.hospital_name}", body))
    for line in data.submit_lines:
        flow.append(Paragraph(line, body))
    flow.append(Spacer(1, 12))
    if data.statement_date:
        opening = (
            "I am writing to apply for financial assistance under your Financial Assistance Policy for the "
            f"statement dated {data.statement_date:%B %d, %Y} (account "
            f"{data.account_reference or 'see enclosed statement'}), amount due ${data.amount_due or 0:,.2f}."
        )
    else:
        opening = (
            "I am writing to apply for financial assistance under your Financial Assistance Policy for the "
            "enclosed statement."
        )
    flow.append(Paragraph(opening, body))
    flow.append(Spacer(1, 8))
    flow.append(
        Paragraph(f"Based on your published policy, I believe I qualify. {data.tier_text}", body)
    )
    for text, quote in data.reasons:
        flow.append(Paragraph(text, body))
        if quote:
            flow.append(Paragraph(f'Your policy states: "{quote}"', body))
    if data.collection_too_early:
        flow.append(Spacer(1, 8))
        flow.append(
            Paragraph(
                "Note: I received a collection notice fewer than 120 days after the first statement. Under "
                "26 CFR 1.501(r)-6, please pause any collection activity while this application is pending.",
                body,
            )
        )
    flow.append(Spacer(1, 8))
    flow.append(Paragraph("Please contact me if you need anything else. Thank you.", body))
    flow.append(Spacer(1, 36))
    flow.append(Paragraph("Signature: ______________________________   Date: ______________", body))
    flow.append(Paragraph(f"Name: {data.patient_name or '______________________________'}", body))
    flow.append(PageBreak())
    flow.append(Paragraph("Application information", heading))
    rows = [
        ("Patient name", data.patient_name or ""),
        ("Account number", data.account_reference or ""),
        ("Statement date", f"{data.statement_date:%m/%d/%Y}" if data.statement_date else ""),
        ("Amount due", f"${data.amount_due:,.2f}" if data.amount_due is not None else ""),
        ("People in household", str(data.household_size or "")),
        (
            "Yearly household income",
            f"${data.annual_income:,.0f}" if data.annual_income is not None else "",
        ),
    ]
    for label, value in rows:
        flow.append(Paragraph(f"<b>{label}:</b> {value}", body))
    if data.form_url:
        flow.append(Spacer(1, 8))
        flow.append(Paragraph(f"The hospital's own application form: {data.form_url}", body))
    flow.append(Paragraph("Documents to enclose", heading))
    for item in data.documents or ["Photo ID", "Proof of income"]:
        flow.append(Paragraph(f"[ ] {item}", body))
    flow.append(Paragraph("[ ] A copy of the hospital statement", body))
    if data.deadlines:
        flow.append(Paragraph("Dates", heading))
        flow.append(Paragraph(f"Apply by: {data.deadlines.application_deadline:%B %d, %Y}", body))
        flow.append(
            Paragraph(
                f"No collection actions allowed before: {data.deadlines.collections_allowed_from:%B %d, %Y}",
                body,
            )
        )
    flow.append(Spacer(1, 12))
    flow.append(
        Paragraph(
            "Prepared with Waive. This is an estimate based on the hospital's published policy; the hospital "
            "makes the final decision. Waive never asks for payment.",
            body,
        )
    )
    document.build(flow)
    return buffer.getvalue()
