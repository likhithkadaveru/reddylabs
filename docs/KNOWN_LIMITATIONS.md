# Known Limitations

This document exists because Molecule Radar's entire value proposition is that it
does not overstate what it knows. Anything that would embarrass us in front of a
pharmaceutical customer belongs here, written plainly, before they find it.

Last updated: 2026-10-05.

> **2026-10-05 update.** A second environment was tested for live network access.
> Every data host is still denied, and the block is a strict allowlist that
> includes `example.com` — not a pharma-specific denial. The full attempt,
> including why no substitute data source was used, is recorded in
> `docs/validation/LIVE_VALIDATION_ATTEMPT_001.md`. Verdict: NOT PROVEN, because
> the methodology was never exposed to real data rather than having failed
> against it.

---

## 1. The limitation that currently dominates everything else

**No source has ever been contacted from this environment.**

Every pharmaceutical, regulatory and scientific host is denied by this
environment's network egress policy at the CONNECT stage (HTTP 403). The
connectors, schema, resolution, scoring, API and UI are implemented and tested,
but the system has never ingested a single real record.

Consequences, stated precisely:

* No source is `LIVE_VERIFIED`. All five Tier-1 sources are `DOC_VERIFIED`: their
  contracts come from the publishers' own documentation, read during Phase 0, not
  from an observed response.
* **The connectors' field mappings are unverified against live data.** They are
  written defensively (header-name parsing, fuzzy column matching, quarantine on
  mismatch) precisely because of this, and the schema-mismatch paths are tested —
  but a first live run should be treated as a contract test, not a data load.
  Expect to fix something.
* Rate limits for ClinicalTrials.gov were not corroborated to a specific number.
  The client runs serialised at 2 requests/second by default. Confirm the real
  limit before raising it.
* The product shows `DATA UNAVAILABLE` throughout rather than placeholder
  content, and `moleculeradar sources verify` reports the blocked hosts honestly.

**What unblocks it:** allow outbound HTTPS to `www.fda.gov`, `api.fda.gov` and
`clinicaltrials.gov`, then run:

```bash
moleculeradar sources contract-test --report docs/validation/CONTRACT_TEST_REPORT.md
```

That is stronger than `sources verify`, which only answers "can we reach it".
The contract test checks envelope shape, record fields, date formats,
identifiers, nullability, pagination and the publisher's currency timestamp
against the live response, and reports every divergence from what our connectors
assume. It is proven against a local mock server with planted faults, so a
passing result means something.

---

## 2. Limitations of the data itself

These do not go away when egress opens. They are properties of public pharma data
and the product must keep stating them.

### 2.1 A DMF is a filing, not a supply relationship

The supplier counts that drive BUILD scoring come from the FDA Drug Master File
list. That list records **filings**, not commerce. Specifically:

* a company may hold an active DMF and never have commercialised the API;
* a company may supply an API worldwide and hold no US DMF at all;
* a company may manufacture captively, which produces no DMF;
* completeness-assessment status ("available for reference") is a stronger
  signal than mere existence, and we record it when the companion list is
  supplied — but we do not currently ingest that list, so it is usually unknown.

Every generated sentence about supplier counts is scoped to "identified in
Molecule Radar's current public-source coverage" and explicitly disclaims being a
global census. That wording is not decoration; it is the honest description.

### 2.2 US-centric coverage

All five Tier-1 sources are US federal. EU, Indian, Chinese, Japanese and
Brazilian activity is largely invisible. For a company whose home market is
India, that is a significant gap, and it means:

* a molecule heavily developed outside the US can look quiet;
* a widely-supplied API can look thinly supplied.

EMA, CDSCO, EU CTIS and TED are recorded as CANDIDATE sources for exactly this
reason.

### 2.3 Orange Book is a snapshot, not a history

The published file carries only *unexpired* patent and exclusivity data. History
must be accumulated by us, snapshot over snapshot. Until we have several months
of snapshots, patent-delisting and exclusivity-expiry events will be sparse.

### 2.4 Orange Book listing is not legal advice

A listed patent is not necessarily enforceable, and its claims do not necessarily
cover a given product. Molecule Radar performs no freedom-to-operate analysis and
says so in every investigation.

### 2.5 Trial registration is incomplete

ClinicalTrials.gov registration is mandatory for some studies and voluntary for
others. Early development and non-US programmes are under-represented. A
"developer count" is a count of *registered* developers.

### 2.6 Combination products

A combination product is treated as its own molecule, not split into components.
That is the right unit for approvals and exclusivity, but it means a component's
competitive picture does not automatically inherit the combination's activity.

### 2.7 No commercial data at all

No pricing, volume, revenue or market size appears anywhere in this product,
because no licensed source for it is in use. Opportunity Score therefore measures
*structural attractiveness from public signals*, not expected value. An analyst
must supply the commercial layer.

---

## 3. Limitations of the methodology

