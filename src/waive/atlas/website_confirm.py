"""Confirm the websites of held sheets with Waive's own discovery search (speed-up plan of
2026-10-24).

A sheet whose income rule the verifier accepted is still held when its hospital's website was
matched with low confidence (0.6, `domain_unconfirmed`, decision 7.9a-1). Waive learns a website
through `discover.discover_domain`: one Tavily search, and `pick_domain` is sure (0.9) only when a
page title names the hospital and the hit names its place, or a page carries its phone. This pass
runs that same search again for each such hospital and accepts the sure answer only when it names
the website already stored. That is the normal discovery rule, not an admin's confirmation: the
confidence becomes the search's own (0.9) and never `CONFIRMED_CONFIDENCE` (1.0), which stays the
admin's call. A search whose sure answer names another website changes nothing on the hospital; it
is filed as an open `website_searched` item for a person. Every search is recorded in such an
item, and every paid call goes through the governor (`TavilyGateway.search` checks the cap before
the call and books the credits after it).
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.discover import (
    MIN_CONFIDENCE,
    discover_domain,
    domain_unconfirmed,
    host_of,
    rejected_domains,
)
from waive.atlas.eve_refetch import merge_refetched
from waive.atlas.schema import ProcedureSheet, SheetStatus
from waive.atlas.tavily_gateway import Depth, TavilyGateway, search_cost
from waive.db import HospitalRow
from waive.governor import BudgetExceeded, Governor

# The hospitals confirmed, for the rebuilds: one 'ST CCN' per line, merged like the Eve refetch's.
CONFIRMED_LIST = Path("var/websites-confirmed.txt")
# The depth `discover_domain` searches at: it passes none, so the gateway's default applies (a test
# compares the two).
SEARCH_DEPTH: Depth = "basic"
# The review item that asked an admin about a website (pipeline._hold_for_domain files it) and the
# one this pass files for each search it makes.
DOMAIN_UNCONFIRMED_ITEM = "domain_unconfirmed"
WEBSITE_SEARCHED = "website_searched"

Outcome = Literal["confirmed", "other_site", "unconfirmed", "skipped", "changed"]


@dataclass(frozen=True)
class SearchOutcome:
    """What one hospital's search came to. `found_domain`, `confidence` and `evidence_url` are the
    search's answer (all None when it found no usable site), whatever the outcome.

    Two outcomes mean the hospital changed under the run (an admin confirmed or cleared its
    website, a build published its sheet) and nothing was written for it: `skipped` before the
    search, which therefore cost nothing and has no answer, and `changed` after it, which was paid
    for and records its answer."""

    ccn: str
    state: str
    outcome: Outcome
    found_domain: str | None
    confidence: float | None
    evidence_url: str | None


def _has_income_rule(sheet: ProcedureSheet) -> bool:
    """Whether the sheet carries a free-care band, a discount table or an assistance ceiling."""
    eligibility = sheet.eligibility
    return (
        eligibility.free_care_max_fpl is not None
        or bool(eligibility.discount_tiers and eligibility.discount_tiers.value)
        or eligibility.assistance_ceiling_fpl is not None
    )


def candidates(
    session: Session,
    *,
    states: Collection[str] | None = None,
    skip: Collection[str] | None = None,
) -> list[HospitalRow]:
    """The hospitals worth a search, ordered by state then CCN: a website matched with less than
    MIN_CONFIDENCE (a missing confidence, and 1.0 which an admin set, are not candidates), an open
    `domain_unconfirmed` item, and a newest sheet that is held and carries an income rule, since
    only that sheet is published by a confirmed website. `states` limits them to those states and
    `skip` (the skip list, by CCN) leaves hospitals out."""
    query = (
        select(HospitalRow)
        .where(
            HospitalRow.website_domain.is_not(None),
            HospitalRow.website_domain != "",
            HospitalRow.domain_confidence.is_not(None),
            HospitalRow.domain_confidence < MIN_CONFIDENCE,
        )
        .order_by(HospitalRow.state, HospitalRow.ccn)
    )
    if states is not None:
        query = query.where(HospitalRow.state.in_([state.upper() for state in states]))
    asked = {
        item.ccn
        for item in repo.open_review_items(session, kind=DOMAIN_UNCONFIRMED_ITEM)
        if item.ccn
    }
    found: list[HospitalRow] = []
    for row in session.scalars(query):
        if row.ccn not in asked or (skip is not None and row.ccn in skip):
            continue
        if _held_with_income_rule(session, row.ccn):
            found.append(row)
    return found


def _held_with_income_rule(session: Session, ccn: str) -> bool:
    """Whether the newest sheet is held and carries an income rule."""
    latest = repo.latest_sheet(session, ccn)
    return (
        latest is not None and latest[0].status is SheetStatus.HELD and _has_income_rule(latest[0])
    )


def _still_waiting(session: Session, row: HospitalRow) -> bool:
    """Whether the hospital, as the database has it now, is still what `candidates` listed: a
    website matched with less than MIN_CONFIDENCE and nobody's answer to the open question, and a
    newest sheet that is held with an income rule. Read afresh by the caller."""
    return (
        bool(row.website_domain)
        and row.domain_confidence is not None
        and domain_unconfirmed(row.website_domain, row.domain_confidence)
        and bool(repo.open_review_items(session, row.ccn, DOMAIN_UNCONFIRMED_ITEM))
        and _held_with_income_rule(session, row.ccn)
    )


def planned_credits(searches: int) -> Decimal:
    """What `searches` hospitals cost: one search each."""
    return search_cost(SEARCH_DEPTH) * searches


def _site_of(domain: str) -> str:
    """The registered domain of a stored website (a bare host, with or without www.), the form
    `host_of` gives a hit's URL."""
    return host_of(domain if "//" in domain else f"https://{domain}")


