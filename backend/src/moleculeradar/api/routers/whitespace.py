"""White Space explorer.

Rows are molecules; columns are the deterministic features and the resulting
scores. Filtering, sorting and CSV export are the point — this is the screen an
analyst actually works in.

Every exported CSV carries the scope note in a header comment, because a
spreadsheet is exactly where a number gets detached from its qualification and
circulated as fact.
"""

from __future__ import annotations

import csv
import datetime as dt
import io

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...core.db import get_db
from ...core.models import Event, FeatureSnapshot, Molecule, Opportunity
from ..queries import latest_as_of_date, molecule_ref, therapeutic_area_name
from ..schemas import DataAvailability, WhiteSpaceResponse, WhiteSpaceRow
from ..security import Principal, require

router = APIRouter(prefix="/whitespace", tags=["whitespace"])

SCOPE_NOTE = (
    "All counts reflect Molecule Radar's current public-source coverage and are not "
    "complete global censuses. Supplier counts are active Type II US drug master file "
    "holders. Opportunity Score and Evidence Confidence are separate measures and "
    "must not be combined."
)


def _focus_api_status(features: dict) -> str:
    holds = features.get("focus_company_holds_api_dmf")
    if holds is True:
        return "HOLDS_ACTIVE_DMF"
    if holds is False:
        return "NO_DMF_IDENTIFIED"
    return "UNKNOWN"


def _build_rows(
    db: Session, as_of: dt.date, *, therapeutic_area: str | None,
    opportunity_type: str | None, min_score: float | None,
    max_suppliers: int | None, focus_status: str | None,
    sort_by: str, descending: bool, limit: int, offset: int,
) -> tuple[list[WhiteSpaceRow], int]:
    snapshots = db.scalars(
        select(FeatureSnapshot).where(FeatureSnapshot.as_of_date == as_of)
    ).all()

    rows: list[WhiteSpaceRow] = []
    for snapshot in snapshots:
        molecule = db.get(Molecule, snapshot.molecule_id)
        if molecule is None:
            continue
        features = dict(snapshot.features or {})
        features.pop("_coverage", None)

        best = db.scalar(
            select(Opportunity).where(
                Opportunity.molecule_id == molecule.id,
                Opportunity.as_of_date == as_of,
            ).order_by(Opportunity.opportunity_score.desc()).limit(1)
        )
        last_event = db.scalar(
            select(func.max(Event.event_date)).where(Event.molecule_id == molecule.id)
        )
        ta_name = therapeutic_area_name(db, molecule.therapeutic_area_id)

        row = WhiteSpaceRow(
            molecule=molecule_ref(molecule),
            therapeutic_area=ta_name,
            developer_count=features.get("distinct_sponsor_count"),
            developer_growth_12m=features.get("trial_growth_12m"),
            observed_api_supplier_count=features.get("observed_api_supplier_count"),
            focus_api_status=_focus_api_status(features),
            focus_capability_adjacency=features.get("focus_capability_adjacency_score"),
            regulatory_events_12m=features.get("regulatory_event_count_12m"),
            months_to_next_loe_event=features.get("months_to_next_loe_event"),
            shortage_events_36m=features.get("shortage_events_36m"),
            opportunity_type=str(best.opportunity_type) if best else None,
            opportunity_score=best.opportunity_score if best else None,
            evidence_confidence=str(best.evidence_confidence) if best else None,
            last_significant_event=last_event,
        )

        if therapeutic_area and (ta_name or "").lower() != therapeutic_area.lower():
            continue
        if opportunity_type and row.opportunity_type != opportunity_type.upper():
            continue
        if min_score is not None and (row.opportunity_score or 0) < min_score:
            continue
        if max_suppliers is not None:
            count = row.observed_api_supplier_count
            # A molecule with unknown supplier count is excluded from a
            # supplier-count filter rather than being treated as zero, which would
            # make unmeasured molecules look like uncontested ones.
            if count is None or count > max_suppliers:
                continue
        if focus_status and row.focus_api_status != focus_status.upper():
            continue
        rows.append(row)

    sort_keys = {
        "opportunity_score": lambda r: (r.opportunity_score is None, r.opportunity_score or 0),
        "molecule": lambda r: r.molecule.preferred_name.lower(),
        "developer_count": lambda r: (r.developer_count is None, r.developer_count or 0),
        "observed_api_supplier_count": lambda r: (
            r.observed_api_supplier_count is None, r.observed_api_supplier_count or 0
        ),
        "months_to_next_loe_event": lambda r: (
            r.months_to_next_loe_event is None, r.months_to_next_loe_event or 10_000
        ),
        "last_significant_event": lambda r: (
            r.last_significant_event is None, r.last_significant_event or dt.date.min
        ),
    }
    key = sort_keys.get(sort_by, sort_keys["opportunity_score"])
    rows.sort(key=key, reverse=descending)
    total = len(rows)
    return rows[offset: offset + limit], total


