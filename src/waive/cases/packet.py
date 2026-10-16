"""The printable packet (spec §9 step 8): the FAP application with cover letter, data sheet and
checklist; or, since task 9.12, a cost-sharing complaint (QMB or Medicaid) or a request to the
payee whose bill the hospital's policy does not cover."""

import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

from waive.atlas.schema import ProcedureSheet
from waive.cases.service import CaseView
from waive.rules.coverage import COST_SHARING_RULE, medicaid_name
from waive.rules.deadlines import ANCHOR_CAVEAT, Deadlines
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


PacketKind = Literal["fap", "cost_sharing", "payee_request"]
# What each letter is called on the form page and in the file name.
PACKET_TITLES: dict[str, str] = {
    "fap": "Request for financial assistance",
    "cost_sharing": "Request to cancel a bill that federal law does not allow",
    "payee_request": "Request for financial assistance",
}
MEDICARE_PHONE = "1-800-MEDICARE (1-800-633-4227)"


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
    kind: PacketKind = "fap"
    # Who the letter goes to when it is not the hospital (payee_request).
    payee_name: str | None = None
    # The state's Medicaid name on the complaint letter (cost_sharing).
    card: str = "Medicaid"

    @property
    def title(self) -> str:
        return PACKET_TITLES[self.kind]


def packet_data(shown: CaseView, sheet: ProcedureSheet | None, today: date) -> PacketData:
    """`sheet` may be None only for a cost-sharing complaint: federal law needs no policy, and
    a hospital without a sheet still gets that letter, addressed as the bill says."""
    kind: PacketKind = "fap"
    if shown.next_step == "cost_sharing":
        kind = "cost_sharing"
    elif shown.next_step == "payee_request":
        kind = "payee_request"
    if sheet is None and kind != "cost_sharing":
        raise ValueError("an application needs the hospital's sheet")
    methods = (
        sheet.apply.submit_methods.value
        if sheet is not None and sheet.apply.submit_methods and kind != "payee_request"
        else []
    )
    if kind == "cost_sharing":
        documents: list[str] = []
    elif sheet is not None and sheet.apply.documents_required:
        documents = [DOC_LABELS.get(d.value, d.value) for d in sheet.apply.documents_required.value]
    else:
        documents = []
    result = shown.result
    bill = shown.bill
    name = shown.hospital_name or (sheet.hospital.name if sheet else None) or "the hospital"
    return PacketData(
        kind=kind,
        payee_name=shown.payee_name,
        card=medicaid_name(shown.state),
        hospital_name=name,
        submit_lines=[f"{METHOD_LABELS[m.kind]}: {m.detail}" for m in methods],
        form_url=sheet.apply.form_url.value if sheet is not None and sheet.apply.form_url else None,
        patient_name=bill.patient_name if bill else None,
        account_reference=bill.account_reference if bill else None,
        amount_due=bill.amount_due if bill else None,
        statement_date=bill.statement_date if bill else None,
        household_size=shown.household_size,
        annual_income=shown.annual_income,
        tier_text=caregiver_summary(result, name).split("\n")[0] if result else "",
        reasons=[(r.text, r.quote or "") for r in (result.reasons if result else [])],
        documents=documents,
        deadlines=shown.deadlines,
        collection_too_early=bool(shown.deadlines and shown.deadlines.collection_notice_too_early),
        today=today,
    )


def _statement_words(data: PacketData) -> str:
    if data.statement_date:
        return (
            f"the statement dated {data.statement_date:%B %d, %Y} (account "
            f"{data.account_reference or 'see enclosed statement'}), amount due "
            f"${data.amount_due or 0:,.2f}"
        )
    return "the enclosed statement"


def _signature(data: PacketData, body) -> list:
    return [
        Spacer(1, 8),
        Paragraph("Please contact me if you need anything else. Thank you.", body),
        Spacer(1, 36),
        Paragraph("Signature: ______________________________   Date: ______________", body),
        Paragraph(f"Name: {data.patient_name or '______________________________'}", body),
    ]


def _footer(body) -> Paragraph:
    return Paragraph(
        "Prepared with Waive. This is an estimate based on the hospital's published policy; the hospital "
        "makes the final decision. Waive never asks for payment.",
        body,
    )


