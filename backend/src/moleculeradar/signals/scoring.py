"""Score assembly and evidence confidence.

Two numbers, never blended. The brief was emphatic about this and it is the
single most important modelling decision in the product:

* **Opportunity Score (0-100)** — how commercially interesting the situation
  looks, given what we can see. Literally the sum of signal points.
* **Evidence Confidence** — how much we trust that picture. Driven entirely by
  source breadth, source count, recency and coverage completeness.

Collapsing these into one number would destroy the most valuable distinction the
product can offer. "Strong opportunity, thin evidence" is an instruction to go
and investigate. A blended 55 is an instruction to do nothing, and is wrong.

The priority band combines them for triage only, and never by arithmetic: an
opportunity with INSUFFICIENT evidence is banded INSUFFICIENT_EVIDENCE whatever
its score, because presenting it as HIGH would mislead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.enums import EvidenceConfidence, OpportunityType, PriorityBand, SignalDirection
from .definitions import SIGNALS_BY_TYPE, SignalResult
from .features import FeatureSet

# Minimum independent sources before we are willing to call evidence anything
# better than LOW. One source is a single point of failure, however official.
_MIN_SOURCES_FOR_MEDIUM = 2
_MIN_SOURCES_FOR_HIGH = 3

# A source document older than this is stale enough to discount.
_STALE_DAYS = 180
_VERY_STALE_DAYS = 400


@dataclass(slots=True)
class ScoredOpportunity:
    opportunity_type: OpportunityType
    opportunity_score: float
    evidence_confidence: EvidenceConfidence
    evidence_confidence_score: float
    priority_band: PriorityBand
    headline: str
    signals: list[SignalResult] = field(default_factory=list)
    skipped_signals: list[dict[str, Any]] = field(default_factory=list)
    confidence_components: dict[str, Any] = field(default_factory=dict)

    @property
    def fired_signal_keys(self) -> list[str]:
        return [s.signal_key for s in self.signals]


def evaluate_signals(
    fs: FeatureSet,
    opportunity_type: OpportunityType,
    context: dict[str, Any],
) -> tuple[list[SignalResult], list[dict[str, Any]]]:
    """Run every signal for a type. Returns (fired, skipped-with-reason).

    Skipped signals are returned rather than discarded so the UI can distinguish
    "this did not apply" from "we have no data for this", which is exactly the
    distinction that stops a thin-data molecule looking like a clean one.
    """
    ctx = dict(context)
    ctx["opportunity_type"] = opportunity_type.value

    fired: list[SignalResult] = []
    skipped: list[dict[str, Any]] = []

    for definition in SIGNALS_BY_TYPE[opportunity_type]:
        missing = [f for f in definition.required_features if fs.is_missing(f)]
        if missing:
            skipped.append({
                "signal_key": definition.key,
                "reason": "missing_features",
                "missing_features": missing,
                "max_points": definition.max_points,
            })
            continue
        result = definition.run(fs, ctx)
        if result is None:
            skipped.append({
                "signal_key": definition.key,
                "reason": "did_not_fire",
                "max_points": definition.max_points,
            })
            continue
        fired.append(result)

    return fired, skipped


def compute_evidence_confidence(
    fs: FeatureSet,
    fired: list[SignalResult],
    skipped: list[dict[str, Any]],
    *,
    resolution_warnings: int = 0,
) -> tuple[EvidenceConfidence, float, dict[str, Any]]:
    """Confidence in the evidence base, independent of how attractive it looks.

    Components, each bounded:

    * source breadth — distinct publishers behind the facts;
    * document count — distinct retrieved artefacts;
    * recency — how old the newest supporting document is;
    * coverage — what proportion of the signal families had any data at all;
    * resolution cleanliness — penalty for entities resolved by fuzzy matching or
      still awaiting human review.
    """
    source_count = fs.values.get("evidence_source_count") or 0
    doc_count = fs.values.get("evidence_document_count") or 0
    recency_days = fs.values.get("evidence_recency_days")

    # Source breadth, 0..1.
    breadth = min(source_count / _MIN_SOURCES_FOR_HIGH, 1.0)

    # Document volume, 0..1. Saturates quickly — ten documents is not ten times
    # better evidenced than one.
    volume = min(doc_count / 6.0, 1.0)

    # Recency, 0..1.
    if recency_days is None:
        recency = 0.0
    elif recency_days <= _STALE_DAYS:
        recency = 1.0
    elif recency_days <= _VERY_STALE_DAYS:
        recency = 0.5
    else:
        recency = 0.2

    # Coverage: of all signals for this type, how many could even be evaluated?
    total_signals = len(fired) + len(skipped)
    missing_data_signals = sum(1 for s in skipped if s["reason"] == "missing_features")
    coverage = (
        1.0 - (missing_data_signals / total_signals) if total_signals else 0.0
    )

    # Resolution cleanliness.
    resolution = 1.0 if resolution_warnings == 0 else max(0.3, 1.0 - resolution_warnings * 0.2)

    score = round(
        0.30 * breadth + 0.15 * volume + 0.20 * recency + 0.25 * coverage + 0.10 * resolution,
        3,
    )

    components = {
        "evidence_source_count": source_count,
        "evidence_document_count": doc_count,
        "evidence_recency_days": recency_days,
        "signal_coverage_ratio": round(coverage, 3),
        "signals_missing_data": missing_data_signals,
        "signals_total": total_signals,
        "resolution_warnings": resolution_warnings,
        "components": {
            "source_breadth": round(breadth, 3),
            "document_volume": round(volume, 3),
            "recency": round(recency, 3),
            "coverage": round(coverage, 3),
            "resolution_cleanliness": round(resolution, 3),
        },
    }

    # Banding. Source breadth is a hard gate, not just a weighted input: a single
    # source cannot yield HIGH confidence no matter how recent or complete it is,
    # because we would have no way to detect that it is wrong.
    if source_count == 0 or doc_count == 0:
        band = EvidenceConfidence.INSUFFICIENT
    elif source_count >= _MIN_SOURCES_FOR_HIGH and score >= 0.75:
        band = EvidenceConfidence.HIGH
    elif source_count >= _MIN_SOURCES_FOR_MEDIUM and score >= 0.50:
        band = EvidenceConfidence.MEDIUM
    else:
        band = EvidenceConfidence.LOW

    return band, score, components


def assign_priority_band(
    opportunity_score: float, confidence: EvidenceConfidence
) -> PriorityBand:
    """Triage band. Deliberately not arithmetic over the two numbers.

    Insufficient evidence always dominates: we would rather show an analyst a
    short, trustworthy list than a long one padded with guesses.
    """
    if confidence is EvidenceConfidence.INSUFFICIENT:
        return PriorityBand.INSUFFICIENT_EVIDENCE
    if confidence is EvidenceConfidence.LOW:
        # A strong-looking score on weak evidence is still worth surfacing, but it
        # is labelled for what it is rather than promoted.
        return PriorityBand.LOW_CONFIDENCE
    if opportunity_score >= 60 and confidence is EvidenceConfidence.HIGH:
        return PriorityBand.HIGH
    if opportunity_score >= 70 and confidence is EvidenceConfidence.MEDIUM:
        return PriorityBand.HIGH
    if opportunity_score >= 35:
        return PriorityBand.WATCH
    return PriorityBand.LOW_CONFIDENCE


def build_headline(
    opportunity_type: OpportunityType,
    molecule_name: str,
    counterparty_name: str | None,
) -> str:
    """User-facing one-liner, written to the product's language rules.

    Never asserts a need, a relationship, or a recommendation.
    """
    if opportunity_type is OpportunityType.SELL:
        who = counterparty_name or "A company"
        return (
            f"{who} exhibits public signals consistent with potential demand for "
            f"{molecule_name} API"
        )
    return f"{molecule_name} surfaced for portfolio investigation"


def score_opportunity(
    fs: FeatureSet,
    opportunity_type: OpportunityType,
    molecule_name: str,
    context: dict[str, Any],
    *,
    counterparty_name: str | None = None,
    resolution_warnings: int = 0,
) -> ScoredOpportunity:
    """Full scoring pass for one candidate opportunity."""
    fired, skipped = evaluate_signals(fs, opportunity_type, context)

    # The score IS the sum of the signal points. No normalisation, no hidden
    # transform — a reader can add the decomposition up by hand and get this
    # number back. Allocations total 100 per type, so the cap can only bind
    # against floating-point drift.
    raw = sum(s.points for s in fired if s.direction is SignalDirection.SUPPORTING)
    opportunity_score = round(min(raw, 100.0), 1)

    confidence, confidence_score, components = compute_evidence_confidence(
        fs, fired, skipped, resolution_warnings=resolution_warnings
    )
    band = assign_priority_band(opportunity_score, confidence)
    headline = build_headline(opportunity_type, molecule_name, counterparty_name)

    return ScoredOpportunity(
        opportunity_type=opportunity_type,
        opportunity_score=opportunity_score,
        evidence_confidence=confidence,
        evidence_confidence_score=confidence_score,
        priority_band=band,
        headline=headline,
        signals=fired,
        skipped_signals=skipped,
        confidence_components=components,
    )
