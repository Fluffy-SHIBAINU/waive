"""Calendar reminders for check-ins and 501(r) deadlines (spec §9 step 10)."""

import uuid
from datetime import date, timedelta

from waive.rules.deadlines import Deadlines


def reminder_events(
    sent_on: date, deadlines: Deadlines | None, hospital_name: str
) -> list[tuple[date, str, str]]:
    events = [
        (
            sent_on + timedelta(days=14),
            f"Check on the {hospital_name} application",
            "Call the hospital's financial assistance office and ask whether they need anything else.",
        ),
        (
            sent_on + timedelta(days=30),
            f"Follow up with {hospital_name}",
            "Ask for the decision or an update in writing.",
        ),
        (
            sent_on + timedelta(days=45),
            f"Still waiting on {hospital_name}?",
            "If there is no decision yet, ask for the expected date and note who you spoke to.",
        ),
    ]
    if deadlines:
        events.append(
            (
                deadlines.collections_allowed_from - timedelta(days=7),
                f"Collections protection ends soon: {hospital_name}",
                "If the application is not decided, send a written reminder that it is pending.",
            )
        )
        events.append(
            (
                deadlines.application_deadline - timedelta(days=14),
                f"Last chance to apply: {hospital_name}",
                "The 240-day application window closes in two weeks.",
            )
        )
    return events


def build_ics(events: list[tuple[date, str, str]]) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Waive//EN"]
    for day, summary, description in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uuid.uuid4()}@waive",
            f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{description}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
