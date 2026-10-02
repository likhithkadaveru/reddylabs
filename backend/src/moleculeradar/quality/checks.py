"""Automated data-quality checks.

These are the checks that run on a schedule and populate the Data Health screen.
Each one answers a specific question about whether the data can be trusted, and
each returns the identifiers of offending rows so an operator can go and look
rather than being told a number.

Severity convention:

* `ERROR`   — the product may be presenting something false. Fix before trusting output.
* `WARN`    — a real problem that degrades quality but does not falsify anything.
* `INFO`    — worth knowing, usually a coverage observation.

Checks are deliberately written as plain SQL-via-ORM rather than a framework. The
point is that an engineer can read what each one actually asserts.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select, union
from sqlalchemy.orm import Session

from ..core.models import (
    ClinicalTrial,
    Company,
    CompanyCapability,
    DataQualityCheckResult,
    DrugMasterFile,
    DrugProduct,
    EntityAlias,
    Event,
    Evidence,
    Exclusivity,
    IngestionRun,
    Molecule,
    Opportunity,
    OpportunitySignal,
    Patent,
    ReviewQueueItem,
    Source,
    SourceDocument,
)

SEVERITY_ERROR = "ERROR"
SEVERITY_WARN = "WARN"
SEVERITY_INFO = "INFO"


@dataclass(slots=True)
class CheckOutcome:
    check_key: str
    severity: str
    passed: bool
    observed_count: int
    threshold: int | None
    detail: str
    sample_ids: list[str] = field(default_factory=list)


def _sample(session: Session, stmt, limit: int = 20) -> list[str]:
    return [str(r) for (r,) in session.execute(stmt.limit(limit)).all()]


# ---------------------------------------------------------------------------
# Provenance integrity
# ---------------------------------------------------------------------------


def check_evidence_without_source_document(session: Session) -> CheckOutcome:
    """Every evidence row must point at a real source document.

    A dangling evidence link means a claim in the UI whose "show me the source"
    button leads nowhere — the single most damaging kind of failure for this
    product.
    """
    stmt = select(Evidence.id).outerjoin(
        SourceDocument, SourceDocument.id == Evidence.source_document_id
    ).where(SourceDocument.id.is_(None))
    ids = _sample(session, stmt)
    count = session.scalar(
        select(func.count()).select_from(Evidence).outerjoin(
            SourceDocument, SourceDocument.id == Evidence.source_document_id
        ).where(SourceDocument.id.is_(None))
    ) or 0
    return CheckOutcome(
        "evidence_without_source_document", SEVERITY_ERROR, count == 0, count, 0,
        "Evidence rows whose source document is missing. Any claim resting on these "
        "cannot be traced to a source and must not be displayed as evidenced.",
        ids,
    )


def check_llm_evidence_without_audit(session: Session) -> CheckOutcome:
    """LLM-derived evidence must carry an extraction audit record.

    A database CHECK constraint enforces this, so a non-zero result means the
    constraint was bypassed (a raw SQL load or a migration) and the extraction is
    unauditable.
    """
    stmt = select(Evidence.id).where(
        Evidence.extraction_method == "llm_extraction",
        Evidence.llm_extraction_id.is_(None),
    )
    ids = _sample(session, stmt)
    return CheckOutcome(
        "llm_evidence_without_audit", SEVERITY_ERROR, not ids, len(ids), 0,
        "Model-derived evidence with no extraction audit record. Such a claim cannot "
        "be reproduced or reviewed and must be treated as unsupported.",
        ids,
    )


def check_opportunity_without_signals(session: Session) -> CheckOutcome:
    """An opportunity with no decomposition rows is an unexplainable score."""
    stmt = select(Opportunity.id).outerjoin(
        OpportunitySignal, OpportunitySignal.opportunity_id == Opportunity.id
    ).where(OpportunitySignal.id.is_(None))
    ids = _sample(session, stmt)
    return CheckOutcome(
        "opportunity_without_signals", SEVERITY_ERROR, not ids, len(ids), 0,
        "Opportunities with no signal rows. The score cannot be explained or "
        "reproduced, so it must not be shown.",
        ids,
    )


def check_score_matches_signal_sum(session: Session) -> CheckOutcome:
    """The score must equal the sum of its supporting signal points.

    This is the product's central explainability promise, so it is asserted
    against stored data and not only in tests.
    """
    sums = session.execute(
        select(
            Opportunity.id,
            Opportunity.opportunity_score,
            func.coalesce(func.sum(OpportunitySignal.points), 0.0),
        )
        .join(OpportunitySignal, OpportunitySignal.opportunity_id == Opportunity.id)
        .where(OpportunitySignal.direction == "SUPPORTING")
        .group_by(Opportunity.id, Opportunity.opportunity_score)
    ).all()
    mismatched = [
        str(oid) for oid, score, total in sums if abs((score or 0) - float(total)) > 0.05
    ]
    return CheckOutcome(
        "score_matches_signal_sum", SEVERITY_ERROR, not mismatched, len(mismatched), 0,
        "Opportunities whose score does not equal the sum of their supporting signal "
        "points. The displayed decomposition would not add up.",
        mismatched[:20],
    )


def check_capability_inference_without_basis(session: Session) -> CheckOutcome:
    """An INFERENCE capability must state its basis."""
    stmt = select(CompanyCapability.id).where(
        CompanyCapability.assertion_level == "INFERENCE",
        or_(CompanyCapability.inference_basis.is_(None),
            CompanyCapability.inference_basis == ""),
    )
    ids = _sample(session, stmt)
    return CheckOutcome(
        "capability_inference_without_basis", SEVERITY_ERROR, not ids, len(ids), 0,
        "Inferred capabilities with no stated basis. These would be displayed as "
        "reasoning the product cannot actually show.",
        ids,
    )


# ---------------------------------------------------------------------------
# Entity integrity
# ---------------------------------------------------------------------------


def check_duplicate_molecules(session: Session) -> CheckOutcome:
    """Two molecule rows with the same normalised name indicate a resolution failure."""
    rows = session.execute(
        select(Molecule.normalized_name, func.count())
        .group_by(Molecule.normalized_name)
        .having(func.count() > 1)
    ).all()
    return CheckOutcome(
        "duplicate_molecules", SEVERITY_ERROR, not rows, len(rows), 0,
        "Normalised molecule names appearing more than once. Supplier counts and "
        "competition features would be split across the duplicates and understated.",
        [str(r[0]) for r in rows[:20]],
    )


def check_duplicate_companies(session: Session) -> CheckOutcome:
    rows = session.execute(
        select(Company.normalized_name, func.count())
        .group_by(Company.normalized_name)
        .having(func.count() > 1)
    ).all()
    return CheckOutcome(
        "duplicate_companies", SEVERITY_ERROR, not rows, len(rows), 0,
        "Normalised company names appearing more than once. A split company would "
        "understate its filings and could hide the focus company's own capability.",
        [str(r[0]) for r in rows[:20]],
    )


def check_ambiguous_aliases(session: Session) -> CheckOutcome:
    """One alias must map to one entity per type.

    A unique constraint enforces this, so any result means it was bypassed.
    """
    rows = session.execute(
        select(EntityAlias.entity_type, EntityAlias.normalized_alias, func.count())
        .group_by(EntityAlias.entity_type, EntityAlias.normalized_alias)
        .having(func.count() > 1)
    ).all()
    return CheckOutcome(
        "ambiguous_aliases", SEVERITY_ERROR, not rows, len(rows), 0,
        "Aliases claimed by more than one entity. Resolution would be "
        "non-deterministic: the same source literal could resolve differently on "
        "different runs.",
        [f"{r[0]}:{r[1]}" for r in rows[:20]],
    )


def check_orphan_type_two_dmfs(session: Session) -> CheckOutcome:
    """Type II DMFs whose subject never resolved to a molecule.

    These are invisible to supplier counting, so a high number means the counts
    understate real supply.
    """
    count = session.scalar(
        select(func.count()).select_from(DrugMasterFile).where(
            DrugMasterFile.dmf_type == "2", DrugMasterFile.molecule_id.is_(None)
        )
    ) or 0
    total = session.scalar(
        select(func.count()).select_from(DrugMasterFile).where(
            DrugMasterFile.dmf_type == "2"
        )
    ) or 0
    ratio = (count / total) if total else 0.0
    return CheckOutcome(
        "orphan_type_two_dmfs", SEVERITY_WARN, ratio <= 0.05, count, int(total * 0.05),
        f"{count} of {total} Type II DMFs have no resolved molecule "
        f"({ratio * 100:.1f}%). These do not contribute to observed supplier counts, "
        f"so a high proportion means counts understate real supply.",
    )


def check_unresolved_company_literals(session: Session) -> CheckOutcome:
    count = session.scalar(
        select(func.count()).select_from(DrugMasterFile).where(
            DrugMasterFile.holder_company_id.is_(None)
        )
    ) or 0
    return CheckOutcome(
        "unresolved_dmf_holders", SEVERITY_WARN, count == 0, count, 0,
        "DMF rows whose holder could not be resolved to a company. Their filings are "
        "not attributed to anyone, so supplier counts are understated.",
    )


# ---------------------------------------------------------------------------
# Temporal integrity
# ---------------------------------------------------------------------------


def check_future_event_dates(session: Session) -> CheckOutcome:
    """An event dated in the future is a parsing error.

    Exclusivity and patent *expiries* are legitimately future-dated, which is why
    those are stored as expiry dates on their own tables; an `event` row is
    something that has happened.
    """
    today = dt.date.today()
    stmt = select(Event.id).where(Event.event_date > today)
    ids = _sample(session, stmt)
    return CheckOutcome(
        "future_event_dates", SEVERITY_ERROR, not ids, len(ids), 0,
        f"Events dated after {today.isoformat()}. An event is something that has "
        f"happened; a future date means a date field was misread and every "
        f"time-windowed feature over it is wrong.",
        ids,
    )


def check_implausible_dates(session: Session) -> CheckOutcome:
    """Dates before the modern pharmaceutical regulatory era are almost always errors."""
    floor = dt.date(1938, 1, 1)  # US Food, Drug, and Cosmetic Act
    stmt = select(Event.id).where(Event.event_date < floor)
    ids = _sample(session, stmt)
    return CheckOutcome(
        "implausible_event_dates", SEVERITY_WARN, not ids, len(ids), 0,
        f"Events dated before {floor.isoformat()}, which predates the modern US "
        f"regulatory framework and almost certainly indicates a misparsed date.",
        ids,
    )


def check_events_missing_publication(session: Session) -> CheckOutcome:
    """Events with no publication date are invisible to historical runs.

    The temporal gate excludes rows it cannot prove were knowable, so these are
    silently absent from every backtest.
    """
    count = session.scalar(
        select(func.count()).select_from(Event).where(
            Event.published_at.is_(None), Event.event_date.is_(None)
        )
    ) or 0
    return CheckOutcome(
        "events_missing_all_dates", SEVERITY_ERROR, count == 0, count, 0,
        "Events with neither a publication date nor an event date. These are "
        "excluded from every historical run, so backtests silently ignore them.",
    )


def check_trial_phase_regression(session: Session) -> CheckOutcome:
    """A trial moving backwards through phases is an impossible transition.

    Detected on the event stream rather than current state, because the current
    state cannot show a regression.
    """
    order = {"PHASE1": 1, "PHASE2": 2, "PHASE3": 3, "PHASE4": 4}

    def rank(value: str | None) -> int | None:
        if not value:
            return None
        best = None
        for token in value.replace(" ", "").upper().split(","):
            if token in order:
                best = max(best or 0, order[token])
        return best

    rows = session.execute(
        select(Event.id, Event.payload).where(Event.event_type == "TRIAL_PHASE_CHANGED")
    ).all()
    offenders = []
    for eid, payload in rows:
        if not payload:
            continue
        before, after = rank(payload.get("from")), rank(payload.get("to"))
        if before is not None and after is not None and after < before:
            offenders.append(str(eid))
    return CheckOutcome(
        "trial_phase_regression", SEVERITY_WARN, not offenders, len(offenders), 0,
        "Recorded trial phase transitions that move backwards. Usually a registry "
        "correction rather than a real regression, but it distorts momentum features.",
        offenders[:20],
    )


# ---------------------------------------------------------------------------
# Freshness and operations
# ---------------------------------------------------------------------------


def check_source_freshness(session: Session) -> CheckOutcome:
    """Enabled sources whose last retrieval is older than twice their cadence.

    A source nobody noticed had stopped updating is how an intelligence product
    quietly starts lying.
    """
    cadence_days = {
        "daily": 2, "frequent": 7, "weekly": 14, "monthly": 60, "quarterly": 200,
    }

    def expected(cadence: str | None) -> int:
        text = (cadence or "").lower()
        for key, days in cadence_days.items():
            if key in text:
                return days
        return 90

    now = dt.datetime.now(dt.UTC)
    stale: list[str] = []
    for source in session.scalars(select(Source).where(Source.is_enabled.is_(True))).all():
        latest = session.scalar(
            select(func.max(SourceDocument.retrieved_at)).where(
                SourceDocument.source_id == source.id
            )
        )
        if latest is None:
            stale.append(f"{source.key}: never retrieved")
            continue
        age = (now - latest).days
        limit = expected(source.expected_update_cadence)
        if age > limit:
            stale.append(f"{source.key}: {age}d old (expected within {limit}d)")

    return CheckOutcome(
        "source_freshness", SEVERITY_WARN, not stale, len(stale), 0,
        "Enabled sources that have not been retrieved within twice their expected "
        "cadence. Conclusions drawn from them may be out of date.",
        stale[:20],
    )


def check_failed_ingestion_runs(session: Session) -> CheckOutcome:
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=7)
    stmt = select(IngestionRun.id).where(
        IngestionRun.status == "failed", IngestionRun.started_at >= since
    )
    ids = _sample(session, stmt)
    return CheckOutcome(
        "failed_ingestion_runs_7d", SEVERITY_WARN, not ids, len(ids), 0,
        "Ingestion runs that failed in the last 7 days. A failed run leaves data "
        "unchanged rather than corrupted, but the data is then stale.",
        ids,
    )


def check_pending_review_backlog(session: Session) -> CheckOutcome:
    """Unreviewed ambiguous resolutions. Not an error — an unpaid debt."""
    count = session.scalar(
        select(func.count()).select_from(ReviewQueueItem).where(
            ReviewQueueItem.status == "pending"
        )
    ) or 0
    return CheckOutcome(
        "pending_review_backlog", SEVERITY_INFO, count < 100, count, 100,
        f"{count} entity-resolution decisions awaiting human review. Each one "
        f"represents a pair the system refused to merge on its own; a long backlog "
        f"means features may be split across provisional duplicates.",
    )


def check_molecules_without_evidence(session: Session) -> CheckOutcome:
    """Molecules with no facts attached at all, usually resolution artefacts."""
    # SQLAlchemy 2.0: union() is a function over selects, not a chainable method
    # on CompoundSelect.
    attached = union(
        select(DrugMasterFile.molecule_id).where(DrugMasterFile.molecule_id.is_not(None)),
        select(DrugProduct.molecule_id).where(DrugProduct.molecule_id.is_not(None)),
        select(ClinicalTrial.molecule_id).where(ClinicalTrial.molecule_id.is_not(None)),
        select(Event.molecule_id).where(Event.molecule_id.is_not(None)),
        select(Patent.molecule_id).where(Patent.molecule_id.is_not(None)),
        select(Exclusivity.molecule_id).where(Exclusivity.molecule_id.is_not(None)),
    ).subquery()
    stmt = select(Molecule.id).where(
        Molecule.id.not_in(select(attached.c[0]))
    )
    ids = _sample(session, stmt)
    count = session.scalar(
        select(func.count()).select_from(Molecule).where(
            Molecule.id.not_in(select(attached.c[0]))
        )
    ) or 0
    return CheckOutcome(
        "molecules_without_evidence", SEVERITY_WARN, count == 0, count, 0,
        "Molecules with no attached products, filings, trials, patents or DMFs. "
        "Usually an artefact of resolving a string that was not really a molecule.",
        ids,
    )


def check_opportunities_without_cited_evidence(session: Session) -> CheckOutcome:
    """An opportunity whose signals cite no evidence at all."""
    rows = session.execute(
        select(Opportunity.id)
        .join(OpportunitySignal, OpportunitySignal.opportunity_id == Opportunity.id)
        .group_by(Opportunity.id)
        .having(
            func.count(
                func.nullif(func.coalesce(func.array_length(
                    OpportunitySignal.evidence_ids, 1
                ), 0), 0)
            ) == 0
        )
    ).all()
    return CheckOutcome(
        "opportunities_without_cited_evidence", SEVERITY_WARN, not rows, len(rows), 0,
        "Opportunities none of whose signals cite an evidence row. The evidence "
        "drawer would be empty, so a reader could not check the claim.",
        [str(r[0]) for r in rows[:20]],
    )


ALL_CHECKS = (
    check_evidence_without_source_document,
    check_llm_evidence_without_audit,
    check_opportunity_without_signals,
    check_score_matches_signal_sum,
    check_capability_inference_without_basis,
    check_duplicate_molecules,
    check_duplicate_companies,
    check_ambiguous_aliases,
    check_orphan_type_two_dmfs,
    check_unresolved_company_literals,
    check_future_event_dates,
    check_implausible_dates,
    check_events_missing_publication,
    check_trial_phase_regression,
    check_source_freshness,
    check_failed_ingestion_runs,
    check_pending_review_backlog,
    check_molecules_without_evidence,
    check_opportunities_without_cited_evidence,
)


def run_all_checks(session: Session, *, persist: bool = True) -> list[CheckOutcome]:
    outcomes: list[CheckOutcome] = []
    for check in ALL_CHECKS:
        outcome = check(session)
        outcomes.append(outcome)
        if persist:
            session.add(DataQualityCheckResult(
                check_key=outcome.check_key,
                severity=outcome.severity,
                passed=outcome.passed,
                observed_count=outcome.observed_count,
                threshold=outcome.threshold,
                detail=outcome.detail,
                sample_ids=outcome.sample_ids or None,
            ))
    if persist:
        session.flush()
    return outcomes


def has_blocking_failures(outcomes: list[CheckOutcome]) -> bool:
    """True if any ERROR check failed. Used to gate publishing a refreshed feed."""
    return any(o.severity == SEVERITY_ERROR and not o.passed for o in outcomes)