def confirm_by_search(session: Session, gateway: TavilyGateway, row: HospitalRow) -> SearchOutcome:
    """Search for the hospital's website the way discovery does (one `discover_domain` call, over
    the sites an admin rejected for it) and settle what it found, recording the search in a
    `website_searched` item. `BudgetExceeded` from the gateway propagates, before anything is
    changed.

    `row` is the hospital as `candidates` loaded it, and the session keeps what it loaded
    (`expire_on_commit` is off), so everything is read again from the database first: a hospital
    that stopped waiting since the plan (an admin confirmed or cleared its website, a build
    published its sheet, the question was closed) is `skipped` with no search and nothing filed,
    and the search compares with the website as it is now. After the search the hospital is locked
    (as `confirm_domain` does) and checked once more; if its website is no longer the one searched
    for or its confidence is no longer below MIN_CONFIDENCE, the outcome is `changed`: the paid
    answer is recorded and nothing else is written, so a search never writes over an admin's 1.0.

    confirmed: the search is sure (at least MIN_CONFIDENCE) of the stored website. The hospital's
    confidence becomes the search's own, and the item is a resolved record.
    other_site: the search is sure of another website. Nothing changes on the hospital, and the
    item stays open for a person, who sees both sites.
    unconfirmed: no usable hit, or only a guess (0.6) or a mention. Nothing changes; the item is
    a resolved record of the spent search."""
    ccn, state = row.ccn, row.state
    session.flush()
    session.expire_all()
    if not _still_waiting(session, row):
        return SearchOutcome(ccn, state, "skipped", None, None, None)
    searched_for = row.website_domain
    result = discover_domain(gateway, repo.hospital_ref(row), rejected_domains(session, ccn))
    found = None if result is None else (result.domain, result.confidence, result.evidence_url)
    detail: dict[str, object] = {
        "outcome": "unconfirmed",
        "domain": None if result is None else result.domain,
        "confidence": None if result is None else result.confidence,
        "evidence_url": None if result is None else result.evidence_url,
    }
    outcome: Outcome = "unconfirmed"
    locked = repo.lock_hospital(session, ccn)
    if (
        locked is None
        or locked.website_domain != searched_for
        or locked.domain_confidence is None
        or locked.domain_confidence >= MIN_CONFIDENCE
    ):
        outcome = "changed"
        detail["outcome"] = outcome
        detail["reason"] = "the hospital's website or its confidence changed while the search ran"
    elif result is not None and result.confidence >= MIN_CONFIDENCE:
        same = _site_of(locked.website_domain or "") == _site_of(result.domain)
        outcome = "confirmed" if same else "other_site"
        detail["outcome"] = outcome
        if same:
            locked.domain_confidence = result.confidence
        else:
            detail["stored"] = locked.website_domain
    item = repo.add_review_item(session, ccn, WEBSITE_SEARCHED, detail)
    if outcome != "other_site":
        repo.set_review_status(session, item.id, "resolved")
    session.flush()
    return SearchOutcome(ccn, state, outcome, *(found or (None, None, None)))


