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
    kind: Literal["mail", "fax", "email", "portal", "in_person", "phone"]
    detail: str


class Apply(BaseModel):
    form_url: Cited[str] | None = None
    form_version: Cited[str] | None = None
    documents_required: Cited[list[DocType]] | None = None
    # Documents hospitals asked patients for that the policy text does not list (layer reported).
    documents_reported: Cited[list[DocType]] | None = None
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
