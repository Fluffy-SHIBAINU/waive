"""`waive atlas eve-refetch`: the Eve sheets' missing source documents, fetched once through Tavily
Extract (task 7.12d).

Nothing here is paid and nothing reaches the network. The module tests use a gateway fake that
records its calls, or the real TavilyGateway over a fake client and a governor on a temporary
ledger. The CLI tests run in a temporary folder with their own database, ledger and skip list, so
var/waive.db and data/ are never read."""

import hashlib
import json
from datetime import date
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from waive.atlas import repo
from waive.atlas.eve_refetch import (
    NAVIGATION_SHELL,
    NOT_ABOUT_ASSISTANCE,
    NOT_REQUESTED,
    NOT_RETURNED,
    TOO_SHORT,
    MissingEntry,
    load_missing,
    merge_refetched,
    plan_refetch,
    read_refetched,
    run_refetch,
)
from waive.atlas.samples import SAMPLE_POLICY_TEXT
from waive.atlas.schema import SourceDoc, SourceKind
from waive.atlas.scout import MAX_CHARS, MIN_CHARS, THIN_CHARS, source_id_for
from waive.atlas.skiplist import SkipEntry
from waive.atlas.tavily_gateway import ExtractedPage, TavilyGateway
from waive.cli import app
from waive.db import init_db, make_engine, session_scope
from waive.governor import BudgetExceeded, Governor, Ledger

TODAY = date(2026, 10, 24)
IMPORTED = date(2026, 10, 23)
PURPOSE = "atlas.eve_refetch"
HOSPITAL = {
    "ccn": "220010",
    "name": "NEW HOSPITAL",
    "city": "BOSTON",
    "state": "MA",
    "zip": "02118",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
}

# A short page whose prose names financial assistance, and a short site menu that only links to
# it: the scout keeps the first and sets the second aside (scout._usable_download).
POLICY = SAMPLE_POLICY_TEXT
MENU = (
    "[Skip to Content](#Content)\n\n[Set Your Location](#SetLocation)\n\n"
    "Set your location to see the providers and services near you.\n\n"
    "What can we help you find?\n\n* [Find a doctor](/our-providers/)\n"
    "* [Find a location](/locations-and-directions/)\n* [Browse our services](/medical-services/)\n"
    "* [Financial Assistance](/patients/financial-assistance/)\n"
    "* [Pay a bill](/patients-visitors/pay-a-bill/)\n* [Careers](/careers/)\n"
)
LONG_PAGE = MENU + "We offer emergency care, surgery, imaging and maternity services. " * 16
PDF_TEXT = "Instructions for completing this form in blue or black ink. " * 6
SHORT = "Page not found."
assert MIN_CHARS <= len(POLICY) < THIN_CHARS and MIN_CHARS <= len(MENU) < THIN_CHARS
assert len(LONG_PAGE) >= THIN_CHARS and MIN_CHARS <= len(PDF_TEXT) < THIN_CHARS
assert len(SHORT) < MIN_CHARS


def policy(n: int) -> str:
    """A page that names financial assistance and differs from every other (n)."""
    return f"{POLICY}\nPage {n}.\n"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def urls(count: int, start: int = 0) -> list[str]:
    return [f"https://www.example.org/policies/{n}" for n in range(start, start + count)]


def fresh_engine():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return engine


def hospitals(session, *ccns: str, state: str = "MA") -> None:
    for ccn in ccns:
        repo.upsert_hospital(
            session, {**HOSPITAL, "ccn": ccn, "name": f"HOSPITAL {ccn}", "state": state}
        )


def stored(
    session, ccn: str, url: str, *, text: str = "", title: str = "Financial Assistance Policy"
) -> SourceDoc:
    """A source the Eve import left behind: the cited URL with a title, and text only when the
    download worked. One document per URL, as the import keeps it (hospitals share its row)."""
    doc = SourceDoc(
        id="eve-" + sha(url)[:16],
        kind=SourceKind.HOSPITAL_WEB,
        url=url,
        title=title,
        fetched_on=IMPORTED,
        sha256=sha(text),
    )
    repo.save_source(session, doc, text, ccn)
    return doc


def entry(ccn: str, *missing: str, state: str = "MA") -> MissingEntry:
    return MissingEntry(ccn, state, f"HOSPITAL {ccn}", tuple(missing), 0, len(missing))


def texts_of(session, ccn: str) -> dict[str, str]:
    """The text of each stored source of a hospital with any, by URL."""
    return {source.url: text for source, text in repo.sources_for(session, ccn) if text}


