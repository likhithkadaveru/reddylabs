# Live Validation Attempt 001

**Date:** 2026-10-05
**Objective:** take Molecule Radar from "working software architecture" to
"working pharmaceutical intelligence product" by proving it against real data.
**Outcome:** **the validation could not be executed.** No real pharmaceutical
data could be obtained from this environment.

This document records exactly what was attempted, what was found, and why no
`GOLDEN_OPPORTUNITY_001` exists.

---

## 1. Step 1 — verify real sources

### 1.1 The command

```
$ moleculeradar sources contract-test
Contract test: fda_orange_book
Contract test: fda_dmf_list
Contract test: openfda_drugsfda
Contract test: openfda_shortages
Contract test: clinicaltrials_gov

│ fda_orange_book    │ no  │ FAIL       │ FAIL=1    │
│ fda_dmf_list       │ no  │ INCOMPLETE │ UNKNOWN=1 │
│ openfda_drugsfda   │ no  │ FAIL       │ FAIL=1    │
│ openfda_shortages  │ no  │ FAIL       │ FAIL=1    │
│ clinicaltrials_gov │ no  │ FAIL       │ FAIL=1    │

exit code 1
```

Every source fails at reachability. Full output: `CONTRACT_TEST_REPORT.md`.

### 1.2 Independent verification, not merely trusting the command

The instruction was explicit: do not merely trust the command. Each host was
probed directly and by a second, independent transport.

| Route | Result |
| --- | --- |
| `curl` from the container | `CONNECT tunnel failed, response 403` on every host |
| Server-side fetch (does not use the container proxy) | `EGRESS_BLOCKED` on every host |
| `moleculeradar sources verify` | `BLOCKED_BY_POLICY` ×5, exit 1 |
| `moleculeradar sources contract-test` | reachability FAIL ×4, UNKNOWN ×1, exit 1 |

### 1.3 Blast radius

Probed to establish whether this is a pharma-specific block or a general one:

**Blocked (403 at CONNECT):** `api.fda.gov`, `www.fda.gov`, `clinicaltrials.gov`,
`eutils.ncbi.nlm.nih.gov`, `dailymed.nlm.nih.gov`, `rxnav.nlm.nih.gov`,
`data.gov`, `healthdata.gov`, `ema.europa.eu`, `ebi.ac.uk`, `api.crossref.org`,
`registry.opendata.aws`, `huggingface.co`, `download.open.fda.gov` — **and also
`example.com` and `wikipedia.org`.**

**Reachable:** `github.com`, `raw.githubusercontent.com`, `api.github.com`,
`codeload.github.com`, `pypi.org`, `files.pythonhosted.org`,
`registry.npmjs.org`, and the generic `s3.amazonaws.com` /
`storage.googleapis.com` endpoints (which returned routing and permission errors,
not data).

The proxy reports `selective: false` with an allowlist containing only package
registries and Anthropic endpoints. GitHub was added between sessions, which is
why `git push` now succeeds while every data source remains blocked.

**Conclusion: this is a strict allowlist, not a pharma-specific denial.** The
blocked set includes `example.com`. No amount of connector work can change it.

### 1.4 What this means for the contract tests

The contract checks the brief asked for — pagination, identifiers, null
handling, date formats, unexpected fields, API limits, retries, source
timestamps, publication dates, duplicate behaviour, historical records — **have
not been performed against live endpoints.** Every connector field mapping
remains `DOC_VERIFIED`: taken from publisher documentation, never observed.

No Phase 0 assumption was confirmed or refuted, because nothing was observed.

---

## 2. Why no substitute data was used

Three routes to data exist from the reachable hosts. All were rejected, and the
reasoning matters more than the conclusion.

**GitHub mirrors of FDA datasets.** `raw.githubusercontent.com` is reachable and
such mirrors exist. Rejected because the vintage is unknown, the transformation
from the FDA original is unverifiable, and — decisively — **FDA itself is
blocked, so the mirror could not be checked against the authority.** Step 6
requires verifying every important claim against the original source. That is
impossible here by construction.

**PyPI packages bundling FDA data.** Same provenance problem, plus staleness.

**S3 / GCS open-data buckets.** `aws-roda-hcls-datalake` returned 403 (no public
listing); the speculative GCS buckets returned 404. `registry.opendata.aws`,
which would let us find a real one, is blocked.

The deeper reason is not procedural. A `GOLDEN_OPPORTUNITY_001` built on an
unverifiable mirror would fail its own Step 6 validation — I would have to reject
it myself in the next step. Producing it would consume a cycle to manufacture
something destined for the bin, and would put a number in front of a DRL analyst
whose provenance answer is "a GitHub copy of FDA data, vintage unknown, which we
could not check against FDA".

That is precisely the failure mode this product exists to prevent. The register
(`DATA_SOURCE_REGISTER.md` §5) already rejects third-party redistribution for
exactly this reason.

---

## 3. Steps 2–9: not executed

| Step | Status | Reason |
| --- | --- | --- |
| 2 — Ingest real data | **Not executed** | No source reachable. The production database remains empty; it has never held a non-test record. |
| 3 — Verify entity resolution against real names | **Not executed** | Requires real company and molecule literals. Fixture data cannot expose the assumptions real data would. |
| 4 — Build the real DRL profile | **Not executed** | Requires the FDA DMF list. No capability may be asserted without it. |
| 5 — Run the opportunity engine on real data | **Not executed** | No real candidates exist to evaluate. |
| 6 — Find and attack the first real opportunity | **Not executed** | Nothing to attack. |
| 7 — `GOLDEN_OPPORTUNITY_001` | **Does not exist** | Correctly so. Creating one from synthetic data would be fabrication. |
| 8 — Verify the product experience on real data | **Not executed** | The UI has been verified against synthetic data only. |
| 9 — Measure the system | **Zero across every real-data metric** | See below. |

