"""API tests.

Three things matter most here and are tested hardest:

1. **Empty states tell the truth.** DATA_UNAVAILABLE and NO_QUALIFYING_SIGNALS are
   different statements and the API must not conflate them.
2. **Auth is enforced**, and the production boot guard actually refuses to start.
3. **Scoped counts carry their scope**, so a client cannot render a bare number.
"""

from __future__ import annotations

import datetime as dt
import importlib

import pytest
from fastapi.testclient import TestClient
from tests.fixtures import builders as b

from moleculeradar.core.models import User
from moleculeradar.core.temporal import TemporalContext
from moleculeradar.signals.engine import OpportunityEngine

pytestmark = pytest.mark.integration

UTC = dt.UTC
AS_OF = dt.date(2026, 1, 1)
PUBLISHED = dt.datetime(2025, 12, 1, tzinfo=UTC)
RETRIEVED = dt.datetime(2025, 12, 15, tzinfo=UTC)


@pytest.fixture
def client(session, monkeypatch):
    """App wired to the test session, with auth disabled for most tests."""
    monkeypatch.setenv("MR_AUTH_ENABLED", "false")
    monkeypatch.setenv("MR_ENVIRONMENT", "development")

    from moleculeradar.core import config

    config.get_settings.cache_clear()

    from moleculeradar.api import app as app_module

    importlib.reload(app_module)
    application = app_module.create_app()

    from moleculeradar.core.db import get_db

    def _override():
        yield session

    application.dependency_overrides[get_db] = _override
    with TestClient(application) as c:
        yield c
    config.get_settings.cache_clear()


@pytest.fixture
def populated(session):
    """A small real graph with one BUILD opportunity."""
    ta = b.make_therapeutic_area(session, "ta_api")
    src_ob = b.make_source(session, "api_ob")
    src_dmf = b.make_source(session, "api_dmf")
    src_ct = b.make_source(session, "api_ct")
    doc_ob = b.make_document(session, src_ob, retrieved_at=RETRIEVED, published_at=PUBLISHED)
    doc_dmf = b.make_document(session, src_dmf, retrieved_at=RETRIEVED, published_at=PUBLISHED)
    doc_ct = b.make_document(session, src_ct, retrieved_at=RETRIEVED, published_at=PUBLISHED)

    molecule = b.make_molecule(session, "Apiazole", therapeutic_area_id=ta.id)
    focus = b.make_company(session, "Dr. Reddy's Laboratories Ltd", is_focus=True)
    competitor = b.make_company(session, "Rival Synthetics Ltd")
    sponsor = b.make_company(session, "Sponsor Alpha Ltd")

    b.make_dmf(session, doc_dmf, molecule=molecule, holder=competitor,
               dmf_number="DMF-API-1", published_at=PUBLISHED)
    b.make_exclusivity(session, doc_ob, molecule=molecule,
                       expiry=dt.date(2028, 7, 1), published_at=PUBLISHED)
    for i, start in enumerate([dt.date(2025, 3, 1), dt.date(2025, 7, 1)]):
        b.make_trial(session, doc_ct, molecule=molecule, sponsor=sponsor,
                     nct_id=f"NCT5500000{i}", start_date=start, published_at=PUBLISHED)
    # Adjacent capability so capability_adjacency can fire.
    adj = b.make_molecule(session, "Apiadjacent One", therapeutic_area_id=ta.id)
    from moleculeradar.core.enums import CapabilityType

    b.make_capability(session, doc_dmf, company=focus,
                      capability_type=CapabilityType.API_SUPPLY, molecule=adj)
    session.flush()

    OpportunityEngine(session, TemporalContext(as_of_date=AS_OF)).run([molecule.id])
    session.flush()
    return molecule


