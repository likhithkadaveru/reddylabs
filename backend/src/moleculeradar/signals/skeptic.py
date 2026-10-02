"""The skeptic engine.

Every high-priority opportunity receives an automatic counter-analysis. The point
is structural: an opportunity engine with no adversary becomes a hype machine,
and a hype machine is worthless to someone making capital allocation decisions.

Every concern here is derived from the same deterministic features as the
supporting signals — the skeptic reads the same evidence, looking for reasons the
thesis fails. It is not an LLM asked to be negative, which would produce
plausible-sounding objections unconnected to our data.

A counter-thesis never alters the opportunity score. It is displayed beside it.
Netting them off would hide exactly the disagreement an analyst needs to see.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.enums import OpportunityType
from .features import FeatureSet


@dataclass(frozen=True, slots=True)
class Concern:
    concern_key: str
    severity: str  # HIGH | MEDIUM | LOW
    explanation: str
    feature_values: dict[str, Any]
    evidence_keys: tuple[str, ...] = ()


def generate_counter_thesis(
    fs: FeatureSet,
    opportunity_type: OpportunityType,
    context: dict[str, Any],
) -> list[Concern]:
    """Reasons this might not be an opportunity, drawn from structured evidence."""
    concerns: list[Concern] = []

    # --- Competition already crowded -------------------------------------
    suppliers = fs.values.get("observed_competing_api_supplier_count")
    if suppliers is not None and suppliers >= 5:
        concerns.append(Concern(
            concern_key="crowded_api_supply",
            severity="HIGH" if suppliers >= 8 else "MEDIUM",
            explanation=(
                f"{suppliers} competing API suppliers identified in current public-source "
                f"coverage. A field this size usually implies price competition that "
                f"erodes the return on a new entrant."
            ),
            feature_values={"observed_competing_api_supplier_count": suppliers},
            evidence_keys=("observed_api_supplier_count",),
        ))

    applicants = fs.values.get("distinct_anda_applicant_count")
    if applicants is not None and applicants >= 8:
        concerns.append(Concern(
            concern_key="crowded_generic_field",
            severity="HIGH" if applicants >= 12 else "MEDIUM",
            explanation=(
                f"{applicants} distinct ANDA applicants identified for this molecule. "
                f"Late entry into a field this crowded has historically produced thin margins."
            ),
            feature_values={"distinct_anda_applicant_count": applicants},
        ))

    # --- Development is fading, not growing -------------------------------
    growth = fs.values.get("trial_growth_12m")
    if growth is not None and growth < -0.25:
        concerns.append(Concern(
            concern_key="declining_clinical_activity",
            severity="MEDIUM",
            explanation=(
                f"Registered trial starts fell {abs(growth) * 100:.0f}% versus the "
                f"preceding 12 months, which runs against a growth thesis."
            ),
            feature_values={"trial_growth_12m": growth},
            evidence_keys=("trial_starts_12m",),
        ))

    terminated = fs.values.get("terminated_trial_count_24m")
    if terminated is not None and terminated >= 2:
        concerns.append(Concern(
            concern_key="trial_failures",
            severity="HIGH" if terminated >= 4 else "MEDIUM",
            explanation=(
                f"{terminated} trial(s) recorded as terminated, withdrawn or suspended "
                f"in the 24 months to the as-of date. Molecule Radar does not hold the "
                f"reasons; the underlying registry records should be read before relying "
                f"on this molecule's development trajectory."
            ),
            feature_values={"terminated_trial_count_24m": terminated},
            evidence_keys=("trial_starts_12m",),
        ))

    # --- Patent barriers -------------------------------------------------
    last_patent = fs.values.get("months_to_last_patent_expiry")
    if last_patent is not None and last_patent > 84:
        concerns.append(Concern(
            concern_key="long_patent_runway",
            severity="HIGH",
            explanation=(
                f"The latest-expiring listed patent identified runs approximately "
                f"{last_patent} months beyond the as-of date. Orange Book listing does "
                f"not establish enforceability or scope, but a runway this long usually "
                f"delays any generic entry."
            ),
            feature_values={"months_to_last_patent_expiry": last_patent},
            evidence_keys=("months_to_next_patent_expiry",),
        ))

    substance_patents = fs.values.get("drug_substance_patent_count")
    if substance_patents is not None and substance_patents >= 1 and (last_patent or 0) > 48:
        concerns.append(Concern(
            concern_key="drug_substance_patent_barrier",
            severity="MEDIUM",
            explanation=(
                f"{substance_patents} listed drug-substance patent(s) identified with a "
                f"long remaining term. Drug-substance claims are typically harder to "
                f"design around than formulation claims."
            ),
            feature_values={"drug_substance_patent_count": substance_patents},
        ))

    # --- Weak or absent development activity -----------------------------
    filings = fs.values.get("filing_count_12m")
    trials = fs.values.get("trial_starts_12m")
    if filings == 0 and trials == 0:
        concerns.append(Concern(
            concern_key="no_recent_development_activity",
            severity="MEDIUM",
            explanation=(
                "No regulatory filing or trial-start activity recorded in the 12 months "
                "to the as-of date. A quiet molecule may be quiet because the "
                "opportunity is not real."
            ),
            feature_values={"filing_count_12m": filings, "trial_starts_12m": trials},
        ))

    # --- Evidence problems ------------------------------------------------
    source_count = fs.values.get("evidence_source_count") or 0
    if source_count <= 1:
        concerns.append(Concern(
            concern_key="single_source_evidence",
            severity="HIGH",
            explanation=(
                f"Only {source_count} independent source(s) support the facts behind this "
                f"opportunity. A single source cannot be cross-checked, so an error in it "
                f"would be invisible to this system."
            ),
            feature_values={"evidence_source_count": source_count},
        ))

    recency = fs.values.get("evidence_recency_days")
    if recency is not None and recency > _STALE_EVIDENCE_DAYS:
        concerns.append(Concern(
            concern_key="stale_evidence",
            severity="MEDIUM",
            explanation=(
                f"The most recent supporting source document is {recency} days old. "
                f"The situation may have changed since."
            ),
            feature_values={"evidence_recency_days": recency},
        ))

    # --- Coverage gaps ----------------------------------------------------
    missing_families = [k for k, present in fs.coverage.items() if not present]
    if missing_families:
        concerns.append(Concern(
            concern_key="coverage_gaps",
            severity="MEDIUM" if len(missing_families) >= 2 else "LOW",
            explanation=(
                "No data ingested for: " + ", ".join(sorted(missing_families)) +
                ". Features depending on these were not evaluated, so this assessment "
                "is partial rather than negative on those dimensions."
            ),
            feature_values={"missing_coverage": sorted(missing_families)},
        ))

    # --- Manufacturing / capability mismatch (BUILD only) ----------------
    if opportunity_type is OpportunityType.BUILD:
        adjacency = fs.values.get("focus_capability_adjacency_score")
        if adjacency is not None and adjacency < 0.3:
            focus_name = context.get("focus_company_name", "the focus company")
            concerns.append(Concern(
                concern_key="weak_capability_fit",
                severity="MEDIUM",
                explanation=(
                    f"Limited overlap with {focus_name}'s publicly evidenced capabilities "
                    f"(adjacency {adjacency:.2f} of 1.00). Public capability evidence is "
                    f"incomplete by nature, so this is a gap in our visibility as much as "
                    f"a statement about the company."
                ),
                feature_values={"focus_capability_adjacency_score": adjacency},
            ))

    # --- SELL-specific ----------------------------------------------------
    if opportunity_type is OpportunityType.SELL:
        cp = context.get("counterparty") or {}
        if cp.get("holds_api_dmf"):
            concerns.append(Concern(
                concern_key="counterparty_has_own_api_filing",
                severity="HIGH",
                explanation=(
                    f"{cp.get('company_name', 'The company')} holds its own active drug "
                    f"master file for this API, which weighs against external sourcing."
                ),
                feature_values=dict(cp),
            ))
        days = cp.get("days_since_latest_event")
        if days is not None and days > 730:
            concerns.append(Concern(
                concern_key="counterparty_activity_stale",
                severity="MEDIUM",
                explanation=(
                    f"{cp.get('company_name', 'The company')}'s most recent recorded "
                    f"activity on this molecule is {days} days before the as-of date. Any "
                    f"sourcing decision has probably already been taken."
                ),
                feature_values=dict(cp),
            ))

    return concerns


_STALE_EVIDENCE_DAYS = 400
