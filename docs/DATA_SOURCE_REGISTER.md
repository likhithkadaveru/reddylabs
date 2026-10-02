# Data Source Register

**Owner:** Llama Labs / Molecule Radar
**Last updated:** 2026-10-02
**Status of this document:** Phase 0 output. Live-verified where stated; otherwise explicitly marked.

---

## 0. Verification honesty statement

Molecule Radar's credibility depends on never overstating what we know. That rule applies to
this register first.

This environment's network egress policy denied **every** external data host at the CONNECT
stage (HTTP 403 from the egress gateway). Package registries (PyPI, npm) are permitted; no
pharmaceutical, regulatory, or scientific host is. Both direct HTTP clients and the
server-side fetch tool were blocked. A web *search* channel remained available, which is how
the documentation claims below were gathered.

Therefore every source in this register carries a **verification level**, and no source may be
promoted to `LIVE-VERIFIED` until a request has actually been executed and its response
recorded as a contract fixture.

| Verification level | Meaning |
| --- | --- |
| `LIVE-VERIFIED` | We executed a real request/download and recorded the observed response shape. |
| `DOC-VERIFIED` | Access mode, licensing and update cadence corroborated against the publisher's own documentation, read this session. Response shape **not** observed. |
| `ASSERTED` | Believed true but not corroborated this session. Treat as unknown. |

**Nothing in this register is currently `LIVE-VERIFIED`.** See `KNOWN_LIMITATIONS.md`.

### Hosts denied by this environment's egress policy

```
api.fda.gov              clinicaltrials.gov        eutils.ncbi.nlm.nih.gov
www.fda.gov              dailymed.nlm.nih.gov      api.crossref.org
accessdata.fda.gov       rxnav.nlm.nih.gov         pubchem.ncbi.nlm.nih.gov
www.ema.europa.eu        cdsco.gov.in              www.drreddys.com
ops.epo.org              search.patentsview.org    patents.google.com
patentscope.wipo.int     www.who.int               api.usaspending.gov
api.ted.europa.eu
```

Molecule Radar is built so that connectivity is the *only* missing input: connectors, schema,
resolution, scoring and UI are implemented and tested against recorded fixtures, and
`moleculeradar sources verify` reports per-source reachability truthfully rather than
pretending.

---

## 1. Status taxonomy

| Status | Meaning |
| --- | --- |
| **ACTIVE** | Connector implemented, in the ingestion schedule, and relied upon by at least one signal. |
| **CANDIDATE** | Wanted and apparently permissible, but not yet implemented or not yet cleared. |
| **RESTRICTED** | Usable only under conditions (licence, API key, attribution, rate limits, manual download). Not freely automatable. |
| **REJECTED** | Will not be used. Reason recorded. |

Note on the word ACTIVE: in this register ACTIVE means *wired into the product*, not *currently
returning data in this environment*. Nothing returns data in this environment.

---

## 2. Tier 1 — the minimum set for a legitimate first signal

Phase 0's central question was: **what is the smallest set of sources that can produce a real
SELL or BUILD signal without any private Dr. Reddy's data?**

Answer: four US federal sources plus one registry. Together they yield molecule-level
competition, exclusivity timing, API supplier concentration, demand-side filing activity, and —
critically — **Dr. Reddy's own API capability as evidenced by its FDA Drug Master File holdings**.
That last point is the key Phase 0 finding: DRL capability can be established from a
first-party regulatory source, so the product does not depend on scraping drreddys.com.

### 2.1 FDA Orange Book data files — **ACTIVE** · `DOC-VERIFIED`

* **Contains:** approved small-molecule drug products (`products.txt`), listed patents with
  expiry incl. extensions (`patent.txt`), and FDA exclusivity codes with expiry
  (`exclusivity.txt`).
* **Access:** bulk ZIP download from fda.gov. Three ASCII files, **tilde (`~`) delimited**.
* **Update frequency:** monthly, generally end of the second week, covering the prior month's
  changes; concurrent with the printed cumulative supplement.
* **Historical coverage:** the file is a *current-state snapshot* of unexpired patent/exclusivity
  data — it is not a history. History must be accumulated by us snapshot-over-snapshot. This is
  a core reason Molecule Radar stores every snapshot: our event history becomes proprietary.
* **Identifiers:** `Appl_No`, `Product_No`, `Appl_Type` (N/A), `Ingredient`, `Trade_Name`,
  `Applicant`, `Applicant_Full_Name`, `TE_Code`, `Approval_Date`, `Patent_No`,
  `Patent_Expire_Date_Text`, `Exclusivity_Code`, `Exclusivity_Date`.
