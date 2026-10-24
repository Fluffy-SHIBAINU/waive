"""`waive atlas confirm-websites`: held sheets on unconfirmed websites, confirmed by Waive's own
discovery search (the speed-up plan of 2026-10-24).

Nothing here is paid and nothing reaches the network. The module tests use a gateway fake that
records its searches; the CLI tests run in a temporary folder with their own database, ledger and
skip list, so var/waive.db and data/ are never read, and build their gateway over a fake (or, for
the budget stop, the real TavilyGateway over a fake client and a governor on a temporary ledger)."""

import inspect
import json
from datetime import date
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from waive.atlas import repo
from waive.atlas.samples import SAMPLE_SOURCE_ID, st_example_sheet
from waive.atlas.schema import Cited, Eligibility, SheetStatus
from waive.atlas.skiplist import SkipEntry
from waive.atlas.tavily_gateway import SearchHit, TavilyGateway
from waive.atlas.website_confirm import (
    SEARCH_DEPTH,
    WEBSITE_SEARCHED,
    SearchOutcome,
    candidates,
    confirm_by_search,
)
from waive.cli import app
from waive.db import init_db, make_engine, session_scope
from waive.governor import BudgetExceeded

HOSPITAL = {
    "ccn": "220010",
    "name": "HARBOR VIEW HOSPITAL",
    "city": "BOSTON",
    "state": "MA",
    "zip": "02118",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
}


def hit(url, title="", content="", score=0.5):
    return SearchHit(url=url, title=title, content=content, score=score)


# The hits discovery's pick_domain scores for Harbor View Hospital in Boston, MA. A title that
# names the hospital and a hit that names its place make a site sure (0.9); a title with no place
# is a guess (0.6); a page that only mentions the hospital in its body is worth less still.
SURE_STORED = hit(
    "https://www.harborview.org/financial-assistance",
    "Financial Assistance | Harbor View Hospital, Boston, MA",
    score=0.8,
)
SURE_OTHER = hit(
    "https://www.harborviewhealth.org/billing",
    "Billing | Harbor View Hospital, Boston, MA",
    score=0.8,
)
GUESS_STORED = hit(
    "https://www.harborview.org/financial-assistance", "Financial Assistance | Harbor View Hospital"
)
MENTION_STORED = hit(
    "https://www.harborview.org/news/foundation",
    "Foundation news",
    "Harbor View Hospital in Boston, MA thanks its donors",
)


class FakeGateway:
    """Answers `search` from a list of hits and records every call as (query, kwargs). Call
    number `budget_at` (counting from zero) raises BudgetExceeded, as the governor does before a
    call it cannot pay for, and call number `fail_at` raises a network error."""

    def __init__(self, hits=(), *, budget_at=None, fail_at=None):
        self.hits = list(hits)
        self.budget_at = budget_at
        self.fail_at = fail_at
        self.attempts = 0
        self.calls: list[tuple[str, dict]] = []

    def search(self, query, **kwargs):
        attempt, self.attempts = self.attempts, self.attempts + 1
        if attempt == self.budget_at:
            raise BudgetExceeded("Tavily cap of 2 credits would be exceeded (used 2).")
        if attempt == self.fail_at:
            raise TimeoutError("Tavily did not answer")
        self.calls.append((query, kwargs))
        return self.answer(query)

    def answer(self, query):
        return list(self.hits)


def sheet_for(ccn, state, status, rule):
    """St. Example's sheet as `ccn`'s, with the income rule `rule` says: "tiers" (a free-care band
    and a discount table), "ceiling" (an assistance ceiling alone) or "none"."""
    base = st_example_sheet()
    if rule == "ceiling":
        eligibility = Eligibility(
            assistance_ceiling_fpl=Cited[Decimal](
                value=Decimal("300"),
                quote="up to 300% of the Federal Poverty Guidelines may qualify",
                source_id=SAMPLE_SOURCE_ID,
                checked_on=date(2026, 10, 2),
            )
        )
    elif rule == "none":
        eligibility = Eligibility()
    else:
        eligibility = base.eligibility
    hospital = base.hospital.model_copy(update={"ccn": ccn, "state": state})
    return base.model_copy(
        update={"hospital": hospital, "status": status, "eligibility": eligibility}
    )


