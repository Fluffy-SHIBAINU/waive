"""Phase 9, task 9.8: the home page and the history for whoever signed in. Google is played by
respx (see test_oidc); nothing here reaches the network."""

import re
from datetime import UTC, datetime, timedelta

import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.auth import sessions
from waive.cases.service import authorize, start_case
from waive.db import AccountCaseRow, AccountRow, CaseRow, SessionRow, session_scope
from waive.web.signin import SESSION_COOKIE

from tests.unit.test_web_app import make_client
from tests.unit.test_web_auth import rows, set_cookies, sign_in, signin_client
from tests.unit.test_web_senior import HOSPITAL, FakeAI, photo

# The home page between <main> and </main> as it was before sign-in existed.
HOME_AS_BEFORE = """

<h1>Hospital bills you may not have to pay</h1>

<p class="lead">Nonprofit hospitals must offer free or discounted care to people with lower incomes. Waive checks a bill against the hospital's own policy and prepares the application.</p>
<form method="post" action="/cases" class="stack">
  <label for="state">Which state is the hospital in?</label>
  <select id="state" name="state" required>
    <option value="MA" selected>Massachusetts</option>
  </select>
  <button type="submit" class="btn primary">Start a case</button>
</form>
<p><a href="/atlas">Browse hospital policies</a></p>


"""
SIGN_IN_WORDS = ("Sign in", "Sign out", "Your cases", "/auth/", "/account", "Google", "Saved to")
PRIVATE = {
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "x-robots-tag": "noindex, nofollow",
}


def seeded(**overrides):
    """An app with sign-in on, the example hospital and its sheet. Returns (client, engine)."""
    client, engine = signin_client(ai=FakeAI(), **overrides)
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, st_example_sheet())
    return client, engine


def main_of(page):
    return page.text.split('<main class="page">', 1)[1].split("</main>", 1)[0]


def waive_text(page) -> str:
    """The page without the footer's Kindred ID sign-out form, which is on every page whatever
    Waive's own sign-in settings say (web/kindred_auth.py)."""
    head, _, rest = page.text.partition('<form class="kindred"')
    return head + rest.partition("</form>")[2]


def begin(client):
    """Press "Start a case"; returns the full link's address (/c/{token})."""
    created = client.post("/cases", data={"state": "MA"}, follow_redirects=False)
    assert created.status_code == 303
    return created.headers["location"].removesuffix("/bill")


def finish(client, base):
    """Walk a case from the photo to its result."""
    client.post(f"{base}/bill", files=photo())
    client.post(f"{base}/confirm", data={"answer": "yes"})
    client.post(f"{base}/household", data={"size": "1", "programs": "none"})
    result = client.post(f"{base}/income", data={"monthly": "1900"}, follow_redirects=True)
    assert str(result.url).endswith("/result") and "likely do not have to pay" in result.text
    return result


def case_id_of(client, base):
    return client.app.state.deps.signer.verify(base.removeprefix("/c/")).case_id


def opened_from_history(page):
    return re.findall(r'<a href="(/account/cases/[0-9a-f]{32})" class="btn">', page.text)


def is_private(response):
    return all(response.headers.get(name) == value for name, value in PRIVATE.items())


def test_without_google_settings_the_home_page_is_exactly_as_it_was():
    client, _ = make_client()
    page = client.get("/")
    assert main_of(page) == HOME_AS_BEFORE
    # A cookie left over from when sign-in was on changes nothing and is not even looked up.
    stale = TestClient(client.app, cookies={SESSION_COOKIE: "x" * 43})
    assert stale.get("/").text == page.text
    assert "x-robots-tag" not in page.headers and page.headers.get("cache-control") != "no-store"
    assert "vary" not in page.headers and "set-cookie" not in page.headers


