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
    run_confirm,
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
    call it cannot pay for, call number `fail_at` raises a network error and call number
    `interrupt_at` a Ctrl-C. `during_search` runs inside every call, before it answers: what
    another session (an admin at the console, say) does while the search is in flight."""

    def __init__(
        self, hits=(), *, budget_at=None, fail_at=None, interrupt_at=None, during_search=None
    ):
        self.hits = list(hits)
        self.budget_at = budget_at
        self.fail_at = fail_at
        self.interrupt_at = interrupt_at
        self.during_search = during_search
        self.attempts = 0
        self.calls: list[tuple[str, dict]] = []

    def search(self, query, **kwargs):
        attempt, self.attempts = self.attempts, self.attempts + 1
        if self.during_search is not None:
            self.during_search()
        if attempt == self.interrupt_at:
            raise KeyboardInterrupt
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
            "220010", "MA", "confirmed", "harborview.org", 0.9, SURE_STORED.url, "harborview.org"
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


def test_a_sure_answer_naming_another_website_changes_nothing_and_asks_a_person():
    with session_scope(fresh_engine()) as session:
        row = seed(session, "220010")
        outcome = confirm_by_search(session, FakeGateway([SURE_OTHER]), row)
        assert outcome == SearchOutcome(
            "220010",
            "MA",
            "other_site",
            "harborviewhealth.org",
            0.9,
            SURE_OTHER.url,
            "harborview.org",
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
            "220010", "MA", "unconfirmed", "harborview.org", 0.6, GUESS_STORED.url, "harborview.org"
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
            assert outcome == SearchOutcome(
                "220010", "MA", "unconfirmed", None, None, None, "harborview.org"
            )
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


# A hospital changes while the run is on it. Sessions keep what they loaded (expire_on_commit is
# off), so the plan's row is stale as soon as an admin, a build or another run touches the
# hospital through another session; these tests use a database file and a second session.


def file_engine(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'race.db'}")
    init_db(engine)
    return engine


def change(engine, ccn, **fields):
    """What another session does to the hospital: set its columns, and commit."""
    with session_scope(engine) as other:
        row = repo.get_hospital(other, ccn)
        for name, value in fields.items():
            setattr(row, name, value)


def admin_confirms(engine, ccn):
    """The console's "Confirm website" (pipeline.confirm_domain): 1.0, and the question closed."""
    with session_scope(engine) as other:
        repo.get_hospital(other, ccn).domain_confidence = 1.0
        for item in repo.open_review_items(other, ccn, "domain_unconfirmed"):
            item.status = "resolved"


def admin_clears(engine, ccn):
    """The console's "Wrong website" (pipeline.reject_domain): no website, question closed."""
    change(engine, ccn, website_domain=None, domain_confidence=None)
    with session_scope(engine) as other:
        for item in repo.open_review_items(other, ccn, "domain_unconfirmed"):
            item.status = "resolved"


def confidence_set_alone(engine, ccn):
    """The confidence changes and the open question stays: only the hospital's row says so."""
    change(engine, ccn, domain_confidence=1.0)


def build_publishes(engine, ccn):
    """A rebuild publishes a newer version of the sheet."""
    with session_scope(engine) as other:
        newer = sheet_for(ccn, "MA", SheetStatus.PUBLISHED, "tiers").model_copy(
            update={"version": 2}
        )
        repo.add_sheet_version(other, newer, None)


def item_resolved(engine, ccn):
    with session_scope(engine) as other:
        for item in repo.open_review_items(other, ccn, "domain_unconfirmed"):
            item.status = "resolved"


def stored_state(engine, ccn):
    """The hospital as a fresh session reads it: (website, confidence)."""
    with session_scope(engine) as other:
        row = repo.get_hospital(other, ccn)
        return row.website_domain, row.domain_confidence


def searched_items(engine, ccn):
    with session_scope(engine) as other:
        return [
            (item.detail["outcome"], item.status)
            for item in repo.review_items_of_kind(other, ccn, WEBSITE_SEARCHED)
        ]


