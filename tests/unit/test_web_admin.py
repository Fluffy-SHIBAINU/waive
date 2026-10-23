import base64
import re
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from pydantic import SecretStr

from waive.atlas import repo
from waive.atlas.eve_import import record_owner_rulings
from waive.atlas.pipeline import build_hospital
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import SAMPLE_POLICY_TEXT, st_example_sheet
from waive.atlas.schema import SheetStatus
from waive.cases.service import confirm_bill, set_household, start_case
from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.config import Settings
from waive.db import init_db, make_engine, session_scope
from waive.learning.classify import PhotoClass
from waive.learning.contributions import submit_contribution
from waive.learning.evidence import SLIP_PATH, add_evidence, flag_confirmed, slip_flag_level
from waive.learning.outcomes import Decision, DenialReason, OutcomeExtract
from waive.learning.triage import record_outcome
from waive.web.app import create_app

from tests.unit.test_contributions import NEW_POLICY_TEXT, PhotoAI
from tests.unit.test_pipeline import (
    NEWER_POLICY,
    NEWER_POLICY_TEXT,
    REAL_HOSPITAL,
    UNSURE_HOSPITAL,
    FakeAI,
    FakeGateway,
    GroundedAI,
    owner_ruling,
)
from tests.unit.test_web_senior import HOSPITAL

TOKEN = "admin-" + "t" * 30
TODAY = date(2026, 10, 2)
LEDGER = (
    '{"provider": "tavily", "units": "12", "usd": "0", "purpose": "atlas.scout", "ts": "t"}\n'
    '{"provider": "token_factory", "units": "5000", "usd": "0.0123", "purpose": "atlas.structure", "ts": "t"}\n'
)


def admin_client(tmp_path, token=TOKEN):
    """Seeded app: one hospital, three denied cases (a re-scout request and an internal flag), one
    contribution waiting, a ledger with some spend."""
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    settings = Settings(
        _env_file=None,
        nebius_api_key=SecretStr("k"),
        admin_token=SecretStr(token) if token else None,
        ledger_path=tmp_path / "usage.jsonl",
    )
    app = create_app(
        settings,
        engine=engine,
        ai=PhotoAI(),
        cipher=FieldCipher(base64.b64decode(new_key())),
        signer=TokenSigner("x" * 40),
        today_fn=lambda: TODAY,
    )
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        sheet = st_example_sheet()
        publish_sheet(session, sheet)
        repo.save_source(session, sheet.sources[0], SAMPLE_POLICY_TEXT, "229999")
        ctx = app.state.deps.context(session)
        for _ in range(3):
            links = start_case(ctx, "MA")
            confirm_bill(
                ctx,
                links.case_id,
                {"statement_date": "2026-09-03", "amount_due": "1850.00"},
                ccn="229999",
            )
            set_household(ctx, links.case_id, 1, Decimal("28000"), ())
            denial = OutcomeExtract(decision=Decision.DENIED, reasons=[DenialReason.OTHER])
            record_outcome(ctx, links.case_id, denial)
        submit_contribution(
            session,
            ccn="229999",
            case_id=links.case_id,
            photo_class=PhotoClass.FAP,
            text=NEW_POLICY_TEXT,
            vision_flag=False,
            today=TODAY,
        )
    (tmp_path / "usage.jsonl").write_text(LEDGER)
    return TestClient(app)


def login(client, token=TOKEN):
    return client.post("/admin/login", data={"token": token}, follow_redirects=True)


def test_admin_pages_need_the_token(tmp_path):
    client = admin_client(tmp_path)
    assert client.get("/admin").status_code == 403
    assert client.get("/admin/review").status_code == 403
    assert client.get("/admin/login").status_code == 200
    assert client.post("/admin/login", data={"token": "wrong"}).status_code == 403
    home = login(client)
    assert home.status_code == 200 and "Admin console" in home.text
    off = admin_client(tmp_path, token=None)
    assert off.post("/admin/login", data={"token": "anything"}).status_code == 403


