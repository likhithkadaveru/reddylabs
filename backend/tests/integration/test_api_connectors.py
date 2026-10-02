"""Tests for the JSON API connectors: Drugs@FDA, shortages, ClinicalTrials.gov.

Payloads are built to each publisher's documented response shape. As with the
bulk connectors, this proves correct behaviour *given* the documented contract and
safe behaviour when the contract differs — which, with no live verification
possible, is the behaviour we are most likely to rely on.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import select

from moleculeradar.core.enums import EventType
from moleculeradar.core.models import (
    ClinicalTrial,
    Company,
    Event,
    ExternalIdentifier,
    Molecule,
    QuarantinedRecord,
    RegulatoryApplication,
    RegulatorySubmission,
)
from moleculeradar.sources.base import FetchedArtefact
from moleculeradar.sources.clinicaltrials import (
    ClinicalTrialsConnector,
    extract_molecule_candidates,
    parse_ctgov_date,
)
from moleculeradar.sources.openfda import (
    DrugsAtFdaConnector,
    DrugShortagesConnector,
)

pytestmark = pytest.mark.integration

UTC = dt.UTC


def _artefact(payload: dict, locator: str = "https://example.invalid/page") -> FetchedArtefact:
    return FetchedArtefact(
        locator=locator,
        content=json.dumps(payload, sort_keys=True).encode("utf-8"),
        media_type="application/json", http_status=200,
    )


def _run(connector, payload: dict, locator: str = "https://example.invalid/page"):
    connector.start_run("test")
    artefact = _artefact(payload, locator)
    doc, _ = connector.record_document(artefact)
    result = connector.load(doc, artefact)
    connector.finish_run(result)
    connector.session.flush()
    return doc, result


# ---------------------------------------------------------------------------
# Drugs@FDA
# ---------------------------------------------------------------------------

DRUGSFDA_PAYLOAD = {
    "meta": {
        "disclaimer": "Do not rely on openFDA to make decisions regarding medical care.",
        "license": "https://open.fda.gov/license/",
        "last_updated": "2026-09-23",
        "results": {"skip": 0, "limit": 1000, "total": 2},
    },
    "results": [
        {
            "application_number": "ANDA077222",
            "sponsor_name": "ALPHA GENERICS INC",
            "openfda": {
                "unii": ["TESTUNII001"], "rxcui": ["999111"],
                "generic_name": ["TESTAZOLE"], "brand_name": ["TESTAGEN"],
            },
            "products": [{
                "product_number": "001", "dosage_form": "TABLET",
                "active_ingredients": [{"name": "TESTAZOLE", "strength": "10MG"}],
            }],
            "submissions": [
                {"submission_type": "ORIG", "submission_number": "1",
                 "submission_status": "AP", "submission_status_date": "20210303"},
                {"submission_type": "SUPPL", "submission_number": "2",
                 "submission_status": "AP", "submission_status_date": "20220101"},
                {"submission_type": "ORIG", "submission_number": "3",
                 "submission_status": "TA", "submission_status_date": ""},
            ],
        },
        {
            "application_number": "NDA021111",
            "sponsor_name": "DR. REDDY'S LABORATORIES LTD",
            "openfda": {"unii": ["TESTUNII001"], "generic_name": ["TESTAZOLE"]},
            "products": [{
                "product_number": "001",
                "active_ingredients": [{"name": "TESTAZOLE", "strength": "10MG"}],
            }],
            "submissions": [
                {"submission_type": "ORIG", "submission_number": "1",
                 "submission_status": "AP", "submission_status_date": "20150115"},
            ],
        },
    ],
}


class TestDrugsAtFda:
    def test_loads_applications_and_submissions(self, session) -> None:
        _run(DrugsAtFdaConnector(session), DRUGSFDA_PAYLOAD)
        apps = {a.application_number for a in session.scalars(
            select(RegulatoryApplication)
        ).all()}
        assert apps == {"ANDA077222", "NDA021111"}
        assert len(session.scalars(select(RegulatorySubmission.id)).all()) == 4

    def test_application_type_inferred_from_number_prefix(self, session) -> None:
        _run(DrugsAtFdaConnector(session), DRUGSFDA_PAYLOAD)
        by_no = {a.application_number: a for a in session.scalars(
            select(RegulatoryApplication)
        ).all()}
        assert by_no["ANDA077222"].application_type == "ANDA"
        assert by_no["NDA021111"].application_type == "NDA"

    def test_unii_bridges_both_applications_to_one_molecule(self, session) -> None:
        """The harmonised identifier is what makes the join deterministic.

        Two applications sharing a UNII must resolve to the same molecule even if
        their ingredient strings differed.
        """
        _run(DrugsAtFdaConnector(session), DRUGSFDA_PAYLOAD)
        molecules = session.scalars(select(Molecule)).all()
        assert len(molecules) == 1
        ident = session.scalar(
            select(ExternalIdentifier).where(
                ExternalIdentifier.scheme == "UNII",
                ExternalIdentifier.value == "TESTUNII001",
            )
        )
        assert ident is not None
        assert ident.entity_id == molecules[0].id

    def test_only_original_submissions_become_events(self, session) -> None:
        """A supplement is not a new market entrant.

        Counting supplements as filings would make a single product's lifecycle
        look like competitive entry.
        """
        _run(DrugsAtFdaConnector(session), DRUGSFDA_PAYLOAD)
        events = session.scalars(select(Event)).all()
        assert all(e.event_type in (EventType.NEW_APPROVAL, EventType.NEW_FILING)
                   for e in events)
        # Two approved ORIG submissions become approvals; the SUPPL does not; the
        # undated ORIG cannot be placed on a timeline.
        approvals = [e for e in events if e.event_type == EventType.NEW_APPROVAL]
        assert len(approvals) == 2
        assert {e.event_date for e in approvals} == {
            dt.date(2021, 3, 3), dt.date(2015, 1, 15)
        }

    def test_undated_submission_is_stored_but_emits_no_event(self, session) -> None:
        _run(DrugsAtFdaConnector(session), DRUGSFDA_PAYLOAD)
        subs = session.scalars(select(RegulatorySubmission)).all()
        undated = [s for s in subs if s.event_date is None]
        assert len(undated) == 1
        assert undated[0].submission_status == "TA"

    def test_publisher_disclaimer_is_retained(self, session) -> None:
        """Stripping a publisher's caveat while reusing its data misrepresents it."""
        connector = DrugsAtFdaConnector(session)
        connector.start_run("test")
        artefact = _artefact(DRUGSFDA_PAYLOAD)
        artefact.disclaimer_text = DRUGSFDA_PAYLOAD["meta"]["disclaimer"]
        artefact.source_last_updated = dt.date(2026, 9, 23)
        doc, _ = connector.record_document(artefact)
        assert "Do not rely on openFDA" in doc.disclaimer_text
        assert doc.source_last_updated == dt.date(2026, 9, 23)

    def test_record_without_application_number_is_quarantined(self, session) -> None:
        payload = {"meta": {"results": {"total": 1}},
                   "results": [{"sponsor_name": "NO NUMBER LTD"}]}
        _, result = _run(DrugsAtFdaConnector(session), payload)
        assert result.records_quarantined == 1
        assert result.records_inserted == 0

    def test_invalid_json_is_quarantined(self, session) -> None:
        connector = DrugsAtFdaConnector(session)
        connector.start_run("test")
        artefact = FetchedArtefact(locator="x://y", content=b"{not json",
                                   media_type="application/json")
        doc, _ = connector.record_document(artefact)
        result = connector.load(doc, artefact)
        assert result.records_quarantined == 1

    def test_reload_is_idempotent(self, session) -> None:
        _run(DrugsAtFdaConnector(session), DRUGSFDA_PAYLOAD)
        before = (
            len(session.scalars(select(RegulatorySubmission.id)).all()),
            len(session.scalars(select(Event.id)).all()),
        )
        connector = DrugsAtFdaConnector(session)
        connector.start_run("test:again")
        artefact = _artefact(DRUGSFDA_PAYLOAD)
        doc = session.scalar(select(ExternalIdentifier))  # any row; reuse the doc
        from moleculeradar.core.models import SourceDocument

        doc = session.scalar(select(SourceDocument))
        connector.load(doc, artefact)
        session.flush()
        after = (
            len(session.scalars(select(RegulatorySubmission.id)).all()),
            len(session.scalars(select(Event.id)).all()),
        )
        assert before == after


