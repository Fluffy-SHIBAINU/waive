"""Phase 9, task 9.8, re-keyed to Kindred accounts (2026-10-21): the home page and the history
of the reader's cases. The reader is whoever the Kindred ID wall let in: tests/conftest.py signs
every request in as `SIGNED_IN`, and a second person is played by switching that stub to another
sub for a few requests. Waive's own Google sign-in is dormant and plays no part here."""

import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.auth import accounts
from waive.cases.service import authorize, start_case
from waive.db import AccountCaseRow, AccountRow, CaseRow, SessionRow, session_scope
from waive.web import kindred_auth
from waive.web.signin import SESSION_COOKIE, signed_in

from tests.conftest import SIGNED_IN
from tests.unit.test_oidc import CLIENT_ID, CLIENT_SECRET
from tests.unit.test_web_app import make_client
from tests.unit.test_web_auth import rows
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
# Waive's own sign-in, dormant: none of it is offered on any page.
WAIVE_SIGN_IN_WORDS = ("Sign in with Google", "/auth/google", "/auth/signout", "Google")
PRIVATE = {
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "x-robots-tag": "noindex, nofollow",
}
ME = SIGNED_IN["sub"]
OTHER = "other"
WAIVE_GOOGLE = {"google_client_id": CLIENT_ID, "google_client_secret": SecretStr(CLIENT_SECRET)}


@contextmanager
def reader(monkeypatch, sub):
    """The requests made inside come from the Kindred account `sub` instead of the conftest's
    (the same stub, another subject); on the way out the conftest's reader is back."""
    with monkeypatch.context() as patch:
        patch.setattr(
            kindred_auth, "bypass_user", lambda env=None, host=None: {**SIGNED_IN, "sub": sub}
        )
        yield


def seeded(**overrides):
    """An app with the example hospital and its sheet. Returns (client, engine)."""
    client, engine = make_client(ai=FakeAI(), **overrides)
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, st_example_sheet())
    return client, engine


def main_of(page):
    return page.text.split('<main class="page">', 1)[1].split("</main>", 1)[0]


def waive_text(page) -> str:
    """The page without the footer's Kindred ID sign-out form, which is on every page."""
    head, _, rest = page.text.partition('<form class="kindred"')
    return head + rest.partition("</form>")[2]


def begin(client, site="same-origin"):
    """Press "Start a case"; returns the full link's address (/c/{token}). `site` is what the
    browser says about where the form post came from."""
    created = client.post(
        "/cases", data={"state": "MA"}, headers={"sec-fetch-site": site}, follow_redirects=False
    )
    assert created.status_code == 303
    return created.headers["location"].removesuffix("/bill")


def unlinked(client, engine):
    """A case in nobody's history (started before accounts, or by someone else): its full link."""
    with session_scope(engine) as session:
        links = start_case(client.app.state.deps.context(session), "MA")
    return f"/c/{links.caregiver_token}", links


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


def account_of(client, engine, sub) -> AccountRow | None:
    """The account row the Kindred subject `sub` maps to, or None before its first request."""
    digest = accounts.signer_subject_hash(client.app.state.deps.signer, "google", sub)
    return next((row for row in rows(engine, AccountRow) if row.subject_hash == digest), None)


def opened_from_history(page):
    return re.findall(r'<a href="(/account/cases/[0-9a-f]{32})" class="btn">', page.text)


def is_private(response):
    return all(response.headers.get(name) == value for name, value in PRIVATE.items())


def test_with_no_cases_yet_the_home_page_is_exactly_as_it_was():
    client, engine = make_client()
    page = client.get("/")
    assert main_of(page) == HOME_AS_BEFORE
    # Nobody's data is on it: the page stays public, cacheable and findable. It differs by
    # account, though, and says so to any cache on the way.
    assert "x-robots-tag" not in page.headers and page.headers.get("cache-control") != "no-store"
    assert page.headers["vary"] == "Cookie" and "set-cookie" not in page.headers
    # A cookie from Waive's own, dormant sign-in changes nothing and is not even looked up.
    stale = TestClient(client.app, cookies={SESSION_COOKIE: "x" * 43})
    assert stale.get("/").text == page.text
    # The reader got their account on first sight: Google's own subject id, as the Issuer signs
    # it, under the hash Waive's own sign-in used, so the same person keeps the same history.
    [account] = rows(engine, AccountRow)
    assert account.provider == "google"
    assert account.subject_hash == accounts.signer_subject_hash(
        client.app.state.deps.signer, "google", ME
    )
    assert rows(engine, SessionRow) == []  # no Waive session: the Kindred cookie is the session


