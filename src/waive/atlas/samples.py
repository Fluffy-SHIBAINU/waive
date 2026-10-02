"""A fictional hospital used by tests, demos and UI work. Not a real policy."""

import hashlib
from datetime import date
from decimal import Decimal
from typing import Any

from waive.atlas.schema import (
    Apply,
    Cited,
    Collections,
    Contacts,
    DiscountTier,
    DocType,
    Eligibility,
    HospitalRef,
    ProcedureSheet,
    Programs,
    SheetStatus,
    SourceDoc,
    SourceKind,
    SubmitMethod,
)

SAMPLE_SOURCE_ID = "src-st-example-fap"

SAMPLE_POLICY_TEXT = (
    "St. Example Medical Center Financial Assistance Policy. Effective March 1, 2026.\n"
    "Patients with household income at or below 250% of the Federal Poverty Guidelines "
    "are eligible for free care.\n"
    "Patients with household income above 250% and at or below 400% of the Federal Poverty "
    "Guidelines receive a 60% discount.\n"
    "Patients enrolled in MassHealth or SNAP are presumptively eligible for free care.\n"
    "Applicants must provide a photo ID and one proof of income.\n"
    "Applications may be mailed to Patient Financial Services, 1 Example Way, Boston, MA 02118, "
    "or faxed to 617-555-0199.\n"
    "Applications are accepted up to 240 days after the first post-discharge billing statement.\n"
    "The hospital will not begin extraordinary collection actions before 120 days after the "
    "first post-discharge billing statement.\n"
    "Questions: call 617-555-0100.\n"
)


def st_example_sheet(checked_on: date = date(2026, 10, 2)) -> ProcedureSheet:
    def cite(value: Any, quote: str) -> dict[str, Any]:
        return {
            "value": value,
            "quote": quote,
            "source_id": SAMPLE_SOURCE_ID,
            "checked_on": checked_on,
        }

    return ProcedureSheet(
        hospital=HospitalRef(
            ccn="229999",
            name="St. Example Medical Center",
            city="Boston",
            state="MA",
            zip="02118",
            phone="617-555-0100",
            ownership="Voluntary non-profit - Private",
            website_domain="example.org",
        ),
        version=1,
        status=SheetStatus.PUBLISHED,
        eligibility=Eligibility(
            free_care_max_fpl=Cited[Decimal](
                **cite(
                    Decimal("250"),
                    "household income at or below 250% of the Federal Poverty Guidelines "
                    "are eligible for free care",
                )
            ),
            discount_tiers=Cited[list[DiscountTier]](
                **cite(
                    [
                        DiscountTier(
                            min_fpl_exclusive=Decimal("250"),
                            max_fpl_inclusive=Decimal("400"),
                            discount_percent=60,
                        )
                    ],
                    "above 250% and at or below 400% of the Federal Poverty Guidelines "
                    "receive a 60% discount",
                )
            ),
        ),
        programs=Programs(
            presumptive=Cited[list[str]](
                **cite(
                    ["MassHealth", "SNAP"],
                    "Patients enrolled in MassHealth or SNAP are presumptively eligible "
                    "for free care",
                )
            )
        ),
        apply=Apply(
            documents_required=Cited[list[DocType]](
                **cite(
                    [DocType.PHOTO_ID, DocType.PROOF_OF_INCOME],
                    "Applicants must provide a photo ID and one proof of income",
                )
            ),
            submit_methods=Cited[list[SubmitMethod]](
                **cite(
                    [
                        SubmitMethod(
                            kind="mail",
                            detail="Patient Financial Services, 1 Example Way, Boston, MA 02118",
                        ),
                        SubmitMethod(kind="fax", detail="617-555-0199"),
                    ],
                    "Applications may be mailed to Patient Financial Services, 1 Example Way, "
                    "Boston, MA 02118, or faxed to 617-555-0199",
                )
            ),
            window_days_from_first_bill=Cited[int](
                **cite(
                    240,
                    "Applications are accepted up to 240 days after the first post-discharge "
                    "billing statement",
                )
            ),
        ),
        collections=Collections(
            eca_wait_days=Cited[int](
                **cite(
                    120,
                    "will not begin extraordinary collection actions before 120 days after the "
                    "first post-discharge billing statement",
                )
            )
        ),
        contacts=Contacts(phone=Cited[str](**cite("617-555-0100", "Questions: call 617-555-0100"))),
        sources=[
            SourceDoc(
                id=SAMPLE_SOURCE_ID,
                kind=SourceKind.HOSPITAL_WEB,
                url="https://www.example.org/st-example/financial-assistance.pdf",
                title="Financial Assistance Policy",
                fetched_on=checked_on,
                sha256=hashlib.sha256(SAMPLE_POLICY_TEXT.encode("utf-8")).hexdigest(),
                effective_date=date(2026, 3, 1),
            )
        ],
    )