class TestEmptyStates:
    def test_status_says_there_is_no_data(self, client) -> None:
        body = client.get("/api/v1/status").json()
        assert body["has_any_data"] is False
        assert body["molecule_count"] == 0
        assert "holds no ingested source data" in body["message"]
        # Must not imply freshness it does not have.
        assert body["last_intelligence_refresh"] is None

    def test_feed_distinguishes_unavailable_from_no_signals(self, client, session) -> None:
        """The two empty states are different statements and must not be conflated."""
        empty = client.get("/api/v1/feed").json()
        assert empty["availability"] == "DATA_UNAVAILABLE"
        assert "No source data has been ingested" in empty["message"]

        # Now add a molecule but run no engine: data exists, signals do not.
        b.make_molecule(session, "Unscoredazole")
        session.flush()
        no_signals = client.get("/api/v1/feed").json()
        assert no_signals["availability"] == "NO_QUALIFYING_SIGNALS"
        assert "engine has not been run" in no_signals["message"]

    def test_feed_includes_source_status_so_ui_can_explain_the_gap(self, client) -> None:
        body = client.get("/api/v1/feed").json()
        assert "source_status" in body

    def test_whitespace_empty_state(self, client) -> None:
        body = client.get("/api/v1/whitespace").json()
        assert body["availability"] == "DATA_UNAVAILABLE"
        assert body["rows"] == []
        # The scope note is present even when empty, so a client cannot learn to
        # render rows without it.
        assert "not complete global censuses" in body["scope_note"]


class TestFeed:
    def test_feed_returns_opportunity_with_decomposition(self, client, populated) -> None:
        body = client.get("/api/v1/feed").json()
        assert body["availability"] == "AVAILABLE"
        assert body["total"] >= 1
        assert body["last_intelligence_refresh"] is not None

        build = body["build"]
        assert build, body
        opp = build[0]
        assert opp["opportunity_score"] > 0
        assert opp["evidence_confidence"] in ("HIGH", "MEDIUM", "LOW", "INSUFFICIENT")
        assert opp["top_signals"]
        assert "surfaced for portfolio investigation" in opp["headline"]

    def test_score_and_confidence_are_separate_fields(self, client, populated) -> None:
        opp = client.get("/api/v1/feed").json()["build"][0]
        assert "opportunity_score" in opp
        assert "evidence_confidence" in opp
        assert "evidence_confidence_score" in opp
        # There is no single blended number anywhere in the payload.
        assert "combined_score" not in opp
        assert "overall_score" not in opp

    def test_opportunity_detail_lists_unevaluated_signals(self, client, populated) -> None:
        """A reader must be able to tell a genuine negative from missing data."""
        opp_id = client.get("/api/v1/feed").json()["build"][0]["id"]
        detail = client.get(f"/api/v1/feed/opportunity/{opp_id}").json()
        assert detail["signals"]
        assert "unevaluated_signals" in detail
        reasons = {u["reason"] for u in detail["unevaluated_signals"]}
        assert reasons <= {"missing_data", "did_not_fire"}
        for u in detail["unevaluated_signals"]:
            assert u["rationale"], "an unevaluated signal must still explain its purpose"

    def test_opportunity_detail_includes_counter_thesis(self, client, populated) -> None:
        opp_id = client.get("/api/v1/feed").json()["build"][0]["id"]
        detail = client.get(f"/api/v1/feed/opportunity/{opp_id}").json()
        assert "counter_theses" in detail
        assert isinstance(detail["counter_theses"], list)

    def test_signal_points_sum_to_the_returned_score(self, client, populated) -> None:
        opp_id = client.get("/api/v1/feed").json()["build"][0]["id"]
        detail = client.get(f"/api/v1/feed/opportunity/{opp_id}").json()
        total = sum(s["points"] for s in detail["signals"] if s["direction"] == "SUPPORTING")
        assert abs(total - detail["opportunity_score"]) < 0.05

    def test_malformed_id_is_a_400_not_a_500(self, client) -> None:
        assert client.get("/api/v1/feed/opportunity/not-a-uuid").status_code == 400

    def test_unknown_id_is_404(self, client) -> None:
        import uuid

        r = client.get(f"/api/v1/feed/opportunity/{uuid.uuid4()}")
        assert r.status_code == 404


