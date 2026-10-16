"""Ledger tables (Phase 10): identities, cost reports, Schedule H facts, exposure inputs and the
computed metrics. One row per CCN, or per CCN and year where the source is yearly.

Kept in the ledger package rather than `waive/db.py` so the branch adds files instead of editing
a hot one; `waive/db.py` imports this module at its end, which registers the rows on `Base` so
`init_db` creates them. Money columns are whole dollars as the cost report states them
(BigInteger: a system's total costs pass 2^31); shares and ranks are floats."""

from __future__ import annotations

from datetime import date

from sqlalchemy import BigInteger, Boolean, Date, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from waive.db import Base

# How a CCN was tied to an EIN, best first: Community Benefit Insight names the CCN outright;
# a sibling CCN shares the PECOS associate id of a CBI-matched hospital; a CBI or 990 facility
# with the same normalised name and ZIP; a fuzzy name match in the same city; set by hand.
MATCH_METHODS = ("cbi", "enrollments", "name_zip", "fuzzy", "manual")
# Which Schedule H line a collection flag came from: line 19 (actions the facility engaged in
# before checking eligibility) or line 18 (actions its policy permits).
FLAG_LINES = ("engaged", "permitted")


class HospitalIdRow(Base):
    """Who the hospital is to the IRS and to PECOS, and how Waive found out."""

    __tablename__ = "hospital_ids"

    ccn: Mapped[str] = mapped_column(ForeignKey("hospitals.ccn"), primary_key=True)
    ein: Mapped[str | None] = mapped_column(String(9), nullable=True, index=True)
    npi: Mapped[str | None] = mapped_column(String(10), nullable=True)
    associate_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    match_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    match_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The hospital's name as the matched source spells it (the 990 filer, or CBI's name).
    matched_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ein_vintage: Mapped[str | None] = mapped_column(String(60), nullable=True)
    npi_vintage: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # Hospital Enrollments' PROPRIETARY NONPROFIT column: N (nonprofit) or P (proprietary).
    nonprofit_flag: Mapped[str | None] = mapped_column(String(1), nullable=True)
    checked_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class CostReportRow(Base):
    """One CMS Hospital Provider Cost Report (HCRIS) per hospital and fiscal year."""

    __tablename__ = "ledger_cost_reports"

    ccn: Mapped[str] = mapped_column(ForeignKey("hospitals.ccn"), primary_key=True)
    fiscal_year: Mapped[int] = mapped_column(Integer, primary_key=True)
    rpt_rec_num: Mapped[str | None] = mapped_column(String(20), nullable=True)
    fy_begin: Mapped[date | None] = mapped_column(Date, nullable=True)
    fy_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    charity_care_cost: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bad_debt: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    uncompensated_care_cost: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_costs: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    net_patient_revenue: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    type_of_control: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hospital_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_file: Mapped[str | None] = mapped_column(String(120), nullable=True)
    vintage: Mapped[str | None] = mapped_column(String(80), nullable=True)
    loaded_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class ScheduleHRow(Base):
    """What one IRS Form 990 Schedule H says about this facility, per tax year."""

    __tablename__ = "ledger_schedule_h"

    ccn: Mapped[str] = mapped_column(ForeignKey("hospitals.ccn"), primary_key=True)
    tax_year: Mapped[int] = mapped_column(Integer, primary_key=True)
    ein: Mapped[str | None] = mapped_column(String(9), nullable=True, index=True)
    tax_period: Mapped[str | None] = mapped_column(String(6), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(24), nullable=True)
    filer_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    facility_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    facility_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    free_care_fpl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    discount_care_fpl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    asset_test: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    lawsuits: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    liens: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    credit_reporting: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sells_debt: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Schema 2016v3.0 and later fold lawsuits, liens and body attachments into one line-19 flag
    # (EngagedLegalJudicialProcessInd); `lawsuits` and `liens` come only from older returns.
    legal_action: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    flag_line: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Line 18, what the facility's policy permits, as a comma-separated list of
    # credit_reporting, selling_debt, legal_action, other, or "none"; empty when unanswered.
    permitted_actions: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Part I line 7a: financial assistance at cost as a percent of total expense.
    charity_pct_of_expense: Mapped[float | None] = mapped_column(Float, nullable=True)
    vintage: Mapped[str | None] = mapped_column(String(80), nullable=True)
    loaded_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class ExposureRow(Base):
    """Who the hospital serves: Medicare inpatient counts and the poor seniors in its ZIPs."""

    __tablename__ = "ledger_exposure"

    ccn: Mapped[str] = mapped_column(ForeignKey("hospitals.ccn"), primary_key=True)
    discharges: Mapped[int | None] = mapped_column(Integer, nullable=True)
    benes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dual_benes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # discharges × dual_benes ÷ benes
    dual_discharges: Mapped[float | None] = mapped_column(Float, nullable=True)
    zips_counted: Mapped[int | None] = mapped_column(Integer, nullable=True)
    zips_suppressed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Σ over service-area ZIPs of ACS B17024 people 65+ under 200% FPL × this hospital's share
    # of the ZIP's unsuppressed Medicare cases.
    poor_seniors_served: Mapped[float | None] = mapped_column(Float, nullable=True)
    inpatient_vintage: Mapped[str | None] = mapped_column(String(80), nullable=True)
    service_area_vintage: Mapped[str | None] = mapped_column(String(80), nullable=True)
    acs_vintage: Mapped[str | None] = mapped_column(String(80), nullable=True)
    loaded_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class LedgerMetricsRow(Base):
    """The three computed metrics with their ranks and peer counts, one row per CCN.
    `waive ledger behaviour` fills the gap and collections columns, `waive ledger exposure`
    the exposure columns; each stamps its own computed_on."""

    __tablename__ = "ledger_metrics"

    ccn: Mapped[str] = mapped_column(ForeignKey("hospitals.ccn"), primary_key=True)
    # Metric 1: stated free-care threshold (% FPL) against charity care at cost ÷ total costs.
    stated_fpl: Mapped[float | None] = mapped_column(Float, nullable=True)
    stated_source: Mapped[str | None] = mapped_column(String(10), nullable=True)
    stated_rank: Mapped[float | None] = mapped_column(Float, nullable=True)
    stated_peers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    charity_cost_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    charity_rank: Mapped[float | None] = mapped_column(Float, nullable=True)
    charity_peers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    charity_pct_990: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_report_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gap: Mapped[float | None] = mapped_column(Float, nullable=True)
    gap_label: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Metric 2: dual discharges + poor seniors served, ranked.
    exposure_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    exposure_rank: Mapped[float | None] = mapped_column(Float, nullable=True)
    exposure_peers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exposure_label: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Metric 3: the four Schedule H flags and bad debt ÷ net patient revenue, ranked.
    lawsuits: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    liens: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    credit_reporting: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sells_debt: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    legal_action: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    flag_line: Mapped[str | None] = mapped_column(String(10), nullable=True)
    permitted_actions: Mapped[str | None] = mapped_column(String(120), nullable=True)
    schedule_h_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bad_debt_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    bad_debt_rank: Mapped[float | None] = mapped_column(Float, nullable=True)
    bad_debt_peers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    behaviour_computed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    exposure_computed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
