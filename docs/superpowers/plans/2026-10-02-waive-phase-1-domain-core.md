# Waive Phase 1 — Domain Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pure, fully tested domain logic: 2026 poverty guidelines, the cited procedure-sheet schema with a fictional sample hospital, eligibility, 501(r) deadlines, plain-language messages, and exact-quote verification.

**Architecture:** No I/O and no network. `waive.rules` holds deterministic math and wording; `waive.atlas.schema` defines the procedure sheet (every fact is a `Cited[T]`); `waive.atlas.verify` checks every documented field against its source text. A fictional hospital in `waive.atlas.samples` gives tests, demos and UI work one consistent fixture.

**Tech Stack:** Python 3.12, pydantic v2, pytest, hypothesis.

**Spec:** `docs/superpowers/specs/2026-10-02-waive-design.md` (§7, §8 step 5, §9 steps 6 and 10, §11, §12)

## Global Constraints

- No network calls and no file writes outside `tmp_path` in tests.
- Eligibility math is deterministic; models never compute eligibility.
- 2026 HHS poverty guidelines (verified 2026-10-02 from ASPE): 48 states and DC $15,960 + $5,680 per additional person; Alaska $19,950 + $7,100; Hawaii $18,360 + $6,530.
- 501(r) minimums: application window ≥ 240 days after the first post-discharge bill; no extraordinary collection actions before day 120.
- Senior wording: "likely", never "guaranteed"; sentences ≤ 14 words.
- Fictional sample data only: phone numbers 617-555-01xx, domain `example.org`.

---

### Task 1.1: Poverty guidelines

**Files:**
- Create: `src/waive/rules/__init__.py`, `src/waive/rules/data/fpl_2026.json`, `src/waive/rules/fpl.py`
- Test: `tests/unit/test_fpl.py`

**Interfaces:**
- Produces: `poverty_guideline(household_size: int, state: str, year: int = 2026) -> Decimal`; `fpl_percent(annual_income: Decimal, household_size: int, state: str, year: int = 2026) -> Decimal` (two decimal places, half-up).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_fpl.py`:

```python
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from waive.rules.fpl import fpl_percent, poverty_guideline


@pytest.mark.parametrize(
    ("size", "state", "expected"),
    [
        (1, "MA", 15960),
        (2, "MA", 21640),
        (4, "MA", 33000),
        (8, "MA", 55720),
        (9, "MA", 61400),
        (1, "AK", 19950),
        (8, "AK", 69650),
        (1, "HI", 18360),
        (8, "HI", 64070),
    ],
)
def test_matches_2026_table(size, state, expected):
    assert poverty_guideline(size, state) == Decimal(expected)


def test_rosa_is_about_143_percent():
    assert fpl_percent(Decimal("22800"), 1, "MA") == Decimal("142.86")


def test_state_code_is_case_insensitive():
    assert poverty_guideline(1, "ak") == Decimal("19950")


def test_rejects_bad_inputs():
    with pytest.raises(ValueError):
        poverty_guideline(0, "MA")
    with pytest.raises(ValueError):
        fpl_percent(Decimal("-1"), 1, "MA")


@given(st.integers(min_value=0, max_value=500_000), st.integers(min_value=1, max_value=12))
def test_more_income_never_lowers_percent(income, size):
    lower = fpl_percent(Decimal(income), size, "MA")
    higher = fpl_percent(Decimal(income + 1000), size, "MA")
    assert higher >= lower
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_fpl.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.rules'`

- [ ] **Step 3: Implement**

`src/waive/rules/__init__.py`:

```python
"""Deterministic eligibility, deadline and wording rules."""
```

`src/waive/rules/data/fpl_2026.json`:

```json
{
  "year": 2026,
  "source": "HHS poverty guidelines, https://aspe.hhs.gov/topics/poverty-economic-mobility/poverty-guidelines (verified 2026-10-02)",
  "regions": {
    "contiguous": {"base": 15960, "per_additional": 5680},
    "AK": {"base": 19950, "per_additional": 7100},
    "HI": {"base": 18360, "per_additional": 6530}
  }
}
```

`src/waive/rules/fpl.py`:

```python
"""HHS poverty guidelines and percent-of-poverty math."""

import json
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from importlib import resources
from typing import Any


@cache
def _table(year: int) -> dict[str, Any]:
    path = resources.files("waive.rules") / "data" / f"fpl_{year}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def poverty_guideline(household_size: int, state: str, year: int = 2026) -> Decimal:
    if household_size < 1:
        raise ValueError("household_size must be at least 1")
    regions = _table(year)["regions"]
    region = regions.get(state.upper(), regions["contiguous"])
    return Decimal(region["base"] + (household_size - 1) * region["per_additional"])


def fpl_percent(annual_income: Decimal, household_size: int, state: str, year: int = 2026) -> Decimal:
    if annual_income < 0:
        raise ValueError("annual_income cannot be negative")
    percent = annual_income / poverty_guideline(household_size, state, year) * 100
    return percent.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
```

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_fpl.py -v`
Expected: all passed (13 test cases including parametrized ones)

- [ ] **Step 5: Commit**

```bash
git add src/waive/rules tests/unit/test_fpl.py
git commit -m "feat: 2026 poverty guidelines and percent-of-poverty math"
```

---

### Task 1.2: Procedure sheet schema and sample hospital

**Files:**
- Create: `src/waive/atlas/schema.py`, `src/waive/atlas/samples.py`
- Test: `tests/unit/test_schema.py`

**Interfaces:**
- Produces (in `waive.atlas.schema`): enums `Layer`, `SourceKind`, `DocType`, `SheetStatus`; models `SourceDoc`, `Cited[T]`, `DiscountTier`, `Eligibility`, `StateProgram`, `Programs`, `SubmitMethod`, `Apply`, `Collections`, `Contacts`, `Coverage`, `HospitalRef`, `ProcedureSheet` (with `field_paths() -> list[tuple[str, Cited]]` and `completeness() -> float`); constants `SECTIONS`, `CORE_FIELDS`.
- Produces (in `waive.atlas.samples`): `SAMPLE_SOURCE_ID: str`, `SAMPLE_POLICY_TEXT: str`, `st_example_sheet(checked_on: date = date(2026, 10, 2)) -> ProcedureSheet`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_schema.py`:

