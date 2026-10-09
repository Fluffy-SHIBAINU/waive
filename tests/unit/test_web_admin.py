import base64
import re
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from pydantic import SecretStr

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import SAMPLE_POLICY_TEXT, st_example_sheet
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


def test_scoreboard_page_and_precheck_queue(tmp_path):
    client = admin_client(tmp_path)
    login(client)
    board = client.get("/admin/scoreboard").text
    assert "ST. EXAMPLE MEDICAL CENTER" in board and "0%" in board and "needs re-check" in board
    queued = client.post("/admin/scoreboard/prechecks", follow_redirects=True)
    assert "Queued re-checks for 1 hospital" in queued.text
    assert "priority_recheck" in client.get("/admin/review").text
