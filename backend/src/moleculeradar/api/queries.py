"""Shared read-side query helpers.

Kept separate from the routers so the same assembly logic serves the feed, the
molecule page and the investigation builder, and so there is one place where the
"nothing is a bare number" convention is applied.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.models import (
    Company,
    CounterThesis,
    Evidence,
    LlmExtraction,
    Molecule,
    Opportunity,
    OpportunitySignal,
    Source,
    SourceDocument,
    TherapeuticArea,
)
from .schemas import (
    CompanyRef,
    CounterThesisDetail,
    DataAvailability,
    EvidenceDetail,
    MoleculeRef,
    OpportunitySummary,
    ScopedCount,
    SignalDetail,
    SourceDocumentRef,
)

# The scope statement attached to every supplier count. Centralised so it cannot
# drift between screens, and so a reviewer can find every place it applies.
SUPPLIER_SCOPE_NOTE = (
    "Active Type II drug master file holders identified in Molecule Radar's current "
    "public-source coverage. Reflects US filings only and is not a complete global "
    "supplier census."
)
DEVELOPER_SCOPE_NOTE = (
    "Distinct companies with a regulatory filing or registered trial identified in "
    "current public-source coverage."
)
TRIAL_SCOPE_NOTE = (
    "Registered trials identified on ClinicalTrials.gov within current coverage; "
    "non-registered and non-US-registered development may not appear."
)
SHORTAGE_SCOPE_NOTE = (
    "FDA drug shortage records within current coverage. US scope; absence of a "
    "record does not imply adequate supply."
)


def molecule_ref(molecule: Molecule | None) -> MoleculeRef | None:
    if molecule is None:
        return None
    return MoleculeRef(
        id=molecule.id, preferred_name=molecule.preferred_name, inn=molecule.inn,
        unii=molecule.unii, molecule_class=str(molecule.molecule_class),
        is_peptide=bool(molecule.is_peptide),
    )


def company_ref(company: Company | None) -> CompanyRef | None:
    if company is None:
        return None
    return CompanyRef(
        id=company.id, canonical_name=company.canonical_name,
        is_focus_company=bool(company.is_focus_company),
    )


def scoped(value: int | None, note: str, *, has_source: bool) -> ScopedCount:
    """Wrap a count with its scope statement and the right availability state.

    The distinction this enforces: a zero when we have the source means "none
    found"; a zero when we do not have the source means "we have not looked", and
    presenting those identically would be misleading.
    """
    if not has_source:
        return ScopedCount(
            value=None,
            scope_note=note + " This source has not been ingested, so no count is available.",
            availability=DataAvailability.DATA_UNAVAILABLE,
        )
    if value is None:
        return ScopedCount(value=None, scope_note=note,
                           availability=DataAvailability.DATA_UNAVAILABLE)
    return ScopedCount(value=value, scope_note=note,
                       availability=DataAvailability.AVAILABLE)


def signal_details(db: Session, opportunity_id: uuid.UUID) -> list[SignalDetail]:
    rows = db.scalars(
        select(OpportunitySignal)
        .where(OpportunitySignal.opportunity_id == opportunity_id)
        .order_by(OpportunitySignal.points.desc())
    ).all()
    return [
        SignalDetail(
            signal_key=r.signal_key, direction=str(r.direction), points=r.points,
            max_points=r.max_points, explanation=r.explanation,
            feature_values=r.feature_values, evidence_ids=r.evidence_ids,
        )
        for r in rows
    ]


def counter_thesis_details(db: Session, opportunity_id: uuid.UUID) -> list[CounterThesisDetail]:
    severity_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    rows = db.scalars(
        select(CounterThesis).where(CounterThesis.opportunity_id == opportunity_id)
    ).all()
    rows = sorted(rows, key=lambda r: severity_rank.get(r.severity, 3))
    return [
        CounterThesisDetail(
            concern_key=r.concern_key, severity=r.severity, explanation=r.explanation,
            feature_values=r.feature_values, evidence_ids=r.evidence_ids,
        )
        for r in rows
    ]


def opportunity_summary(
    db: Session, opp: Opportunity, *, include_top_signals: int = 4
) -> OpportunitySummary:
    molecule = db.get(Molecule, opp.molecule_id)
    counterparty = (
        db.get(Company, opp.counterparty_company_id)
        if opp.counterparty_company_id else None
    )
    signal_count = db.scalar(
        select(func.count()).select_from(OpportunitySignal)
        .where(OpportunitySignal.opportunity_id == opp.id)
    ) or 0
    counter_count = db.scalar(
        select(func.count()).select_from(CounterThesis)
        .where(CounterThesis.opportunity_id == opp.id)
    ) or 0
    top: list[SignalDetail] = []
    if include_top_signals:
        top = signal_details(db, opp.id)[:include_top_signals]

    return OpportunitySummary(
        id=opp.id, opportunity_type=str(opp.opportunity_type),
        molecule=molecule_ref(molecule), counterparty=company_ref(counterparty),
        as_of_date=opp.as_of_date, opportunity_score=opp.opportunity_score,
        evidence_confidence=str(opp.evidence_confidence),
        evidence_confidence_score=opp.evidence_confidence_score,
        priority_band=str(opp.priority_band), headline=opp.headline,
        engine_version=opp.engine_version, signal_count=signal_count,
        counter_thesis_count=counter_count, top_signals=top,
    )


def evidence_detail(db: Session, evidence_id: uuid.UUID) -> EvidenceDetail | None:
    """Assemble the full evidence chain: claim → evidence → document → source.

    This is what the evidence drawer renders, and it is the feature that makes the
    rest of the product trustworthy, so it returns the publisher's own licence and
    disclaimer alongside our extract.
    """
    evidence = db.get(Evidence, evidence_id)
    if evidence is None:
        return None

    doc = db.get(SourceDocument, evidence.source_document_id)
    doc_ref = None
    if doc is not None:
        source = db.get(Source, doc.source_id)
        doc_ref = SourceDocumentRef(
            id=doc.id,
            source_key=source.key if source else None,
            source_name=source.name if source else None,
            publisher=source.publisher if source else None,
            licence=source.licence if source else None,
            locator=doc.locator, sha256=doc.sha256, retrieved_at=doc.retrieved_at,
            published_at=doc.published_at,
            source_last_updated=doc.source_last_updated,
            disclaimer_text=doc.disclaimer_text,
            attribution_text=source.attribution_text if source else None,
        )

    llm_audit = None
    if evidence.llm_extraction_id:
        extraction = db.get(LlmExtraction, evidence.llm_extraction_id)
        if extraction is not None:
            # The full audit trail the brief required: model, prompt version,
            # raw output, confidence, timestamp.
            llm_audit = {
                "model": extraction.model,
                "prompt_version": extraction.prompt_version,
                "prompt_hash": extraction.prompt_hash,
                "task": extraction.task,
                "raw_output": extraction.raw_output,
                "parsed_output": extraction.parsed_output,
                "confidence": extraction.confidence,
                "created_at": extraction.created_at.isoformat()
                if extraction.created_at else None,
                "token_usage": extraction.token_usage,
                "succeeded": extraction.succeeded,
            }

    return EvidenceDetail(
        id=evidence.id, claim_text=evidence.claim_text,
        extract_text=evidence.extract_text,
        structured_record=evidence.structured_record,
        source_url=evidence.source_url,
        extraction_method=str(evidence.extraction_method),
        confidence=evidence.confidence, event_date=evidence.event_date,
        published_at=evidence.published_at, retrieved_at=evidence.retrieved_at,
        observed_at=evidence.observed_at, source_document=doc_ref,
        llm_audit=llm_audit,
    )


def therapeutic_area_name(db: Session, ta_id: uuid.UUID | None) -> str | None:
    if ta_id is None:
        return None
    ta = db.get(TherapeuticArea, ta_id)
    return ta.name if ta else None


def latest_as_of_date(db: Session) -> dt.date | None:
    return db.scalar(select(func.max(Opportunity.as_of_date)))


def last_refresh(db: Session) -> dt.datetime | None:
    """The real timestamp of the most recent source retrieval.

    Never a formatted 'now': if nothing has been retrieved this is None and the UI
    says so rather than implying freshness.
    """
    return db.scalar(select(func.max(SourceDocument.retrieved_at)))


def source_status_rows(db: Session) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for source in db.scalars(select(Source).order_by(Source.key)).all():
        latest = db.scalar(
            select(func.max(SourceDocument.retrieved_at))
            .where(SourceDocument.source_id == source.id)
        )
        count = db.scalar(
            select(func.count()).select_from(SourceDocument)
            .where(SourceDocument.source_id == source.id)
        ) or 0
        out.append({
            "key": source.key, "name": source.name, "publisher": source.publisher,
            "status": str(source.status),
            "verification_level": str(source.verification_level),
            "licence": source.licence,
            "expected_update_cadence": source.expected_update_cadence,
            "last_retrieved_at": latest.isoformat() if latest else None,
            "document_count": count, "is_enabled": source.is_enabled,
        })
    return out
