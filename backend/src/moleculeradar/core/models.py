"""Canonical SQLAlchemy models.

Design notes that matter when reading this file:

* Surrogate UUID primary keys everywhere. No natural key is ever a PK — pharma
  names are not stable enough to be identities.
* `TemporalMixin` is applied to every table that asserts something about the
  world, giving the uniform time contract described in docs/DATA_MODEL.md.
* Rows that assert a fact carry `evidence_id`. The data-quality suite flags any
  that do not.
* `dedupe_key` columns with unique constraints are what make ingestion idempotent:
  re-running a load updates rather than duplicating.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, ClassVar

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .enums import (
    AccessMode,
    Agency,
    AliasKind,
    ApplicationType,
    AssertionLevel,
    CapabilityType,
    CompanyRole,
    EntityType,
    EventType,
    EvidenceConfidence,
    ExtractionMethod,
    IdentifierScheme,
    MoleculeClass,
    OpportunityType,
    PriorityBand,
    ResolutionMethod,
    ReviewStatus,
    RunStatus,
    SignalDirection,
    SourceStatus,
    VerificationLevel,
)


class Base(DeclarativeBase):
    # SQLAlchemy reads this mapping at class-construction time and never mutates
    # it; the ClassVar annotation tells linters the same.
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB,
        list[str]: ARRAY(Text),
    }


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TemporalMixin:
    """The uniform time contract. See core/temporal.py for how these are used."""

    # When it happened in the world.
    event_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # When the publisher made it public.
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When we fetched the artefact it came from.
    retrieved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When Molecule Radar first recorded it.
    observed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Slowly-changing state validity. valid_to IS NULL => currently believed true.
    valid_from: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[dt.date | None] = mapped_column(Date, nullable=True)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


class Source(Base, TimestampMixin):
    """A publisher dataset, e.g. 'openFDA Drugs@FDA'."""

    __tablename__ = "source"

    id: Mapped[uuid.UUID] = _uuid_pk()
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    publisher: Mapped[str] = mapped_column(String(255), nullable=False)
    homepage_url: Mapped[str | None] = mapped_column(Text)
    licence: Mapped[str] = mapped_column(String(255), nullable=False)
    licence_url: Mapped[str | None] = mapped_column(Text)
    access_mode: Mapped[AccessMode] = mapped_column(String(16), nullable=False)
    status: Mapped[SourceStatus] = mapped_column(String(16), nullable=False)
    verification_level: Mapped[VerificationLevel] = mapped_column(String(16), nullable=False)
    # Honest record of whether we have ever actually spoken to this source.
    last_live_verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    terms_reviewed_at: Mapped[dt.date | None] = mapped_column(Date)
    terms_notes: Mapped[str | None] = mapped_column(Text)
    expected_update_cadence: Mapped[str | None] = mapped_column(String(64))
    attribution_text: Mapped[str | None] = mapped_column(Text)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    documents: Mapped[list[SourceDocument]] = relationship(back_populates="source")


class SourceDocument(Base, TimestampMixin):
    """One retrieval of one artefact. Immutable once written."""

    __tablename__ = "source_document"
    __table_args__ = (
        UniqueConstraint("source_id", "sha256", name="uq_source_document_source_sha"),
        Index("ix_source_document_retrieved", "source_id", "retrieved_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source.id"), nullable=False)
    locator: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    retrieved_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # The publisher's own statement of currency, e.g. openFDA meta.last_updated.
    source_last_updated: Mapped[dt.date | None] = mapped_column(Date)
    byte_size: Mapped[int | None] = mapped_column(Integer)
    media_type: Mapped[str | None] = mapped_column(String(128))
    http_status: Mapped[int | None] = mapped_column(Integer)
    # Pointer into object storage for the verbatim payload.
    raw_payload_ref: Mapped[str | None] = mapped_column(Text)
    # The publisher's disclaimer/licence text as served, carried to the UI.
    disclaimer_text: Mapped[str | None] = mapped_column(Text)
    ingestion_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ingestion_run.id"))

    source: Mapped[Source] = relationship(back_populates="documents")


class LlmExtraction(Base, TimestampMixin):
    """Audit record for model-assisted extraction. Required for any LLM-derived row."""

    __tablename__ = "llm_extraction"

    id: Mapped[uuid.UUID] = _uuid_pk()
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    task: Mapped[str] = mapped_column(String(64), nullable=False)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_document.id")
    )
    input_ref: Mapped[str | None] = mapped_column(Text)
    input_text: Mapped[str | None] = mapped_column(Text)
    raw_output: Mapped[str | None] = mapped_column(Text)
    parsed_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error_text: Mapped[str | None] = mapped_column(Text)


class Evidence(Base, TimestampMixin, TemporalMixin):
    """A single traceable claim. The spine of the product's credibility."""

    __tablename__ = "evidence"
    __table_args__ = (
        Index("ix_evidence_source_document", "source_document_id"),
        CheckConstraint(
            "(extraction_method <> 'llm_extraction') OR (llm_extraction_id IS NOT NULL)",
            name="ck_evidence_llm_requires_audit",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_document.id"), nullable=False
    )
    # Verbatim supporting extract, where the source is textual.
    extract_text: Mapped[str | None] = mapped_column(Text)
    # The parsed structured record, where the source is structured data.
    structured_record: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Deep link back into the publisher's own UI, for a human to check.
    source_url: Mapped[str | None] = mapped_column(Text)
    extraction_method: Mapped[ExtractionMethod] = mapped_column(String(32), nullable=False)
    llm_extraction_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("llm_extraction.id"))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    source_document: Mapped[SourceDocument] = relationship()