def test_without_google_settings_nothing_about_sign_in_is_rendered_anywhere():
    client, engine = make_client(ai=FakeAI())
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, st_example_sheet())
    base = begin(client)
    pages = [client.get("/"), client.get(f"{base}/bill"), finish(client, base), client.get(base)]
    pages.append(client.post(f"{base}/approve", follow_redirects=True))
    pages.append(client.post(f"{base}/delete"))
    for page in pages:
        assert page.status_code == 200
        for word in SIGN_IN_WORDS:
            assert word not in waive_text(page), (str(page.url), word)
    # The pages that only exist for someone signed in are not there at all.
    assert client.get("/account", follow_redirects=False).status_code == 404
    assert client.get("/account/cases/" + "a" * 32, follow_redirects=False).status_code == 404
    assert client.post("/account/delete", follow_redirects=False).status_code == 404
    assert client.post(f"{base}/save", follow_redirects=False).status_code in (403, 404)
    assert rows(engine, AccountRow) == [] and rows(engine, AccountCaseRow) == []


def test_with_google_settings_the_home_page_offers_sign_in_below_start():
    client, _ = signin_client()
    page = client.get("/")
    text = waive_text(page)
    button = '<a href="/auth/google/start" class="btn">Sign in with Google</a>'
    assert button in text
    assert text.index("Start a case") < text.index(button) < text.index("/atlas")
    assert "Your cases" not in text and "Sign out" not in text and "/account" not in text
    assert "<script" not in text  # sign-in needs no script
    # Nobody's data is on it: the page stays public, cacheable and findable. It differs by
    # cookie, though, and says so to any cache on the way.
    assert "x-robots-tag" not in page.headers and page.headers.get("cache-control") != "no-store"
    assert page.headers["vary"] == "Cookie"
    # A session that is unknown or ran out is the same as none.
    stranger = TestClient(client.app, cookies={SESSION_COOKIE: "x" * 43})
    assert button in stranger.get("/").text


@respx.mock
def test_signed_in_the_home_page_lists_your_cases_newest_first_and_offers_sign_out():
    client, engine = seeded()
    sign_in(client)
    empty = client.get("/")
    assert "Your cases" in empty.text and "no saved cases yet" in empty.text
    assert "Sign in with Google" not in empty.text

    older = begin(client)
    finish(client, older)
    newer = begin(client)
    with session_scope(engine) as session:
        session.get(CaseRow, case_id_of(client, older)).created_at = datetime(
            2026, 9, 3, 15, 0, tzinfo=UTC
        )
        session.get(CaseRow, case_id_of(client, newer)).created_at = datetime(
            2026, 10, 9, 15, 0, tzinfo=UTC
        )
    page = client.get("/")
    text = page.text
    assert text.index("Start a case") < text.index("Your cases") < text.index("/atlas")
    # Newest first: the day it started, the hospital once a bill was read, how far it got.
    assert opened_from_history(page) == [
        f"/account/cases/{case_id_of(client, newer)}",
        f"/account/cases/{case_id_of(client, older)}",
    ]
    assert text.index("October 9, 2026") < text.index("September 3, 2026")
    assert "ST. EXAMPLE MEDICAL CENTER" in text
    assert "Waiting for a photo of the bill" in text and "Result ready" in text
    assert text.count("Open this case") == 2
    assert '<form method="post" action="/auth/signout"' in text and "Sign out" in text
    assert "<script" not in text
    # Only what the list shows was unsealed: no amount, and no link to any case is on the page.
    assert "1,850" not in text and "/c/" not in text
    # A page with someone's cases on it is never cached, indexed or leaked as a referrer.
    assert is_private(page) and page.headers["vary"] == "Cookie"


