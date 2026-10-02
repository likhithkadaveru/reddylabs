"""Data Health and system status.

Data Health is admin-only: it exposes operational detail (quarantine reasons,
failed runs, resolution backlog) that an analyst does not need and that would
clutter the intelligence surface.

System status is open to any authenticated user, because "what does this system
actually hold" is a question anyone reading an opportunity is entitled to ask.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import ENGINE_VERSION
from ...core.config import Settings, get_settings
from ...core.db import get_db
from ...core.models import (
    Company,
    DataQualityCheckResult,
    Event,
    IngestionRun,
    Molecule,
    Opportunity,
    QuarantinedRecord,
    ReviewQueueItem,
    Source,
    SourceDocument,
)
from ...quality.checks import SEVERITY_ERROR, run_all_checks
from ..queries import last_refresh
from ..schemas import (
    DataHealthCheck,
    DataHealthResponse,
    IngestionRunSummary,
    SourceStatusRow,
    SystemStatusResponse,
)
from ..security import Principal, require

router = APIRouter(tags=["health"])


@router.get("/status", response_model=SystemStatusResponse)
def system_status(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(require("read:opportunities")),
) -> SystemStatusResponse:
    molecule_count = db.scalar(select(func.count()).select_from(Molecule)) or 0
    company_count = db.scalar(select(func.count()).select_from(Company)) or 0
    event_count = db.scalar(select(func.count()).select_from(Event)) or 0
    opportunity_count = db.scalar(select(func.count()).select_from(Opportunity)) or 0
    sources_total = db.scalar(select(func.count()).select_from(Source)) or 0
    sources_live = db.scalar(
        select(func.count()).select_from(Source).where(
            Source.verification_level == "LIVE_VERIFIED"
        )
    ) or 0

    has_data = molecule_count > 0
    message = None
    if not has_data:
        message = (
            "Molecule Radar holds no ingested source data. Nothing is displayed rather "
            "than placeholder content. Check source reachability with "
            "`moleculeradar sources verify`."
        )
    elif opportunity_count == 0:
        message = (
            "Source data is present but the opportunity engine has not produced any "
            "qualifying signals yet."
        )

    return SystemStatusResponse(
        environment=settings.environment,
        engine_version=ENGINE_VERSION,
        focus_company=settings.focus_company_name,
        auth_enabled=settings.auth_enabled,
        has_any_data=has_data,
        molecule_count=molecule_count,
        company_count=company_count,
        event_count=event_count,
        opportunity_count=opportunity_count,
        last_intelligence_refresh=last_refresh(db),
        sources_live_verified=sources_live,
        sources_total=sources_total,
        message=message,
    )


@router.get("/data-health", response_model=DataHealthResponse)
def data_health(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require("read:datahealth")),
    rerun: bool = Query(
        False, description="Recompute checks now instead of reading the last stored run."
    ),
) -> DataHealthResponse:
    if rerun:
        outcomes = run_all_checks(db, persist=True)
        db.commit()
        checks = [
            DataHealthCheck(
                check_key=o.check_key, severity=o.severity, passed=o.passed,
                observed_count=o.observed_count, threshold=o.threshold,
                detail=o.detail, sample_ids=o.sample_ids or None,
            )
            for o in outcomes
        ]
    else:
        # Latest stored result per check key.
        latest_ids = select(
            func.max(DataQualityCheckResult.created_at).label("ts"),
            DataQualityCheckResult.check_key.label("key"),
        ).group_by(DataQualityCheckResult.check_key).subquery()
        rows = db.scalars(
            select(DataQualityCheckResult).join(
                latest_ids,
                (DataQualityCheckResult.check_key == latest_ids.c.key)
                & (DataQualityCheckResult.created_at == latest_ids.c.ts),
            )
        ).all()
        checks = [DataHealthCheck.model_validate(r) for r in rows]

    sources: list[SourceStatusRow] = []
    for source in db.scalars(select(Source).order_by(Source.key)).all():
        latest = db.scalar(
            select(func.max(SourceDocument.retrieved_at))
            .where(SourceDocument.source_id == source.id)
        )
        count = db.scalar(
            select(func.count()).select_from(SourceDocument)
            .where(SourceDocument.source_id == source.id)
        ) or 0
        sources.append(SourceStatusRow(
            key=source.key, name=source.name, publisher=source.publisher,
            status=str(source.status),
            verification_level=str(source.verification_level),
            access_mode=str(source.access_mode), licence=source.licence,
            expected_update_cadence=source.expected_update_cadence,
            last_live_verified_at=source.last_live_verified_at,
            last_retrieved_at=latest, document_count=count,
            is_enabled=source.is_enabled,
        ))

    runs = db.scalars(
        select(IngestionRun).order_by(IngestionRun.started_at.desc()).limit(25)
    ).all()

    return DataHealthResponse(
        generated_at=dt.datetime.now(dt.UTC),
        blocking_failures=sum(
            1 for c in checks if c.severity == SEVERITY_ERROR and not c.passed
        ),
        checks=checks,
        sources=sources,
        recent_runs=[IngestionRunSummary.model_validate(r) for r in runs],
        pending_reviews=db.scalar(
            select(func.count()).select_from(ReviewQueueItem)
            .where(ReviewQueueItem.status == "pending")
        ) or 0,
        quarantined_records=db.scalar(
            select(func.count()).select_from(QuarantinedRecord)
        ) or 0,
    )
