"""Opportunity engine tests against fixture scenarios with known expected results.

This is the file that answers the question "does the opportunity engine actually
work?". Each scenario is constructed so that every signal's contribution can be
computed by hand from the published methodology, and the test asserts those exact
values. If a scoring change is made without updating the methodology, these fail.

The headline invariant, asserted in every scenario: **the opportunity score is
exactly the sum of its signal points.** That is what makes the decomposition
shown to users arithmetically honest.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
from sqlalchemy import select
from tests.fixtures import builders as b

from moleculeradar.core.enums import (
    AssertionLevel,
    CapabilityType,
    EntityType,
    EventType,
    EvidenceConfidence,
    OpportunityType,
    PriorityBand,
)
from moleculeradar.core.models import CounterThesis, Opportunity, OpportunitySignal
from moleculeradar.core.temporal import TemporalContext, VisibilityMode
from moleculeradar.signals.engine import OpportunityEngine

pytestmark = pytest.mark.integration

UTC = dt.UTC
AS_OF = dt.date(2026, 1, 1)
PUBLISHED = dt.datetime(2025, 12, 1, tzinfo=UTC)
RETRIEVED = dt.datetime(2025, 12, 15, tzinfo=UTC)


def _ctx(as_of: dt.date = AS_OF) -> TemporalContext:
    return TemporalContext(as_of_date=as_of, mode=VisibilityMode.KNOWABLE)


class BuildScenario:
    """A fully specified BUILD scenario with hand-computable expected scores.

    Expected decomposition at as_of = 2026-01-01, from docs/OPPORTUNITY_ENGINE.md:

      loe_window                  18.0  (exclusivity expiry 2028-07-01 = 30 months,
                                         the centre of the 12-48 month window)
      limited_observed_api_supply  12.8  (1 competing supplier -> 15 * 0.85)
      developer_momentum           13.0  (3 filings vs 1 prior year -> growth 2.0, capped)
      clinical_momentum             9.6  (4 trial starts vs 2 prior -> growth 1.0 -> 12 * 0.8)
      capability_adjacency         10.9  (2 same-TA API capabilities + class match -> 0.64 * 17)
      geographic_breadth            2.0  (4 countries -> 6 * 0.333)
      ------------------------------------
      TOTAL                        66.3
    """

    def __init__(self, session) -> None:
        self.session = session
        ta = b.make_therapeutic_area(session, "ta_alpha")

        # Four independent sources, so evidence confidence can reach HIGH.
        self.src_ob = b.make_source(session, "test_orange_book")
        self.src_dmf = b.make_source(session, "test_dmf_list")
        self.src_fda = b.make_source(session, "test_drugsfda")
        self.src_ct = b.make_source(session, "test_ctgov")

        self.doc_ob = b.make_document(session, self.src_ob, retrieved_at=RETRIEVED,
                                      published_at=PUBLISHED)
        self.doc_dmf = b.make_document(session, self.src_dmf, retrieved_at=RETRIEVED,
                                       published_at=PUBLISHED)
        self.doc_fda = b.make_document(session, self.src_fda, retrieved_at=RETRIEVED,
                                       published_at=PUBLISHED)
        self.doc_ct = b.make_document(session, self.src_ct, retrieved_at=RETRIEVED,
                                      published_at=PUBLISHED)

        self.molecule = b.make_molecule(session, "Testrazole", therapeutic_area_id=ta.id)
        self.focus = b.make_company(session, "Dr. Reddy's Laboratories Ltd", is_focus=True)
        self.competitor_api = b.make_company(session, "Alpha Synthetics Ltd")

        # One competing API supplier; the focus company holds NO DMF here, which
        # is what makes this a BUILD candidate rather than a SELL one.
        b.make_dmf(session, self.doc_dmf, molecule=self.molecule,
                   holder=self.competitor_api, dmf_number="DMF-TEST-0001",
                   published_at=PUBLISHED)

        # Exclusivity expiring 30 months after the as-of date.
        b.make_exclusivity(session, self.doc_ob, molecule=self.molecule,
                           expiry=dt.date(2028, 7, 1), published_at=PUBLISHED)

        # Filing events: 3 in the trailing 12 months, 1 in the preceding 12.
        for i, (d, name) in enumerate([
            (dt.date(2025, 3, 10), "Beta Generics Ltd"),
            (dt.date(2025, 6, 20), "Gamma Generics Ltd"),
            (dt.date(2025, 9, 5), "Delta Generics Ltd"),
        ]):
            applicant = b.make_company(session, name)
            app = b.make_regulatory_application(
                session, self.doc_fda, applicant=applicant,
                application_number=f"ANDA-TEST-{i:03d}",
            )
            b.make_event(session, self.doc_fda, event_type=EventType.NEW_FILING,
                         molecule=self.molecule, company=applicant, event_date=d,
                         subject_entity_type=EntityType.REGULATORY_APPLICATION,
                         subject_entity_id=app.id, published_at=PUBLISHED)

        prior_applicant = b.make_company(session, "Epsilon Generics Ltd")
        prior_app = b.make_regulatory_application(
            session, self.doc_fda, applicant=prior_applicant,
            application_number="ANDA-TEST-900",
        )
        b.make_event(session, self.doc_fda, event_type=EventType.NEW_FILING,
                     molecule=self.molecule, company=prior_applicant,
                     event_date=dt.date(2024, 6, 1),
                     subject_entity_type=EntityType.REGULATORY_APPLICATION,
                     subject_entity_id=prior_app.id, published_at=PUBLISHED)

        # Trials: 4 starts in the trailing 12 months, 2 in the preceding 12.
        # Four distinct countries across them.
        sponsors = [b.make_company(session, f"Sponsor {n} Ltd") for n in ("One", "Two", "Three")]
        recent = [
            (dt.date(2025, 2, 1), ["US"]),
            (dt.date(2025, 5, 1), ["DE"]),
            (dt.date(2025, 8, 1), ["IN"]),
            (dt.date(2025, 11, 1), ["BR"]),
        ]
        for i, (start, countries) in enumerate(recent):
            b.make_trial(session, self.doc_ct, molecule=self.molecule,
                         sponsor=sponsors[i % len(sponsors)], nct_id=f"NCT9000000{i}",
                         start_date=start, countries=countries, published_at=PUBLISHED)
        for i, start in enumerate([dt.date(2024, 3, 1), dt.date(2024, 9, 1)]):
            b.make_trial(session, self.doc_ct, molecule=self.molecule,
                         sponsor=sponsors[i % len(sponsors)], nct_id=f"NCT8000000{i}",
                         start_date=start, countries=["US"], published_at=PUBLISHED)

        # Focus company capability: API supply for two OTHER molecules in the same
        # therapeutic area. This is the honest basis for capability adjacency.
        for n in ("Adjacentazole One", "Adjacentazole Two"):
            adj = b.make_molecule(session, n, therapeutic_area_id=ta.id)
            b.make_capability(session, self.doc_dmf, company=self.focus,
                              capability_type=CapabilityType.API_SUPPLY, molecule=adj,
                              assertion_level=AssertionLevel.FACT)
        session.flush()


class TestBuildScenario:
    def test_produces_one_build_opportunity(self, session) -> None:
        scenario = BuildScenario(session)
        engine = OpportunityEngine(session, _ctx())
        summary = engine.run([scenario.molecule.id])

        assert summary.molecules_evaluated == 1
        assert summary.build_opportunities == 1
        assert summary.sell_opportunities == 0

        opp = session.scalar(select(Opportunity))
        assert opp is not None
        assert opp.opportunity_type == OpportunityType.BUILD
        assert opp.counterparty_company_id is None

    def test_exact_signal_decomposition(self, session) -> None:
        """Each signal's points must match the published methodology exactly."""
        scenario = BuildScenario(session)
        OpportunityEngine(session, _ctx()).run([scenario.molecule.id])

        opp = session.scalar(select(Opportunity))
        signals = {
            s.signal_key: s
            for s in session.scalars(
                select(OpportunitySignal).where(OpportunitySignal.opportunity_id == opp.id)
            ).all()
        }

        expected = {
            "loe_window": 18.0,
            "limited_observed_api_supply": 12.8,
            "developer_momentum": 13.0,
            "clinical_momentum": 9.6,
            "capability_adjacency": 10.9,
            "geographic_breadth": 2.0,
        }
        assert set(signals) == set(expected), (
            f"unexpected signal set: fired={sorted(signals)} expected={sorted(expected)}"
        )
        for key, points in expected.items():
            assert signals[key].points == pytest.approx(points, abs=0.05), key

    def test_score_is_exactly_the_sum_of_its_signals(self, session) -> None:
        """The core explainability invariant.

        A user reading the decomposition must be able to add the points up and get
        the score back. Any normalisation or hidden transform would break this.
        """
        scenario = BuildScenario(session)
        OpportunityEngine(session, _ctx()).run([scenario.molecule.id])

        opp = session.scalar(select(Opportunity))
        signals = session.scalars(
            select(OpportunitySignal).where(OpportunitySignal.opportunity_id == opp.id)
        ).all()
        total = sum(s.points for s in signals)
        assert opp.opportunity_score == pytest.approx(total, abs=0.01)
        assert opp.opportunity_score == pytest.approx(66.3, abs=0.1)

    def test_every_signal_has_a_human_readable_explanation(self, session) -> None:
        scenario = BuildScenario(session)
        OpportunityEngine(session, _ctx()).run([scenario.molecule.id])
        opp = session.scalar(select(Opportunity))
        signals = session.scalars(
            select(OpportunitySignal).where(OpportunitySignal.opportunity_id == opp.id)
        ).all()
        for s in signals:
            assert s.explanation and len(s.explanation) > 20
            assert s.max_points > 0
            assert s.points <= s.max_points

    def test_evidence_confidence_is_separate_from_score(self, session) -> None:
        scenario = BuildScenario(session)
        OpportunityEngine(session, _ctx()).run([scenario.molecule.id])
        opp = session.scalar(select(Opportunity))

        # Four independent sources, recent documents, most signals evaluable.
        assert opp.evidence_confidence == EvidenceConfidence.HIGH
        assert opp.evidence_confidence_score == pytest.approx(0.919, abs=0.01)
        assert opp.priority_band == PriorityBand.HIGH
        # The two numbers are stored independently and are not derivable from
        # each other.
        assert opp.opportunity_score != opp.evidence_confidence_score * 100

    def test_language_rules_are_enforced_in_generated_text(self, session) -> None:
        """Generated text must never overstate. Checked against the brief's rules."""
        scenario = BuildScenario(session)
        OpportunityEngine(session, _ctx()).run([scenario.molecule.id])
        opp = session.scalar(select(Opportunity))
        signals = session.scalars(
            select(OpportunitySignal).where(OpportunitySignal.opportunity_id == opp.id)
        ).all()

        assert "surfaced for portfolio investigation" in opp.headline
        for banned in ("should manufacture", "should make", "needs this", "will need"):
            assert banned not in opp.headline.lower()

        supply = next(s for s in signals if s.signal_key == "limited_observed_api_supply")
        # Must be scoped to our coverage, not stated as a fact about the world.
        assert "current public-source coverage" in supply.explanation
        # The banned construction is the absolute claim ("only two suppliers
        # exist"), not the word "only" — which the text uses legitimately to
        # LIMIT its own scope ("US filings only"). Test the pattern, not the word.
        text = supply.explanation.lower()
        assert not re.search(r"only \d+", text)
        assert not re.search(r"only (one|two|three|four|five)\b", text)
        assert "there are only" not in text
        assert "suppliers exist" not in text
        # And it must actively disclaim completeness.
        assert "not a complete global supplier census" in text

        capability = next(s for s in signals if s.signal_key == "capability_adjacency")
        assert "inference" in capability.explanation.lower()

    def test_counter_thesis_is_generated(self, session) -> None:
        """Even a strong opportunity must carry its counter-analysis.

        Here the only genuine concern is a coverage gap: no shortage data was
        ingested, so that dimension was not assessed. Saying so is the honest
        alternative to scoring it as a negative.
        """
        scenario = BuildScenario(session)
        OpportunityEngine(session, _ctx()).run([scenario.molecule.id])
        opp = session.scalar(select(Opportunity))
        concerns = session.scalars(
            select(CounterThesis).where(CounterThesis.opportunity_id == opp.id)
        ).all()
        keys = {c.concern_key for c in concerns}
        assert "coverage_gaps" in keys
        gap = next(c for c in concerns if c.concern_key == "coverage_gaps")
        assert "shortages" in gap.explanation

    def test_missing_data_is_not_scored_as_zero(self, session) -> None:
        """A signal with no data must be recorded as unevaluated, not as a zero.

        Scoring absent data as zero would make a thin molecule look clean.
        """
        scenario = BuildScenario(session)
        OpportunityEngine(session, _ctx()).run([scenario.molecule.id])
        opp = session.scalar(select(Opportunity))
        signals = session.scalars(
            select(OpportunitySignal).where(OpportunitySignal.opportunity_id == opp.id)
        ).all()
        # supply_stress had no data at all, so it must not appear with 0 points.
        assert "supply_stress" not in {s.signal_key for s in signals}

    def test_rerun_is_idempotent(self, session) -> None:
        """Re-running must replace, not accumulate. A repeated job cannot inflate the feed."""
        scenario = BuildScenario(session)
        engine = OpportunityEngine(session, _ctx())
        engine.run([scenario.molecule.id])
        first_count = len(session.scalars(select(Opportunity.id)).all())
        first_signals = len(session.scalars(select(OpportunitySignal.id)).all())

        OpportunityEngine(session, _ctx()).run([scenario.molecule.id])
        assert len(session.scalars(select(Opportunity.id)).all()) == first_count
        assert len(session.scalars(select(OpportunitySignal.id)).all()) == first_signals


