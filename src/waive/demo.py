"""Fictional demo data so the app can be tried without keys or real hospitals."""

from sqlalchemy import delete
from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.db import CaseRow


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
