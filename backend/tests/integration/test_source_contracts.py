"""Contract-test harness validation.

The harness exists to catch divergence between a live source and what our
connectors assume. A harness that cannot catch a planted fault is worse than
none, because it manufactures false confidence.

So these tests run it against a **real local HTTP server** — using the real
IngestionHttpClient, real requests, real headers, real pagination round-trips —
serving both conformant responses and deliberately mutated ones, and assert that
each specific fault is detected as the specific failure it is.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from moleculeradar.sources.contracts import (
    CONTRACTS,
    CheckStatus,
    ContractTester,
    collect_field_paths,
    parse_with_formats,
    render_markdown,
    resolve_path,
)
from moleculeradar.sources.http import IngestionHttpClient

pytestmark = pytest.mark.integration

TODAY = dt.date.today()


# ---------------------------------------------------------------------------
# Mock source server
# ---------------------------------------------------------------------------


def _drugsfda_page(app_numbers: list[str], *, last_updated: str | None = None,
                   drop_field: str | None = None, bad_date: bool = False,
                   extra_field: bool = False, null_sponsor: bool = False) -> dict:
    results = []
    for n in app_numbers:
        rec = {
            "application_number": n,
            "sponsor_name": None if null_sponsor else f"SPONSOR {n} LTD",
            "products": [{"product_number": "001",
                          "active_ingredients": [{"name": "TESTAZOLE"}]}],
            "submissions": [{
                "submission_type": "ORIG",
                "submission_number": "1",
                "submission_status": "AP",
                # A genuinely unparseable value. "2021-03-03" would NOT do: the
            # contract accepts both compact and ISO forms, so it is conformant.
            "submission_status_date": "March 3, 2021" if bad_date else "20210303",
            }],
        }
        if extra_field:
            rec["brand_new_publisher_field"] = "surprise"
        if drop_field == "application_number":
            rec.pop("application_number")
        results.append(rec)

    meta = {
        "disclaimer": "Do not rely on openFDA to make decisions regarding medical care.",
        "license": "https://open.fda.gov/license/",
        "last_updated": last_updated or TODAY.isoformat(),
        "results": {"skip": 0, "limit": 2, "total": 100},
    }
    if drop_field == "meta.last_updated":
        meta.pop("last_updated")
    return {"meta": meta, "results": results}


def _ctgov_page(nct_ids: list[str], *, token: str | None = None) -> dict:
    studies = [{
        "protocolSection": {
            "identificationModule": {"nctId": n, "briefTitle": f"Study {n}"},
            "statusModule": {
                "overallStatus": "RECRUITING",
                "startDateStruct": {"date": "2025-06"},
                "studyFirstPostDateStruct": {"date": "2025-07-01"},
            },
            "designModule": {"phases": ["PHASE3"]},
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": "OMEGA LTD"}},
            "armsInterventionsModule": {
                "interventions": [{"type": "DRUG", "name": "Testazole"}]
            },
        }
    } for n in nct_ids]
    payload: dict = {"studies": studies, "totalCount": 50}
    if token:
        payload["nextPageToken"] = token
    return payload


def _orange_book_zip(*, drop_column: str | None = None,
                     drop_member: str | None = None) -> bytes:
    headers = list(CONTRACTS["fda_orange_book"].expected_headers)
    if drop_column:
        headers.remove(drop_column)
    row = {h: "X" for h in headers}
    if "Ingredient" in row:
        row["Ingredient"] = "TESTAZOLE"
    if "Appl_No" in row:
        row["Appl_No"] = "021111"
    products = "~".join(headers) + "\n" + "~".join(row[h] for h in headers) + "\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if drop_member != "products.txt":
            z.writestr("products.txt", products)
        if drop_member != "patent.txt":
            z.writestr("patent.txt", "Appl_Type~Appl_No~Patent_No\nN~021111~9111111\n")
        if drop_member != "exclusivity.txt":
            z.writestr("exclusivity.txt", "Appl_Type~Appl_No~Exclusivity_Code\nN~021111~NCE\n")
    return buf.getvalue()


class MockSourceHandler(BaseHTTPRequestHandler):
    """Serves whatever `MockSourceHandler.mode` dictates."""

    mode = "conformant"

    def log_message(self, *args):  # silence test output
        pass

    def _json(self, payload: dict, headers: dict | None = None):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, body: bytes, headers: dict | None = None):
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        mode = MockSourceHandler.mode

        if parsed.path == "/drugsfda":
            skip = int(qs.get("skip", ["0"])[0])
            if mode == "conformant":
                ids = ["ANDA001", "ANDA002"] if skip == 0 else ["ANDA003", "ANDA004"]
                return self._json(_drugsfda_page(ids),
                                  {"X-RateLimit-Limit": "240", "X-RateLimit-Remaining": "239"})
            if mode == "missing_identifier":
                return self._json(_drugsfda_page(["ANDA001"], drop_field="application_number"))
            if mode == "bad_date":
                return self._json(_drugsfda_page(["ANDA001"], bad_date=True))
            if mode == "extra_field":
                return self._json(_drugsfda_page(["ANDA001", "ANDA002"], extra_field=True))
            if mode == "pagination_repeats":
                return self._json(_drugsfda_page(["ANDA001", "ANDA002"]))
            if mode == "stale":
                old = (TODAY - dt.timedelta(days=400)).isoformat()
                ids = ["ANDA001", "ANDA002"] if skip == 0 else ["ANDA003"]
                return self._json(_drugsfda_page(ids, last_updated=old))
            if mode == "no_timestamp":
                ids = ["ANDA001", "ANDA002"] if skip == 0 else ["ANDA003"]
                return self._json(_drugsfda_page(ids, drop_field="meta.last_updated"))
            if mode == "null_sponsor":
                ids = ["ANDA001", "ANDA002"] if skip == 0 else ["ANDA003"]
                return self._json(_drugsfda_page(ids, null_sponsor=True))
            if mode == "bad_envelope":
                return self._json({"data": []})
            if mode == "no_headers":
                ids = ["ANDA001", "ANDA002"] if skip == 0 else ["ANDA003"]
                return self._json(_drugsfda_page(ids))

        if parsed.path == "/ctgov":
            token = qs.get("pageToken", [None])[0]
            if mode == "conformant":
                if token is None:
                    return self._json(_ctgov_page(["NCT001", "NCT002"], token="TOKEN2"))
                return self._json(_ctgov_page(["NCT003", "NCT004"]))
            if mode == "no_token":
                return self._json(_ctgov_page(["NCT001"]))
            if mode == "pagination_repeats":
                return self._json(_ctgov_page(["NCT001", "NCT002"], token="TOKEN2"))

        if parsed.path == "/orangebook":
            hdrs = {"Last-Modified": "Fri, 14 Aug 2026 12:00:00 GMT"}
            if mode == "conformant":
                return self._bytes(_orange_book_zip(), hdrs)
            if mode == "missing_column":
                return self._bytes(_orange_book_zip(drop_column="Applicant_Full_Name"), hdrs)
            if mode == "missing_member":
                return self._bytes(_orange_book_zip(drop_member="patent.txt"), hdrs)
            if mode == "not_a_zip":
                return self._bytes(b"this is not a zip", hdrs)
            if mode == "no_last_modified":
                return self._bytes(_orange_book_zip())

        self.send_response(404)
        self.end_headers()


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), MockSourceHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.fixture
def tester():
    # Real client: this exercises the actual HTTP path, headers and retries.
    client = IngestionHttpClient(allow_without_contact=True)
    yield ContractTester(client)
    client.close()


def _by_check(report, prefix: str):
    return [r for r in report.results if r.check.startswith(prefix)]


def _status(report, check: str) -> CheckStatus | None:
    for r in report.results:
        if r.check == check:
            return r.status
    return None


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


class TestPathResolution:
    def test_descends_nested_arrays(self) -> None:
        payload = {"results": [
            {"subs": [{"t": "ORIG"}, {"t": "SUPPL"}]},
            {"subs": [{"t": "ORIG"}]},
        ]}
        assert resolve_path(payload, "results[].subs[].t") == ["ORIG", "SUPPL", "ORIG"]

    def test_missing_path_is_empty_not_error(self) -> None:
        assert resolve_path({"a": 1}, "b.c") == []

    def test_collects_field_paths_for_drift_reporting(self) -> None:
        paths = collect_field_paths({"meta": {"x": 1}, "results": [{"a": {"b": 2}}]})
        assert "meta.x" in paths
        assert "results[].a.b" in paths

    @pytest.mark.parametrize(
        ("value", "formats", "expected"),
        [
            ("20210303", ("%Y%m%d",), dt.date(2021, 3, 3)),
            ("2025-06", ("%Y-%m-%d", "%Y-%m"), dt.date(2025, 6, 1)),
            ("2025", ("%Y-%m-%d", "%Y-%m", "%Y"), dt.date(2025, 1, 1)),
            ("not a date", ("%Y%m%d",), None),
            ("", ("%Y%m%d",), None),
            (None, ("%Y%m%d",), None),
        ],
    )
    def test_date_parsing(self, value, formats, expected) -> None:
        assert parse_with_formats(value, formats) == expected


# ---------------------------------------------------------------------------
# Conformant source passes
# ---------------------------------------------------------------------------


class TestConformantSource:
    def test_json_contract_passes_cleanly(self, server, tester) -> None:
        MockSourceHandler.mode = "conformant"
        report = tester.run(CONTRACTS["openfda_drugsfda"],
                            url_override=f"{server}/drugsfda")
        assert report.reachable
        assert not report.failures, [f"{f.check}: {f.detail}" for f in report.failures]
        assert not report.unknowns, [u.check for u in report.unknowns]
        assert report.passed

    def test_identifier_uniqueness_checked(self, server, tester) -> None:
        MockSourceHandler.mode = "conformant"
        report = tester.run(CONTRACTS["openfda_drugsfda"],
                            url_override=f"{server}/drugsfda")
        ident = _status(report, "identifier:results[].application_number")
        assert ident is CheckStatus.PASS

    def test_rate_limit_headers_are_recorded(self, server, tester) -> None:
        """We report the publisher's limits; we never probe them by tripping one."""
        MockSourceHandler.mode = "conformant"
        report = tester.run(CONTRACTS["openfda_drugsfda"],
                            url_override=f"{server}/drugsfda")
        assert _status(report, "api_limits") is CheckStatus.PASS
        limits = next(r for r in report.results if r.check == "api_limits")
        assert "x-ratelimit-limit" in (limits.observed or {})

    def test_page_token_pagination_passes(self, server, tester) -> None:
        MockSourceHandler.mode = "conformant"
        report = tester.run(CONTRACTS["clinicaltrials_gov"],
                            url_override=f"{server}/ctgov")
        assert _status(report, "pagination") is CheckStatus.PASS
        assert _status(report, "duplicate_behaviour") is CheckStatus.PASS

    def test_partial_dates_accepted_for_ctgov(self, server, tester) -> None:
        """YYYY-MM is normal in the registry and must not be a failure."""
        MockSourceHandler.mode = "conformant"
        report = tester.run(CONTRACTS["clinicaltrials_gov"],
                            url_override=f"{server}/ctgov")
        start = _status(
            report,
            "date_format:studies[].protocolSection.statusModule.startDateStruct.date",
        )
        assert start is CheckStatus.PASS


