"""Fictional demo data so the app can be tried without keys or real hospitals, and a reset that
puts the local database and the demo images back to the demo script's starting point."""

import json
import random
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.cases.synth import BillTruth, render_benefit_letter, render_bill
from waive.db import (
    CaseRow,
    ContributionRow,
    ReportedEvidenceRow,
    ReviewItemRow,
    SheetRow,
    SourceDocRow,
    hospital_sources,
)

DEMO_DIR = Path("var/demo")

# The spec's persona (§2): Rosa, 74, one-person household, $1,900 a month from Social Security,
# an $1,850 bill after an ER visit. St. Example gives free care up to 250% FPL, so the answer is
# "You likely do not have to pay this bill." Everything here is fictional.
DEMO_BILL = BillTruth(
    hospital_name="St. Example Medical Center",
    hospital_phone="617-555-0100",
    fap_phone="617-555-0100",
    fap_url="www.example.org/financial-assistance",
    statement_date=date(2026, 9, 3),
    account_reference="ACCT-20260903",
    patient_name="Rosa Alvarez",
    amount_due=Decimal("1850.00"),
    collection_notice=False,
)
DEMO_MONTHLY_BENEFIT = Decimal("1900.00")
DEMO_LETTER_DATE = date(2026, 1, 10)


def seed_demo(session: Session) -> None:
    sheet = st_example_sheet()
    h = sheet.hospital
    repo.upsert_hospital(
        session,
        {
            "ccn": h.ccn,
            "name": h.name.upper(),
            "address": "1 EXAMPLE WAY",
            "city": h.city.upper(),
            "state": h.state,
            "zip": h.zip,
            "phone": h.phone,
            "hospital_type": "Acute Care Hospitals",
            "ownership": h.ownership,
            "website_domain": h.website_domain,
        },
    )
    publish_sheet(session, sheet)


def forget_cases(session: Session) -> int:
    return session.execute(delete(CaseRow)).rowcount


def write_demo_images(out_dir: Path) -> list[Path]:
    """The demo bill (JPEG + ground truth) and the benefit letter. Deterministic, so re-running
    the reset never changes what the camera sees."""
    out_dir.mkdir(parents=True, exist_ok=True)
    bill = out_dir / "bill.jpg"
    bill.write_bytes(
        render_bill(DEMO_BILL, random.Random(2026), layout=0, rotate_deg=0.0, blur=0.0)
    )
    truth = out_dir / "bill.json"
    truth.write_text(
        json.dumps(DEMO_BILL.model_dump(mode="json"), indent=1, sort_keys=True), encoding="utf-8"
    )
    letter = out_dir / "letter.jpg"
    letter.write_bytes(
        render_benefit_letter(DEMO_BILL.patient_name, DEMO_MONTHLY_BENEFIT, DEMO_LETTER_DATE)
    )
    return [bill, truth, letter]


@dataclass
class ResetReport:
    cases_deleted: int
    review_items_deleted: int
    contributions_deleted: int
    evidence_deleted: int
    sources_unlinked: int
    documents_deleted: int
    sheet_versions_deleted: int
    sheet_version: int
    files: list[Path] = field(default_factory=list)


def reset_demo(
    session: Session, out_dir: Path = DEMO_DIR, *, write_files: bool = True
) -> ResetReport:
    """Back to the demo script's starting point: every case is deleted (cases are personal
    data), the demo hospital's learning rows, source links, demo-only documents and sheet
    versions are removed and St. Example is re-seeded at version 1, and the demo images are
    rewritten. Real hospitals' sheets, documents and review items are untouched."""
    demo = list(repo.DEMO_CCNS)
    cases = forget_cases(session)
    items = session.execute(delete(ReviewItemRow).where(ReviewItemRow.ccn.in_(demo))).rowcount
    contributions = session.execute(
        delete(ContributionRow).where(ContributionRow.ccn.in_(demo))
    ).rowcount
    evidence = session.execute(
        delete(ReportedEvidenceRow).where(ReportedEvidenceRow.ccn.in_(demo))
    ).rowcount
    demo_sources = set(
        session.scalars(
            select(hospital_sources.c.source_id).where(hospital_sources.c.ccn.in_(demo))
        )
    )
    unlinked = session.execute(
        delete(hospital_sources).where(hospital_sources.c.ccn.in_(demo))
    ).rowcount
    # Documents only the demo hospital held (an approved demo-run patient photo's screened text,
    # say) would otherwise survive as orphans. Documents still linked to a real hospital stay, and
    # so do state overlay documents, which are shared across hospitals. Real hospitals' own
    # unlinked documents are never touched: older sheet versions cite them.
    documents = 0
    if demo_sources:
        documents = session.execute(
            delete(SourceDocRow).where(
                SourceDocRow.id.in_(demo_sources),
                SourceDocRow.id.not_in(select(hospital_sources.c.source_id)),
                SourceDocRow.id.not_like("state-%"),
            )
        ).rowcount
    versions = session.execute(delete(SheetRow).where(SheetRow.ccn.in_(demo))).rowcount
    session.flush()
    seed_demo(session)
    session.flush()
    latest = repo.latest_sheet(session, demo[0])
    version = latest[1].version if latest else 0
    files = write_demo_images(out_dir) if write_files else []
    return ResetReport(
        cases, items, contributions, evidence, unlinked, documents, versions, version, files
    )
