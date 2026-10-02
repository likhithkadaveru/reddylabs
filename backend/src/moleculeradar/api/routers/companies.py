"""Company intelligence page, including observable overlaps with the focus company.

The overlap list is the commercially interesting part and also the most dangerous
to get wrong, so its language is fixed in code: an overlap is an *observable
product/API overlap*, never an assertion that a commercial relationship exists or
should.
"""

from __future__ import annotations

import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ...core.db import get_db
from ...core.models import (
    ClinicalTrial,
    Company,
    CompanyCapability,
    CompanyRoleAssertion,
    Country,
    DrugMasterFile,
    Event,
    Molecule,
    Opportunity,
)
from ...resolution.focus import focus_company_ids
from ...resolution.normalize import normalize_company_name
from ..queries import (
    DEVELOPER_SCOPE_NOTE,
    SUPPLIER_SCOPE_NOTE,
    TRIAL_SCOPE_NOTE,
    company_ref,
    molecule_ref,
    opportunity_summary,
    scoped,
)
from ..schemas import (
    CapabilityDetail,
    CompanyDetail,
    CompanyOverlap,
    CompanyRef,
    TimelineEntry,
)
from ..security import Principal, audit, require

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("", response_model=list[CompanyRef])
def search_companies(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require("read:companies")),
    q: str | None = Query(None),
    focus_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
) -> list[CompanyRef]:
    stmt = select(Company).order_by(Company.canonical_name)
    if focus_only:
        stmt = stmt.where(Company.is_focus_company.is_(True))
    if q:
        normalized = normalize_company_name(q)
        stmt = stmt.where(or_(
            func.lower(Company.canonical_name).like(f"%{q.lower()}%"),
            Company.normalized_name.like(f"%{normalized}%"),
        ))
    return [company_ref(c) for c in db.scalars(stmt.limit(limit)).all()]