def seed(
    session,
    ccn,
    *,
    state="MA",
    name="HARBOR VIEW HOSPITAL",
    city="BOSTON",
    domain="harborview.org",
    confidence=0.6,
    status=SheetStatus.HELD,
    rule="tiers",
    item=True,
):
    """A hospital with its website and confidence, a sheet in `status`, and an open
    `domain_unconfirmed` item when `item`."""
    row = repo.upsert_hospital(
        session,
        {
            **HOSPITAL,
            "ccn": ccn,
            "state": state,
            "name": name,
            "city": city,
            "website_domain": domain,
            "domain_confidence": confidence,
        },
    )
    if status is not None:
        repo.add_sheet_version(session, sheet_for(ccn, state, status, rule), None)
    if item:
        repo.add_review_item(
            session, ccn, "domain_unconfirmed", {"domain": domain, "confidence": confidence}
        )
    return row


def fresh_engine():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return engine


def ccns(rows):
    return [row.ccn for row in rows]


# candidates


def test_candidates_lists_a_held_sheet_with_an_income_rule_on_an_unconfirmed_website():
    with session_scope(fresh_engine()) as session:
        seed(session, "220010")
        assert ccns(candidates(session)) == ["220010"]


def test_candidates_leaves_out_every_hospital_that_is_not_waiting_on_its_website():
    with session_scope(fresh_engine()) as session:
        seed(session, "220010")  # listed
        seed(session, "220011", rule="none")  # held, but no income rule to publish
        seed(session, "220012", confidence=0.9)  # sure already
        seed(session, "220013", confidence=1.0)  # an admin confirmed it
        seed(session, "220014", item=False)  # nothing asked an admin about the website
        seed(session, "220015", status=SheetStatus.PUBLISHED)
        seed(session, "220016", confidence=None)  # no confidence recorded
        seed(session, "220017", domain=None)  # no website to confirm
        seed(session, "220018", status=SheetStatus.DRAFT)  # an Eve draft, not held
        seed(session, "220019", status=None)  # no sheet at all
        seed(session, "220020")  # skip-listed
        skip = {"220020": SkipEntry("220020", "MA", "SKIPPED GENERAL", "site blocks")}
        assert ccns(candidates(session, skip=skip)) == ["220010"]
        assert "220020" in ccns(candidates(session))  # only the skip list left it out


def test_candidates_leaves_out_a_hospital_whose_open_item_was_resolved():
    with session_scope(fresh_engine()) as session:
        seed(session, "220010")
        seed(session, "220011")
        for item in repo.open_review_items(session, ccn="220011", kind="domain_unconfirmed"):
            repo.set_review_status(session, item.id, "resolved")
        assert ccns(candidates(session)) == ["220010"]


@pytest.mark.parametrize("rule", ["tiers", "ceiling"])
def test_candidates_counts_a_free_care_band_a_discount_table_or_a_ceiling_as_an_income_rule(rule):
    with session_scope(fresh_engine()) as session:
        seed(session, "220010", rule=rule)
        assert ccns(candidates(session)) == ["220010"]


def test_candidates_reads_the_latest_version_of_the_sheet():
    with session_scope(fresh_engine()) as session:
        seed(session, "220010", status=SheetStatus.PUBLISHED)  # version 1
        newer = sheet_for("220010", "MA", SheetStatus.HELD, "tiers").model_copy(
            update={"version": 2}
        )
        repo.add_sheet_version(session, newer, None)
        seed(session, "220011", status=SheetStatus.HELD)  # version 1 held ...
        published = sheet_for("220011", "MA", SheetStatus.PUBLISHED, "tiers").model_copy(
            update={"version": 2}
        )
        repo.add_sheet_version(session, published, None)  # ... and version 2 published
        assert ccns(candidates(session)) == ["220010"]