* **Licensing:** US federal government work, public domain. Automated retrieval of the
  published bulk file is the intended use.
* **Why it matters:** loss-of-exclusivity timing (BUILD), applicant counts per molecule
  (developer competition), and brand/originator identification (SELL target discovery).
* **Reliability:** high. Authoritative, but known to lag real-world patent events and to contain
  applicant-name inconsistency — which is precisely why entity resolution is a first-class
  subsystem.

### 2.2 FDA List of Drug Master Files (DMFs) — **ACTIVE** · `DOC-VERIFIED`

* **Contains:** every DMF received by FDA with DMF number, **holder name**, **subject (the API
  or material)**, **type** (II = drug substance/API), **activity status** (A=active, I=inactive),
  and submission date. A companion list identifies Type II DMFs that have **passed completeness
  assessment and are available for reference by ANDAs** under GDUFA.
* **Access:** quarterly published spreadsheet (XLS) on fda.gov.
* **Update frequency:** quarterly. The current edition at time of research covered DMFs received
  by 2026-06-30 with acknowledgement letters sent before 2026-07-19, and flags changes to
  status/type/holder/subject since the prior 2026-03-31 edition. The publisher therefore
  supplies its own change set — useful, but we still diff snapshots ourselves.
* **Identifiers:** DMF number (stable external key), holder name (string — needs resolution),
  subject (API name — needs molecule resolution).
* **Licensing:** US federal government work, public domain.
* **Why it matters — the single highest-value source for this product:**
  1. **Observed API supplier count per molecule.** Counting active Type II DMF holders per
     subject gives a defensible, evidence-backed proxy for API supplier concentration. This is
     the backbone of BUILD scoring.
  2. **Dr. Reddy's public API capability.** DMFs held by Dr. Reddy's entities are first-party
     regulatory evidence that DRL supplies a given API — far stronger provenance than a
     marketing catalogue, and with no terms-of-use risk.
  3. **SELL counterparty logic.** A company developing a molecule while holding no DMF for its
     API exhibits a public signal consistent with external API demand.
* **Caveat we must state in the product:** a DMF list is evidence of *filing*, not of current
  commercial supply, and non-US supply needs no US DMF. Supplier counts are therefore always
  rendered as "identified in current public-source coverage", never as "the number of suppliers".

### 2.3 openFDA Drugs@FDA (`/drug/drugsfda`) — **ACTIVE** · `DOC-VERIFIED`

* **Contains:** NDA/ANDA/BLA applications with sponsor, products, and **submission history**
  (type, number, status, submission date).
* **Access:** REST JSON API at `api.fda.gov`, no API key required for low volume (keyed access
  raises limits); **also a bulk zipped-JSON download**. openFDA publishes a download manifest
  describing available `partitions` per endpoint; Drugs@FDA was a single ~8.9 MB file,
  last updated 2026-09-23 at time of research.
* **Update frequency:** openFDA drug download partitions were refreshed 2026-09-23; Drugs@FDA
  tracks FDA approval actions, effectively weekly-to-monthly.
* **Response envelope:** `meta` (disclaimer, licence link, last-updated, `results.total`) +
  `results` array. Query syntax `search=field:term`, `limit` max 1000, offset via `skip`.
  Harmonised `openfda` block attaches cross-identifiers (UNII, RxCUI, SPL IDs, NDCs,
  pharmacologic class, `application_number`) to records across endpoints — this is our main
  identifier bridge.
* **Licensing:** public domain; openFDA publishes a data licence link in every `meta` block,
  which we store with the provenance record.
* **Why it matters:** ANDA filing and approval activity per molecule = generic developer
  entry/momentum (both BUILD and SELL). Submission history gives dated events.
* **Important caveat:** openFDA explicitly disclaims that its data is not for clinical decisions
  and may be incomplete; we carry that disclaimer through to the evidence record.

### 2.4 openFDA Drug Shortages (`/drug/shortages`) — **ACTIVE** · `DOC-VERIFIED`

* **Contains:** current and resolved US drug shortage records with status, reason, dates,
  presentation detail, and the harmonised `openfda` identifier block (incl. `manufacturer_name`,
  `generic_name`, `application_number`, `unii`, `rxcui`).