class TestTemporalLeakage:
    """The engine must not see anything published after the as-of date."""

    def test_no_opportunity_before_any_evidence_was_public(self, session) -> None:
        scenario = BuildScenario(session)
        # Everything was published 2025-12-01; as of 2024-01-01 nothing was knowable.
        summary = OpportunityEngine(session, _ctx(dt.date(2024, 1, 1))).run(
            [scenario.molecule.id]
        )
        assert summary.build_opportunities == 0
        assert summary.sell_opportunities == 0
        assert session.scalar(select(Opportunity)) is None

    def test_late_published_exclusivity_is_excluded(self, session) -> None:
        """A fact published after the as-of date must not contribute, even if the
        event it describes predates that date.

        This is the leak that would make a backtest look prescient.
        """
        ta = b.make_therapeutic_area(session, "ta_leak")
        src = b.make_source(session, "leak_src")
        early_doc = b.make_document(
            session, src, retrieved_at=dt.datetime(2025, 1, 1, tzinfo=UTC),
            published_at=dt.datetime(2025, 1, 1, tzinfo=UTC),
        )
        late_doc = b.make_document(
            session, src, retrieved_at=dt.datetime(2026, 6, 1, tzinfo=UTC),
            published_at=dt.datetime(2026, 6, 1, tzinfo=UTC),
        )
        mol = b.make_molecule(session, "Leakazole", therapeutic_area_id=ta.id)
        comp = b.make_company(session, "Leak Supplier Ltd")
        b.make_dmf(session, early_doc, molecule=mol, holder=comp,
                   dmf_number="DMF-LEAK-1",
                   published_at=dt.datetime(2025, 1, 1, tzinfo=UTC))
        # Published AFTER the as-of date.
        b.make_exclusivity(session, late_doc, molecule=mol, expiry=dt.date(2028, 7, 1),
                           published_at=dt.datetime(2026, 6, 1, tzinfo=UTC))
        session.flush()

        from moleculeradar.signals.features import FeatureComputer

        fc = FeatureComputer(session, _ctx())
        fs = fc.compute(mol.id, set())
        assert fs.values["months_to_next_exclusivity_expiry"] is None, (
            "an exclusivity record published after the as-of date must be invisible"
        )

        # With no temporal restriction it becomes visible, proving the gate is what
        # excluded it rather than the data being absent.
        fs_live = FeatureComputer(session, TemporalContext()).compute(mol.id, set())
        assert fs_live.values["months_to_next_exclusivity_expiry"] is not None

    def test_observed_mode_is_stricter_than_knowable(self, session) -> None:
        """OBSERVED mode additionally requires that we had recorded the fact."""
        src = b.make_source(session, "obs_src")
        doc = b.make_document(
            session, src, retrieved_at=dt.datetime(2026, 5, 1, tzinfo=UTC),
            published_at=dt.datetime(2020, 1, 1, tzinfo=UTC),
        )
        mol = b.make_molecule(session, "Observazole")
        comp = b.make_company(session, "Obs Supplier Ltd")
        b.make_dmf(session, doc, molecule=mol, holder=comp, dmf_number="DMF-OBS-1",
                   published_at=dt.datetime(2020, 1, 1, tzinfo=UTC))
        session.flush()

        from moleculeradar.signals.features import FeatureComputer

        knowable = FeatureComputer(session, _ctx()).compute(mol.id, set())
        assert knowable.values["observed_api_supplier_count"] == 1

        observed = FeatureComputer(
            session, TemporalContext(as_of_date=AS_OF, mode=VisibilityMode.OBSERVED)
        ).compute(mol.id, set())
        assert observed.values["observed_api_supplier_count"] in (0, None), (
            "a fact we had not yet recorded must not appear in OBSERVED mode"
        )


