# Security

Molecule Radar ingests only public data, but the **opportunity set a company is
looking at is itself commercially sensitive**. Knowing which molecules Dr. Reddy's
is investigating is valuable to a competitor. The system is therefore built with
real authentication from the start — retrofitting auth is how products end up
with holes.

---

## 1. Authentication

* **argon2id** password hashing. Chosen over bcrypt because bcrypt silently
  truncates input at 72 bytes (a long passphrase is weaker than it looks) and
  because passlib's bcrypt backend is incompatible with current bcrypt releases.
* **JWT bearer tokens**, HS256, expiry configurable (default 12h).
* `verify_password` fails closed: a malformed stored hash returns False rather
  than raising an exception a caller might mistake for a server fault.
* **Login does not enumerate accounts** — unknown user and wrong password return
  the identical message.

## 2. Authorisation

Capability-based, RBAC-ready. Roles map to capability sets in
`api/security.py`; when roles need per-resource permissions the map becomes a
table without changing call sites.

| Role | Capabilities |
| --- | --- |
| `viewer` | read opportunities, molecules, companies, evidence, white space |
| `analyst` | viewer + create investigations |
| `admin` | analyst + data health, review queue, audit |

Data Health and the review queue are admin-only: they expose operational detail
an analyst does not need, and tests assert an analyst receives 403.

## 3. Production boot guard

`create_app()` **refuses to start** in production if any of these hold:

* `MR_JWT_SECRET` unset or shorter than 32 characters
* `MR_AUTH_ENABLED=false`
* `MR_HTTP_CONTACT_EMAIL` unset (several public APIs require a contactable agent)
* `MR_RAW_STORAGE_BACKEND=filesystem` (not durable)
* wildcard CORS origin

A production deployment missing its secret should fail loudly, not serve. This is
tested.

## 4. Transport and headers

Every response carries `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
`Cross-Origin-Opener-Policy: same-origin`, a restrictive
`Content-Security-Policy` (`default-src 'none'; frame-ancestors 'none'`), a
`Permissions-Policy` denying geolocation/microphone/camera, and HSTS in
production. The CSP is restrictive because the API serves JSON only.

Interactive API docs (`/docs`, `/openapi.json`) are disabled in production.

## 5. Rate limiting and input validation

Per-IP rate limiting via slowapi, configurable (default 120/min). All input is
validated by Pydantic; path parameters are parsed as UUIDs and a malformed id
returns 400, not 500.

## 6. Error handling

The unhandled-exception handler logs the detail and returns only
`{"detail": "Internal server error."}`. An internal error message can leak schema
and query structure.

## 7. Audit logging

`audit_log_entry` records actor, action, resource, detail and IP for **reads as
well as writes**. Who looked at which opportunity is exactly the question that
matters if a leak is ever investigated.

## 8. Secrets

Never in source control. `.gitignore` excludes `.env`; `ops/.env.example`
documents the contract with empty values. Configuration is environment-only via
pydantic-settings. No credential appears in any log line, error message or API
response.

## 9. Outbound requests

A single HTTP client governs all egress: a contactable User-Agent (ingestion
refuses to start without `MR_HTTP_CONTACT_EMAIL`), conservative serialised rate
limiting, exponential backoff, `Retry-After` honoured on 429, and **no retry on
other 4xx** — a 403 means we are not permitted and hammering it is both rude and
useless.

## 10. Dependency scanning

`pip-audit` is a dev dependency and runs in CI. `npm audit` runs for the frontend.

## 11. Not yet done

* No refresh tokens or token revocation list; tokens are short-lived instead.
* No MFA.
* No field-level encryption (nothing ingested is personal or secret).
* No per-tenant isolation — single-tenant by design today.
* Audit log has no tamper-evidence (append-only table, no hash chain).