# ---------------------------------------------------------------------------
# Planted faults must be caught
# ---------------------------------------------------------------------------


class TestPlantedFaultsAreCaught:
    def test_missing_required_identifier_fails(self, server, tester) -> None:
        MockSourceHandler.mode = "missing_identifier"
        report = tester.run(CONTRACTS["openfda_drugsfda"],
                            url_override=f"{server}/drugsfda")
        assert report.failures
        assert not report.passed
        assert any("application_number" in f.check for f in report.failures)

    def test_wrong_date_format_fails(self, server, tester) -> None:
        """A date the connector cannot parse would silently lose every event."""
        MockSourceHandler.mode = "bad_date"
        report = tester.run(CONTRACTS["openfda_drugsfda"],
                            url_override=f"{server}/drugsfda")
        date_failures = [f for f in report.failures if f.check.startswith("date_format")]
        assert date_failures, [r.check for r in report.results]
        assert "submission_status_date" in date_failures[0].check

    def test_unexpected_field_is_warned_not_failed(self, server, tester) -> None:
        """Publishers add fields. That is information, not an error."""
        MockSourceHandler.mode = "extra_field"
        report = tester.run(CONTRACTS["openfda_drugsfda"],
                            url_override=f"{server}/drugsfda")
        assert _status(report, "unexpected_fields") is CheckStatus.WARN
        assert any("brand_new_publisher_field" in f for f in report.unknown_fields)
        assert not any(f.check == "unexpected_fields" for f in report.failures)

    def test_repeating_pagination_fails(self, server, tester) -> None:
        """Page 2 returning page 1's records means our paging assumption is wrong."""
        MockSourceHandler.mode = "pagination_repeats"
        report = tester.run(CONTRACTS["openfda_drugsfda"],
                            url_override=f"{server}/drugsfda")
        assert _status(report, "pagination") is CheckStatus.FAIL
        assert not report.passed

    def test_ctgov_repeating_pagination_fails(self, server, tester) -> None:
        MockSourceHandler.mode = "pagination_repeats"
        report = tester.run(CONTRACTS["clinicaltrials_gov"],
                            url_override=f"{server}/ctgov")
        assert _status(report, "pagination") is CheckStatus.FAIL

    def test_absent_page_token_is_warned(self, server, tester) -> None:
        MockSourceHandler.mode = "no_token"
        report = tester.run(CONTRACTS["clinicaltrials_gov"],
                            url_override=f"{server}/ctgov")
        assert _status(report, "pagination") is CheckStatus.WARN

    def test_stale_publisher_timestamp_is_warned(self, server, tester) -> None:
        MockSourceHandler.mode = "stale"
        report = tester.run(CONTRACTS["openfda_drugsfda"],
                            url_override=f"{server}/drugsfda")
        assert _status(report, "source_timestamp") is CheckStatus.WARN

    def test_missing_publisher_timestamp_fails(self, server, tester) -> None:
        """Losing the currency timestamp costs us the freshness check entirely."""
        MockSourceHandler.mode = "no_timestamp"
        report = tester.run(CONTRACTS["openfda_drugsfda"],
                            url_override=f"{server}/drugsfda")
        assert _status(report, "source_timestamp") is CheckStatus.FAIL

    def test_null_in_nullable_field_is_warned_not_failed(self, server, tester) -> None:
        MockSourceHandler.mode = "null_sponsor"
        report = tester.run(CONTRACTS["openfda_drugsfda"],
                            url_override=f"{server}/drugsfda")
        assert _status(report, "field:results[].sponsor_name") is CheckStatus.WARN
        assert not any("sponsor_name" in f.check for f in report.failures)

    def test_wrong_envelope_fails(self, server, tester) -> None:
        MockSourceHandler.mode = "bad_envelope"
        report = tester.run(CONTRACTS["openfda_drugsfda"],
                            url_override=f"{server}/drugsfda")
        assert _status(report, "envelope_keys") is CheckStatus.FAIL
        assert not report.passed

    def test_unreachable_source_fails_and_does_not_pass(self, server, tester) -> None:
        report = tester.run(CONTRACTS["openfda_drugsfda"],
                            url_override=f"{server}/does-not-exist")
        assert not report.reachable
        assert not report.passed
        assert _status(report, "reachability") is CheckStatus.FAIL