@router.get("", response_model=WhiteSpaceResponse)
def get_whitespace(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require("read:whitespace")),
    as_of: dt.date | None = Query(None),
    therapeutic_area: str | None = Query(None),
    opportunity_type: str | None = Query(None, description="SELL or BUILD."),
    min_score: float | None = Query(None, ge=0, le=100),
    max_suppliers: int | None = Query(None, ge=0),
    focus_status: str | None = Query(
        None, description="HOLDS_ACTIVE_DMF, NO_DMF_IDENTIFIED or UNKNOWN."
    ),
    sort_by: str = Query("opportunity_score"),
    descending: bool = Query(True),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> WhiteSpaceResponse:
    target = as_of or latest_as_of_date(db)
    if target is None:
        molecule_count = db.scalar(select(func.count()).select_from(Molecule)) or 0
        return WhiteSpaceResponse(
            availability=(
                DataAvailability.DATA_UNAVAILABLE if molecule_count == 0
                else DataAvailability.NO_QUALIFYING_SIGNALS
            ),
            message=(
                "No source data has been ingested."
                if molecule_count == 0 else
                "Source data is present but the opportunity engine has not been run."
            ),
            scope_note=SCOPE_NOTE,
        )

    rows, total = _build_rows(
        db, target, therapeutic_area=therapeutic_area,
        opportunity_type=opportunity_type, min_score=min_score,
        max_suppliers=max_suppliers, focus_status=focus_status,
        sort_by=sort_by, descending=descending, limit=limit, offset=offset,
    )
    return WhiteSpaceResponse(
        availability=(
            DataAvailability.AVAILABLE if rows else DataAvailability.NO_QUALIFYING_SIGNALS
        ),
        as_of_date=target, total=total, rows=rows,
        message=None if rows else "No molecules matched the current filters.",
        scope_note=SCOPE_NOTE,
    )


@router.get("/export.csv")
def export_whitespace_csv(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require("read:whitespace")),
    as_of: dt.date | None = Query(None),
    therapeutic_area: str | None = Query(None),
    opportunity_type: str | None = Query(None),
    min_score: float | None = Query(None, ge=0, le=100),
    max_suppliers: int | None = Query(None, ge=0),
    focus_status: str | None = Query(None),
    sort_by: str = Query("opportunity_score"),
    descending: bool = Query(True),
) -> StreamingResponse:
    target = as_of or latest_as_of_date(db)
    rows: list[WhiteSpaceRow] = []
    if target is not None:
        rows, _ = _build_rows(
            db, target, therapeutic_area=therapeutic_area,
            opportunity_type=opportunity_type, min_score=min_score,
            max_suppliers=max_suppliers, focus_status=focus_status,
            sort_by=sort_by, descending=descending, limit=10_000, offset=0,
        )

    buffer = io.StringIO()
    # The scope note travels with the file. A CSV is precisely where a number gets
    # separated from its qualification and then circulated as fact.
    buffer.write("# Molecule Radar white space export\n")
    buffer.write(f"# Generated: {dt.datetime.now(dt.UTC).isoformat()}\n")
    buffer.write(f"# As-of date: {target.isoformat() if target else 'none'}\n")
    buffer.write(f"# Scope: {SCOPE_NOTE}\n")
    buffer.write("#\n")

    writer = csv.writer(buffer)
    writer.writerow([
        "molecule", "unii", "therapeutic_area", "developer_count",
        "developer_growth_12m", "observed_api_supplier_count_public_coverage",
        "focus_api_status", "focus_capability_adjacency", "regulatory_events_12m",
        "months_to_next_loe_event", "shortage_events_36m", "opportunity_type",
        "opportunity_score", "evidence_confidence", "last_significant_event",
    ])
    for row in rows:
        writer.writerow([
            row.molecule.preferred_name, row.molecule.unii or "",
            row.therapeutic_area or "",
            _csv_value(row.developer_count), _csv_value(row.developer_growth_12m),
            _csv_value(row.observed_api_supplier_count), row.focus_api_status,
            _csv_value(row.focus_capability_adjacency),
            _csv_value(row.regulatory_events_12m),
            _csv_value(row.months_to_next_loe_event),
            _csv_value(row.shortage_events_36m),
            row.opportunity_type or "", _csv_value(row.opportunity_score),
            row.evidence_confidence or "",
            row.last_significant_event.isoformat() if row.last_significant_event else "",
        ])

    buffer.seek(0)
    filename = f"molecule-radar-whitespace-{target.isoformat() if target else 'empty'}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _csv_value(value) -> str:
    """Render a missing value as DATA_UNAVAILABLE, never as 0 or an empty cell.

    An empty cell in a spreadsheet gets read as zero, which would turn "we did not
    measure this" into "there are none of these".
    """
    if value is None:
        return "DATA_UNAVAILABLE"
    return str(value)