* **Access:** REST JSON API at `api.fda.gov`, same envelope and syntax as above.
* **Update frequency:** frequent; shortage status changes are the point of the dataset.
* **Licensing:** public domain.
* **Why it matters:** shortages are the most commercially legible supply signal available in
  public data, and they are dated — ideal for `SHORTAGE_OPENED` / `SHORTAGE_RESOLVED` events and
  for shortage-frequency features.

### 2.5 ClinicalTrials.gov API v2 — **ACTIVE** · `DOC-VERIFIED`

* **Contains:** registered studies: sponsor/collaborators, interventions, phase, status, dates,
  locations, conditions.
* **Access:** REST API v2 described by an OpenAPI 3.0 specification, `pageToken`/`pageSize`
  pagination, field selection supported. Bulk alternative exists via the third-party AACT
  database (separate entry below).
* **Update frequency:** daily.
* **Historical coverage:** registry back to 2000; record version history is retained by the
  registry, giving us genuine dated phase transitions.
* **Identifiers:** `NCTId` (stable), sponsor name (string — resolution required), intervention
  name (molecule resolution required).
* **Licensing:** NLM terms and conditions apply to all data however acquired. The API is the
  sanctioned access route; NLM has published explicitly on the unsuitability of screen scraping.
  **We use the API only.**
* **Why it matters:** clinical development activity and developer growth per molecule; sponsor
  identification for SELL counterparties; geographic expansion via study locations.
* **Rate limits:** not corroborated to a specific numeric limit this session → treated as
  `ASSERTED`. Our client is conservative by default (serialised, backoff, caching) and the real
  limit must be confirmed on first live run.

---

## 3. Tier 2 — CANDIDATE sources (wanted, not yet wired)

| Source | Contains | Access | Status | Note |
| --- | --- | --- | --- | --- |
| **DailyMed (SPL)** | Structured product labels, incl. **manufacturer/establishment** and NDC detail | Public REST services + bulk SPL downloads | CANDIDATE · `DOC-VERIFIED` | Best route to marketed-product ↔ manufacturer links; large volume, so deferred past Tier 1. |
| **openFDA NDC Directory** | Marketed product listing, labeller, dosage form, marketing dates | REST + bulk zipped JSON (1 file) | CANDIDATE · `DOC-VERIFIED` | Cheap to add; gives marketing start/end dates = market entry/withdrawal events. |
| **openFDA Drug Label** | SPL label content, 14 bulk files | REST + bulk | CANDIDATE · `DOC-VERIFIED` | Useful for indications/formulation classification via LLM extraction. Large. |
| **FDA Purple Book** | Licensed biologics & biosimilars, exclusivity | Published downloadable database | CANDIDATE · `ASSERTED` | Needed only when biosimilar scope opens. Out of Tier 1. |
| **PubMed (E-utilities)** | Literature | Public API, keyed for higher rates | CANDIDATE · `DOC-VERIFIED` | Research-intensity feature, weak commercial signal. Low priority — included only if it earns its place. |
| **Crossref** | DOI metadata | Public REST API, polite pool via contact email | CANDIDATE · `DOC-VERIFIED` | Same reasoning as PubMed. |
| **RxNorm / RxNav** | Normalised drug nomenclature, ingredient↔product graph | Public REST API | CANDIDATE · `DOC-VERIFIED` | Strong candidate for molecule normalisation; complements UNII. |
| **PubChem PUG-REST** | Chemical identity, synonyms, structure | Public REST API | CANDIDATE · `DOC-VERIFIED` | Salt/base normalisation and synonym expansion. |
| **AACT (CTTI)** | Full ClinicalTrials.gov as a relational DB | Free account, bulk Postgres dump | CANDIDATE · `ASSERTED` | Would transform trial backfill economics. Requires registration → account provisioning decision. |
| **PatentsView** | US patent bibliographic + assignee data | Public API, API key required | CANDIDATE · `ASSERTED` | Legitimate patent route. Orange Book already gives the *pharma-relevant* patent linkage, so this is an enrichment, not Tier 1. |
| **EU CTIS / EudraCT** | European trial registrations | Public portal; bulk access unclear | CANDIDATE · `ASSERTED` | Needed for EU developer activity. Access route must be established. |
| **EMA medicines data** | EU authorised medicines, EPARs | Published downloadable tables | CANDIDATE · `ASSERTED` | Required for EU regulatory momentum. Terms to be confirmed. |
| **CDSCO (India)** | Indian approvals, manufacturing permissions | Government portal | CANDIDATE · `ASSERTED` | Highly relevant to DRL's home market; portal structure historically unstable. Terms to be confirmed. |
| **TED (EU public procurement)** | EU tender notices incl. pharmaceutical supply | Documented API | CANDIDATE · `ASSERTED` | Real demand-side signal for SELL. Promising; access terms to be confirmed. |

