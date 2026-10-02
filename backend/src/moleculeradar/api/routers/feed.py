"""Opportunity feed — the home screen.

Answers one question: what changed, and why should I care?

The empty states are as important as the populated one. With no data the response
is `DATA_UNAVAILABLE` with the source status attached, so the UI can tell the
operator exactly which sources are missing instead of rendering a plausible-looking
but empty dashboard.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import ENGINE_VERSION
from ...core.db import get_db
from ...core.enums import OpportunityType, PriorityBand
from ...core.models import Event, Molecule, Opportunity
from ..queries import (
    last_refresh,
    latest_as_of_date,
    opportunity_summary,
    source_status_rows,
)
from ..schemas import (
    DataAvailability,
    FeedBandCounts,
    FeedResponse,
    OpportunityDetail,
    TimelineEntry,
)
from ..security import Principal, audit, require

router = APIRouter(prefix="/feed", tags=["feed"])


@router.get("", response_model=FeedResponse)
def get_feed(
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require("read:opportunities")),
    as_of: dt.date | None = Query(None, description="Feed as of a specific engine run date."),
    limit: int = Query(25, ge=1, le=200),
    min_band: str | None = Query(
        None, description="Filter to a minimum priority band: HIGH or WATCH."
    ),
) -> FeedResponse:
    molecule_count = db.scalar(select(func.count()).select_from(Molecule)) or 0
    refresh = last_refresh(db)

    if molecule_count == 0:
        # Nothing has ever been ingested. Say so precisely, and hand the UI the
        # source status so it can show what is missing.
        return FeedResponse(
            availability=DataAvailability.DATA_UNAVAILABLE,
            last_intelligence_refresh=refresh,
            engine_version=ENGINE_VERSION,
            message=(
                "No source data has been ingested, so no opportunities can exist. "
                "Molecule Radar shows nothing rather than placeholder content. Run "
                "`moleculeradar sources verify` to check source reachability, then "
                "`moleculeradar sources ingest all`."
            ),
            source_status=source_status_rows(db),
        )

    target_date = as_of or latest_as_of_date(db)
    if target_date is None:
        return FeedResponse(
            availability=DataAvailability.NO_QUALIFYING_SIGNALS,
            last_intelligence_refresh=refresh,
            engine_version=ENGINE_VERSION,
            message=(
                "Source data is present but the opportunity engine has not been run. "
                "Run `moleculeradar engine run`."
            ),
            source_status=source_status_rows(db),
        )

    stmt = select(Opportunity).where(Opportunity.as_of_date == target_date)
    if min_band == "HIGH":
        stmt = stmt.where(Opportunity.priority_band == PriorityBand.HIGH)
    elif min_band == "WATCH":
        stmt = stmt.where(
            Opportunity.priority_band.in_([PriorityBand.HIGH, PriorityBand.WATCH])
        )

    all_for_date = db.scalars(stmt.order_by(Opportunity.opportunity_score.desc())).all()

    if not all_for_date:
        return FeedResponse(
            availability=DataAvailability.NO_QUALIFYING_SIGNALS,
            as_of_date=target_date,
            last_intelligence_refresh=refresh,
            engine_version=ENGINE_VERSION,
            message=(
                f"The engine ran for {target_date.isoformat()} and no molecule met the "
                f"scoring thresholds. This is a real result, not missing data."
            ),
            source_status=source_status_rows(db),
        )

    bands = FeedBandCounts(
        high=sum(1 for o in all_for_date if o.priority_band == PriorityBand.HIGH),
        watch=sum(1 for o in all_for_date if o.priority_band == PriorityBand.WATCH),
        low_confidence=sum(
            1 for o in all_for_date if o.priority_band == PriorityBand.LOW_CONFIDENCE
        ),
        insufficient_evidence=sum(
            1 for o in all_for_date
            if o.priority_band == PriorityBand.INSUFFICIENT_EVIDENCE
        ),
    )

    sell = [o for o in all_for_date if o.opportunity_type == OpportunityType.SELL][:limit]
    build = [o for o in all_for_date if o.opportunity_type == OpportunityType.BUILD][:limit]

    audit(db, principal, "read:feed", resource_type="feed",
          resource_id=target_date.isoformat(),
          detail={"returned": len(sell) + len(build)}, request=request)

    return FeedResponse(
        availability=DataAvailability.AVAILABLE,
        as_of_date=target_date,
        last_intelligence_refresh=refresh,
        engine_version=ENGINE_VERSION,
        total=len(all_for_date),
        bands=bands,
        sell=[opportunity_summary(db, o) for o in sell],
        build=[opportunity_summary(db, o) for o in build],
        source_status=source_status_rows(db),
    )


@router.get("/opportunity/{opportunity_id}", response_model=OpportunityDetail)
def get_opportunity(
    opportunity_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require("read:opportunities")),
) -> OpportunityDetail:
    import uuid as _uuid

    from fastapi import HTTPException

    from ...core.models import FeatureSnapshot
    from ..queries import counter_thesis_details, signal_details

    try:
        oid = _uuid.UUID(opportunity_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Malformed opportunity id.") from exc

    opp = db.get(Opportunity, oid)
    if opp is None:
        raise HTTPException(status_code=404, detail="Opportunity not found.")

    summary = opportunity_summary(db, opp, include_top_signals=0)
    snapshot = db.get(FeatureSnapshot, opp.feature_snapshot_id)
    features = dict(snapshot.features) if snapshot else {}
    coverage = features.pop("_coverage", {}) if features else {}

    # Which signals could not be evaluated, and why. Shown so a reader can tell a
    # genuine negative from a gap in our coverage.
    from ...signals.definitions import SIGNALS_BY_TYPE

    fired = {s.signal_key for s in signal_details(db, opp.id)}
    unevaluated = []
    for definition in SIGNALS_BY_TYPE[OpportunityType(str(opp.opportunity_type))]:
        if definition.key in fired:
            continue
        missing = [f for f in definition.required_features if features.get(f) is None]
        unevaluated.append({
            "signal_key": definition.key,
            "max_points": definition.max_points,
            "reason": "missing_data" if missing else "did_not_fire",
            "missing_features": missing,
            "rationale": definition.rationale,
        })

    audit(db, principal, "read:opportunity", resource_type="opportunity",
          resource_id=str(oid), request=request)

    return OpportunityDetail(
        **summary.model_dump(),
        signals=signal_details(db, opp.id),
        counter_theses=counter_thesis_details(db, opp.id),
        features=features,
        coverage=coverage,
        unevaluated_signals=unevaluated,
    )


@router.get("/changes", response_model=list[TimelineEntry])
def recent_changes(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require("read:opportunities")),
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(50, ge=1, le=200),
) -> list[TimelineEntry]:
    """Recent dated events across the whole graph — the 'what changed' stream."""
    from ...core.models import Company

    since = dt.date.today() - dt.timedelta(days=days)
    events = db.scalars(
        select(Event).where(Event.event_date >= since)
        .order_by(Event.event_date.desc(), Event.created_at.desc())
        .limit(limit)
    ).all()
    out = []
    for e in events:
        company = db.get(Company, e.company_id) if e.company_id else None
        from ..queries import company_ref

        out.append(TimelineEntry(
            id=e.id, event_type=str(e.event_type), event_date=e.event_date,
            published_at=e.published_at, summary=e.summary,
            company=company_ref(company), payload=e.payload, evidence_id=e.evidence_id,
        ))
    return out
