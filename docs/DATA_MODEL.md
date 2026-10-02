# Data Model

**Status:** Phase 0 design, implemented in `backend/src/moleculeradar/core/models.py`.

---

## 1. Principles

1. **Stable internal IDs, preserved external IDs.** Every canonical row has a surrogate UUID
   primary key. No name is ever a primary key. Source identifiers (`NCTId`, `Appl_No`, DMF
   number, UNII, RxCUI, NDC) are preserved in `external_identifier` so we never lose the
   ability to re-join to source.
2. **Raw → normalized → canonical.** Ingestion never writes canonical rows directly.
   Raw payloads are persisted verbatim, normalized into typed staging records, then promoted to
   canonical entities through resolution. Each stage is independently re-runnable.
3. **Nothing is a fact without provenance.** Any row asserting something about the world carries
   an `evidence_id` chain. The schema makes an unsourced claim difficult to express and the data
   quality suite makes it visible.
4. **Time is modelled, not implied.** Every assertion carries both when it was *true* and when we
   *learned* it, so the engine can be replayed as of any past date.
5. **Collapse aggressively.** The brief listed ~35 entity types. Several are better as rows in a
   general table or as attributes. Over-modelling an ontology is the classic way to never ship.

---

## 2. Deliberate collapses (what we did *not* build as separate tables)

The brief said to think critically rather than implement every noun. These are the judgements:

| Brief entity | Decision | Reasoning |
| --- | --- | --- |
| `Molecule`, `ActiveIngredient` | **One table: `molecule`** | In small-molecule generics the commercially relevant unit is the active moiety. A separate `ActiveIngredient` would duplicate it. Salt/ester/hydrate variation is captured as `molecule_form` rows pointing at a parent moiety — that is where the real distinction lives (and it matters: a DMF is filed on a specific form). |
| `Brand` | **Attribute of `drug_product`** (`brand_name`) + alias rows | A brand with no product behind it has no commercial meaning for us. Promote later only if we start tracking brand-level commercial performance. |
| `Company`, `Manufacturer`, `APIManufacturer` | **One table: `company`** + typed `company_role` rows | These are *roles a company plays on a specific molecule/product*, not distinct kinds of legal entity. Dr. Reddy's is simultaneously all three. Modelling them as separate tables would guarantee duplicate, divergent copies of the same firm — the exact entity-resolution failure we must avoid. |
| `CompanyAlias` | **`entity_alias`** (polymorphic over molecule / company) | Aliasing is the same mechanism for companies, molecules and brands. One tested implementation beats three. |
| `DRLCapability` | **`company_capability` rows where `company_id` = the resolved Dr. Reddy's group** | A separate DRL-only table would hard-code a tenant into the schema and make "run this for another client" a rewrite. DRL is configuration (`settings.focus_company_*`), not schema. |
| `TherapeuticArea`, `Indication` | **`indication`** + `therapeutic_area` as a controlled vocabulary column | Real indication hierarchies are a research project. We need grouping, not ontology completeness. |
| `Target` | **Deferred** | No Tier-1 source gives us drug targets reliably, and no implemented signal uses one. Adding it now would be an empty table that implies capability we lack. |
| `Market`, `Country` | **`country`** (ISO 3166-1) only | "Market" was going to mean country in every Tier-1 use. Revisit when pricing/volume data arrives. |
| `Facility` | **Deferred** | Requires FDA establishment/inspection data (not Tier 1). Manufacturing *capability* currently comes from DMF holdings, which need no facility row. |
| `PatentFamily` | **Deferred** | Orange Book gives patent numbers, not families. A family table would be mostly null until EPO OPS is licensed. |
| `SupplyEvent`, `ShortageEvent`, `CommercialEvent` | **One table: `event`**, discriminated by `event_type` | They differ only in type and payload. One event stream is what the opportunity feed and the backtest both need; three tables would require three-way unions everywhere. |
| `Source`, `SourceDocument` | **Both kept** — genuinely distinct | `source` is the publisher/dataset (openFDA Drugs@FDA). `source_document` is a specific retrieval of a specific artefact (that endpoint, that day, that checksum). The evidence chain needs both. |
| `OpportunitySignal` | **Kept** as the decomposition rows of an `opportunity` score | This is what makes the score explainable; it is the most important table in the product. |
| `PotentialCustomer` | **`opportunity` of type `SELL`** with a `counterparty_company_id` | A "potential customer" is not an entity, it is an *assessment* about a company. Storing it as an entity would assert a commercial relationship we have no right to assert. |