### Step 9 measurements, truthfully

```
records ingested, by source ........ 0  (all five sources)
canonical molecules ................ 0
canonical companies ................ 0
events ............................. 0
evidence records ................... 0
entity-resolution decisions ........ 0
opportunity candidates evaluated ... 0
qualifying opportunities ........... 0
false positives found in review .... 0  (nothing to review)
broken evidence links .............. 0  (no evidence exists)
source freshness ................... never retrieved, all five sources
ingestion failures ................. 5  (all reachability, all environmental)
```

Small truthful numbers were acceptable. These are zero, and zero is the truth.

---

## 4. What was built instead

Step 1 asks for a contract test of the entire ingestion layer. It could not run,
but it could be **built and proven**, so that the moment egress opens it executes
in one command.

`moleculeradar sources contract-test [--report PATH] [--promote]`

Each Tier-1 source now declares a machine-checkable contract: envelope shape,
record fields, date formats and their accepted patterns, identifiers,
nullability, pagination mechanism, and the publisher's own currency timestamp.
The runner executes it live and reports every divergence.

Design decisions worth recording:

* **An unknown field is reported, never failed.** Publishers add fields; that is
  information we want. A *missing required* field is a failure, because a
  connector reads it.
* **A check that could not be evaluated returns UNKNOWN, not PASS**, and a
  contract with any UNKNOWN does not pass. Reporting "passed" for something
  untested is how a contract suite becomes decorative.
* **Only a passing contract may promote a source to `LIVE_VERIFIED`.**
  Reachability alone must not, or the register starts claiming validation we have
  not performed.
* Rate limits are **reported, not probed**. Discovering a limit by tripping it
  would be rude to a shared public service.

### 4.1 The harness is proven, not merely written

32 tests run it against a **real local HTTP server** — real requests, real
headers, real pagination round-trips, using the production HTTP client — serving
conformant responses and then deliberately mutated ones. Each planted fault must
be caught as the specific failure it is:

| Planted fault | Required detection |
| --- | --- |
| Required identifier removed | FAIL on that field |
| Unparseable date format | FAIL on `date_format:<path>` |
| New undeclared publisher field | WARN, field listed — never FAIL |
| Page 2 repeats page 1's records | FAIL on `pagination` |
| `nextPageToken` absent | WARN |
| Publisher currency timestamp removed | FAIL |
| Timestamp 400 days stale | WARN |
| Null in a nullable field | WARN, not FAIL |
| Wrong envelope shape | FAIL |
| Orange Book column dropped | FAIL naming the column |
| Archive member missing | FAIL |
| Corrupt archive | FAIL |
| `Last-Modified` absent | WARN (publication date would fall back to retrieval) |

### 4.2 Two bugs the harness found in itself

Both would have made it report false confidence:

1. **The schema-drift check was blind to record fields.** `records_path`
   (`"results"`) was in the declared set, and the suppression rule excluded any
   path beneath a declared one — so every field inside every record was treated as
   known. The check would have reported "no unexpected fields" no matter what the
   publisher changed. Only explicitly-declared *opaque containers* now suppress
   their descendants.
2. **`dict(response.headers)` lowercases keys in httpx**, so the `Last-Modified`
   lookup always missed and every archive source would have been reported as
   having no publication date.

A third failure was my own test being wrong rather than the code: the "bad date"
fixture used `2021-03-03`, which the contract legitimately accepts.

---

## 5. Verdict

**NOT PROVEN.**

With one essential qualification, because the distinction is the whole point:

> The opportunity methodology **did not fail against real data. It was never
> exposed to real data.**

"NOT PROVEN" is the correct verdict because nothing has been proven — we cannot
claim Molecule Radar works. But the cause is environmental, not methodological,
and the report would be false if it implied the engine had been tried and found
wanting.

What remains true and what remains unknown:

**Established (software):** 282 tests pass. The pipeline runs end to end through
the real connectors. Scores decompose arithmetically. Leakage gating is tested.
Entity resolution refuses to merge on name similarity alone. The UI renders real
database state and shows `DATA UNAVAILABLE` rather than inventing content.

**Unknown (product):** whether our connector field mappings match reality;
whether entity resolution survives real pharmaceutical naming; whether the DRL
capability graph can be built from the live DMF list; whether the engine surfaces
anything a DRL analyst would act on; and whether any opportunity would survive
Step 6 scrutiny.

The central question — *can Molecule Radar discover something about the
pharmaceutical market that would cause a Dr. Reddy's analyst to investigate an
opportunity they might otherwise have missed?* — **remains unanswered.** It
cannot be answered from an environment with no access to pharmaceutical data.

---

## 6. The single blocker

Allow outbound HTTPS to three hosts:

```
www.fda.gov
api.fda.gov
clinicaltrials.gov
```

(Cloud environment menu → Edit → Network access.)

Then:

```bash
moleculeradar sources contract-test --report docs/validation/CONTRACT_TEST_REPORT.md
```

That executes the full Step 1 verification and reports every divergence from our
assumptions. Expect divergences — the mappings have never been checked — and
expect to fix connectors before Step 2. That is what the first live run is for.