```python
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from waive.atlas.samples import st_example_sheet
from waive.atlas.schema import Cited, DiscountTier, Eligibility, Layer, ProcedureSheet

TODAY = date(2026, 10, 2)


def test_documented_field_requires_quote_and_source():
    with pytest.raises(ValidationError):
        Cited[int](value=240, checked_on=TODAY)


def test_reported_field_requires_support():
    with pytest.raises(ValidationError):
        Cited[int](value=41, layer=Layer.REPORTED, checked_on=TODAY)
    reported = Cited[int](value=41, layer=Layer.REPORTED, support_count=5, checked_on=TODAY)
    assert reported.support_count == 5


def test_discount_tiers_must_not_overlap():
    tiers = [
        DiscountTier(
            min_fpl_exclusive=Decimal("200"), max_fpl_inclusive=Decimal("300"), discount_percent=50
        ),
        DiscountTier(
            min_fpl_exclusive=Decimal("250"), max_fpl_inclusive=Decimal("400"), discount_percent=25
        ),
    ]
    with pytest.raises(ValidationError):
        Eligibility(
            discount_tiers=Cited[list[DiscountTier]](
                value=tiers, quote="discount tiers text", source_id="s", checked_on=TODAY
            )
        )


def test_sheet_rejects_unknown_source():
    data = st_example_sheet().model_dump()
    data["contacts"]["phone"]["source_id"] = "missing-source"
    with pytest.raises(ValidationError):
        ProcedureSheet.model_validate(data)


def test_field_paths_and_completeness():
    sheet = st_example_sheet()
    paths = [path for path, _ in sheet.field_paths()]
    assert "eligibility.free_care_max_fpl" in paths
    assert "collections.eca_wait_days" in paths
    assert len(paths) == 8
    assert sheet.completeness() == pytest.approx(5 / 6)


def test_sheet_round_trips_through_json():
    sheet = st_example_sheet()
    assert ProcedureSheet.model_validate_json(sheet.model_dump_json()) == sheet
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.atlas.samples'`

- [ ] **Step 3: Implement the schema**

`src/waive/atlas/schema.py`:

```python
"""Procedure sheet: one cited, versioned record per hospital (spec section 7)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field, model_validator

T = TypeVar("T")


class Layer(StrEnum):
    DOCUMENTED = "documented"
    REPORTED = "reported"


class SourceKind(StrEnum):
    HOSPITAL_WEB = "hospital_web"
    STATE_REPOSITORY = "state_repository"
    PATIENT_PHOTO = "patient_photo"
    IRS_990 = "irs_990"


class DocType(StrEnum):
    PHOTO_ID = "photo_id"
    PROOF_OF_INCOME = "proof_of_income"
    SOCIAL_SECURITY_LETTER = "social_security_letter"
    TAX_RETURN = "tax_return"
    PAY_STUBS = "pay_stubs"
    BANK_STATEMENTS = "bank_statements"
    PROOF_OF_RESIDENCY = "proof_of_residency"
    INSURANCE_CARD = "insurance_card"
    MEDICAID_DENIAL = "medicaid_denial"
    OTHER = "other"


class SheetStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    HELD = "held"


class SourceDoc(BaseModel):
    id: str
    kind: SourceKind
    url: str | None = None
    title: str = ""
    fetched_on: date
    sha256: str
    effective_date: date | None = None


class Cited(BaseModel, Generic[T]):
    """A fact plus where it came from. Documented facts quote their source exactly."""

    value: T
    layer: Layer = Layer.DOCUMENTED
    quote: str | None = None
    source_id: str | None = None
    checked_on: date
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    support_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check_layer(self) -> Cited[T]:
        if self.layer is Layer.DOCUMENTED and not (self.quote and self.source_id):
            raise ValueError("documented fields need a quote and a source_id")
        if self.layer is Layer.REPORTED and self.support_count < 1:
            raise ValueError("reported fields need support_count of at least 1")
        return self


class DiscountTier(BaseModel):
    min_fpl_exclusive: Decimal = Field(ge=0)
    max_fpl_inclusive: Decimal = Field(gt=0)
    discount_percent: int = Field(ge=1, le=99)


class Eligibility(BaseModel):
    free_care_max_fpl: Cited[Decimal] | None = None
    discount_tiers: Cited[list[DiscountTier]] | None = None
    asset_test: Cited[bool] | None = None
    residency: Cited[list[str]] | None = None
    insured_patients_covered: Cited[bool] | None = None
    min_balance: Cited[Decimal] | None = None
    services_excluded: Cited[list[str]] | None = None

    @model_validator(mode="after")
    def _check_tiers(self) -> Eligibility:
        if self.discount_tiers is None:
            return self
        tiers = self.discount_tiers.value
        for tier in tiers:
            if tier.max_fpl_inclusive <= tier.min_fpl_exclusive:
                raise ValueError("each discount tier needs max above min")
        for lower, upper in zip(tiers, tiers[1:], strict=False):
            if upper.min_fpl_exclusive < lower.max_fpl_inclusive:
                raise ValueError("discount tiers must be ascending and must not overlap")
        return self


class StateProgram(BaseModel):
    name: str
    how_to_apply: str


class Programs(BaseModel):
    presumptive: Cited[list[str]] | None = None
    state_programs: Cited[list[StateProgram]] | None = None


class SubmitMethod(BaseModel):
    kind: Literal["mail", "fax", "email", "portal", "in_person"]
    detail: str


class Apply(BaseModel):
    form_url: Cited[str] | None = None
    form_version: Cited[str] | None = None
    documents_required: Cited[list[DocType]] | None = None
    submit_methods: Cited[list[SubmitMethod]] | None = None
    window_days_from_first_bill: Cited[int] | None = None
    decision_days: Cited[int] | None = None
    decision_days_reported: Cited[int] | None = None


class Collections(BaseModel):
    eca_wait_days: Cited[int] | None = None
    collection_agencies: Cited[list[str]] | None = None


class Contacts(BaseModel):
    phone: Cited[str] | None = None
    hours: Cited[str] | None = None
    languages: Cited[list[str]] | None = None


class Coverage(BaseModel):
    facilities: Cited[list[str]] | None = None
    provider_list_url: Cited[str] | None = None


class HospitalRef(BaseModel):
    ccn: str
    name: str
    city: str
    state: str = Field(min_length=2, max_length=2)
    zip: str
    phone: str | None = None
    ownership: str
    website_domain: str | None = None
    system: str | None = None


SECTIONS = ("eligibility", "programs", "apply", "collections", "contacts", "coverage")
CORE_FIELDS = (
    "eligibility.free_care_max_fpl",
    "eligibility.discount_tiers",
    "apply.form_url",
    "apply.documents_required",
    "apply.submit_methods",
    "contacts.phone",
)


class ProcedureSheet(BaseModel):
    hospital: HospitalRef
    version: int = Field(ge=1)
    status: SheetStatus = SheetStatus.DRAFT
    eligibility: Eligibility = Field(default_factory=Eligibility)
    programs: Programs = Field(default_factory=Programs)
    apply: Apply = Field(default_factory=Apply)
    collections: Collections = Field(default_factory=Collections)
    contacts: Contacts = Field(default_factory=Contacts)
    coverage: Coverage = Field(default_factory=Coverage)
    sources: list[SourceDoc] = Field(default_factory=list)
    accuracy: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_sources(self) -> ProcedureSheet:
        known = {source.id for source in self.sources}
        for path, cited in self.field_paths():
            if cited.source_id is not None and cited.source_id not in known:
                raise ValueError(f"{path} cites unknown source {cited.source_id}")
        return self

    def field_paths(self) -> list[tuple[str, Cited[Any]]]:
        found: list[tuple[str, Cited[Any]]] = []
        for section_name in SECTIONS:
            section = getattr(self, section_name)
            for field_name in type(section).model_fields:
                value = getattr(section, field_name)
                if isinstance(value, Cited):
                    found.append((f"{section_name}.{field_name}", value))
        return found

    def completeness(self) -> float:
        present = {path for path, _ in self.field_paths()}
        return sum(1 for path in CORE_FIELDS if path in present) / len(CORE_FIELDS)
```

- [ ] **Step 4: Implement the sample hospital**

`src/waive/atlas/samples.py`:

```python
"""A fictional hospital used by tests, demos and UI work. Not a real policy."""

import hashlib
from datetime import date
from decimal import Decimal
from typing import Any

from waive.atlas.schema import (
    Apply,
    Cited,
    Collections,
    Contacts,
    DiscountTier,
    DocType,
    Eligibility,
    HospitalRef,
    ProcedureSheet,
    Programs,
    SheetStatus,
    SourceDoc,
    SourceKind,
    SubmitMethod,
)

SAMPLE_SOURCE_ID = "src-st-example-fap"

SAMPLE_POLICY_TEXT = (
    "St. Example Medical Center Financial Assistance Policy. Effective March 1, 2026.\n"
    "Patients with household income at or below 250% of the Federal Poverty Guidelines "
    "are eligible for free care.\n"
    "Patients with household income above 250% and at or below 400% of the Federal Poverty "
    "Guidelines receive a 60% discount.\n"
    "Patients enrolled in MassHealth or SNAP are presumptively eligible for free care.\n"
    "Applicants must provide a photo ID and one proof of income.\n"
    "Applications may be mailed to Patient Financial Services, 1 Example Way, Boston, MA 02118, "
    "or faxed to 617-555-0199.\n"
    "Applications are accepted up to 240 days after the first post-discharge billing statement.\n"
    "The hospital will not begin extraordinary collection actions before 120 days after the "
    "first post-discharge billing statement.\n"
    "Questions: call 617-555-0100.\n"
)


def st_example_sheet(checked_on: date = date(2026, 10, 2)) -> ProcedureSheet:
    def cite(value: Any, quote: str) -> dict[str, Any]:
        return {
            "value": value,
            "quote": quote,
            "source_id": SAMPLE_SOURCE_ID,
            "checked_on": checked_on,
        }

    return ProcedureSheet(
        hospital=HospitalRef(
            ccn="229999",
            name="St. Example Medical Center",
            city="Boston",
            state="MA",
            zip="02118",
            phone="617-555-0100",
            ownership="Voluntary non-profit - Private",
            website_domain="example.org",
        ),
        version=1,
        status=SheetStatus.PUBLISHED,
        eligibility=Eligibility(
            free_care_max_fpl=Cited[Decimal](
                **cite(
                    Decimal("250"),
                    "household income at or below 250% of the Federal Poverty Guidelines "
                    "are eligible for free care",
                )
            ),
            discount_tiers=Cited[list[DiscountTier]](
                **cite(
                    [
                        DiscountTier(
                            min_fpl_exclusive=Decimal("250"),
                            max_fpl_inclusive=Decimal("400"),
                            discount_percent=60,
                        )
                    ],
                    "above 250% and at or below 400% of the Federal Poverty Guidelines "
                    "receive a 60% discount",
                )
            ),
        ),
        programs=Programs(
            presumptive=Cited[list[str]](
                **cite(
                    ["MassHealth", "SNAP"],
                    "Patients enrolled in MassHealth or SNAP are presumptively eligible "
                    "for free care",
                )
            )
        ),
        apply=Apply(
            documents_required=Cited[list[DocType]](
                **cite(
                    [DocType.PHOTO_ID, DocType.PROOF_OF_INCOME],
                    "Applicants must provide a photo ID and one proof of income",
                )
            ),
            submit_methods=Cited[list[SubmitMethod]](
                **cite(
                    [
                        SubmitMethod(
                            kind="mail",
                            detail="Patient Financial Services, 1 Example Way, Boston, MA 02118",
                        ),
                        SubmitMethod(kind="fax", detail="617-555-0199"),
                    ],
                    "Applications may be mailed to Patient Financial Services, 1 Example Way, "
                    "Boston, MA 02118, or faxed to 617-555-0199",
                )
            ),
            window_days_from_first_bill=Cited[int](
                **cite(
                    240,
                    "Applications are accepted up to 240 days after the first post-discharge "
                    "billing statement",
                )
            ),
        ),
        collections=Collections(
            eca_wait_days=Cited[int](
                **cite(
                    120,
                    "will not begin extraordinary collection actions before 120 days after the "
                    "first post-discharge billing statement",
                )
            )
        ),
        contacts=Contacts(phone=Cited[str](**cite("617-555-0100", "Questions: call 617-555-0100"))),
        sources=[
            SourceDoc(
                id=SAMPLE_SOURCE_ID,
                kind=SourceKind.HOSPITAL_WEB,
                url="https://www.example.org/st-example/financial-assistance.pdf",
                title="Financial Assistance Policy",
                fetched_on=checked_on,
                sha256=hashlib.sha256(SAMPLE_POLICY_TEXT.encode("utf-8")).hexdigest(),
                effective_date=date(2026, 3, 1),
            )
        ],
    )
```

- [ ] **Step 5: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_schema.py -v`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add src/waive/atlas/schema.py src/waive/atlas/samples.py tests/unit/test_schema.py
git commit -m "feat: cited procedure sheet schema and fictional sample hospital"
```

---

### Task 1.3: Eligibility engine

**Files:**
- Create: `src/waive/rules/eligibility.py`
- Test: `tests/unit/test_eligibility.py`

**Interfaces:**
- Consumes: `ProcedureSheet`, `Cited` (1.2); `fpl_percent` (1.1).
- Produces: `Tier` (`free`, `discount`, `not_eligible`, `needs_info`, `unknown`); `Household(size: int, annual_income: Decimal | None, state: str, programs: tuple[str, ...] = ())`; `Reason(text, field_path, quote, source_id)`; `EligibilityResult(tier, fpl_percent=None, discount_percent=None, reasons=(), missing=(), warnings=())`; `evaluate_eligibility(sheet: ProcedureSheet, household: Household) -> EligibilityResult`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_eligibility.py`:

```python
from datetime import date
from decimal import Decimal

from waive.atlas.samples import SAMPLE_SOURCE_ID, st_example_sheet
from waive.atlas.schema import Cited
from waive.rules.eligibility import Household, Tier, evaluate_eligibility

GUIDELINE_1 = Decimal("15960")
TODAY = date(2026, 10, 2)


def household(income, size=1, state="MA", programs=()):
    return Household(size=size, annual_income=income, state=state, programs=programs)


def with_eligibility(**changes):
    sheet = st_example_sheet()
    return sheet.model_copy(update={"eligibility": sheet.eligibility.model_copy(update=changes)})


def test_rosa_gets_free_care_with_citation():
    result = evaluate_eligibility(st_example_sheet(), household(Decimal("22800")))
    assert result.tier is Tier.FREE
    assert result.fpl_percent == Decimal("142.86")
    assert result.discount_percent == 100
    assert result.reasons[0].field_path == "eligibility.free_care_max_fpl"
    assert "250%" in result.reasons[0].quote


def test_exactly_at_free_limit_is_free():
    result = evaluate_eligibility(st_example_sheet(), household(GUIDELINE_1 * Decimal("2.5")))
    assert result.tier is Tier.FREE


def test_discount_tier():
    result = evaluate_eligibility(st_example_sheet(), household(GUIDELINE_1 * 3))
    assert (result.tier, result.discount_percent) == (Tier.DISCOUNT, 60)
    assert result.reasons[0].field_path == "eligibility.discount_tiers"


def test_above_all_limits_is_not_eligible():
    result = evaluate_eligibility(st_example_sheet(), household(GUIDELINE_1 * 5))
    assert result.tier is Tier.NOT_ELIGIBLE
    assert result.fpl_percent == Decimal("500.00")


def test_missing_income_needs_info():
    result = evaluate_eligibility(st_example_sheet(), household(None))
    assert result.tier is Tier.NEEDS_INFO
    assert result.missing == ("household income",)


def test_presumptive_program_wins_over_income():
    result = evaluate_eligibility(
        st_example_sheet(), household(GUIDELINE_1 * 5, programs=("snap",))
    )
    assert result.tier is Tier.FREE
    assert result.reasons[0].field_path == "programs.presumptive"