# ---------------------------------------------------------------------------
# Shortages
# ---------------------------------------------------------------------------

SHORTAGE_PAYLOAD = {
    "meta": {"disclaimer": "openFDA disclaimer", "last_updated": "2026-09-23",
             "results": {"skip": 0, "limit": 1000, "total": 3}},
    "results": [
        {
            "generic_name": "TESTAZOLE", "company_name": "ALPHA GENERICS INC",
            "status": "Current", "initial_posting_date": "20250601",
            "reason": "Demand increase for the drug",
            "package_ndc": "11111-222-33",
            "openfda": {"generic_name": ["TESTAZOLE"], "unii": ["TESTUNII001"]},
        },
        {
            "generic_name": "TESTAZOLE", "company_name": "BETA PHARMA LLC",
            "status": "Resolved", "initial_posting_date": "20240101",
            "update_date": "20250201", "package_ndc": "44444-555-66",
            "openfda": {"generic_name": ["TESTAZOLE"]},
        },
        {
            "generic_name": "UNDATEDAZOLE", "company_name": "GAMMA LTD",
            "status": "Current", "openfda": {},
        },
    ],
}


class TestShortages:
    def test_open_and_resolved_events_are_distinguished(self, session) -> None:
        _run(DrugShortagesConnector(session), SHORTAGE_PAYLOAD)
        events = session.scalars(select(Event)).all()
        types = {e.event_type for e in events}
        assert EventType.SHORTAGE_OPENED in types
        assert EventType.SHORTAGE_RESOLVED in types

    def test_resolved_event_dated_by_update_not_initial_posting(self, session) -> None:
        """A resolution happened when it was reported resolved, not when it opened."""
        _run(DrugShortagesConnector(session), SHORTAGE_PAYLOAD)
        resolved = session.scalar(
            select(Event).where(Event.event_type == EventType.SHORTAGE_RESOLVED)
        )
        assert resolved.event_date == dt.date(2025, 2, 1)

    def test_undated_shortage_is_quarantined(self, session) -> None:
        """A record with no date cannot be placed on a timeline, so it is not guessed."""
        _, result = _run(DrugShortagesConnector(session), SHORTAGE_PAYLOAD)
        assert any(
            q.reason_code == "undated_shortage"
            for q in session.scalars(select(QuarantinedRecord)).all()
        )
        assert result.records_quarantined == 1

    def test_shortage_reason_is_carried_into_the_summary(self, session) -> None:
        _run(DrugShortagesConnector(session), SHORTAGE_PAYLOAD)
        opened = session.scalar(
            select(Event).where(Event.event_type == EventType.SHORTAGE_OPENED)
        )
        assert "Demand increase" in opened.summary

    def test_feature_reflects_open_shortage(self, session) -> None:
        from moleculeradar.core.temporal import TemporalContext
        from moleculeradar.signals.features import FeatureComputer

        _run(DrugShortagesConnector(session), SHORTAGE_PAYLOAD)
        molecule = session.scalar(
            select(Molecule).where(Molecule.normalized_name == "testazole")
        )
        fs = FeatureComputer(
            session, TemporalContext(as_of_date=dt.date(2026, 1, 1))
        ).compute(molecule.id, set())
        assert fs.values["shortage_events_36m"] == 1
        # One opened, one resolved on this molecule, so nothing unresolved remains.
        assert fs.values["shortage_open_now"] is False


