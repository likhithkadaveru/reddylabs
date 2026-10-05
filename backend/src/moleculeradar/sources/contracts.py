"""Source contract tests.

`sources verify` answers only "can we reach it". That is not enough to trust an
ingestion layer. This module answers the harder question: **does the live source
actually match what our connectors assume?**

Each Tier-1 source declares a contract — envelope shape, record fields, date
formats, identifiers, nullability, pagination mechanism — and the runner executes
it against the live endpoint, reporting every divergence. It is the contract test
of the ingestion layer that Phase 0 could not perform, because Phase 0 had no
network access and had to work from publisher documentation alone.

Design notes that matter:

* **An unknown field is reported, never failed.** Publishers add fields; that is
  normal and is information we want, not an error. A *missing required* field is
  a failure, because a connector reads it.
* **A check that cannot be evaluated returns UNKNOWN, not PASS.** Reporting
  "passed" for something we could not test is how a contract suite becomes
  decorative.
* **Only a passing contract may promote a source to LIVE_VERIFIED.** Reachability
  alone must not, or the register starts lying about what we have validated.
* Checks are read-only and cheap: one or two small pages per source. A contract
  test that hammers a public API would be both rude and self-defeating.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from io import BytesIO
from typing import Any

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    # Could not be evaluated. Deliberately distinct from PASS.
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class CheckResult:
    check: str
    status: CheckStatus
    detail: str
    observed: Any = None
    expected: Any = None

    @property
    def ok(self) -> bool:
        return self.status in (CheckStatus.PASS, CheckStatus.WARN)


@dataclass(slots=True)
class ContractReport:
    source_key: str
    probe_url: str
    reachable: bool
    results: list[CheckResult] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    sample_record: dict[str, Any] | None = None
    error: str | None = None

    def add(self, check: str, status: CheckStatus, detail: str,
            observed: Any = None, expected: Any = None) -> None:
        self.results.append(CheckResult(check, status, detail, observed, expected))

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is CheckStatus.FAIL]

    @property
    def unknowns(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is CheckStatus.UNKNOWN]

    @property
    def passed(self) -> bool:
        """A contract passes only with zero failures AND zero unevaluated checks.

        An unevaluated check is not a pass: promoting a source on the strength of
        checks we could not run would defeat the point of running them.
        """
        return self.reachable and not self.failures and not self.unknowns

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r.status] = counts.get(r.status, 0) + 1
        return " ".join(f"{k}={v}" for k, v in sorted(counts.items()))


# ---------------------------------------------------------------------------
# Contract specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One field a connector reads.

    `path` is dotted, with `[]` marking an array to descend into:
    `results[].openfda.unii`.
    """

    path: str
    required: bool = True
    kind: str = "string"          # string | number | bool | date | datetime | array | object
    date_formats: tuple[str, ...] = ()
    nullable: bool = False
    note: str = ""


@dataclass(frozen=True, slots=True)
class PaginationSpec:
    """How to ask for the next page, and how to prove it worked."""

    style: str                    # "skip_limit" | "page_token"
    # Query parameters for page 1 and page 2.
    first_params: dict[str, Any] = field(default_factory=dict)
    # For page_token style, where the token lives in the response.
    token_path: str = ""
    # Parameter name carrying the token / offset on the next request.
    next_param: str = ""
    # Path to the identifier used to prove page 2 differs from page 1.
    identity_path: str = ""


@dataclass(frozen=True, slots=True)
class SourceContract:
    source_key: str
    probe_url: str
    kind: str                     # "json" | "zip_delimited" | "workbook"
    records_path: str = ""        # e.g. "results" or "studies"
    envelope_keys: tuple[str, ...] = ()
    fields: tuple[FieldSpec, ...] = ()
    identifiers: tuple[str, ...] = ()
    pagination: PaginationSpec | None = None
    # Where the publisher states the data's own currency.
    cadence_path: str = ""
    # For archive sources: members we require.
    archive_members: tuple[str, ...] = ()
    delimiter: str = ""
    expected_headers: tuple[str, ...] = ()
    notes: str = ""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def resolve_path(payload: Any, path: str) -> list[Any]:
    """Resolve a dotted path, descending into arrays marked `[]`.

    Returns every value found, so a missing field yields an empty list and a
    field inside an array yields one value per element.
    """
    if not path:
        return [payload]
    current: list[Any] = [payload]
    for part in path.split("."):
        descend = part.endswith("[]")
        key = part[:-2] if descend else part
        nxt: list[Any] = []
        for node in current:
            if not isinstance(node, dict) or key not in node:
                continue
            value = node[key]
            if descend:
                if isinstance(value, list):
                    nxt.extend(value)
            else:
                nxt.append(value)
        current = nxt
        if not current:
            return []
    return current