def test_residency_mismatch_is_not_eligible():
    residency = Cited[list[str]](
        value=["MA"],
        quote="Massachusetts residents only",
        source_id=SAMPLE_SOURCE_ID,
        checked_on=TODAY,
    )
    sheet = with_eligibility(residency=residency)
    result = evaluate_eligibility(sheet, household(Decimal("1000"), state="NH"))
    assert result.tier is Tier.NOT_ELIGIBLE
    assert result.reasons[0].field_path == "eligibility.residency"


def test_sheet_without_limits_is_unknown():
    sheet = with_eligibility(free_care_max_fpl=None, discount_tiers=None)
    result = evaluate_eligibility(sheet, household(Decimal("1000")))
    assert result.tier is Tier.UNKNOWN
    assert result.missing == ("hospital income limits",)


def test_asset_test_adds_warning():
    assets = Cited[bool](
        value=True, quote="assets are considered", source_id=SAMPLE_SOURCE_ID, checked_on=TODAY
    )
    result = evaluate_eligibility(with_eligibility(asset_test=assets), household(Decimal("22800")))
    assert result.tier is Tier.FREE
    assert result.warnings == ("This hospital also looks at savings and other assets.",)
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_eligibility.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.rules.eligibility'`

- [ ] **Step 3: Implement**

`src/waive/rules/eligibility.py`:

```python
"""Deterministic eligibility check against a procedure sheet (spec section 9, step 6)."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from waive.atlas.schema import Cited, ProcedureSheet
from waive.rules.fpl import fpl_percent

ASSET_WARNING = "This hospital also looks at savings and other assets."


class Tier(StrEnum):
    FREE = "free"
    DISCOUNT = "discount"
    NOT_ELIGIBLE = "not_eligible"
    NEEDS_INFO = "needs_info"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Household:
    size: int
    annual_income: Decimal | None
    state: str
    programs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Reason:
    text: str
    field_path: str
    quote: str | None
    source_id: str | None


@dataclass(frozen=True)
class EligibilityResult:
    tier: Tier
    fpl_percent: Decimal | None = None
    discount_percent: int | None = None
    reasons: tuple[Reason, ...] = ()
    missing: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _reason(text: str, path: str, cited: Cited[Any]) -> Reason:
    return Reason(text=text, field_path=path, quote=cited.quote, source_id=cited.source_id)


def evaluate_eligibility(sheet: ProcedureSheet, household: Household) -> EligibilityResult:
    eligibility = sheet.eligibility
    warnings: tuple[str, ...] = ()
    if eligibility.asset_test is not None and eligibility.asset_test.value:
        warnings = (ASSET_WARNING,)

    residency = eligibility.residency
    if residency is not None:
        allowed = {state.upper() for state in residency.value}
        if household.state.upper() not in allowed:
            text = f"The policy covers residents of {', '.join(residency.value)}."
            return EligibilityResult(
                Tier.NOT_ELIGIBLE,
                reasons=(_reason(text, "eligibility.residency", residency),),
                warnings=warnings,
            )

    presumptive = sheet.programs.presumptive
    if presumptive is not None:
        listed = {program.lower(): program for program in presumptive.value}
        matches = [listed[p.lower()] for p in household.programs if p.lower() in listed]
        if matches:
            text = f"People enrolled in {matches[0]} qualify for free care without an income check."
            return EligibilityResult(
                Tier.FREE,
                discount_percent=100,
                reasons=(_reason(text, "programs.presumptive", presumptive),),
                warnings=warnings,
            )

    free_limit = eligibility.free_care_max_fpl
    tiers = eligibility.discount_tiers
    has_tiers = tiers is not None and bool(tiers.value)
    if free_limit is None and not has_tiers:
        return EligibilityResult(
            Tier.UNKNOWN, missing=("hospital income limits",), warnings=warnings
        )
    if household.annual_income is None:
        return EligibilityResult(Tier.NEEDS_INFO, missing=("household income",), warnings=warnings)

    percent = fpl_percent(household.annual_income, household.size, household.state)
    if free_limit is not None and percent <= free_limit.value:
        text = (
            f"Income is {percent:.0f}% of the poverty line; "
            f"free care covers up to {free_limit.value:.0f}%."
        )
        return EligibilityResult(
            Tier.FREE,
            percent,
            100,
            (_reason(text, "eligibility.free_care_max_fpl", free_limit),),
            warnings=warnings,
        )

    if tiers is not None and has_tiers:
        for tier in tiers.value:
            if tier.min_fpl_exclusive < percent <= tier.max_fpl_inclusive:
                text = (
                    f"Income is {percent:.0f}% of the poverty line; the policy gives a "
                    f"{tier.discount_percent}% discount between {tier.min_fpl_exclusive:.0f}% "
                    f"and {tier.max_fpl_inclusive:.0f}%."
                )
                return EligibilityResult(
                    Tier.DISCOUNT,
                    percent,
                    tier.discount_percent,
                    (_reason(text, "eligibility.discount_tiers", tiers),),
                    warnings=warnings,
                )
        top = max(tier.max_fpl_inclusive for tier in tiers.value)
        cited: Cited[Any] = tiers
        path = "eligibility.discount_tiers"
    else:
        assert free_limit is not None
        top = free_limit.value
        cited = free_limit
        path = "eligibility.free_care_max_fpl"
    text = f"Income is {percent:.0f}% of the poverty line; this policy helps up to {top:.0f}%."
    return EligibilityResult(
        Tier.NOT_ELIGIBLE, percent, None, (_reason(text, path, cited),), warnings=warnings
    )
```

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_eligibility.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add src/waive/rules/eligibility.py tests/unit/test_eligibility.py
git commit -m "feat: deterministic eligibility engine with cited reasons"
```

---

### Task 1.4: Deadlines

**Files:**
- Create: `src/waive/rules/deadlines.py`
- Test: `tests/unit/test_deadlines.py`

**Interfaces:**
- Consumes: `ProcedureSheet` (1.2).
- Produces: `MIN_WINDOW_DAYS = 240`, `MIN_ECA_WAIT_DAYS = 120`; `Deadlines(application_deadline: date, collections_allowed_from: date, days_left_to_apply: int, collection_notice_too_early: bool | None)`; `compute_deadlines(first_statement, today, *, window_days=240, eca_wait_days=120, collection_notice=None) -> Deadlines`; `deadlines_for(sheet, first_statement, today, collection_notice=None) -> Deadlines`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_deadlines.py`:

