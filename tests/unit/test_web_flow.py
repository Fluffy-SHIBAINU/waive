"""Phase 9: one link drives the whole case. The full link (/c/{token}) runs every step; the
limited link (/s/{token}) still only adds photos and sees the result."""

import re
from decimal import Decimal

from fastapi.testclient import TestClient

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.cases.service import start_case
from waive.db import CaseRow, session_scope
from waive.web.deps import TEMPLATES_DIR
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


def to_full_result(client, links):
    base = f"/c/{links.caregiver_token}"
    client.post(f"{base}/bill", files=photo())
    client.post(f"{base}/confirm", data={"answer": "yes"})
    client.post(f"{base}/household", data={"size": "1", "programs": "none"})
    return base, client.post(f"{base}/income", files=photo(), follow_redirects=True)


def test_result_then_check_then_approve_then_the_form_in_that_order():
    client, links = seeded_client()
    base, result = to_full_result(client, links)
    # Screen 6 to 7: one big button, and the hospital's own rule quoted under the answer.
    assert f'<a href="{base}" class="btn primary">Check what we read</a>' in result.text
    assert result.text.count('class="btn primary"') == 1
    assert "at or below 250% of the Federal Poverty Guidelines" in result.text
    assert "helper" not in result.text.lower()

    check = client.get(base)
    assert check.status_code == 200 and "<h1>Check what we read</h1>" in check.text
    for read in (
        "ST. EXAMPLE MEDICAL CENTER",
        "$1,850.00",
        "September 3, 2026",
        "People in your home",
        "$22,800.00",
        "Likely free care",
        "May 1, 2027",  # the date that matters: apply by
    ):
        assert read in check.text, read
    assert "Fix something" in check.text
    assert f'<form method="post" action="{base}/approve"' in check.text
    assert "Looks right, make my form" in check.text

    # Screen 7 to 8.
    approved = client.post(f"{base}/approve", follow_redirects=False)
    assert approved.status_code == 303 and approved.headers["location"] == f"{base}/form"
    form = client.get(f"{base}/form")
    assert form.status_code == 200 and "<h1>Your form and reminders</h1>" in form.text
    assert f'href="{base}/packet.pdf"' in form.text
    assert f'href="{base}/reminders.ics"' in form.text
    assert "Mail: Patient Financial Services, 1 Example Way, Boston, MA 02118" in form.text
    assert "Fax: 617-555-0199" in form.text
    assert "Photo ID" in form.text and "Proof of income" in form.text
    assert "May 1, 2027" in form.text
    assert form.headers["cache-control"] == "no-store"
    assert client.get(f"{base}/packet.pdf").content[:5] == b"%PDF-"
    assert "BEGIN:VCALENDAR" in client.get(f"{base}/reminders.ics").text


def test_fix_something_returns_to_the_check_page_with_the_new_values():
    client, links = seeded_client()
    base, _ = to_full_result(client, links)
    assert '<details id="fix">' in client.get(base).text
    assert '<details id="fix" open>' in client.get(f"{base}?fix=1").text
    fixed = client.post(
        f"{base}/correct",
        data={"amount_due": "900", "size": "3", "annual_income": "90000"},
        follow_redirects=False,
    )
    assert fixed.status_code == 303 and fixed.headers["location"] == base
    check = client.get(base).text
    assert "<h1>Check what we read</h1>" in check
    assert "$900.00" in check and "$1,850.00" not in check
    assert "$90,000.00" in check and "Likely free care" not in check
    assert "Looks right, make my form" in check


def test_approving_too_early_or_twice_changes_nothing():
    client, links = seeded_client()
    base = f"/c/{links.caregiver_token}"
    # Too early: nothing was read, so there is no form to make.
    early = client.post(f"{base}/approve", follow_redirects=False)
    assert early.status_code == 303 and early.headers["location"] == base
    assert status_of(client, links.case_id) == "new"
    no_form = client.get(f"{base}/form", follow_redirects=False)
    assert no_form.status_code == 303 and no_form.headers["location"] == base
    new = client.get(base).text
    assert "Looks right, make my form" not in new
    assert f'<a href="{base}/bill" class="btn primary">Take a photo of the bill</a>' in new

    to_full_result(client, links)
    for _ in range(2):
        again = client.post(f"{base}/approve", follow_redirects=False)
        assert again.status_code == 303 and again.headers["location"] == f"{base}/form"
        assert status_of(client, links.case_id) == "approved"
    # Once approved, the check page and the result both lead to the form instead.
    check = client.get(base).text
    assert "Looks right, make my form" not in check and f'action="{base}/approve"' not in check
    assert f'<a href="{base}/form" class="btn primary">Your form and reminders</a>' in check
    result = client.get(f"{base}/result").text
    assert f'<a href="{base}/form" class="btn primary">Your form and reminders</a>' in result
    assert "Check what we read" not in result