def test_admin_cookie_is_a_session_id_not_the_secret(tmp_path):
    """Cookie disclosure (a synced profile, a HAR file) must not hand over WAIVE_ADMIN_TOKEN,
    which can only be rotated by redeploying; a session id can simply be signed out."""
    client = admin_client(tmp_path)
    signed_in = client.post("/admin/login", data={"token": TOKEN}, follow_redirects=False)
    assert signed_in.status_code == 303
    cookie = signed_in.headers["set-cookie"]
    assert TOKEN not in cookie and "HttpOnly" in cookie and "samesite=strict" in cookie.lower()
    assert "secure" not in cookie.lower()  # plain http on the laptop demo
    session_id = client.cookies["waive_admin"]
    assert session_id != TOKEN and len(session_id) >= 32
    assert client.get("/admin").status_code == 200
    # The cookie value is not a credential: it does not sign anyone else in.
    assert client.post("/admin/login", data={"token": session_id}).status_code == 403
    over_https = TestClient(client.app, base_url="https://testserver")
    secure = over_https.post("/admin/login", data={"token": TOKEN}, follow_redirects=False)
    assert "secure" in secure.headers["set-cookie"].lower()
    assert over_https.get("/admin").status_code == 200


def test_admin_can_sign_out_and_errors_speak_to_the_admin(tmp_path):
    client = admin_client(tmp_path)
    locked_out = client.get("/admin")
    assert locked_out.status_code == 403
    assert "Admin sign-in" in locked_out.text and "helper" not in locked_out.text.lower()
    wrong = client.post("/admin/login", data={"token": "wrong"})
    assert wrong.status_code == 403 and "not right" in wrong.text and "Admin sign-in" in wrong.text
    home = login(client)
    assert "Sign out" in home.text
    out = client.post("/admin/logout", follow_redirects=False)
    assert out.status_code == 303 and out.headers["location"] == "/admin/login"
    assert client.get("/admin").status_code == 403
    assert client.get("/admin/review").status_code == 403


def test_repeated_wrong_tokens_lock_the_sign_in_for_a_while(tmp_path):
    client = admin_client(tmp_path)
    assert client.app.state.settings.admin_login_failures == 10
    for _ in range(10):
        assert client.post("/admin/login", data={"token": "wrong"}).status_code == 403
    locked = client.post("/admin/login", data={"token": "wrong"})
    assert locked.status_code == 429 and "too many" in locked.text.lower()
    # Locked for everyone, right token included: the limiter is global because the client
    # address behind the front end is whatever X-Forwarded-For says.
    assert client.post("/admin/login", data={"token": TOKEN}).status_code == 429
    client.app.state.limiter.advance(15 * 60 + 1)
    signed_in = client.post("/admin/login", data={"token": TOKEN}, follow_redirects=False)
    assert signed_in.status_code == 303
    assert client.get("/admin").status_code == 200


def test_home_shows_counts_budget_and_a_clean_audit(tmp_path):
    client = admin_client(tmp_path)
    home = login(client)
    assert "2 open" in home.text  # rescout_request + accountability_flag
    assert "1 waiting" in home.text and "3 outcomes" in home.text
    assert "12 of 1000" in home.text and "$0.01 of $15" in home.text
    assert "No personal data found" in home.text


def test_review_queue_resolves_a_rescout_as_sheet_wrong_and_withdraws_slips(tmp_path):
    client = admin_client(tmp_path)
    login(client)
    queue = client.get("/admin/review").text
    assert "rescout_request" in queue and "accountability_flag" in queue
    item_id = int(re.search(r'action="/admin/review/(\d+)"', queue).group(1))
    after = client.post(
        f"/admin/review/{item_id}",
        data={"status": "resolved", "verdict": "sheet_wrong"},
        follow_redirects=True,
    )
    assert "rescout_request" not in after.text and "accountability_flag" not in after.text
    assert "Nothing to review" in after.text