```python
from datetime import date

import pytest

from waive.atlas.samples import SAMPLE_SOURCE_ID, st_example_sheet
from waive.atlas.schema import Cited
from waive.rules.deadlines import compute_deadlines, deadlines_for

FIRST = date(2026, 9, 3)
TODAY = date(2026, 10, 2)


def with_window(days):
    sheet = st_example_sheet()
    window = Cited[int](
        value=days,
        quote=f"accepted up to {days} days",
        source_id=SAMPLE_SOURCE_ID,
        checked_on=TODAY,
    )
    return sheet.model_copy(
        update={"apply": sheet.apply.model_copy(update={"window_days_from_first_bill": window})}
    )


def test_standard_501r_windows():
    deadlines = compute_deadlines(FIRST, TODAY)
    assert deadlines.application_deadline == date(2027, 5, 1)
    assert deadlines.collections_allowed_from == date(2027, 1, 1)
    assert deadlines.days_left_to_apply == 211
    assert deadlines.collection_notice_too_early is None


def test_flags_collection_notice_before_day_120():
    early = compute_deadlines(FIRST, TODAY, collection_notice=date(2026, 11, 15))
    on_time = compute_deadlines(FIRST, TODAY, collection_notice=date(2027, 1, 2))
    assert early.collection_notice_too_early is True
    assert on_time.collection_notice_too_early is False


def test_rejects_windows_shorter_than_the_law():
    with pytest.raises(ValueError):
        compute_deadlines(FIRST, TODAY, window_days=200)
    with pytest.raises(ValueError):
        compute_deadlines(FIRST, TODAY, eca_wait_days=90)


def test_uses_a_more_generous_policy_window():
    assert deadlines_for(with_window(365), FIRST, TODAY).application_deadline == date(2027, 9, 3)


def test_never_shorter_than_the_law_even_if_the_sheet_says_so():
    assert deadlines_for(with_window(90), FIRST, TODAY).application_deadline == date(2027, 5, 1)
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_deadlines.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.rules.deadlines'`

- [ ] **Step 3: Implement**

`src/waive/rules/deadlines.py`:

```python
"""501(r) timing: application window and collections protection (spec section 9, step 10)."""

from dataclasses import dataclass
from datetime import date, timedelta

from waive.atlas.schema import ProcedureSheet

MIN_WINDOW_DAYS = 240
MIN_ECA_WAIT_DAYS = 120


@dataclass(frozen=True)
class Deadlines:
    application_deadline: date
    collections_allowed_from: date
    days_left_to_apply: int
    collection_notice_too_early: bool | None


def compute_deadlines(
    first_statement: date,
    today: date,
    *,
    window_days: int = MIN_WINDOW_DAYS,
    eca_wait_days: int = MIN_ECA_WAIT_DAYS,
    collection_notice: date | None = None,
) -> Deadlines:
    if window_days < MIN_WINDOW_DAYS:
        raise ValueError("501(r) requires an application window of at least 240 days")
    if eca_wait_days < MIN_ECA_WAIT_DAYS:
        raise ValueError("501(r) requires at least 120 days before collection actions")
    deadline = first_statement + timedelta(days=window_days)
    collections_from = first_statement + timedelta(days=eca_wait_days)
    too_early = None if collection_notice is None else collection_notice < collections_from
    return Deadlines(deadline, collections_from, (deadline - today).days, too_early)


def deadlines_for(
    sheet: ProcedureSheet,
    first_statement: date,
    today: date,
    collection_notice: date | None = None,
) -> Deadlines:
    window = sheet.apply.window_days_from_first_bill
    wait = sheet.collections.eca_wait_days
    return compute_deadlines(
        first_statement,
        today,
        window_days=max(window.value if window else MIN_WINDOW_DAYS, MIN_WINDOW_DAYS),
        eca_wait_days=max(wait.value if wait else MIN_ECA_WAIT_DAYS, MIN_ECA_WAIT_DAYS),
        collection_notice=collection_notice,
    )
```

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_deadlines.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/waive/rules/deadlines.py tests/unit/test_deadlines.py
git commit -m "feat: 501(r) application window and collections timing"
```

---

### Task 1.5: Plain-language messages

**Files:**
- Create: `src/waive/rules/explain.py`
- Test: `tests/unit/test_explain.py`

**Interfaces:**
- Consumes: `EligibilityResult`, `Tier` (1.3).
- Produces: `senior_message(result: EligibilityResult, hospital_name: str) -> str`; `caregiver_summary(result: EligibilityResult, hospital_name: str) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_explain.py`:

```python
import re
from decimal import Decimal