# ---------------------------------------------------------------------------
# Identity and resolution
# ---------------------------------------------------------------------------


class ExternalIdentifier(Base, TimestampMixin):
    """The deterministic layer of entity resolution."""

    __tablename__ = "external_identifier"
    __table_args__ = (
        UniqueConstraint("scheme", "value", "entity_type", name="uq_external_identifier"),
        Index("ix_external_identifier_entity", "entity_type", "entity_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    scheme: Mapped[IdentifierScheme] = mapped_column(String(32), nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[EntityType] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    first_seen_source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_document.id")
    )


class EntityAlias(Base, TimestampMixin):
    """Every name the world uses for an entity. Never deleted."""

    __tablename__ = "entity_alias"
    __table_args__ = (
        # One normalised alias resolves to at most ONE entity of a given type.
        # Without this, two companies could both claim the alias "drl" and
        # resolution would non-deterministically pick whichever row the planner
        # returned first. A genuinely ambiguous alias must go to human review
        # rather than be stored twice.
        UniqueConstraint(
            "entity_type", "normalized_alias", name="uq_entity_alias_type_norm"
        ),
        Index("ix_entity_alias_lookup", "entity_type", "normalized_alias"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    entity_type: Mapped[EntityType] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_alias: Mapped[str] = mapped_column(Text, nullable=False)
    alias_kind: Mapped[AliasKind] = mapped_column(String(32), nullable=False)
    resolution_method: Mapped[ResolutionMethod] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    first_seen_source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_document.id")
    )


class EntityMergeLog(Base, TimestampMixin):
    """Why two entities were considered the same. Every merge must be explainable."""

    __tablename__ = "entity_merge_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    entity_type: Mapped[EntityType] = mapped_column(String(32), nullable=False)
    surviving_entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    merged_entity_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    input_literal: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_method: Mapped[ResolutionMethod] = mapped_column(String(32), nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    features: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_document.id")
    )


class ReviewQueueItem(Base, TimestampMixin):
    """Ambiguous resolution decisions awaiting a human. Nothing is silently merged."""

    __tablename__ = "review_queue_item"
    __table_args__ = (Index("ix_review_queue_status", "status", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    item_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[EntityType] = mapped_column(String(32), nullable=False)
    input_literal: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    proposed_action: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[ReviewStatus] = mapped_column(
        String(16), nullable=False, default=ReviewStatus.PENDING
    )
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_document.id")
    )


# ---------------------------------------------------------------------------
# Reference vocabularies
# ---------------------------------------------------------------------------


class Country(Base, TimestampMixin):
    __tablename__ = "country"

    id: Mapped[uuid.UUID] = _uuid_pk()
    iso_alpha2: Mapped[str] = mapped_column(String(2), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    region: Mapped[str | None] = mapped_column(String(64))


class TherapeuticArea(Base, TimestampMixin):
    __tablename__ = "therapeutic_area"

    id: Mapped[uuid.UUID] = _uuid_pk()
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class Indication(Base, TimestampMixin):
    __tablename__ = "indication"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    therapeutic_area_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("therapeutic_area.id")
    )


# ---------------------------------------------------------------------------
# Pharma entities
# ---------------------------------------------------------------------------


class Molecule(Base, TimestampMixin, TemporalMixin):
    __tablename__ = "molecule"
    __table_args__ = (Index("ix_molecule_normalized", "normalized_name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    preferred_name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    inn: Mapped[str | None] = mapped_column(String(255))
    unii: Mapped[str | None] = mapped_column(String(32), index=True)
    cas: Mapped[str | None] = mapped_column(String(64))
    molecule_class: Mapped[MoleculeClass] = mapped_column(
        String(32), nullable=False, default=MoleculeClass.UNKNOWN
    )
    is_peptide: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Each flag carries its own evidence reference, e.g.
    # {"hpapi": {"value": true, "evidence_id": "...", "assertion_level": "INFERENCE"}}
    complexity_flags: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    therapeutic_area_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("therapeutic_area.id")
    )
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evidence.id"))


class MoleculeForm(Base, TimestampMixin):
    """A specific salt/ester/hydrate. DMFs and products are filed on forms."""

    __tablename__ = "molecule_form"
    __table_args__ = (
        UniqueConstraint("molecule_id", "normalized_form_name", name="uq_molecule_form"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    molecule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("molecule.id"), nullable=False)
    form_name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_form_name: Mapped[str] = mapped_column(String(512), nullable=False)
    salt_or_ester: Mapped[str | None] = mapped_column(String(128))


class Company(Base, TimestampMixin, TemporalMixin):
    __tablename__ = "company"
    __table_args__ = (Index("ix_company_normalized", "normalized_name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    canonical_name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    country_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("country.id"))
    company_type: Mapped[str | None] = mapped_column(String(64))
    parent_company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("company.id"))
    # True for the configured focus company group (DRL). Configuration, not schema.
    is_focus_company: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evidence.id"))


class CompanyRoleAssertion(Base, TimestampMixin, TemporalMixin):
    """A role a company plays, on a molecule or product, with evidence."""

    __tablename__ = "company_role"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_company_role_dedupe"),
        Index("ix_company_role_company", "company_id", "role"),
        Index("ix_company_role_molecule", "molecule_id", "role"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("company.id"), nullable=False)
    role: Mapped[CompanyRole] = mapped_column(String(32), nullable=False)
    molecule_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("molecule.id"))
    drug_product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("drug_product.id"))
    evidence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence.id"), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)


class RegulatoryApplication(Base, TimestampMixin, TemporalMixin):
    __tablename__ = "regulatory_application"
    __table_args__ = (
        UniqueConstraint("agency", "application_number", name="uq_regulatory_application"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    agency: Mapped[Agency] = mapped_column(String(16), nullable=False)
    application_type: Mapped[ApplicationType] = mapped_column(String(16), nullable=False)
    application_number: Mapped[str] = mapped_column(String(64), nullable=False)
    applicant_company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("company.id"))
    applicant_literal: Mapped[str | None] = mapped_column(Text)
    first_approval_date: Mapped[dt.date | None] = mapped_column(Date)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evidence.id"))


class RegulatorySubmission(Base, TimestampMixin, TemporalMixin):
    """A dated submission within an application. Source of filing/approval events."""

    __tablename__ = "regulatory_submission"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_regulatory_submission_dedupe"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    regulatory_application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("regulatory_application.id"), nullable=False
    )
    submission_type: Mapped[str | None] = mapped_column(String(64))
    submission_number: Mapped[str | None] = mapped_column(String(32))
    submission_status: Mapped[str | None] = mapped_column(String(64))
    submission_class_code: Mapped[str | None] = mapped_column(String(64))
    review_priority: Mapped[str | None] = mapped_column(String(64))
    evidence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence.id"), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)


class DrugProduct(Base, TimestampMixin, TemporalMixin):
    __tablename__ = "drug_product"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_drug_product_dedupe"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    regulatory_application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("regulatory_application.id")
    )
    molecule_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("molecule.id"))
    molecule_form_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("molecule_form.id"))
    brand_name: Mapped[str | None] = mapped_column(String(512))
    product_number: Mapped[str | None] = mapped_column(String(32))
    dosage_form: Mapped[str | None] = mapped_column(String(255))
    route: Mapped[str | None] = mapped_column(String(255))
    strength_text: Mapped[str | None] = mapped_column(Text)
    te_code: Mapped[str | None] = mapped_column(String(16))
    reference_listed_drug: Mapped[bool | None] = mapped_column(Boolean)
    approval_date: Mapped[dt.date | None] = mapped_column(Date)
    marketing_status: Mapped[str | None] = mapped_column(String(64))
    evidence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence.id"), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)