@dataclass
class ConfirmReport:
    """What a run did. `total` is the hospitals it was to search and `outcomes` the ones it
    finished, in order (those `skipped` were not searched). `stopped` is the governor's message
    when the budget ended the run, and `failed` the name and message of any other error that did
    (a timeout, an HTTP 429 or 5xx, a failed commit or write of the list): the hospitals before it
    are committed."""

    total: int = 0
    outcomes: list[SearchOutcome] = field(default_factory=list)
    credits_before: Decimal = Decimal(0)
    credits_after: Decimal = Decimal(0)
    stopped: str | None = None
    failed: str | None = None

    @property
    def credits_spent(self) -> Decimal:
        return self.credits_after - self.credits_before

    def count(self, outcome: Outcome) -> int:
        return sum(1 for found in self.outcomes if found.outcome == outcome)

    @property
    def searched(self) -> int:
        """The searches made: every finished hospital but those skipped before theirs."""
        return len(self.outcomes) - self.count("skipped")

    @property
    def confirmed(self) -> list[tuple[str, str]]:
        """The sorted (state, CCN) of every hospital confirmed, as the rebuilds' list names them."""
        return sorted(
            (found.state, found.ccn) for found in self.outcomes if found.outcome == "confirmed"
        )

    @property
    def other_sites(self) -> list[SearchOutcome]:
        return [found for found in self.outcomes if found.outcome == "other_site"]


def _tavily_credits(governor: Governor | None) -> Decimal:
    return governor.summary()["tavily"][0] if governor is not None else Decimal(0)


def run_confirm(
    session: Session,
    gateway: TavilyGateway,
    rows: Sequence[HospitalRow],
    *,
    governor: Governor | None = None,
    out: Path | None = None,
) -> ConfirmReport:
    """Search for each hospital in order and commit after each, so even an interrupt keeps the
    searches before it. A search the governor refuses (`BudgetExceeded`) ends the run, and so does
    any other error (a timeout at the tenth call, a failed commit or write): the report says
    which, and the hospitals confirmed so far stay committed. Pass the governor the gateway
    spends through to have the report count the credits; without it they read as none.

    A hospital confirmed at 0.9 is no candidate any more, and the list the rebuilds read is its
    only road to a rebuild, so with `out` each confirmed hospital is merged into that list
    (`merge_refetched`: union, sorted) right after its commit rather than at the end: a Ctrl-C or a
    kill loses none that is committed. The commit sits inside the same handler as the search, so
    a commit that fails is the run's failure, and that hospital is neither counted nor listed."""
    report = ConfirmReport(total=len(rows), credits_before=_tavily_credits(governor))
    for row in rows:
        try:
            outcome = confirm_by_search(session, gateway, row)
            session.commit()
        except BudgetExceeded as error:
            session.rollback()
            report.stopped = str(error)
            break
        except Exception as error:
            # Not swallowed: the report names it and the command exits non-zero. It is caught here
            # so the caller still has the hospitals confirmed before it to write down.
            session.rollback()
            report.failed = f"{type(error).__name__}: {error}"
            break
        report.outcomes.append(outcome)
        if outcome.outcome == "confirmed" and out is not None:
            try:
                merge_refetched(out, [(outcome.state, outcome.ccn)])
            except OSError as error:
                report.failed = f"{type(error).__name__}: {error}"
                break
    report.credits_after = _tavily_credits(governor)
    return report