---

## 4. RESTRICTED

| Source | Restriction | Handling |
| --- | --- | --- |
| **Dr. Reddy's public API catalogue / corporate site** (`drreddys.com`) | We could not retrieve `robots.txt` or the site terms (egress denied), so we cannot establish whether automated collection is permitted. | **Not ingested.** DRL capability is instead derived from FDA DMF holdings, Orange Book applicant records and openFDA — all first-party regulatory sources. The corporate site may later be added **only** after its terms are read and recorded, and preferably as human-curated, individually-cited entries rather than a crawl. |
| **Company annual reports / investor decks / press releases** | Copyrighted documents. Facts are usable; reproduction is not. Each publisher's terms differ. | Permitted pattern: human supplies a document, we extract structured facts with provenance and store a short quotation plus a link. No bulk crawling of investor-relations sites. |
| **EPO OPS** | Requires registered API key; quota-limited; licence terms attach. | Only after a key is provisioned and terms recorded. |
| **WIPO PATENTSCOPE / Google Patents** | Terms do not clearly permit automated collection; Google Patents in particular is a UI, not a sanctioned bulk API. | Use EPO OPS or PatentsView instead. |
| **Commercial pharma databases** (IQVIA, Evaluate, Citeline, GlobalData, Newport, Pharmacompass) | Licensed products. Automated collection would breach contract and, for some, law. | **Not used.** Named here so nobody adds one casually. |

---

## 5. REJECTED

| Source | Reason for rejection |
| --- | --- |
| **Third-party scraper marketplaces** (e.g. Apify "Orange Book scraper" actors surfaced during research) | They re-publish data we can obtain authoritatively from FDA ourselves. Adds a dependency, an unknown refresh cadence, an unverifiable transformation step, and a provenance gap. Molecule Radar's moat is a clean evidence chain; a scraper intermediary destroys it. |
| **Screen-scraping ClinicalTrials.gov** | NLM has published specifically against it and provides an API. Using the API is both lawful and better. |
| **LLM "knowledge" as a data source** | A model's recollection of approvals, suppliers or market sizes is not evidence and cannot be cited. Structurally excluded: evidence rows require a `source_document`, and the schema cannot represent a fact whose only origin is a model. |
| **Paywalled market-size estimates** | Cannot be licensed or cited at this stage. The product shows `DATA UNAVAILABLE` rather than an invented number. |
| **Adverse-event data (FAERS) for opportunity scoring** | Available and public, but spontaneous-report data cannot support commercial-opportunity inference without serious epidemiological care. Including it would create exactly the false authority we are trying to avoid. Revisit only with a defensible methodology. |

---

## 6. How these sources attach to the entity model

```
Orange Book products.txt   → RegulatoryApplication, DrugProduct, Company(applicant), Molecule(ingredient)
Orange Book patent.txt     → Patent, Exclusivity(→ loss-of-exclusivity timing)
Orange Book exclusivity.txt→ Exclusivity
FDA DMF list               → DrugMasterFile, Company(holder), Molecule(subject)
                             ⇒ CompanyCapability(API supply, evidence-backed)
                             ⇒ DRLCapability where holder resolves to Dr. Reddy's
openFDA drugsfda           → RegulatoryApplication, RegulatorySubmission, Company(sponsor), DrugProduct
openFDA shortages          → ShortageEvent, Company(manufacturer), Molecule
ClinicalTrials.gov v2      → ClinicalTrial, Company(sponsor), Molecule(intervention), Country(location)
```

Every one of these arrows passes through entity resolution (`docs/ENTITY_RESOLUTION.md`) and
produces an `Evidence` row bound to a `SourceDocument` (`docs/EVIDENCE_MODEL.md`).

---

## 7. Register maintenance rules

1. A source may not be used by a signal until it appears here with an implemented connector.
2. Promotion to `LIVE-VERIFIED` requires a recorded response fixture committed to the repo.
3. Licensing and update frequency are re-reviewed quarterly; the review date is recorded.
4. If a publisher's terms change to prohibit automated collection, the source moves to
   RESTRICTED or REJECTED and dependent signals are disabled rather than silently degraded.
