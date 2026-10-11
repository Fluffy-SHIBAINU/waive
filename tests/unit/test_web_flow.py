"""Phase 9: one link drives the whole case. The full link (/c/{token}) runs every step; the
limited link (/s/{token}) still only adds photos and sees the result."""

from decimal import Decimal

from fastapi.testclient import TestClient

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.cases.service import start_case
from waive.db import CaseRow, session_scope
from waive.web.routes_senior import to_decimal

from tests.unit.test_web_app import make_client
from tests.unit.test_web_senior import HOSPITAL, UnreadableAI, approved_case, photo, seeded_client


def sealed_of(client, case_id):
    deps = client.app.state.deps
    with session_scope(deps.engine) as session:
        return deps.cipher.decrypt(session.get(CaseRow, case_id).sealed)


def status_of(client, case_id):
    with session_scope(client.app.state.deps.engine) as session:
        return session.get(CaseRow, case_id).status


def test_every_step_from_photo_to_result_works_under_the_full_link():
    client, links = seeded_client()
    base = f"/c/{links.caregiver_token}"
    pages = []

    start = client.get(f"{base}/bill")
    assert start.status_code == 200
    assert "Take a photo of the bill" in start.text and 'capture="environment"' in start.text
    assert f'action="{base}/bill"' in start.text
    pages.append(start)

    readback = client.post(f"{base}/bill", files=photo())
    assert readback.status_code == 200
    assert "ST. EXAMPLE MEDICAL CENTER" in readback.text and "$1,850.00" in readback.text
    assert f'action="{base}/confirm"' in readback.text
    assert "No, fix it" in readback.text
    pages.append(readback)

    household = client.post(f"{base}/confirm", data={"answer": "yes"}, follow_redirects=True)
    assert str(household.url).endswith(f"{base}/household")
    assert "How many people" in household.text and f'action="{base}/household"' in household.text
    pages.append(household)

    income = client.post(
        f"{base}/household", data={"size": "1", "programs": "none"}, follow_redirects=True
    )
    assert str(income.url).endswith(f"{base}/income")
    assert "Social Security" in income.text and f'action="{base}/income"' in income.text
    pages.append(income)

    result = client.post(f"{base}/income", files=photo(), follow_redirects=True)
    assert result.status_code == 200 and str(result.url).endswith(f"{base}/result")
    assert "likely do not have to pay" in result.text and "Read this to me" in result.text
    pages.append(result)

    # One link: no step sends the person to the limited link's pages.
    for page in pages:
        assert 'action="/s/' not in page.text and 'href="/s/' not in page.text, page.url
    assert status_of(client, links.case_id) == "evaluated"


def test_no_fix_it_under_the_full_link_opens_the_fix_form_instead_of_waiting_for_a_helper():
    client, links = seeded_client()
    base = f"/c/{links.caregiver_token}"
    client.post(f"{base}/bill", files=photo())
    answer = client.post(f"{base}/confirm", data={"answer": "no"}, follow_redirects=False)
    assert answer.status_code == 303
    assert answer.headers["location"] == f"{base}?fix=1#fix"
    review = client.get(f"{base}?fix=1")
    assert review.status_code == 200 and 'id="fix"' in review.text
    assert 'name="amount_due"' in review.text
    # The limited link cannot fix anything, so it still hands over to the helper.
    wait = client.post(f"/s/{links.senior_token}/confirm", data={"answer": "no"})
    assert wait.status_code == 200 and "helper" in wait.text.lower()