class Patent(Base, TimestampMixin, TemporalMixin):
    __tablename__ = "patent"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_patent_dedupe"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    patent_number: Mapped[str] = mapped_column(String(64), nullable=False)
    jurisdiction: Mapped[str] = mapped_column(String(8), nullable=False, default="US")
    regulatory_application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("regulatory_application.id")
    )
    drug_product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("drug_product.id"))
    molecule_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("molecule.id"))
    expiry_date: Mapped[dt.date | None] = mapped_column(Date)
    drug_substance_flag: Mapped[bool | None] = mapped_column(Boolean)
    drug_product_flag: Mapped[bool | None] = mapped_column(Boolean)
    use_code: Mapped[str | None] = mapped_column(String(32))
    delist_requested: Mapped[bool | None] = mapped_column(Boolean)
    evidence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence.id"), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)


class Exclusivity(Base, TimestampMixin, TemporalMixin):
    __tablename__ = "exclusivity"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_exclusivity_dedupe"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    exclusivity_code: Mapped[str] = mapped_column(String(32), nullable=False)
    regulatory_application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("regulatory_application.id")
    )
    drug_product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("drug_product.id"))
    molecule_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("molecule.id"))
    expiry_date: Mapped[dt.date | None] = mapped_column(Date)
    evidence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence.id"), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)