class SellScenario:
    """A SELL scenario with hand-computable expected scores.

    The focus company holds an active, reference-ready Type II DMF for the API.
    A separate company shows public development activity on the molecule and holds
    no API filing of its own.

    Expected decomposition at as_of = 2026-01-01:

      focus_supplies_api                        28.0  (active, available-for-reference DMF)
      counterparty_development_activity         15.0  (2 filings * 6 + 1 trial * 3)
      counterparty_no_observed_api_capability   20.0  (no DMF of its own, full allocation)
      counterparty_recent_entry                 10.9  (latest activity 118 days prior)
      --------------------------------------------------
      TOTAL                                     73.9
    """

    def __init__(self, session) -> None:
        self.session = session
        self.src_dmf = b.make_source(session, "sell_dmf_list")
        self.src_fda = b.make_source(session, "sell_drugsfda")
        self.src_ct = b.make_source(session, "sell_ctgov")
        self.doc_dmf = b.make_document(session, self.src_dmf, retrieved_at=RETRIEVED,
                                       published_at=PUBLISHED)
        self.doc_fda = b.make_document(session, self.src_fda, retrieved_at=RETRIEVED,
                                       published_at=PUBLISHED)
        self.doc_ct = b.make_document(session, self.src_ct, retrieved_at=RETRIEVED,
                                      published_at=PUBLISHED)

        self.molecule = b.make_molecule(session, "Sellazole")
        self.focus = b.make_company(session, "Dr. Reddy's Laboratories Ltd", is_focus=True)

        # The focus company publicly appears capable of supplying this API.
        b.make_dmf(session, self.doc_dmf, molecule=self.molecule, holder=self.focus,
                   dmf_number="DMF-SELL-0001", available_for_reference=True,
                   published_at=PUBLISHED)

        # A company showing public activity on the molecule, with no API filing.
        self.counterparty = b.make_company(session, "Omega Formulations Ltd")
        for i, d in enumerate([dt.date(2025, 4, 10), dt.date(2025, 9, 5)]):
            app = b.make_regulatory_application(
                session, self.doc_fda, applicant=self.counterparty,
                application_number=f"ANDA-SELL-{i:03d}",
            )
            b.make_event(session, self.doc_fda, event_type=EventType.NEW_FILING,
                         molecule=self.molecule, company=self.counterparty, event_date=d,
                         subject_entity_type=EntityType.REGULATORY_APPLICATION,
                         subject_entity_id=app.id, published_at=PUBLISHED)
        b.make_trial(session, self.doc_ct, molecule=self.molecule,
                     sponsor=self.counterparty, nct_id="NCT77000001",
                     start_date=dt.date(2025, 6, 1), published_at=PUBLISHED)
        session.flush()