@respx.mock
def test_the_history_shows_only_the_signed_in_accounts_cases():
    client, engine = seeded()
    other = TestClient(client.app)
    sign_in(client, subject="person-one")
    sign_in(other, subject="person-two")
    mine, theirs = begin(client), begin(other)
    nobody = begin(TestClient(client.app))  # started signed out: in nobody's history
    for browser, own, foreign in ((client, mine, theirs), (other, theirs, mine)):
        for path in ("/", "/account"):
            listed = opened_from_history(browser.get(path))
            assert listed == [f"/account/cases/{case_id_of(client, own)}"], path
        # Another account's case cannot be opened from here, nor can one that is in no account.
        for locked in (foreign, nobody):
            refused = browser.get(
                f"/account/cases/{case_id_of(client, locked)}", follow_redirects=False
            )
            assert refused.status_code == 404
    assert len(rows(engine, AccountCaseRow)) == 2 and len(rows(engine, CaseRow)) == 3


@respx.mock
def test_a_case_started_while_signed_in_is_saved_without_asking():
    client, engine = seeded()
    before = begin(client)  # signed out: nothing to save it to
    assert rows(engine, AccountCaseRow) == []
    sign_in(client)
    after = begin(client)
    [link] = rows(engine, AccountCaseRow)
    assert link.case_id == case_id_of(client, after)
    assert opened_from_history(client.get("/")) == [f"/account/cases/{link.case_id}"]
    assert "Saved to your account" in finish(client, after).text
    # The one from before signing in is offered, not taken.
    assert "Save this case to my account" in client.get(before).text
    # Another site posting the start form does not put a case into the account.
    client.post("/cases", data={"state": "MA"}, headers={"sec-fetch-site": "cross-site"})
    assert len(rows(engine, AccountCaseRow)) == 1 and len(rows(engine, CaseRow)) == 3


@respx.mock
def test_the_result_and_check_pages_offer_to_keep_the_case_and_say_when_it_is_saved():
    client, engine = seeded()
    base = begin(client)
    token = base.removeprefix("/c/")
    offer = f'<a href="/auth/google/start?case={token}" class="btn">'
    result = finish(client, base)
    for page in (result, client.get(base)):
        assert offer + "Sign in with Google to keep this case</a>" in page.text, str(page.url)
        assert "Saved to your account" not in page.text
        assert "Save this case to my account" not in page.text
    # The offer sits before the case bar, and it is not on the steps before the result.
    assert result.text.index("Sign in with Google to keep") < result.text.index('class="casebar"')
    for step in ("bill", "household", "income"):
        assert "Sign in with Google" not in client.get(f"{base}/{step}").text

    # Through Google and back: the case is in the account and the page says so.
    back = sign_in(client, case=token)
    assert back.headers["location"] == f"{base}/result"
    for path in (f"{base}/result", base):
        page = client.get(path)
        assert "Saved to your account" in page.text and 'href="/account"' in page.text
        assert "Sign in with Google" not in page.text
        assert "Save this case to my account" not in page.text
    [link] = rows(engine, AccountCaseRow)
    assert link.case_id == case_id_of(client, base)


@respx.mock
def test_signed_in_a_case_from_before_is_saved_with_one_tap():
    client, engine = seeded()
    base = begin(client)
    finish(client, base)
    sign_in(client)  # from the home page: the case is not in the account yet
    assert opened_from_history(client.get("/")) == []
    for back, landing in (("result", f"{base}/result"), ("review", base)):
        page = client.get(landing)
        form = (
            f'<form method="post" action="{base}/save" class="stack">\n'
            f'  <input type="hidden" name="back" value="{back}">\n'
            '  <button type="submit" class="btn">Save this case to my account</button>'
        )
        assert form in page.text, landing
        assert "Sign in with Google" not in page.text and "Saved to your account" not in page.text
    saved = client.post(f"{base}/save", data={"back": "result"}, follow_redirects=False)
    assert saved.status_code == 303 and saved.headers["location"] == f"{base}/result"
    assert "Saved to your account" in client.get(f"{base}/result").text
    assert opened_from_history(client.get("/")) == [f"/account/cases/{case_id_of(client, base)}"]
    # Saving again changes nothing; any other `back` lands on the check page.
    again = client.post(f"{base}/save", data={"back": "https://evil.example"})
    assert str(again.url).endswith(base) and len(rows(engine, AccountCaseRow)) == 1


