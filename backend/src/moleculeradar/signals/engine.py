"""Opportunity engine orchestration.

Takes a temporal context and produces persisted, decomposed opportunities.

The run is idempotent for a given `(molecule, as_of_date, engine_version)`:
re-running replaces the previous result rather than accumulating duplicates, so
a failed or repeated run cannot inflate the feed.

Nothing here decides anything. It computes features, applies the declared
signals, computes evidence confidence, asks the skeptic for counter-arguments,
and writes the result down with its decomposition. Every number is traceable to
a feature and every feature to evidence.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import ENGINE_VERSION
from ..core.config import get_settings
from ..core.enums import EventType, OpportunityType
from ..core.models import (
    ClinicalTrial,
    Company,
    CounterThesis,
    DrugMasterFile,
    Event,
    FeatureSnapshot,
    Molecule,
    Opportunity,
    OpportunitySignal,
    ReviewQueueItem,
)
from ..core.temporal import TemporalContext, visibility_clause
from .features import FeatureComputer, FeatureSet
from .scoring import ScoredOpportunity, score_opportunity
from .skeptic import generate_counter_thesis

# A SELL candidate must clear this before we are willing to name a company at all.
# Naming a company on a weak signal is the most reputationally dangerous thing
# this product can do, so the bar is deliberately higher than for BUILD.
SELL_MIN_SCORE = 30.0
BUILD_MIN_SCORE = 20.0


@dataclass(slots=True)
class EngineRunSummary:
    as_of_date: dt.date
    engine_version: str
    molecules_evaluated: int = 0
    build_opportunities: int = 0
    sell_opportunities: int = 0
    counter_theses: int = 0
    below_threshold: int = 0
    notes: list[str] = field(default_factory=list)


class OpportunityEngine:
    def __init__(self, session: Session, ctx: TemporalContext) -> None:
        self.session = session
        self.ctx = ctx
        self.settings = get_settings()
        self.features = FeatureComputer(session, ctx)
        self.as_of = ctx.effective_date
        self._focus_ids: set[uuid.UUID] | None = None

    # -- focus company -----------------------------------------------------

    @property
    def focus_company_ids(self) -> set[uuid.UUID]:
        """All company rows belonging to the focus company group.

        Includes subsidiaries, since a DMF held by a US subsidiary is still the
        group's capability. Resolved once per run.
        """
        if self._focus_ids is not None:
            return self._focus_ids

        direct = set(
            self.session.scalars(
                select(Company.id).where(Company.is_focus_company.is_(True))
            ).all()
        )
        if direct:
            children = set(
                self.session.scalars(
                    select(Company.id).where(Company.parent_company_id.in_(direct))
                ).all()
            )
            direct |= children
        self._focus_ids = direct
        return direct

    # -- public ------------------------------------------------------------

    def run(self, molecule_ids: list[uuid.UUID] | None = None) -> EngineRunSummary:
        summary = EngineRunSummary(as_of_date=self.as_of, engine_version=ENGINE_VERSION)

        if not self.focus_company_ids:
            summary.notes.append(
                "No focus company identified in the database. SELL opportunities "
                "require public evidence that the focus company supplies an API, so "
                "none can be generated. Run `moleculeradar focus bootstrap` after "
                "ingesting DMF data."
            )

        if molecule_ids is None:
            molecule_ids = list(self.session.scalars(select(Molecule.id)).all())

        for molecule_id in molecule_ids:
            summary.molecules_evaluated += 1
            fs = self.features.compute(molecule_id, self.focus_company_ids)
            snapshot = self._persist_snapshot(fs)
            molecule = self.session.get(Molecule, molecule_id)
            if molecule is None:
                continue

            context = {
                "focus_company_name": self.settings.focus_company_name,
                "as_of_date": self.as_of.isoformat(),
            }

            # --- BUILD ---------------------------------------------------
            # A molecule the focus company already supplies is not a BUILD
            # candidate: BUILD asks what is *missing* from the portfolio.
            already_supplied = fs.values.get("focus_company_holds_api_dmf")
            if not already_supplied:
                scored = score_opportunity(
                    fs, OpportunityType.BUILD, molecule.preferred_name, context,
                    resolution_warnings=self._resolution_warnings(molecule_id),
                )
                if scored.opportunity_score >= BUILD_MIN_SCORE:
                    self._persist(molecule, None, snapshot, scored, fs, context)
                    summary.build_opportunities += 1
                    summary.counter_theses += self._count_concerns(
                        fs, OpportunityType.BUILD, context
                    )
                else:
                    summary.below_threshold += 1

            # --- SELL ----------------------------------------------------
            if already_supplied:
                for counterparty in self._sell_candidates(molecule_id):
                    cp_context = dict(context)
                    cp_context["counterparty"] = counterparty
                    scored = score_opportunity(
                        fs, OpportunityType.SELL, molecule.preferred_name, cp_context,
                        counterparty_name=counterparty["company_name"],
                        resolution_warnings=self._resolution_warnings(molecule_id),
                    )
                    if scored.opportunity_score < SELL_MIN_SCORE:
                        summary.below_threshold += 1
                        continue
                    company = self.session.get(
                        Company, uuid.UUID(counterparty["company_id"])
                    )
                    self._persist(molecule, company, snapshot, scored, fs, cp_context)
                    summary.sell_opportunities += 1
                    summary.counter_theses += self._count_concerns(
                        fs, OpportunityType.SELL, cp_context
                    )

        return summary

    # -- SELL candidate discovery ------------------------------------------

    def _sell_candidates(self, molecule_id: uuid.UUID) -> list[dict]:
        """Companies showing public activity on this molecule, excluding the focus group.

        A candidate is any company with a dated filing event or a registered trial
        involving the molecule. We compute its own API-filing status so the
        scoring layer can express whether external sourcing is even plausible.
        """
        window_start = self.as_of - dt.timedelta(days=730)

        ev_clause = visibility_clause(Event, self.ctx)
        stmt = (
            select(
                Event.company_id,
                func.count().label("filing_count"),
                func.max(Event.event_date).label("latest"),
            )
            .where(
                Event.molecule_id == molecule_id,
                Event.company_id.is_not(None),
                Event.event_type.in_((EventType.NEW_FILING, EventType.NEW_APPROVAL)),
                Event.event_date >= window_start,
            )
            .group_by(Event.company_id)
        )
        if ev_clause is not None:
            stmt = stmt.where(ev_clause)
        filing_rows = {r[0]: (r[1], r[2]) for r in self.session.execute(stmt).all()}

        tr_clause = visibility_clause(ClinicalTrial, self.ctx)
        tstmt = (
            select(
                ClinicalTrial.sponsor_company_id,
                func.count().label("trial_count"),
                func.max(ClinicalTrial.start_date).label("latest"),
            )
            .where(
                ClinicalTrial.molecule_id == molecule_id,
                ClinicalTrial.sponsor_company_id.is_not(None),
            )
            .group_by(ClinicalTrial.sponsor_company_id)
        )
        if tr_clause is not None:
            tstmt = tstmt.where(tr_clause)
        trial_rows = {r[0]: (r[1], r[2]) for r in self.session.execute(tstmt).all()}

        candidate_ids = (set(filing_rows) | set(trial_rows)) - self.focus_company_ids

        # Which of them hold their own active Type II DMF for this API?
        dmf_clause = visibility_clause(DrugMasterFile, self.ctx)
        dstmt = select(DrugMasterFile.holder_company_id).where(
            DrugMasterFile.molecule_id == molecule_id,
            DrugMasterFile.dmf_type == "2",
            DrugMasterFile.status == "A",
        )
        if dmf_clause is not None:
            dstmt = dstmt.where(dmf_clause)
        own_dmf_holders = set(self.session.scalars(dstmt).all())

        out: list[dict] = []
        for cid in candidate_ids:
            company = self.session.get(Company, cid)
            if company is None:
                continue
            filings, filing_latest = filing_rows.get(cid, (0, None))
            trials, trial_latest = trial_rows.get(cid, (0, None))
            latest_dates = [d for d in (filing_latest, trial_latest) if d is not None]
            latest = max(latest_dates) if latest_dates else None
            out.append({
                # Stringified: this dict is persisted verbatim into a JSONB column
                # (opportunity_signal.feature_values), and a raw UUID is not JSON
                # serialisable. Callers convert back when they need the entity.
                "company_id": str(cid),
                "company_name": company.canonical_name,
                "filing_count_24m": filings,
                "trial_count": trials,
                "holds_api_dmf": cid in own_dmf_holders,
                "days_since_latest_event": (self.as_of - latest).days if latest else None,
            })
        # Deterministic ordering so repeated runs produce identical output.
        out.sort(key=lambda c: (-c["filing_count_24m"], -c["trial_count"], c["company_name"]))
        return out

    # -- persistence -------------------------------------------------------

    def _persist_snapshot(self, fs: FeatureSet) -> FeatureSnapshot:
        existing = self.session.scalar(
            select(FeatureSnapshot).where(
                FeatureSnapshot.molecule_id == fs.molecule_id,
                FeatureSnapshot.as_of_date == fs.as_of_date,
                FeatureSnapshot.engine_version == ENGINE_VERSION,
            )
        )
        payload = dict(fs.values)
        payload["_coverage"] = fs.coverage
        if existing is not None:
            existing.features = payload
            existing.feature_evidence = fs.evidence
            existing.visibility_mode = self.ctx.mode.value
            self.session.flush()
            return existing
        snapshot = FeatureSnapshot(
            molecule_id=fs.molecule_id,
            as_of_date=fs.as_of_date,
            engine_version=ENGINE_VERSION,
            visibility_mode=self.ctx.mode.value,
            features=payload,
            feature_evidence=fs.evidence,
        )
        self.session.add(snapshot)
        self.session.flush()
        return snapshot

    def _persist(
        self,
        molecule: Molecule,
        counterparty: Company | None,
        snapshot: FeatureSnapshot,
        scored: ScoredOpportunity,
        fs: FeatureSet,
        context: dict,
    ) -> Opportunity:
        dedupe = (
            f"opp:{scored.opportunity_type.value}:{molecule.id}:"
            f"{counterparty.id if counterparty else 'none'}:{self.as_of}:{ENGINE_VERSION}"
        )
        # Idempotent: replace any previous result for this exact key.
        prior = self.session.scalar(select(Opportunity).where(Opportunity.dedupe_key == dedupe))
        if prior is not None:
            self.session.execute(
                delete(OpportunitySignal).where(OpportunitySignal.opportunity_id == prior.id)
            )
            self.session.execute(
                delete(CounterThesis).where(CounterThesis.opportunity_id == prior.id)
            )
            self.session.delete(prior)
            self.session.flush()

        opp = Opportunity(
            opportunity_type=scored.opportunity_type,
            molecule_id=molecule.id,
            counterparty_company_id=counterparty.id if counterparty else None,
            as_of_date=self.as_of,
            opportunity_score=scored.opportunity_score,
            evidence_confidence=scored.evidence_confidence,
            evidence_confidence_score=scored.evidence_confidence_score,
            priority_band=scored.priority_band,
            headline=scored.headline,
            feature_snapshot_id=snapshot.id,
            engine_version=ENGINE_VERSION,
            dedupe_key=dedupe,
        )
        self.session.add(opp)
        self.session.flush()

        for sig in scored.signals:
            self.session.add(OpportunitySignal(
                opportunity_id=opp.id,
                signal_key=sig.signal_key,
                direction=sig.direction,
                points=sig.points,
                max_points=sig.max_points,
                explanation=sig.explanation,
                feature_values=sig.feature_values,
                evidence_ids=fs.evidence_for(*sig.evidence_keys) or None,
            ))

        for concern in generate_counter_thesis(fs, scored.opportunity_type, context):
            self.session.add(CounterThesis(
                opportunity_id=opp.id,
                concern_key=concern.concern_key,
                severity=concern.severity,
                explanation=concern.explanation,
                feature_values=concern.feature_values,
                evidence_ids=fs.evidence_for(*concern.evidence_keys) or None,
            ))

        self.session.flush()
        return opp

    # -- helpers -----------------------------------------------------------

    def _count_concerns(
        self, fs: FeatureSet, opportunity_type: OpportunityType, context: dict
    ) -> int:
        return len(generate_counter_thesis(fs, opportunity_type, context))

    def _resolution_warnings(self, molecule_id: uuid.UUID) -> int:
        """Pending review items touching THIS molecule lower its evidence confidence.

        If we are not sure the entities behind a molecule are right, we should not
        be confident about conclusions drawn from them.

        Scoped to the molecule and to the companies linked to it. A global count
        would let one ambiguous entity anywhere depress confidence everywhere,
        which would be both wrong and untraceable for the analyst reading it.
        """
        linked_company_ids = set(
            self.session.scalars(
                select(DrugMasterFile.holder_company_id).where(
                    DrugMasterFile.molecule_id == molecule_id,
                    DrugMasterFile.holder_company_id.is_not(None),
                )
            ).all()
        )
        linked_company_ids |= set(
            self.session.scalars(
                select(ClinicalTrial.sponsor_company_id).where(
                    ClinicalTrial.molecule_id == molecule_id,
                    ClinicalTrial.sponsor_company_id.is_not(None),
                )
            ).all()
        )
        linked_company_ids |= set(
            self.session.scalars(
                select(Event.company_id).where(
                    Event.molecule_id == molecule_id,
                    Event.company_id.is_not(None),
                )
            ).all()
        )

        relevant_ids = {str(molecule_id)} | {str(cid) for cid in linked_company_ids}

        pending = self.session.execute(
            select(
                ReviewQueueItem.candidate_payload["candidate_entity_id"].astext,
                ReviewQueueItem.candidate_payload["provisional_entity_id"].astext,
            ).where(ReviewQueueItem.status == "pending")
        ).all()

        return sum(
            1 for candidate, provisional in pending
            if (candidate in relevant_ids) or (provisional in relevant_ids)
        )