class TestSellScenario:
    def test_produces_a_sell_opportunity_naming_the_counterparty(self, session) -> None:
        scenario = SellScenario(session)
        summary = OpportunityEngine(session, _ctx()).run([scenario.molecule.id])

        assert summary.sell_opportunities == 1
        # A molecule the focus company already supplies is not a BUILD candidate.
        assert summary.build_opportunities == 0

        opp = session.scalar(select(Opportunity))
        assert opp.opportunity_type == OpportunityType.SELL
        assert opp.counterparty_company_id == scenario.counterparty.id

    def test_exact_signal_decomposition(self, session) -> None:
        scenario = SellScenario(session)
        OpportunityEngine(session, _ctx()).run([scenario.molecule.id])
        opp = session.scalar(select(Opportunity))
        signals = {
            s.signal_key: s.points
            for s in session.scalars(
                select(OpportunitySignal).where(OpportunitySignal.opportunity_id == opp.id)
            ).all()
        }
        expected = {
            "focus_supplies_api": 28.0,
            "counterparty_development_activity": 15.0,
            "counterparty_no_observed_api_capability": 20.0,
            "counterparty_recent_entry": 10.9,
        }
        assert set(signals) == set(expected)
        for key, points in expected.items():
            assert signals[key] == pytest.approx(points, abs=0.05), key
        assert opp.opportunity_score == pytest.approx(sum(expected.values()), abs=0.05)
        assert opp.opportunity_score == pytest.approx(73.9, abs=0.1)

    def test_never_asserts_that_the_company_needs_the_api(self, session) -> None:
        """The most important language rule in the product.

        Asserting a commercial need we cannot know would be the fastest way to
        destroy credibility with a pharmaceutical customer.
        """
        scenario = SellScenario(session)
        OpportunityEngine(session, _ctx()).run([scenario.molecule.id])
        opp = session.scalar(select(Opportunity))
        signals = session.scalars(
            select(OpportunitySignal).where(OpportunitySignal.opportunity_id == opp.id)
        ).all()

        assert "exhibits public signals consistent with potential demand" in opp.headline
        all_text = (opp.headline + " " + " ".join(s.explanation for s in signals)).lower()
        for banned in (
            "needs this api", "needs drl", "requires drl", "will buy",
            "is a customer", "sources from", "supplies to",
        ):
            assert banned not in all_text, banned

    def test_absence_of_counterparty_api_filing_is_hedged(self, session) -> None:
        """Absence in our coverage must not be presented as absence in the world."""
        scenario = SellScenario(session)
        OpportunityEngine(session, _ctx()).run([scenario.molecule.id])
        opp = session.scalar(select(Opportunity))
        sig = session.scalar(
            select(OpportunitySignal).where(
                OpportunitySignal.opportunity_id == opp.id,
                OpportunitySignal.signal_key == "counterparty_no_observed_api_capability",
            )
        )
        text = sig.explanation.lower()
        assert "identified in current public-source coverage" in text
        assert "captive manufacturing or non-us sourcing" in text
        assert "not proof of" in text

    def test_focus_company_is_never_its_own_counterparty(self, session) -> None:
        """The focus group must be excluded from SELL candidates.

        Otherwise the product would recommend that Dr. Reddy's sell to itself.
        """
        scenario = SellScenario(session)
        # Give the focus company its own filing activity on the molecule.
        app = b.make_regulatory_application(
            session, scenario.doc_fda, applicant=scenario.focus,
            application_number="ANDA-SELL-FOCUS",
        )
        b.make_event(session, scenario.doc_fda, event_type=EventType.NEW_FILING,
                     molecule=scenario.molecule, company=scenario.focus,
                     event_date=dt.date(2025, 7, 1),
                     subject_entity_type=EntityType.REGULATORY_APPLICATION,
                     subject_entity_id=app.id, published_at=PUBLISHED)
        session.flush()

        OpportunityEngine(session, _ctx()).run([scenario.molecule.id])
        opps = session.scalars(select(Opportunity)).all()
        assert all(o.counterparty_company_id != scenario.focus.id for o in opps)

    def test_no_sell_opportunity_without_focus_api_evidence(self, session) -> None:
        """Without public evidence that the focus company supplies the API there is
        nothing to sell, however active the counterparty is."""
        src = b.make_source(session, "nosell_src")
        doc = b.make_document(session, src, retrieved_at=RETRIEVED, published_at=PUBLISHED)
        mol = b.make_molecule(session, "Nosellazole")
        b.make_company(session, "Dr. Reddy's Laboratories Ltd", is_focus=True)
        other = b.make_company(session, "Busy Formulations Ltd")
        app = b.make_regulatory_application(
            session, doc, applicant=other, application_number="ANDA-NOSELL-1",
        )
        b.make_event(session, doc, event_type=EventType.NEW_FILING, molecule=mol,
                     company=other, event_date=dt.date(2025, 8, 1),
                     subject_entity_type=EntityType.REGULATORY_APPLICATION,
                     subject_entity_id=app.id, published_at=PUBLISHED)
        session.flush()

        summary = OpportunityEngine(session, _ctx()).run([mol.id])
        assert summary.sell_opportunities == 0