def test_the_full_link_can_type_the_monthly_income_instead_of_a_photo():
    client, links = seeded_client()
    base = f"/c/{links.caregiver_token}"
    client.post(f"{base}/bill", files=photo())
    client.post(f"{base}/confirm", data={"answer": "yes"})
    client.post(f"{base}/household", data={"size": "2", "programs": "SNAP"})
    form = client.get(f"{base}/income")
    assert 'name="monthly"' in form.text and "My helper will fill it in" not in form.text

    bad = client.post(f"{base}/income", data={"monthly": "about two thousand"})
    assert bad.status_code == 200 and "as a number" in bad.text
    assert sealed_of(client, links.case_id)["household"]["annual_income"] is None

    result = client.post(f"{base}/income", data={"monthly": "$1,900"}, follow_redirects=True)
    assert str(result.url).endswith(f"{base}/result")
    assert "likely do not have to pay" in result.text
    # The size and the programs answered one step earlier are kept.
    assert sealed_of(client, links.case_id)["household"] == {
        "size": 2,
        "annual_income": "22800",
        "programs": ["SNAP"],
    }


def test_the_limited_link_cannot_type_an_income():
    client, links = seeded_client()
    base = f"/s/{links.senior_token}"
    client.post(f"{base}/bill", files=photo())
    client.post(f"{base}/confirm", data={"answer": "yes"})
    client.post(f"{base}/household", data={"size": "1", "programs": "none"})
    form = client.get(f"{base}/income")
    assert 'name="monthly"' not in form.text and "My helper will fill it in" in form.text
    client.post(f"{base}/income", data={"monthly": "1900"})
    assert sealed_of(client, links.case_id)["household"]["annual_income"] is None


def test_a_step_that_cannot_finish_under_the_full_link_points_at_the_check_page_not_a_helper():
    client, engine = make_client(ai=UnreadableAI())
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, st_example_sheet())
        links = start_case(client.app.state.deps.context(session), "MA")
    client = TestClient(client.app, raise_server_exceptions=False)
    base = f"/c/{links.caregiver_token}"
    unread = client.post(f"{base}/bill", files=photo())
    assert unread.status_code == 200
    assert "helper" not in unread.text.lower()
    assert "could not read" in unread.text.lower()
    assert f'href="{base}"' in unread.text and "Check what we read" in unread.text
    assert f'href="{base}/bill"' in unread.text  # start again with a new photo
    # Nothing was read, so there is no result yet: the result step says so the same way.
    unfinished = client.get(f"{base}/result")
    assert unfinished.status_code == 200 and "helper" not in unfinished.text.lower()
    assert f'href="{base}"' in unfinished.text

    off, _ = make_client()
    off.app.state.deps.ai = None  # no reader configured on this machine
    with session_scope(off.app.state.deps.engine) as session:
        links = start_case(off.app.state.deps.context(session), "MA")
    switched_off = off.post(f"/c/{links.caregiver_token}/bill", files=photo())
    assert switched_off.status_code == 200 and "switched off" in switched_off.text
    assert "helper" not in switched_off.text.lower()
    # The limited link keeps its own words.
    limited = off.post(f"/s/{links.senior_token}/bill", files=photo())
    assert "reading is switched off right now" in limited.text and "helper" in limited.text


def test_the_limited_link_is_refused_on_everything_but_photos_and_the_result():
    client, links, caregiver = approved_case()
    limited = links.senior_token
    refused = {
        "review": client.get(f"/c/{limited}"),
        "correct": client.post(f"/c/{limited}/correct", data={"size": "4"}),
        "approve": client.post(f"/c/{limited}/approve"),
        "packet": client.get(f"/c/{limited}/packet.pdf"),
        "reminders": client.get(f"/c/{limited}/reminders.ics"),
        "relink": client.post(f"/c/{limited}/relink"),
        "outcome": client.post(f"/c/{limited}/outcome", data={"decision": "approved"}),
        "check-in": client.post(f"/c/{limited}/check-in", data={"day": "14"}),
        "paper": client.post(f"/c/{limited}/paper", files=photo()),
        # The steps under the full link's address belong to the full link too.
        "bill page": client.get(f"/c/{limited}/bill"),
        "bill": client.post(f"/c/{limited}/bill", files=photo()),
        "confirm": client.post(f"/c/{limited}/confirm", data={"answer": "yes"}),
        "household page": client.get(f"/c/{limited}/household"),
        "household": client.post(f"/c/{limited}/household", data={"size": "4"}),
        "income page": client.get(f"/c/{limited}/income"),
        "income": client.post(f"/c/{limited}/income", data={"monthly": "1"}),
        "result": client.get(f"/c/{limited}/result"),
        "skip": client.post(f"/c/{limited}/paper/skip"),
        "delete": client.post(f"/c/{limited}/delete"),
    }
    for name, response in refused.items():
        assert response.status_code == 403, name
        assert "not valid" in response.text, name
    # Nothing moved: the case is still there, approved, with the helper's numbers.
    assert status_of(client, links.case_id) == "approved"
    assert sealed_of(client, links.case_id)["household"]["annual_income"] == "22800"
    assert client.get(f"{caregiver}/packet.pdf").status_code == 200
    # What the limited link is for still works.
    result = client.get(f"/s/{limited}/result")
    assert result.status_code == 200 and "likely do not have to pay" in result.text