def test_the_check_page_asks_for_what_is_still_missing_before_the_form():
    client, links = seeded_client()
    base = f"/c/{links.caregiver_token}"
    client.post(f"{base}/bill", files=photo())
    client.post(f"{base}/confirm", data={"answer": "yes"})
    # Bill confirmed, the two questions not answered yet.
    check = client.get(base).text
    assert "More information needed" in check and "household income" in check
    assert f'<a href="{base}/household" class="btn primary">' in check
    assert "Looks right, make my form" not in check
    client.post(f"{base}/household", data={"size": "1", "programs": "none"})
    client.post(f"{base}/income", data={"skip": "1"})
    # Household answered, income skipped: straight to the income step.
    result = client.get(f"{base}/result").text
    assert "helper" not in result.lower() and "next page" in result
    check = client.get(base).text
    assert f'<a href="{base}/income" class="btn primary">' in check
    assert "Looks right, make my form" not in check


def test_the_limited_link_never_sees_the_check_or_form_steps():
    client, links = seeded_client()
    base, _ = to_full_result(client, links)
    limited = f"/s/{links.senior_token}"
    before = client.get(f"{limited}/result").text
    client.post(f"{base}/approve")
    after = client.get(f"{limited}/result").text
    for page in (before, after):
        assert "likely do not have to pay" in page and "Your helper will check this" in page
        assert "Check what we read" not in page and "Your form and reminders" not in page
        assert "/c/" not in page and "Federal Poverty Guidelines" not in page
    assert client.get(f"/c/{links.senior_token}/form").status_code == 403


BAR = 'aria-label="Your case"'


def share_path(text):
    """The photo-only link the case bar offers to send to someone else."""
    return re.search(r'id="share-link" data-link="(/s/[^"]+)"', text).group(1)


def test_every_page_under_the_full_link_has_the_case_bar():
    client, links = seeded_client()
    base = f"/c/{links.caregiver_token}"
    pages = {"photo": client.get(f"{base}/bill")}
    pages["read-back"] = client.post(f"{base}/bill", files=photo())
    pages["household"] = client.post(
        f"{base}/confirm", data={"answer": "yes"}, follow_redirects=True
    )
    pages["income"] = client.post(
        f"{base}/household", data={"size": "1", "programs": "none"}, follow_redirects=True
    )
    pages["could not read"] = client.post(f"{base}/income", data={"monthly": "lots"})
    pages["result"] = client.post(f"{base}/income", files=photo(), follow_redirects=True)
    pages["check"] = client.get(base)
    pages["form"] = client.post(f"{base}/approve", follow_redirects=True)
    assert str(pages["form"].url).endswith("/form")
    for name, page in pages.items():
        text = page.text
        assert page.status_code == 200 and text.count(BAR) == 1, name
        # Copy my link: a button, and the link itself as text that can be selected without it.
        assert "Copy my link" in text and 'data-copy="my-link"' in text, name
        assert f'id="my-link" data-link="{base}">http://testserver{base}</p>' in text, name
        # Send the photo step to someone else: the limited link and what it can and cannot do.
        assert "Send the photo step to someone else" in text, name
        shared = share_path(text)
        assert f'data-link="{shared}">http://testserver{shared}</p>' in text, name
        assert 'data-share="share-link" hidden' in text and 'data-copy="share-link"' in text
        assert "only lets someone add photos and see the result" in text, name
        assert "cannot check, approve, open the form or delete" in text, name
        assert f'action="{base}/relink"' in text and "Make new links" in text, name
        # Delete my case: always there, and it asks before it deletes.
        assert "Delete my case" in text and f'action="{base}/delete"' in text, name
        assert text.count('<script src="/static/case.js"></script>') == 1, name
        assert "onsubmit=" not in text and "onclick=" not in text, name
    script = client.get("/static/case.js")
    assert script.status_code == 200
    assert "navigator.clipboard" in script.text and "navigator.share" in script.text
    css = client.get("/static/waive.css").text
    assert ".casebar" in css and "user-select: all" in css