def test_candidates_are_ordered_by_state_then_ccn_and_filtered_by_state():
    with session_scope(fresh_engine()) as session:
        seed(session, "410001", state="RI", name="BAYSIDE HOSPITAL", city="PROVIDENCE")
        seed(session, "220045", name="CEDAR GROVE HOSPITAL", city="WORCESTER")
        seed(session, "220010")
        assert ccns(candidates(session)) == ["220010", "220045", "410001"]
        assert ccns(candidates(session, states=["RI"])) == ["410001"]
        assert ccns(candidates(session, states=["ri", "MA"])) == ["220010", "220045", "410001"]
        assert candidates(session, states=["AK"]) == []


# confirm_by_search


def only_item(session, ccn):
    items = repo.review_items_of_kind(session, ccn, WEBSITE_SEARCHED)
    assert len(items) == 1
    return items[0]


def test_a_sure_answer_naming_the_stored_website_confirms_it_at_the_searchs_own_confidence():
    gateway = FakeGateway([SURE_STORED])
    with session_scope(fresh_engine()) as session:
        row = seed(session, "220010")
        outcome = confirm_by_search(session, gateway, row)
        assert outcome == SearchOutcome(
            "220010", "MA", "confirmed", "harborview.org", 0.9, SURE_STORED.url
        )
        assert (row.website_domain, row.domain_confidence) == ("harborview.org", 0.9)
        item = only_item(session, "220010")
        assert item.detail == {
            "domain": "harborview.org",
            "confidence": 0.9,
            "evidence_url": SURE_STORED.url,
            "outcome": "confirmed",
        }
    # One search, through discovery's own query and purpose, at the gateway's default depth.
    assert len(gateway.calls) == 1
    query, kwargs = gateway.calls[0]
    assert query == "HARBOR VIEW HOSPITAL BOSTON MA hospital financial assistance"
    assert kwargs["purpose"] == "atlas.discover" and "depth" not in kwargs


def test_the_search_is_planned_at_the_depth_discovery_uses():
    assert inspect.signature(TavilyGateway.search).parameters["depth"].default == SEARCH_DEPTH


def test_the_stored_website_may_be_written_with_www_and_the_hit_on_a_subdomain():
    sure_on_subdomain = hit(
        "https://billing.harborview.org/assistance",
        "Financial Assistance | Harbor View Hospital, Boston, MA",
    )
    with session_scope(fresh_engine()) as session:
        row = seed(session, "220010", domain="www.harborview.org")
        outcome = confirm_by_search(session, FakeGateway([sure_on_subdomain]), row)
        assert outcome.outcome == "confirmed"
        assert (row.website_domain, row.domain_confidence) == ("www.harborview.org", 0.9)


def test_a_search_never_lowers_a_confidence_an_admin_set():
    with session_scope(fresh_engine()) as session:
        row = seed(session, "220010", confidence=1.0)
        outcome = confirm_by_search(session, FakeGateway([SURE_STORED]), row)
        assert outcome.outcome == "confirmed"
        assert row.domain_confidence == 1.0


def test_a_sure_answer_naming_another_website_changes_nothing_and_asks_a_person():
    with session_scope(fresh_engine()) as session:
        row = seed(session, "220010")
        outcome = confirm_by_search(session, FakeGateway([SURE_OTHER]), row)
        assert outcome == SearchOutcome(
            "220010", "MA", "other_site", "harborviewhealth.org", 0.9, SURE_OTHER.url
        )
        assert (row.website_domain, row.domain_confidence) == ("harborview.org", 0.6)
        item = only_item(session, "220010")
        assert item.detail == {
            "outcome": "other_site",
            "domain": "harborviewhealth.org",
            "confidence": 0.9,
            "evidence_url": SURE_OTHER.url,
            "stored": "harborview.org",
        }
        assert item.status == "open"  # for a person
        assert repo.open_review_items(session, "220010", "domain_unconfirmed")  # still asked


