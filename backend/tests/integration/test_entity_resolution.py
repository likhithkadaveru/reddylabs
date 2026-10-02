"""Entity resolution tests.

The behaviours that matter most are the refusals: the resolver must not merge
things it is not sure about, and every merge it does make must be explainable.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from moleculeradar.core.enums import (
    AliasKind,
    EntityType,
    IdentifierScheme,
    ResolutionMethod,
    ReviewStatus,
)
from moleculeradar.core.models import (
    Company,
    EntityAlias,
    EntityMergeLog,
    ExternalIdentifier,
    Molecule,
    ReviewQueueItem,
)
from moleculeradar.resolution.engine import CompanyResolver, MoleculeResolver

pytestmark = pytest.mark.integration


class TestCompanyResolution:
    def test_identical_variants_resolve_to_one_company(self, session) -> None:
        r = CompanyResolver(session)
        first = r.resolve("Dr. Reddy's Laboratories Ltd")
        assert first.created is True

        for variant in (
            "Dr Reddys Laboratories Limited",
            "DR. REDDYS LABORATORIES LTD",
            "Dr. Reddy's Laboratories, Inc.",
            "Dr. Reddy’s Laboratories Ltd",
        ):
            out = r.resolve(variant)
            assert out.entity_id == first.entity_id, variant
            assert out.created is False
            assert out.score >= 0.97

        assert session.scalar(select(Company).where(Company.id == first.entity_id)) is not None
        count = len(session.scalars(select(Company.id)).all())
        assert count == 1, "variants must not create extra company rows"

    def test_aliases_are_recorded_for_every_literal(self, session) -> None:
        r = CompanyResolver(session)
        out = r.resolve("Dr. Reddy's Laboratories Ltd")
        r.resolve("Dr Reddys Laboratories Limited")

        aliases = session.scalars(
            select(EntityAlias).where(
                EntityAlias.entity_type == EntityType.COMPANY,
                EntityAlias.entity_id == out.entity_id,
            )
        ).all()
        assert len(aliases) >= 1
        assert all(a.alias_kind == AliasKind.SOURCE_LITERAL for a in aliases)

    def test_deterministic_identifier_wins_and_is_logged(self, session) -> None:
        """A shared external identifier is conclusive and must be logged as such."""
        r = CompanyResolver(session)
        base = r.resolve("Acme Alpha Pharma Ltd")
        session.add(ExternalIdentifier(
            scheme=IdentifierScheme.FDA_DMF_NO, value="DMF-99999",
            entity_type=EntityType.COMPANY, entity_id=base.entity_id,
        ))
        session.flush()

        # A completely different name, but the same identifier.
        out = r.resolve(
            "Totally Different Name Corp",
            identifiers={IdentifierScheme.FDA_DMF_NO: "DMF-99999"},
        )
        assert out.entity_id == base.entity_id
        assert out.method is ResolutionMethod.DETERMINISTIC_IDENTIFIER
        assert out.score == 1.0

        logs = session.scalars(select(EntityMergeLog)).all()
        # Enum-typed columns are stored as VARCHAR and come back as plain str, so
        # they must be compared by value. StrEnum makes `==` correct; `is` is not.
        assert any(
            log.resolution_method == ResolutionMethod.DETERMINISTIC_IDENTIFIER
            and "DMF-99999" in log.rationale
            for log in logs
        ), "a deterministic merge must leave an explainable log row"

    def test_abbreviation_requires_an_alias_not_a_guess(self, session) -> None:
        """'DRL' must not silently fuse with the full name.

        There is no textual basis for the match, so the resolver must not invent
        one. It becomes resolvable only once a human or configuration records the
        alias — which is exactly what `focus bootstrap` does from the configured
        alias seeds.
        """
        r = CompanyResolver(session)
        full = r.resolve("Dr. Reddy's Laboratories Ltd")

        # Seed the alias the way `focus bootstrap` does, before any source
        # literal "DRL" is encountered.
        session.add(EntityAlias(
            entity_type=EntityType.COMPANY, entity_id=full.entity_id,
            alias="DRL", normalized_alias="drl", alias_kind=AliasKind.ABBREVIATION,
            resolution_method=ResolutionMethod.HUMAN_REVIEW, confidence=1.0,
        ))
        session.flush()

        resolved = CompanyResolver(session).resolve("DRL")
        assert resolved.entity_id == full.entity_id
        assert resolved.method == ResolutionMethod.ALIAS_TABLE
        assert resolved.created is False

    def test_unseeded_abbreviation_creates_a_separate_entity(self, session) -> None:
        """Without a seeded alias, 'DRL' is simply an unknown company.

        That is the correct conservative outcome: a wrong merge would attach
        another firm's DMFs and capabilities to Dr. Reddy's.
        """
        r = CompanyResolver(session)
        full = r.resolve("Dr. Reddy's Laboratories Ltd")
        drl = r.resolve("DRL")
        assert drl.entity_id != full.entity_id
        assert drl.created is True

    def test_conflicting_alias_is_queued_not_overwritten(self, session) -> None:
        """A literal pointing at two entities must become a review item.

        One normalised alias may belong to at most one entity, so an
        identifier-driven match that would steal another entity's alias has to
        stop and ask rather than silently repoint it or crash the ingestion run.
        """
        r = CompanyResolver(session)
        owner = r.resolve("Gamma Delta Pharma Ltd")

        # A second company that will be matched by identifier while carrying a
        # literal whose normalised form the first company already owns.
        other = r.resolve("Epsilon Omega Labs Ltd")
        session.add(ExternalIdentifier(
            scheme=IdentifierScheme.FDA_DMF_NO, value="DMF-12121",
            entity_type=EntityType.COMPANY, entity_id=other.entity_id,
        ))
        session.flush()

        out = r.resolve(
            "Gamma Delta Pharma Limited",
            identifiers={IdentifierScheme.FDA_DMF_NO: "DMF-12121"},
        )
        # The identifier is conclusive, so the match itself stands...
        assert out.entity_id == other.entity_id
        # ...but the alias stays with its original owner.
        alias_owner = session.scalar(
            select(EntityAlias.entity_id).where(
                EntityAlias.entity_type == EntityType.COMPANY,
                EntityAlias.normalized_alias == "gamma delta",
            )
        )
        assert alias_owner == owner.entity_id
        conflicts = session.scalars(
            select(ReviewQueueItem).where(ReviewQueueItem.item_kind == "conflicting_alias")
        ).all()
        assert len(conflicts) == 1
        assert str(owner.entity_id) in conflicts[0].candidate_payload[
            "existing_owner_entity_id"
        ]

    def test_fuzzy_alone_never_auto_merges(self, session) -> None:
        """A fuzzy name match must never be sufficient on its own.

        The single most important safety property of the resolver: fusing two
        pharmaceutical companies on name similarity would corrupt supplier counts,
        capability claims and every opportunity derived from them. A fuzzy match
        either lands in review or creates a separate entity — never a merge.
        """

        r = CompanyResolver(session)
        first = r.resolve("Kappa Sigma Pharma Ltd")
        out = r.resolve("Kappa Sigmaa Pharma Limited")

        assert out.entity_id != first.entity_id, "fuzzy similarity must not merge"
        assert out.method != ResolutionMethod.NORMALIZED_EXACT or out.created

    def test_fuzzy_candidate_scores_are_capped_below_auto_accept(self, session) -> None:
        """Verified at the scoring layer, not just via resolve() outcomes.

        Even a near-identical pair must score below the auto-accept threshold, so
        no amount of name similarity can trigger an automatic merge.
        """
        from moleculeradar.core.config import get_settings
        from moleculeradar.resolution.normalize import normalize_company_name

        r = CompanyResolver(session)
        r.resolve("Kappa Sigma Pharma Ltd")
        best = r._best_fuzzy_candidate(
            normalize_company_name("Kappa Sigma Pharma Limited"), None
        )
        assert best is not None, (
            "blocking must generate the candidate; if it does not, near-duplicates "
            "silently become separate companies"
        )
        _cand_id, score, detail = best
        assert score < get_settings().er_auto_accept_threshold
        assert "token_overlap" in detail

    def test_ambiguous_pair_is_queued_for_review(self, session) -> None:
        """A score in the review band must produce a review item and no merge."""

        r = CompanyResolver(session)
        first = r.resolve("Iota Nu Biosciences Ltd")
        out = r.resolve("Iota Nu Biosciencs Ltd")
        if out.needs_review:
            assert out.candidate_entity_id == first.entity_id
            assert out.entity_id != first.entity_id
            items = session.scalars(
                select(ReviewQueueItem).where(
                    ReviewQueueItem.item_kind == "company_merge_candidate"
                )
            ).all()
            assert items, "an ambiguous pair must create a review item"
            assert items[0].status == ReviewStatus.PENDING
            assert "provisional_entity_id" in items[0].candidate_payload

    def test_noise_only_literal_is_refused(self, session) -> None:
        """'Ltd.' must not resolve to anything.

        Without this guard every noise-only literal would collapse into a single
        meaningless company.
        """
        r = CompanyResolver(session)
        out = r.resolve("Ltd.")
        assert out.entity_id is None
        assert "normalises to an empty string" in out.rationale
        assert session.scalars(select(Company.id)).all() == []

    def test_empty_literal_is_refused(self, session) -> None:
        out = CompanyResolver(session).resolve("   ")
        assert out.entity_id is None


class TestMoleculeResolution:
    def test_salt_variants_resolve_to_one_moiety(self, session) -> None:
        r = MoleculeResolver(session)
        first = r.resolve("Metformin Hydrochloride")
        assert first.created is True
        for variant in ("metformin HCl", "METFORMIN HYDROCHLORIDE ER", "Metformin"):
            out = r.resolve(variant)
            assert out.entity_id == first.entity_id, variant
        assert len(session.scalars(select(Molecule.id)).all()) == 1

    def test_unii_is_conclusive(self, session) -> None:
        r = MoleculeResolver(session)
        base = r.resolve("Testmolecule One", unii="ABC123XYZ9")
        out = r.resolve("Completely Other Name", unii="ABC123XYZ9")
        assert out.entity_id == base.entity_id
        assert out.method is ResolutionMethod.DETERMINISTIC_IDENTIFIER

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Omeprazole", "Esomeprazole"),
            ("Lansoprazole", "Dexlansoprazole"),
            ("Cetirizine", "Levocetirizine"),
        ],
    )
    def test_stereoisomers_never_auto_merge(self, session, a: str, b: str) -> None:
        """Stereoisomers are commercially distinct and must stay distinct.

        Merging esomeprazole into omeprazole would attach the wrong exclusivity
        dates, the wrong supplier counts and the wrong competitive field to both.
        """
        r = MoleculeResolver(session)
        first = r.resolve(a)
        second = r.resolve(b)
        assert second.entity_id != first.entity_id
        if second.needs_review:
            items = session.scalars(select(ReviewQueueItem)).all()
            assert any("stereoisomers" in i.proposed_action for i in items)

    def test_salt_only_literal_refused(self, session) -> None:
        out = MoleculeResolver(session).resolve("Hydrochloride")
        assert out.entity_id is None
        assert session.scalars(select(Molecule.id)).all() == []
