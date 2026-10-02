"""Entity resolution.

The contract, in order of precedence:

1. **Deterministic identifier** — a shared UNII, RxCUI, DMF number, application
   number or NCT ID is conclusive. Score 1.0.
2. **Alias table** — a literal we have already resolved and recorded. Score 0.99.
3. **Normalized exact** — identical after normalisation. Score 0.97.
4. **Fuzzy** — token-set similarity over normalised forms, scored and *bounded*
   so fuzzy alone can never reach the auto-accept threshold without
   corroboration.
5. **Contextual** — shared molecule/country/role evidence raises a fuzzy
   candidate's score.
6. **LLM-assisted** — only invoked for candidates already in the review band,
   and only ever as a *recommendation* attached to a review item. An LLM can
   never commit a merge.

Then:

* score >= `er_auto_accept_threshold` → link, write an `EntityMergeLog`.
* `er_review_floor_threshold` <= score < auto-accept → **review queue**, and the
  caller gets a new provisional entity rather than a guess.
* score < review floor → new entity.

Nothing is merged without an `EntityMergeLog` row stating the method, score and
rationale. That is what makes every merge explainable after the fact.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.enums import (
    AliasKind,
    EntityType,
    IdentifierScheme,
    ResolutionMethod,
    ReviewStatus,
)
from ..core.models import (
    Company,
    EntityAlias,
    EntityMergeLog,
    ExternalIdentifier,
    Molecule,
    ReviewQueueItem,
)
from .normalize import (
    company_blocking_key,
    company_core_tokens,
    molecule_blocking_key,
    normalize_company_name,
    normalize_molecule_name,
)


@dataclass(slots=True)
class ResolutionOutcome:
    """The result of resolving one literal.

    `entity_id` is None only when the literal was unresolvable (empty after
    normalisation). `needs_review` means a candidate existed but was not
    confident enough to merge — the entity_id refers to a newly created
    provisional entity, and a human must confirm or reject the link.
    """

    entity_id: uuid.UUID | None
    created: bool
    method: ResolutionMethod
    score: float
    rationale: str
    needs_review: bool = False
    candidate_entity_id: uuid.UUID | None = None
    features: dict[str, object] = field(default_factory=dict)


class CompanyResolver:
    """Resolves company name literals to canonical companies."""

    def __init__(self, session: Session, *, actor: str = "system") -> None:
        self.session = session
        self.actor = actor
        settings = get_settings()
        self.auto_accept = settings.er_auto_accept_threshold
        self.review_floor = settings.er_review_floor_threshold

    # -- public API --------------------------------------------------------

    def resolve(
        self,
        literal: str,
        *,
        identifiers: dict[IdentifierScheme, str] | None = None,
        context_country_id: uuid.UUID | None = None,
        source_document_id: uuid.UUID | None = None,
        create_if_missing: bool = True,
    ) -> ResolutionOutcome:
        literal = (literal or "").strip()
        if not literal:
            return ResolutionOutcome(
                entity_id=None,
                created=False,
                method=ResolutionMethod.NORMALIZED_EXACT,
                score=0.0,
                rationale="Empty company literal; not resolvable.",
            )

        normalized = normalize_company_name(literal)
        if not normalized:
            # The literal was entirely legal-form noise ("Ltd", "Inc"). Refusing
            # is correct: every such literal would otherwise collapse together.
            return ResolutionOutcome(
                entity_id=None,
                created=False,
                method=ResolutionMethod.NORMALIZED_EXACT,
                score=0.0,
                rationale=(
                    f"Company literal {literal!r} normalises to an empty string "
                    "(entirely legal-form/noise tokens); refusing to resolve."
                ),
            )

        # 1. Deterministic identifiers.
        if identifiers:
            for scheme, value in identifiers.items():
                if not value:
                    continue
                hit = self._by_identifier(scheme, value)
                if hit is not None:
                    self._log_merge(
                        EntityType.COMPANY, hit, literal,
                        ResolutionMethod.DETERMINISTIC_IDENTIFIER, 1.0,
                        f"Matched on {scheme.value}={value}.", source_document_id,
                    )
                    self._ensure_alias(EntityType.COMPANY, hit, literal,
                                       ResolutionMethod.DETERMINISTIC_IDENTIFIER, 1.0,
                                       source_document_id)
                    return ResolutionOutcome(hit, False,
                                             ResolutionMethod.DETERMINISTIC_IDENTIFIER, 1.0,
                                             f"Matched on {scheme.value}={value}.")

        # 2. Alias table.
        alias_hit = self._by_alias(EntityType.COMPANY, normalized)
        if alias_hit is not None:
            return ResolutionOutcome(
                alias_hit, False, ResolutionMethod.ALIAS_TABLE, 0.99,
                f"Literal {literal!r} already recorded as an alias.",
            )

        # 3. Normalized exact.
        exact = self.session.scalar(
            select(Company.id).where(Company.normalized_name == normalized)
        )
        if exact is not None:
            self._ensure_alias(EntityType.COMPANY, exact, literal,
                               ResolutionMethod.NORMALIZED_EXACT, 0.97, source_document_id)
            return ResolutionOutcome(
                exact, False, ResolutionMethod.NORMALIZED_EXACT, 0.97,
                f"Normalised form {normalized!r} matches exactly.",
            )

        # 4/5. Fuzzy + contextual.
        best = self._best_fuzzy_candidate(normalized, context_country_id)
        if best is not None:
            cand_id, score, detail = best
            if score >= self.auto_accept:
                self._log_merge(EntityType.COMPANY, cand_id, literal,
                                ResolutionMethod.FUZZY, score, detail, source_document_id)
                self._ensure_alias(EntityType.COMPANY, cand_id, literal,
                                   ResolutionMethod.FUZZY, score, source_document_id)
                return ResolutionOutcome(cand_id, False, ResolutionMethod.FUZZY, score, detail)

            if score >= self.review_floor:
                new_id = self._create(literal, normalized, context_country_id) \
                    if create_if_missing else None
                self._queue_review(literal, normalized, cand_id, new_id, score,
                                   detail, source_document_id)
                return ResolutionOutcome(
                    new_id, created=new_id is not None, method=ResolutionMethod.FUZZY,
                    score=score, rationale=detail, needs_review=True,
                    candidate_entity_id=cand_id,
                )

        if not create_if_missing:
            return ResolutionOutcome(None, False, ResolutionMethod.FUZZY, 0.0,
                                     "No confident match and creation disabled.")

        new_id = self._create(literal, normalized, context_country_id)
        self._ensure_alias(EntityType.COMPANY, new_id, literal,
                           ResolutionMethod.NORMALIZED_EXACT, 1.0, source_document_id)
        return ResolutionOutcome(
            new_id, True, ResolutionMethod.NORMALIZED_EXACT, 1.0,
            f"No existing company matched {literal!r}; created new canonical entity.",
        )

    # -- internals ---------------------------------------------------------

    def _by_identifier(self, scheme: IdentifierScheme, value: str) -> uuid.UUID | None:
        return self.session.scalar(
            select(ExternalIdentifier.entity_id).where(
                ExternalIdentifier.scheme == scheme,
                ExternalIdentifier.value == value,
                ExternalIdentifier.entity_type == EntityType.COMPANY,
            )
        )

    def _by_alias(self, entity_type: EntityType, normalized: str) -> uuid.UUID | None:
        return self.session.scalar(
            select(EntityAlias.entity_id).where(
                EntityAlias.entity_type == entity_type,
                EntityAlias.normalized_alias == normalized,
            )
        )

    def _best_fuzzy_candidate(
        self, normalized: str, context_country_id: uuid.UUID | None
    ) -> tuple[uuid.UUID, float, str] | None:
        """Score blocked candidates. Fuzzy alone is capped below auto-accept."""
        block = company_blocking_key(normalized)
        if not block:
            return None

        candidates = self.session.execute(
            select(Company.id, Company.canonical_name, Company.normalized_name,
                   Company.country_id)
            .where(Company.normalized_name.like(f"{block}%"))
            .limit(200)
        ).all()
        if not candidates:
            return None

        query_tokens = company_core_tokens(normalized)
        best: tuple[uuid.UUID, float, str] | None = None

        for cand_id, cand_name, cand_norm, cand_country in candidates:
            token_set = fuzz.token_set_ratio(normalized, cand_norm) / 100.0
            partial = fuzz.partial_ratio(normalized, cand_norm) / 100.0
            cand_tokens = company_core_tokens(cand_norm)
            overlap = (
                len(query_tokens & cand_tokens) / max(len(query_tokens | cand_tokens), 1)
                if query_tokens or cand_tokens else 0.0
            )

            # Weighted blend. Jaccard overlap is weighted highest because
            # token-set ratio is generous with substrings: "sun pharma" vs
            # "sun pharma advanced research" scores high on ratio but low on
            # overlap, and those are different companies.
            raw = 0.45 * overlap + 0.35 * token_set + 0.20 * partial

            # Cap fuzzy-only evidence below auto-accept. A fuzzy name match must
            # never, on its own, be enough to fuse two pharmaceutical companies.
            score = min(raw, self.auto_accept - 0.01)
            reasons = [
                f"token_overlap={overlap:.2f}",
                f"token_set_ratio={token_set:.2f}",
                f"partial_ratio={partial:.2f}",
            ]

            # Contextual corroboration can lift a strong fuzzy match over the line.
            if context_country_id is not None and cand_country == context_country_id:
                score = min(score + 0.03, 0.99)
                reasons.append("same_country=yes")

            if best is None or score > best[1]:
                detail = (
                    f"Fuzzy match to {cand_name!r} ({', '.join(reasons)}); "
                    f"score={score:.3f}"
                )
                best = (cand_id, score, detail)

        return best

    def _create(
        self, literal: str, normalized: str, country_id: uuid.UUID | None
    ) -> uuid.UUID:
        company = Company(
            canonical_name=literal,
            normalized_name=normalized,
            country_id=country_id,
            observed_at=dt.datetime.now(dt.UTC),
        )
        self.session.add(company)
        self.session.flush()
        return company.id

    def _ensure_alias(
        self, entity_type: EntityType, entity_id: uuid.UUID, literal: str,
        method: ResolutionMethod, confidence: float,
        source_document_id: uuid.UUID | None,
    ) -> None:
        normalized = (
            normalize_company_name(literal)
            if entity_type is EntityType.COMPANY
            else normalize_molecule_name(literal)
        )
        if not normalized:
            return
        exists = self.session.scalar(
            select(EntityAlias.id).where(
                EntityAlias.entity_type == entity_type,
                EntityAlias.entity_id == entity_id,
                EntityAlias.normalized_alias == normalized,
            )
        )
        if exists:
            return
        self.session.add(
            EntityAlias(
                entity_type=entity_type, entity_id=entity_id, alias=literal,
                normalized_alias=normalized, alias_kind=AliasKind.SOURCE_LITERAL,
                resolution_method=method, confidence=confidence,
                first_seen_source_document_id=source_document_id,
            )
        )

    def _log_merge(
        self, entity_type: EntityType, surviving: uuid.UUID, literal: str,
        method: ResolutionMethod, score: float, rationale: str,
        source_document_id: uuid.UUID | None,
    ) -> None:
        self.session.add(
            EntityMergeLog(
                entity_type=entity_type, surviving_entity_id=surviving,
                input_literal=literal, resolution_method=method, score=score,
                rationale=rationale, decided_by=self.actor,
                source_document_id=source_document_id,
            )
        )

    def _queue_review(
        self, literal: str, normalized: str, candidate_id: uuid.UUID,
        provisional_id: uuid.UUID | None, score: float, detail: str,
        source_document_id: uuid.UUID | None,
    ) -> None:
        candidate = self.session.get(Company, candidate_id)
        self.session.add(
            ReviewQueueItem(
                item_kind="company_merge_candidate",
                entity_type=EntityType.COMPANY,
                input_literal=literal,
                candidate_payload={
                    "normalized_literal": normalized,
                    "candidate_entity_id": str(candidate_id),
                    "candidate_name": candidate.canonical_name if candidate else None,
                    "provisional_entity_id": str(provisional_id) if provisional_id else None,
                    "score": score,
                    "detail": detail,
                },
                proposed_action=(
                    f"Review whether {literal!r} is the same company as "
                    f"{candidate.canonical_name if candidate else candidate_id!r}. "
                    "A provisional separate entity was created in the meantime so no "
                    "uncertain merge has occurred."
                ),
                confidence=score,
                status=ReviewStatus.PENDING,
                source_document_id=source_document_id,
            )
        )


class MoleculeResolver:
    """Resolves molecule/ingredient literals to canonical molecules.

    Molecules are easier than companies when UNII is present and harder when it
    is not: salt forms, combination products and stereoisomers all matter
    commercially. The normaliser handles salts; stereochemistry is deliberately
    preserved, so esomeprazole never resolves to omeprazole.
    """

    def __init__(self, session: Session, *, actor: str = "system") -> None:
        self.session = session
        self.actor = actor
        settings = get_settings()
        self.auto_accept = settings.er_auto_accept_threshold
        self.review_floor = settings.er_review_floor_threshold

    def resolve(
        self,
        literal: str,
        *,
        unii: str | None = None,
        rxcui: str | None = None,
        source_document_id: uuid.UUID | None = None,
        create_if_missing: bool = True,
    ) -> ResolutionOutcome:
        literal = (literal or "").strip()
        if not literal:
            return ResolutionOutcome(None, False, ResolutionMethod.NORMALIZED_EXACT, 0.0,
                                     "Empty molecule literal; not resolvable.")

        normalized = normalize_molecule_name(literal)
        if not normalized:
            return ResolutionOutcome(
                None, False, ResolutionMethod.NORMALIZED_EXACT, 0.0,
                f"Molecule literal {literal!r} normalises to empty "
                "(only salt/dosage tokens); refusing to resolve.",
            )

        # 1. Deterministic identifiers — UNII is the strongest available.
        for scheme, value in ((IdentifierScheme.UNII, unii), (IdentifierScheme.RXCUI, rxcui)):
            if not value:
                continue
            hit = self.session.scalar(
                select(ExternalIdentifier.entity_id).where(
                    ExternalIdentifier.scheme == scheme,
                    ExternalIdentifier.value == value,
                    ExternalIdentifier.entity_type == EntityType.MOLECULE,
                )
            )
            if hit is not None:
                self._ensure_alias(hit, literal, ResolutionMethod.DETERMINISTIC_IDENTIFIER,
                                   1.0, source_document_id)
                return ResolutionOutcome(hit, False,
                                         ResolutionMethod.DETERMINISTIC_IDENTIFIER, 1.0,
                                         f"Matched on {scheme.value}={value}.")

        # 2. Alias table.
        alias_hit = self.session.scalar(
            select(EntityAlias.entity_id).where(
                EntityAlias.entity_type == EntityType.MOLECULE,
                EntityAlias.normalized_alias == normalized,
            )
        )
        if alias_hit is not None:
            return ResolutionOutcome(alias_hit, False, ResolutionMethod.ALIAS_TABLE, 0.99,
                                     f"Literal {literal!r} already recorded as an alias.")

        # 3. Normalized exact.
        exact = self.session.scalar(
            select(Molecule.id).where(Molecule.normalized_name == normalized)
        )
        if exact is not None:
            self._ensure_alias(exact, literal, ResolutionMethod.NORMALIZED_EXACT, 0.97,
                               source_document_id)
            return ResolutionOutcome(exact, False, ResolutionMethod.NORMALIZED_EXACT, 0.97,
                                     f"Normalised moiety {normalized!r} matches exactly.")

        # 4. Fuzzy, bounded and reviewed.
        best = self._best_fuzzy(normalized)
        if best is not None:
            cand_id, score, detail = best
            if score >= self.auto_accept:
                self.session.add(
                    EntityMergeLog(
                        entity_type=EntityType.MOLECULE, surviving_entity_id=cand_id,
                        input_literal=literal, resolution_method=ResolutionMethod.FUZZY,
                        score=score, rationale=detail, decided_by=self.actor,
                        source_document_id=source_document_id,
                    )
                )
                self._ensure_alias(cand_id, literal, ResolutionMethod.FUZZY, score,
                                   source_document_id)
                return ResolutionOutcome(cand_id, False, ResolutionMethod.FUZZY, score, detail)

            if score >= self.review_floor:
                new_id = self._create(literal, normalized, unii) if create_if_missing else None
                candidate = self.session.get(Molecule, cand_id)
                self.session.add(
                    ReviewQueueItem(
                        item_kind="molecule_merge_candidate",
                        entity_type=EntityType.MOLECULE,
                        input_literal=literal,
                        candidate_payload={
                            "normalized_literal": normalized,
                            "candidate_entity_id": str(cand_id),
                            "candidate_name": candidate.preferred_name if candidate else None,
                            "provisional_entity_id": str(new_id) if new_id else None,
                            "score": score,
                            "detail": detail,
                        },
                        proposed_action=(
                            f"Review whether {literal!r} is the same active moiety as "
                            f"{candidate.preferred_name if candidate else cand_id!r}. "
                            "Note that stereoisomers and distinct salts are NOT the same "
                            "commercial molecule."
                        ),
                        confidence=score,
                        status=ReviewStatus.PENDING,
                        source_document_id=source_document_id,
                    )
                )
                return ResolutionOutcome(new_id, new_id is not None, ResolutionMethod.FUZZY,
                                         score, detail, needs_review=True,
                                         candidate_entity_id=cand_id)

        if not create_if_missing:
            return ResolutionOutcome(None, False, ResolutionMethod.FUZZY, 0.0,
                                     "No confident match and creation disabled.")

        new_id = self._create(literal, normalized, unii)
        self._ensure_alias(new_id, literal, ResolutionMethod.NORMALIZED_EXACT, 1.0,
                           source_document_id)
        return ResolutionOutcome(new_id, True, ResolutionMethod.NORMALIZED_EXACT, 1.0,
                                 f"No existing molecule matched {literal!r}; created new.")

    def _best_fuzzy(self, normalized: str) -> tuple[uuid.UUID, float, str] | None:
        block = molecule_blocking_key(normalized)
        if not block:
            return None
        candidates = self.session.execute(
            select(Molecule.id, Molecule.preferred_name, Molecule.normalized_name)
            .where(Molecule.normalized_name.like(f"{block}%"))
            .limit(200)
        ).all()
        if not candidates:
            return None

        best: tuple[uuid.UUID, float, str] | None = None
        for cand_id, cand_name, cand_norm in candidates:
            ratio = fuzz.ratio(normalized, cand_norm) / 100.0
            # Stereochemistry guard: names differing only by a known isomer prefix
            # are chemically and commercially distinct. Penalise hard so they land
            # in review rather than merging.
            stereo_conflict = _differs_only_by_stereo_prefix(normalized, cand_norm)
            score = min(ratio, self.auto_accept - 0.01)
            note = f"ratio={ratio:.2f}"
            if stereo_conflict:
                score = min(score, self.review_floor + 0.01)
                note += ", stereochemistry_prefix_difference=yes (not auto-merged)"
            if best is None or score > best[1]:
                best = (cand_id, score,
                        f"Fuzzy match to {cand_name!r} ({note}); score={score:.3f}")
        return best

    def _create(self, literal: str, normalized: str, unii: str | None) -> uuid.UUID:
        molecule = Molecule(
            preferred_name=literal,
            normalized_name=normalized,
            unii=unii,
            observed_at=dt.datetime.now(dt.UTC),
        )
        self.session.add(molecule)
        self.session.flush()
        if unii:
            self.session.add(
                ExternalIdentifier(
                    scheme=IdentifierScheme.UNII, value=unii,
                    entity_type=EntityType.MOLECULE, entity_id=molecule.id,
                )
            )
        return molecule.id

    def _ensure_alias(
        self, entity_id: uuid.UUID, literal: str, method: ResolutionMethod,
        confidence: float, source_document_id: uuid.UUID | None,
    ) -> None:
        normalized = normalize_molecule_name(literal)
        if not normalized:
            return
        exists = self.session.scalar(
            select(EntityAlias.id).where(
                EntityAlias.entity_type == EntityType.MOLECULE,
                EntityAlias.entity_id == entity_id,
                EntityAlias.normalized_alias == normalized,
            )
        )
        if exists:
            return
        self.session.add(
            EntityAlias(
                entity_type=EntityType.MOLECULE, entity_id=entity_id, alias=literal,
                normalized_alias=normalized, alias_kind=AliasKind.SOURCE_LITERAL,
                resolution_method=method, confidence=confidence,
                first_seen_source_document_id=source_document_id,
            )
        )


_STEREO_TOKENS = ("levo", "dextro", "es", "ar", "dex", "lev", "rac")


def _differs_only_by_stereo_prefix(a: str, b: str) -> bool:
    """True when two normalised names differ only by a stereochemistry prefix.

    `esomeprazole` vs `omeprazole`, `dexlansoprazole` vs `lansoprazole`,
    `levocetirizine` vs `cetirizine`. These must never auto-merge: they are
    separate products with separate exclusivity and separate market dynamics.
    """
    if a == b:
        return False
    shorter, longer = sorted((a.replace(" ", ""), b.replace(" ", "")), key=len)
    if not shorter or not longer.endswith(shorter):
        return False
    prefix = longer[: len(longer) - len(shorter)]
    return prefix in _STEREO_TOKENS