class TestCrowdedMoleculeIsNotPromoted:
    """A crowded, barriered molecule must score low and attract concerns.

    This is the anti-hype test: the engine must be capable of saying no.
    """

    def test_crowded_molecule_scores_low_with_concerns(self, session) -> None:
        src_dmf = b.make_source(session, "crowd_dmf")
        src_ob = b.make_source(session, "crowd_ob")
        doc_dmf = b.make_document(session, src_dmf, retrieved_at=RETRIEVED,
                                  published_at=PUBLISHED)
        doc_ob = b.make_document(session, src_ob, retrieved_at=RETRIEVED,
                                 published_at=PUBLISHED)
        mol = b.make_molecule(session, "Crowdazole")
        b.make_company(session, "Dr. Reddy's Laboratories Ltd", is_focus=True)

        # Ten competing API suppliers.
        for i in range(10):
            holder = b.make_company(session, f"Crowd Supplier {i:02d} Ltd")
            b.make_dmf(session, doc_dmf, molecule=mol, holder=holder,
                       dmf_number=f"DMF-CROWD-{i:04d}", published_at=PUBLISHED)
        # A long patent runway.
        b.make_patent(session, doc_ob, molecule=mol, patent_number="RE99999",
                      expiry=dt.date(2036, 1, 1), drug_substance=True,
                      published_at=PUBLISHED)
        session.flush()

        OpportunityEngine(session, _ctx()).run([mol.id])
        opp = session.scalar(select(Opportunity))

        if opp is not None:
            # limited_observed_api_supply must not fire with 10 suppliers.
            keys = {
                s.signal_key for s in session.scalars(
                    select(OpportunitySignal).where(
                        OpportunitySignal.opportunity_id == opp.id
                    )
                ).all()
            }
            assert "limited_observed_api_supply" not in keys
            concerns = {
                c.concern_key for c in session.scalars(
                    select(CounterThesis).where(CounterThesis.opportunity_id == opp.id)
                ).all()
            }
            assert "crowded_api_supply" in concerns
            assert "long_patent_runway" in concerns
        else:
            # Falling below the BUILD threshold entirely is also a correct outcome.
            assert True

    def test_skeptic_sees_crowding_even_when_no_opportunity_is_persisted(
        self, session
    ) -> None:
        """The counter-analysis is computed from features, so it works regardless."""
        from moleculeradar.signals.features import FeatureComputer
        from moleculeradar.signals.skeptic import generate_counter_thesis

        src = b.make_source(session, "crowd2_dmf")
        doc = b.make_document(session, src, retrieved_at=RETRIEVED, published_at=PUBLISHED)
        mol = b.make_molecule(session, "Crowdazole Two")
        for i in range(9):
            holder = b.make_company(session, f"Crowd2 Supplier {i:02d} Ltd")
            b.make_dmf(session, doc, molecule=mol, holder=holder,
                       dmf_number=f"DMF-CROWD2-{i:04d}", published_at=PUBLISHED)
        session.flush()

        fs = FeatureComputer(session, _ctx()).compute(mol.id, set())
        concerns = generate_counter_thesis(fs, OpportunityType.BUILD, {})
        keys = {c.concern_key for c in concerns}
        assert "crowded_api_supply" in keys
        crowded = next(c for c in concerns if c.concern_key == "crowded_api_supply")
        assert crowded.severity == "HIGH"