def test_a_guess_at_the_stored_website_is_not_a_confirmation():
    with session_scope(fresh_engine()) as session:
        row = seed(session, "220010")
        outcome = confirm_by_search(session, FakeGateway([GUESS_STORED]), row)
        assert outcome == SearchOutcome(
            "220010", "MA", "unconfirmed", "harborview.org", 0.6, GUESS_STORED.url
        )
        assert (row.website_domain, row.domain_confidence) == ("harborview.org", 0.6)
        assert only_item(session, "220010").detail["outcome"] == "unconfirmed"


def test_a_hit_that_only_mentions_the_hospital_is_not_a_confirmation():
    with session_scope(fresh_engine()) as session:
        row = seed(session, "220010")
        outcome = confirm_by_search(session, FakeGateway([MENTION_STORED]), row)
        assert outcome.outcome == "unconfirmed" and outcome.confidence == 0.6
        assert (row.website_domain, row.domain_confidence) == ("harborview.org", 0.6)
        item = only_item(session, "220010")
        assert item.detail["outcome"] == "unconfirmed"
        assert item.status == "resolved"  # a record of the search, not work for the queue


def test_a_search_with_no_usable_hit_is_unconfirmed_and_says_so():
    directory = hit("https://www.healthgrades.com/hospital/harbor-view", "Harbor View Hospital")
    with session_scope(fresh_engine()) as session:
        row = seed(session, "220010")
        for hits in ([], [directory]):
            outcome = confirm_by_search(session, FakeGateway(hits), row)
            assert outcome == SearchOutcome("220010", "MA", "unconfirmed", None, None, None)
        assert (row.website_domain, row.domain_confidence) == ("harborview.org", 0.6)
        items = repo.review_items_of_kind(session, "220010", WEBSITE_SEARCHED)
        assert [item.detail["outcome"] for item in items] == ["unconfirmed", "unconfirmed"]
        assert items[0].detail["domain"] is None


def test_a_site_an_admin_rejected_is_never_taken_for_the_hospitals():
    # The rejected site outscores the stored one; without the rejection it would be the answer.
    rejected = hit(SURE_OTHER.url, SURE_OTHER.title, score=0.95)
    with session_scope(fresh_engine()) as session:
        row = seed(session, "220010")
        repo.add_review_item(session, "220010", "domain", {"cleared": "harborviewhealth.org"})
        outcome = confirm_by_search(session, FakeGateway([rejected, SURE_STORED]), row)
        assert outcome.outcome == "confirmed" and outcome.found_domain == "harborview.org"


def test_the_budget_stopping_a_search_propagates_and_leaves_the_hospital_alone():
    with session_scope(fresh_engine()) as session:
        row = seed(session, "220010")
        with pytest.raises(BudgetExceeded):
            confirm_by_search(session, FakeGateway(budget_at=0), row)
        assert (row.website_domain, row.domain_confidence) == ("harborview.org", 0.6)
        assert repo.review_items_of_kind(session, "220010", WEBSITE_SEARCHED) == []


# The command

BLOCKED = "site blocks server fetches (403)"
HARBOR = SURE_STORED
CEDAR = hit(
    "https://www.cedargrovehealth.org/", "Cedar Grove Hospital, Worcester, MA", score=0.8
)  # sure, but of a site other than the stored cedargrove.org
BAYSIDE = hit(
    "https://www.baysidecommunity.org/news",
    "News",
    "Bayside Community Hospital in Providence, RI opened a clinic",
)  # only a mention
ANSWERS = {
    "HARBOR VIEW HOSPITAL": [HARBOR],
    "CEDAR GROVE HOSPITAL": [CEDAR],
    "BAYSIDE COMMUNITY HOSPITAL": [BAYSIDE],
}
PLAN = (
    "website search plan: {n} held sheets on unconfirmed websites; "
    "about {n} Tavily credits (one search each)"
)


