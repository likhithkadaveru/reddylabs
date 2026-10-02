"""Signal definitions.

A signal is a named, bounded, deterministic contribution to an opportunity score.
Each one declares:

* the features it needs — if any are missing it does not fire, and the absence is
  recorded as missing rather than scored as zero;
* a maximum point allocation, so the score is bounded and comparable;
* an explanation phrased to the product's language rules.

**Language rules are enforced here, in the code that generates user-visible
text**, not left to the UI or to an LLM:

* never "there are only two suppliers" → "two API suppliers identified in current
  public-source coverage";
* never "Company X needs this API" → "Company X exhibits public signals
  consistent with potential demand";
* never "DRL should make this" → "surfaced for portfolio investigation".

Point allocations are a stated, reviewable methodology, not a tuned model. We
have no outcome labels yet, so claiming fitted weights would be dishonest.
`docs/OPPORTUNITY_ENGINE.md` records the reasoning for each allocation and
Phase 7 backtesting is what will eventually justify changing them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..core.enums import OpportunityType, SignalDirection
from .features import FeatureSet


@dataclass(frozen=True, slots=True)
class SignalResult:
    signal_key: str
    direction: SignalDirection
    points: float
    max_points: float
    explanation: str
    feature_values: dict[str, Any]
    evidence_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SignalDefinition:
    key: str
    applies_to: tuple[OpportunityType, ...]
    max_points: float
    required_features: tuple[str, ...]
    evidence_features: tuple[str, ...]
    rationale: str
    evaluate: Callable[[FeatureSet, dict[str, Any]], SignalResult | None]

    def run(self, fs: FeatureSet, context: dict[str, Any]) -> SignalResult | None:
        for feat in self.required_features:
            if fs.is_missing(feat):
                return None
        return self.evaluate(fs, context)


# ---------------------------------------------------------------------------
# Point allocations — the published methodology.
#
# Each opportunity type's allocations sum to exactly 100 so that an opportunity
# score IS the sum of its signal points. That is what makes the decomposition
# shown to users arithmetically honest: a score of 78 is literally 18 + 15 + ...
# and a reader can add it up by hand. Normalising after the fact would break that
# property, so the allocations are chosen to total 100 instead.
#
# These are a stated, reviewable methodology, not fitted weights. We have no
# outcome labels yet; Phase 7 backtesting is what would justify changing them.
# The rationale for each allocation is recorded in docs/OPPORTUNITY_ENGINE.md.
# ---------------------------------------------------------------------------

ALLOCATIONS: dict[str, float] = {
    # BUILD — totals 100
    "loe_window": 18.0,
    "limited_observed_api_supply": 15.0,
    "capability_adjacency": 17.0,
    "developer_momentum": 13.0,
    "clinical_momentum": 12.0,
    "supply_stress_build": 10.0,
    "technical_barrier": 9.0,
    "geographic_breadth": 6.0,
    # SELL — totals 100
    "focus_supplies_api": 28.0,
    "counterparty_development_activity": 26.0,
    "counterparty_no_observed_api_capability": 20.0,
    "counterparty_recent_entry": 14.0,
    "supply_stress_sell": 12.0,
}

BUILD_TOTAL = sum(
    v for k, v in ALLOCATIONS.items()
    if k in {
        "loe_window", "limited_observed_api_supply", "capability_adjacency",
        "developer_momentum", "clinical_momentum", "supply_stress_build",
        "technical_barrier", "geographic_breadth",
    }
)
SELL_TOTAL = sum(
    v for k, v in ALLOCATIONS.items()
    if k in {
        "focus_supplies_api", "counterparty_development_activity",
        "counterparty_no_observed_api_capability", "counterparty_recent_entry",
        "supply_stress_sell",
    }
)
# Guarded at import: an allocation edit that breaks the sum fails fast rather
# than silently producing scores that cannot be added up.
assert BUILD_TOTAL == 100.0, f"BUILD allocations must total 100, got {BUILD_TOTAL}"
assert SELL_TOTAL == 100.0, f"SELL allocations must total 100, got {SELL_TOTAL}"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# BUILD signals
# ---------------------------------------------------------------------------


def _loe_window(fs: FeatureSet, ctx: dict[str, Any]) -> SignalResult | None:
    """A forthcoming loss-of-exclusivity event inside a commercially actionable window.

    Rationale: generic API development and qualification takes time. An expiry
    inside roughly 12-48 months is actionable; closer than that and a late entrant
    is already behind; much further and it is not yet a decision.
    """
    months = fs.get("months_to_next_loe_event")
    if months is None or months <= 0:
        return None
    max_pts = ALLOCATIONS["loe_window"]
    if 12 <= months <= 48:
        # Peak in the middle of the window, taper at both ends.
        points = max_pts * (1.0 - abs(months - 30) / 30.0)
        points = _clamp(points, max_pts * 0.5, max_pts)
    elif months < 12:
        points = max_pts * 0.35
    elif months <= 72:
        points = max_pts * 0.30
    else:
        return None

    date_str = fs.values.get("next_exclusivity_expiry_date") or fs.values.get(
        "next_patent_expiry_date"
    )
    detail = f" (earliest recorded expiry {date_str})" if date_str else ""
    return SignalResult(
        signal_key="loe_window",
        direction=SignalDirection.SUPPORTING,
        points=round(points, 1),
        max_points=max_pts,
        explanation=(
            f"Earliest exclusivity or patent expiry identified in Orange Book coverage "
            f"falls in approximately {months} months{detail}."
        ),
        feature_values={
            "months_to_next_loe_event": months,
            "months_to_next_exclusivity_expiry": fs.values.get(
                "months_to_next_exclusivity_expiry"
            ),
            "months_to_next_patent_expiry": fs.values.get("months_to_next_patent_expiry"),
        },
        evidence_keys=("months_to_next_exclusivity_expiry", "months_to_next_patent_expiry"),
    )


def _limited_observed_api_supply(fs: FeatureSet, ctx: dict[str, Any]) -> SignalResult | None:
    """Few API suppliers observed in our coverage.

    Rationale: a thin observed supplier field is the classic condition for an API
    entrant. Stated strictly as an observation about *our coverage* — a DMF list
    records US filings, so a molecule supplied widely outside the US can look thin.
    """
    count = fs.get("observed_competing_api_supplier_count")
    if count is None:
        return None
    max_pts = ALLOCATIONS["limited_observed_api_supply"]
    if count == 0:
        points, qualifier = max_pts, "no competing API suppliers"
    elif count == 1:
        points, qualifier = max_pts * 0.85, "one competing API supplier"
    elif count == 2:
        points, qualifier = max_pts * 0.65, "two competing API suppliers"
    elif count <= 4:
        points, qualifier = max_pts * 0.40, f"{count} competing API suppliers"
    elif count <= 7:
        points, qualifier = max_pts * 0.15, f"{count} competing API suppliers"
    else:
        return None

    return SignalResult(
        signal_key="limited_observed_api_supply",
        direction=SignalDirection.SUPPORTING,
        points=round(points, 1),
        max_points=max_pts,
        explanation=(
            f"{qualifier.capitalize()} identified in Molecule Radar's current "
            f"public-source coverage (active Type II DMF holders). This reflects US "
            f"drug master file filings only and is not a complete global supplier census."
        ),
        feature_values={
            "observed_competing_api_supplier_count": count,
            "observed_api_supplier_count": fs.values.get("observed_api_supplier_count"),
            "observed_api_supplier_count_reference_ready": fs.values.get(
                "observed_api_supplier_count_reference_ready"
            ),
        },
        evidence_keys=("observed_api_supplier_count",),
    )


def _developer_momentum(fs: FeatureSet, ctx: dict[str, Any]) -> SignalResult | None:
    """Rising regulatory filing activity: other developers are moving.

    Rationale: filings are dated, public, and directly indicate commercial intent.
    """
    recent = fs.get("filing_count_12m")
    prev = fs.get("filing_count_prev_12m")
    if recent is None:
        return None
    if recent == 0:
        return None
    max_pts = ALLOCATIONS["developer_momentum"]
    growth = None
    if prev and prev > 0:
        growth = (recent - prev) / prev
        points = max_pts * _clamp(0.4 + growth * 0.4, 0.3, 1.0)
        growth_text = f", up from {prev} in the preceding 12 months"
    else:
        points = max_pts * _clamp(recent / 5.0, 0.2, 0.7)
        growth_text = ""

    return SignalResult(
        signal_key="developer_momentum",
        direction=SignalDirection.SUPPORTING,
        points=round(points, 1),
        max_points=max_pts,
        explanation=(
            f"{recent} regulatory filing event(s) recorded in the 12 months to the "
            f"as-of date{growth_text}."
        ),
        feature_values={
            "filing_count_12m": recent,
            "filing_count_prev_12m": prev,
            "filing_growth_12m": round(growth, 3) if growth is not None else None,
            "distinct_anda_applicant_count": fs.values.get("distinct_anda_applicant_count"),
        },
        evidence_keys=("regulatory_event_count_12m",),
    )


def _clinical_momentum(fs: FeatureSet, ctx: dict[str, Any]) -> SignalResult | None:
    """Growing clinical development activity across sponsors."""
    starts = fs.get("trial_starts_12m")
    sponsors = fs.get("distinct_sponsor_count")
    if starts is None:
        return None
    if starts == 0:
        return None
    max_pts = ALLOCATIONS["clinical_momentum"]
    growth = fs.values.get("trial_growth_12m")
    if growth is not None:
        points = max_pts * _clamp(0.4 + growth * 0.4, 0.25, 1.0)
        growth_text = f" ({growth * 100:+.0f}% versus the preceding 12 months)"
    else:
        points = max_pts * _clamp(starts / 6.0, 0.2, 0.6)
        growth_text = " (prior-year base too small to express a growth rate)"

    return SignalResult(
        signal_key="clinical_momentum",
        direction=SignalDirection.SUPPORTING,
        points=round(points, 1),
        max_points=max_pts,
        explanation=(
            f"{starts} registered trial(s) started in the 12 months to the as-of "
            f"date{growth_text}; {sponsors} distinct sponsor(s) identified overall."
        ),
        feature_values={
            "trial_starts_12m": starts,
            "trial_starts_prev_12m": fs.values.get("trial_starts_prev_12m"),
            "trial_growth_12m": growth,
            "distinct_sponsor_count": sponsors,
            "late_phase_trial_count": fs.values.get("late_phase_trial_count"),
        },
        evidence_keys=("trial_starts_12m",),
    )


def _capability_adjacency(fs: FeatureSet, ctx: dict[str, Any]) -> SignalResult | None:
    """Fit with the focus company's publicly evidenced capability.

    This is an INFERENCE and is labelled as such. It says the molecule sits near
    capability the company has publicly demonstrated — never that the company can
    make it.
    """
    score = fs.get("focus_capability_adjacency_score")
    if score is None or score <= 0:
        return None
    max_pts = ALLOCATIONS["capability_adjacency"]
    points = max_pts * score
    ta_count = fs.values.get("focus_same_ta_api_capability_count") or 0
    class_match = fs.values.get("focus_class_capability_match")
    focus_name = ctx.get("focus_company_name", "the focus company")

    bits: list[str] = []
    if ta_count:
        bits.append(
            f"{ta_count} publicly evidenced API capability/ies in the same therapeutic area"
        )
    if class_match:
        bits.append("demonstrated handling of this molecule class")
    basis = "; ".join(bits) if bits else "partial capability overlap"

    return SignalResult(
        signal_key="capability_adjacency",
        direction=SignalDirection.SUPPORTING,
        points=round(points, 1),
        max_points=max_pts,
        explanation=(
            f"Overlap with {focus_name}'s publicly disclosed capabilities: {basis}. "
            f"This is an inference from public capability evidence, not a statement "
            f"about internal manufacturing feasibility."
        ),
        feature_values={
            "focus_capability_adjacency_score": score,
            "focus_same_ta_api_capability_count": ta_count,
            "focus_class_capability_match": class_match,
        },
        evidence_keys=("focus_capability_adjacency_score",),
    )


def _supply_stress(fs: FeatureSet, ctx: dict[str, Any]) -> SignalResult | None:
    """Recorded shortage activity: demonstrated supply fragility."""
    count = fs.get("shortage_events_36m")
    if count is None or count == 0:
        return None
    # Shared signal: the caller states which opportunity type is being scored
    # so the correct per-type allocation applies.
    key = "supply_stress_sell" if ctx.get("opportunity_type") == "SELL" else "supply_stress_build"
    max_pts = ALLOCATIONS[key]
    open_now = bool(fs.values.get("shortage_open_now"))
    points = max_pts * _clamp(count / 3.0, 0.3, 1.0)
    if open_now:
        points = max_pts
    state = "an unresolved shortage is recorded" if open_now else "no unresolved shortage recorded"
    return SignalResult(
        signal_key="supply_stress",
        direction=SignalDirection.SUPPORTING,
        points=round(points, 1),
        max_points=max_pts,
        explanation=(
            f"{count} shortage event(s) recorded in the 36 months to the as-of date; "
            f"{state} as at that date (FDA drug shortage records; US scope)."
        ),
        feature_values={
            "shortage_events_36m": count,
            "shortage_open_now": open_now,
        },
        evidence_keys=("shortage_events_36m",),
    )


def _technical_barrier(fs: FeatureSet, ctx: dict[str, Any]) -> SignalResult | None:
    """Technical difficulty as a durable competitive moat.

    Only fires on evidence-backed complexity flags or an established peptide
    classification — never on a guess about how hard a molecule looks.
    """
    is_peptide = fs.values.get("is_peptide")
    flag_count = fs.values.get("complexity_flag_count") or 0
    if not is_peptide and flag_count == 0:
        return None
    max_pts = ALLOCATIONS["technical_barrier"]
    points = 0.0
    bits: list[str] = []
    if is_peptide:
        points += max_pts * 0.6
        bits.append("classified as a peptide")
    if flag_count:
        points += min(max_pts * 0.4, flag_count * 2.0)
        flags = ", ".join(fs.values.get("complexity_flags") or [])
        bits.append(f"evidence-backed complexity flags: {flags}")
    return SignalResult(
        signal_key="technical_barrier",
        direction=SignalDirection.SUPPORTING,
        points=round(min(points, max_pts), 1),
        max_points=max_pts,
        explanation=(
            "Elevated technical barrier indicated — " + "; ".join(bits) +
            ". A higher barrier tends to limit the number of entrants."
        ),
        feature_values={
            "is_peptide": is_peptide,
            "complexity_flag_count": flag_count,
            "complexity_flags": fs.values.get("complexity_flags"),
        },
        evidence_keys=(),
    )


def _geographic_breadth(fs: FeatureSet, ctx: dict[str, Any]) -> SignalResult | None:
    """Breadth of geographies in which development is observed."""
    count = fs.get("trial_country_count")
    if count is None or count < 3:
        return None
    max_pts = ALLOCATIONS["geographic_breadth"]
    points = max_pts * _clamp(count / 12.0, 0.3, 1.0)
    return SignalResult(
        signal_key="geographic_breadth",
        direction=SignalDirection.SUPPORTING,
        points=round(points, 1),
        max_points=max_pts,
        explanation=(
            f"Registered development activity observed across {count} countries, "
            f"indicating multi-market interest."
        ),
        feature_values={"trial_country_count": count},
        evidence_keys=("distinct_sponsor_count",),
    )


# ---------------------------------------------------------------------------
# SELL signals
# ---------------------------------------------------------------------------


def _focus_supplies_api(fs: FeatureSet, ctx: dict[str, Any]) -> SignalResult | None:
    """The focus company publicly appears capable of supplying this API.

    This is the gate for a SELL opportunity: without it there is nothing to sell.
    Evidenced by an active Type II DMF held by the focus company.
    """
    holds = fs.values.get("focus_company_holds_api_dmf")
    if not holds:
        return None
    max_pts = ALLOCATIONS["focus_supplies_api"]
    ref_ready = bool(fs.values.get("focus_company_api_dmf_reference_ready"))
    points = max_pts if ref_ready else max_pts * 0.8
    focus_name = ctx.get("focus_company_name", "the focus company")
    extra = (
        " The filing is recorded as having passed completeness assessment and being "
        "available for reference by ANDAs, which is stronger evidence than filing alone."
        if ref_ready else
        " Filing is recorded as active; completeness-assessment status is not recorded "
        "as available-for-reference in current coverage."
    )
    return SignalResult(
        signal_key="focus_supplies_api",
        direction=SignalDirection.SUPPORTING,
        points=round(points, 1),
        max_points=max_pts,
        explanation=(
            f"{focus_name} holds an active Type II drug master file covering this API "
            f"in current public-source coverage.{extra}"
        ),
        feature_values={
            "focus_company_holds_api_dmf": True,
            "focus_company_api_dmf_reference_ready": ref_ready,
        },
        evidence_keys=("focus_company_holds_api_dmf",),
    )


def _counterparty_development_activity(fs: FeatureSet, ctx: dict[str, Any]) -> SignalResult | None:
    """The counterparty is publicly active on this molecule.

    Evidenced by that company's own dated filings or trial registrations — not by
    any assumption about its sourcing.
    """
    cp = ctx.get("counterparty")
    if not cp:
        return None
    filings = cp.get("filing_count_24m", 0)
    trials = cp.get("trial_count", 0)
    if filings == 0 and trials == 0:
        return None
    max_pts = ALLOCATIONS["counterparty_development_activity"]
    points = _clamp(filings * 6.0 + trials * 3.0, 0.0, max_pts)
    name = cp.get("company_name", "This company")
    parts: list[str] = []
    if filings:
        parts.append(f"{filings} regulatory filing event(s) in the 24 months to the as-of date")
    if trials:
        parts.append(f"{trials} registered trial(s) involving this molecule")
    return SignalResult(
        signal_key="counterparty_development_activity",
        direction=SignalDirection.SUPPORTING,
        points=round(points, 1),
        max_points=max_pts,
        explanation=(
            f"{name} shows public development activity on this molecule: "
            + "; ".join(parts) + "."
        ),
        feature_values=dict(cp),
        evidence_keys=(),
    )


def _counterparty_no_observed_api_capability(
    fs: FeatureSet, ctx: dict[str, Any]
) -> SignalResult | None:
    """No API filing of the counterparty's own is observed for this molecule.

    Carefully worded. A company with no US Type II DMF for an API may still make
    it captively or source it outside the US. This is a *coverage observation*
    that makes external sourcing plausible, nothing more.
    """
    cp = ctx.get("counterparty")
    if not cp:
        return None
    if cp.get("holds_api_dmf") is None:
        return None
    if cp.get("holds_api_dmf"):
        return None
    max_pts = ALLOCATIONS["counterparty_no_observed_api_capability"]
    name = cp.get("company_name", "This company")
    return SignalResult(
        signal_key="counterparty_no_observed_api_capability",
        direction=SignalDirection.SUPPORTING,
        points=max_pts,
        max_points=max_pts,
        explanation=(
            f"No active Type II drug master file held by {name} for this API is "
            f"identified in current public-source coverage. Captive manufacturing or "
            f"non-US sourcing would not necessarily appear in this coverage, so this "
            f"is consistent with — but not proof of — external API sourcing."
        ),
        feature_values=dict(cp),
        evidence_keys=(),
    )


def _counterparty_recent_entry(fs: FeatureSet, ctx: dict[str, Any]) -> SignalResult | None:
    """The counterparty's activity is recent, so a sourcing decision may be live."""
    cp = ctx.get("counterparty")
    if not cp:
        return None
    days = cp.get("days_since_latest_event")
    if days is None or days > 540:
        return None
    max_pts = ALLOCATIONS["counterparty_recent_entry"]
    points = max_pts * _clamp(1.0 - days / 540.0, 0.2, 1.0)
    name = cp.get("company_name", "This company")
    return SignalResult(
        signal_key="counterparty_recent_entry",
        direction=SignalDirection.SUPPORTING,
        points=round(points, 1),
        max_points=max_pts,
        explanation=(
            f"{name}'s most recent recorded activity on this molecule is "
            f"{days} day(s) before the as-of date, so any sourcing decision may still be open."
        ),
        feature_values=dict(cp),
        evidence_keys=(),
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

BUILD = (OpportunityType.BUILD,)
SELL = (OpportunityType.SELL,)
BOTH = (OpportunityType.BUILD, OpportunityType.SELL)

SIGNAL_DEFINITIONS: tuple[SignalDefinition, ...] = (
    SignalDefinition(
        key="loe_window",
        applies_to=BUILD,
        max_points=ALLOCATIONS["loe_window"],
        required_features=("months_to_next_loe_event",),
        evidence_features=("months_to_next_exclusivity_expiry", "months_to_next_patent_expiry"),
        rationale=(
            "Exclusivity/patent runway is the single most reliable dated predictor of "
            "generic entry timing available in public data."
        ),
        evaluate=_loe_window,
    ),
    SignalDefinition(
        key="limited_observed_api_supply",
        applies_to=BUILD,
        max_points=ALLOCATIONS["limited_observed_api_supply"],
        required_features=("observed_competing_api_supplier_count",),
        evidence_features=("observed_api_supplier_count",),
        rationale=(
            "Active Type II DMF holder count is the best public proxy for API supplier "
            "concentration. Scoped explicitly to US filings in all generated language."
        ),
        evaluate=_limited_observed_api_supply,
    ),
    SignalDefinition(
        key="developer_momentum",
        applies_to=BUILD,
        max_points=ALLOCATIONS["developer_momentum"],
        required_features=("filing_count_12m",),
        evidence_features=("regulatory_event_count_12m",),
        rationale="Dated filings are direct evidence of competitor commercial intent.",
        evaluate=_developer_momentum,
    ),
    SignalDefinition(
        key="clinical_momentum",
        applies_to=BUILD,
        max_points=ALLOCATIONS["clinical_momentum"],
        required_features=("trial_starts_12m",),
        evidence_features=("trial_starts_12m",),
        rationale="Registered trial starts are dated and sponsor-attributed.",
        evaluate=_clinical_momentum,
    ),
    SignalDefinition(
        key="capability_adjacency",
        applies_to=BUILD,
        max_points=ALLOCATIONS["capability_adjacency"],
        required_features=("focus_capability_adjacency_score",),
        evidence_features=("focus_capability_adjacency_score",),
        rationale=(
            "A BUILD candidate is only actionable if it sits near demonstrated "
            "capability. Explicitly an inference and labelled as such."
        ),
        evaluate=_capability_adjacency,
    ),
    SignalDefinition(
        key="supply_stress",
        applies_to=BOTH,
        max_points=ALLOCATIONS["supply_stress_build"],
        required_features=("shortage_events_36m",),
        evidence_features=("shortage_events_36m",),
        rationale="Shortages are the most commercially legible public supply signal.",
        evaluate=_supply_stress,
    ),
    SignalDefinition(
        key="technical_barrier",
        applies_to=BUILD,
        max_points=ALLOCATIONS["technical_barrier"],
        required_features=(),
        evidence_features=(),
        rationale=(
            "Technical difficulty limits entrants. Fires only on evidence-backed "
            "flags or an established peptide classification."
        ),
        evaluate=_technical_barrier,
    ),
    SignalDefinition(
        key="geographic_breadth",
        applies_to=BUILD,
        max_points=ALLOCATIONS["geographic_breadth"],
        required_features=("trial_country_count",),
        evidence_features=("distinct_sponsor_count",),
        rationale="Multi-market development indicates breadth of commercial interest.",
        evaluate=_geographic_breadth,
    ),
    SignalDefinition(
        key="focus_supplies_api",
        applies_to=SELL,
        max_points=ALLOCATIONS["focus_supplies_api"],
        required_features=(),
        evidence_features=("focus_company_holds_api_dmf",),
        rationale=(
            "Gate for SELL: without public evidence that the focus company supplies "
            "the API there is no supply opportunity to assess."
        ),
        evaluate=_focus_supplies_api,
    ),
    SignalDefinition(
        key="counterparty_development_activity",
        applies_to=SELL,
        max_points=ALLOCATIONS["counterparty_development_activity"],
        required_features=(),
        evidence_features=(),
        rationale="The counterparty's own dated filings and trials evidence activity.",
        evaluate=_counterparty_development_activity,
    ),
    SignalDefinition(
        key="counterparty_no_observed_api_capability",
        applies_to=SELL,
        max_points=ALLOCATIONS["counterparty_no_observed_api_capability"],
        required_features=(),
        evidence_features=(),
        rationale=(
            "Absence of the counterparty's own API filing makes external sourcing "
            "plausible. Worded as a coverage observation, never as a conclusion."
        ),
        evaluate=_counterparty_no_observed_api_capability,
    ),
    SignalDefinition(
        key="counterparty_recent_entry",
        applies_to=SELL,
        max_points=ALLOCATIONS["counterparty_recent_entry"],
        required_features=(),
        evidence_features=(),
        rationale="Recency indicates a sourcing decision may still be open.",
        evaluate=_counterparty_recent_entry,
    ),
)

SIGNALS_BY_TYPE: dict[OpportunityType, tuple[SignalDefinition, ...]] = {
    OpportunityType.BUILD: tuple(
        s for s in SIGNAL_DEFINITIONS if OpportunityType.BUILD in s.applies_to
    ),
    OpportunityType.SELL: tuple(
        s for s in SIGNAL_DEFINITIONS if OpportunityType.SELL in s.applies_to
    ),
}


def max_possible_points(opportunity_type: OpportunityType) -> float:
    """Total points available for a type. Always 100 by construction."""
    return BUILD_TOTAL if opportunity_type is OpportunityType.BUILD else SELL_TOTAL
