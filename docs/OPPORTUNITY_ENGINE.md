# Opportunity Engine

**Implementation:** `backend/src/moleculeradar/signals/`
**Tests:** `backend/tests/integration/test_opportunity_engine.py`

---

## 1. The two numbers

Molecule Radar reports two measures for every opportunity and **never combines
them**:

| | Question | Range |
| --- | --- | --- |
| **Opportunity Score** | How commercially interesting does this situation look, given what we can see? | 0–100 |
| **Evidence Confidence** | How much of that picture can we actually evidence? | HIGH / MEDIUM / LOW / INSUFFICIENT |

Blending them would destroy the most useful distinction the product offers. "High
opportunity, low confidence" is an instruction to go and investigate. "Medium
opportunity, high confidence" is a different instruction. A single blended 55 is
an instruction to do nothing, and is wrong in both cases.

---

## 2. The score IS the decomposition

An Opportunity Score is the arithmetic sum of its signal points. Nothing else.

```
Opportunity Score 72.2
  +18.0  Exclusivity runway
  +13.0  Regulatory filing momentum
  +12.8  Limited observed API supply
  +10.0  Supply stress
  + 9.6  Clinical development momentum
  + 6.8  Capability alignment
  + 2.0  Geographic breadth
  -----
   72.2
```

Point allocations total exactly 100 per opportunity type, guarded by an assert at
import. That constraint is deliberate: it is what lets a reader add the column up
by hand and get the score back. Normalising after the fact would break the
property, so the allocations were chosen to total 100 instead.

A data-quality check (`score_matches_signal_sum`) asserts this against stored data
as well as in tests, because an unexplainable score is worse than no score.

---

## 3. Deterministic features

Every feature is a count, a date difference, or a bounded ratio computed from
canonical rows under the temporal gate. No feature is produced by a model.

A feature whose inputs are absent is `None`, never `0`. The distinction carries
through the whole stack: a signal whose required features are missing does not
fire and is recorded as *unevaluated with a reason*, the API returns
`DATA_UNAVAILABLE`, and the UI prints "DATA UNAVAILABLE". Scoring absent data as
zero would make a thinly-covered molecule look like a cleanly contested one.

Principal features, and the source each depends on:

| Feature | Source |
| --- | --- |
| `months_to_next_loe_event`, `months_to_last_patent_expiry`, `drug_substance_patent_count` | Orange Book patent/exclusivity |
| `observed_api_supplier_count`, `observed_competing_api_supplier_count`, `focus_company_holds_api_dmf` | FDA DMF list (Type II, active) |
| `filing_count_12m`, `filing_count_prev_12m`, `approval_count_12m`, `distinct_anda_applicant_count` | Drugs@FDA submissions |
| `trial_starts_12m`, `trial_growth_12m`, `distinct_sponsor_count`, `late_phase_trial_count`, `terminated_trial_count_24m`, `trial_country_count` | ClinicalTrials.gov |
| `shortage_events_36m`, `shortage_open_now` | openFDA shortages |
| `focus_capability_adjacency_score` | derived from DMF-evidenced capabilities |
| `evidence_source_count`, `evidence_document_count`, `evidence_recency_days` | provenance tables |

**Growth guard.** `trial_growth_12m` is `None` when the prior-year base is below
2. One trial after zero is not infinite growth, and reporting it as such would
make the thinnest molecules score highest.

---

## 4. BUILD signals

*Which molecules deserve investigation for the portfolio?* BUILD is only computed
for molecules the focus company does **not** already supply.

| Signal | Max | Rationale |
| --- | --- | --- |
| `loe_window` | 18 | Exclusivity/patent runway is the most reliable dated predictor of generic entry timing in public data. Peaks at ~30 months — the point at which API development is actionable but not already late. |
| `capability_adjacency` | 17 | A candidate is only actionable if it sits near demonstrated capability. Always labelled INFERENCE. |
| `limited_observed_api_supply` | 15 | Active Type II DMF holder count is the best public proxy for supplier concentration. Does not fire above 7 competitors. |
| `developer_momentum` | 13 | Dated filings are direct evidence of competitor commercial intent. |
| `clinical_momentum` | 12 | Registered trial starts are dated and sponsor-attributed. |
| `supply_stress` | 10 | Shortages are the most commercially legible public supply signal. |
| `technical_barrier` | 9 | Difficulty limits entrants. Fires only on evidence-backed complexity flags or an established peptide classification — never on a guess. |
| `geographic_breadth` | 6 | Multi-market development indicates breadth of interest. |
| **Total** | **100** | |