class TestScopedCounts:
    def test_molecule_counts_carry_their_scope_note(self, client, populated) -> None:
        """A client must not be able to render a bare supplier count."""
        detail = client.get(f"/api/v1/molecules/{populated.id}").json()
        suppliers = detail["observed_api_suppliers"]
        assert suppliers["value"] == 1
        assert "current public-source coverage" in suppliers["scope_note"]
        assert "not a complete global supplier census" in suppliers["scope_note"]
        assert suppliers["availability"] == "AVAILABLE"

    def test_missing_source_yields_data_unavailable_not_zero(self, client, populated) -> None:
        """No shortage data was ingested, so the count must be null, not 0.

        Returning 0 would say "no shortages" when the truth is "we did not look".
        """
        detail = client.get(f"/api/v1/molecules/{populated.id}").json()
        shortages = detail["shortage_events_36m"]
        assert shortages["value"] is None
        assert shortages["availability"] == "DATA_UNAVAILABLE"
        assert "has not been ingested" in shortages["scope_note"]

    def test_molecule_exposes_coverage_map(self, client, populated) -> None:
        detail = client.get(f"/api/v1/molecules/{populated.id}").json()
        assert detail["coverage"]["dmf"] is True
        assert detail["coverage"]["shortages"] is False


class TestEvidenceChain:
    def test_evidence_resolves_to_a_source_document(self, client, populated) -> None:
        opp_id = client.get("/api/v1/feed").json()["build"][0]["id"]
        detail = client.get(f"/api/v1/feed/opportunity/{opp_id}").json()
        evidence_ids = [
            eid for s in detail["signals"] for eid in (s["evidence_ids"] or [])
        ]
        assert evidence_ids, "signals must cite evidence"

        ev = client.get(f"/api/v1/evidence/{evidence_ids[0]}").json()
        assert ev["claim_text"]
        assert ev["source_document"]["sha256"]
        assert ev["source_document"]["retrieved_at"]
        assert ev["source_document"]["publisher"]
        assert ev["extraction_method"] == "deterministic_parse"
        # Not model-derived, so no audit record.
        assert ev["llm_audit"] is None

    def test_missing_evidence_is_404_with_an_explicit_warning(self, client) -> None:
        import uuid

        r = client.get(f"/api/v1/evidence/{uuid.uuid4()}")
        assert r.status_code == 404
        assert "must not be treated as supported" in r.json()["detail"]

    def test_batch_evidence_fetch(self, client, populated) -> None:
        opp_id = client.get("/api/v1/feed").json()["build"][0]["id"]
        detail = client.get(f"/api/v1/feed/opportunity/{opp_id}").json()
        ids = [eid for s in detail["signals"] for eid in (s["evidence_ids"] or [])][:3]
        r = client.get("/api/v1/evidence", params=[("ids", i) for i in ids])
        assert r.status_code == 200
        assert len(r.json()) == len(ids)


