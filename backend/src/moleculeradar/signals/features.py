"""Deterministic feature computation.

Every feature here is a count, a date difference, or a ratio computed from
canonical rows under the temporal gate. No feature is produced by a model, and
no feature exists unless a Tier-1 source actually supplies the underlying field
(see docs/DATA_SOURCE_REGISTER.md section 6 for the source→entity mapping).

Each feature carries the evidence IDs that produced it, so a score can be walked
all the way back to source documents.

A feature whose inputs are absent is `None`, not zero. The distinction is
critical: "no API suppliers identified in our coverage" and "we have not
ingested DMF data" must not score the same, and the UI renders the latter as
DATA UNAVAILABLE rather than as an attractive opportunity.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, func, or_, select, union
from sqlalchemy.orm import Session

from ..core.enums import CapabilityType, EventType
from ..core.models import (
    ClinicalTrial,
    CompanyCapability,
    DrugMasterFile,
    Event,
    Evidence,
    Exclusivity,
    Molecule,
    Patent,
    RegulatoryApplication,
    SourceDocument,
)
from ..core.temporal import TemporalContext, months_between, visibility_clause


@dataclass(slots=True)
class FeatureSet:
    """Computed features for one molecule at one as-of date.

    `values` holds the numbers. `evidence` maps feature name → evidence IDs.
    `coverage` records which source families actually contributed, which is what
    lets the product distinguish a genuine negative from missing data.
    """

    molecule_id: uuid.UUID
    as_of_date: dt.date
    values: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, list[str]] = field(default_factory=dict)
    coverage: dict[str, bool] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        value = self.values.get(key, default)
        return default if value is None else value

    def is_missing(self, key: str) -> bool:
        return self.values.get(key) is None

    def evidence_for(self, *keys: str) -> list[str]:
        out: list[str] = []
        for key in keys:
            for eid in self.evidence.get(key, []):
                if eid not in out:
                    out.append(eid)
        return out


class FeatureComputer:
    """Computes the deterministic feature set for a molecule.

    All reads pass through `_gate`, which applies the temporal visibility clause.
    That is the only way this class touches dates, so leakage safety is a
    property of one method rather than of every query.
    """

    def __init__(self, session: Session, ctx: TemporalContext) -> None:
        self.session = session
        self.ctx = ctx
        self.as_of = ctx.effective_date

    # -- gating ------------------------------------------------------------

    def _gate(self, stmt: Select, model: Any) -> Select:
        clause = visibility_clause(model, self.ctx)
        return stmt if clause is None else stmt.where(clause)

    # -- public ------------------------------------------------------------

    def compute(self, molecule_id: uuid.UUID, focus_company_ids: set[uuid.UUID]) -> FeatureSet:
        fs = FeatureSet(molecule_id=molecule_id, as_of_date=self.as_of)
        self._exclusivity_and_patent(molecule_id, fs)
        self._api_supply(molecule_id, focus_company_ids, fs)
        self._regulatory_activity(molecule_id, fs)
        self._clinical_activity(molecule_id, fs)
        self._supply_events(molecule_id, fs)
        self._molecule_character(molecule_id, fs)
        self._focus_capability(molecule_id, focus_company_ids, fs)
        self._evidence_quality(molecule_id, fs)
        return fs

    # -- feature families --------------------------------------------------

    def _exclusivity_and_patent(self, molecule_id: uuid.UUID, fs: FeatureSet) -> None:
        """Runway to the next loss-of-exclusivity event.

        Source: Orange Book patent.txt and exclusivity.txt. Only *future* expiries
        relative to the as-of date are considered, because an expiry already past
        is not a forthcoming event.
        """
        excl_stmt = self._gate(
            select(Exclusivity.expiry_date, Exclusivity.evidence_id, Exclusivity.exclusivity_code)
            .where(
                Exclusivity.molecule_id == molecule_id,
                Exclusivity.expiry_date.is_not(None),
                Exclusivity.expiry_date > self.as_of,
            ),
            Exclusivity,
        )
        excl_rows = self.session.execute(excl_stmt).all()

        pat_stmt = self._gate(
            select(Patent.expiry_date, Patent.evidence_id, Patent.drug_substance_flag)
            .where(
                Patent.molecule_id == molecule_id,
                Patent.expiry_date.is_not(None),
                Patent.expiry_date > self.as_of,
                # A patent the applicant has asked to delist is not a live barrier.
                or_(Patent.delist_requested.is_(None), Patent.delist_requested.is_(False)),
            ),
            Patent,
        )
        pat_rows = self.session.execute(pat_stmt).all()

        # Coverage: did we see any Orange Book exclusivity/patent data for this
        # molecule at all? Absence of future expiries is meaningful only if we did.
        any_ob = self.session.scalar(
            self._gate(
                select(func.count()).select_from(Exclusivity)
                .where(Exclusivity.molecule_id == molecule_id),
                Exclusivity,
            )
        ) or 0
        any_ob += self.session.scalar(
            self._gate(
                select(func.count()).select_from(Patent)
                .where(Patent.molecule_id == molecule_id),
                Patent,
            )
        ) or 0
        fs.coverage["orange_book"] = any_ob > 0

        if excl_rows:
            nearest = min(r[0] for r in excl_rows)
            fs.values["months_to_next_exclusivity_expiry"] = months_between(self.as_of, nearest)
            fs.values["next_exclusivity_expiry_date"] = nearest.isoformat()
            fs.values["future_exclusivity_count"] = len(excl_rows)
            fs.evidence["months_to_next_exclusivity_expiry"] = [
                str(r[1]) for r in excl_rows if r[1] and r[0] == nearest
            ]
        else:
            fs.values["months_to_next_exclusivity_expiry"] = None
            fs.values["future_exclusivity_count"] = 0 if fs.coverage["orange_book"] else None

        if pat_rows:
            nearest_pat = min(r[0] for r in pat_rows)
            fs.values["months_to_next_patent_expiry"] = months_between(self.as_of, nearest_pat)
            fs.values["next_patent_expiry_date"] = nearest_pat.isoformat()
            fs.values["future_patent_count"] = len(pat_rows)
            # The last barrier to fall is what actually gates generic entry.
            latest_pat = max(r[0] for r in pat_rows)
            fs.values["months_to_last_patent_expiry"] = months_between(self.as_of, latest_pat)
            fs.evidence["months_to_next_patent_expiry"] = [
                str(r[1]) for r in pat_rows if r[1] and r[0] == nearest_pat
            ]
            fs.values["drug_substance_patent_count"] = sum(1 for r in pat_rows if r[2])
        else:
            fs.values["months_to_next_patent_expiry"] = None
            fs.values["months_to_last_patent_expiry"] = None
            fs.values["future_patent_count"] = 0 if fs.coverage["orange_book"] else None
            fs.values["drug_substance_patent_count"] = None

        # The single actionable runway figure: the earlier of the two is when the
        # first barrier lifts; we report both and the minimum.
        candidates = [
            v for v in (
                fs.values.get("months_to_next_exclusivity_expiry"),
                fs.values.get("months_to_next_patent_expiry"),
            ) if v is not None
        ]
        fs.values["months_to_next_loe_event"] = min(candidates) if candidates else None

    def _api_supply(
        self, molecule_id: uuid.UUID, focus_company_ids: set[uuid.UUID], fs: FeatureSet
    ) -> None:
        """Observed API supplier concentration, from the FDA DMF list.

        Counts *distinct holders*, not DMFs: one company may hold several DMFs for
        the same API. Type II DMFs only — those are the drug-substance filings.
        """
        base = select(DrugMasterFile.holder_company_id, DrugMasterFile.evidence_id,
                      DrugMasterFile.available_for_reference, DrugMasterFile.status).where(
            DrugMasterFile.molecule_id == molecule_id,
            DrugMasterFile.dmf_type == "2",
        )
        rows = self.session.execute(self._gate(base, DrugMasterFile)).all()

        any_dmf_data = self.session.scalar(
            self._gate(select(func.count()).select_from(DrugMasterFile), DrugMasterFile)
        ) or 0
        fs.coverage["dmf"] = any_dmf_data > 0

        if not fs.coverage["dmf"]:
            # No DMF data ingested at all: every DMF-derived feature is unknown.
            fs.values["observed_api_supplier_count"] = None
            fs.values["observed_api_supplier_count_reference_ready"] = None
            fs.values["focus_company_holds_api_dmf"] = None
            return

        active = [r for r in rows if (r[3] or "").upper() == "A"]
        holders = {r[0] for r in active if r[0] is not None}
        ref_ready = {r[0] for r in active if r[0] is not None and r[2]}

        fs.values["observed_api_supplier_count"] = len(holders)
        fs.values["observed_api_supplier_count_reference_ready"] = len(ref_ready)
        fs.values["observed_api_dmf_count"] = len(active)
        fs.evidence["observed_api_supplier_count"] = [str(r[1]) for r in active if r[1]]

        focus_holders = holders & focus_company_ids
        fs.values["focus_company_holds_api_dmf"] = bool(focus_holders)
        fs.values["focus_company_api_dmf_reference_ready"] = bool(ref_ready & focus_company_ids)
        if focus_holders:
            fs.evidence["focus_company_holds_api_dmf"] = [
                str(r[1]) for r in active if r[0] in focus_company_ids and r[1]
            ]
        # Suppliers other than the focus company — the competitive field DRL faces.
        fs.values["observed_competing_api_supplier_count"] = len(holders - focus_company_ids)

    def _regulatory_activity(self, molecule_id: uuid.UUID, fs: FeatureSet) -> None:
        """Filing and approval momentum, from Drugs@FDA submissions and events."""
        window_start = self.as_of - dt.timedelta(days=365)
        prev_start = self.as_of - dt.timedelta(days=730)

        def count_events(types: tuple[EventType, ...], start: dt.date, end: dt.date) -> int:
            stmt = select(func.count()).select_from(Event).where(
                Event.molecule_id == molecule_id,
                Event.event_type.in_(types),
                Event.event_date >= start,
                Event.event_date < end,
            )
            return self.session.scalar(self._gate(stmt, Event)) or 0

        any_events = self.session.scalar(
            self._gate(
                select(func.count()).select_from(Event)
                .where(Event.molecule_id == molecule_id),
                Event,
            )
        ) or 0
        fs.coverage["events"] = any_events > 0

        filings = (EventType.NEW_FILING,)
        approvals = (EventType.NEW_APPROVAL,)

        fs.values["filing_count_12m"] = count_events(filings, window_start, self.as_of)
        fs.values["filing_count_prev_12m"] = count_events(filings, prev_start, window_start)
        fs.values["approval_count_12m"] = count_events(approvals, window_start, self.as_of)
        fs.values["approval_count_prev_12m"] = count_events(approvals, prev_start, window_start)
        fs.values["regulatory_event_count_12m"] = (
            fs.values["filing_count_12m"] + fs.values["approval_count_12m"]
        )

        # Distinct ANDA applicants = the generic developer field.
        anda_stmt = select(func.count(func.distinct(RegulatoryApplication.applicant_company_id))) \
            .select_from(RegulatoryApplication) \
            .join(Event, Event.subject_entity_id == RegulatoryApplication.id) \
            .where(
                Event.molecule_id == molecule_id,
                RegulatoryApplication.application_type == "ANDA",
            )
        fs.values["distinct_anda_applicant_count"] = (
            self.session.scalar(self._gate(anda_stmt, Event)) or 0
        ) if fs.coverage["events"] else None

        evid_stmt = select(Event.evidence_id).where(
            Event.molecule_id == molecule_id,
            Event.event_type.in_(filings + approvals),
            Event.event_date >= window_start,
        ).limit(25)
        fs.evidence["regulatory_event_count_12m"] = [
            str(e) for (e,) in self.session.execute(self._gate(evid_stmt, Event)).all() if e
        ]

    def _clinical_activity(self, molecule_id: uuid.UUID, fs: FeatureSet) -> None:
        """Development activity and developer breadth, from ClinicalTrials.gov."""
        window_start = self.as_of - dt.timedelta(days=365)
        prev_start = self.as_of - dt.timedelta(days=730)

        any_trials = self.session.scalar(
            self._gate(select(func.count()).select_from(ClinicalTrial), ClinicalTrial)
        ) or 0
        fs.coverage["clinical_trials"] = any_trials > 0

        if not fs.coverage["clinical_trials"]:
            for key in (
                "trial_count_total", "trial_starts_12m", "trial_starts_prev_12m",
                "trial_growth_12m", "distinct_sponsor_count", "active_trial_count",
                "late_phase_trial_count", "terminated_trial_count_24m", "trial_country_count",
            ):
                fs.values[key] = None
            return

        rows = self.session.execute(
            self._gate(
                select(
                    ClinicalTrial.id, ClinicalTrial.sponsor_company_id, ClinicalTrial.phase,
                    ClinicalTrial.overall_status, ClinicalTrial.start_date,
                    ClinicalTrial.evidence_id, ClinicalTrial.location_country_codes,
                ).where(ClinicalTrial.molecule_id == molecule_id),
                ClinicalTrial,
            )
        ).all()

        fs.values["trial_count_total"] = len(rows)
        starts_12m = [r for r in rows if r[4] and window_start <= r[4] < self.as_of]
        starts_prev = [r for r in rows if r[4] and prev_start <= r[4] < window_start]
        fs.values["trial_starts_12m"] = len(starts_12m)
        fs.values["trial_starts_prev_12m"] = len(starts_prev)

        # Growth is only meaningful against a non-trivial base. With a prior-year
        # base of zero, a single new trial is not "infinite growth" — we report
        # None and let the signal treat it as absent rather than spectacular.
        if len(starts_prev) >= 2:
            fs.values["trial_growth_12m"] = round(
                (len(starts_12m) - len(starts_prev)) / len(starts_prev), 3
            )
        else:
            fs.values["trial_growth_12m"] = None

        active_statuses = {"RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION",
                           "NOT_YET_RECRUITING", "AVAILABLE"}
        fs.values["active_trial_count"] = sum(
            1 for r in rows if (r[3] or "").upper().replace(" ", "_") in active_statuses
        )
        fs.values["late_phase_trial_count"] = sum(
            1 for r in rows
            if r[2] and any(
                ph in r[2].upper() for ph in ("PHASE3", "PHASE 3", "PHASE4", "PHASE 4")
            )
        )
        fs.values["terminated_trial_count_24m"] = sum(
            1 for r in rows
            if (r[3] or "").upper() in {"TERMINATED", "WITHDRAWN", "SUSPENDED"}
            and r[4] and r[4] >= prev_start
        )
        fs.values["distinct_sponsor_count"] = len({r[1] for r in rows if r[1]})

        countries: set[str] = set()
        for r in rows:
            for code in (r[6] or []):
                countries.add(code)
        fs.values["trial_country_count"] = len(countries)
        fs.values["trial_countries"] = sorted(countries)[:30]
        fs.evidence["trial_starts_12m"] = [str(r[5]) for r in starts_12m if r[5]]
        fs.evidence["distinct_sponsor_count"] = [str(r[5]) for r in rows if r[5]][:25]

    def _supply_events(self, molecule_id: uuid.UUID, fs: FeatureSet) -> None:
        """Shortage history, from openFDA drug shortages."""
        window_start = self.as_of - dt.timedelta(days=365 * 3)

        shortage_types = (EventType.SHORTAGE_OPENED, EventType.SHORTAGE_RESOLVED)
        any_shortage_data = self.session.scalar(
            self._gate(
                select(func.count()).select_from(Event)
                .where(Event.event_type.in_(shortage_types)),
                Event,
            )
        ) or 0
        fs.coverage["shortages"] = any_shortage_data > 0

        if not fs.coverage["shortages"]:
            fs.values["shortage_events_36m"] = None
            fs.values["shortage_open_now"] = None
            return

        rows = self.session.execute(
            self._gate(
                select(Event.event_type, Event.event_date, Event.evidence_id).where(
                    Event.molecule_id == molecule_id,
                    Event.event_type.in_(shortage_types),
                    Event.event_date >= window_start,
                ).order_by(Event.event_date),
                Event,
            )
        ).all()

        opened = [r for r in rows if r[0] == EventType.SHORTAGE_OPENED]
        resolved = [r for r in rows if r[0] == EventType.SHORTAGE_RESOLVED]
        fs.values["shortage_events_36m"] = len(opened)
        fs.values["shortage_open_now"] = len(opened) > len(resolved)
        fs.evidence["shortage_events_36m"] = [str(r[2]) for r in rows if r[2]]

    def _molecule_character(self, molecule_id: uuid.UUID, fs: FeatureSet) -> None:
        """Technical character of the molecule: peptide, complex chemistry, class."""
        mol = self.session.get(Molecule, molecule_id)
        if mol is None:
            fs.values["is_peptide"] = None
            fs.values["molecule_class"] = None
            fs.values["complexity_flag_count"] = None
            return
        fs.values["is_peptide"] = bool(mol.is_peptide)
        fs.values["molecule_class"] = str(mol.molecule_class)
        flags = mol.complexity_flags or {}
        # Count only flags that are asserted true and carry evidence.
        asserted = [
            k for k, v in flags.items()
            if isinstance(v, dict) and v.get("value") and v.get("evidence_id")
        ]
        fs.values["complexity_flag_count"] = len(asserted)
        fs.values["complexity_flags"] = asserted

    def _focus_capability(
        self, molecule_id: uuid.UUID, focus_company_ids: set[uuid.UUID], fs: FeatureSet
    ) -> None:
        """Overlap between this molecule and the focus company's public capability.

        Two distinct things, kept separate because they drive different
        opportunity types:

        * `focus_company_holds_api_dmf` (computed in `_api_supply`) — direct, and
          is what makes a SELL opportunity possible at all.
        * `focus_capability_adjacency_score` — the focus company does *not* hold a
          DMF here, but holds API DMFs for molecules in the same therapeutic area
          and demonstrably handles this molecule class. That is the honest basis
          for a BUILD suggestion, and it is an INFERENCE, never a FACT.
        """
        if not focus_company_ids:
            fs.values["focus_capability_adjacency_score"] = None
            fs.values["focus_company_capability_count"] = None
            fs.coverage["focus_capability"] = False
            return

        caps = self.session.execute(
            self._gate(
                select(
                    CompanyCapability.capability_type, CompanyCapability.molecule_id,
                    CompanyCapability.assertion_level, CompanyCapability.evidence_id,
                    CompanyCapability.value_text,
                ).where(CompanyCapability.company_id.in_(focus_company_ids)),
                CompanyCapability,
            )
        ).all()
        fs.coverage["focus_capability"] = len(caps) > 0
        fs.values["focus_company_capability_count"] = len(caps)

        if not caps:
            fs.values["focus_capability_adjacency_score"] = None
            return

        mol = self.session.get(Molecule, molecule_id)
        ta_id = mol.therapeutic_area_id if mol else None

        # Molecules in the same therapeutic area for which the focus company holds
        # an API-supply capability.
        same_ta_api = 0
        if ta_id is not None:
            cap_mol_ids = [c[1] for c in caps if c[0] == CapabilityType.API_SUPPLY and c[1]]
            if cap_mol_ids:
                same_ta_api = self.session.scalar(
                    select(func.count()).select_from(Molecule).where(
                        Molecule.id.in_(cap_mol_ids),
                        Molecule.therapeutic_area_id == ta_id,
                    )
                ) or 0

        # Does the focus company demonstrably handle this molecule class?
        class_match = False
        if fs.values.get("is_peptide"):
            class_match = any(
                c[0] in (CapabilityType.PEPTIDE_SYNTHESIS,) for c in caps
            )
        else:
            class_match = any(c[0] == CapabilityType.API_SUPPLY for c in caps)

        # Bounded 0..1, from two corroborating components. Deliberately coarse:
        # a finer score would imply precision the inputs do not support.
        ta_component = min(same_ta_api / 5.0, 1.0) * 0.6
        class_component = 0.4 if class_match else 0.0
        fs.values["focus_capability_adjacency_score"] = round(ta_component + class_component, 3)
        fs.values["focus_same_ta_api_capability_count"] = same_ta_api
        fs.values["focus_class_capability_match"] = class_match
        fs.evidence["focus_capability_adjacency_score"] = [
            str(c[3]) for c in caps if c[3]
        ][:25]

    def _evidence_quality(self, molecule_id: uuid.UUID, fs: FeatureSet) -> None:
        """How well-evidenced this molecule is. Feeds evidence confidence, never score.

        Kept rigorously separate from opportunity scoring: a molecule can be a
        strong opportunity on thin evidence, and the product must say so rather
        than quietly discounting it into the middle.

        Every contributing sub-select is temporally gated, so a historical run
        reports the evidence base as it stood then. Ungated counts here would let
        a backtest claim confidence it did not have.
        """
        # Each fact-bearing table contributes the evidence rows it points at.
        parts = [
            self._gate(
                select(Event.evidence_id).where(Event.molecule_id == molecule_id), Event
            ),
            self._gate(
                select(DrugMasterFile.evidence_id).where(
                    DrugMasterFile.molecule_id == molecule_id
                ),
                DrugMasterFile,
            ),
            self._gate(
                select(ClinicalTrial.evidence_id).where(
                    ClinicalTrial.molecule_id == molecule_id
                ),
                ClinicalTrial,
            ),
            self._gate(
                select(Patent.evidence_id).where(Patent.molecule_id == molecule_id), Patent
            ),
            self._gate(
                select(Exclusivity.evidence_id).where(
                    Exclusivity.molecule_id == molecule_id
                ),
                Exclusivity,
            ),
        ]
        # SQLAlchemy 2.0: union() is a function over selects; CompoundSelect has no
        # .union() method to chain.
        evidence_ids = union(*parts).subquery()

        row = self.session.execute(
            select(
                func.count(func.distinct(SourceDocument.source_id)),
                func.count(func.distinct(SourceDocument.id)),
                func.max(SourceDocument.retrieved_at),
            )
            .select_from(Evidence)
            .join(SourceDocument, SourceDocument.id == Evidence.source_document_id)
            .where(Evidence.id.in_(select(evidence_ids.c[0])))
        ).one_or_none()

        if row is None or not row[1]:
            fs.values["evidence_source_count"] = 0
            fs.values["evidence_document_count"] = 0
            fs.values["evidence_recency_days"] = None
            return

        fs.values["evidence_source_count"] = int(row[0] or 0)
        fs.values["evidence_document_count"] = int(row[1] or 0)
        latest = row[2]
        if latest is not None:
            latest_date = latest.date() if isinstance(latest, dt.datetime) else latest
            fs.values["evidence_recency_days"] = (self.as_of - latest_date).days
        else:
            fs.values["evidence_recency_days"] = None