@respx.mock
def test_saving_needs_the_full_link_and_a_session():
    client, engine = seeded()
    with session_scope(engine) as session:
        links = start_case(client.app.state.deps.context(session), "MA")
    base = f"/c/{links.caregiver_token}"
    # Signed out (the session ran out since the page was shown): off to sign in, which keeps it.
    signed_out = client.post(f"{base}/save", follow_redirects=False)
    assert signed_out.status_code == 303
    assert signed_out.headers["location"] == f"/auth/google/start?case={links.caregiver_token}"
    assert rows(engine, AccountCaseRow) == []
    sign_in(client)
    # The photo-only link cannot put the case into an account.
    assert client.post(f"/c/{links.senior_token}/save").status_code == 403
    assert client.post(f"/s/{links.senior_token}/save").status_code in (404, 405)
    assert rows(engine, AccountCaseRow) == []
    # And its pages make no offer.
    client.post(f"{base}/bill", files=photo())
    limited = client.get(f"/s/{links.senior_token}/result")
    for word in ("Sign in with Google", "Save this case", "Saved to your account"):
        assert word not in limited.text


@respx.mock
def test_opening_a_case_from_the_history_mints_a_fresh_full_link():
    client, engine = seeded()
    sign_in(client)
    base = begin(client)
    case_id = case_id_of(client, base)
    client.post(f"{base}/bill", files=photo())
    opened = client.get(f"/account/cases/{case_id}", follow_redirects=False)
    assert opened.status_code == 303 and is_private(opened)
    location = opened.headers["location"]
    token = location.removeprefix("/c/")
    claims = client.app.state.deps.signer.verify(token)
    assert (claims.case_id, claims.scope, claims.generation) == (case_id, "caregiver", 1)
    # The same life as any full link.
    assert timedelta(days=364) < claims.expires_at - datetime.now(UTC) <= timedelta(days=365)
    page = client.get(location)
    assert page.status_code == 200 and "Check what we read" in page.text
    assert "$1,850.00" in page.text
    # No link is stored with the account: the table has the case's id and nothing to open it.
    [link] = rows(engine, AccountCaseRow)
    assert (link.case_id, hasattr(link, "token")) == (case_id, False)

    # "Make new links" still revokes: every old link dies, the one just minted included ...
    client.post(f"{base}/relink")
    for dead in (base, location):
        assert client.get(dead).status_code == 403
    # ... and the history opens the case with a link of the new generation.
    reopened = client.get(f"/account/cases/{case_id}", follow_redirects=False)
    fresh = reopened.headers["location"].removeprefix("/c/")
    assert client.app.state.deps.signer.verify(fresh).generation == 2
    assert client.get(reopened.headers["location"]).status_code == 200

    # A case with no bill yet opens at the photo step.
    empty = begin(client)
    step = client.get(f"/account/cases/{case_id_of(client, empty)}", follow_redirects=False)
    assert re.fullmatch(r"/c/[^/]+/bill", step.headers["location"])
    # Signed out, the history opens nothing; an id that is no case is not found.
    assert client.get("/account/cases/" + "0" * 32, follow_redirects=False).status_code == 404
    client.post("/auth/signout")
    signed_out = client.get(f"/account/cases/{case_id}", follow_redirects=False)
    assert signed_out.status_code == 303 and signed_out.headers["location"] == "/"