class TestWhiteSpace:
    def test_rows_are_returned_with_scope(self, client, populated) -> None:
        body = client.get("/api/v1/whitespace").json()
        assert body["availability"] == "AVAILABLE"
        assert body["total"] >= 1
        row = body["rows"][0]
        assert row["focus_api_status"] in (
            "HOLDS_ACTIVE_DMF", "NO_DMF_IDENTIFIED", "UNKNOWN"
        )

    def test_csv_export_carries_the_scope_note_and_no_bare_blanks(
        self, client, populated
    ) -> None:
        """A CSV is where a number gets detached from its qualification."""
        r = client.get("/api/v1/whitespace/export.csv")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        text = r.text
        assert "# Scope:" in text
        assert "not complete global censuses" in text
        # Missing values must read DATA_UNAVAILABLE, never an empty cell that a
        # spreadsheet would interpret as zero.
        assert "DATA_UNAVAILABLE" in text

    def test_zero_suppliers_is_a_real_zero_when_dmf_data_exists(
        self, client, session, populated
    ) -> None:
        """With DMF data ingested, no DMF rows for a molecule is a true zero.

        "No active Type II holders identified" is a genuine finding once we have
        looked, and such a molecule legitimately matches a max_suppliers=0 filter.
        """
        b.make_molecule(session, "Nosupplierazole")
        session.flush()
        OpportunityEngine(session, TemporalContext(as_of_date=AS_OF)).run()
        session.flush()
        body = client.get("/api/v1/whitespace", params={"max_suppliers": 0}).json()
        names = {r["molecule"]["preferred_name"] for r in body["rows"]}
        assert "Nosupplierazole" in names
        row = next(r for r in body["rows"]
                   if r["molecule"]["preferred_name"] == "Nosupplierazole")
        assert row["observed_api_supplier_count"] == 0

    def test_filter_excludes_molecules_with_unknown_supplier_counts(
        self, client, session
    ) -> None:
        """When no DMF data exists at all, the count is unknown, not zero.

        Such a molecule must NOT match a supplier-count filter, or unmeasured
        molecules would be presented as uncontested ones.
        """
        molecule = b.make_molecule(session, "Unmeasuredazole")
        src = b.make_source(session, "ws_trials_only")
        doc = b.make_document(session, src, retrieved_at=RETRIEVED, published_at=PUBLISHED)
        sponsor = b.make_company(session, "WS Sponsor Ltd")
        b.make_exclusivity(session, doc, molecule=molecule,
                           expiry=dt.date(2028, 7, 1), published_at=PUBLISHED)
        b.make_trial(session, doc, molecule=molecule, sponsor=sponsor,
                     nct_id="NCT66000001", start_date=dt.date(2025, 6, 1),
                     published_at=PUBLISHED)
        session.flush()
        OpportunityEngine(session, TemporalContext(as_of_date=AS_OF)).run([molecule.id])
        session.flush()

        body = client.get("/api/v1/whitespace", params={"max_suppliers": 0}).json()
        names = {r["molecule"]["preferred_name"] for r in body["rows"]}
        assert "Unmeasuredazole" not in names

        # Unfiltered, it appears with an explicitly unknown count.
        unfiltered = client.get("/api/v1/whitespace").json()
        row = next(
            (r for r in unfiltered["rows"]
             if r["molecule"]["preferred_name"] == "Unmeasuredazole"), None
        )
        if row is not None:
            assert row["observed_api_supplier_count"] is None


