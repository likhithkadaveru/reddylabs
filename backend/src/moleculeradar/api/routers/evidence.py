"""Evidence drawer.

Every claim in the product must be clickable through to this endpoint. It returns
the full chain — claim → evidence → source document → publisher, licence and
disclaimer — plus the model-extraction audit record when a claim was
model-assisted.
"""

from __future__ import annotations

import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...core.db import get_db
from ..queries import evidence_detail
from ..schemas import EvidenceDetail
from ..security import Principal, require

router = APIRouter(prefix="/evidence", tags=["evidence"])


@router.get("/{evidence_id}", response_model=EvidenceDetail)
def get_evidence(
    evidence_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require("read:evidence")),
) -> EvidenceDetail:
    try:
        eid = _uuid.UUID(evidence_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Malformed evidence id.") from exc

    detail = evidence_detail(db, eid)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Evidence not found. A claim whose evidence cannot be retrieved must "
                "not be treated as supported."
            ),
        )
    return detail


@router.get("", response_model=list[EvidenceDetail])
def get_evidence_batch(
    ids: list[str] = Query(..., description="Evidence ids, repeated."),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require("read:evidence")),
) -> list[EvidenceDetail]:
    """Batch fetch, so a signal's citations load in one request."""
    if len(ids) > 100:
        raise HTTPException(status_code=400, detail="At most 100 evidence ids per request.")
    out = []
    for raw in ids:
        try:
            eid = _uuid.UUID(raw)
        except ValueError:
            continue
        detail = evidence_detail(db, eid)
        if detail is not None:
            out.append(detail)
    return out