class DrugMasterFile(Base, TimestampMixin, TemporalMixin):
    """FDA DMF. The backbone of API-supplier and focus-company capability evidence."""

    __tablename__ = "drug_master_file"
    __table_args__ = (
        UniqueConstraint("dmf_number", name="uq_dmf_number"),
        Index("ix_dmf_molecule_status", "molecule_id", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    dmf_number: Mapped[str] = mapped_column(String(32), nullable=False)
    dmf_type: Mapped[str | None] = mapped_column(String(16))
    holder_company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("company.id"))
    holder_literal: Mapped[str] = mapped_column(Text, nullable=False)
    subject_text: Mapped[str] = mapped_column(Text, nullable=False)
    molecule_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("molecule.id"))
    status: Mapped[str | None] = mapped_column(String(8))
    submission_date: Mapped[dt.date | None] = mapped_column(Date)
    # GDUFA completeness-assessment flag: materially stronger evidence than mere filing.
    available_for_reference: Mapped[bool | None] = mapped_column(Boolean)
    evidence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence.id"), nullable=False)


class ClinicalTrial(Base, TimestampMixin, TemporalMixin):
    __tablename__ = "clinical_trial"
    __table_args__ = (
        UniqueConstraint("nct_id", name="uq_clinical_trial_nct"),
        Index("ix_clinical_trial_molecule", "molecule_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    nct_id: Mapped[str] = mapped_column(String(32), nullable=False)
    brief_title: Mapped[str | None] = mapped_column(Text)
    sponsor_company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("company.id"))
    sponsor_literal: Mapped[str | None] = mapped_column(Text)
    phase: Mapped[str | None] = mapped_column(String(32))
    overall_status: Mapped[str | None] = mapped_column(String(64))
    study_type: Mapped[str | None] = mapped_column(String(64))
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    primary_completion_date: Mapped[dt.date | None] = mapped_column(Date)
    last_update_posted_date: Mapped[dt.date | None] = mapped_column(Date)
    enrollment: Mapped[int | None] = mapped_column(Integer)
    molecule_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("molecule.id"))
    indication_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("indication.id"))
    location_country_codes: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    evidence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence.id"), nullable=False)