def collect_field_paths(payload: Any, prefix: str = "", depth: int = 0,
                        max_depth: int = 4) -> set[str]:
    """Every dotted field path present in a payload, for schema-drift reporting."""
    found: set[str] = set()
    if depth > max_depth:
        return found
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else key
            found.add(path)
            found |= collect_field_paths(value, path, depth + 1, max_depth)
    elif isinstance(payload, list) and payload:
        found |= collect_field_paths(payload[0], f"{prefix}[]", depth + 1, max_depth)
    return found


_TRUTHY_DATE = re.compile(r"\d")


def parse_with_formats(value: Any, formats: Iterable[str]) -> dt.date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or not _TRUTHY_DATE.search(text):
        return None
    for fmt in formats:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# The contracts
# ---------------------------------------------------------------------------

OPENFDA_ENVELOPE = ("meta", "results")

DRUGSFDA_CONTRACT = SourceContract(
    source_key="openfda_drugsfda",
    probe_url="https://api.fda.gov/drug/drugsfda.json",
    kind="json",
    records_path="results",
    envelope_keys=OPENFDA_ENVELOPE,
    cadence_path="meta.last_updated",
    fields=(
        FieldSpec("meta.disclaimer", kind="string",
                  note="Publisher caveat; we store and display it."),
        FieldSpec("meta.last_updated", kind="date", date_formats=("%Y-%m-%d", "%Y%m%d"),
                  note="Publisher's own statement of currency."),
        FieldSpec("meta.license", kind="string", required=False, nullable=True,
                  note="Licence URL; stored with provenance."),
        FieldSpec("meta.results.total", kind="number", note="Drives paging."),
        FieldSpec("meta.results.skip", kind="number", required=False,
                  note="Paging offset echoed back."),
        FieldSpec("meta.results.limit", kind="number", required=False,
                  note="Page size echoed back."),
        FieldSpec("results[].application_number", kind="string",
                  note="Primary identifier; connector keys applications on it."),
        FieldSpec("results[].sponsor_name", kind="string", nullable=True,
                  note="Resolved to a canonical company."),
        FieldSpec("results[].products", kind="array", required=False, nullable=True),
        FieldSpec("results[].submissions", kind="array", required=False, nullable=True,
                  note="Source of dated filing/approval events."),
        FieldSpec("results[].submissions[].submission_type", kind="string",
                  required=False, nullable=True,
                  note="Connector only emits events for ORIG."),
        FieldSpec("results[].submissions[].submission_number", kind="string",
                  required=False, nullable=True),
        FieldSpec("results[].submissions[].submission_status", kind="string",
                  required=False, nullable=True,
                  note="Connector treats a status starting 'AP' as an approval."),
        FieldSpec("results[].submissions[].submission_status_date", kind="date",
                  required=False, nullable=True, date_formats=("%Y%m%d", "%Y-%m-%d"),
                  note="Event date. Compact YYYYMMDD per openFDA docs."),
    ),
    identifiers=("results[].application_number",),
    pagination=PaginationSpec(
        style="skip_limit",
        first_params={"limit": 2, "skip": 0},
        next_param="skip",
        identity_path="results[].application_number",
    ),
    notes="openFDA skip-based paging; documented limit 1000/call, skip ceiling ~25000.",
)