@pytest.mark.parametrize("overrides", [{}, WAIVE_GOOGLE], ids=["unconfigured", "configured"])
def test_nothing_about_waives_own_sign_in_is_rendered_anywhere(overrides):
    client, engine = seeded(**overrides)
    assert client.app.state.settings.sign_in_configured is bool(overrides)
    base = begin(client)
    pages = [client.get("/"), client.get(f"{base}/bill"), finish(client, base), client.get(base)]
    pages.append(client.get("/account"))
    pages.append(client.post(f"{base}/approve", follow_redirects=True))
    pages.append(client.post(f"{base}/delete"))
    for page in pages:
        assert page.status_code == 200
        for word in WAIVE_SIGN_IN_WORDS:
            assert word not in waive_text(page), (str(page.url), word)
    # Its routes stay as they were: there when configured, 404 when not, never pointed at.
    expected = 302 if overrides else 404
    assert client.get("/auth/google/start", follow_redirects=False).status_code == expected
    assert rows(engine, SessionRow) == []


def test_without_a_kindred_session_on_the_request_nobody_is_signed_in():
    """The wall puts the Issuer's session on the request before any route runs; a request that
    carries none (never behind the wall) belongs to nobody."""
    client, engine = make_client()
    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
    with session_scope(engine) as session:
        assert signed_in(Request(scope), session) is None
    assert rows(engine, AccountRow) == []


def test_without_a_kindred_session_the_account_routes_lead_home_and_write_nothing(monkeypatch):
    """Behind the wall no request reaches these routes without a session. One that does (the
    wall stood down for them here, the local bypass off) belongs to nobody: the account routes
    send it to the home page, where the wall asks for the sign-in; a case page makes no offer
    to save, and a save posted there changes nothing and goes back to the case, never home; no
    account row is made for it."""
    client, engine = seeded(wall_exempt_prefixes="/healthz,/static/,/account,/c/")
    monkeypatch.setattr(kindred_auth, "bypass_user", lambda env=None, host=None: None)
    base, links = unlinked(client, engine)
    attempts = (
        ("get", "/account"),
        ("get", f"/account/cases/{links.case_id}"),
        ("post", "/account/delete"),
    )
    for method, path in attempts:
        response = getattr(client, method)(
            path, headers={"sec-fetch-site": "same-origin"}, follow_redirects=False
        )
        assert (response.status_code, response.headers["location"]) == (303, "/"), path
        assert is_private(response), path
    finish(client, base)
    for page in (client.get(f"{base}/result"), client.get(base)):
        assert page.status_code == 200, page.url
        assert "Save this case" not in page.text and "Saved to your account" not in page.text
    for data, landing in (({"back": "result"}, f"{base}/result"), ({}, base)):
        saved = client.post(
            f"{base}/save",
            data=data,
            headers={"sec-fetch-site": "same-origin"},
            follow_redirects=False,
        )
        assert (saved.status_code, saved.headers["location"]) == (303, landing), data
        assert is_private(saved), data
    assert rows(engine, AccountRow) == [] and rows(engine, AccountCaseRow) == []


def test_a_case_started_by_the_reader_is_in_their_history_and_never_in_anothers(monkeypatch):
    client, engine = seeded()
    mine = begin(client)
    with reader(monkeypatch, OTHER):
        theirs = begin(client)
    nobody = begin(client, site="cross-site")  # another site's form post: in nobody's history
    for sub, own, foreign in ((ME, mine, theirs), (OTHER, theirs, mine)):
        with reader(monkeypatch, sub):
            for path in ("/", "/account"):
                listed = opened_from_history(client.get(path))
                assert listed == [f"/account/cases/{case_id_of(client, own)}"], (sub, path)
            # Another account's case cannot be opened from here, nor one that is in no account.
            for locked in (foreign, nobody):
                refused = client.get(
                    f"/account/cases/{case_id_of(client, locked)}", follow_redirects=False
                )
                assert refused.status_code == 404, (sub, locked)
    assert len(rows(engine, AccountRow)) == 2
    assert len(rows(engine, AccountCaseRow)) == 2 and len(rows(engine, CaseRow)) == 3


def test_the_home_page_shows_your_cases_only_once_there_are_some_newest_first():
    client, engine = seeded()
    empty = client.get("/")
    assert "Your cases" not in empty.text and "/account" not in empty.text
    assert "x-robots-tag" not in empty.headers and empty.headers.get("cache-control") != "no-store"

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
    assert '<a href="/account" class="btn quiet">Your account</a>' in text
    assert "<script" not in text
    # Only what the list shows was unsealed: no amount, and no link to any case is on the page.
    assert "1,850" not in text and "/c/" not in text
    # A page with someone's cases on it is never cached, indexed or leaked as a referrer.
    assert is_private(page) and page.headers["vary"] == "Cookie"