def _cost_sharing_letter(data: PacketData, styles) -> list:
    """To the hospital: the bill is Medicare or Medicaid cost-sharing that federal law forbids
    billing to a QMB or Medicaid enrollee; cancel it, refund, recall, confirm in writing."""
    body, title, heading = styles["BodyText"], styles["Title"], styles["Heading2"]
    flow = [Paragraph(data.title, title), Spacer(1, 12)]
    flow.append(Paragraph(f"Date: {data.today:%B %d, %Y}", body))
    flow.append(Paragraph(f"To: Patient Financial Services, {data.hospital_name}", body))
    for line in data.submit_lines or ["Sent to the address on the bill"]:
        flow.append(Paragraph(line, body))
    flow.append(Spacer(1, 12))
    flow.append(
        Paragraph(
            f"I am writing about {_statement_words(data)}. I am enrolled in {data.card} "
            "(Medicaid) or the Qualified Medicare Beneficiary (QMB) program; a copy of my card "
            "is enclosed.",
            body,
        )
    )
    flow.append(Spacer(1, 8))
    flow.append(
        Paragraph(
            "Federal law does not allow a provider to bill me for Medicare deductibles, "
            "coinsurance or copayments, or for any Medicaid cost-sharing: the Medicare and "
            "Medicaid payments must be accepted as payment in full. "
            f"Authority: {COST_SHARING_RULE.quote}",
            body,
        )
    )
    flow.append(Spacer(1, 8))
    flow.append(Paragraph("Please, within 30 days:", body))
    for item in (
        "cancel the balance on this account and stop sending me statements for it;",
        "refund any amount I have already paid on it;",
        "recall the account from any collection agency and correct any credit reporting;",
        "confirm in writing that the balance is zero.",
    ):
        flow.append(Paragraph(f"- {item}", body))
    flow.append(Spacer(1, 8))
    flow.append(
        Paragraph(
            f"If the bill is not cancelled, I will report it to {MEDICARE_PHONE}, to my state "
            "Medicaid agency and to the Consumer Financial Protection Bureau.",
            body,
        )
    )
    flow.extend(_signature(data, body))
    flow.append(PageBreak())
    flow.append(Paragraph("Put these with the letter", heading))
    flow.append(Paragraph(f"[ ] Copy of your {data.card} or QMB card (front and back)", body))
    flow.append(Paragraph("[ ] A copy of the hospital statement", body))
    flow.append(Spacer(1, 12))
    flow.append(
        Paragraph(
            f"Keep a copy. If nothing changes in 30 days, call {MEDICARE_PHONE} and say you "
            "are a QMB or Medicaid enrollee who was billed cost-sharing.",
            body,
        )
    )
    flow.append(Spacer(1, 12))
    flow.append(
        Paragraph(
            "Prepared with Waive. The hospital decides; this letter states what federal law says. "
            "Waive never asks for payment.",
            body,
        )
    )
    return flow


def _payee_request_letter(data: PacketData, styles) -> list:
    """To the payee the hospital's policy does not cover: ask for its own assistance program."""
    body, title, heading = styles["BodyText"], styles["Title"], styles["Heading2"]
    who = data.payee_name or "the billing office"
    flow = [Paragraph(data.title, title), Spacer(1, 12)]
    flow.append(Paragraph(f"Date: {data.today:%B %d, %Y}", body))
    flow.append(Paragraph(f"To: {who}", body))
    flow.append(Paragraph("Sent to the address on the bill", body))
    flow.append(Spacer(1, 12))
    flow.append(
        Paragraph(
            f"I am writing about {_statement_words(data)}. I cannot afford this bill. Please "
            "send me your financial assistance, charity care or hardship policy and its "
            "application form, or tell me in writing that you have none.",
            body,
        )
    )
    flow.append(Spacer(1, 8))
    flow.append(
        Paragraph(
            "Until you answer, please hold this account: do not send it to a collection agency "
            "and do not report it to a credit bureau.",
            body,
        )
    )
    flow.extend(_signature(data, body))
    flow.append(PageBreak())
    flow.append(Paragraph("Put these with the letter", heading))
    flow.append(Paragraph("[ ] A copy of the statement", body))
    flow.append(
        Paragraph(
            f"[ ] If {data.hospital_name} already gave you financial assistance for the same "
            "visit, a copy of that approval letter",
            body,
        )
    )
    flow.append(Spacer(1, 12))
    flow.append(_footer(body))
    return flow


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
    if data.kind == "cost_sharing":
        document.build(_cost_sharing_letter(data, styles))
        return buffer.getvalue()
    if data.kind == "payee_request":
        document.build(_payee_request_letter(data, styles))
        return buffer.getvalue()
    flow = [Paragraph(data.title, title), Spacer(1, 12)]
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
    flow.extend(_signature(data, body))
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
        if not data.deadlines.anchor_confirmed:
            flow.append(Paragraph(ANCHOR_CAVEAT, body))
    flow.append(Spacer(1, 12))
    flow.append(_footer(body))
    document.build(flow)
    return buffer.getvalue()
