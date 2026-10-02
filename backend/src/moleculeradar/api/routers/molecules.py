"""Molecule intelligence page."""

from __future__ import annotations

import datetime as dt
import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ...core.db import get_db
from ...core.models import (
    ClinicalTrial,
    Company,
    CompanyCapability,
    DrugMasterFile,
    Event,
    Molecule,
    Opportunity,
)
from ...core.temporal import TemporalContext
from ...resolution.focus import focus_company_ids
from ...resolution.normalize import normalize_molecule_name
from ...signals.features import FeatureComputer
from ..queries import (
    DEVELOPER_SCOPE_NOTE,
    SHORTAGE_SCOPE_NOTE,
    SUPPLIER_SCOPE_NOTE,
    TRIAL_SCOPE_NOTE,
    company_ref,
    molecule_ref,
    opportunity_summary,
    scoped,
    therapeutic_area_name,
)
from ..schemas import (
    CapabilityDetail,
    MoleculeDetail,
    MoleculeRef,
    TimelineEntry,
)
from ..security import Principal, audit, require

router = APIRouter(prefix="/molecules", tags=["molecules"])


@router.get("", response_model=list[MoleculeRef])
def search_molecules(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require("read:molecules")),
    q: str | None = Query(None, description="Name search."),
    limit: int = Query(50, ge=1, le=200),
) -> list[MoleculeRef]:
    stmt = select(Molecule).order_by(Molecule.preferred_name)
    if q:
        # Search the normalised form as well as the display name, so "metformin
        # hcl" finds the moiety row.
        normalized = normalize_molecule_name(q)
        pattern = f"%{q.lower()}%"
        stmt = stmt.where(or_(
            func.lower(Molecule.preferred_name).like(pattern),
            Molecule.normalized_name.like(f"%{normalized}%"),
        ))
    return [molecule_ref(m) for m in db.scalars(stmt.limit(limit)).all()]


@router.get("/{molecule_id}", response_model=MoleculeDetail)
def get_molecule(
    molecule_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require("read:molecules")),
    as_of: dt.date | None = Query(None),
) -> MoleculeDetail:
    try:
        mid = _uuid.UUID(molecule_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Malformed molecule id.") from exc

    molecule = db.get(Molecule, mid)
    if molecule is None:
        raise HTTPException(status_code=404, detail="Molecule not found.")

    ctx = TemporalContext(as_of_date=as_of)
    focus_ids = focus_company_ids(db)
    fs = FeatureComputer(db, ctx).compute(mid, focus_ids)
    coverage = dict(fs.coverage)

    # API suppliers, from active Type II DMFs.
    supplier_rows = db.execute(
        select(Company).join(
            DrugMasterFile, DrugMasterFile.holder_company_id == Company.id
        ).where(
            DrugMasterFile.molecule_id == mid,
            DrugMasterFile.dmf_type == "2",
            DrugMasterFile.status == "A",
        ).distinct()
    ).scalars().all()

    # Developers: anyone with a filing event or a registered trial.
    developer_ids = set(db.scalars(
        select(Event.company_id).where(
            Event.molecule_id == mid, Event.company_id.is_not(None)
        )
    ).all()) | set(db.scalars(
        select(ClinicalTrial.sponsor_company_id).where(
            ClinicalTrial.molecule_id == mid,
            ClinicalTrial.sponsor_company_id.is_not(None),
        )
    ).all())
    developers = db.scalars(
        select(Company).where(Company.id.in_(developer_ids))
    ).all() if developer_ids else []

    focus_caps = []
    if focus_ids:
        cap_rows = db.scalars(
            select(CompanyCapability).where(
                CompanyCapability.company_id.in_(focus_ids),
                CompanyCapability.molecule_id == mid,
            )
        ).all()
        for cap in cap_rows:
            focus_caps.append(CapabilityDetail(
                capability_type=str(cap.capability_type),
                molecule=molecule_ref(molecule),
                value_text=cap.value_text,
                assertion_level=str(cap.assertion_level),
                inference_basis=cap.inference_basis,
                confidence=cap.confidence,
                evidence_id=cap.evidence_id,
            ))

    opportunities = db.scalars(
        select(Opportunity).where(Opportunity.molecule_id == mid)
        .order_by(Opportunity.as_of_date.desc(), Opportunity.opportunity_score.desc())
        .limit(20)
    ).all()

    events = db.scalars(
        select(Event).where(Event.molecule_id == mid)
        .order_by(Event.event_date.desc().nullslast())
        .limit(200)
    ).all()
    timeline = []
    for e in events:
        company = db.get(Company, e.company_id) if e.company_id else None
        timeline.append(TimelineEntry(
            id=e.id, event_type=str(e.event_type), event_date=e.event_date,
            published_at=e.published_at, summary=e.summary,
            company=company_ref(company), payload=e.payload, evidence_id=e.evidence_id,
        ))

    next_loe = fs.values.get("months_to_next_loe_event")
    next_loe_date = None
    for key in ("next_exclusivity_expiry_date", "next_patent_expiry_date"):
        raw = fs.values.get(key)
        if raw:
            parsed = dt.date.fromisoformat(raw)
            if next_loe_date is None or parsed < next_loe_date:
                next_loe_date = parsed

    audit(db, principal, "read:molecule", resource_type="molecule",
          resource_id=str(mid), request=request)

    return MoleculeDetail(
        molecule=molecule_ref(molecule),
        therapeutic_area=therapeutic_area_name(db, molecule.therapeutic_area_id),
        observed_api_suppliers=scoped(
            fs.values.get("observed_api_supplier_count"), SUPPLIER_SCOPE_NOTE,
            has_source=coverage.get("dmf", False),
        ),
        focus_company_supplies_api=fs.values.get("focus_company_holds_api_dmf"),
        distinct_developers=scoped(
            len(developer_ids) if developer_ids or coverage.get("events") else None,
            DEVELOPER_SCOPE_NOTE,
            has_source=coverage.get("events", False) or coverage.get("clinical_trials", False),
        ),
        active_trials=scoped(
            fs.values.get("active_trial_count"), TRIAL_SCOPE_NOTE,
            has_source=coverage.get("clinical_trials", False),
        ),
        next_loe_event_months=next_loe,
        next_loe_event_date=next_loe_date,
        shortage_events_36m=scoped(
            fs.values.get("shortage_events_36m"), SHORTAGE_SCOPE_NOTE,
            has_source=coverage.get("shortages", False),
        ),
        markets=fs.values.get("trial_countries") or [],
        api_suppliers=[company_ref(c) for c in supplier_rows],
        developers=[company_ref(c) for c in developers],
        focus_capabilities=focus_caps,
        opportunities=[opportunity_summary(db, o) for o in opportunities],
        timeline=timeline,
        features=fs.values,
        coverage=coverage,
    )