@pytest.mark.parametrize(
    "meanwhile",
    [admin_confirms, admin_clears, confidence_set_alone, build_publishes, item_resolved],
)
def test_a_hospital_that_stopped_waiting_since_the_plan_is_skipped_without_a_search(
    tmp_path, meanwhile
):
    engine = file_engine(tmp_path)
    with session_scope(engine) as session:
        seed(session, "220010")
    gateway = FakeGateway([SURE_STORED])
    with session_scope(engine) as session:
        [row] = candidates(session)
        meanwhile(engine, "220010")
        outcome = confirm_by_search(session, gateway, row)
        assert outcome == SearchOutcome("220010", "MA", "skipped", None, None, None)
    # No credit spent, nothing written over the other session's change, no record of a search.
    assert gateway.calls == []
    assert searched_items(engine, "220010") == []
    assert stored_state(engine, "220010")[1] != 0.9


def test_a_skipped_admin_confirmation_stays_at_one_point_zero(tmp_path):
    engine = file_engine(tmp_path)
    with session_scope(engine) as session:
        seed(session, "220010")
    with session_scope(engine) as session:
        [row] = candidates(session)
        admin_confirms(engine, "220010")
        confirm_by_search(session, FakeGateway([SURE_STORED]), row)
        assert row.domain_confidence == 1.0  # the row was read again, not left as planned
    assert stored_state(engine, "220010") == ("harborview.org", 1.0)


def test_the_search_compares_against_the_website_as_it_is_now_not_as_planned(tmp_path):
    engine = file_engine(tmp_path)
    with session_scope(engine) as session:
        seed(session, "220010")
    with session_scope(engine) as session:
        [row] = candidates(session)
        # Still unconfirmed, but the website moved to another site since the plan.
        change(engine, "220010", website_domain="harborviewhealth.org")
        outcome = confirm_by_search(session, FakeGateway([SURE_OTHER]), row)
        assert outcome.outcome == "confirmed" and outcome.found_domain == "harborviewhealth.org"
    assert stored_state(engine, "220010") == ("harborviewhealth.org", 0.9)


def test_a_sure_answer_for_the_planned_website_is_not_taken_for_the_moved_one(tmp_path):
    engine = file_engine(tmp_path)
    with session_scope(engine) as session:
        seed(session, "220010")
    with session_scope(engine) as session:
        [row] = candidates(session)
        change(engine, "220010", website_domain="harborviewhealth.org")
        outcome = confirm_by_search(session, FakeGateway([SURE_STORED]), row)
        assert outcome.outcome == "other_site"
        # What the CLI prints as "stored" is the website compared with, not the plan's.
        assert outcome.stored_domain == "harborviewhealth.org"
    assert stored_state(engine, "220010") == ("harborviewhealth.org", 0.6)
    with session_scope(engine) as session:
        [item] = repo.review_items_of_kind(session, "220010", WEBSITE_SEARCHED)
        assert item.detail["stored"] == "harborviewhealth.org"


def test_an_admin_confirming_during_the_search_is_not_overwritten_and_the_search_is_recorded(
    tmp_path,
):
    engine = file_engine(tmp_path)
    with session_scope(engine) as session:
        seed(session, "220010")
    gateway = FakeGateway([SURE_STORED], during_search=lambda: admin_confirms(engine, "220010"))
    with session_scope(engine) as session:
        [row] = candidates(session)
        outcome = confirm_by_search(session, gateway, row)
        # The search was paid for and its answer is reported, but nothing is written.
        assert outcome == SearchOutcome(
            "220010", "MA", "changed", "harborview.org", 0.9, SURE_STORED.url, "harborview.org"
        )
    assert len(gateway.calls) == 1
    assert stored_state(engine, "220010") == ("harborview.org", 1.0)
    assert searched_items(engine, "220010") == [("changed", "resolved")]