def test_a_case_started_from_this_site_is_saved_without_asking_and_the_pages_say_so():
    client, engine = seeded()
    base = begin(client)
    [link] = rows(engine, AccountCaseRow)
    assert link.case_id == case_id_of(client, base)
    assert link.account_id == account_of(client, engine, ME).id
    assert opened_from_history(client.get("/")) == [f"/account/cases/{link.case_id}"]
    result = finish(client, base)
    for page in (result, client.get(base)):
        assert "Saved to your account" in page.text and 'href="/account"' in page.text, page.url
        assert "Save this case to my account" not in page.text
    # The word sits before the case bar, and the steps before the result say nothing about it.
    assert result.text.index("Saved to your account") < result.text.index('class="casebar"')
    for step in ("bill", "household", "income"):
        assert "your account" not in client.get(f"{base}/{step}").text
    # Another site posting the start form does not put a case into the account.
    begin(client, site="cross-site")
    assert len(rows(engine, AccountCaseRow)) == 1 and len(rows(engine, CaseRow)) == 2


def test_a_case_from_outside_the_history_is_saved_with_one_tap(monkeypatch):
    client, engine = seeded()
    base, links = unlinked(client, engine)
    finish(client, base)
    assert opened_from_history(client.get("/")) == []
    for back, landing in (("result", f"{base}/result"), ("review", base)):
        page = client.get(landing)
        form = (
            f'<form method="post" action="{base}/save" class="stack">\n'
            f'  <input type="hidden" name="back" value="{back}">\n'
            '  <button type="submit" class="btn">Save this case to my account</button>'
        )
        assert form in page.text, landing
        assert "Saved to your account" not in page.text
    # One tap, straight back to the page: nobody is sent anywhere to sign in first.
    saved = client.post(f"{base}/save", data={"back": "result"}, follow_redirects=False)
    assert saved.status_code == 303 and saved.headers["location"] == f"{base}/result"
    assert "Saved to your account" in client.get(f"{base}/result").text
    assert opened_from_history(client.get("/")) == [f"/account/cases/{links.case_id}"]
    # Saving again changes nothing; any other `back` lands on the check page.
    again = client.post(f"{base}/save", data={"back": "https://evil.example"})
    assert str(again.url).endswith(base) and len(rows(engine, AccountCaseRow)) == 1
    # A neighbour on the same registrable domain, which the Lax cookie lets through, cannot
    # put a case into the reader's history for them: as on /cases, only this site's own form.
    # They land back on the case page they asked for, never on the home page.
    for data, landing in (({"back": "result"}, f"{base}/result"), ({}, base)):
        neighbour = client.post(
            f"{base}/save",
            data=data,
            headers={"sec-fetch-site": "same-site"},
            follow_redirects=False,
        )
        assert (neighbour.status_code, neighbour.headers["location"]) == (303, landing), data
    assert len(rows(engine, AccountCaseRow)) == 1
    # Someone else who holds the full link is offered the case for their own account.
    with reader(monkeypatch, OTHER):
        page = client.get(base)
        assert "Save this case to my account" in page.text
        assert "Saved to your account" not in page.text
        client.post(f"{base}/save")
        assert opened_from_history(client.get("/")) == [f"/account/cases/{links.case_id}"]
    assert len(rows(engine, AccountCaseRow)) == 2


def test_saving_needs_the_full_link():
    client, engine = seeded()
    base, links = unlinked(client, engine)
    # The photo-only link cannot put the case into an account.
    assert client.post(f"/c/{links.senior_token}/save").status_code == 403
    assert client.post(f"/s/{links.senior_token}/save").status_code in (404, 405)
    assert rows(engine, AccountCaseRow) == []
    # And its pages make no offer.
    finish(client, base)
    limited = client.get(f"/s/{links.senior_token}/result")
    assert limited.status_code == 200 and "likely do not have to pay" in limited.text
    for word in ("Save this case", "Saved to your account", "/account"):
        assert word not in limited.text


def test_opening_a_case_from_the_history_mints_a_fresh_full_link(monkeypatch):
    client, engine = seeded()
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
    # Another reader's history does not open it; an id that is no case is not found.
    with reader(monkeypatch, OTHER):
        assert client.get(f"/account/cases/{case_id}", follow_redirects=False).status_code == 404
    assert client.get("/account/cases/" + "0" * 32, follow_redirects=False).status_code == 404


