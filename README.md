# Molecule Radar

**Global pharmaceutical opportunity intelligence — by Llama Labs**

Molecule Radar continuously scans public pharmaceutical information to surface two
kinds of opportunity for a focus company (configured here as Dr. Reddy's
Laboratories):

* **SELL** — APIs the focus company publicly appears able to supply, together with
  companies exhibiting public signals consistent with potential demand.
* **BUILD** — molecules the focus company does not publicly supply, surfaced for
  portfolio investigation on the basis of exclusivity timing, observed supplier
  concentration, developer momentum and capability adjacency.

It is built entirely on publicly accessible or appropriately licensed external
data. There is no private Dr. Reddy's data anywhere in it. That is deliberate: the
objective is to be able to show a pharmaceutical company real opportunities found
without access to their systems.

---

## The rule that shapes everything

**Truth over visual impressiveness.**

* Counts are always scoped: "two API suppliers *identified in Molecule Radar's
  current public-source coverage*", never "only two suppliers exist".
* `DATA UNAVAILABLE` and `NO QUALIFYING SIGNALS` are different statements and the
  product distinguishes them everywhere. Missing data is never scored as zero.
* **Opportunity Score** (how interesting) and **Evidence Confidence** (how well
  evidenced) are separate numbers and are never blended.
* Every opportunity carries a **counter-thesis** derived from the same evidence.
  They are shown side by side and never netted off.
* Every material claim resolves to a source document with a checksum, a retrieval
  timestamp, and the publisher's own licence and disclaimer.
* An LLM may summarise and extract. It can never be the origin of a fact — the
  schema has no shape for that.

---

## Current status, stated plainly

> **No source has ever been contacted from this environment.** Every
> pharmaceutical, regulatory and scientific host is denied by this environment's
> network egress policy (HTTP 403 at CONNECT). The connectors, schema, resolution,
> scoring, API and UI are implemented and tested, but the system has never ingested
> a real record.

All five Tier-1 sources are therefore `DOC_VERIFIED` (contract read from the
publisher's documentation) rather than `LIVE_VERIFIED` (response observed). The
product shows `DATA UNAVAILABLE` rather than placeholder content, and
`moleculeradar sources verify` reports the blocked hosts honestly.

**To unblock:** allow outbound HTTPS to `www.fda.gov`, `api.fda.gov` and
`clinicaltrials.gov`, then run `moleculeradar sources verify`.

See `docs/KNOWN_LIMITATIONS.md` for the full picture, including the limitations
that remain after egress opens.

---

## Quick start

```bash
createdb moleculeradar

cd backend
uv venv .venv && . .venv/bin/activate
uv pip install -e ".[dev]"
cp ../ops/.env.example .env          # set MR_HTTP_CONTACT_EMAIL
alembic upgrade head

moleculeradar sources verify         # run this FIRST
moleculeradar sources ingest all
moleculeradar focus bootstrap
moleculeradar engine run
moleculeradar quality check

uvicorn moleculeradar.api.app:create_app --factory --port 8000
```

```bash
cd frontend && npm install && npm run dev     # http://localhost:3000
```

---

## The data

The smallest set capable of producing a legitimate first signal, chosen in
Phase 0 (`docs/DATA_SOURCE_REGISTER.md`):

| Source | Gives us |
| --- | --- |
| **FDA Orange Book** | Loss-of-exclusivity timing, patents, applicants |
| **FDA Drug Master File list** | Observed API supplier concentration, **and the focus company's own API capability from a first-party regulatory record** |
| **openFDA Drugs@FDA** | Dated filing and approval activity per applicant |
| **openFDA Drug Shortages** | Supply stress events |
| **ClinicalTrials.gov v2** | Development activity, sponsors, geographies |

The DMF list is the linchpin. It means the Dr. Reddy's capability graph rests on
FDA filings rather than on scraping drreddys.com — stronger provenance and no
terms-of-use risk. (drreddys.com is recorded RESTRICTED: its terms could not be
retrieved to verify whether automated collection is permitted.)

---

## Repository

```
backend/
  src/moleculeradar/
    core/          config, enums, canonical models, DB, temporal gate
    sources/       connectors (fetch → parse → load), HTTP client, registry
    resolution/    normalisation, entity resolution, focus bootstrap
    signals/       features, signal definitions, scoring, skeptic, engine
    llm/           investigation synthesis
    quality/       19 data-quality checks
    api/           FastAPI app, routers, schemas, security
    cli/           operator command line
  migrations/      Alembic
  tests/           248 tests: unit, integration, connector, end-to-end
frontend/          Next.js 15 terminal UI
docs/              10 documents, including KNOWN_LIMITATIONS.md
ops/               Docker, environment contract
```

## Documentation

| Document | Covers |
| --- | --- |
| `docs/DATA_SOURCE_REGISTER.md` | Every source, its licence, cadence, status and **verification level** |
| `docs/DATA_MODEL.md` | The ontology, and which entities were deliberately collapsed |
| `docs/ENTITY_RESOLUTION.md` | The resolution ladder and why fuzzy can never auto-merge |
| `docs/OPPORTUNITY_ENGINE.md` | Features, signals, point allocations, the two numbers |
| `docs/EVIDENCE_MODEL.md` | The claim → evidence → document → source chain |
| `docs/BACKTESTING.md` | Leakage safety and what is not yet possible |
| `docs/ARCHITECTURE.md` | Stack, boundaries, and what we deliberately did not build |
| `docs/SECURITY.md` | Auth, RBAC, boot guard, audit |
| `docs/DEPLOYMENT.md` | Running it, scheduling it, backing it up |
| `docs/KNOWN_LIMITATIONS.md` | Everything that would embarrass us if a customer found it first |

## Tests

```bash
cd backend && pytest                 # 248 tests, needs Postgres
cd frontend && npm run typecheck && npm run build
```

Fixture scenarios assert exact score decompositions, that re-runs are idempotent,
that the focus company is never its own counterparty, that a crowded molecule is
not promoted, and that facts published after the as-of date are invisible to the
engine. The end-to-end test drives synthetic artefacts through the **real**
connectors so resolution, capability derivation, scoring and the skeptic are all
exercised as they would be in production.

## Licence and data use

Molecule Radar uses only sources whose terms permit automated collection, and
records each source's licence and attribution alongside the data. Publisher
disclaimers are reproduced rather than stripped. Nothing in this product asserts a
commercial relationship between any companies, and nothing in it constitutes
legal, regulatory or investment advice.
