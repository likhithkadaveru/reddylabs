"""End-to-end pipeline test.

Runs synthetic artefacts through the **real connectors** — not fixture builders —
so that resolution, event emission, capability derivation, feature computation,
scoring and the skeptic are all exercised as they would be in production. The only
thing substituted is the bytes that would have come over the network.

This is the test that answers "does the whole thing actually work?", and it is the
closest thing to a live run that is possible in an environment with no egress.

The scenario is constructed so that both opportunity classes appear from the same
data:

* `TESTAZOLE` — the focus company holds an active Type II DMF, and another company
  is publicly developing it while holding no API filing of its own → **SELL**.
* `BUILDAZOLE` — the focus company holds no DMF, one competing supplier is
  observed, exclusivity expires inside the actionable window, and filings and
  trials are growing → **BUILD**.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import zipfile

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from moleculeradar.core.enums import (
    AssertionLevel,
    OpportunityType,
)
from moleculeradar.core.models import (
    Company,
    CompanyCapability,
    DrugMasterFile,
    Event,
    Evidence,
    Molecule,
    Opportunity,
    OpportunitySignal,
    SourceDocument,
)
from moleculeradar.core.temporal import TemporalContext, VisibilityMode
from moleculeradar.quality.checks import run_all_checks
from moleculeradar.resolution.focus import bootstrap_focus_company, focus_company_ids
from moleculeradar.signals.engine import OpportunityEngine
from moleculeradar.sources.base import FetchedArtefact
from moleculeradar.sources.clinicaltrials import ClinicalTrialsConnector
from moleculeradar.sources.fda_dmf import FdaDmfConnector
from moleculeradar.sources.openfda import DrugsAtFdaConnector
from moleculeradar.sources.orange_book import OrangeBookConnector

pytestmark = pytest.mark.integration

UTC = dt.UTC
AS_OF = dt.date(2026, 9, 1)
OB_PUBLISHED = dt.datetime(2026, 8, 14, tzinfo=UTC)
DMF_PUBLISHED = dt.datetime(2026, 7, 19, tzinfo=UTC)


# --- Orange Book -----------------------------------------------------------

OB_PRODUCTS = """Ingredient~DF;Route~Trade_Name~Applicant~Strength~Appl_Type~Appl_No~Product_No~TE_Code~Approval_Date~RLD~RS~Type~Applicant_Full_Name
TESTAZOLE~TABLET;ORAL~TESTABRAND~ORIGCO~10MG~N~021111~001~~Jan 15, 2015~Yes~Yes~RX~ORIGINATOR PHARMA LTD
BUILDAZOLE~TABLET;ORAL~BUILDBRAND~BLDCO~25MG~N~022222~001~~Mar 1, 2016~Yes~Yes~RX~BUILD ORIGINATOR LTD
"""

OB_PATENTS = """Appl_Type~Appl_No~Product_No~Patent_No~Patent_Expire_Date_Text~Drug_Substance_Flag~Drug_Product_Flag~Patent_Use_Code~Delist_Requested
N~022222~001~9444444~Apr 1, 2029~N~Y~~N
"""

# BUILDAZOLE exclusivity expires 2029-03-01: 30 months after the as-of date,
# the centre of the actionable loss-of-exclusivity window.
OB_EXCLUSIVITY = """Appl_Type~Appl_No~Product_No~Exclusivity_Code~Exclusivity_Date
N~022222~001~NCE~Mar 1, 2029
"""


def _ob_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("products.txt", OB_PRODUCTS)
        z.writestr("patent.txt", OB_PATENTS)
        z.writestr("exclusivity.txt", OB_EXCLUSIVITY)
    return buf.getvalue()


# --- DMF list --------------------------------------------------------------

DMF_ROWS = [
    # The focus company supplies TESTAZOLE -> enables SELL.
    [40001, "II", "DR. REDDY'S LABORATORIES LTD", "TESTAZOLE", "A", "2016-03-01"],
    # One competing supplier on BUILDAZOLE, and the focus company holds none.
    [40002, "II", "SOLO SYNTHETICS LTD", "BUILDAZOLE", "A", "2019-05-01"],
    # The focus company's other API capabilities, which establish adjacency.
    [40003, "II", "DR. REDDY'S LABORATORIES LIMITED", "ADJACENTAZOLE ONE", "A", "2017-01-01"],
    [40004, "II", "DR. REDDY'S LABORATORIES INC", "ADJACENTAZOLE TWO", "A", "2018-01-01"],
]


def _dmf_workbook() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["LIST OF DRUG MASTER FILES (DMFs)"])
    ws.append(["Current through 30 June 2026"])
    ws.append(["DMF NUMBER", "DMF TYPE", "HOLDER NAME", "SUBJECT", "STATUS", "SUBMIT DATE"])
    for row in DMF_ROWS:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- Drugs@FDA -------------------------------------------------------------

def _drugsfda_payload() -> dict:
    results = [
        # OMEGA is developing TESTAZOLE and holds no API DMF -> SELL counterparty.
        {
            "application_number": "ANDA090001", "sponsor_name": "OMEGA FORMULATIONS LTD",
            "openfda": {"generic_name": ["TESTAZOLE"]},
            "products": [{"product_number": "001",
                          "active_ingredients": [{"name": "TESTAZOLE"}]}],
            "submissions": [{"submission_type": "ORIG", "submission_number": "1",
                             "submission_status": "AP",
                             "submission_status_date": "20260301"}],
        },
    ]
    # Three BUILDAZOLE filings in the trailing 12 months, one in the prior year,
    # which is the growth the developer_momentum signal reads.
    for i, (sponsor, date) in enumerate([
        ("GENERIC ALPHA LTD", "20251001"),
        ("GENERIC BETA LTD", "20260201"),
        ("GENERIC GAMMA LTD", "20260601"),
        ("GENERIC DELTA LTD", "20250301"),
    ]):
        results.append({
            "application_number": f"ANDA09100{i}", "sponsor_name": sponsor,
            "openfda": {"generic_name": ["BUILDAZOLE"]},
            "products": [{"product_number": "001",
                          "active_ingredients": [{"name": "BUILDAZOLE"}]}],
            "submissions": [{"submission_type": "ORIG", "submission_number": "1",
                             "submission_status": "TA",
                             "submission_status_date": date}],
        })
    return {
        "meta": {"disclaimer": "openFDA disclaimer", "last_updated": "2026-08-23",
                 "results": {"skip": 0, "limit": 1000, "total": len(results)}},
        "results": results,
    }


# --- ClinicalTrials.gov ----------------------------------------------------

def _ctgov_payload() -> dict:
    def study(nct: str, sponsor: str, molecule: str, start: str, posted: str,
              phases: list[str], countries: list[str]) -> dict:
        return {"protocolSection": {
            "identificationModule": {"nctId": nct, "briefTitle": f"Study of {molecule}"},
            "statusModule": {
                "overallStatus": "RECRUITING",
                "startDateStruct": {"date": start},
                "studyFirstPostDateStruct": {"date": posted},
            },
            "designModule": {"phases": phases, "studyType": "INTERVENTIONAL",
                             "enrollmentInfo": {"count": 100}},
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": sponsor}},
            "armsInterventionsModule": {"interventions": [
                {"type": "DRUG", "name": molecule},
                {"type": "DRUG", "name": "Placebo"},
            ]},
            "conditionsModule": {"conditions": ["Test Condition"]},
            "contactsLocationsModule": {
                "locations": [{"country": c} for c in countries]
            },
        }}

    studies = [
        study("NCT20000001", "OMEGA FORMULATIONS LTD", "Testazole",
              "2026-01-15", "2026-01-15", ["PHASE3"], ["United States"]),
        # Four BUILDAZOLE starts in the trailing 12 months, two before: growth 1.0.
        study("NCT20000002", "GENERIC ALPHA LTD", "Buildazole",
              "2025-10-01", "2025-10-01", ["PHASE3"], ["United States"]),
        study("NCT20000003", "GENERIC BETA LTD", "Buildazole",
              "2026-01-01", "2026-01-01", ["PHASE3"], ["Germany"]),
        study("NCT20000004", "GENERIC GAMMA LTD", "Buildazole",
              "2026-04-01", "2026-04-01", ["PHASE2"], ["India"]),
        study("NCT20000005", "GENERIC DELTA LTD", "Buildazole",
              "2026-07-01", "2026-07-01", ["PHASE3"], ["Brazil"]),
        study("NCT20000006", "GENERIC ALPHA LTD", "Buildazole",
              "2024-10-01", "2024-10-01", ["PHASE1"], ["United States"]),
        study("NCT20000007", "GENERIC BETA LTD", "Buildazole",
              "2025-02-01", "2025-02-01", ["PHASE1"], ["United States"]),
    ]
    return {"studies": studies, "totalCount": len(studies)}


def _load(connector, content: bytes, locator: str, published_at=None,
          media_type="application/json"):
    connector.start_run(f"e2e:{connector.spec.key}")
    artefact = FetchedArtefact(locator=locator, content=content, media_type=media_type,
                              http_status=200, published_at=published_at)
    doc, is_new = connector.record_document(artefact)
    assert is_new, f"{locator} should be a new document"
    result = connector.load(doc, artefact)
    connector.finish_run(result)
    connector.session.flush()
    return result


@pytest.fixture
def pipeline(session):
    """Run the full ingestion pipeline in the registry's intended order."""
    results = {}
    results["orange_book"] = _load(
        OrangeBookConnector(session), _ob_zip(),
        "https://www.fda.gov/media/76860/download", OB_PUBLISHED, "application/zip",
    )
    results["dmf"] = _load(
        FdaDmfConnector(session, artefact_url="https://example.invalid/dmf.xlsx"),
        _dmf_workbook(), "https://www.fda.gov/media/testing/dmf.xlsx", DMF_PUBLISHED,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    results["drugsfda"] = _load(
        DrugsAtFdaConnector(session),
        json.dumps(_drugsfda_payload(), sort_keys=True).encode(),
        "https://api.fda.gov/drug/drugsfda.json?skip=0",
    )
    results["ctgov"] = _load(
        ClinicalTrialsConnector(session),
        json.dumps(_ctgov_payload(), sort_keys=True).encode(),
        "https://clinicaltrials.gov/api/v2/studies?pageToken=first",
    )
    bootstrap_focus_company(session)
    session.flush()
    return results


class TestIngestionAcrossSources:
    def test_all_four_sources_loaded_records(self, session, pipeline) -> None:
        for key, result in pipeline.items():
            assert result.records_inserted > 0, f"{key} inserted nothing"

    def test_molecules_are_shared_across_sources(self, session, pipeline) -> None:
        """A molecule named in four different sources must be one row.

        If resolution failed, supplier counts, filings and trials would each land
        on a different duplicate and every feature would be understated.
        """
        testazole = session.scalars(
            select(Molecule).where(Molecule.normalized_name == "testazole")
        ).all()
        assert len(testazole) == 1

        mol = testazole[0]
        assert session.scalar(
            select(func.count()).select_from(DrugMasterFile).where(
                DrugMasterFile.molecule_id == mol.id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(Event).where(Event.molecule_id == mol.id)
        ) >= 2

    def test_focus_group_members_are_linked_not_duplicated(self, session, pipeline) -> None:
        """LTD, LIMITED and INC editions of the holder name are one group.

        Without this the focus company would appear to hold a fraction of its
        actual filings and capability adjacency would collapse.
        """
        ids = focus_company_ids(session)
        assert ids, "focus company must be established"
        held = session.scalar(
            select(func.count()).select_from(DrugMasterFile).where(
                DrugMasterFile.holder_company_id.in_(ids),
                DrugMasterFile.dmf_type == "2",
            )
        )
        assert held == 3, "all three focus-group DMFs must be attributed to the group"

    def test_capability_graph_is_built_with_fact_assertions(self, session, pipeline) -> None:
        ids = focus_company_ids(session)
        caps = session.scalars(
            select(CompanyCapability).where(CompanyCapability.company_id.in_(ids))
        ).all()
        assert len(caps) == 3
        assert all(c.assertion_level == AssertionLevel.FACT for c in caps)
        assert all(c.evidence_id is not None for c in caps)

    def test_every_canonical_row_has_traceable_evidence(self, session, pipeline) -> None:
        """The product's central promise, checked across the whole loaded graph."""
        for dmf in session.scalars(select(DrugMasterFile)).all():
            evidence = session.get(Evidence, dmf.evidence_id)
            assert evidence is not None
            doc = session.get(SourceDocument, evidence.source_document_id)
            assert doc is not None and doc.sha256
        for event in session.scalars(select(Event)).all():
            assert session.get(Evidence, event.evidence_id) is not None


class TestOpportunityGeneration:
    def test_both_opportunity_classes_are_produced(self, session, pipeline) -> None:
        ctx = TemporalContext(as_of_date=AS_OF, mode=VisibilityMode.KNOWABLE)
        summary = OpportunityEngine(session, ctx).run()

        assert summary.sell_opportunities >= 1, summary.notes
        assert summary.build_opportunities >= 1, summary.notes

        types = {
            o.opportunity_type for o in session.scalars(select(Opportunity)).all()
        }
        assert types == {OpportunityType.SELL, OpportunityType.BUILD}

    def test_sell_opportunity_names_the_right_counterparty(self, session, pipeline) -> None:
        ctx = TemporalContext(as_of_date=AS_OF, mode=VisibilityMode.KNOWABLE)
        OpportunityEngine(session, ctx).run()

        sell = session.scalar(
            select(Opportunity).where(Opportunity.opportunity_type == OpportunityType.SELL)
        )
        molecule = session.get(Molecule, sell.molecule_id)
        counterparty = session.get(Company, sell.counterparty_company_id)

        assert molecule.normalized_name == "testazole"
        assert "OMEGA" in counterparty.canonical_name.upper()
        assert counterparty.id not in focus_company_ids(session)
        assert "consistent with potential demand" in sell.headline

    def test_build_opportunity_is_the_unsupplied_molecule(self, session, pipeline) -> None:
        ctx = TemporalContext(as_of_date=AS_OF, mode=VisibilityMode.KNOWABLE)
        OpportunityEngine(session, ctx).run()

        build = session.scalar(
            select(Opportunity).where(Opportunity.opportunity_type == OpportunityType.BUILD)
        )
        molecule = session.get(Molecule, build.molecule_id)
        assert molecule.normalized_name == "buildazole"
        assert "surfaced for portfolio investigation" in build.headline

        signals = {
            s.signal_key: s for s in session.scalars(
                select(OpportunitySignal).where(
                    OpportunitySignal.opportunity_id == build.id
                )
            ).all()
        }
        # The loss-of-exclusivity window is the centre of the actionable range.
        assert "loe_window" in signals
        assert signals["loe_window"].points == pytest.approx(18.0, abs=0.1)
        # One competing supplier observed.
        assert "limited_observed_api_supply" in signals
        assert signals["limited_observed_api_supply"].points == pytest.approx(12.8, abs=0.1)
        # Capability adjacency, from two same-therapeutic-area API capabilities.
        assert "capability_adjacency" in signals

    def test_score_equals_signal_sum_for_every_opportunity(self, session, pipeline) -> None:
        ctx = TemporalContext(as_of_date=AS_OF, mode=VisibilityMode.KNOWABLE)
        OpportunityEngine(session, ctx).run()
        for opp in session.scalars(select(Opportunity)).all():
            signals = session.scalars(
                select(OpportunitySignal).where(
                    OpportunitySignal.opportunity_id == opp.id
                )
            ).all()
            assert opp.opportunity_score == pytest.approx(
                sum(s.points for s in signals), abs=0.05
            )

    def test_every_opportunity_cites_evidence(self, session, pipeline) -> None:
        ctx = TemporalContext(as_of_date=AS_OF, mode=VisibilityMode.KNOWABLE)
        OpportunityEngine(session, ctx).run()
        for opp in session.scalars(select(Opportunity)).all():
            signals = session.scalars(
                select(OpportunitySignal).where(
                    OpportunitySignal.opportunity_id == opp.id
                )
            ).all()
            cited = [s for s in signals if s.evidence_ids]
            assert cited, f"{opp.opportunity_type} cites no evidence at all"
            for sig in cited:
                for eid in sig.evidence_ids:
                    import uuid as _uuid

                    assert session.get(Evidence, _uuid.UUID(eid)) is not None


class TestLeakageSafetyOnRealPipeline:
    def test_run_before_publication_yields_nothing(self, session, pipeline) -> None:
        """Every artefact was published in 2026, so a 2020 run must see nothing."""
        ctx = TemporalContext(as_of_date=dt.date(2020, 1, 1), mode=VisibilityMode.KNOWABLE)
        summary = OpportunityEngine(session, ctx).run()
        assert summary.build_opportunities == 0
        assert summary.sell_opportunities == 0

    def test_earlier_as_of_sees_fewer_filings(self, session, pipeline) -> None:
        """A run before the latest filings must not count them.

        The filings were posted across 2025-2026; a run as of early 2026 must see
        strictly fewer than a run as of September 2026.
        """
        from moleculeradar.signals.features import FeatureComputer

        molecule = session.scalar(
            select(Molecule).where(Molecule.normalized_name == "buildazole")
        )
        ids = focus_company_ids(session)

        late = FeatureComputer(
            session, TemporalContext(as_of_date=AS_OF)
        ).compute(molecule.id, ids)
        early = FeatureComputer(
            session, TemporalContext(as_of_date=dt.date(2025, 11, 1))
        ).compute(molecule.id, ids)

        assert early.values["trial_starts_12m"] < late.values["trial_starts_12m"]


class TestDataQualityOnLoadedGraph:
    def test_no_blocking_quality_failures_after_a_clean_run(self, session, pipeline) -> None:
        """The loaded graph must pass every ERROR-severity check.

        If it does not, the product could be presenting something false.
        """
        ctx = TemporalContext(as_of_date=AS_OF, mode=VisibilityMode.KNOWABLE)
        OpportunityEngine(session, ctx).run()

        outcomes = run_all_checks(session, persist=False)
        failures = [o for o in outcomes if not o.passed and o.severity == "ERROR"]
        assert not failures, [
            f"{o.check_key}: {o.detail} samples={o.sample_ids[:3]}" for o in failures
        ]

    def test_score_sum_check_passes_on_real_data(self, session, pipeline) -> None:
        ctx = TemporalContext(as_of_date=AS_OF, mode=VisibilityMode.KNOWABLE)
        OpportunityEngine(session, ctx).run()
        outcomes = {o.check_key: o for o in run_all_checks(session, persist=False)}
        assert outcomes["score_matches_signal_sum"].passed
        assert outcomes["opportunity_without_signals"].passed
        assert outcomes["duplicate_molecules"].passed
        assert outcomes["duplicate_companies"].passed
        assert outcomes["ambiguous_aliases"].passed