def test_review_queue_resolves_a_rescout_as_hospital_slip_and_confirms_the_public_flag(tmp_path):
    client = admin_client(tmp_path)
    login(client)
    with session_scope(client.app.state.deps.engine) as session:
        for index in range(2):  # three real denials plus two more: the public threshold
            add_evidence(
                session, "229999", SLIP_PATH, "denied_despite_policy", f"extra-{index}", TODAY
            )
        assert slip_flag_level(session, "229999") == "public"
    assert "report being denied" not in client.get("/atlas/229999").text
    queue = client.get("/admin/review").text
    item_id = int(re.search(r'action="/admin/review/(\d+)"', queue).group(1))
    after = client.post(
        f"/admin/review/{item_id}",
        data={"status": "resolved", "verdict": "hospital_slip"},
        follow_redirects=True,
    )
    assert "rescout_request" not in after.text and "accountability_flag" not in after.text
    assert "5 patients report being denied" in client.get("/atlas/229999").text
    with session_scope(client.app.state.deps.engine) as session:
        assert flag_confirmed(session, "229999")


def test_contribution_approval_rebuild_and_sheet_diffs(tmp_path):
    client = admin_client(tmp_path)
    login(client)
    page = client.get("/admin/contributions").text
    assert "household income at or below 150%" in page
    contribution_id = int(re.search(r"/admin/contributions/(\d+)/approve", page).group(1))
    approved = client.post(f"/admin/contributions/{contribution_id}/approve", follow_redirects=True)
    assert "No contributions waiting" in approved.text
    rebuilt = client.post("/admin/rebuild/229999", follow_redirects=True)
    assert "published; version 2" in rebuilt.text
    assert "eligibility.free_care_max_fpl" in rebuilt.text
    assert "was:</span> 250" in rebuilt.text and "now:</span> 150" in rebuilt.text
    assert "flag level: internal" in rebuilt.text


def test_the_rebuild_button_keeps_the_owners_ruling(tmp_path):
    # The button passes no decision and the server has no decisions file; the ruling stored with
    # the hospital holds out the 250% the model reads as free care and the owner ruled a discount.
    client = admin_client(tmp_path)
    login(client)
    client.app.state.deps.ai = FakeAI()
    with session_scope(client.app.state.deps.engine) as session:
        record_owner_rulings(session, {"229999": owner_ruling("discount")})
    rebuilt = client.post("/admin/rebuild/229999", follow_redirects=True)
    assert "owner review says 250% is a discount or assistance limit" in rebuilt.text
    with session_scope(client.app.state.deps.engine) as session:
        latest, _ = repo.latest_sheet(session, "229999")
        assert latest.eligibility.free_care_max_fpl is None
        assert latest.eligibility.assistance_ceiling_fpl.value == 250
        assert "owner_decision" in {i.kind for i in repo.open_review_items(session, "229999")}


def test_scoreboard_page_and_precheck_queue(tmp_path):
    client = admin_client(tmp_path)
    login(client)
    board = client.get("/admin/scoreboard").text
    assert "ST. EXAMPLE MEDICAL CENTER" in board and "0%" in board and "needs re-check" in board
    queued = client.post("/admin/scoreboard/prechecks", follow_redirects=True)
    assert "Queued re-checks for 1 hospital" in queued.text
    assert "priority_recheck" in client.get("/admin/review").text


def hold_for_its_website(client):
    """Real General (220031), built from a website discovery matched with low confidence: its
    sheet is held until an admin confirms the site (decision 7.9a-1)."""
    with session_scope(client.app.state.deps.engine) as session:
        repo.upsert_hospital(session, UNSURE_HOSPITAL)
        assert build_hospital(session, FakeGateway(), FakeAI(), "220031", TODAY).outcome == "held"


