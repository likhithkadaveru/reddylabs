"""API response models.

Two conventions run through all of these and matter more than the field lists:

* **Nothing is a bare number.** A supplier count travels with the scope statement
  that qualifies it; a score travels with its decomposition; a capability travels
  with its assertion level. A client that renders only the number is still
  rendering something defensible.
* **Absence is explicit.** `DataAvailability` distinguishes "we looked and there
  is nothing" from "we have not ingested this source", so the UI can show
  NO QUALIFYING SIGNALS and DATA UNAVAILABLE as the different statements they are.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DataAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    # We have the source and there is genuinely nothing.
    NO_QUALIFYING_SIGNALS = "NO_QUALIFYING_SIGNALS"
    # We have not ingested the source that would answer this.
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    # We have some data but too little to say anything.
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SourceDocumentRef(ORMModel):
    id: uuid.UUID
    source_key: str | None = None
    source_name: str | None = None
    publisher: str | None = None
    licence: str | None = None
    locator: str
    sha256: str
    retrieved_at: dt.datetime
    published_at: dt.datetime | None = None
    source_last_updated: dt.date | None = None
    disclaimer_text: str | None = None
    attribution_text: str | None = None


class EvidenceDetail(ORMModel):
    id: uuid.UUID
    claim_text: str
    extract_text: str | None = None
    structured_record: dict[str, Any] | None = None
    source_url: str | None = None
    extraction_method: str
    confidence: float
    event_date: dt.date | None = None
    published_at: dt.datetime | None = None
    retrieved_at: dt.datetime | None = None
    observed_at: dt.datetime | None = None
    source_document: SourceDocumentRef | None = None
    # Present only for model-assisted extraction, so a reader can audit it.
    llm_audit: dict[str, Any] | None = None


class SignalDetail(ORMModel):
    signal_key: str
    direction: str
    points: float
    max_points: float
    explanation: str
    feature_values: dict[str, Any] | None = None
    evidence_ids: list[str] | None = None


class CounterThesisDetail(ORMModel):
    concern_key: str
    severity: str
    explanation: str
    feature_values: dict[str, Any] | None = None
    evidence_ids: list[str] | None = None


class MoleculeRef(ORMModel):
    id: uuid.UUID
    preferred_name: str
    inn: str | None = None
    unii: str | None = None
    molecule_class: str
    is_peptide: bool


class CompanyRef(ORMModel):
    id: uuid.UUID
    canonical_name: str
    country_code: str | None = None
    is_focus_company: bool = False


class OpportunitySummary(ORMModel):
    id: uuid.UUID
    opportunity_type: str
    molecule: MoleculeRef
    counterparty: CompanyRef | None = None
    as_of_date: dt.date
    # Deliberately two numbers, never blended.
    opportunity_score: float
    evidence_confidence: str
    evidence_confidence_score: float
    priority_band: str
    headline: str
    engine_version: str
    signal_count: int
    counter_thesis_count: int
    top_signals: list[SignalDetail] = Field(default_factory=list)


class OpportunityDetail(OpportunitySummary):
    signals: list[SignalDetail] = Field(default_factory=list)
    counter_theses: list[CounterThesisDetail] = Field(default_factory=list)
    features: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, bool] = Field(default_factory=dict)
    # Signals that could not be evaluated, with the reason. The UI shows these so
    # a reader can tell a genuine negative from a gap in our data.
    unevaluated_signals: list[dict[str, Any]] = Field(default_factory=list)


class FeedBandCounts(BaseModel):
    high: int = 0
    watch: int = 0
    low_confidence: int = 0
    insufficient_evidence: int = 0


class FeedResponse(BaseModel):
    availability: DataAvailability
    as_of_date: dt.date | None = None
    last_intelligence_refresh: dt.datetime | None = None
    engine_version: str | None = None
    total: int = 0
    bands: FeedBandCounts = Field(default_factory=FeedBandCounts)
    sell: list[OpportunitySummary] = Field(default_factory=list)
    build: list[OpportunitySummary] = Field(default_factory=list)
    # Populated when availability is not AVAILABLE, explaining exactly why.
    message: str | None = None
    source_status: list[dict[str, Any]] = Field(default_factory=list)


class CapabilityDetail(ORMModel):
    capability_type: str
    molecule: MoleculeRef | None = None
    value_text: str | None = None
    # FACT / INFERENCE / UNKNOWN, kept separate as the brief requires.
    assertion_level: str
    inference_basis: str | None = None
    confidence: float
    evidence_id: uuid.UUID | None = None


class TimelineEntry(ORMModel):
    id: uuid.UUID
    event_type: str
    event_date: dt.date | None
    published_at: dt.datetime | None
    summary: str
    company: CompanyRef | None = None
    payload: dict[str, Any] | None = None
    evidence_id: uuid.UUID | None = None


class ScopedCount(BaseModel):
    """A count that carries the statement qualifying it.

    The product must never render "2 suppliers" without "identified in current
    public-source coverage", so the two travel together in the payload rather
    than relying on every client to remember.
    """

    value: int | None
    scope_note: str
    availability: DataAvailability = DataAvailability.AVAILABLE


class MoleculeDetail(ORMModel):
    molecule: MoleculeRef
    therapeutic_area: str | None = None
    observed_api_suppliers: ScopedCount
    focus_company_supplies_api: bool | None = None
    distinct_developers: ScopedCount
    active_trials: ScopedCount
    next_loe_event_months: int | None = None
    next_loe_event_date: dt.date | None = None
    shortage_events_36m: ScopedCount
    markets: list[str] = Field(default_factory=list)
    api_suppliers: list[CompanyRef] = Field(default_factory=list)
    developers: list[CompanyRef] = Field(default_factory=list)
    focus_capabilities: list[CapabilityDetail] = Field(default_factory=list)
    opportunities: list[OpportunitySummary] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    features: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, bool] = Field(default_factory=dict)


class CompanyOverlap(BaseModel):
    molecule: MoleculeRef
    focus_company_supplies_api: bool
    company_holds_own_api_dmf: bool | None
    company_activity_summary: str
    strength: str  # STRONG | MODERATE | INSUFFICIENT_EVIDENCE


class CompanyDetail(ORMModel):
    company: CompanyRef
    countries: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    molecules: list[MoleculeRef] = Field(default_factory=list)
    trials: ScopedCount
    regulatory_events: ScopedCount
    observed_api_filings: ScopedCount
    capabilities: list[CapabilityDetail] = Field(default_factory=list)
    overlaps: list[CompanyOverlap] = Field(default_factory=list)
    overlap_counts: dict[str, int] = Field(default_factory=dict)
    opportunities: list[OpportunitySummary] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)


class WhiteSpaceRow(BaseModel):
    molecule: MoleculeRef
    therapeutic_area: str | None = None
    developer_count: int | None = None
    developer_growth_12m: float | None = None
    observed_api_supplier_count: int | None = None
    focus_api_status: str  # HOLDS_ACTIVE_DMF | NO_DMF_IDENTIFIED | UNKNOWN
    focus_capability_adjacency: float | None = None
    regulatory_events_12m: int | None = None
    months_to_next_loe_event: int | None = None
    shortage_events_36m: int | None = None
    opportunity_type: str | None = None
    opportunity_score: float | None = None
    evidence_confidence: str | None = None
    last_significant_event: dt.date | None = None


class WhiteSpaceResponse(BaseModel):
    availability: DataAvailability
    as_of_date: dt.date | None = None
    total: int = 0
    rows: list[WhiteSpaceRow] = Field(default_factory=list)
    message: str | None = None
    scope_note: str = (
        "All counts reflect Molecule Radar's current public-source coverage and are "
        "not complete global censuses."
    )


class InvestigationSection(BaseModel):
    key: str
    title: str
    body: str
    evidence_ids: list[str] = Field(default_factory=list)
    availability: DataAvailability = DataAvailability.AVAILABLE


class InvestigationResponse(BaseModel):
    id: uuid.UUID | None = None
    opportunity_id: uuid.UUID
    as_of_date: dt.date
    engine_version: str
    synthesis_method: str
    sections: list[InvestigationSection]
    created_at: dt.datetime | None = None


class DataHealthCheck(ORMModel):
    check_key: str
    severity: str
    passed: bool
    observed_count: int
    threshold: int | None = None
    detail: str | None = None
    sample_ids: list[str] | None = None
    created_at: dt.datetime | None = None


class IngestionRunSummary(ORMModel):
    source_key: str
    job_name: str
    status: str
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    duration_seconds: float | None = None
    records_processed: int
    records_inserted: int
    records_updated: int
    records_quarantined: int
    events_emitted: int
    error_count: int
    error_text: str | None = None


class SourceStatusRow(ORMModel):
    key: str
    name: str
    publisher: str
    status: str
    verification_level: str
    access_mode: str
    licence: str
    expected_update_cadence: str | None = None
    last_live_verified_at: dt.datetime | None = None
    last_retrieved_at: dt.datetime | None = None
    document_count: int = 0
    is_enabled: bool = False
    reachable: bool | None = None


class DataHealthResponse(BaseModel):
    generated_at: dt.datetime
    blocking_failures: int
    checks: list[DataHealthCheck]
    sources: list[SourceStatusRow]
    recent_runs: list[IngestionRunSummary]
    pending_reviews: int
    quarantined_records: int


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    email: str


class SystemStatusResponse(BaseModel):
    """What the system actually holds. The honest front door."""

    environment: str
    engine_version: str
    focus_company: str
    auth_enabled: bool
    has_any_data: bool
    molecule_count: int
    company_count: int
    event_count: int
    opportunity_count: int
    last_intelligence_refresh: dt.datetime | None = None
    sources_live_verified: int
    sources_total: int
    message: str | None = None