from waive.atlas.samples import st_example_sheet
from waive.rules.eligibility import EligibilityResult, Household, Tier, evaluate_eligibility
from waive.rules.explain import caregiver_summary, senior_message

ALL_TIERS = [
    EligibilityResult(Tier.FREE, discount_percent=100),
    EligibilityResult(Tier.DISCOUNT, discount_percent=60),
    EligibilityResult(Tier.NOT_ELIGIBLE),
    EligibilityResult(Tier.NEEDS_INFO),
    EligibilityResult(Tier.UNKNOWN),
]


def test_senior_messages_are_short_and_hedged():
    for result in ALL_TIERS:
        message = senior_message(result, "St. Example")
        sentences = [part for part in re.split(r"[.!?]\s*", message) if part]
        assert all(len(sentence.split()) <= 14 for sentence in sentences), message
        assert "guarantee" not in message.lower()
    assert "likely" in senior_message(ALL_TIERS[0], "St. Example")
    assert "60%" in senior_message(ALL_TIERS[1], "St. Example")


def test_caregiver_summary_cites_the_policy():
    result = evaluate_eligibility(st_example_sheet(), Household(1, Decimal("22800"), "MA"))
    summary = caregiver_summary(result, "St. Example Medical Center")
    assert summary.startswith("Likely free care at St. Example Medical Center.")
    assert 'Policy says: "household income at or below 250%' in summary
    assert summary.endswith("This is an estimate. The hospital makes the final decision.")


def test_caregiver_summary_lists_missing_information():
    summary = caregiver_summary(
        EligibilityResult(Tier.NEEDS_INFO, missing=("household income",)), "St. Example"
    )
    assert "Still needed: household income." in summary
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_explain.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.rules.explain'`

- [ ] **Step 3: Implement**

`src/waive/rules/explain.py`:

```python
"""Plain-language messages for seniors and caregivers (spec sections 9 and 11)."""

from waive.rules.eligibility import EligibilityResult, Tier

DISCLAIMER = "This is an estimate. The hospital makes the final decision."


def senior_message(result: EligibilityResult, hospital_name: str) -> str:
    if result.tier is Tier.FREE:
        return (
            "Good news. You likely do not have to pay this bill. "
            f"{hospital_name} gives free care to people like you. "
            "We can fill in the form for you."
        )
    if result.tier is Tier.DISCOUNT:
        return (
            f"Good news. You likely get {result.discount_percent}% off this bill. "
            f"{hospital_name} gives discounts to people like you. "
            "We can fill in the form for you."
        )
    if result.tier is Tier.NOT_ELIGIBLE:
        return (
            f"This bill likely does not qualify under {hospital_name}'s rules. "
            "Your helper can look at other options with you."
        )
    if result.tier is Tier.NEEDS_INFO:
        return "We need one more thing to check this bill. Your helper will ask you."
    return f"We are still learning {hospital_name}'s rules. We will tell you soon."


def caregiver_summary(result: EligibilityResult, hospital_name: str) -> str:
    headline = {
        Tier.FREE: "Likely free care",
        Tier.DISCOUNT: f"Likely {result.discount_percent}% discount",
        Tier.NOT_ELIGIBLE: "Likely not eligible",
        Tier.NEEDS_INFO: "More information needed",
        Tier.UNKNOWN: "Policy not yet known",
    }[result.tier]
    lines = [f"{headline} at {hospital_name}."]
    for reason in result.reasons:
        lines.append(reason.text)
        if reason.quote:
            lines.append(f'Policy says: "{reason.quote}"')
    if result.missing:
        lines.append("Still needed: " + ", ".join(result.missing) + ".")
    lines.extend(result.warnings)
    lines.append(DISCLAIMER)
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_explain.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/waive/rules/explain.py tests/unit/test_explain.py
git commit -m "feat: plain-language senior messages and cited caregiver summaries"
```

---

### Task 1.6: Quote verification

**Files:**
- Create: `src/waive/atlas/verify.py`
- Test: `tests/unit/test_verify.py`

**Interfaces:**
- Consumes: `ProcedureSheet`, `Layer`, `DiscountTier` (1.2); `SAMPLE_POLICY_TEXT`, `SAMPLE_SOURCE_ID`, `st_example_sheet` (1.2).
- Produces: `MIN_QUOTE_CHARS = 12`; `normalize(text) -> str`; `quote_found(quote, document_text) -> bool`; `value_in_quote(value, quote) -> bool`; `VerificationReport(accepted: list[str], rejected: list[tuple[str, str]])` with `.ok`; `verify_sheet(sheet, documents: dict[str, str]) -> VerificationReport`. Rejection reasons are exactly `"source text missing"`, `"quote not found in source"`, `"value not in quote"`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_verify.py`:

```python
from decimal import Decimal

from waive.atlas.samples import SAMPLE_POLICY_TEXT, SAMPLE_SOURCE_ID, st_example_sheet
from waive.atlas.verify import normalize, quote_found, value_in_quote, verify_sheet

DOCS = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT}


def test_sample_sheet_verifies_completely():
    sheet = st_example_sheet()
    report = verify_sheet(sheet, DOCS)
    assert report.ok, report.rejected
    assert len(report.accepted) == len(sheet.field_paths())


def test_tampered_value_is_rejected():
    sheet = st_example_sheet()
    cited = sheet.eligibility.free_care_max_fpl.model_copy(update={"value": Decimal("300")})
    sheet = sheet.model_copy(
        update={"eligibility": sheet.eligibility.model_copy(update={"free_care_max_fpl": cited})}
    )
    report = verify_sheet(sheet, DOCS)
    assert ("eligibility.free_care_max_fpl", "value not in quote") in report.rejected