# ---------------------------------------------------------------------------
# ClinicalTrials.gov
# ---------------------------------------------------------------------------


def _study(nct: str, *, sponsor: str, phases: list[str], status: str,
           start: str | None, first_posted: str, interventions: list[dict],
           last_update: str | None = None, countries: list[str] | None = None) -> dict:
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct, "briefTitle": f"Study {nct}"},
            "statusModule": {
                "overallStatus": status,
                "startDateStruct": {"date": start} if start else {},
                "studyFirstPostDateStruct": {"date": first_posted},
                "lastUpdatePostDateStruct": (
                    {"date": last_update} if last_update else {}
                ),
            },
            "designModule": {"phases": phases, "studyType": "INTERVENTIONAL",
                             "enrollmentInfo": {"count": 120}},
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": sponsor}},
            "armsInterventionsModule": {"interventions": interventions},
            "conditionsModule": {"conditions": ["Test Condition"]},
            "contactsLocationsModule": {
                "locations": [{"country": c} for c in (countries or ["United States"])]
            },
        }
    }


class TestCtgovParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("2025-06-01", dt.date(2025, 6, 1)), ("2025-06", dt.date(2025, 6, 1)),
         ("2025", dt.date(2025, 1, 1)), ("", None), (None, None)],
    )
    def test_partial_dates(self, raw, expected) -> None:
        """The registry serves YYYY, YYYY-MM and YYYY-MM-DD."""
        assert parse_ctgov_date(raw) == expected

    def test_placebo_and_non_substance_arms_are_excluded(self) -> None:
        """Counting a placebo arm as development would create a 'Placebo' molecule
        and credit sponsors with developing it."""
        interventions = [
            {"type": "DRUG", "name": "Testazole"},
            {"type": "DRUG", "name": "Placebo"},
            {"type": "DRUG", "name": "Matching Placebo"},
            {"type": "OTHER", "name": "Normal Saline"},
            {"type": "BEHAVIORAL", "name": "Counselling"},
            {"type": "DEVICE", "name": "Inhaler Device"},
            {"type": "BIOLOGICAL", "name": "Testamab"},
        ]
        assert extract_molecule_candidates(interventions) == ["Testazole", "Testamab"]

    def test_empty_interventions_yields_no_candidates(self) -> None:
        assert extract_molecule_candidates([]) == []