class CompanyCapability(Base, TimestampMixin, TemporalMixin):
    """A capability a company appears to have. FACT/INFERENCE/UNKNOWN kept separate."""

    __tablename__ = "company_capability"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_company_capability_dedupe"),
        Index("ix_company_capability_lookup", "company_id", "capability_type", "molecule_id"),
        CheckConstraint(
            "(assertion_level <> 'INFERENCE') OR (inference_basis IS NOT NULL)",
            name="ck_capability_inference_requires_basis",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("company.id"), nullable=False)
    capability_type: Mapped[CapabilityType] = mapped_column(String(32), nullable=False)
    molecule_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("molecule.id"))
    value_text: Mapped[str | None] = mapped_column(Text)
    assertion_level: Mapped[AssertionLevel] = mapped_column(String(16), nullable=False)
    inference_basis: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    evidence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence.id"), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class Event(Base, TimestampMixin, TemporalMixin):
    """The temporal spine. One stream for every kind of change."""

    __tablename__ = "event"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_event_dedupe"),
        Index("ix_event_molecule_date", "molecule_id", "event_date"),
        Index("ix_event_type_date", "event_type", "event_date"),
        Index("ix_event_company_date", "company_id", "event_date"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    event_type: Mapped[EventType] = mapped_column(String(48), nullable=False)
    subject_entity_type: Mapped[EntityType] = mapped_column(String(32), nullable=False)
    subject_entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    molecule_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("molecule.id"))
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("company.id"))
    country_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("country.id"))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    evidence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence.id"), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)


# ---------------------------------------------------------------------------
# Opportunity engine
# ---------------------------------------------------------------------------


class FeatureSnapshot(Base, TimestampMixin):
    """Deterministic feature values for one molecule at one as-of date."""

    __tablename__ = "feature_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "molecule_id", "as_of_date", "engine_version", name="uq_feature_snapshot"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    molecule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("molecule.id"), nullable=False)
    as_of_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    visibility_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    # All deterministic feature values, plus the evidence ids backing each.
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    feature_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    computed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Opportunity(Base, TimestampMixin):
    __tablename__ = "opportunity"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_opportunity_dedupe"),
        Index("ix_opportunity_feed", "as_of_date", "priority_band", "opportunity_score"),
        CheckConstraint(
            "(opportunity_type <> 'SELL') OR (counterparty_company_id IS NOT NULL)",
            name="ck_sell_requires_counterparty",
        ),
        CheckConstraint(
            "opportunity_score >= 0 AND opportunity_score <= 100",
            name="ck_opportunity_score_range",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    opportunity_type: Mapped[OpportunityType] = mapped_column(String(8), nullable=False)
    molecule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("molecule.id"), nullable=False)
    # SELL only: the company exhibiting signals consistent with potential demand.
    counterparty_company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("company.id"))
    as_of_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    # Deliberately two separate numbers. Never blended.
    opportunity_score: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_confidence: Mapped[EvidenceConfidence] = mapped_column(String(16), nullable=False)
    evidence_confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    priority_band: Mapped[PriorityBand] = mapped_column(String(24), nullable=False)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    feature_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("feature_snapshot.id"), nullable=False
    )
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)

    signals: Mapped[list[OpportunitySignal]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan"
    )
    counter_theses: Mapped[list[CounterThesis]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan"
    )