### 3.1 Signal weights are a stated methodology, not fitted parameters

The point allocations in `signals/definitions.py` were chosen by argument, not by
fitting to outcomes, because there are no outcome labels yet. They total exactly
100 per opportunity type so scores are comparable and decomposable. They should
be revised only on backtest evidence (Phase 7), not on intuition.

### 3.2 Scores are comparable within a type, not across types

A BUILD score of 70 and a SELL score of 70 are not the same claim. They are
computed from different signal sets answering different questions.

### 3.3 Capability adjacency is a coarse inference

`focus_capability_adjacency_score` blends same-therapeutic-area API capability
count with a molecule-class match. It is deliberately coarse: a finer score would
imply precision the inputs do not support. It is always labelled INFERENCE and
never presented as an assessment of manufacturing feasibility.

### 3.4 The skeptic engine can only see what we have ingested

"No counter-arguments found" means none were derivable from current evidence. On
thin coverage that reflects our blindness more than the strength of the thesis,
and the UI says so in those words.

### 3.5 Evidence confidence does not measure correctness

It measures source breadth, volume, recency, coverage completeness and resolution
cleanliness. Three independent sources that are all wrong would still yield HIGH.

---

## 4. Limitations of entity resolution

### 4.1 Recall is deliberately traded for precision

A fuzzy name match can never auto-merge. The consequence is duplicate entities
when a company's name varies more than normalisation handles. Duplicates split a
company's filings and understate its activity — which is visible on the Data
Health screen (`duplicate_companies`) and in the review queue, but is real.

### 4.2 Abbreviations need seeding

"DRL" has no textual relationship to "Dr. Reddy's Laboratories Ltd". The resolver
will not invent that link. It must be seeded via `MR_FOCUS_COMPANY_ALIAS_SEEDS`
or recorded through the review queue. The same applies to every other
abbreviation in the data, which are not seeded.

### 4.3 Subsidiary linking is name-based

Group membership is inferred from identity-token containment, so "Dr. Reddy's
Laboratories Inc" links but a differently-named subsidiary does not. Corporate
structure is not in any Tier-1 source. Candidates that share some tokens go to
review rather than being linked.

### 4.4 No merge-back for provisional entities

When an ambiguous pair is accepted in review, an alias is recorded and the
decision logged, but rows already pointing at the provisional entity are not
repointed. Merging the rows is a separate operation that does not exist yet.

---

## 5. Limitations of the backtest

### 5.1 OBSERVED mode is not yet meaningful

`VisibilityMode.OBSERVED` reproduces what the system actually held, which is the
stricter and more defensible test. It requires accumulated observation history,
and we have none. Until then, backtests must run in `KNOWABLE` mode, which is
"what a careful analyst could have known" — a weaker claim.

### 5.2 Snapshot sources limit historical reach

Orange Book and the DMF list are current-state snapshots. A `KNOWABLE` backtest
before our first ingestion sees only rows whose `published_at` precedes the as-of
date, which for snapshot sources is approximately nothing. Meaningful historical
evaluation needs either archived editions of those files or months of our own
snapshots.

### 5.3 No outcome labels

We have no ground truth about which opportunities were real. Phase 6 manual
validation is the intended first step, and nothing in the engine should be tuned
before it exists.

---

## 6. Limitations of the implementation

* **LLM features are scaffolded, not wired.** `llm_extraction` auditing, the
  `extraction_method` discipline and the CHECK constraint are implemented, and
  investigations are assembled deterministically. No model call is made yet.
  Enabling one must preserve the rule that it rewords prose whose claims already
  carry evidence IDs.
* **No background scheduler.** Ingestion runs from the CLI. A scheduler is a
  deployment concern and is documented in `DEPLOYMENT.md`, not implemented.
* **Raw payload storage is filesystem-only in code.** The S3 backend is a
  configuration option that production validation requires, but the S3 writer
  itself is not implemented — only the guard that refuses to boot production on
  filesystem storage.
* **Single-tenant.** The focus company is configuration rather than schema, so
  multi-tenancy is a schema change away, not a rewrite — but it is not built.
* **No pagination on some list endpoints.** Molecule and company searches cap at
  200 rows. Fine at current scale, not at full Orange Book scale.
* **`feed/changes` uses wall-clock "today"** rather than the engine's as-of date,
  so it is a live view and not leakage-safe. It is a convenience panel, not an
  input to any score.

---

## 7. What we would fix first, in order

1. Open egress, run `sources verify`, then ingest each source once and treat the
   first run as a contract test.
2. Ingest the Type II "available for reference" companion list, which materially
   strengthens supplier evidence.
3. Add DailyMed and the NDC directory for marketed-product and manufacturer
   links.
4. Add EMA and CDSCO to reduce US-centricity, especially for a client whose home
   market is India.
5. Begin accumulating snapshots so OBSERVED-mode backtesting becomes possible.
6. Manually validate 30–50 opportunities and only then revisit the weights.