class TestCtgovLoading:
    def test_loads_trials_with_sponsor_and_molecule(self, session) -> None:
        payload = {"studies": [_study(
            "NCT10000001", sponsor="OMEGA FORMULATIONS LTD", phases=["PHASE3"],
            status="RECRUITING", start="2025-02-01", first_posted="2025-01-15",
            interventions=[{"type": "DRUG", "name": "Testazole"}],
            countries=["United States", "Germany"],
        )], "totalCount": 1}
        _run(ClinicalTrialsConnector(session), payload)

        trial = session.scalar(select(ClinicalTrial))
        assert trial.nct_id == "NCT10000001"
        assert trial.phase == "PHASE3"
        assert trial.start_date == dt.date(2025, 2, 1)
        assert trial.location_country_codes == ["Germany", "United States"]
        assert trial.molecule_id is not None
        assert trial.sponsor_company_id is not None

    def test_new_trial_event_dated_by_publication_not_start(self, session) -> None:
        """A trial that started before it was posted was not knowable at its start.

        Dating the event at the start date is the classic leak that makes a
        backtest look prescient.
        """
        payload = {"studies": [_study(
            "NCT10000002", sponsor="OMEGA FORMULATIONS LTD", phases=["PHASE2"],
            status="RECRUITING", start="2022-06-01", first_posted="2023-02-01",
            interventions=[{"type": "DRUG", "name": "Testazole"}],
        )]}
        _run(ClinicalTrialsConnector(session), payload)
        event = session.scalar(
            select(Event).where(Event.event_type == EventType.NEW_TRIAL)
        )
        assert event.event_date == dt.date(2023, 2, 1)
        trial = session.scalar(select(ClinicalTrial))
        # The start date is still recorded; it is simply not what made it knowable.
        assert trial.start_date == dt.date(2022, 6, 1)
        assert trial.published_at.date() == dt.date(2023, 2, 1)

    def test_phase_change_emits_an_event(self, session) -> None:
        """The API returns current state, so changes must be found by diffing."""
        first = {"studies": [_study(
            "NCT10000003", sponsor="OMEGA FORMULATIONS LTD", phases=["PHASE2"],
            status="RECRUITING", start="2024-01-01", first_posted="2024-01-01",
            interventions=[{"type": "DRUG", "name": "Testazole"}],
        )]}
        _run(ClinicalTrialsConnector(session), first)

        second = {"studies": [_study(
            "NCT10000003", sponsor="OMEGA FORMULATIONS LTD", phases=["PHASE3"],
            status="RECRUITING", start="2024-01-01", first_posted="2024-01-01",
            last_update="2025-06-01",
            interventions=[{"type": "DRUG", "name": "Testazole"}],
        )]}
        _run(ClinicalTrialsConnector(session), second,
             locator="https://example.invalid/page2")

        change = session.scalar(
            select(Event).where(Event.event_type == EventType.TRIAL_PHASE_CHANGED)
        )
        assert change is not None
        assert change.payload["from"] == "PHASE2"
        assert change.payload["to"] == "PHASE3"
        assert change.event_date == dt.date(2025, 6, 1)
        assert len(session.scalars(select(ClinicalTrial.id)).all()) == 1

    def test_termination_emits_a_terminated_event(self, session) -> None:
        first = {"studies": [_study(
            "NCT10000004", sponsor="OMEGA FORMULATIONS LTD", phases=["PHASE3"],
            status="RECRUITING", start="2024-01-01", first_posted="2024-01-01",
            interventions=[{"type": "DRUG", "name": "Testazole"}],
        )]}
        _run(ClinicalTrialsConnector(session), first)
        second = {"studies": [_study(
            "NCT10000004", sponsor="OMEGA FORMULATIONS LTD", phases=["PHASE3"],
            status="TERMINATED", start="2024-01-01", first_posted="2024-01-01",
            last_update="2025-08-01",
            interventions=[{"type": "DRUG", "name": "Testazole"}],
        )]}
        _run(ClinicalTrialsConnector(session), second,
             locator="https://example.invalid/page2")
        assert session.scalar(
            select(Event).where(Event.event_type == EventType.TRIAL_TERMINATED)
        ) is not None

    def test_placebo_only_study_creates_no_molecule(self, session) -> None:
        payload = {"studies": [_study(
            "NCT10000005", sponsor="OMEGA FORMULATIONS LTD", phases=["PHASE1"],
            status="RECRUITING", start="2025-01-01", first_posted="2025-01-01",
            interventions=[{"type": "DRUG", "name": "Placebo"}],
        )]}
        _run(ClinicalTrialsConnector(session), payload)
        assert session.scalars(select(Molecule.id)).all() == []
        trial = session.scalar(select(ClinicalTrial))
        assert trial.molecule_id is None

    def test_missing_studies_key_is_quarantined(self, session) -> None:
        _, result = _run(ClinicalTrialsConnector(session), {"unexpected": []})
        q = session.scalar(select(QuarantinedRecord))
        assert q.reason_code == "schema_mismatch"
        assert "unexpected" in q.reason_detail
        assert result.records_quarantined == 1

    def test_study_without_nct_id_is_quarantined(self, session) -> None:
        _, result = _run(ClinicalTrialsConnector(session),
                         {"studies": [{"protocolSection": {}}]})
        assert result.records_quarantined == 1

    def test_sponsor_resolution_shares_companies_across_sources(self, session) -> None:
        """A sponsor already known from another source must not be duplicated."""
        from moleculeradar.resolution.engine import CompanyResolver

        existing = CompanyResolver(session).resolve("Omega Formulations Limited")
        payload = {"studies": [_study(
            "NCT10000006", sponsor="OMEGA FORMULATIONS LTD", phases=["PHASE3"],
            status="RECRUITING", start="2025-01-01", first_posted="2025-01-01",
            interventions=[{"type": "DRUG", "name": "Testazole"}],
        )]}
        _run(ClinicalTrialsConnector(session), payload)
        trial = session.scalar(select(ClinicalTrial))
        assert trial.sponsor_company_id == existing.entity_id
        assert len(session.scalars(select(Company.id)).all()) == 1