class TestThinEvidenceBanding:
    def test_single_source_molecule_cannot_reach_high_confidence(self, session) -> None:
        """Evidence confidence is gated on source breadth, not just score.

        One source cannot be cross-checked, so an error in it would be invisible.
        """
        src = b.make_source(session, "thin_src")
        doc = b.make_document(session, src, retrieved_at=RETRIEVED, published_at=PUBLISHED)
        mol = b.make_molecule(session, "Thinazole")
        b.make_company(session, "Dr. Reddy's Laboratories Ltd", is_focus=True)
        holder = b.make_company(session, "Thin Supplier Ltd")
        b.make_dmf(session, doc, molecule=mol, holder=holder, dmf_number="DMF-THIN-1",
                   published_at=PUBLISHED)
        b.make_exclusivity(session, doc, molecule=mol, expiry=dt.date(2028, 7, 1),
                           published_at=PUBLISHED)
        session.flush()

        OpportunityEngine(session, _ctx()).run([mol.id])
        opp = session.scalar(select(Opportunity))
        if opp is not None:
            assert opp.evidence_confidence != EvidenceConfidence.HIGH
            assert opp.priority_band != PriorityBand.HIGH
            concerns = {
                c.concern_key for c in session.scalars(
                    select(CounterThesis).where(CounterThesis.opportunity_id == opp.id)
                ).all()
            }
            assert "single_source_evidence" in concerns

    def test_molecule_with_no_data_yields_no_opportunity(self, session) -> None:
        """NO QUALIFYING SIGNALS is the correct output for an empty molecule."""
        mol = b.make_molecule(session, "Emptyazole")
        summary = OpportunityEngine(session, _ctx()).run([mol.id])
        assert summary.build_opportunities == 0
        assert summary.sell_opportunities == 0
        assert session.scalar(select(Opportunity)) is None