def test_the_website_actions_need_an_admin(tmp_path):
    client = admin_client(tmp_path)
    hold_for_its_website(client)
    for action in ("confirm-website", "wrong-website"):
        refused = client.post(f"/admin/sheets/220031/{action}", data={"domain": "example.org"})
        assert refused.status_code == 403 and "Admin sign-in" in refused.text
    with session_scope(client.app.state.deps.engine) as session:
        row = repo.get_hospital(session, "220031")
        assert (row.website_domain, row.domain_confidence) == ("example.org", 0.6)
        assert repo.latest_sheet(session, "220031")[0].status is SheetStatus.HELD
    login(client)
    unknown = client.post("/admin/sheets/000000/confirm-website", data={"domain": "example.org"})
    assert unknown.status_code == 404


def test_an_admin_confirms_a_website_and_its_held_sheet_publishes(tmp_path):
    client = admin_client(tmp_path)
    hold_for_its_website(client)
    login(client)
    page = client.get("/admin/sheets/220031").text
    assert "Website not confirmed" in page
    assert '<a href="https://www.example.org" rel="noreferrer">example.org</a>' in page
    assert "matched with confidence 0.6" in page
    assert ">Confirm website<" in page and ">Wrong website<" in page
    assert client.get("/admin/review").text.count(">Confirm website<") == 1
    # A page left open while the website changed confirms nothing.
    stale = client.post(
        "/admin/sheets/220031/confirm-website", data={"domain": "other.org"}, follow_redirects=True
    )
    assert "skipped; the website is example.org now, not other.org; nothing confirmed" in stale.text
    confirmed = client.post(
        "/admin/sheets/220031/confirm-website",
        data={"domain": "example.org"},
        follow_redirects=False,
    )
    assert confirmed.status_code == 303
    assert confirmed.headers["location"].startswith("/admin/sheets/220031?note=")
    page = client.get(confirmed.headers["location"]).text
    assert "published; version 2; website example.org confirmed" in page
    assert ">Confirm website<" not in page and ">Wrong website<" not in page
    assert "domain_unconfirmed" not in client.get("/admin/review").text
    with session_scope(client.app.state.deps.engine) as session:
        assert repo.get_hospital(session, "220031").domain_confidence == 1.0
        assert repo.latest_sheet(session, "220031")[0].status is SheetStatus.PUBLISHED


def test_confirming_a_website_keeps_the_sheet_held_while_another_reason_remains(tmp_path):
    client = admin_client(tmp_path)
    with session_scope(client.app.state.deps.engine) as session:
        repo.upsert_hospital(session, REAL_HOSPITAL)
        assert build_hospital(session, FakeGateway(), FakeAI(), "220031", TODAY).outcome == (
            "published"
        )
        repo.save_source(session, NEWER_POLICY, NEWER_POLICY_TEXT, "220031")
        repo.get_hospital(session, "220031").domain_confidence = 0.6
        rebuilt = build_hospital(
            session, FakeGateway(), GroundedAI(), "220031", TODAY, reuse_sources=True
        )
        assert rebuilt.outcome == "held"
    login(client)
    after = client.post(
        "/admin/sheets/220031/confirm-website",
        data={"domain": "example.org"},
        follow_redirects=True,
    )
    assert "held; version 2; website example.org confirmed" in after.text
    assert "documents state different free-care limits (fap-newer-pdf: 200%)" in after.text
    assert ">Confirm website<" not in after.text  # the website is settled; the documents are not
    with session_scope(client.app.state.deps.engine) as session:
        assert repo.get_hospital(session, "220031").domain_confidence == 1.0
        assert repo.latest_sheet(session, "220031")[0].status is SheetStatus.HELD


