# Source Contract Test Report

Run: 2026-10-05T04:22:04.495488+00:00

Verifies that live sources match the assumptions our connectors encode.
An UNKNOWN result is a check that could not be evaluated — it is not a pass.

| Source | Reachable | Result | Checks |
| --- | --- | --- | --- |
| `fda_orange_book` | no | FAIL | FAIL=1 |
| `fda_dmf_list` | no | INCOMPLETE | UNKNOWN=1 |
| `openfda_drugsfda` | no | FAIL | FAIL=1 |
| `openfda_shortages` | no | FAIL | FAIL=1 |
| `clinicaltrials_gov` | no | FAIL | FAIL=1 |

## fda_orange_book

Probe: `https://www.fda.gov/media/76860/download?attachment`

**Error:** SourceUnavailableError: www.fda.gov: ProxyError: 403 Forbidden

- **FAIL** `reachability` — SourceUnavailableError: www.fda.gov: ProxyError: 403 Forbidden

## fda_dmf_list

Probe: `(none)`

**Error:** fda_dmf_list: no probe URL. Quarterly workbook; the download URL changes each edition.

- **UNKNOWN** `reachability` — fda_dmf_list: no probe URL. Quarterly workbook; the download URL changes each edition.

## openfda_drugsfda

Probe: `https://api.fda.gov/drug/drugsfda.json`

**Error:** SourceUnavailableError: api.fda.gov: ProxyError: 403 Forbidden

- **FAIL** `reachability` — SourceUnavailableError: api.fda.gov: ProxyError: 403 Forbidden

## openfda_shortages

Probe: `https://api.fda.gov/drug/shortages.json`

**Error:** SourceUnavailableError: api.fda.gov: ProxyError: 403 Forbidden

- **FAIL** `reachability` — SourceUnavailableError: api.fda.gov: ProxyError: 403 Forbidden

## clinicaltrials_gov

Probe: `https://clinicaltrials.gov/api/v2/studies`

**Error:** SourceUnavailableError: clinicaltrials.gov: ProxyError: 403 Forbidden

- **FAIL** `reachability` — SourceUnavailableError: clinicaltrials.gov: ProxyError: 403 Forbidden