def test_pages_reached_without_the_full_link_have_no_case_bar():
    client, links = seeded_client()
    limited = f"/s/{links.senior_token}"
    pages = [client.get(path) for path in ("/", "/atlas", "/atlas/229999", limited)]
    pages.append(client.get(f"{limited}/bill"))
    pages.append(client.post(f"{limited}/bill", files=photo()))
    pages.append(client.post(f"{limited}/confirm", data={"answer": "yes"}, follow_redirects=True))
    pages.append(
        client.post(
            f"{limited}/household", data={"size": "1", "programs": "none"}, follow_redirects=True
        )
    )
    pages.append(client.post(f"{limited}/income", files=photo(), follow_redirects=True))
    pages.append(client.get("/c/not-a-real-token"))  # the 403 page
    for page in pages:
        for control in (BAR, "Copy my link", "Send the photo step", "Delete my case", "case.js"):
            assert control not in page.text, (str(page.url), control)
        assert links.caregiver_token not in page.text, str(page.url)
    # The script is a file of its own: the page skeleton carries no inline script.
    skeleton = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
    assert "<script>" not in skeleton and "/static/case.js" in skeleton


def test_the_photo_step_link_from_the_bar_only_adds_photos_and_sees_the_result():
    client, links = seeded_client()
    base = f"/c/{links.caregiver_token}"
    shared = share_path(client.get(f"{base}/bill").text)
    token = shared.removeprefix("/s/")
    assert token not in (links.caregiver_token, "")
    claims = client.app.state.deps.signer.verify(token)
    assert (claims.case_id, claims.scope) == (links.case_id, "senior")
    start = client.get(shared)
    assert start.status_code == 200 and "Take a photo of the bill" in start.text
    assert BAR not in start.text and "/c/" not in start.text
    # The other person adds the photo; it shows up under the full link.
    assert client.post(f"{shared}/bill", files=photo()).status_code == 200
    assert "$1,850.00" in client.get(base).text
    refused = {
        "check": client.get(f"/c/{token}"),
        "approve": client.post(f"/c/{token}/approve"),
        "form": client.get(f"/c/{token}/form"),
        "packet": client.get(f"/c/{token}/packet.pdf"),
        "relink": client.post(f"/c/{token}/relink"),
        "delete": client.post(f"/c/{token}/delete"),
    }
    for name, response in refused.items():
        assert response.status_code == 403, name
    assert status_of(client, links.case_id) == "bill_read"


def test_make_new_links_from_the_bar_revokes_the_link_that_was_sent_too():
    client, links = seeded_client()
    base = f"/c/{links.caregiver_token}"
    client.post(f"{base}/bill", files=photo())
    shared = share_path(client.get(base).text)
    page = client.post(f"{base}/relink")
    assert page.status_code == 200 and "old links stopped working" in page.text
    assert page.headers["cache-control"] == "no-store"
    new_base = re.search(
        r'<a href="(/c/[^"]+)" class="btn primary">Go on with my case</a>', page.text
    ).group(1)
    new_shared = share_path(page.text)
    assert f'id="my-link" data-link="{new_base}">http://testserver{new_base}</p>' in page.text
    assert "Copy my link" in page.text and 'data-copy="share-link"' in page.text
    assert page.text.count('<script src="/static/case.js"></script>') == 1
    for old in (base, f"{base}/bill", shared, f"/s/{links.senior_token}"):
        assert client.get(old).status_code == 403, old
    check = client.get(new_base)
    assert check.status_code == 200 and "$1,850.00" in check.text  # the case itself survived
    assert f'id="my-link" data-link="{new_base}"' in check.text
    assert client.get(new_shared).status_code == 200
    assert client.get(share_path(check.text)).status_code == 200


def test_delete_my_case_from_the_bar_asks_first_and_then_removes_everything():
    client, links = seeded_client()
    base = f"/c/{links.caregiver_token}"
    client.post(f"{base}/bill", files=photo())
    text = client.get(f"{base}/household").text
    # The button only opens the question; the button under it does the deleting.
    assert re.search(
        r'<details>\s*<summary class="btn danger">Delete my case</summary>.*?'
        rf'<form method="post" action="{re.escape(base)}/delete"[^>]*>.*?'
        r'<button type="submit" class="btn danger">Yes, delete everything</button>',
        text,
        re.DOTALL,
    )
    assert "cannot be undone" in text
    gone = client.post(f"{base}/delete")
    assert gone.status_code == 200 and "deleted" in gone.text.lower()
    assert gone.headers["clear-site-data"] == '"cache"' and BAR not in gone.text
    assert client.get(base).status_code == 403
    with session_scope(client.app.state.deps.engine) as session:
        assert session.get(CaseRow, links.case_id) is None