class TestInvestigation:
    def test_investigation_has_every_required_section(self, client, populated) -> None:
        opp_id = client.get("/api/v1/feed").json()["build"][0]["id"]
        r = client.post(f"/api/v1/investigations/{opp_id}")
        assert r.status_code == 200
        body = r.json()
        keys = [s["key"] for s in body["sections"]]
        for required in (
            "executive_summary", "what_changed", "why_the_signal_fired",
            "molecule_landscape", "developers", "regulatory_landscape",
            "api_supply_landscape", "focus_capability_match", "potential_customers",
            "patent_exclusivity_context", "risks", "counter_thesis",
            "what_we_do_not_know", "recommended_human_investigation", "sources",
        ):
            assert required in keys, required

    def test_investigation_is_reproducible(self, client, populated) -> None:
        """Same opportunity, same as-of date, same engine → same document."""
        opp_id = client.get("/api/v1/feed").json()["build"][0]["id"]
        first = client.post(f"/api/v1/investigations/{opp_id}").json()
        second = client.post(f"/api/v1/investigations/{opp_id}").json()
        assert first["id"] == second["id"]
        assert first["sections"] == second["sections"]

    def test_decomposition_is_stated_as_summing_to_the_score(self, client, populated) -> None:
        opp_id = client.get("/api/v1/feed").json()["build"][0]["id"]
        body = client.post(f"/api/v1/investigations/{opp_id}").json()
        why = next(s for s in body["sections"] if s["key"] == "why_the_signal_fired")
        assert "which is the Opportunity Score" in why["body"]
        assert "checked by hand" in why["body"]

    def test_what_we_do_not_know_disclaims_market_size(self, client, populated) -> None:
        """The product must never state a market size, and must say so."""
        opp_id = client.get("/api/v1/feed").json()["build"][0]["id"]
        body = client.post(f"/api/v1/investigations/{opp_id}").json()
        unknowns = next(s for s in body["sections"] if s["key"] == "what_we_do_not_know")
        assert "no market size or value is stated" in unknowns["body"]
        assert "freedom to operate" in unknowns["body"].lower()

    def test_build_investigation_names_no_counterparty(self, client, populated) -> None:
        opp_id = client.get("/api/v1/feed").json()["build"][0]["id"]
        body = client.post(f"/api/v1/investigations/{opp_id}").json()
        customers = next(s for s in body["sections"] if s["key"] == "potential_customers")
        assert customers["availability"] == "NO_QUALIFYING_SIGNALS"
        assert "no counterparty is named" in customers["body"]

    def test_sections_with_no_data_say_so(self, client, populated) -> None:
        opp_id = client.get("/api/v1/feed").json()["build"][0]["id"]
        body = client.post(f"/api/v1/investigations/{opp_id}").json()
        availabilities = {s["availability"] for s in body["sections"]}
        # At least one section should be explicitly marked as lacking data, since
        # no shortage source was ingested.
        assert availabilities & {
            "DATA_UNAVAILABLE", "NO_QUALIFYING_SIGNALS", "INSUFFICIENT_EVIDENCE"
        }

    def test_synthesis_method_is_recorded(self, client, populated) -> None:
        """Whether prose came from a model must be visible to the reader."""
        opp_id = client.get("/api/v1/feed").json()["build"][0]["id"]
        body = client.post(f"/api/v1/investigations/{opp_id}").json()
        assert body["synthesis_method"] == "deterministic_from_evidence"


class TestDataHealth:
    def test_data_health_reports_checks_and_sources(self, client, populated) -> None:
        body = client.get("/api/v1/data-health", params={"rerun": True}).json()
        assert body["checks"]
        assert "blocking_failures" in body
        assert isinstance(body["sources"], list)
        assert isinstance(body["recent_runs"], list)

    def test_clean_graph_has_no_blocking_failures(self, client, populated) -> None:
        body = client.get("/api/v1/data-health", params={"rerun": True}).json()
        failing = [
            c for c in body["checks"] if c["severity"] == "ERROR" and not c["passed"]
        ]
        assert not failing, failing