@respx.mock
def test_deleting_a_case_takes_it_out_of_the_history():
    client, engine = seeded()
    sign_in(client)
    kept, gone = begin(client), begin(client)
    assert len(opened_from_history(client.get("/"))) == 2
    page = client.post(f"{gone}/delete")
    # The home page that follows already shows the history without it.
    assert page.status_code == 200 and "deleted" in page.text.lower()
    assert opened_from_history(page) == [f"/account/cases/{case_id_of(client, kept)}"]
    assert opened_from_history(client.get("/account")) == opened_from_history(page)
    assert [link.case_id for link in rows(engine, AccountCaseRow)] == [case_id_of(client, kept)]
    assert page.headers["clear-site-data"] == '"cache"' and is_private(page)
    assert len(rows(engine, AccountRow)) == 1 and len(rows(engine, SessionRow)) == 1


@respx.mock
def test_the_account_page_lists_the_cases_and_asks_before_deleting_everything():
    client, _ = seeded()
    sign_in(client)
    base = begin(client)
    page = client.get("/account")
    assert page.status_code == 200 and is_private(page)
    assert "<h1>Your cases</h1>" in page.text
    assert opened_from_history(page) == [f"/account/cases/{case_id_of(client, base)}"]
    assert "no name and no email" in page.text
    assert '<form method="post" action="/auth/signout"' in page.text
    # The button only opens the question; the button under it does the deleting.
    assert re.search(
        r'<details>\s*<summary class="btn danger">Delete my account and all my cases</summary>'
        r'\s*<form method="post" action="/account/delete"[^>]*>.*?cannot be undone.*?'
        r'<button type="submit" class="btn danger">Yes, delete everything</button>',
        page.text,
        re.DOTALL,
    )
    assert "<script" not in page.text and "onclick=" not in page.text
    assert "onsubmit=" not in page.text
    assert client.get("/account/delete").status_code == 405  # a link cannot delete