---

## 3. Core tables

### 3.1 Reference / identity

* **`source`** — a publisher dataset. `key`, `name`, `publisher`, `licence`, `licence_url`,
  `access_mode` (`api` | `bulk` | `manual`), `verification_level`, `terms_reviewed_at`,
  `expected_update_cadence`, `is_enabled`.
* **`source_document`** — one retrieval. `source_id`, `locator` (URL or file path), `sha256`,
  `retrieved_at`, `published_at`, `byte_size`, `media_type`, `http_status`, `raw_payload_ref`.
  Immutable. The `sha256` is how we detect that a monthly file genuinely changed.
* **`external_identifier`** — `(scheme, value)` → canonical entity. Schemes: `UNII`, `RXCUI`,
  `NDC`, `FDA_APPL_NO`, `FDA_DMF_NO`, `NCT_ID`, `ISO3166A2`, `INN`, `CAS`. Unique on
  `(scheme, value, entity_type)`. This table *is* the deterministic layer of entity resolution.
* **`entity_alias`** — `entity_type`, `entity_id`, `alias`, `normalized_alias`, `alias_kind`
  (`legal_name` | `trade_style` | `abbreviation` | `former_name` | `salt_form` | `synonym` |
  `source_literal`), `first_seen_source_document_id`, `confidence`, `resolution_method`.
  Never deleted — aliases are evidence of how the world refers to a thing.

### 3.2 Pharma entities

* **`molecule`** — canonical active moiety. `preferred_name` (INN where known), `inn`, `unii`,
  `cas`, `molecule_class` (`small_molecule` | `peptide` | `biologic` | `other`), `is_peptide`,
  `complexity_flags` (JSONB: HPAPI / controlled / sterile / complex-chemistry markers, each
  with its own evidence reference).
* **`molecule_form`** — specific salt/ester/hydrate of a parent molecule. Matters because DMFs
  and products are filed on forms, while competition is assessed on moieties.
* **`company`** — canonical firm. `canonical_name`, `country_id`, `company_type`,
  `parent_company_id` (self-referential: subsidiary → group), `is_focus_company`.
* **`company_role`** — `company_id`, `role` (`nda_holder` | `anda_applicant` | `dmf_holder` |
  `trial_sponsor` | `labeller` | `shortage_reporter`), `molecule_id?`, `drug_product_id?`,
  `evidence_id`, temporal columns.
* **`drug_product`** — marketed/approved product. `regulatory_application_id`, `molecule_id`,
  `brand_name`, `dosage_form`, `route`, `strength_text`, `te_code`, `approval_date`,
  `marketing_status`.
* **`regulatory_application`** — `agency` (`FDA` | `EMA` | `CDSCO`), `application_type`
  (`NDA` | `ANDA` | `BLA`), `application_number`, `applicant_company_id`, `first_approval_date`.
* **`regulatory_submission`** — dated submission events within an application (from Drugs@FDA
  submission history). This is where approval/filing *events* come from.
* **`patent`** — `patent_number`, `jurisdiction`, `expiry_date`, `drug_substance_flag`,
  `drug_product_flag`, `use_code`, linked to a `regulatory_application`.
* **`exclusivity`** — `exclusivity_code`, `expiry_date`, linked to a product/application.
* **`drug_master_file`** — `dmf_number`, `dmf_type`, `holder_company_id`, `subject_text`,
  `molecule_id` (resolved), `status` (`A`/`I`), `submission_date`,
  `available_for_reference` (the GDUFA completeness-assessment flag — materially stronger
  evidence than mere existence of a filing).
* **`clinical_trial`** — `nct_id`, `sponsor_company_id`, `phase`, `overall_status`,
  `start_date`, `primary_completion_date`, `last_update_posted_date`, `molecule_id`,
  `indication_id`.
* **`indication`**, **`therapeutic_area`**, **`country`** — reference vocabularies.

### 3.3 Capability