class TestArchiveContract:
    def test_conformant_orange_book_passes(self, server, tester) -> None:
        MockSourceHandler.mode = "conformant"
        report = tester.run(CONTRACTS["fda_orange_book"],
                            url_override=f"{server}/orangebook")
        assert report.reachable
        assert not report.failures, [f"{f.check}: {f.detail}" for f in report.failures]
        assert _status(report, "headers:products.txt") is CheckStatus.PASS
        assert _status(report, "publication_date") is CheckStatus.PASS

    def test_missing_column_fails(self, server, tester) -> None:
        """A dropped column is exactly what positional parsing would hide."""
        MockSourceHandler.mode = "missing_column"
        report = tester.run(CONTRACTS["fda_orange_book"],
                            url_override=f"{server}/orangebook")
        assert _status(report, "headers:products.txt") is CheckStatus.FAIL
        failure = next(f for f in report.failures if f.check == "headers:products.txt")
        assert "Applicant_Full_Name" in failure.detail

    def test_missing_member_fails(self, server, tester) -> None:
        MockSourceHandler.mode = "missing_member"
        report = tester.run(CONTRACTS["fda_orange_book"],
                            url_override=f"{server}/orangebook")
        assert _status(report, "member:patent.txt") is CheckStatus.FAIL

    def test_corrupt_archive_fails(self, server, tester) -> None:
        MockSourceHandler.mode = "not_a_zip"
        report = tester.run(CONTRACTS["fda_orange_book"],
                            url_override=f"{server}/orangebook")
        assert _status(report, "archive") is CheckStatus.FAIL

    def test_absent_last_modified_is_warned(self, server, tester) -> None:
        """Without it, publication date falls back to retrieval time, weakening
        historical gating — worth knowing, not fatal."""
        MockSourceHandler.mode = "no_last_modified"
        report = tester.run(CONTRACTS["fda_orange_book"],
                            url_override=f"{server}/orangebook")
        assert _status(report, "publication_date") is CheckStatus.WARN


class TestReportSemantics:
    def test_unknown_is_not_a_pass(self, server, tester) -> None:
        """A contract with an unevaluated check must not be reported as passing."""
        MockSourceHandler.mode = "conformant"
        report = tester.run(CONTRACTS["fda_dmf_list"])  # no probe URL configured
        assert _status(report, "reachability") is CheckStatus.UNKNOWN
        assert not report.passed
        assert report.unknowns

    def test_markdown_renders_every_source(self, server, tester) -> None:
        MockSourceHandler.mode = "conformant"
        reports = [
            tester.run(CONTRACTS["openfda_drugsfda"], url_override=f"{server}/drugsfda"),
            tester.run(CONTRACTS["fda_orange_book"], url_override=f"{server}/orangebook"),
        ]
        md = render_markdown(reports)
        assert "# Source Contract Test Report" in md
        assert "openfda_drugsfda" in md
        assert "fda_orange_book" in md
        assert "UNKNOWN result is a check that could not be evaluated" in md