class OpportunitySignal(Base, TimestampMixin):
    """One decomposition row. The score is the sum of these — reproducible by hand."""

    __tablename__ = "opportunity_signal"

    id: Mapped[uuid.UUID] = _uuid_pk()
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opportunity.id", ondelete="CASCADE"), nullable=False
    )
    signal_key: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[SignalDirection] = mapped_column(String(16), nullable=False)
    points: Mapped[float] = mapped_column(Float, nullable=False)
    max_points: Mapped[float] = mapped_column(Float, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    feature_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    evidence_ids: Mapped[list[str] | None] = mapped_column(ARRAY(Text))

    opportunity: Mapped[Opportunity] = relationship(back_populates="signals")


class CounterThesis(Base, TimestampMixin):
    """Skeptic-engine output. Every HIGH opportunity gets one."""

    __tablename__ = "counter_thesis"

    id: Mapped[uuid.UUID] = _uuid_pk()
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opportunity.id", ondelete="CASCADE"), nullable=False
    )
    concern_key: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    feature_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    evidence_ids: Mapped[list[str] | None] = mapped_column(ARRAY(Text))

    opportunity: Mapped[Opportunity] = relationship(back_populates="counter_theses")


class Investigation(Base, TimestampMixin):
    """A reproducible investigation run."""

    __tablename__ = "investigation"

    id: Mapped[uuid.UUID] = _uuid_pk()
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opportunity.id"), nullable=False
    )
    as_of_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    sections: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    synthesis_method: Mapped[str] = mapped_column(String(32), nullable=False)
    llm_extraction_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("llm_extraction.id"))
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="system")


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


class IngestionRun(Base, TimestampMixin):
    """Per-job observability. A failed run must never silently corrupt data."""

    __tablename__ = "ingestion_run"
    __table_args__ = (Index("ix_ingestion_run_source", "source_key", "started_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    job_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[RunStatus] = mapped_column(String(16), nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    records_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_inserted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_unchanged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_quarantined: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    events_emitted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_text: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    as_of_date: Mapped[dt.date | None] = mapped_column(Date)


class QuarantinedRecord(Base, TimestampMixin):
    """A record we refused to promote. Visible on the Data Health screen."""

    __tablename__ = "quarantined_record"

    id: Mapped[uuid.UUID] = _uuid_pk()
    ingestion_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ingestion_run.id"))
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_detail: Mapped[str] = mapped_column(Text, nullable=False)
    raw_record: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class DataQualityCheckResult(Base, TimestampMixin):
    __tablename__ = "data_quality_check_result"
    __table_args__ = (Index("ix_dq_check_run", "check_key", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    check_key: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    observed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    threshold: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(Text)
    sample_ids: Mapped[list[str] | None] = mapped_column(ARRAY(Text))


class AuditLogEntry(Base, TimestampMixin):
    """Who did what. Required even while all data is public."""

    __tablename__ = "audit_log_entry"
    __table_args__ = (Index("ix_audit_actor_time", "actor", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip_address: Mapped[str | None] = mapped_column(String(64))


class User(Base, TimestampMixin):
    """RBAC-ready: a role column now, a role table when roles need permissions."""

    __tablename__ = "app_user"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="analyst")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
