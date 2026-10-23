"""Refetch the Eve sheets' missing source documents through Tavily Extract (task 7.12d).

The Eve waive-agent cited documents its sheets quote, and `import-eve --db` stored each one as a
source. For 134 sheets 175 of those URLs could not be downloaded here: the stored source has no
text (or there is none), so a rebuild from stored documents (`waive atlas build
--reuse-sources`) has nothing to read for them. `data/atlas-eve/_missing-sources.json` lists
them. This pass asks Tavily Extract for each unique URL once, at the advanced depth, and stores
what comes back the way the scout stores what it scouts: a new source (`store_scouted`) linked to
every hospital that cites the URL. The old row with no text stays where it is, because
`repo.save_source` never overwrites a stored document and older sheet versions cite it.

There is no direct-download fallback: these are the URLs the downloader already failed on. Every
paid call goes through the governor (`TavilyGateway.extract` checks the cap before the call and
books the credits after it), and a hospital on the skip list is never refetched.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.scout import (
    MIN_CHARS,
    THIN_CHARS,
    ScoutedDoc,
    _scouted,
    _usable_download,
    classify_doc,
    navigation_shells,
    store_scouted,
)
from waive.atlas.skiplist import SkipEntry
from waive.atlas.tavily_gateway import Depth, ExtractedPage, TavilyGateway, extract_cost
from waive.governor import BudgetExceeded, Governor

MISSING_SOURCES = Path("data/atlas-eve/_missing-sources.json")
# The hospitals that gained a source, for the rebuilds (`scripts/eve_rebuild_batch.py --ccns`).
REFETCHED_LIST = Path("var/eve-refetched.txt")
PURPOSE = "atlas.eve_refetch"
DEPTH: Depth = "advanced"
# Tavily bills Extract per five URLs (`extract_cost`), so a batch is as many as one bill covers.
BATCH_SIZE = 5

# Why a listed URL ended without a source; the report keeps the URLs under these.
NOT_RETURNED = "Tavily returned no page for the URL"
NOT_REQUESTED = "Tavily returned a page for a URL that was not asked for"
TOO_SHORT = f"fewer than {MIN_CHARS} characters of text"
NAVIGATION_SHELL = "the site's navigation shell (the same text at several URLs)"
NOT_ABOUT_ASSISTANCE = f"a page under {THIN_CHARS} characters that never names financial assistance"


@dataclass(frozen=True)
class MissingEntry:
    """One sheet of `_missing-sources.json`: the source URLs of its draft that could not be
    downloaded (`fetched` of its `total` could)."""

    ccn: str
    state: str
    name: str
    missing_urls: tuple[str, ...]
    fetched: int = 0
    total: int = 0


def load_missing(path: Path = MISSING_SOURCES) -> list[MissingEntry]:
    """The listed sheets, in the file's order. A missing file is an empty list (the container
    image carries no data/ folder). A file that is not a JSON list is an error rather than an
    empty list, so a wrong file never looks like nothing to do. Rows that are not objects or name
    no hospital are ignored."""
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a JSON list of hospitals")
    entries: list[MissingEntry] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("ccn"):
            continue
        listed = row.get("missing_urls")
        entries.append(
            MissingEntry(
                ccn=str(row["ccn"]),
                state=str(row.get("state") or ""),
                name=str(row.get("name") or ""),
                missing_urls=tuple(str(url) for url in listed if url)
                if isinstance(listed, list)
                else (),
                fetched=int(row.get("fetched") or 0),
                total=int(row.get("total") or 0),
            )
        )
    return entries


@dataclass
class RefetchPlan:
    """What a run would ask Tavily for. `urls` are the unique URLs to fetch, in the order the file
    first lists them; `wanted_by` holds the hospitals (CCNs) that cite each one, `titles` the title
    to store it under and `states` the state of each hospital. `pairs` counts every (hospital,
    URL) pair the file lists, `already_have_text` the pairs left out because that hospital's
    stored source for the URL has text, and `skipped_ccns` the listed hospitals left out for
    being on the skip list."""

    pairs: int
    already_have_text: int
    skipped_ccns: int
    urls: list[str]
    wanted_by: dict[str, list[str]]
    titles: dict[str, str]
    states: dict[str, str]
    estimated_credits: Decimal

    @property
    def ccns(self) -> list[str]:
        """The hospitals that want at least one URL."""
        return sorted({ccn for wanting in self.wanted_by.values() for ccn in wanting})


def _batches(urls: list[str], size: int) -> list[list[str]]:
    return [urls[start : start + size] for start in range(0, len(urls), size)]


def plan_refetch(
    session: Session, entries: Sequence[MissingEntry], skip: Mapping[str, SkipEntry]
) -> RefetchPlan:
    """The URLs to fetch for `entries`, with no call to Tavily. A hospital on the skip list is
    dropped whole. A pair is dropped when a source linked to that hospital with that exact URL
    already has text: the rest of the pairs are what a run fetches, so a rerun asks only for what
    is still missing. A URL several hospitals list is fetched once and stored for each. The
    title it is stored under is the one a stored source with that URL carries, if any listed
    hospital has one, else none: Tavily Extract returns text without a title."""
    wanted_by: dict[str, list[str]] = {}
    stored_titles: dict[str, str] = {}
    states: dict[str, str] = {}
    pairs = already_have_text = 0
    skipped: set[str] = set()
    for entry in entries:
        pairs += len(entry.missing_urls)
        if entry.ccn in skip:
            skipped.add(entry.ccn)
            continue
        sources = repo.sources_for(session, entry.ccn)
        for url in entry.missing_urls:
            with_url = [(source, text) for source, text in sources if source.url == url]
            if title := next((source.title for source, _ in with_url if source.title), ""):
                stored_titles.setdefault(url, title)
            if any(text.strip() for _, text in with_url):
                already_have_text += 1
                continue
            wanting = wanted_by.setdefault(url, [])
            if entry.ccn not in wanting:
                wanting.append(entry.ccn)
            states[entry.ccn] = entry.state
    urls = list(wanted_by)
    estimated = sum(
        (extract_cost(len(batch), DEPTH) for batch in _batches(urls, BATCH_SIZE)), Decimal(0)
    )
    return RefetchPlan(
        pairs=pairs,
        already_have_text=already_have_text,
        skipped_ccns=len(skipped),
        urls=urls,
        wanted_by=wanted_by,
        titles={url: stored_titles.get(url, "") for url in urls},
        states=states,
        estimated_credits=estimated,
    )


@dataclass
class RefetchReport:
    """What a run did. `requested` counts the URLs sent to Tavily (a batch the budget refused is
    not sent), `returned` the pages that came back, `kept` the pages stored and `stored` the
    sources stored: one per kept page and hospital that cites it. `reasons` holds the URLs that
    gave no source under why not. `gained` is the sorted (state, CCN) of every hospital that got
    at least one source. `stopped` is the governor's message when the budget ended the run."""

    requested: int = 0
    returned: int = 0
    kept: int = 0
    stored: int = 0
    credits_before: Decimal = Decimal(0)
    credits_after: Decimal = Decimal(0)
    reasons: dict[str, list[str]] = field(default_factory=dict)
    gained: list[tuple[str, str]] = field(default_factory=list)
    stopped: str | None = None

    @property
    def credits_spent(self) -> Decimal:
        return self.credits_after - self.credits_before

    def reject(self, reason: str, url: str) -> None:
        self.reasons.setdefault(reason, []).append(url)