def test_invented_quote_is_rejected():
    sheet = st_example_sheet()
    cited = sheet.contacts.phone.model_copy(
        update={"quote": "Call our billing team any time at 617-555-0100"}
    )
    sheet = sheet.model_copy(
        update={"contacts": sheet.contacts.model_copy(update={"phone": cited})}
    )
    report = verify_sheet(sheet, DOCS)
    assert ("contacts.phone", "quote not found in source") in report.rejected


def test_missing_source_text_is_rejected():
    report = verify_sheet(st_example_sheet(), {})
    assert not report.ok
    assert all(reason == "source text missing" for _, reason in report.rejected)


def test_typography_and_line_breaks_do_not_break_matching():
    document = (
        "Patients enrolled in MassHealth or SNAP are presump-\ntively eligible "
        "for “free care”."
    )
    quote = 'enrolled in MassHealth or SNAP are presumptively eligible for "free care"'
    assert quote_found(quote, document)


def test_short_quotes_are_not_trusted():
    assert not quote_found("250%", SAMPLE_POLICY_TEXT)


def test_numbers_must_match_whole_tokens():
    assert value_in_quote(Decimal("250"), "at or below 250% of the guidelines")
    assert not value_in_quote(Decimal("250"), "at or below 2500 dollars")
    assert value_in_quote(1000, "balances over $1,000 qualify")


def test_normalize_collapses_whitespace_and_case():
    assert normalize("  Free CARE \n here ") == "free care here"
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_verify.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.atlas.verify'`

- [ ] **Step 3: Implement**

`src/waive/atlas/verify.py`:

```python
"""Exact-quote verification for procedure sheet fields (spec section 8, step 5)."""

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from waive.atlas.schema import DiscountTier, Layer, ProcedureSheet

MIN_QUOTE_CHARS = 12

_REPLACEMENTS = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "−": "-",
    "­": "",
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    for old, new in _REPLACEMENTS.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip().lower()


def _squash(text: str) -> str:
    """Hyphen- and space-insensitive form, for PDFs that split words across lines."""
    return re.sub(r"[\s\-]+", "", normalize(text))


def quote_found(quote: str, document_text: str) -> bool:
    normalized_quote = normalize(quote)
    if len(normalized_quote) < MIN_QUOTE_CHARS:
        return False
    if normalized_quote in normalize(document_text):
        return True
    return _squash(quote) in _squash(document_text)


def _number_in(number: Decimal, quote: str) -> bool:
    digits = format(number.normalize(), "f")
    haystack = normalize(quote).replace(",", "")
    return re.search(rf"(?<![\d.]){re.escape(digits)}(?!\d)", haystack) is not None


def value_in_quote(value: Any, quote: str) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int | Decimal):
        return _number_in(Decimal(value), quote)
    if isinstance(value, list) and value and all(isinstance(v, DiscountTier) for v in value):
        return all(
            _number_in(tier.min_fpl_exclusive, quote)
            and _number_in(tier.max_fpl_inclusive, quote)
            and _number_in(Decimal(tier.discount_percent), quote)
            for tier in value
        )
    return True


@dataclass
class VerificationReport:
    accepted: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejected


def verify_sheet(sheet: ProcedureSheet, documents: dict[str, str]) -> VerificationReport:
    report = VerificationReport()
    for path, cited in sheet.field_paths():
        if cited.layer is Layer.REPORTED:
            report.accepted.append(path)
            continue
        text = documents.get(cited.source_id or "")
        if text is None:
            report.rejected.append((path, "source text missing"))
        elif not quote_found(cited.quote or "", text):
            report.rejected.append((path, "quote not found in source"))
        elif not value_in_quote(cited.value, cited.quote or ""):
            report.rejected.append((path, "value not in quote"))
        else:
            report.accepted.append(path)
    return report
```

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_verify.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/waive/atlas/verify.py tests/unit/test_verify.py
git commit -m "feat: exact-quote verification for procedure sheet fields"
```

---

### Task 1.7: Phase 1 exit check

**Files:**
- Modify: `docs/PROGRESS.md`

- [ ] **Step 1: Run lint and the full suite with coverage**

Run: `uv run ruff check . && uv run pytest --cov=waive.rules --cov=waive.atlas.schema --cov=waive.atlas.verify --cov-report=term-missing --cov-fail-under=90`
Expected: `All checks passed!`, all tests pass, `Required test coverage of 90% reached`.

- [ ] **Step 2: If coverage is below 90%**

Read the `Missing` column, add a unit test for each uncovered branch in the module it belongs to (same style as the tests above), and re-run Step 1.

- [ ] **Step 3: Record and commit**

Tick tasks 1.1–1.7 in `docs/PROGRESS.md`, set "Current phase" to Phase 2 with next task "2.0 Write detailed Phase 2 plan", add a log line with the coverage figure.

```bash
git add docs/PROGRESS.md tests
git commit -m "chore: close Phase 1 with coverage check"
```

---

## Self-review

- **Spec coverage:** §7 schema → 1.2; §8 step 5 quote verification → 1.6; §9 step 6 eligibility → 1.3; §9 step 10 deadlines → 1.4; §11 wording ("likely", estimate disclaimer) → 1.5; §12 rows "Model invents a field value", "Wrong eligibility math", "Overpromising", "Missed deadlines" → 1.6, 1.1/1.3, 1.5, 1.4. Spec §18 poverty table item is closed (values in Global Constraints).
- **Placeholders:** none.
- **Type consistency:** `Cited`, `ProcedureSheet.field_paths()`, `Household`, `EligibilityResult`, `Tier` names match across 1.2–1.6; rejection reason strings match between implementation and tests.
