"""Pydantic views of the ledger: the `ledger` key of `/atlas/{ccn}.json`, the page block and the
result screen's "What to expect here" lines. Built by `view.py` from the stored metrics; the
headlines, sentences and advice come from `explain.py`."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class MetricStatus(StrEnum):
    KNOWN = "known"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class GapLabel(StrEnum):
    PROMISES_MORE = "promises_more_delivers_less"
    DELIVERS = "delivers_what_it_promises"
    PROMISES_LESS = "promises_less_delivers_more"


class ExposureLabel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Source(BaseModel):
    """Where a number came from and how old it is: shown under every card."""

    name: str
    vintage: str
    url: str | None = None


class Ids(BaseModel):
    ein: str | None = None
    npi: str | None = None
    match_method: str | None = None
    match_confidence: float | None = None


class Metric(BaseModel):
    """What every card shares: a status, the words, what is missing and the sources."""

    status: MetricStatus = MetricStatus.UNKNOWN
    headline: str = ""
    sentence: str = ""
    # One line for the result screen's "What to expect here"; the screen skips the metric when
    # it is empty, because a bare headline such as "Medium" says nothing on its own.
    short: str = ""
    advice: str = ""
    missing: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)


class GapMetric(Metric):
    """Metric 1: the free-care threshold the hospital states against the share of its costs it
    spent on charity care, both ranked against the other hospitals with the same number."""

    stated_free_care_fpl: float | None = None
    stated_source: Literal["sheet", "irs990"] | None = None
    stated_rank: float | None = None
    stated_peers: int | None = None
    charity_cost_pct: float | None = None
    charity_rank: float | None = None
    charity_peers: int | None = None
    charity_pct_990: float | None = None
    cost_report_year: int | None = None
    gap: float | None = None
    label: GapLabel | None = None


class ExposureMetric(Metric):
    """Metric 2: Medicare inpatient stays by people also on Medicaid, plus the poor seniors in
    the ZIP codes the hospital serves."""

    discharges: int | None = None
    dual_discharges: int | None = None
    poor_seniors_served: int | None = None
    score: float | None = None
    rank: float | None = None
    peers: int | None = None
    label: ExposureLabel | None = None


class CollectionsMetric(Metric):
    """Metric 3: the Schedule H collection flags and bad debt as a share of patient revenue."""

    lawsuits: bool | None = None
    liens: bool | None = None
    credit_reporting: bool | None = None
    sells_debt: bool | None = None
    # One flag for lawsuits, liens and body attachments together (Schedule H since 2016).
    legal_action: bool | None = None
    flag_line: Literal["engaged", "permitted"] | None = None
    # What the facility's policy permits (line 18): credit_reporting, selling_debt, legal_action,
    # other; empty when the line was not answered.
    permitted_actions: list[str] = Field(default_factory=list)
    schedule_h_year: int | None = None
    bad_debt_pct: float | None = None
    bad_debt_rank: float | None = None
    bad_debt_peers: int | None = None


class LedgerView(BaseModel):
    ccn: str
    ids: Ids = Field(default_factory=Ids)
    gap: GapMetric = Field(default_factory=GapMetric)
    exposure: ExposureMetric = Field(default_factory=ExposureMetric)
    collections: CollectionsMetric = Field(default_factory=CollectionsMetric)
    computed_on: date | None = None

    def expect_lines(self) -> list[tuple[str, str]]:
        """(short line, advice) for each metric that has a short line: the result screen's
        form of the block. A metric with no short line is left out rather than shown as its
        headline alone."""
        return [
            (metric.short, metric.advice)
            for metric in (self.gap, self.collections, self.exposure)
            if metric.status is not MetricStatus.UNKNOWN and metric.short
        ]