class FakeGateway:
    """Answers `extract` from a table of texts and records every call it answers as
    (urls, purpose, depth). A URL missing from the table is one Tavily returned no page for.
    Call number `budget_at` (counting from zero) raises BudgetExceeded, as the governor does before
    a call it cannot pay for, and call number `fail_at` raises a network error; later calls are
    answered, so a run that went on after either would show it."""

    def __init__(self, texts, *, budget_at=None, fail_at=None):
        self.texts = texts
        self.budget_at = budget_at
        self.fail_at = fail_at
        self.attempts = 0
        self.calls: list[tuple[list[str], str, str]] = []

    def extract(self, urls, *, purpose, depth="basic"):
        attempt, self.attempts = self.attempts, self.attempts + 1
        if attempt == self.budget_at:
            raise BudgetExceeded("Tavily cap of 2 credits would be exceeded (used 2).")
        if attempt == self.fail_at:
            raise TimeoutError("Tavily did not answer")
        self.calls.append((list(urls), purpose, depth))
        return [ExtractedPage(url, self.texts[url]) for url in urls if url in self.texts]


class FakeTavilyClient:
    """The client TavilyGateway wraps: Extract answers from a table in the API's own shape. Call
    number `fail_at` (counting from zero) raises a network error before it answers."""

    def __init__(self, texts, *, fail_at=None):
        self.texts = texts
        self.fail_at = fail_at
        self.attempts = 0
        self.calls: list[tuple[list[str], str]] = []

    def extract(self, urls, **kwargs):
        attempt, self.attempts = self.attempts, self.attempts + 1
        if attempt == self.fail_at:
            raise TimeoutError("Tavily did not answer")
        self.calls.append((list(urls), kwargs["extract_depth"]))
        results = [{"url": u, "raw_content": self.texts[u]} for u in urls if u in self.texts]
        return {"results": results}


# load_missing


def test_load_missing_reads_the_entries(tmp_path):
    path = tmp_path / "_missing-sources.json"
    path.write_text(
        json.dumps(
            [
                {
                    "ccn": "040071",
                    "state": "AR",
                    "name": "JEFFERSON REGIONAL MEDICAL CENTER",
                    "missing_urls": ["https://www.jrmc.org/application.pdf"],
                    "fetched": 1,
                    "total": 2,
                },
                {
                    "ccn": "041334",
                    "state": "AR",
                    "name": "MENA REGIONAL HEALTH SYSTEM",
                    "missing_urls": [
                        "https://menaregional.com/a.pdf",
                        "https://menaregional.com/b/",
                    ],
                    "fetched": 1,
                    "total": 3,
                },
            ]
        ),
        encoding="utf-8",
    )
    assert load_missing(path) == [
        MissingEntry(
            "040071",
            "AR",
            "JEFFERSON REGIONAL MEDICAL CENTER",
            ("https://www.jrmc.org/application.pdf",),
            1,
            2,
        ),
        MissingEntry(
            "041334",
            "AR",
            "MENA REGIONAL HEALTH SYSTEM",
            ("https://menaregional.com/a.pdf", "https://menaregional.com/b/"),
            1,
            3,
        ),
    ]


def test_load_missing_without_the_file_is_an_empty_list(tmp_path):
    # The container image carries no data/ folder.
    assert load_missing(tmp_path / "_missing-sources.json") == []


def test_load_missing_ignores_rows_that_name_no_hospital_and_refuses_a_file_that_is_no_list(
    tmp_path,
):
    path = tmp_path / "missing.json"
    row = {"ccn": "040071", "state": "AR", "name": "JRMC", "missing_urls": ["https://x.org/a"]}
    path.write_text(json.dumps([row, "stray", {"state": "AR", "missing_urls": []}]))
    assert [found.ccn for found in load_missing(path)] == ["040071"]
    # A file in another shape is an error, not an empty list that quietly refetches nothing.
    path.write_text(json.dumps({"040071": row}))
    with pytest.raises(ValueError, match="expected a JSON list"):
        load_missing(path)


# plan_refetch


def test_plan_fetches_a_url_listed_by_two_hospitals_once_and_both_want_it():
    shared, only_first = urls(2)
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010", "220031")
        for ccn in ("220010", "220031"):
            stored(session, ccn, shared)
        stored(session, "220010", only_first)
        plan = plan_refetch(
            session, [entry("220010", shared, only_first), entry("220031", shared)], {}
        )
    assert plan.urls == [shared, only_first]  # unique, in the order they were first listed
    assert plan.wanted_by == {shared: ["220010", "220031"], only_first: ["220010"]}
    assert plan.ccns == ["220010", "220031"]
    assert (plan.pairs, plan.already_have_text, plan.skipped_ccns) == (3, 0, 0)


def test_plan_drops_a_pair_whose_stored_source_already_has_text_and_counts_it():
    have, empty, never_stored, other_url = urls(4)
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010", "220031")
        stored(session, "220010", have, text=POLICY)
        stored(session, "220010", empty)
        # Text stored at a different address is not this URL's text.
        stored(session, "220010", other_url + "/", text=POLICY)
        # Another hospital's text for the same address is not this hospital's.
        stored(session, "220031", never_stored, text=POLICY)
        plan = plan_refetch(
            session,
            [
                entry("220010", have, empty, never_stored, other_url),
                entry("220031", never_stored),
            ],
            {},
        )
    assert plan.urls == [empty, never_stored, other_url]
    assert plan.wanted_by == {
        empty: ["220010"],
        never_stored: ["220010"],
        other_url: ["220010"],
    }
    assert (plan.pairs, plan.already_have_text) == (5, 2)