SHORTAGES_CONTRACT = SourceContract(
    source_key="openfda_shortages",
    probe_url="https://api.fda.gov/drug/shortages.json",
    kind="json",
    records_path="results",
    envelope_keys=OPENFDA_ENVELOPE,
    cadence_path="meta.last_updated",
    fields=(
        FieldSpec("meta.last_updated", kind="date", date_formats=("%Y-%m-%d", "%Y%m%d")),
        FieldSpec("results[].status", kind="string", nullable=True,
                  note="Connector classifies Resolved/Discontinued as SHORTAGE_RESOLVED."),
        FieldSpec("results[].generic_name", kind="string", required=False, nullable=True,
                  note="Falls back to openfda.generic_name / substance_name."),
        FieldSpec("results[].initial_posting_date", kind="date", required=False,
                  nullable=True, date_formats=("%Y%m%d", "%Y-%m-%d"),
                  note="Event date for SHORTAGE_OPENED."),
        FieldSpec("results[].update_date", kind="date", required=False, nullable=True,
                  date_formats=("%Y%m%d", "%Y-%m-%d"),
                  note="Event date for SHORTAGE_RESOLVED."),
    ),
    identifiers=(),
    pagination=PaginationSpec(
        style="skip_limit",
        first_params={"limit": 2, "skip": 0},
        next_param="skip",
        identity_path="results[].generic_name",
    ),
    notes="US scope. Absence of a record does not imply adequate supply.",
)

CTGOV_CONTRACT = SourceContract(
    source_key="clinicaltrials_gov",
    probe_url="https://clinicaltrials.gov/api/v2/studies",
    kind="json",
    records_path="studies",
    envelope_keys=("studies",),
    fields=(
        FieldSpec("studies[].protocolSection.identificationModule.nctId", kind="string",
                  note="Stable primary identifier."),
        FieldSpec("studies[].protocolSection.statusModule.overallStatus", kind="string",
                  required=False, nullable=True),
        FieldSpec("studies[].protocolSection.statusModule.startDateStruct.date",
                  kind="date", required=False, nullable=True,
                  date_formats=("%Y-%m-%d", "%Y-%m", "%Y"),
                  note="Partial dates are normal; connector resolves to period start."),
        FieldSpec("studies[].protocolSection.statusModule.studyFirstPostDateStruct.date",
                  kind="date", required=False, nullable=True,
                  date_formats=("%Y-%m-%d", "%Y-%m", "%Y"),
                  note="PUBLICATION date — this is what gates temporal visibility."),
        FieldSpec("studies[].protocolSection.sponsorCollaboratorsModule.leadSponsor.name",
                  kind="string", required=False, nullable=True),
        FieldSpec("studies[].protocolSection.designModule.phases", kind="array",
                  required=False, nullable=True),
        FieldSpec("studies[].protocolSection.armsInterventionsModule.interventions",
                  kind="array", required=False, nullable=True,
                  note="Connector filters placebo and non-substance arms."),
    ),
    identifiers=("studies[].protocolSection.identificationModule.nctId",),
    pagination=PaginationSpec(
        style="page_token",
        first_params={"pageSize": 2, "countTotal": "true"},
        token_path="nextPageToken",
        next_param="pageToken",
        identity_path="studies[].protocolSection.identificationModule.nctId",
    ),
    notes="NLM terms apply. API only; screen-scraping is rejected in our register.",
)

ORANGE_BOOK_CONTRACT = SourceContract(
    source_key="fda_orange_book",
    probe_url="https://www.fda.gov/media/76860/download?attachment",
    kind="zip_delimited",
    archive_members=("products.txt", "patent.txt", "exclusivity.txt"),
    delimiter="~",
    expected_headers=(
        "Ingredient", "DF;Route", "Trade_Name", "Applicant", "Strength", "Appl_Type",
        "Appl_No", "Product_No", "TE_Code", "Approval_Date", "RLD", "RS", "Type",
        "Applicant_Full_Name",
    ),
    notes="Monthly snapshot, tilde-delimited ASCII. Parsed by header name, not position.",
)

DMF_CONTRACT = SourceContract(
    source_key="fda_dmf_list",
    probe_url="",  # Quarterly URL changes; supplied at run time.
    kind="workbook",
    expected_headers=("DMF NUMBER", "DMF TYPE", "HOLDER NAME", "SUBJECT", "STATUS"),
    notes="Quarterly workbook; the download URL changes each edition.",
)