def test_the_limited_link_keeps_its_own_pages_without_the_full_links_controls():
    client, links = seeded_client()
    base = f"/s/{links.senior_token}"
    pages = [client.get(base), client.get(f"{base}/bill")]
    pages.append(client.post(f"{base}/bill", files=photo()))
    pages.append(client.post(f"{base}/confirm", data={"answer": "yes"}, follow_redirects=True))
    pages.append(
        client.post(
            f"{base}/household", data={"size": "1", "programs": "none"}, follow_redirects=True
        )
    )
    pages.append(client.post(f"{base}/income", files=photo(), follow_redirects=True))
    for page in pages:
        assert page.status_code == 200, page.url
        assert "/c/" not in page.text, page.url
        assert links.caregiver_token not in page.text, page.url
        for control in ("Check what we read", "Approve", "Delete", "No, fix it"):
            assert control not in page.text, (page.url, control)
    assert "Something is wrong" in pages[2].text  # the read-back keeps its own second button
    assert "Your helper will check this" in pages[-1].text


def test_the_full_links_steps_lead_to_the_result_once_the_case_is_approved():
    """The lock on an approved case holds on both links; the correction form on the check page
    stays the one way to change it."""
    client, links, caregiver = approved_case()
    attempts = (
        lambda: client.get(f"{caregiver}/bill"),
        lambda: client.post(f"{caregiver}/bill", files=photo()),
        lambda: client.post(f"{caregiver}/confirm", data={"answer": "yes"}),
        lambda: client.get(f"{caregiver}/household"),
        lambda: client.post(f"{caregiver}/household", data={"size": "4", "programs": "none"}),
        lambda: client.get(f"{caregiver}/income"),
        lambda: client.post(f"{caregiver}/income", data={"monthly": "9000"}),
    )
    for attempt in attempts:
        response = attempt()
        assert response.status_code == 200 and str(response.url).endswith(f"{caregiver}/result")
        assert "likely do not have to pay" in response.text
        assert status_of(client, links.case_id) == "approved"
        assert sealed_of(client, links.case_id)["household"] == {
            "size": 1,
            "annual_income": "22800",
            "programs": [],
        }


def test_a_typed_amount_must_be_a_plain_number():
    assert to_decimal("$1,900.50") == Decimal("1900.50")
    # Decimal() itself accepts "NaN" and "Infinity"; neither may reach the sealed household.
    for text in ("", "  ", "lots", "NaN", "Infinity", "-inf", "1e9999999"):
        assert to_decimal(text) is None, text
    client, links = seeded_client()
    base = f"/c/{links.caregiver_token}"
    client.post(f"{base}/bill", files=photo())
    client.post(f"{base}/confirm", data={"answer": "yes"})
    for text in ("NaN", "Infinity", "-12"):
        refused = client.post(f"{base}/income", data={"monthly": text})
        assert refused.status_code == 200 and "as a number" in refused.text, text
    assert "household" not in sealed_of(client, links.case_id)