def _tavily_credits(governor: Governor | None) -> Decimal:
    return governor.summary()["tavily"][0] if governor is not None else Decimal(0)


def _documents_to_keep(
    batch: list[str], pages: list[ExtractedPage], titles: Mapping[str, str], report: RefetchReport
) -> list[ScoutedDoc]:
    """The documents among one batch's pages that the scout would keep, by its own rules; every
    other URL (and every page nobody asked for) is entered in `report.reasons` with why. A page
    is kept when its text, cut to the scout's limit, has the scout's minimum length, it is not a
    navigation shell among the batch's pages, and it is a PDF, a page of the thin limit or more,
    or a shorter page whose prose names financial assistance."""
    by_url = {page.url: page.text for page in pages}
    asked = set(batch)
    for page in pages:
        if page.url not in asked:
            report.reject(NOT_REQUESTED, page.url)
    candidates: list[ScoutedDoc] = []
    for url in batch:
        if url not in by_url:
            report.reject(NOT_RETURNED, url)
            continue
        title = titles.get(url, "")
        doc = _scouted(url, classify_doc(url, title) or "fap", title, by_url[url])
        if doc is None:
            report.reject(TOO_SHORT, url)
        else:
            candidates.append(doc)
    shells = navigation_shells((doc.url, doc.text) for doc in candidates)
    kept: list[ScoutedDoc] = []
    for doc in candidates:
        if doc.url in shells:
            report.reject(NAVIGATION_SHELL, doc.url)
        elif not _usable_download(doc.url, doc.text):
            report.reject(NOT_ABOUT_ASSISTANCE, doc.url)
        else:
            kept.append(doc)
    return kept


def run_refetch(
    session: Session,
    gateway: TavilyGateway,
    plan: RefetchPlan,
    today: date,
    *,
    batch_size: int = BATCH_SIZE,
    governor: Governor | None = None,
) -> RefetchReport:
    """Ask Tavily Extract, at the advanced depth, for the planned URLs `batch_size` at a time, and
    store each page worth keeping as a new source for every hospital that cites its URL.

    A batch the governor refuses (`BudgetExceeded`) ends the run with what was stored so far; any
    other failure propagates. Each batch is committed as soon as it is stored, so a failure
    later in the run (a timeout at the tenth call) loses nothing that was paid for, and a rerun
    plans only the URLs still missing. Pass the governor the gateway spends through to have the
    report count the credits; without it they read as none."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    report = RefetchReport(credits_before=_tavily_credits(governor))
    gained: set[tuple[str, str]] = set()
    for batch in _batches(plan.urls, batch_size):
        try:
            pages = gateway.extract(batch, purpose=PURPOSE, depth=DEPTH)
        except BudgetExceeded as error:
            report.stopped = str(error)
            break
        report.requested += len(batch)
        report.returned += len(pages)
        for doc in _documents_to_keep(batch, pages, plan.titles, report):
            report.kept += 1
            for ccn in plan.wanted_by[doc.url]:
                store_scouted(session, ccn, [doc], today)
                report.stored += 1
                gained.add((plan.states[ccn], ccn))
        session.commit()
    report.gained = sorted(gained)
    report.credits_after = _tavily_credits(governor)
    return report
