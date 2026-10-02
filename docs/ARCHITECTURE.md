# Architecture

---

## 1. Shape

A **modular monolith**. One Python package, one database, one API, one frontend.

```
                   ┌──────────────────────────────────────────┐
  public sources   │  sources/   connectors (fetch→parse→load) │
  (FDA, NLM)  ───► │      ↓                                    │
                   │  resolution/   entity resolution          │
                   │      ↓                                    │
                   │  core/      canonical schema + temporal   │
                   │      ↓                                    │
                   │  signals/   features → signals → scores   │
                   │      ↓                                    │
                   │  llm/       investigation synthesis       │
                   │      ↓                                    │
                   │  api/       FastAPI, auth, RBAC           │
                   └──────────────┬───────────────────────────┘
                                  ▼
                        Next.js server components
```

### What we deliberately did not build

Kafka, Kubernetes, Neo4j, Elasticsearch, microservices. None is justified by the
data volumes or the functionality:

* **Graph database?** The queries are joins over a few million rows with known
  shapes, not arbitrary traversals. Postgres with the right indexes is faster to
  build, easier to reason about, and transactional.
* **Search engine?** Molecule and company search is prefix and trigram matching
  over tens of thousands of rows. Postgres handles it. Revisit if full-text over
  label documents becomes a feature.
* **Message queue?** Ingestion is a handful of scheduled batch jobs. A queue would
  add failure modes without removing any.
* **Microservices?** There is one team and one deployable. Splitting now would buy
  distributed-systems problems and sell nothing.

The modules above are real boundaries — `signals/` does not import from
`sources/`, `api/` does not reach into connectors — so extraction later is
mechanical if scale ever demands it.

## 2. Stack

| Layer | Choice | Why |
| --- | --- | --- |
| Database | PostgreSQL 16 | JSONB for payloads and features, arrays, CHECK constraints, real transactions. The CHECK constraints carry product rules. |
| ORM / migrations | SQLAlchemy 2.0 + Alembic | Typed models, explicit migrations. |
| Backend | Python 3.11, FastAPI, Pydantic v2 | The data stack is Python; the API and ingestion share one type system. |
| CLI | Typer + Rich | Operators need `sources verify` more than a web console. |
| Frontend | Next.js 15 (App Router), TypeScript strict | Server components keep the API URL and token out of the browser bundle. |
| Auth | argon2id + JWT | Argon2 has no 72-byte truncation limit (see §6). |
| Tests | pytest against real Postgres | The engine depends on JSONB, arrays, UNION and CHECK semantics. Testing that on SQLite would prove nothing. |

## 3. Data flow

**raw → normalized → canonical**, with provenance recorded at each step.

1. `fetch()` retrieves bytes and records a `SourceDocument` with a SHA-256.
   An unchanged checksum short-circuits the whole load.
2. `load()` parses defensively, resolves entities, writes canonical rows each
   carrying an `evidence_id`, and emits dated `Event` rows.
3. The engine computes features under the temporal gate, applies signals, and
   stores opportunities with their full decomposition.

Properties: idempotent (dedupe keys), transactional (a failure leaves data
unchanged rather than half-written), and quarantining (a record we cannot parse is
recorded with a reason, never dropped and never fatal).

## 4. Key boundaries

* **`core/temporal.py` is the only place date filtering is expressed.** Every
  historical claim depends on it, so there is exactly one place to audit.
* **`signals/definitions.py` is the only place user-visible signal text is
  generated.** Language rules are therefore testable.
* **`resolution/engine.py` is the only path to a canonical entity.** No connector
  creates a company or molecule directly.
* **`sources/base.py` owns provenance.** A connector cannot write a canonical row
  without an evidence chain, because the helpers to do so do not exist.

## 5. Conventions worth knowing

* **Enum columns are VARCHAR, not native Postgres enums.** Adding a value to a
  native enum needs a migration and a lock, and we expect to add event and signal
  types often. The consequence: values round-trip as `str`, so compare with `==`,
  never `is`. StrEnum makes `==` correct.
* **`None` never means zero.** It means unknown, all the way from feature to API
  to UI.
* **Surrogate UUID primary keys everywhere.** No pharmaceutical name is stable
  enough to be an identity.
* **The focus company is configuration, not schema.** Nothing is hard-wired to
  Dr. Reddy's, so running for another client is configuration plus ingestion.

## 6. Decisions worth recording

**argon2id over bcrypt.** bcrypt silently truncates input at 72 bytes, so a long
passphrase is weaker than it looks, and passlib's bcrypt backend is incompatible
with current bcrypt releases. Found by a failing test.

**No app at import time.** `api/app.py` exposes `create_app()` and does not build
an app on import, because `create_app()` refuses to run under invalid production
configuration and an import-time raise makes the module impossible to import for
tests or tooling. Served with `uvicorn ... --factory`.

**Parse by header name, never by position.** Positional parsing of a reordered
tilde-delimited file shifts every value one column across and produces
confidently wrong data — the hardest failure to notice and the most damaging.

**Quarantine must never raise.** Postgres rejects NUL in text and JSONB, so
malformed binary input used to crash the run *while being quarantined*. Text is
now sanitised on the way into storage.

## 7. Scaling expectations

Full Orange Book is ~50k product rows; the DMF list ~40k; Drugs@FDA ~30k
applications; ClinicalTrials.gov ~500k studies. Events accumulate at perhaps
~100k/year. This is comfortably within a single Postgres instance for years. The
first thing to become slow will be per-molecule feature computation across the
whole corpus, which is a batch job and parallelises trivially by molecule.