class NamedGateway(FakeGateway):
    """Answers each hospital's search from `ANSWERS` by the name its query starts with; a search
    for any other hospital (the skip-listed one, say) fails the test."""

    def answer(self, query):
        for name, hits in ANSWERS.items():
            if query.startswith(name):
                return list(hits)
        raise AssertionError(f"searched for a hospital nobody expected: {query}")


class FakeTavilyClient:
    """The client TavilyGateway wraps: Search answers from `ANSWERS` in the API's own shape."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def search(self, query, **kwargs):
        self.calls.append((query, kwargs["search_depth"]))
        hits = next(hits for name, hits in ANSWERS.items() if query.startswith(name))
        results = [
            {"url": h.url, "title": h.title, "content": h.content, "score": h.score} for h in hits
        ]
        return {"results": results}


@pytest.fixture
def workdir(monkeypatch, tmp_path):
    """A temporary working folder with a local database, a usage ledger path and a skip list, all
    its own; no .env and no Tavily key. Three hospitals wait on their websites (Harbor View, MA,
    whose search confirms it; Cedar Grove, MA, whose search names another site; Bayside, RI, whose
    search only mentions it), a fourth would but is on the skip list, and a fifth is published."""
    monkeypatch.chdir(tmp_path)
    url = f"sqlite:///{tmp_path / 'local.db'}"
    monkeypatch.setenv("WAIVE_DATABASE_URL", url)
    monkeypatch.setenv("WAIVE_LEDGER_PATH", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("WAIVE_SCOUT_SKIP_LIST", str(tmp_path / "skip.json"))
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    (tmp_path / "skip.json").write_text(
        json.dumps(
            [
                {
                    "ccn": "220031",
                    "state": "MA",
                    "name": "SKIPPED GENERAL HOSPITAL",
                    "reason": BLOCKED,
                }
            ]
        ),
        encoding="utf-8",
    )
    engine = make_engine(url)
    init_db(engine)
    with session_scope(engine) as session:
        seed(session, "220010")
        seed(
            session,
            "220045",
            name="CEDAR GROVE HOSPITAL",
            city="WORCESTER",
            domain="cedargrove.org",
        )
        seed(
            session,
            "410001",
            state="RI",
            name="BAYSIDE COMMUNITY HOSPITAL",
            city="PROVIDENCE",
            domain="baysidecommunity.org",
        )
        seed(session, "220031", name="SKIPPED GENERAL HOSPITAL")
        seed(session, "220050", name="PUBLISHED HOSPITAL", status=SheetStatus.PUBLISHED)
    yield engine
    engine.dispose()


def invoke(*args: str, status: int = 0):
    result = CliRunner().invoke(app, ["atlas", "confirm-websites", *args])
    assert result.exit_code == status, result.output
    return result.output.splitlines()


def tavily(monkeypatch, gateway):
    """Make `--run` use `gateway` as its Tavily gateway; returns the list of governors it was
    handed (one entry per gateway made)."""
    built = []

    def make(settings, governor):
        built.append(governor)
        return gateway

    monkeypatch.setattr("waive.cli.make_tavily_gateway", make)
    return built


def confidences(engine):
    with session_scope(engine) as session:
        return {row.ccn: row.domain_confidence for row in repo.list_hospitals(session)}


def test_the_dry_run_prints_the_plan_and_spends_nothing(workdir, monkeypatch, tmp_path):
    gateway = NamedGateway()
    built = tavily(monkeypatch, gateway)
    # Neither the default nor --dry-run builds a gateway, searches, or touches the ledger; the
    # skip-listed and the published hospital are not in the plan.
    assert invoke() == [PLAN.format(n=3)]
    assert invoke("--dry-run") == [PLAN.format(n=3)]
    assert built == [] and gateway.calls == []
    assert not (tmp_path / "usage.jsonl").exists()
    assert not (tmp_path / "var").exists()
    assert set(confidences(workdir).values()) == {0.6}
    with session_scope(workdir) as session:
        assert repo.open_review_items(session, kind=WEBSITE_SEARCHED) == []
        assert repo.review_items_of_kind(session, "220010", WEBSITE_SEARCHED) == []


def test_the_plan_honours_state_and_limit(workdir):
    assert invoke("--state", "ri") == [PLAN.format(n=1)]
    assert invoke("--state", "MA") == [PLAN.format(n=2)]
    assert invoke("--limit", "2") == [PLAN.format(n=2)]
    assert invoke("--state", "AK") == [PLAN.format(n=0)]


def test_run_searches_each_candidate_in_order_and_writes_the_confirmed_hospitals(
    workdir, monkeypatch, tmp_path
):
    gateway = NamedGateway()
    built = tavily(monkeypatch, gateway)
    out = tmp_path / "confirmed.txt"
    lines = invoke("--run", "--out", str(out))
    assert lines == [
        PLAN.format(n=3),
        "searched 3 of 3 held sheets: 1 confirmed, 1 other site, 1 unconfirmed; "
        "0 Tavily credits spent",
        "other site: MA 220045: stored cedargrove.org, the search found cedargrovehealth.org",
        f"1 hospitals confirmed; 1 listed in {out} for the rebuilds",
    ]
    assert len(built) == 1
    # One search each, state then CCN; the skip-listed hospital is never searched.
    assert [query.split(" hospital financial")[0] for query, _ in gateway.calls] == [
        "HARBOR VIEW HOSPITAL BOSTON MA",
        "CEDAR GROVE HOSPITAL WORCESTER MA",
        "BAYSIDE COMMUNITY HOSPITAL PROVIDENCE RI",
    ]
    assert out.read_text(encoding="utf-8") == "MA 220010\n"
    # Committed: a new session sees the confirmation and the records of all three searches.
    assert confidences(workdir) == {
        "220010": 0.9,
        "220045": 0.6,
        "410001": 0.6,
        "220031": 0.6,
        "220050": 0.6,
    }
    with session_scope(workdir) as session:
        outcomes = {
            ccn: [
                item.detail["outcome"]
                for item in repo.review_items_of_kind(session, ccn, WEBSITE_SEARCHED)
            ]
            for ccn in ("220010", "220045", "410001", "220031")
        }
    assert outcomes == {
        "220010": ["confirmed"],
        "220045": ["other_site"],
        "410001": ["unconfirmed"],
        "220031": [],
    }


def test_run_writes_var_websites_confirmed_by_default_and_adds_to_what_it_found_there(
    workdir, monkeypatch, tmp_path
):
    tavily(monkeypatch, NamedGateway())
    default = tmp_path / "var" / "websites-confirmed.txt"
    default.parent.mkdir()
    default.write_text("RI 410001\nstray\nMA 220010 extra\n", encoding="utf-8")
    lines = invoke("--run")
    assert lines[-1] == (
        "1 hospitals confirmed; 2 listed in var/websites-confirmed.txt for the rebuilds"
    )
    assert default.read_text(encoding="utf-8") == "MA 220010\nRI 410001\n"
    # 220010 is confirmed now, so a second run leaves it out and finds nothing new to list.
    tavily(monkeypatch, NamedGateway())
    again = invoke("--run")
    assert again[0] == PLAN.format(n=2)
    assert again[-1] == (
        "0 hospitals confirmed; 2 listed in var/websites-confirmed.txt for the rebuilds"
    )
    assert default.read_text(encoding="utf-8") == "MA 220010\nRI 410001\n"


def test_run_honours_state_and_limit(workdir, monkeypatch, tmp_path):
    gateway = NamedGateway()
    tavily(monkeypatch, gateway)
    invoke("--run", "--state", "ma", "--limit", "1", "--out", str(tmp_path / "out.txt"))
    assert [query.split(" ")[0] for query, _ in gateway.calls] == ["HARBOR"]


def test_run_stops_cleanly_at_the_credit_cap_and_keeps_what_was_confirmed(
    workdir, monkeypatch, tmp_path
):
    monkeypatch.setenv("WAIVE_TAVILY_CREDIT_CAP", "2")
    client = FakeTavilyClient()

    def make(settings, governor):
        return TavilyGateway(client, governor)

    monkeypatch.setattr("waive.cli.make_tavily_gateway", make)
    out = tmp_path / "confirmed.txt"
    lines = invoke("--run", "--out", str(out))
    assert lines == [
        PLAN.format(n=3),
        "searched 2 of 3 held sheets: 1 confirmed, 1 other site, 0 unconfirmed; "
        "2 Tavily credits spent",
        "other site: MA 220045: stored cedargrove.org, the search found cedargrovehealth.org",
        "stopped: Tavily cap of 2 credits would be exceeded (used 2). The results so far are kept.",
        f"1 hospitals confirmed; 1 listed in {out} for the rebuilds",
    ]
    assert [depth for _, depth in client.calls] == ["basic", "basic"]  # the planned depth
    assert out.read_text(encoding="utf-8") == "MA 220010\n"
    assert confidences(workdir)["220010"] == 0.9  # committed
    assert confidences(workdir)["410001"] == 0.6  # never searched
    with session_scope(workdir) as session:
        assert repo.review_items_of_kind(session, "410001", WEBSITE_SEARCHED) == []
    ledger = (tmp_path / "usage.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["purpose"] for line in ledger] == ["atlas.discover"] * 2


def test_run_that_fails_on_another_error_writes_what_it_confirmed_and_exits_1(
    workdir, monkeypatch, tmp_path
):
    gateway = NamedGateway(fail_at=1)  # the second search times out
    tavily(monkeypatch, gateway)
    out = tmp_path / "confirmed.txt"
    lines = invoke("--run", "--out", str(out), status=1)
    assert lines == [
        PLAN.format(n=3),
        "searched 1 of 3 held sheets: 1 confirmed, 0 other site, 0 unconfirmed; "
        "0 Tavily credits spent",
        "failed: TimeoutError: Tavily did not answer. The results so far are kept.",
        f"1 hospitals confirmed; 1 listed in {out} for the rebuilds",
    ]
    assert len(gateway.calls) == 1  # the run stopped there
    assert out.read_text(encoding="utf-8") == "MA 220010\n"
    assert confidences(workdir)["220010"] == 0.9  # committed before the failure
    # The rerun the failure asks for plans only what is still unconfirmed.
    tavily(monkeypatch, NamedGateway())
    assert invoke("--run", "--out", str(out))[0] == PLAN.format(n=2)


def test_run_with_nothing_to_search_builds_no_gateway_and_still_writes_the_list(
    workdir, monkeypatch, tmp_path
):
    built = tavily(monkeypatch, NamedGateway())
    lines = invoke("--run", "--state", "AK", "--out", "none.txt")
    assert lines == [
        PLAN.format(n=0),
        "searched 0 of 0 held sheets: 0 confirmed, 0 other site, 0 unconfirmed; "
        "0 Tavily credits spent",
        "0 hospitals confirmed; 0 listed in none.txt for the rebuilds",
    ]
    assert built == []
    assert (tmp_path / "none.txt").read_text(encoding="utf-8") == ""


def test_run_without_a_tavily_key_says_so_before_anything_is_searched(workdir, tmp_path):
    # No gateway is faked here: the real factory refuses, as `atlas build` does without a key.
    result = CliRunner().invoke(app, ["atlas", "confirm-websites", "--run"])
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert "TAVILY_API_KEY" in str(result.exception)
    assert not (tmp_path / "usage.jsonl").exists()
    assert set(confidences(workdir).values()) == {0.6}