def test_plan_drops_a_skip_listed_hospital_and_counts_it():
    wanted_by_both, only_skipped = urls(2)
    skip = {"220031": SkipEntry("220031", "MA", "SKIPPED", "site blocks server fetches (403)")}
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010", "220031")
        plan = plan_refetch(
            session,
            [entry("220010", wanted_by_both), entry("220031", wanted_by_both, only_skipped)],
            skip,
        )
    assert plan.urls == [wanted_by_both]
    assert plan.wanted_by == {wanted_by_both: ["220010"]}
    assert (plan.pairs, plan.already_have_text, plan.skipped_ccns) == (3, 0, 1)


def test_plan_titles_each_url_after_a_stored_source_with_that_url_else_empty():
    titled, borrowed, untitled = urls(3)
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010", "220031")
        stored(session, "220010", titled, title="Charity Care Application")
        # 220031 has this document with its text, so its own pair is dropped; the title it
        # carries still names the page for the hospital that does not have it.
        stored(session, "220031", borrowed, text=POLICY, title="Charity Care Policy")
        plan = plan_refetch(
            session,
            [entry("220010", titled, borrowed, untitled), entry("220031", borrowed)],
            {},
        )
    assert plan.titles == {
        titled: "Charity Care Application",
        borrowed: "Charity Care Policy",
        untitled: "",
    }


@pytest.mark.parametrize(
    ("count", "credits"), [(0, 0), (1, 2), (5, 2), (6, 4), (7, 4), (10, 4), (11, 6)]
)
def test_plan_estimates_two_credits_per_batch_of_five_at_the_advanced_depth(count, credits):
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010")
        plan = plan_refetch(session, [entry("220010", *urls(count))], {})
    assert len(plan.urls) == count
    assert plan.estimated_credits == Decimal(credits)


# run_refetch


def plan_for(session, *entries: MissingEntry):
    return plan_refetch(session, list(entries), {})


def test_run_asks_for_batches_of_five_at_the_advanced_depth_for_the_eve_refetch_purpose():
    seven = urls(7)
    gateway = FakeGateway({url: policy(n) for n, url in enumerate(seven)})
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010")
        report = run_refetch(session, gateway, plan_for(session, entry("220010", *seven)), TODAY)
    assert gateway.calls == [(seven[:5], PURPOSE, "advanced"), (seven[5:], PURPOSE, "advanced")]
    assert (report.requested, report.returned, report.kept, report.stored) == (7, 7, 7, 7)
    assert report.reasons == {} and report.stopped is None


def test_run_takes_the_batch_size_it_is_given_and_refuses_one_that_cannot_batch():
    five = urls(5)
    gateway = FakeGateway({url: policy(n) for n, url in enumerate(five)})
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010")
        plan = plan_for(session, entry("220010", *five))
        run_refetch(session, gateway, plan, TODAY, batch_size=2)
        with pytest.raises(ValueError, match="batch_size"):
            run_refetch(session, gateway, plan, TODAY, batch_size=0)
    assert [batch for batch, _, _ in gateway.calls] == [five[:2], five[2:4], five[4:]]


def test_run_stores_a_kept_page_as_a_new_source_for_every_hospital_that_wants_it():
    shared = "https://www.example.org/docs/financial-assistance-policy.pdf"
    application = "https://www.example.org/docs/application.pdf"
    page, form = policy(1), policy(2)
    gateway = FakeGateway({shared: page, application: form})
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010")
        hospitals(session, "410001", state="AL")
        for ccn in ("220010", "410001"):
            stored(session, ccn, shared)
        stored(session, "220010", application, title="Financial Assistance Application")
        plan = plan_for(
            session,
            entry("220010", shared, application),
            entry("410001", shared, state="AL"),
        )
        report = run_refetch(session, gateway, plan, TODAY)

        for ccn in ("220010", "410001"):
            rows = repo.sources_for(session, ccn)
            fresh = [(source, text) for source, text in rows if text]
            old = [source for source, text in rows if not text]
            # The old row, with no text, stays where it is; the page is a new source beside it.
            assert [source.id for source in old if source.url == shared] == [
                "eve-" + sha(shared)[:16]
            ]
            source, text = next((s, t) for s, t in fresh if s.url == shared)
            assert source.id == source_id_for("fap", sha(page))
            assert (source.kind, source.title, source.fetched_on) == (
                SourceKind.HOSPITAL_WEB,
                "Financial Assistance Policy",
                TODAY,
            )
            assert (source.sha256, text) == (sha(page), page)
        # The application is classified by its stored title and belongs to its one hospital.
        (form_source,) = [s for s, t in repo.sources_for(session, "220010") if t == form]
        assert form_source.id == source_id_for("application", sha(form))
        assert form_source.title == "Financial Assistance Application"
        assert texts_of(session, "410001") == {shared: page}
    assert (report.kept, report.stored) == (2, 3)  # two pages, three hospitals' sources
    # Sorted by state, then CCN: Alabama's 410001 comes before Massachusetts's 220010.
    assert report.gained == [("AL", "410001"), ("MA", "220010")]