def test_the_website_changing_during_the_search_is_not_written_over(tmp_path):
    engine = file_engine(tmp_path)
    with session_scope(engine) as session:
        seed(session, "220010")
    gateway = FakeGateway(
        [SURE_STORED],
        during_search=lambda: change(engine, "220010", website_domain="harborviewhealth.org"),
    )
    with session_scope(engine) as session:
        [row] = candidates(session)
        outcome = confirm_by_search(session, gateway, row)
        assert outcome.outcome == "changed"
    assert stored_state(engine, "220010") == ("harborviewhealth.org", 0.6)
    assert searched_items(engine, "220010") == [("changed", "resolved")]


def test_a_run_skips_the_hospital_an_admin_confirmed_after_the_plan_and_pays_for_no_search(
    tmp_path,
):
    engine = file_engine(tmp_path)
    with session_scope(engine) as session:
        seed(session, "220010")
        seed(
            session,
            "220045",
            name="CEDAR GROVE HOSPITAL",
            city="WORCESTER",
            domain="cedargrove.org",
        )
    # While the first hospital is searched, an admin confirms the second.
    gateway = NamedGateway(during_search=lambda: admin_confirms(engine, "220045"))
    with session_scope(engine) as session:
        report = run_confirm(session, gateway, candidates(session))
    assert [(found.ccn, found.outcome) for found in report.outcomes] == [
        ("220010", "confirmed"),
        ("220045", "skipped"),
    ]
    assert report.confirmed == [("MA", "220010")]
    assert len(gateway.calls) == 1
    assert stored_state(engine, "220010") == ("harborview.org", 0.9)
    assert stored_state(engine, "220045") == ("cedargrove.org", 1.0)


# A commit that fails, or a run that is interrupted, must not lose a hospital that is already
# committed at 0.9 from the list the rebuilds read: it is no longer a candidate, and the list is
# its only road to a rebuild.


class FailsToCommit:
    """A session whose Nth commit raises, as a full disk or a lost connection does; everything
    else is the real session's."""

    def __init__(self, session, fail_on):
        self._session = session
        self._fail_on = fail_on
        self.commits = 0

    def commit(self):
        self.commits += 1
        if self.commits == self._fail_on:
            raise RuntimeError("could not commit")
        self._session.commit()

    def __getattr__(self, name):
        return getattr(self._session, name)


def test_a_failed_commit_ends_the_run_and_the_hospital_before_it_stays_in_the_out_file(tmp_path):
    engine = file_engine(tmp_path)
    with session_scope(engine) as session:
        seed(session, "220010")
        seed(session, "220011")  # the same name and website, so the same search confirms it
    out = tmp_path / "confirmed.txt"
    with session_scope(engine) as session:
        report = run_confirm(
            FailsToCommit(session, fail_on=2),
            FakeGateway([SURE_STORED]),
            candidates(session),
            out=out,
        )
    assert report.failed == "RuntimeError: could not commit"
    assert [found.ccn for found in report.outcomes] == ["220010"]
    assert report.confirmed == [("MA", "220010")]
    assert out.read_text(encoding="utf-8") == "MA 220010\n"
    assert stored_state(engine, "220010") == ("harborview.org", 0.9)  # committed
    assert stored_state(engine, "220011") == ("harborview.org", 0.6)  # rolled back
    assert searched_items(engine, "220011") == []


def test_each_confirmed_hospital_is_listed_as_soon_as_it_is_committed(tmp_path):
    engine = file_engine(tmp_path)
    with session_scope(engine) as session:
        seed(session, "220010")
        seed(session, "220011")
        seed(session, "220012")
    out = tmp_path / "var" / "confirmed.txt"
    out.parent.mkdir()
    out.write_text("RI 410001\n", encoding="utf-8")
    seen = []
    gateway = FakeGateway(
        [SURE_STORED],
        during_search=lambda: seen.append(out.read_text(encoding="utf-8")),
    )
    with session_scope(engine) as session:
        report = run_confirm(session, gateway, candidates(session), out=out)
    # What the file held when each search began: the earlier confirmations were already in it.
    assert seen == [
        "RI 410001\n",
        "MA 220010\nRI 410001\n",
        "MA 220010\nMA 220011\nRI 410001\n",
    ]
    assert report.confirmed == [("MA", "220010"), ("MA", "220011"), ("MA", "220012")]
    assert out.read_text(encoding="utf-8").splitlines() == [
        "MA 220010",
        "MA 220011",
        "MA 220012",
        "RI 410001",
    ]


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


