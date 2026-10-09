from datetime import date

from waive.cases.reminders import build_ics, reminder_events
from waive.rules.deadlines import compute_deadlines


def test_events_cover_checkins_and_deadlines():
    deadlines = compute_deadlines(date(2026, 9, 3), date(2026, 10, 2))
    events = reminder_events(date(2026, 10, 2), deadlines, "St. Example")
    dates = [d for d, _, _ in events]
    assert dates == [
        date(2026, 10, 16),
        date(2026, 11, 1),
        date(2026, 11, 16),
        date(2026, 12, 25),
        date(2027, 4, 17),
    ]
    assert "St. Example" in events[0][1]


def test_provisional_deadlines_say_so_in_the_reminders():
    confirmed = compute_deadlines(date(2026, 9, 3), date(2026, 10, 2))
    provisional = compute_deadlines(date(2026, 9, 3), date(2026, 10, 2), anchor_confirmed=False)
    sure = reminder_events(date(2026, 10, 2), confirmed, "St. Example")
    unsure = reminder_events(date(2026, 10, 2), provisional, "St. Example")
    assert [d for d, _, _ in sure] == [d for d, _, _ in unsure]
    assert all("first bill" not in description for _, _, description in sure)
    assert all("first bill" in description for _, _, description in unsure[-2:])
    assert all("first bill" not in description for _, _, description in unsure[:3])


def test_ics_is_well_formed():
    ics = build_ics([(date(2026, 10, 16), "Check on the application", "Call the hospital")])
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.rstrip().endswith("END:VCALENDAR")
    assert "DTSTART;VALUE=DATE:20261016" in ics and "SUMMARY:Check on the application" in ics