@router.get("/{company_id}", response_model=CompanyDetail)
def get_company(
    company_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require("read:companies")),
) -> CompanyDetail:
    try:
        cid = _uuid.UUID(company_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Malformed company id.") from exc

    company = db.get(Company, cid)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found.")

    focus_ids = focus_company_ids(db)

    roles = sorted({
        str(r) for r in db.scalars(
            select(CompanyRoleAssertion.role).where(
                CompanyRoleAssertion.company_id == cid
            ).distinct()
        ).all()
    })

    molecule_ids = set(db.scalars(
        select(CompanyRoleAssertion.molecule_id).where(
            CompanyRoleAssertion.company_id == cid,
            CompanyRoleAssertion.molecule_id.is_not(None),
        )
    ).all())
    molecules = db.scalars(
        select(Molecule).where(Molecule.id.in_(molecule_ids)).order_by(
            Molecule.preferred_name
        )
    ).all() if molecule_ids else []

    trial_count = db.scalar(
        select(func.count()).select_from(ClinicalTrial)
        .where(ClinicalTrial.sponsor_company_id == cid)
    ) or 0
    event_count = db.scalar(
        select(func.count()).select_from(Event).where(Event.company_id == cid)
    ) or 0
    own_dmf_count = db.scalar(
        select(func.count()).select_from(DrugMasterFile).where(
            DrugMasterFile.holder_company_id == cid,
            DrugMasterFile.dmf_type == "2",
            DrugMasterFile.status == "A",
        )
    ) or 0

    capabilities = [
        CapabilityDetail(
            capability_type=str(c.capability_type),
            molecule=molecule_ref(db.get(Molecule, c.molecule_id)) if c.molecule_id else None,
            value_text=c.value_text, assertion_level=str(c.assertion_level),
            inference_basis=c.inference_basis, confidence=c.confidence,
            evidence_id=c.evidence_id,
        )
        for c in db.scalars(
            select(CompanyCapability).where(CompanyCapability.company_id == cid)
        ).all()
    ]

    overlaps, overlap_counts = _compute_overlaps(db, cid, focus_ids, molecule_ids)

    opportunities = db.scalars(
        select(Opportunity).where(Opportunity.counterparty_company_id == cid)
        .order_by(Opportunity.as_of_date.desc(), Opportunity.opportunity_score.desc())
        .limit(20)
    ).all()

    events = db.scalars(
        select(Event).where(Event.company_id == cid)
        .order_by(Event.event_date.desc().nullslast()).limit(100)
    ).all()
    timeline = [
        TimelineEntry(
            id=e.id, event_type=str(e.event_type), event_date=e.event_date,
            published_at=e.published_at, summary=e.summary,
            company=company_ref(company), payload=e.payload, evidence_id=e.evidence_id,
        )
        for e in events
    ]

    countries = []
    if company.country_id:
        country = db.get(Country, company.country_id)
        if country:
            countries.append(country.iso_alpha2)

    audit(db, principal, "read:company", resource_type="company",
          resource_id=str(cid), request=request)

    has_dmf_source = (
        db.scalar(select(func.count()).select_from(DrugMasterFile)) or 0
    ) > 0

    return CompanyDetail(
        company=company_ref(company),
        countries=countries,
        roles=roles,
        molecules=[molecule_ref(m) for m in molecules],
        trials=scoped(trial_count, TRIAL_SCOPE_NOTE, has_source=True),
        regulatory_events=scoped(event_count, DEVELOPER_SCOPE_NOTE, has_source=True),
        observed_api_filings=scoped(
            own_dmf_count, SUPPLIER_SCOPE_NOTE, has_source=has_dmf_source
        ),
        capabilities=capabilities,
        overlaps=overlaps,
        overlap_counts=overlap_counts,
        opportunities=[opportunity_summary(db, o) for o in opportunities],
        timeline=timeline,
    )


def _compute_overlaps(
    db: Session, company_id: _uuid.UUID, focus_ids: set[_uuid.UUID],
    company_molecule_ids: set[_uuid.UUID],
) -> tuple[list[CompanyOverlap], dict[str, int]]:
    """Observable product/API overlaps with the focus company.

    An overlap exists where the focus company holds an active API filing for a
    molecule this company is publicly active on. Strength is graded on what the
    evidence supports, never on an assumed relationship:

    * STRONG   — focus supplies the API, and this company holds no API filing of
                 its own for it.
    * MODERATE — focus supplies the API, and this company also holds one, so
                 external sourcing is less likely.
    * INSUFFICIENT_EVIDENCE — we cannot establish the company's own API position.
    """
    if company_id in focus_ids or not focus_ids or not company_molecule_ids:
        return [], {"strong": 0, "moderate": 0, "insufficient_evidence": 0, "total": 0}

    focus_supplied = set(db.scalars(
        select(DrugMasterFile.molecule_id).where(
            DrugMasterFile.holder_company_id.in_(focus_ids),
            DrugMasterFile.dmf_type == "2",
            DrugMasterFile.status == "A",
            DrugMasterFile.molecule_id.is_not(None),
        )
    ).all())

    own_dmf_molecules = set(db.scalars(
        select(DrugMasterFile.molecule_id).where(
            DrugMasterFile.holder_company_id == company_id,
            DrugMasterFile.dmf_type == "2",
            DrugMasterFile.status == "A",
        )
    ).all())
    any_dmf_data = (db.scalar(select(func.count()).select_from(DrugMasterFile)) or 0) > 0

    overlaps: list[CompanyOverlap] = []
    counts = {"strong": 0, "moderate": 0, "insufficient_evidence": 0, "total": 0}

    for mid in sorted(company_molecule_ids & focus_supplied, key=str):
        molecule = db.get(Molecule, mid)
        if molecule is None:
            continue
        filings = db.scalar(
            select(func.count()).select_from(Event).where(
                Event.company_id == company_id, Event.molecule_id == mid
            )
        ) or 0
        trials = db.scalar(
            select(func.count()).select_from(ClinicalTrial).where(
                ClinicalTrial.sponsor_company_id == company_id,
                ClinicalTrial.molecule_id == mid,
            )
        ) or 0

        if not any_dmf_data:
            strength = "INSUFFICIENT_EVIDENCE"
            holds_own = None
        elif mid in own_dmf_molecules:
            strength = "MODERATE"
            holds_own = True
        else:
            strength = "STRONG"
            holds_own = False

        counts[strength.lower()] = counts.get(strength.lower(), 0) + 1
        counts["total"] += 1
        overlaps.append(CompanyOverlap(
            molecule=molecule_ref(molecule),
            focus_company_supplies_api=True,
            company_holds_own_api_dmf=holds_own,
            company_activity_summary=(
                f"{filings} recorded regulatory event(s) and {trials} registered "
                f"trial(s) involving this molecule in current public-source coverage."
            ),
            strength=strength,
        ))

    overlaps.sort(key=lambda o: {"STRONG": 0, "MODERATE": 1}.get(o.strength, 2))
    return overlaps, counts