def test_run_stores_a_page_without_a_class_in_its_address_or_title_as_the_policy():
    (url,) = urls(1)
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010")
        plan = plan_for(session, entry("220010", url))
        run_refetch(session, FakeGateway({url: policy(1)}), plan, TODAY)
        (source,) = [s for s, t in repo.sources_for(session, "220010") if t]
    assert source.id == source_id_for("fap", sha(policy(1)))
    assert source.title == ""


def test_run_leaves_out_pages_that_are_too_short_navigation_shells_or_not_returned():
    good, short, shell_a, shell_b, missing = urls(5)
    gateway = FakeGateway({good: POLICY, short: SHORT, shell_a: LONG_PAGE, shell_b: LONG_PAGE})
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010")
        plan = plan_for(session, entry("220010", good, short, shell_a, shell_b, missing))
        report = run_refetch(session, gateway, plan, TODAY)
        assert texts_of(session, "220010") == {good: POLICY}
    assert (report.requested, report.returned, report.kept, report.stored) == (5, 4, 1, 1)
    assert report.reasons == {
        TOO_SHORT: [short],
        NAVIGATION_SHELL: [shell_a, shell_b],
        NOT_RETURNED: [missing],
    }
    assert report.gained == [("MA", "220010")]


def test_run_sets_aside_a_long_menu_answered_for_several_urls_not_a_page_standing_alone():
    # A site that has moved answers every old address with one menu page, long enough to pass for
    # a document: only the same text at several URLs gives it away (scout.navigation_shells,
    # applied to the pages of each batch). A long page that differs from the others is a page.
    twin_a, twin_b, alone = urls(3)
    texts = {twin_a: LONG_PAGE, twin_b: LONG_PAGE + "\n", alone: LONG_PAGE + "Closing words."}
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010")
        plan = plan_for(session, entry("220010", twin_a, twin_b, alone))
        report = run_refetch(session, FakeGateway(texts), plan, TODAY)
        assert texts_of(session, "220010") == {alone: texts[alone]}
    assert report.reasons == {NAVIGATION_SHELL: [twin_a, twin_b]}
    assert report.kept == 1


def test_run_calls_the_same_short_menu_at_two_urls_a_shell_and_not_just_a_short_page():
    twin_a, twin_b = urls(2)
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010")
        plan = plan_for(session, entry("220010", twin_a, twin_b))
        report = run_refetch(session, FakeGateway({twin_a: MENU, twin_b: MENU}), plan, TODAY)
        assert texts_of(session, "220010") == {}
    assert report.reasons == {NAVIGATION_SHELL: [twin_a, twin_b]}


def test_run_keeps_what_the_scouts_download_rule_keeps():
    pdf = "https://www.example.org/docs/form.pdf"
    short_policy, menu, long_page = urls(3)
    texts = {pdf: PDF_TEXT, short_policy: POLICY, menu: MENU, long_page: LONG_PAGE}
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010")
        plan = plan_for(session, entry("220010", pdf, short_policy, menu, long_page))
        report = run_refetch(session, FakeGateway(texts), plan, TODAY)
        assert texts_of(session, "220010") == {
            pdf: PDF_TEXT,  # a PDF is what it is
            short_policy: POLICY,  # a short page whose prose names financial assistance
            long_page: LONG_PAGE,  # a page of THIN_CHARS or more
        }
    # A short web page that never names assistance is a menu, not a document.
    assert report.reasons == {NOT_ABOUT_ASSISTANCE: [menu]}


def test_run_cuts_a_page_to_the_scouts_limit_before_storing_it():
    (url,) = urls(1)
    long_text = POLICY + "x" * (MAX_CHARS + 10_000)
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010")
        plan = plan_for(session, entry("220010", url))
        run_refetch(session, FakeGateway({url: long_text}), plan, TODAY)
        ((source, text),) = [(s, t) for s, t in repo.sources_for(session, "220010") if t]
    assert text == long_text[:MAX_CHARS]
    assert source.sha256 == sha(text)


