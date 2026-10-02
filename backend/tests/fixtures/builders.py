"""Synthetic fixture builders.

EVERYTHING IN THIS MODULE IS SYNTHETIC AND FOR TESTS ONLY.

The company and molecule names here are deliberately obvious placeholders
("Testrazole", "Alpha Generics") rather than real products or firms. That is a
safety property, not stylistic preference: a fixture that looked like real data
could leak into a demo and become exactly the fabricated-evidence problem the
product exists to avoid. Nothing in this module is importable from the
application package.

Each builder returns the IDs a test needs and sets the temporal columns
explicitly, so leakage tests can control precisely what was knowable when.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy.orm import Session

from moleculeradar.core.enums import (
    AccessMode,
    Agency,
    ApplicationType,
    AssertionLevel,
    CapabilityType,
    EntityType,
    EventType,
    ExtractionMethod,
    IdentifierScheme,
    MoleculeClass,
    SourceStatus,
    VerificationLevel,
)
from moleculeradar.core.models import (
    ClinicalTrial,
    Company,
    CompanyCapability,
    DrugMasterFile,
    Event,
    Evidence,
    Exclusivity,
    ExternalIdentifier,
    Molecule,
    Patent,
    RegulatoryApplication,
    Source,
    SourceDocument,
    TherapeuticArea,
)

UTC = dt.UTC


def make_source(session: Session, key: str = "test_source", name: str | None = None) -> Source:
    source = Source(
        key=key,
        name=name or f"Synthetic test source {key}",
        publisher="Molecule Radar test fixtures",
        licence="synthetic-fixture-not-real-data",
        access_mode=AccessMode.API,
        status=SourceStatus.ACTIVE,
        verification_level=VerificationLevel.ASSERTED,
        is_enabled=True,
    )
    session.add(source)
    session.flush()
    return source


def make_document(
    session: Session,
    source: Source,
    *,
    retrieved_at: dt.datetime | None = None,
    published_at: dt.datetime | None = None,
    sha: str | None = None,
) -> SourceDocument:
    doc = SourceDocument(
        source_id=source.id,
        locator=f"synthetic://{source.key}/{uuid.uuid4()}",
        sha256=sha or uuid.uuid4().hex + uuid.uuid4().hex[:0].ljust(0, "0"),
        retrieved_at=retrieved_at or dt.datetime(2026, 1, 1, tzinfo=UTC),
        published_at=published_at,
    )
    # sha256 column is 64 chars; uuid4().hex is 32, so double it.
    doc.sha256 = (sha or (uuid.uuid4().hex + uuid.uuid4().hex))[:64]
    session.add(doc)
    session.flush()
    return doc


def make_evidence(
    session: Session,
    doc: SourceDocument,
    claim: str,
    *,
    event_date: dt.date | None = None,
    published_at: dt.datetime | None = None,
    observed_at: dt.datetime | None = None,
    confidence: float = 1.0,
) -> Evidence:
    ev = Evidence(
        claim_text=claim,
        source_document_id=doc.id,
        structured_record={"synthetic": True},
        extraction_method=ExtractionMethod.DETERMINISTIC_PARSE,
        confidence=confidence,
        event_date=event_date,
        published_at=published_at or doc.published_at,
        retrieved_at=doc.retrieved_at,
        observed_at=observed_at or doc.retrieved_at,
    )
    session.add(ev)
    session.flush()
    return ev


def make_therapeutic_area(session: Session, key: str = "ta_test") -> TherapeuticArea:
    ta = TherapeuticArea(key=key, name=f"Synthetic therapeutic area {key}")
    session.add(ta)
    session.flush()
    return ta


def make_molecule(
    session: Session,
    name: str,
    *,
    unii: str | None = None,
    therapeutic_area_id: uuid.UUID | None = None,
    is_peptide: bool = False,
    molecule_class: MoleculeClass = MoleculeClass.SMALL_MOLECULE,
    complexity_flags: dict | None = None,
) -> Molecule:
    from moleculeradar.resolution.normalize import normalize_molecule_name

    mol = Molecule(
        preferred_name=name,
        normalized_name=normalize_molecule_name(name),
        unii=unii,
        therapeutic_area_id=therapeutic_area_id,
        is_peptide=is_peptide,
        molecule_class=molecule_class,
        complexity_flags=complexity_flags,
        observed_at=dt.datetime(2020, 1, 1, tzinfo=UTC),
    )
    session.add(mol)
    session.flush()
    if unii:
        session.add(ExternalIdentifier(
            scheme=IdentifierScheme.UNII, value=unii,
            entity_type=EntityType.MOLECULE, entity_id=mol.id,
        ))
        session.flush()
    return mol


def make_company(
    session: Session, name: str, *, is_focus: bool = False
) -> Company:
    from moleculeradar.resolution.normalize import normalize_company_name

    company = Company(
        canonical_name=name,
        normalized_name=normalize_company_name(name) or name.lower(),
        is_focus_company=is_focus,
        observed_at=dt.datetime(2020, 1, 1, tzinfo=UTC),
    )
    session.add(company)
    session.flush()
    return company


def make_dmf(
    session: Session,
    doc: SourceDocument,
    *,
    molecule: Molecule,
    holder: Company,
    dmf_number: str,
    status: str = "A",
    dmf_type: str = "2",
    available_for_reference: bool = True,
    submission_date: dt.date = dt.date(2019, 6, 1),
    published_at: dt.datetime | None = None,
) -> DrugMasterFile:
    ev = make_evidence(
        session, doc,
        f"DMF {dmf_number} held by {holder.canonical_name} for {molecule.preferred_name}",
        event_date=submission_date, published_at=published_at,
    )
    dmf = DrugMasterFile(
        dmf_number=dmf_number,
        dmf_type=dmf_type,
        holder_company_id=holder.id,
        holder_literal=holder.canonical_name,
        subject_text=molecule.preferred_name,
        molecule_id=molecule.id,
        status=status,
        submission_date=submission_date,
        available_for_reference=available_for_reference,
        evidence_id=ev.id,
        event_date=submission_date,
        published_at=published_at or doc.published_at,
        retrieved_at=doc.retrieved_at,
        observed_at=doc.retrieved_at,
    )
    session.add(dmf)
    session.flush()
    return dmf


def make_exclusivity(
    session: Session,
    doc: SourceDocument,
    *,
    molecule: Molecule,
    expiry: dt.date,
    code: str = "SYN-EXCL",
    published_at: dt.datetime | None = None,
) -> Exclusivity:
    ev = make_evidence(
        session, doc, f"Exclusivity {code} for {molecule.preferred_name} expiring {expiry}",
        event_date=expiry, published_at=published_at,
    )
    excl = Exclusivity(
        exclusivity_code=code,
        molecule_id=molecule.id,
        expiry_date=expiry,
        evidence_id=ev.id,
        dedupe_key=f"excl:{molecule.id}:{code}:{expiry}",
        published_at=published_at or doc.published_at,
        retrieved_at=doc.retrieved_at,
        observed_at=doc.retrieved_at,
    )
    session.add(excl)
    session.flush()
    return excl


def make_patent(
    session: Session,
    doc: SourceDocument,
    *,
    molecule: Molecule,
    patent_number: str,
    expiry: dt.date,
    drug_substance: bool = False,
    delist_requested: bool = False,
    published_at: dt.datetime | None = None,
) -> Patent:
    ev = make_evidence(
        session, doc, f"Patent {patent_number} listed for {molecule.preferred_name}",
        event_date=expiry, published_at=published_at,
    )
    pat = Patent(
        patent_number=patent_number,
        jurisdiction="US",
        molecule_id=molecule.id,
        expiry_date=expiry,
        drug_substance_flag=drug_substance,
        delist_requested=delist_requested,
        evidence_id=ev.id,
        dedupe_key=f"pat:{molecule.id}:{patent_number}",
        published_at=published_at or doc.published_at,
        retrieved_at=doc.retrieved_at,
        observed_at=doc.retrieved_at,
    )
    session.add(pat)
    session.flush()
    return pat


def make_trial(
    session: Session,
    doc: SourceDocument,
    *,
    molecule: Molecule,
    sponsor: Company,
    nct_id: str,
    start_date: dt.date,
    phase: str = "PHASE3",
    status: str = "RECRUITING",
    countries: list[str] | None = None,
    published_at: dt.datetime | None = None,
) -> ClinicalTrial:
    ev = make_evidence(
        session, doc, f"Trial {nct_id} sponsored by {sponsor.canonical_name}",
        event_date=start_date, published_at=published_at,
    )
    trial = ClinicalTrial(
        nct_id=nct_id,
        brief_title=f"Synthetic study of {molecule.preferred_name}",
        sponsor_company_id=sponsor.id,
        sponsor_literal=sponsor.canonical_name,
        phase=phase,
        overall_status=status,
        start_date=start_date,
        molecule_id=molecule.id,
        location_country_codes=countries or ["US"],
        evidence_id=ev.id,
        event_date=start_date,
        published_at=published_at or doc.published_at,
        retrieved_at=doc.retrieved_at,
        observed_at=doc.retrieved_at,
    )
    session.add(trial)
    session.flush()
    return trial


def make_event(
    session: Session,
    doc: SourceDocument,
    *,
    event_type: EventType,
    molecule: Molecule,
    company: Company | None = None,
    event_date: dt.date,
    subject_entity_type: EntityType = EntityType.MOLECULE,
    subject_entity_id: uuid.UUID | None = None,
    summary: str | None = None,
    published_at: dt.datetime | None = None,
    observed_at: dt.datetime | None = None,
) -> Event:
    ev = make_evidence(
        session, doc, summary or f"{event_type.value} for {molecule.preferred_name}",
        event_date=event_date, published_at=published_at, observed_at=observed_at,
    )
    event = Event(
        event_type=event_type,
        subject_entity_type=subject_entity_type,
        subject_entity_id=subject_entity_id or molecule.id,
        molecule_id=molecule.id,
        company_id=company.id if company else None,
        summary=summary or f"{event_type.value} for {molecule.preferred_name}",
        evidence_id=ev.id,
        dedupe_key=(
            f"evt:{event_type.value}:{molecule.id}:"
            f"{company.id if company else 'none'}:{event_date}:{uuid.uuid4().hex[:8]}"
        ),
        event_date=event_date,
        published_at=published_at or doc.published_at,
        retrieved_at=doc.retrieved_at,
        observed_at=observed_at or doc.retrieved_at,
    )
    session.add(event)
    session.flush()
    return event


def make_regulatory_application(
    session: Session,
    doc: SourceDocument,
    *,
    applicant: Company,
    application_number: str,
    application_type: ApplicationType = ApplicationType.ANDA,
    approval_date: dt.date | None = None,
) -> RegulatoryApplication:
    ev = make_evidence(session, doc, f"Application {application_number}")
    app = RegulatoryApplication(
        agency=Agency.FDA,
        application_type=application_type,
        application_number=application_number,
        applicant_company_id=applicant.id,
        applicant_literal=applicant.canonical_name,
        first_approval_date=approval_date,
        evidence_id=ev.id,
        event_date=approval_date,
        published_at=doc.published_at,
        retrieved_at=doc.retrieved_at,
        observed_at=doc.retrieved_at,
    )
    session.add(app)
    session.flush()
    return app


def make_capability(
    session: Session,
    doc: SourceDocument,
    *,
    company: Company,
    capability_type: CapabilityType,
    molecule: Molecule | None = None,
    assertion_level: AssertionLevel = AssertionLevel.FACT,
    inference_basis: str | None = None,
    value_text: str | None = None,
) -> CompanyCapability:
    ev = make_evidence(
        session, doc,
        f"{company.canonical_name} capability {capability_type.value}"
        + (f" for {molecule.preferred_name}" if molecule else ""),
    )
    cap = CompanyCapability(
        company_id=company.id,
        capability_type=capability_type,
        molecule_id=molecule.id if molecule else None,
        value_text=value_text,
        assertion_level=assertion_level,
        inference_basis=inference_basis,
        evidence_id=ev.id,
        dedupe_key=(
            f"cap:{company.id}:{capability_type.value}:"
            f"{molecule.id if molecule else 'none'}"
        ),
        published_at=doc.published_at,
        retrieved_at=doc.retrieved_at,
        observed_at=doc.retrieved_at,
    )
    session.add(cap)
    session.flush()
    return cap