def test_deleting_a_case_takes_it_out_of_the_history():
    client, engine = seeded()
    kept, gone = begin(client), begin(client)
    assert len(opened_from_history(client.get("/"))) == 2
    page = client.post(f"{gone}/delete")
    # The home page that follows already shows the history without it.
    assert page.status_code == 200 and "deleted" in page.text.lower()
    assert opened_from_history(page) == [f"/account/cases/{case_id_of(client, kept)}"]
    assert opened_from_history(client.get("/account")) == opened_from_history(page)
    assert [link.case_id for link in rows(engine, AccountCaseRow)] == [case_id_of(client, kept)]
    assert page.headers["clear-site-data"] == '"cache"' and is_private(page)
    assert len(rows(engine, AccountRow)) == 1


def test_the_account_page_lists_the_cases_and_asks_before_deleting_everything():
    client, _ = seeded()
    base = begin(client)
    page = client.get("/account")
    assert page.status_code == 200 and is_private(page)
    assert "<h1>Your cases</h1>" in page.text
    assert opened_from_history(page) == [f"/account/cases/{case_id_of(client, base)}"]
    assert "signed in to Kindred" in page.text and "no name and no email" in page.text
    # The one sign-out is Kindred's, in the footer: Waive's own is gone from the page.
    assert "/auth/signout" not in page.text
    assert 'action="https://id.kindred-ai.dev/logout"' in page.text
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


def test_deleting_the_account_deletes_only_the_readers_cases(monkeypatch):
    client, engine = seeded()
    mine, also_mine = begin(client), begin(client)
    finish(client, mine)
    with reader(monkeypatch, OTHER):
        theirs = begin(client)
    loose = begin(client, site="cross-site")
    assert len(rows(engine, AccountRow)) == 2 and len(rows(engine, CaseRow)) == 4
    before = account_of(client, engine, ME).id

    # Another site cannot trigger it.
    for site in ("cross-site", "same-site"):
        blocked = client.post(
            "/account/delete", headers={"sec-fetch-site": site}, follow_redirects=False
        )
        assert blocked.status_code == 303 and len(rows(engine, CaseRow)) == 4
    assert len(rows(engine, AccountRow)) == 2

    page = client.post("/account/delete", headers={"sec-fetch-site": "same-origin"})
    assert page.status_code == 200
    assert "Your account and all your cases were deleted." in page.text
    assert "Your cases" not in page.text and "Open this case" not in page.text
    assert is_private(page) and page.headers["clear-site-data"] == '"cache"'

    assert {row.id for row in rows(engine, CaseRow)} == {
        case_id_of(client, theirs),
        case_id_of(client, loose),
    }
    assert [link.case_id for link in rows(engine, AccountCaseRow)] == [case_id_of(client, theirs)]
    # The cases are gone for every link that opened them.
    for dead in (mine, also_mine):
        assert client.get(dead).status_code == 403
    # The reader is still signed in to Kindred: the page that followed began a fresh, empty
    # account for them, and the old one is gone with everything in it.
    fresh = account_of(client, engine, ME)
    assert fresh is not None and fresh.id != before
    assert len(rows(engine, AccountRow)) == 2
    assert opened_from_history(client.get("/")) == []
    # The other person noticed nothing.
    with reader(monkeypatch, OTHER):
        assert opened_from_history(client.get("/")) == [
            f"/account/cases/{case_id_of(client, theirs)}"
        ]
    with session_scope(engine) as session:
        ctx = client.app.state.deps.context(session)
        assert authorize(ctx, theirs.removeprefix("/c/"), "caregiver").id


def test_the_history_names_a_hospital_the_registry_did_not_match_as_the_bill_printed_it():
    from waive.cases.extract import BillExtract

    class UnknownHospitalAI:
        def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
            return BillExtract(
                hospital_name="Somewhere Else Hospital", patient_name="Rosa Alvarez", confidence=0.9
            )

    client, engine = make_client(ai=UnknownHospitalAI())
    base = begin(client)
    assert "Hospital not read yet" in client.get("/account").text
    client.post(f"{base}/bill", files=photo())
    page = client.get("/account")
    assert "Somewhere Else Hospital" in page.text and "Hospital not read yet" not in page.text
    assert "Bill read" in page.text
    assert "Rosa" not in page.text  # nothing else from the sealed bill


def test_the_history_says_in_plain_words_how_far_each_case_got():
    client, engine = seeded()
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
