"""Investigation endpoint — what INVESTIGATE produces.

An investigation is reproducible: the same opportunity at the same as-of date with
the same engine version yields the same document, because it is assembled from
stored evidence rather than generated afresh.
"""

from __future__ import annotations

import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.db import get_db
from ...core.models import Investigation, Opportunity
from ...llm.investigation import build_investigation
from ..schemas import InvestigationResponse, InvestigationSection
from ..security import Principal, audit, require

router = APIRouter(prefix="/investigations", tags=["investigations"])


@router.post("/{opportunity_id}", response_model=InvestigationResponse)
def create_investigation(
    opportunity_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require("write:investigation")),
    force: bool = Query(False, description="Rebuild even if one already exists."),
) -> InvestigationResponse:
    try:
        oid = _uuid.UUID(opportunity_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Malformed opportunity id.") from exc

    opportunity = db.get(Opportunity, oid)
    if opportunity is None:
        raise HTTPException(status_code=404, detail="Opportunity not found.")

    existing = db.scalar(
        select(Investigation).where(
            Investigation.opportunity_id == oid,
            Investigation.as_of_date == opportunity.as_of_date,
            Investigation.engine_version == opportunity.engine_version,
        ).order_by(Investigation.created_at.desc())
    )
    if existing is not None and not force:
        return _to_response(existing)

    sections = build_investigation(db, opportunity)
    investigation = Investigation(
        opportunity_id=oid,
        as_of_date=opportunity.as_of_date,
        engine_version=opportunity.engine_version,
        sections={"sections": sections},
        # Deterministic assembly from stored evidence. When LLM rewording is
        # enabled this becomes "llm_assisted" and the audit record is attached.
        synthesis_method="deterministic_from_evidence",
        created_by=principal.email,
    )
    db.add(investigation)
    db.commit()

    audit(db, principal, "create:investigation", resource_type="opportunity",
          resource_id=str(oid), request=request)
    return _to_response(investigation)


@router.get("/{opportunity_id}", response_model=InvestigationResponse)
def get_investigation(
    opportunity_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require("read:opportunities")),
) -> InvestigationResponse:
    try:
        oid = _uuid.UUID(opportunity_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Malformed opportunity id.") from exc

    investigation = db.scalar(
        select(Investigation).where(Investigation.opportunity_id == oid)
        .order_by(Investigation.created_at.desc())
    )
    if investigation is None:
        raise HTTPException(
            status_code=404,
            detail="No investigation exists yet for this opportunity. POST to create one.",
        )
    return _to_response(investigation)


def _to_response(investigation: Investigation) -> InvestigationResponse:
    raw = (investigation.sections or {}).get("sections", [])
    return InvestigationResponse(
        id=investigation.id,
        opportunity_id=investigation.opportunity_id,
        as_of_date=investigation.as_of_date,
        engine_version=investigation.engine_version,
        synthesis_method=investigation.synthesis_method,
        sections=[InvestigationSection(**s) for s in raw],
        created_at=investigation.created_at,
    )