* **`company_capability`** — the heart of the DRL profile, but generic over companies.
  `company_id`, `capability_type` (`api_supply` | `dosage_form` | `peptide_synthesis` |
  `hpapi_handling` | `complex_chemistry` | `biologics` | `market_presence` |
  `therapeutic_area`), `molecule_id?`, `value_text`, **`assertion_level`**
  (`FACT` | `INFERENCE` | `UNKNOWN`), `evidence_id`, `confidence`, temporal columns.

  `assertion_level` is non-null and enforced. A `FACT` requires at least one evidence row whose
  source document is a first-party regulatory record. An `INFERENCE` must record its
  `inference_basis`. This is how the product keeps the promise never to present an inferred
  capability as fact.

### 3.4 Evidence and events

* **`evidence`** — see `docs/EVIDENCE_MODEL.md`. `claim_text`, `source_document_id`,
  `extract_text`, `structured_record` (JSONB), `event_date`, `published_at`, `retrieved_at`,
  `confidence`, `extraction_method` (`deterministic_parse` | `llm_extraction` |
  `human_curation`), `llm_extraction_id?`.
* **`event`** — the temporal spine. `event_type`, `event_date`, `observed_at`,
  `subject_entity_type`, `subject_entity_id`, `molecule_id?`, `company_id?`, `payload` (JSONB),
  `evidence_id`, `dedupe_key` (unique — idempotent ingestion).
* **`llm_extraction`** — audit record for every model-assisted extraction: `model`,
  `prompt_version`, `prompt_hash`, `input_ref`, `raw_output`, `parsed_output`,
  `confidence`, `created_at`, `token_usage`. Required whenever `extraction_method` is
  `llm_extraction`.

### 3.5 Opportunity

* **`feature_snapshot`** — deterministic feature values per `(molecule_id, as_of_date)`:
  `developer_count`, `anda_filing_count_12m`, `trial_count_active`, `trial_growth_12m`,
  `observed_api_supplier_count`, `months_to_next_exclusivity_expiry`,
  `months_to_next_patent_expiry`, `shortage_events_36m`, `shortage_open_now`,
  `regulatory_submission_count_12m`, `country_count`, `focus_company_capability_overlap`,
  `evidence_source_count`, `evidence_recency_days`. Every value computed from `event`/canonical
  rows under a temporal gate, with the contributing evidence IDs recorded.
* **`opportunity`** — `opportunity_type` (`SELL` | `BUILD`), `molecule_id`,
  `counterparty_company_id?` (SELL only), `as_of_date`, **`opportunity_score`**,
  **`evidence_confidence`** (separate, never blended), `priority_band`
  (`HIGH` | `WATCH` | `LOW_CONFIDENCE` | `INSUFFICIENT_EVIDENCE`), `feature_snapshot_id`,
  `engine_version`, `created_at`.
* **`opportunity_signal`** — one row per contributing signal: `opportunity_id`, `signal_key`,
  `direction` (`SUPPORTING` | `COUNTER`), `points`, `feature_values` (JSONB),
  `explanation_template`, `evidence_ids` (array). The score is literally the sum of
  `points` over `SUPPORTING` rows, bounded — reproducible by hand from this table.
* **`counter_thesis`** — skeptic-engine output: `opportunity_id`, `concern_key`, `severity`,
  `explanation`, `evidence_ids`.
* **`investigation`** — a reproducible investigation run: `opportunity_id`, `as_of_date`,
  `engine_version`, `sections` (JSONB), `llm_extraction_id?`, `created_by`, `created_at`.
* **`review_queue_item`** — ambiguous entity-resolution decisions awaiting a human
  (`candidate_payload`, `proposed_action`, `confidence`, `status`, `decided_by`, `decided_at`).

---

## 4. Temporal columns (uniform contract)

Every table that asserts something about the world carries:

| Column | Meaning |
| --- | --- |
| `event_date` | When the thing happened in the world (approval date, trial start). Nullable only when the source genuinely has no date. |
| `published_at` | When the publisher made it public. |
| `retrieved_at` | When we fetched it. |
| `observed_at` | When Molecule Radar first recorded it. |
| `valid_from` / `valid_to` | For slowly-changing state (DMF status, marketing status). `valid_to IS NULL` = currently believed true. |

The backtest gate is: a row is visible at `as_of_date` **iff**
`COALESCE(published_at, event_date) <= as_of_date` **and** `observed_at <= as_of_date` where
`observed_at` is available. See `docs/BACKTESTING.md`. This is enforced in one place
(`core/temporal.py`) and tested by dedicated leakage tests, because a leak anywhere invalidates
every historical claim we would ever make to a customer.
