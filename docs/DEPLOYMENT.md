# Deployment

---

## 1. Local development

```bash
# Database
createdb moleculeradar

# Backend
cd backend
uv venv .venv && . .venv/bin/activate
uv pip install -e ".[dev]"
cp ../ops/.env.example .env        # set MR_HTTP_CONTACT_EMAIL
alembic upgrade head

# Check what is actually reachable BEFORE anything else
moleculeradar sources verify

# Ingest, profile, score
moleculeradar sources ingest fda_orange_book
moleculeradar sources ingest fda_dmf_list --url "<current quarterly XLS URL>"
moleculeradar sources ingest openfda_drugsfda --max-records 5000
moleculeradar sources ingest openfda_shortages
moleculeradar sources ingest clinicaltrials_gov
moleculeradar focus bootstrap
moleculeradar engine run
moleculeradar quality check

# Serve
uvicorn moleculeradar.api.app:create_app --factory --reload --port 8000
```

```bash
# Frontend
cd frontend
npm install
cp .env.example .env.local         # MR_API_BASE_URL
npm run dev
```

**`moleculeradar sources verify` is the first command to run in any new
environment.** It probes every source host and exits non-zero if any is
unreachable, naming the hosts an egress policy must allow.

## 2. Configuration

Entirely environment-driven; see `ops/.env.example`. Required in production:
`MR_DATABASE_URL`, `MR_JWT_SECRET` (≥32 chars), `MR_HTTP_CONTACT_EMAIL`,
`MR_RAW_STORAGE_BACKEND=s3` with `MR_RAW_STORAGE_BUCKET`,
`MR_CORS_ALLOWED_ORIGINS` (no wildcard). The application refuses to boot if any
is missing or unsafe.

## 3. Containers

`ops/docker-compose.yml` runs Postgres, the API and the frontend for local use.
`ops/Dockerfile.backend` and `ops/Dockerfile.frontend` are multi-stage and run as
a non-root user.

## 4. Scheduling

Ingestion is batch work driven by the CLI. Cadences matching the publishers:

| Job | Cadence |
| --- | --- |
| `sources verify` | hourly — catches an egress or publisher change early |
| `sources ingest fda_orange_book` | weekly (published monthly; checksum short-circuits an unchanged file) |
| `sources ingest fda_dmf_list --url ...` | monthly (published quarterly; the URL changes each edition, so this one needs a human) |
| `sources ingest openfda_drugsfda` | daily |
| `sources ingest openfda_shortages` | daily |
| `sources ingest clinicaltrials_gov` | daily |
| `engine run` | after ingestion completes |
| `quality check` | after `engine run` — **and it gates publishing** |

`quality check` exits non-zero on any ERROR-severity failure. Wire that exit code
so a failing check stops the refreshed feed from being published: the product may
be presenting something false.

A failed ingestion leaves data unchanged rather than half-written, so a retry is
always safe.

## 5. Health probes

* `/healthz` — liveness. Deliberately does **not** touch the database: a liveness
  probe that fails on a database blip causes a restart storm that makes the
  outage worse.
* `/readyz` — readiness. Requires a working database connection.

## 6. Backups

Postgres is the only durable state that cannot be rebuilt: raw payloads can be
re-fetched, but **our accumulated snapshot and event history cannot** — it is the
moat. Nightly `pg_dump`, 30-day retention, and a restore rehearsed quarterly. A
backup that has never been restored is a hypothesis.

## 7. Migrations

Alembic, forward-only in production. Run `alembic upgrade head` as a release step
before the new application version starts.

## 8. Observability

`ingestion_run` records status, counts, duration, errors and quarantine totals
per job; `quarantined_record` holds what was refused and why;
`data_quality_check_result` holds each check's history. All three surface on the
Data Health screen. Alert on: a FAILED run, any ERROR-severity quality failure,
source freshness beyond twice its cadence, and a growing review-queue backlog.

## 9. First production run

Treat it as a contract test, not a data load. No connector has been validated
against live data (see `KNOWN_LIMITATIONS.md` §1). Run each source once with
`--dry-run`, inspect quarantine reasons, then load for real.