def test_the_account_page_needs_a_session():
    client, _ = signin_client()
    for browser in (client, TestClient(client.app, cookies={SESSION_COOKIE: "x" * 43})):
        response = browser.get("/account", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/"
        assert is_private(response)
        response = browser.post("/account/delete", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/"


@respx.mock
def test_a_session_that_ran_out_is_signed_out_everywhere():
    client, engine = seeded()
    sign_in(client)
    base = begin(client)
    with session_scope(engine) as session:
        for row in session.scalars(select(SessionRow)):
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert "Sign in with Google" in client.get("/").text
    assert "Your cases" not in client.get("/").text
    assert client.get("/account", follow_redirects=False).status_code == 303
    assert "Sign in with Google to keep this case" in client.get(base).text
    # The case itself still opens with its link, as it does for anyone who holds it.
    assert client.get(f"{base}/bill").status_code == 200


@respx.mock
def test_deleting_the_account_deletes_its_cases_and_sessions_and_signs_out():
    client, engine = seeded()
    other, second_browser = TestClient(client.app), TestClient(client.app)
    sign_in(client, subject="person-one")
    sign_in(second_browser, subject="person-one")
    sign_in(other, subject="person-two")
    mine, also_mine, theirs = begin(client), begin(second_browser), begin(other)
    finish(client, mine)
    loose = begin(TestClient(client.app))
    assert len(rows(engine, SessionRow)) == 3 and len(rows(engine, CaseRow)) == 4

    # Another site cannot trigger it.
    for site in ("cross-site", "same-site"):
        blocked = client.post(
            "/account/delete", headers={"sec-fetch-site": site}, follow_redirects=False
        )
        assert blocked.status_code == 303 and len(rows(engine, CaseRow)) == 4
    assert len(rows(engine, AccountRow)) == 2

    session_id = client.cookies[SESSION_COOKIE]
    page = client.post("/account/delete", headers={"sec-fetch-site": "same-origin"})
    assert page.status_code == 200
    assert "Your account and all your cases were deleted." in page.text
    # The home page again, signed out.
    assert "Sign in with Google" in page.text and "Your cases" not in page.text
    assert is_private(page) and page.headers["clear-site-data"] == '"cache"'
    [cleared] = [line for line in set_cookies(page) if line.startswith(f"{SESSION_COOKIE}=")]
    assert "max-age=0" in cleared.lower() and SESSION_COOKIE not in client.cookies

    assert {row.id for row in rows(engine, CaseRow)} == {
        case_id_of(client, theirs),
        case_id_of(client, loose),
    }
    [account] = rows(engine, AccountRow)
    [session_row] = rows(engine, SessionRow)
    assert session_row.account_id == account.id
    assert [link.case_id for link in rows(engine, AccountCaseRow)] == [case_id_of(client, theirs)]
    # The cases are gone for every link, and every browser of that account is signed out.
    for dead in (mine, also_mine):
        assert client.get(dead).status_code == 403
    with session_scope(engine) as session:
        assert sessions.read(session, session_id) is None
    assert "Sign in with Google" in second_browser.get("/").text
    assert second_browser.get("/account", follow_redirects=False).status_code == 303
    # The other person noticed nothing.
    assert opened_from_history(other.get("/")) == [f"/account/cases/{case_id_of(client, theirs)}"]
    with session_scope(engine) as session:
        ctx = client.app.state.deps.context(session)
        assert authorize(ctx, theirs.removeprefix("/c/"), "caregiver").id


@respx.mock
def test_signing_out_ends_access_to_the_history_but_not_to_a_link_someone_holds():
    client, _ = seeded()
    sign_in(client)
    base = begin(client)
    client.post("/auth/signout")
    home = client.get("/")
    assert "Sign in with Google" in home.text and "Your cases" not in home.text
    assert "x-robots-tag" not in home.headers
    assert client.get("/account", follow_redirects=False).status_code == 303
    assert client.get(f"{base}/bill").status_code == 200
    # Signing in again brings the history back.
    sign_in(client)
    assert opened_from_history(client.get("/")) == [f"/account/cases/{case_id_of(client, base)}"]


@respx.mock
def test_the_history_names_a_hospital_the_registry_did_not_match_as_the_bill_printed_it():
    from waive.cases.extract import BillExtract

    class UnknownHospitalAI:
        def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
            return BillExtract(
                hospital_name="Somewhere Else Hospital", patient_name="Rosa Alvarez", confidence=0.9
            )

    client, engine = signin_client(ai=UnknownHospitalAI())
    sign_in(client)
    base = begin(client)
    assert "Hospital not read yet" in client.get("/account").text
    client.post(f"{base}/bill", files=photo())
    page = client.get("/account")
    assert "Somewhere Else Hospital" in page.text and "Hospital not read yet" not in page.text
    assert "Bill read" in page.text
    assert "Rosa" not in page.text  # nothing else from the sealed bill


@respx.mock
def test_the_history_says_in_plain_words_how_far_each_case_got():
    client, engine = seeded()
    sign_in(client)
    base = begin(client)
    case_id = case_id_of(client, base)

    def status():
        return client.get("/account").text

    assert "Waiting for a photo of the bill" in status()
    client.post(f"{base}/bill", files=photo())
    assert "Bill read. Check what we read." in status()
    client.post(f"{base}/confirm", data={"answer": "yes"})
    assert "Some questions are left" in status()
    client.post(f"{base}/household", data={"size": "1", "programs": "none"})
    client.post(f"{base}/income", data={"monthly": "1900"})
    assert "Result ready. Next: make your form." in status()
    client.post(f"{base}/approve")
    assert "Form ready to print and send" in status()
    answers = {
        "approved": "The hospital approved it",
        "partial": "The hospital gave a discount",
        "denied": "The hospital said no",
        "more_info": "The hospital asks for more",
    }
    for decision, words in answers.items():
        with session_scope(engine) as session:
            session.get(CaseRow, case_id).outcome = {"decision": decision}
        assert words in status(), decision
    # Raw status words never reach the page.
    for raw in ("bill_read", "more_info", "evaluated"):
        assert raw not in status()
