"""Focus company bootstrap.

The focus company (Dr. Reddy's by default) is *configuration*, not schema. This
module is what turns that configuration into database state: it establishes the
canonical company, records the configured alias seeds, and links the group's
subsidiaries.

Why alias seeds must be explicit: "DRL" has no textual relationship to
"Dr. Reddy's Laboratories Ltd". The resolver is deliberately unwilling to invent
that link, because inventing it in the general case would fuse unrelated firms.
Seeding it here is a recorded human decision, attributed to `resolution_method =
human_review`, not a guess.

Subsidiary linking is kept conservative. A DMF held by "Dr. Reddy's Laboratories
Inc" (the US entity) is genuinely the group's capability, so it should count. But
treating every company whose name merely contains "Reddy" as part of the group
would be exactly the kind of overreach that produces a false capability claim, so
candidates that are not confidently part of the group go to the review queue.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.enums import AliasKind, EntityType, ResolutionMethod, ReviewStatus
from ..core.models import Company, EntityAlias, EntityMergeLog, ReviewQueueItem
from .engine import claim_alias
from .normalize import company_core_tokens, normalize_company_name


@dataclass(slots=True)
class FocusBootstrapResult:
    focus_company_id: uuid.UUID | None = None
    created_company: bool = False
    aliases_recorded: int = 0
    aliases_conflicted: int = 0
    subsidiaries_linked: int = 0
    candidates_queued: int = 0
    notes: list[str] = field(default_factory=list)


def bootstrap_focus_company(session: Session, *, actor: str = "system") -> FocusBootstrapResult:
    """Establish the focus company and its aliases from configuration.

    Idempotent: running it repeatedly records nothing new.
    """
    settings = get_settings()
    result = FocusBootstrapResult()
    canonical = settings.focus_company_name
    normalized = normalize_company_name(canonical)

    if not normalized:
        result.notes.append(
            f"Configured focus company name {canonical!r} normalises to an empty "
            f"string, so it cannot be established. Check MR_FOCUS_COMPANY_NAME."
        )
        return result

    # Prefer an existing company reached through any already-known alias, so a
    # company first created by ingestion is adopted rather than duplicated.
    company_id = session.scalar(
        select(EntityAlias.entity_id).where(
            EntityAlias.entity_type == EntityType.COMPANY,
            EntityAlias.normalized_alias == normalized,
        )
    )
    if company_id is None:
        company_id = session.scalar(
            select(Company.id).where(Company.normalized_name == normalized)
        )

    if company_id is None:
        company = Company(
            canonical_name=canonical,
            normalized_name=normalized,
            is_focus_company=True,
            observed_at=dt.datetime.now(dt.UTC),
        )
        session.add(company)
        session.flush()
        company_id = company.id
        result.created_company = True
    else:
        company = session.get(Company, company_id)
        if company is not None and not company.is_focus_company:
            company.is_focus_company = True

    result.focus_company_id = company_id

    # Record the configured alias seeds as human-attributed decisions.
    for seed in [canonical, *settings.focus_company_alias_seeds]:
        seed_normalized = normalize_company_name(seed)
        if not seed_normalized:
            continue
        kind = (
            AliasKind.ABBREVIATION
            if len(seed_normalized.split()) == 1 and len(seed_normalized) <= 5
            else AliasKind.LEGAL_NAME
        )
        recorded = claim_alias(
            session, EntityType.COMPANY, company_id, seed, seed_normalized,
            ResolutionMethod.HUMAN_REVIEW, 1.0, None, alias_kind=kind,
        )
        if recorded:
            result.aliases_recorded += 1
        else:
            result.aliases_conflicted += 1
            result.notes.append(
                f"Alias {seed!r} is already held by a different company; a review "
                f"item was created rather than repointing it."
            )

    session.add(EntityMergeLog(
        entity_type=EntityType.COMPANY,
        surviving_entity_id=company_id,
        input_literal=canonical,
        resolution_method=ResolutionMethod.HUMAN_REVIEW,
        score=1.0,
        rationale=(
            f"Focus company established from configuration "
            f"(MR_FOCUS_COMPANY_NAME={canonical!r}) with "
            f"{len(settings.focus_company_alias_seeds)} configured alias seeds."
        ),
        decided_by=actor,
    ))

    result.subsidiaries_linked, result.candidates_queued = _link_group_members(
        session, company_id, canonical, actor
    )
    session.flush()
    return result


def _link_group_members(
    session: Session, focus_id: uuid.UUID, canonical: str, actor: str
) -> tuple[int, int]:
    """Link companies that are confidently part of the focus group.

    Confident means: every identity-bearing token of the focus company's
    normalised name is present in the candidate's. "dr reddys" is contained in
    "dr reddys inc" and in "dr reddys new york", so both link. A company called
    "Reddy Pharmaceuticals" shares only one token and does not.

    Anything that shares *some* but not all tokens is queued for review rather
    than linked, because a wrongly-attributed subsidiary would hand the focus
    company another firm's capabilities.
    """
    focus = session.get(Company, focus_id)
    if focus is None:
        return 0, 0
    focus_tokens = company_core_tokens(focus.normalized_name)
    if not focus_tokens:
        return 0, 0

    anchor = sorted(focus_tokens, key=len, reverse=True)[0]
    candidates = session.scalars(
        select(Company).where(
            Company.id != focus_id,
            Company.normalized_name.like(f"%{anchor}%"),
        )
    ).all()

    linked = 0
    queued = 0
    for candidate in candidates:
        if candidate.parent_company_id == focus_id or candidate.is_focus_company:
            continue
        candidate_tokens = company_core_tokens(candidate.normalized_name)
        if focus_tokens <= candidate_tokens:
            candidate.parent_company_id = focus_id
            session.add(EntityMergeLog(
                entity_type=EntityType.COMPANY,
                surviving_entity_id=focus_id,
                merged_entity_id=candidate.id,
                input_literal=candidate.canonical_name,
                resolution_method=ResolutionMethod.CONTEXTUAL,
                score=1.0,
                rationale=(
                    f"Linked as a group member of {canonical}: every identity token "
                    f"of the focus company name ({sorted(focus_tokens)}) is present "
                    f"in {candidate.canonical_name!r}. Linked as parent/child; the "
                    f"companies are NOT merged."
                ),
                decided_by=actor,
            ))
            linked += 1
        elif focus_tokens & candidate_tokens:
            already = session.scalar(
                select(ReviewQueueItem.id).where(
                    ReviewQueueItem.item_kind == "focus_group_candidate",
                    ReviewQueueItem.input_literal == candidate.canonical_name,
                )
            )
            if already:
                continue
            session.add(ReviewQueueItem(
                item_kind="focus_group_candidate",
                entity_type=EntityType.COMPANY,
                input_literal=candidate.canonical_name,
                candidate_payload={
                    "candidate_entity_id": str(candidate.id),
                    "focus_entity_id": str(focus_id),
                    "shared_tokens": sorted(focus_tokens & candidate_tokens),
                    "focus_tokens": sorted(focus_tokens),
                    "candidate_tokens": sorted(candidate_tokens),
                },
                proposed_action=(
                    f"{candidate.canonical_name!r} shares some but not all identity "
                    f"tokens with {canonical}. Decide whether it is part of the group. "
                    f"It has NOT been linked: a wrongly attributed subsidiary would "
                    f"credit the focus company with another firm's capabilities."
                ),
                confidence=0.5,
                status=ReviewStatus.PENDING,
            ))
            queued += 1
    return linked, queued


def focus_company_ids(session: Session) -> set[uuid.UUID]:
    """The focus company and its linked group members."""
    direct = set(session.scalars(
        select(Company.id).where(Company.is_focus_company.is_(True))
    ).all())
    if not direct:
        return set()
    children = set(session.scalars(
        select(Company.id).where(Company.parent_company_id.in_(direct))
    ).all())
    return direct | children