class TestAuthEnforcement:
    @pytest.fixture
    def secure_client(self, session, monkeypatch):
        monkeypatch.setenv("MR_AUTH_ENABLED", "true")
        monkeypatch.setenv("MR_JWT_SECRET", "x" * 48)
        monkeypatch.setenv("MR_ENVIRONMENT", "development")

        from moleculeradar.core import config

        config.get_settings.cache_clear()
        from moleculeradar.api import app as app_module

        importlib.reload(app_module)
        application = app_module.create_app()
        from moleculeradar.core.db import get_db

        def _override():
            yield session

        application.dependency_overrides[get_db] = _override
        with TestClient(application) as c:
            yield c
        config.get_settings.cache_clear()

    def test_unauthenticated_request_is_rejected(self, secure_client) -> None:
        assert secure_client.get("/api/v1/feed").status_code == 401

    def test_invalid_token_is_rejected(self, secure_client) -> None:
        r = secure_client.get(
            "/api/v1/feed", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert r.status_code == 401

    def test_login_then_access(self, secure_client, session) -> None:
        from moleculeradar.api.security import hash_password

        session.add(User(
            email="analyst@example.invalid", password_hash=hash_password("correct-horse"),
            role="analyst", is_active=True,
        ))
        session.commit()

        bad = secure_client.post("/api/v1/auth/login", json={
            "email": "analyst@example.invalid", "password": "wrong",
        })
        assert bad.status_code == 401
        # The same message for wrong password and unknown user, so the endpoint
        # cannot be used to enumerate accounts.
        assert bad.json()["detail"] == "Invalid email or password."

        unknown = secure_client.post("/api/v1/auth/login", json={
            "email": "nobody@example.invalid", "password": "whatever",
        })
        assert unknown.json()["detail"] == bad.json()["detail"]

        good = secure_client.post("/api/v1/auth/login", json={
            "email": "analyst@example.invalid", "password": "correct-horse",
        })
        assert good.status_code == 200
        token = good.json()["access_token"]
        assert secure_client.get(
            "/api/v1/feed", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 200

    def test_analyst_cannot_read_data_health(self, secure_client, session) -> None:
        """Data Health is operational detail an analyst does not need."""
        from moleculeradar.api.security import hash_password

        session.add(User(
            email="analyst2@example.invalid", password_hash=hash_password("pw-pw-pw-pw"),
            role="analyst", is_active=True,
        ))
        session.commit()
        token = secure_client.post("/api/v1/auth/login", json={
            "email": "analyst2@example.invalid", "password": "pw-pw-pw-pw",
        }).json()["access_token"]
        r = secure_client.get(
            "/api/v1/data-health", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 403
        assert "lacks capability" in r.json()["detail"]

    def test_viewer_cannot_create_an_investigation(self, secure_client, session) -> None:
        from moleculeradar.api.security import hash_password

        session.add(User(
            email="viewer@example.invalid", password_hash=hash_password("pw-pw-pw-pw"),
            role="viewer", is_active=True,
        ))
        session.commit()
        token = secure_client.post("/api/v1/auth/login", json={
            "email": "viewer@example.invalid", "password": "pw-pw-pw-pw",
        }).json()["access_token"]
        import uuid

        r = secure_client.post(
            f"/api/v1/investigations/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403


def _production_env(monkeypatch) -> None:
    """A production configuration that is otherwise valid.

    Each boot-guard test then breaks exactly one setting, so a failure names the
    condition actually under test rather than whatever else happened to be unset.
    """
    monkeypatch.setenv("MR_ENVIRONMENT", "production")
    monkeypatch.setenv("MR_JWT_SECRET", "p" * 48)
    monkeypatch.setenv("MR_AUTH_ENABLED", "true")
    monkeypatch.setenv("MR_HTTP_CONTACT_EMAIL", "ops@example.invalid")
    monkeypatch.setenv("MR_RAW_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("MR_CORS_ALLOWED_ORIGINS", '["https://app.example.invalid"]')

    from moleculeradar.core import config

    config.get_settings.cache_clear()


class TestProductionBootGuard:
    def test_production_without_jwt_secret_refuses_to_start(self, monkeypatch) -> None:
        """A production deployment missing its secret must fail loudly, not serve."""
        _production_env(monkeypatch)
        monkeypatch.delenv("MR_JWT_SECRET", raising=False)

        from moleculeradar.api.app import create_app

        with pytest.raises(RuntimeError, match="MR_JWT_SECRET"):
            create_app()

    def test_production_with_auth_disabled_refuses_to_start(self, monkeypatch) -> None:
        _production_env(monkeypatch)
        monkeypatch.setenv("MR_AUTH_ENABLED", "false")

        from moleculeradar.api.app import create_app

        with pytest.raises(RuntimeError, match="MR_AUTH_ENABLED"):
            create_app()

    def test_production_hides_api_docs(self, monkeypatch) -> None:
        """Interactive docs must not be served in production."""
        _production_env(monkeypatch)

        from moleculeradar.api.app import create_app

        application = create_app()
        assert application.docs_url is None
        assert application.openapi_url is None

    def test_wildcard_cors_is_refused_in_production(self, monkeypatch) -> None:
        _production_env(monkeypatch)
        monkeypatch.setenv("MR_CORS_ALLOWED_ORIGINS", '["*"]')

        from moleculeradar.api.app import create_app

        with pytest.raises(RuntimeError, match="Wildcard CORS"):
            create_app()


class TestSecurityHeaders:
    def test_headers_are_present(self, client) -> None:
        r = client.get("/healthz")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
        assert r.headers["Referrer-Policy"] == "no-referrer"
