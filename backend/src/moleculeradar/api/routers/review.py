"""Entity-resolution review queue.

The human-in-the-loop surface. Every item here is a pair the system refused to
merge on its own, which is the behaviour that keeps the entity graph trustworthy —
so working this queue is real analytical work, not janitorial cleanup.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.db import get_db
from ...core.enums import (
    AliasKind,
    EntityType,
    ResolutionMethod,
    ReviewStatus,
)
from ...core.models import EntityMergeLog, ReviewQueueItem
from ..security import Principal, audit, require

router = APIRouter(prefix="/review", tags=["review"])


class ReviewItemOut(BaseModel):
    id: _uuid.UUID
    item_kind: str
    entity_type: str
    input_literal: str
    candidate_payload: dict
    proposed_action: str
    confidence: float | None
    status: str
    created_at: dt.datetime


class ReviewDecision(BaseModel):
    accept: bool
    note: str | None = None


@router.get("", response_model=list[ReviewItemOut])
def list_review_items(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require("read:reviewqueue")),
    status_filter: str = Query("pending", alias="status"),
    limit: int = Query(50, ge=1, le=200),
) -> list[ReviewItemOut]:
    stmt = select(ReviewQueueItem).order_by(ReviewQueueItem.created_at.desc())
    if status_filter != "all":
        stmt = stmt.where(ReviewQueueItem.status == status_filter)
    return [
        ReviewItemOut(
            id=r.id, item_kind=r.item_kind, entity_type=str(r.entity_type),
            input_literal=r.input_literal, candidate_payload=r.candidate_payload,
            proposed_action=r.proposed_action, confidence=r.confidence,
            status=str(r.status), created_at=r.created_at,
        )
        for r in db.scalars(stmt.limit(limit)).all()
    ]


@router.post("/{item_id}", response_model=ReviewItemOut)
def decide_review_item(
    item_id: str,
    decision: ReviewDecision,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require("write:reviewqueue")),
) -> ReviewItemOut:
    """Record a human decision on an ambiguous resolution.

    Accepting a merge candidate records an alias pointing the literal at the
    candidate entity and writes a merge-log row attributed to the deciding user.
    The provisional entity is deliberately *not* deleted: other rows may already
    reference it, and silently removing it would break those references. Merging
    the rows themselves is a separate, explicit operation.
    """
    try:
        rid = _uuid.UUID(item_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Malformed review item id.") from exc

    item = db.get(ReviewQueueItem, rid)
    if item is None:
        raise HTTPException(status_code=404, detail="Review item not found.")
    if item.status != ReviewStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Review item already {item.status}; decisions are not reversible here.",
        )

    item.status = ReviewStatus.ACCEPTED if decision.accept else ReviewStatus.REJECTED
    item.decided_by = principal.email
    item.decided_at = dt.datetime.now(dt.UTC)
    item.decision_note = decision.note

    if decision.accept:
        candidate_id = item.candidate_payload.get("candidate_entity_id")
        if candidate_id:
            entity_type = EntityType(str(item.entity_type))
            target = _uuid.UUID(candidate_id)
            normalized = item.candidate_payload.get("normalized_literal")
            if normalized:
                from ...resolution.engine import claim_alias

                claim_alias(
                    db, entity_type, target, item.input_literal, normalized,
                    ResolutionMethod.HUMAN_REVIEW, 1.0, item.source_document_id,
                    alias_kind=AliasKind.SOURCE_LITERAL,
                )
            db.add(EntityMergeLog(
                entity_type=entity_type,
                surviving_entity_id=target,
                merged_entity_id=(
                    _uuid.UUID(item.candidate_payload["provisional_entity_id"])
                    if item.candidate_payload.get("provisional_entity_id") else None
                ),
                input_literal=item.input_literal,
                resolution_method=ResolutionMethod.HUMAN_REVIEW,
                score=1.0,
                rationale=(
                    f"Human review accepted the link. Note: "
                    f"{decision.note or 'none provided'}."
                ),
                decided_by=principal.email,
                source_document_id=item.source_document_id,
            ))

    db.commit()
    audit(db, principal, "decide:review_item", resource_type="review_queue_item",
          resource_id=str(rid), detail={"accepted": decision.accept}, request=request)

    return ReviewItemOut(
        id=item.id, item_kind=item.item_kind, entity_type=str(item.entity_type),
        input_literal=item.input_literal, candidate_payload=item.candidate_payload,
        proposed_action=item.proposed_action, confidence=item.confidence,
        status=str(item.status), created_at=item.created_at,
    )