def test_run_says_when_tavily_answers_for_a_url_that_was_not_asked_for():
    (asked,) = urls(1)
    answered = "https://www.example.org/policies/redirected"

    class AnswersForAnotherAddress(FakeGateway):
        def extract(self, urls, **kwargs):
            super().extract(urls, **kwargs)
            return [ExtractedPage(answered, policy(1))]

    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010")
        plan = plan_for(session, entry("220010", asked))
        report = run_refetch(session, AnswersForAnotherAddress({}), plan, TODAY)
        assert texts_of(session, "220010") == {}
    assert report.returned == 1 and report.kept == 0
    assert report.reasons == {NOT_RETURNED: [asked], NOT_REQUESTED: [answered]}


def test_run_stops_at_the_budget_and_keeps_what_it_stored_before():
    twelve = urls(12)
    # The second call is refused; a third would be answered, and must never be asked for.
    gateway = FakeGateway({url: policy(n) for n, url in enumerate(twelve)}, budget_at=1)
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010")
        plan = plan_for(session, entry("220010", *twelve))
        report = run_refetch(session, gateway, plan, TODAY)
        assert sorted(texts_of(session, "220010")) == sorted(twelve[:5])
    assert gateway.calls == [(twelve[:5], PURPOSE, "advanced")]
    assert gateway.attempts == 2
    assert report.stopped == "Tavily cap of 2 credits would be exceeded (used 2)."
    # Asked for five of the twelve; the seven never asked for are not "not kept".
    assert (report.requested, report.returned, report.kept, report.stored) == (5, 5, 5, 5)
    assert report.reasons == {}
    assert report.gained == [("MA", "220010")]


def test_run_books_its_credits_through_the_governor_and_reports_them(tmp_path):
    seven = urls(7)
    governor = Governor(Ledger(tmp_path / "usage.jsonl"), 100, Decimal("1"))
    governor.record_tavily(Decimal("3"), "earlier work")
    client = FakeTavilyClient({url: policy(n) for n, url in enumerate(seven)})
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010")
        plan = plan_for(session, entry("220010", *seven))
        report = run_refetch(
            session, TavilyGateway(client, governor), plan, TODAY, governor=governor
        )
    assert client.calls == [(seven[:5], "advanced"), (seven[5:], "advanced")]
    assert (report.credits_before, report.credits_after) == (Decimal("3"), Decimal("7"))
    assert report.credits_spent == plan.estimated_credits == Decimal("4")


def test_run_stops_when_the_governors_cap_would_be_passed(tmp_path):
    seven = urls(7)
    governor = Governor(Ledger(tmp_path / "usage.jsonl"), 2, Decimal("1"))
    client = FakeTavilyClient({url: policy(n) for n, url in enumerate(seven)})
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010")
        plan = plan_for(session, entry("220010", *seven))
        report = run_refetch(
            session, TavilyGateway(client, governor), plan, TODAY, governor=governor
        )
        assert sorted(texts_of(session, "220010")) == seven[:5]
    assert client.calls == [(seven[:5], "advanced")]  # the second batch was never sent
    assert report.credits_spent == Decimal("2")
    assert report.stopped is not None and "cap of 2 credits" in report.stopped


def test_run_ends_on_any_other_error_from_tavily_names_it_and_keeps_the_batches_before_it():
    twelve = urls(12)
    # The second call fails; a third would be answered, and must never be asked for.
    gateway = FakeGateway({url: policy(n) for n, url in enumerate(twelve)}, fail_at=1)
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010")
        plan = plan_for(session, entry("220010", *twelve))
        report = run_refetch(session, gateway, plan, TODAY)
        assert sorted(texts_of(session, "220010")) == sorted(twelve[:5])
    assert gateway.calls == [(twelve[:5], PURPOSE, "advanced")] and gateway.attempts == 2
    assert report.failed == "TimeoutError: Tavily did not answer"
    assert report.stopped is None  # the budget did not end it
    assert (report.requested, report.kept, report.stored) == (5, 5, 5)
    assert report.gained == [("MA", "220010")]


def test_run_commits_each_batch_so_an_interrupt_later_loses_nothing_that_was_paid_for(tmp_path):
    seven = urls(7)
    engine = make_engine(f"sqlite:///{tmp_path / 'atlas.db'}")
    init_db(engine)
    with session_scope(engine) as session:
        hospitals(session, "220010")

    class Interrupted(FakeGateway):
        def extract(self, urls, **kwargs):
            if self.calls:
                raise KeyboardInterrupt  # not an Exception: nothing rolls the session back
            return super().extract(urls, **kwargs)

    gateway = Interrupted({url: policy(n) for n, url in enumerate(seven)})
    with pytest.raises(KeyboardInterrupt), session_scope(engine) as session:
        run_refetch(session, gateway, plan_for(session, entry("220010", *seven)), TODAY)
    # The session was closed with the second batch pending; the first was committed before it.
    with session_scope(engine) as session:
        assert sorted(texts_of(session, "220010")) == seven[:5]
        # A second run asks only for what is still missing.
        plan = plan_for(session, entry("220010", *seven))
    assert plan.urls == seven[5:] and plan.already_have_text == 5
    engine.dispose()