def test_an_interrupt_keeps_the_hospitals_already_committed_and_listed_for_the_rebuilds(
    workdir, monkeypatch, tmp_path
):
    gateway = NamedGateway(interrupt_at=1)  # Ctrl-C during the second search
    tavily(monkeypatch, gateway)
    out = tmp_path / "confirmed.txt"
    result = CliRunner().invoke(app, ["atlas", "confirm-websites", "--run", "--out", str(out)])
    assert result.exit_code != 0  # Click turns Ctrl-C into an abort
    assert len(gateway.calls) == 1
    # 220010 is committed at 0.9, so it is no candidate any more; the list must already name it.
    assert confidences(workdir)["220010"] == 0.9
    assert out.read_text(encoding="utf-8") == "MA 220010\n"


def test_run_reports_the_hospital_that_changed_after_the_plan_and_was_not_searched(
    workdir, monkeypatch, tmp_path
):
    # While the first hospital is searched, an admin confirms Bayside's website.
    gateway = NamedGateway(during_search=lambda: admin_confirms(workdir, "410001"))
    tavily(monkeypatch, gateway)
    out = tmp_path / "confirmed.txt"
    assert invoke("--run", "--out", str(out)) == [
        PLAN.format(n=3),
        "searched 2 of 3 held sheets: 1 confirmed, 1 other site, 0 unconfirmed; "
        "1 skipped (changed since the plan, not searched); 0 Tavily credits spent",
        "other site: MA 220045: stored cedargrove.org, the search found cedargrovehealth.org",
        f"1 hospitals confirmed; 1 listed in {out} for the rebuilds",
    ]
    assert len(gateway.calls) == 2
    assert confidences(workdir)["410001"] == 1.0  # the admin's, untouched


def test_run_reports_the_hospital_that_changed_during_its_search_and_writes_nothing_for_it(
    workdir, monkeypatch, tmp_path
):
    # An admin confirms Harbor View's website while its search is in flight.
    gateway = NamedGateway(during_search=lambda: admin_confirms(workdir, "220010"))
    tavily(monkeypatch, gateway)
    out = tmp_path / "confirmed.txt"
    assert invoke("--run", "--out", str(out)) == [
        PLAN.format(n=3),
        "searched 3 of 3 held sheets: 0 confirmed, 1 other site, 1 unconfirmed; "
        "1 changed during the search (nothing written); 0 Tavily credits spent",
        "other site: MA 220045: stored cedargrove.org, the search found cedargrovehealth.org",
        f"0 hospitals confirmed; 0 listed in {out} for the rebuilds",
    ]
    assert confidences(workdir)["220010"] == 1.0
    assert out.read_text(encoding="utf-8") == ""


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


def test_run_that_cannot_write_the_list_says_so_and_exits_1(workdir, monkeypatch, tmp_path):
    tavily(monkeypatch, NamedGateway())
    folder = tmp_path / "a-folder"
    folder.mkdir()
    lines = invoke("--run", "--state", "AK", "--out", str(folder), status=1)
    assert lines[-1].startswith(f"could not write {folder}: ")
    assert folder.is_dir() and list(folder.iterdir()) == []


def test_run_without_a_tavily_key_says_so_before_anything_is_searched(workdir, tmp_path):
    # No gateway is faked here: the real factory refuses, as `atlas build` does without a key.
    result = CliRunner().invoke(app, ["atlas", "confirm-websites", "--run"])
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert "TAVILY_API_KEY" in str(result.exception)
    assert not (tmp_path / "usage.jsonl").exists()
    assert set(confidences(workdir).values()) == {0.6}