def test_the_website_actions_show_only_where_a_held_sheet_waits_on_its_website(tmp_path):
    client = admin_client(tmp_path)
    with session_scope(client.app.state.deps.engine) as session:
        # Held for a reason of its own (the models disagree) on a site discovery was sure of.
        repo.upsert_hospital(session, REAL_HOSPITAL)
        ai = FakeAI(free_limit_for_fast="300", free_limit_for_tiebreak="999")
        assert build_hospital(session, FakeGateway(), ai, "220031", TODAY).outcome == "held"
        # Published before decision 7.9a-1 from a guess: `recheck --withdraw` holds it first.
        repo.get_hospital(session, "229999").domain_confidence = 0.6
    login(client)
    for ccn in ("229999", "220031"):
        page = client.get(f"/admin/sheets/{ccn}").text
        assert "Website not confirmed" not in page
        assert ">Confirm website<" not in page and ">Wrong website<" not in page
    queue = client.get("/admin/review").text
    assert "conflict" in queue and ">Confirm website<" not in queue


def test_an_admin_marks_a_website_wrong_and_the_sheet_stays_held_without_it(tmp_path):
    client = admin_client(tmp_path)
    hold_for_its_website(client)
    login(client)
    after = client.post(
        "/admin/sheets/220031/wrong-website",
        data={"domain": "example.org", "reason": " "},
        follow_redirects=True,
    )
    assert "held; version 2; domain example.org cleared (an admin found it is not the" in after.text
    assert ">Confirm website<" not in after.text
    assert "domain_unconfirmed" not in client.get("/admin/review").text
    with session_scope(client.app.state.deps.engine) as session:
        row = repo.get_hospital(session, "220031")
        assert (row.website_domain, row.domain_confidence) == (None, None)
        held, _ = repo.latest_sheet(session, "220031")
        assert held.status is SheetStatus.HELD and held.eligibility.free_care_max_fpl is None


def test_the_card_warns_when_the_held_sheet_was_built_from_another_website(tmp_path):
    client = admin_client(tmp_path)
    hold_for_its_website(client)
    with session_scope(client.app.state.deps.engine) as session:
        # Discovery moved to another site after the build; the held fields are example.org's.
        row = repo.get_hospital(session, "220031")
        row.website_domain, row.domain_confidence = "newsite.org", 0.6
    login(client)
    page = client.get("/admin/sheets/220031").text
    assert "Website not confirmed" in page and ">Confirm website<" in page
    assert "built from example.org, not newsite.org" in page
    assert "Confirming publishes it" not in page
    after = client.post(
        "/admin/sheets/220031/confirm-website",
        data={"domain": "newsite.org"},
        follow_redirects=True,
    )
    assert "held; version 1; website newsite.org confirmed" in after.text
    assert "uv run waive atlas build --state MA --ccn 220031" in after.text
    with session_scope(client.app.state.deps.engine) as session:
        assert repo.latest_sheet(session, "220031")[0].status is SheetStatus.HELD


def test_console_changes_from_another_site_are_refused(tmp_path):
    # waive.kindred-ai.dev is same-site with kindred-ai.dev and its other subdomains, so the
    # SameSite=Strict cookie alone would let a page there post to the console.
    client = admin_client(tmp_path)
    hold_for_its_website(client)
    login(client)
    for site in ("cross-site", "same-site"):
        refused = client.post(
            "/admin/sheets/220031/confirm-website",
            data={"domain": "example.org"},
            headers={"sec-fetch-site": site},
        )
        assert refused.status_code == 403 and "another site" in refused.text
    with session_scope(client.app.state.deps.engine) as session:
        assert repo.get_hospital(session, "220031").domain_confidence == 0.6
    # Reading the console from a link elsewhere is fine; changing it from this site works.
    page = client.get("/admin/sheets/220031", headers={"sec-fetch-site": "cross-site"})
    assert page.status_code == 200
    done = client.post(
        "/admin/sheets/220031/confirm-website",
        data={"domain": "example.org"},
        headers={"sec-fetch-site": "same-origin"},
        follow_redirects=True,
    )
    assert "published; version 2; website example.org confirmed" in done.text