class TestEvidenceRecency:
    """Recency must measure how old the newest PUBLIC fact is.

    Measuring from our own retrieval timestamp made the value negative in any
    historical run — ingestion necessarily runs after the as-of date — and leaked
    our operational schedule into a historical assessment.
    """

    def test_recency_measured_from_publication_not_retrieval(self, session) -> None:
        from moleculeradar.signals.features import FeatureComputer

        src = b.make_source(session, "recency_src")
        # Published well before the as-of date, but downloaded long after it.
        doc = b.make_document(
            session, src,
            retrieved_at=dt.datetime(2026, 10, 5, tzinfo=UTC),
            published_at=dt.datetime(2025, 7, 1, tzinfo=UTC),
        )
        mol = b.make_molecule(session, "Recencyazole")
        holder = b.make_company(session, "Recency Supplier Ltd")
        b.make_dmf(session, doc, molecule=mol, holder=holder, dmf_number="DMF-REC-1",
                   published_at=dt.datetime(2025, 7, 1, tzinfo=UTC))
        session.flush()

        fs = FeatureComputer(session, _ctx(dt.date(2026, 1, 1))).compute(mol.id, set())
        recency = fs.values["evidence_recency_days"]

        assert recency is not None
        assert recency >= 0, "recency can never be negative"
        # 2025-07-01 to 2026-01-01 is 184 days.
        assert recency == 184, (
            "recency must be measured from publication, not from our download time"
        )

    def test_recency_is_never_negative(self, session) -> None:
        """Even same-day publication yields zero, not a negative number."""
        from moleculeradar.signals.features import FeatureComputer

        src = b.make_source(session, "recency_src2")
        doc = b.make_document(
            session, src,
            retrieved_at=dt.datetime(2026, 10, 5, tzinfo=UTC),
            published_at=dt.datetime(2026, 1, 1, tzinfo=UTC),
        )
        mol = b.make_molecule(session, "Sameday Azole")
        holder = b.make_company(session, "Sameday Supplier Ltd")
        b.make_dmf(session, doc, molecule=mol, holder=holder, dmf_number="DMF-REC-2",
                   published_at=dt.datetime(2026, 1, 1, tzinfo=UTC))
        session.flush()

        fs = FeatureComputer(session, _ctx(dt.date(2026, 1, 1))).compute(mol.id, set())
        assert fs.values["evidence_recency_days"] == 0