# The list of hospitals for the rebuilds


def test_merge_refetched_adds_to_the_list_sorted_and_ignores_lines_that_are_not_pairs(tmp_path):
    path = tmp_path / "var" / "eve-refetched.txt"
    assert read_refetched(path) == []
    # A first run writes the list, even an empty one, creating its folder.
    assert merge_refetched(path, []) == [] and path.read_text(encoding="utf-8") == ""
    path.write_text("RI 410001\n\nstray\nMA 220010 extra\nMA 220031\n", encoding="utf-8")
    assert read_refetched(path) == [("RI", "410001"), ("MA", "220031")]
    merged = merge_refetched(path, [("AL", "010001"), ("MA", "220031")])
    assert merged == [("AL", "010001"), ("MA", "220031"), ("RI", "410001")]
    assert path.read_text(encoding="utf-8") == "AL 010001\nMA 220031\nRI 410001\n"


def test_merge_refetched_that_fails_mid_write_leaves_the_old_list_whole(tmp_path, monkeypatch):
    path = tmp_path / "eve-refetched.txt"
    path.write_text("MA 220010\n", encoding="utf-8")

    def interrupted(source, target):
        raise KeyboardInterrupt

    monkeypatch.setattr("waive.atlas.eve_refetch.os.replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        merge_refetched(path, [("RI", "410001")])
    assert path.read_text(encoding="utf-8") == "MA 220010\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["eve-refetched.txt"]


def test_merge_refetched_leaves_the_file_as_it_was_when_a_run_gained_nothing(tmp_path):
    path = tmp_path / "eve-refetched.txt"
    written = "RI 410001\nMA 220010\n"  # not sorted: untouched, not rewritten
    path.write_text(written, encoding="utf-8")
    assert merge_refetched(path, []) == [("MA", "220010"), ("RI", "410001")]
    assert path.read_text(encoding="utf-8") == written


def test_a_rerun_plans_only_the_pages_that_were_not_stored():
    kept, short = urls(2)
    with session_scope(fresh_engine()) as session:
        hospitals(session, "220010")
        stored(session, "220010", kept)
        stored(session, "220010", short)
        first = plan_for(session, entry("220010", kept, short))
        run_refetch(session, FakeGateway({kept: POLICY, short: SHORT}), first, TODAY)
        second = plan_for(session, entry("220010", kept, short))
    assert first.urls == [kept, short]
    assert second.urls == [short]
    assert (second.pairs, second.already_have_text) == (2, 1)


# The command


BLOCKED = "site blocks server fetches (403)"
APPLICATION = "https://www.example.org/docs/application.pdf"
POLICY_PDF = "https://www.example.org/docs/financial-assistance-policy.pdf"
SKIPPED_PDF = "https://www.example.org/docs/skipped.pdf"
HAVE_TEXT = "https://www.example.org/docs/has-text.pdf"


@pytest.fixture
def workdir(monkeypatch, tmp_path):
    """A temporary working folder with a local database, a usage ledger, a skip list naming
    220031 and a missing-sources file, all its own; no .env, no model or Tavily key. Hospitals
    220010 (MA) and 410001 (RI) want the policy; 410001 also lists a document it has text for,
    220010 an application, and the skip-listed 220031 a document of its own."""
    monkeypatch.chdir(tmp_path)
    url = f"sqlite:///{tmp_path / 'local.db'}"
    monkeypatch.setenv("WAIVE_DATABASE_URL", url)
    monkeypatch.setenv("WAIVE_LEDGER_PATH", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("WAIVE_SCOUT_SKIP_LIST", str(tmp_path / "skip.json"))
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    (tmp_path / "skip.json").write_text(
        json.dumps(
            [{"ccn": "220031", "state": "MA", "name": "SKIPPED GENERAL", "reason": BLOCKED}]
        ),
        encoding="utf-8",
    )
    (tmp_path / "missing.json").write_text(
        json.dumps(
            [
                {
                    "ccn": "220010",
                    "state": "MA",
                    "name": "NEW HOSPITAL",
                    "missing_urls": [POLICY_PDF, APPLICATION],
                    "fetched": 0,
                    "total": 2,
                },
                {
                    "ccn": "220031",
                    "state": "MA",
                    "name": "SKIPPED GENERAL",
                    "missing_urls": [SKIPPED_PDF],
                    "fetched": 0,
                    "total": 1,
                },
                {
                    "ccn": "410001",
                    "state": "RI",
                    "name": "BAYSIDE COMMUNITY HOSPITAL",
                    "missing_urls": [POLICY_PDF, HAVE_TEXT],
                    "fetched": 0,
                    "total": 2,
                },
            ]
        ),
        encoding="utf-8",
    )
    engine = make_engine(url)
    init_db(engine)
    with session_scope(engine) as session:
        hospitals(session, "220010", "220031")
        hospitals(session, "410001", state="RI")
        stored(session, "220010", POLICY_PDF)
        stored(session, "410001", POLICY_PDF)
        stored(session, "220010", APPLICATION, title="Financial Assistance Application")
        stored(session, "410001", HAVE_TEXT, text=POLICY)
        stored(session, "220031", SKIPPED_PDF)
    yield engine
    engine.dispose()


def invoke(*args: str, status: int = 0):
    result = CliRunner().invoke(app, ["atlas", "eve-refetch", *args])
    assert result.exit_code == status, result.output
    return result.output.splitlines()


def tavily(monkeypatch, client):
    """Make `--run` build its gateway over `client`, as the command does over the real one;
    returns the list of governors it was built with (one entry per gateway made)."""
    built = []

    def make(settings, governor):
        built.append(governor)
        return TavilyGateway(client, governor)

    monkeypatch.setattr("waive.cli.make_tavily_gateway", make)
    return built


PLAN_LINE = (
    "refetch plan: 2 unique URLs for 2 hospitals (5 listed pairs, 1 already have text, "
    "1 on the skip list); about 2 Tavily credits at advanced depth"
)


def test_the_dry_run_prints_the_plan_and_spends_nothing(workdir, monkeypatch, tmp_path):
    client = FakeTavilyClient({POLICY_PDF: policy(1), APPLICATION: policy(2)})
    built = tavily(monkeypatch, client)
    # Neither the default nor --dry-run builds a gateway, asks Tavily or touches the ledger.
    assert invoke("--file", "missing.json") == [PLAN_LINE]
    assert invoke("--file", "missing.json", "--dry-run") == [PLAN_LINE]
    assert built == [] and client.calls == []
    assert not (tmp_path / "usage.jsonl").exists()
    assert not (tmp_path / "var").exists()
    with session_scope(workdir) as session:
        assert texts_of(session, "220010") == {}


def test_the_default_file_is_data_atlas_eve_missing_sources_and_a_missing_one_plans_nothing(
    workdir,
):
    # Run from the temporary folder, data/atlas-eve/_missing-sources.json is not there.
    assert invoke() == [
        "refetch plan: 0 unique URLs for 0 hospitals (0 listed pairs, 0 already have text, "
        "0 on the skip list); about 0 Tavily credits at advanced depth"
    ]


def test_run_fetches_stores_and_writes_the_hospitals_that_gained_a_source(
    workdir, monkeypatch, tmp_path
):
    client = FakeTavilyClient({POLICY_PDF: policy(1), APPLICATION: SHORT})
    tavily(monkeypatch, client)
    out = tmp_path / "gained.txt"
    lines = invoke("--file", "missing.json", "--run", "--out", str(out))
    assert lines[0] == PLAN_LINE
    assert lines[1] == (
        "refetched 1 of 2 URLs; stored 2 sources for 2 hospitals; 2 Tavily credits spent"
    )
    assert lines[2:4] == [f"not stored (1): {TOO_SHORT}", f"  {APPLICATION}"]
    assert lines[4] == f"2 hospitals gained a source; 2 listed in {out} for the rebuilds"
    assert len(lines) == 5
    assert client.calls == [([POLICY_PDF, APPLICATION], "advanced")]
    assert out.read_text(encoding="utf-8") == "MA 220010\nRI 410001\n"
    # Committed: a new session sees both hospitals' new source, and the ledger has the credits.
    with session_scope(workdir) as session:
        assert texts_of(session, "220010") == {POLICY_PDF: policy(1)}
        assert texts_of(session, "410001") == {POLICY_PDF: policy(1), HAVE_TEXT: POLICY}
        assert texts_of(session, "220031") == {}  # the skip list wins
    ledger = (tmp_path / "usage.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["purpose"] for line in ledger] == [PURPOSE]


def test_run_writes_var_eve_refetched_by_default_and_a_rerun_that_gains_nothing_keeps_it(
    workdir, monkeypatch, tmp_path
):
    client = FakeTavilyClient({POLICY_PDF: policy(1), APPLICATION: policy(2)})
    built = tavily(monkeypatch, client)
    first = invoke("--file", "missing.json", "--run")
    assert first[1] == (
        "refetched 2 of 2 URLs; stored 3 sources for 2 hospitals; 2 Tavily credits spent"
    )
    default = tmp_path / "var" / "eve-refetched.txt"
    assert default.read_text(encoding="utf-8") == "MA 220010\nRI 410001\n"
    # Everything is stored now: nothing to ask Tavily, so no gateway is built. An accidental
    # second --run gained nothing and must not empty the list the rebuilds still need.
    again = invoke("--file", "missing.json", "--run")
    assert again[0].startswith("refetch plan: 0 unique URLs for 0 hospitals (5 listed pairs, 4 ")
    assert again[1] == (
        "refetched 0 of 0 URLs; stored 0 sources for 0 hospitals; 0 Tavily credits spent"
    )
    assert again[2] == (
        "0 hospitals gained a source; 2 listed in var/eve-refetched.txt for the rebuilds"
    )
    assert len(built) == 1 and len(client.calls) == 1
    assert default.read_text(encoding="utf-8") == "MA 220010\nRI 410001\n"


def write_two_batches(tmp_path, urls_of):
    """A missing-sources file whose first five URLs are 220010's and whose last two are 410001's:
    two batches, and each hospital gains from one of them."""
    first, second = urls_of[:5], urls_of[5:]
    (tmp_path / "missing.json").write_text(
        json.dumps(
            [
                {"ccn": "220010", "state": "MA", "name": "NEW HOSPITAL", "missing_urls": first},
                {"ccn": "410001", "state": "RI", "name": "BAYSIDE", "missing_urls": second},
            ]
        )
    )


def test_run_stops_at_the_credit_cap_and_the_rerun_adds_to_the_list_it_wrote(
    workdir, monkeypatch, tmp_path
):
    monkeypatch.setenv("WAIVE_TAVILY_CREDIT_CAP", "2")
    seven = urls(7)
    write_two_batches(tmp_path, seven)
    client = FakeTavilyClient({url: policy(n) for n, url in enumerate(seven)})
    tavily(monkeypatch, client)
    lines = invoke("--file", "missing.json", "--run", "--out", "gained.txt")
    assert lines[1] == (
        "refetched 5 of 7 URLs; stored 5 sources for 1 hospitals; 2 Tavily credits spent"
    )
    assert lines[2] == (
        "stopped: Tavily cap of 2 credits would be exceeded (used 2). "
        "The sources stored so far are kept; run again for the rest."
    )
    assert lines[3] == "1 hospitals gained a source; 1 listed in gained.txt for the rebuilds"
    assert len(client.calls) == 1
    assert (tmp_path / "gained.txt").read_text(encoding="utf-8") == "MA 220010\n"
    with session_scope(workdir) as session:
        assert sorted(texts_of(session, "220010")) == seven[:5]
    # The rerun the command asks for, with room under the cap: it fetches the rest and adds
    # 410001 to the list; 220010, gained by the first run, stays in it.
    monkeypatch.setenv("WAIVE_TAVILY_CREDIT_CAP", "10")
    rest = invoke("--file", "missing.json", "--run", "--out", "gained.txt")
    assert rest[1] == (
        "refetched 2 of 2 URLs; stored 2 sources for 1 hospitals; 2 Tavily credits spent"
    )
    assert rest[-1] == "1 hospitals gained a source; 2 listed in gained.txt for the rebuilds"
    assert (tmp_path / "gained.txt").read_text(encoding="utf-8") == "MA 220010\nRI 410001\n"


def test_run_that_fails_on_another_error_still_stores_and_lists_what_it_gained_and_exits_1(
    workdir, monkeypatch, tmp_path
):
    seven = urls(7)
    write_two_batches(tmp_path, seven)
    # The second call times out after the first batch was stored.
    client = FakeTavilyClient({url: policy(n) for n, url in enumerate(seven)}, fail_at=1)
    tavily(monkeypatch, client)
    lines = invoke("--file", "missing.json", "--run", "--out", "gained.txt", status=1)
    assert lines[1] == (
        "refetched 5 of 7 URLs; stored 5 sources for 1 hospitals; 2 Tavily credits spent"
    )
    assert (
        lines[2]
        == "failed: TimeoutError: Tavily did not answer. The sources stored before it are kept."
    )
    assert lines[3] == "1 hospitals gained a source; 1 listed in gained.txt for the rebuilds"
    assert len(lines) == 4
    assert (tmp_path / "gained.txt").read_text(encoding="utf-8") == "MA 220010\n"
    with session_scope(workdir) as session:
        assert sorted(texts_of(session, "220010")) == seven[:5]  # committed, and listed above
        assert not set(seven[5:]) & set(texts_of(session, "410001"))
    # The rerun asks only for the last two URLs and adds their hospital to the list.
    client.fail_at = None
    rest = invoke("--file", "missing.json", "--run", "--out", "gained.txt")
    assert rest[0].startswith("refetch plan: 2 unique URLs for 1 hospitals (7 listed pairs, 5 ")
    assert (tmp_path / "gained.txt").read_text(encoding="utf-8") == "MA 220010\nRI 410001\n"


def test_run_without_a_tavily_key_says_so_before_anything_is_fetched(workdir, tmp_path):
    # No gateway is faked here: the real factory refuses, as `atlas build` does without a key.
    result = CliRunner().invoke(app, ["atlas", "eve-refetch", "--file", "missing.json", "--run"])
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert "TAVILY_API_KEY" in str(result.exception)
    assert not (tmp_path / "usage.jsonl").exists()