## 5. SELL signals

*Who exhibits public signals consistent with potential demand for an API the
focus company appears able to supply?*

`focus_supplies_api` is a **gate**: without public evidence that the focus company
supplies the API, there is no supply opportunity and no SELL opportunity is
created.

| Signal | Max | Rationale |
| --- | --- | --- |
| `focus_supplies_api` | 28 | Gate. Full points only when the DMF is recorded as having passed completeness assessment. |
| `counterparty_development_activity` | 26 | The counterparty's own dated filings and trials. |
| `counterparty_no_observed_api_capability` | 20 | No API filing of its own makes external sourcing plausible. Worded as a coverage observation, never a conclusion. |
| `counterparty_recent_entry` | 14 | Recency suggests a sourcing decision may still be open. |
| `supply_stress` | 12 | Shortage raises sourcing urgency. |
| **Total** | **100** | |

A SELL opportunity must also clear a higher threshold (30 vs 20 for BUILD) before
it is persisted, because naming a company is the most reputationally dangerous
thing this product does.

---

## 6. Evidence confidence

Computed independently of the score from five bounded components:

| Component | Weight | What it measures |
| --- | --- | --- |
| Source breadth | 0.30 | Distinct publishers behind the facts |
| Coverage | 0.25 | Proportion of signals that could be evaluated at all |
| Recency | 0.20 | Age of the newest supporting document |
| Document volume | 0.15 | Distinct retrieved artefacts (saturates fast) |
| Resolution cleanliness | 0.10 | Penalty for entities resolved by fuzzy match or awaiting review |

**Source breadth is a hard gate, not just a weighted input.** One source cannot
yield HIGH confidence however recent or complete it is, because an error in it
would be invisible to us. Three independent sources are required for HIGH, two
for MEDIUM.

### Priority bands

Bands are triage, and deliberately not arithmetic over the two numbers:

```
INSUFFICIENT evidence                      -> INSUFFICIENT_EVIDENCE (always)
LOW evidence                               -> LOW_CONFIDENCE
score >= 60 and HIGH evidence              -> HIGH
score >= 70 and MEDIUM evidence            -> HIGH
score >= 35                                -> WATCH
otherwise                                  -> LOW_CONFIDENCE
```

Insufficient evidence dominates whatever the score: a short trustworthy list beats
a long one padded with guesses.

---

## 7. The skeptic engine

Every opportunity receives a counter-analysis derived from the **same features**
as the supporting signals. It is not a model asked to be negative, which would
produce plausible objections unconnected to our data.

Concerns: `crowded_api_supply`, `crowded_generic_field`,
`declining_clinical_activity`, `trial_failures`, `long_patent_runway`,
`drug_substance_patent_barrier`, `no_recent_development_activity`,
`single_source_evidence`, `stale_evidence`, `coverage_gaps`,
`weak_capability_fit`, `counterparty_has_own_api_filing`,
`counterparty_activity_stale`.

**A counter-thesis never alters the score.** It is displayed beside it. Netting
them off would hide exactly the disagreement an analyst needs to see.

---

## 8. Language rules, enforced in code

Generated text is produced in `signals/definitions.py` and
`signals/scoring.py` — not in the UI, and not by a model — so the rules are
testable:

| Never | Always |
| --- | --- |
| "Only two suppliers exist" | "Two competing API suppliers identified in Molecule Radar's current public-source coverage... not a complete global supplier census" |
| "Company X needs this API" | "Company X exhibits public signals consistent with potential demand" |
| "DRL should manufacture this" | "Surfaced for portfolio investigation" |
| "Company X has no API capability" | "No active Type II DMF held by Company X is identified in current coverage. Captive manufacturing or non-US sourcing would not necessarily appear" |
| Presenting an inference as fact | Capability entries carry FACT or INFERENCE, and an inference states its basis |

`test_opportunity_engine.py` asserts these against the banned constructions.

---

## 9. Idempotency and reproducibility

A run is keyed on `(molecule, as_of_date, engine_version)`. Re-running replaces
rather than accumulates, so a retried job cannot inflate the feed. `ENGINE_VERSION`
is stored on every opportunity; bump it whenever scoring changes in a way that
makes scores non-comparable with previously stored ones.

---

## 10. What would justify changing the weights

Only backtest evidence (Phase 7) and manual validation (Phase 6). Not intuition,
and not a desire for more HIGH-priority signals. The allocations are a published
methodology; changing them silently would make every historical score
incomparable.