CONTRACTS: dict[str, SourceContract] = {
    c.source_key: c
    for c in (
        ORANGE_BOOK_CONTRACT, DMF_CONTRACT, DRUGSFDA_CONTRACT,
        SHORTAGES_CONTRACT, CTGOV_CONTRACT,
    )
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class ContractTester:
    """Executes a contract against a live source via an injected HTTP client.

    The client is injected rather than constructed so the suite can be proven
    against a local mock server with no network access — which is how this
    harness itself was validated.
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    def run(self, contract: SourceContract, *, url_override: str | None = None
            ) -> ContractReport:
        url = url_override or contract.probe_url
        report = ContractReport(contract.source_key, url, reachable=False)

        if not url:
            report.error = (
                f"{contract.source_key}: no probe URL. "
                f"{contract.notes or 'Supply one with --url.'}"
            )
            report.add("reachability", CheckStatus.UNKNOWN, report.error)
            return report

        if contract.kind == "json":
            return self._run_json(contract, url, report)
        return self._run_binary(contract, url, report)

    # -- JSON APIs ---------------------------------------------------------

    def _run_json(self, contract: SourceContract, url: str,
                  report: ContractReport) -> ContractReport:
        params = dict(contract.pagination.first_params) if contract.pagination else {}
        try:
            payload = self.client.get_json(url, params=params)
            report.reachable = True
            report.add("reachability", CheckStatus.PASS, f"Fetched {url}.")
        except Exception as exc:
            report.error = f"{type(exc).__name__}: {exc}"
            report.add("reachability", CheckStatus.FAIL, report.error)
            return report

        if not isinstance(payload, dict):
            report.add("envelope", CheckStatus.FAIL,
                       f"Expected a JSON object, got {type(payload).__name__}.")
            return report

        # Envelope
        missing_env = [k for k in contract.envelope_keys if k not in payload]
        report.add(
            "envelope_keys",
            CheckStatus.PASS if not missing_env else CheckStatus.FAIL,
            ("All expected envelope keys present."
             if not missing_env else f"Missing envelope key(s): {missing_env}."),
            observed=sorted(payload.keys()), expected=list(contract.envelope_keys),
        )

        records = resolve_path(payload, contract.records_path) if contract.records_path else []
        if contract.records_path and not records:
            report.add("records_present", CheckStatus.FAIL,
                       f"No records at path {contract.records_path!r}.")
            return report
        report.add("records_present", CheckStatus.PASS,
                   f"{len(records)} record(s) at {contract.records_path!r}.")
        if records and isinstance(records[0], dict):
            report.sample_record = records[0]

        self._check_fields(contract, payload, report)
        self._check_identifiers(contract, payload, report)
        self._check_cadence(contract, payload, report)
        self._check_unknown_fields(contract, payload, report)
        self._check_pagination(contract, url, payload, report)
        self._check_limits(url, report)
        return report

    def _check_fields(self, contract: SourceContract, payload: Any,
                      report: ContractReport) -> None:
        for spec in contract.fields:
            values = resolve_path(payload, spec.path)
            if not values:
                status = CheckStatus.FAIL if spec.required else CheckStatus.WARN
                report.add(
                    f"field:{spec.path}", status,
                    ("Required field absent — a connector reads it."
                     if spec.required else "Optional field absent in this sample."),
                    expected=spec.kind,
                )
                continue

            non_null = [v for v in values if v is not None]
            if not non_null:
                status = CheckStatus.WARN if spec.nullable else CheckStatus.FAIL
                report.add(
                    f"field:{spec.path}", status,
                    ("Present but null in every sampled record (declared nullable)."
                     if spec.nullable else
                     "Present but null in every sampled record, and not declared nullable."),
                )
                continue

            if spec.kind == "date":
                bad = [v for v in non_null if parse_with_formats(v, spec.date_formats) is None]
                if bad:
                    report.add(
                        f"date_format:{spec.path}", CheckStatus.FAIL,
                        f"Value(s) do not parse with any expected format "
                        f"{list(spec.date_formats)}.",
                        observed=bad[:3], expected=list(spec.date_formats),
                    )
                else:
                    report.add(f"date_format:{spec.path}", CheckStatus.PASS,
                               f"Parses with {list(spec.date_formats)}.",
                               observed=non_null[:2])
            elif spec.kind == "array":
                bad = [v for v in non_null if not isinstance(v, list)]
                report.add(
                    f"field:{spec.path}",
                    CheckStatus.PASS if not bad else CheckStatus.FAIL,
                    "Array as expected." if not bad else "Expected an array.",
                    observed=type(non_null[0]).__name__,
                )
            elif spec.kind == "number":
                bad = [v for v in non_null if not isinstance(v, (int, float))]
                report.add(
                    f"field:{spec.path}",
                    CheckStatus.PASS if not bad else CheckStatus.FAIL,
                    "Numeric as expected." if not bad else "Expected a number.",
                    observed=non_null[:2],
                )
            else:
                report.add(f"field:{spec.path}", CheckStatus.PASS, "Present.",
                           observed=str(non_null[0])[:80])

    def _check_identifiers(self, contract: SourceContract, payload: Any,
                           report: ContractReport) -> None:
        if not contract.identifiers:
            report.add("identifiers", CheckStatus.WARN,
                       "This source declares no stable record identifier.")
            return
        for path in contract.identifiers:
            values = [v for v in resolve_path(payload, path) if v is not None]
            if not values:
                report.add(f"identifier:{path}", CheckStatus.FAIL, "Identifier absent.")
                continue
            duplicates = len(values) - len(set(map(str, values)))
            report.add(
                f"identifier:{path}",
                CheckStatus.PASS if duplicates == 0 else CheckStatus.WARN,
                ("Present and unique within the page."
                 if duplicates == 0 else
                 f"{duplicates} duplicate identifier(s) within one page — "
                 f"ingestion dedupe keys must absorb this."),
                observed=values[:3],
            )

    def _check_cadence(self, contract: SourceContract, payload: Any,
                       report: ContractReport) -> None:
        if not contract.cadence_path:
            report.add("source_timestamp", CheckStatus.WARN,
                       "Source publishes no currency timestamp; freshness must be "
                       "inferred from our own retrieval.")
            return
        values = resolve_path(payload, contract.cadence_path)
        if not values or values[0] is None:
            report.add("source_timestamp", CheckStatus.FAIL,
                       f"Expected a currency timestamp at {contract.cadence_path!r}.")
            return
        parsed = parse_with_formats(values[0], ("%Y-%m-%d", "%Y%m%d"))
        if parsed is None:
            report.add("source_timestamp", CheckStatus.FAIL,
                       "Currency timestamp did not parse.", observed=values[0])
            return
        age = (dt.date.today() - parsed).days
        report.add(
            "source_timestamp",
            CheckStatus.PASS if age <= 120 else CheckStatus.WARN,
            f"Publisher states data current to {parsed.isoformat()} ({age} days ago).",
            observed=parsed.isoformat(),
        )

    def _check_unknown_fields(self, contract: SourceContract, payload: Any,
                              report: ContractReport) -> None:
        """Report fields the live source has that our contract does not mention.

        A WARN, never a FAIL: publishers add fields, and learning about them early
        is the point of running this.
        """
        # Exact declarations: anything we explicitly named is known.
        exact = {s.path for s in contract.fields} | set(contract.envelope_keys)
        exact |= {contract.records_path, contract.cadence_path}
        exact = {d.replace("[]", "") for d in exact if d}

        # Only fields we declared as OPAQUE containers suppress their descendants.
        # The envelope and the records container must NOT, or every record field
        # would be treated as known and the drift check would be blind to exactly
        # the fields most worth watching.
        opaque = {
            s.path.replace("[]", "")
            for s in contract.fields
            if s.kind in ("array", "object")
        }

        observed = collect_field_paths(payload)
        unknown = sorted(
            p for p in observed
            if (flat := p.replace("[]", "")) not in exact
            and not any(flat.startswith(o + ".") for o in opaque)
        )
        report.unknown_fields = unknown[:60]
        report.add(
            "unexpected_fields",
            CheckStatus.PASS if not unknown else CheckStatus.WARN,
            ("No fields beyond the declared contract."
             if not unknown else
             f"{len(unknown)} field(s) present that the contract does not declare. "
             f"Not an error — publishers add fields — but review whether any are useful."),
            observed=unknown[:15],
        )

    def _check_pagination(self, contract: SourceContract, url: str, first: Any,
                          report: ContractReport) -> None:
        spec = contract.pagination
        if spec is None:
            report.add("pagination", CheckStatus.UNKNOWN,
                       "No pagination declared for this source.")
            return

        first_ids = [str(v) for v in resolve_path(first, spec.identity_path) if v is not None]
        params = dict(spec.first_params)

        if spec.style == "page_token":
            tokens = resolve_path(first, spec.token_path)
            token = tokens[0] if tokens else None
            if not token:
                report.add(
                    "pagination", CheckStatus.WARN,
                    f"No {spec.token_path!r} in the first page. Either the result set "
                    f"fits one page, or the paging mechanism has changed.",
                )
                return
            params[spec.next_param] = token
        else:
            params[spec.next_param] = len(first_ids) or 1

        try:
            second = self.client.get_json(url, params=params)
        except Exception as exc:
            report.add("pagination", CheckStatus.FAIL,
                       f"Second page request failed: {type(exc).__name__}: {exc}")
            return

        second_ids = [str(v) for v in resolve_path(second, spec.identity_path)
                      if v is not None]
        if not second_ids:
            report.add("pagination", CheckStatus.WARN,
                       "Second page returned no records; result set may be small.")
            return

        overlap = set(first_ids) & set(second_ids)
        if overlap:
            report.add(
                "pagination", CheckStatus.FAIL,
                f"Page 2 repeats {len(overlap)} identifier(s) from page 1. Ingestion "
                f"would re-process records; dedupe keys protect the database but the "
                f"paging assumption is wrong.",
                observed=sorted(overlap)[:5],
            )
        else:
            report.add("pagination", CheckStatus.PASS,
                       f"Page 2 advanced cleanly ({spec.style}); no overlap with page 1.",
                       observed={"page1": first_ids[:2], "page2": second_ids[:2]})

        report.add(
            "duplicate_behaviour",
            CheckStatus.PASS if not overlap else CheckStatus.WARN,
            ("No duplicate records observed across page boundary."
             if not overlap else
             "Duplicates observed across pages; connector dedupe keys must absorb them."),
        )

    def _check_limits(self, url: str, report: ContractReport) -> None:
        """Record any rate-limit headers the publisher exposes.

        Reported rather than asserted: we deliberately do not probe a limit by
        tripping it, which would be rude to a shared public service.
        """
        try:
            response = self.client.get(url, params={"limit": 1})
        except Exception as exc:
            report.add("api_limits", CheckStatus.UNKNOWN,
                       f"Could not read response headers: {type(exc).__name__}: {exc}")
            return
        headers = {k.lower(): v for k, v in dict(response.headers).items()}
        interesting = {k: v for k, v in headers.items()
                       if "ratelimit" in k or "retry-after" in k}
        report.add(
            "api_limits",
            CheckStatus.PASS if interesting else CheckStatus.WARN,
            (f"Publisher exposes rate-limit headers: {interesting}."
             if interesting else
             "Publisher exposes no rate-limit headers; our client stays conservative "
             "(serialised, backoff, no retry on non-429 4xx)."),
            observed=interesting or None,
        )

    # -- archive / workbook sources ---------------------------------------

    def _run_binary(self, contract: SourceContract, url: str,
                    report: ContractReport) -> ContractReport:
        try:
            content, response = self.client.get_bytes(url)
            report.reachable = True
            report.add("reachability", CheckStatus.PASS,
                       f"Fetched {len(content)} bytes from {url}.")
        except Exception as exc:
            report.error = f"{type(exc).__name__}: {exc}"
            report.add("reachability", CheckStatus.FAIL, report.error)
            return report

        # httpx lowercases keys when a Headers object is cast to dict, so a
        # "Last-Modified" lookup on that dict always misses. Read case-insensitively.
        last_modified = None
        if response is not None:
            headers = {k.lower(): v for k, v in dict(response.headers).items()}
            last_modified = headers.get("last-modified")
        report.add(
            "publication_date",
            CheckStatus.PASS if last_modified else CheckStatus.WARN,
            (f"Last-Modified: {last_modified}. Used as the artefact's publication date, "
             f"which gates temporal visibility."
             if last_modified else
             "No Last-Modified header; publication date falls back to retrieval time, "
             "which weakens historical gating for this source."),
            observed=last_modified,
        )

        if contract.kind == "zip_delimited":
            self._check_zip(contract, content, report)
        else:
            self._check_workbook(contract, content, report)
        return report

    def _check_zip(self, contract: SourceContract, content: bytes,
                   report: ContractReport) -> None:
        try:
            archive = zipfile.ZipFile(BytesIO(content))
        except zipfile.BadZipFile as exc:
            report.add("archive", CheckStatus.FAIL, f"Not a readable ZIP: {exc}")
            return
        names = archive.namelist()
        report.add("archive", CheckStatus.PASS, f"Readable ZIP with {len(names)} member(s).",
                   observed=names)

        lowered = {n.lower(): n for n in names}
        for member in contract.archive_members:
            match = next((v for k, v in lowered.items() if k.endswith(member)), None)
            if match is None:
                report.add(f"member:{member}", CheckStatus.FAIL,
                           f"Expected member {member!r} not in archive.", observed=names)
                continue
            raw = archive.read(match)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
                report.add(f"encoding:{member}", CheckStatus.WARN,
                           "Not valid UTF-8; fell back to latin-1 as the connector does.")
            header_line = text.splitlines()[0] if text else ""
            headers = [h.strip() for h in header_line.split(contract.delimiter)]
            report.add(f"member:{member}", CheckStatus.PASS,
                       f"Present, {len(text.splitlines())} line(s).")

            if member == "products.txt" and contract.expected_headers:
                missing = [h for h in contract.expected_headers if h not in headers]
                extra = [h for h in headers if h not in contract.expected_headers]
                report.add(
                    "headers:products.txt",
                    CheckStatus.PASS if not missing else CheckStatus.FAIL,
                    ("All expected columns present."
                     if not missing else f"Missing column(s): {missing}."),
                    observed=headers, expected=list(contract.expected_headers),
                )
                if extra:
                    report.add("unexpected_fields", CheckStatus.WARN,
                               f"New column(s) in products.txt: {extra}.", observed=extra)
                    report.unknown_fields = extra

    def _check_workbook(self, contract: SourceContract, content: bytes,
                        report: ContractReport) -> None:
        from .fda_dmf import map_headers, read_tabular

        try:
            headers, rows = read_tabular(content)
        except Exception as exc:
            report.add("workbook", CheckStatus.FAIL,
                       f"Unreadable workbook: {type(exc).__name__}: {exc}")
            return
        report.add("workbook", CheckStatus.PASS,
                   f"Readable; {len(headers)} column(s), {len(rows)} row(s).",
                   observed=headers)
        mapping = map_headers(headers)
        missing = [f for f in ("dmf_number", "holder", "subject") if f not in mapping]
        report.add(
            "headers:dmf", CheckStatus.PASS if not missing else CheckStatus.FAIL,
            ("Required columns mapped." if not missing
             else f"Could not map required field(s): {missing}."),
            observed=headers, expected=list(contract.expected_headers),
        )


def render_markdown(reports: list[ContractReport]) -> str:
    """A report an engineer can read and a reviewer can diff."""
    lines = [
        "# Source Contract Test Report",
        "",
        f"Run: {dt.datetime.now(dt.UTC).isoformat()}",
        "",
        "Verifies that live sources match the assumptions our connectors encode.",
        "An UNKNOWN result is a check that could not be evaluated — it is not a pass.",
        "",
        "| Source | Reachable | Result | Checks |",
        "| --- | --- | --- | --- |",
    ]
    for r in reports:
        verdict = "PASS" if r.passed else ("FAIL" if r.failures else "INCOMPLETE")
        lines.append(
            f"| `{r.source_key}` | {'yes' if r.reachable else 'no'} | {verdict} | "
            f"{r.summary() or '—'} |"
        )
    for r in reports:
        lines += ["", f"## {r.source_key}", "", f"Probe: `{r.probe_url or '(none)'}`", ""]
        if r.error:
            lines += [f"**Error:** {r.error}", ""]
        for c in r.results:
            lines.append(f"- **{c.status}** `{c.check}` — {c.detail}")
            if c.observed is not None:
                lines.append(f"  - observed: `{json.dumps(c.observed, default=str)[:300]}`")
        if r.unknown_fields:
            lines += ["", "Fields present but not declared in our contract:", ""]
            lines += [f"  - `{f}`" for f in r.unknown_fields[:40]]
    return "\n".join(lines) + "\n"
