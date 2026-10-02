"""ClinicalTrials.gov API v2 connector.

Access is via the sanctioned REST API only. NLM has published specifically on the
unsuitability of screen-scraping the site, and the API exists precisely so third
parties do not need to; `docs/DATA_SOURCE_REGISTER.md` records screen-scraping as
REJECTED.

**Contract verification status: DOC-VERIFIED, not LIVE-VERIFIED.** The v2 response
shape (`studies[].protocolSection.*`, `nextPageToken`) comes from NLM's published
API documentation. No live response has been observed, so every field access is
defensive and an unexpected shape quarantines with what it saw.

Two modelling points worth noting:

* A trial's **publication** date (`lastUpdatePostDate`, or `studyFirstPostDate`) is
  what made it knowable, while `startDate` is when it happened. Both are stored,
  and the temporal gate uses publication — a trial that started in 2022 but was
  first posted in 2023 was not knowable in 2022.
* Phase changes are detected by diffing against what we already hold, because the
  API returns current state rather than a change feed.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import select

from ..core.enums import (
    AccessMode,
    CompanyRole,
    EntityType,
    EventType,
    IdentifierScheme,
    SourceStatus,
    VerificationLevel,
)
from ..core.models import (
    ClinicalTrial,
    CompanyRoleAssertion,
    Event,
    ExternalIdentifier,
    Indication,
    SourceDocument,
)
from ..resolution.engine import CompanyResolver, MoleculeResolver
from ..resolution.normalize import normalize_molecule_name
from .base import FetchedArtefact, LoadResult, SourceConnector, SourceSpec

CTGOV_ENDPOINT = "https://clinicaltrials.gov/api/v2/studies"
CTGOV_HOME = "https://clinicaltrials.gov/data-api/api"
PAGE_SIZE = 100

SPEC = SourceSpec(
    key="clinicaltrials_gov",
    name="ClinicalTrials.gov API v2",
    publisher="U.S. National Library of Medicine",
    homepage_url=CTGOV_HOME,
    licence="NLM terms and conditions apply to all data however acquired",
    licence_url="https://clinicaltrials.gov/about-site/terms-conditions",
    access_mode=AccessMode.API,
    status=SourceStatus.ACTIVE,
    verification_level=VerificationLevel.DOC_VERIFIED,
    expected_update_cadence="daily",
    attribution_text=(
        "Source: ClinicalTrials.gov, U.S. National Library of Medicine. "
        "Used via the public API in accordance with NLM terms and conditions."
    ),
    terms_notes=(
        "API is the sanctioned access route; screen-scraping is explicitly rejected "
        "in our register. Published numeric rate limits were not corroborated during "
        "Phase 0, so the client runs serialised and conservatively by default and the "
        "real limit must be confirmed on the first live run."
    ),
    required_hosts=("clinicaltrials.gov",),
)

# Intervention types that denote a substance. Behavioural and device arms do not
# evidence molecule development and would inflate developer counts.
_SUBSTANCE_INTERVENTION_TYPES = {"DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT", "DIETARY_SUPPLEMENT"}

# Intervention names that are not molecules. Counting a placebo arm as development
# activity would be a straightforward error.
_NON_MOLECULE_NAMES = {
    "placebo", "placebos", "normal saline", "saline", "saline solution", "vehicle",
    "sham", "standard of care", "best supportive care", "no intervention",
    "matching placebo", "placebo comparator", "water", "dextrose", "sugar pill",
}


def parse_ctgov_date(value: Any) -> dt.date | None:
    """Parse a ClinicalTrials.gov date, which may be YYYY, YYYY-MM or YYYY-MM-DD.

    A partial date is resolved to the first of the period. That is a real
    imprecision and it is recorded in the evidence record rather than hidden.
    """
    if not value:
        return None
    text = str(value).strip()
    for fmt, _ in (("%Y-%m-%d", "day"), ("%Y-%m", "month"), ("%Y", "year")):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def extract_molecule_candidates(interventions: list[dict]) -> list[str]:
    """Substance intervention names worth attempting to resolve.

    Filters out placebo and non-substance arms, which would otherwise create
    molecules called "Placebo" and credit sponsors with developing them.
    """
    out: list[str] = []
    for iv in interventions:
        iv_type = str(iv.get("type") or "").strip().upper()
        if iv_type and iv_type not in _SUBSTANCE_INTERVENTION_TYPES:
            continue
        name = str(iv.get("name") or "").strip()
        if not name:
            continue
        normalized = normalize_molecule_name(name)
        if not normalized or normalized in _NON_MOLECULE_NAMES:
            continue
        if any(token in normalized for token in ("placebo", "saline", "sham")):
            continue
        out.append(name)
    return out


class ClinicalTrialsConnector(SourceConnector):
    spec = SPEC

    def __init__(
        self, session, *, query_term: str | None = None, max_pages: int | None = None
    ) -> None:
        super().__init__(session)
        self.query_term = query_term
        self.max_pages = max_pages

    def fetch(self, client: Any) -> list[FetchedArtefact]:
        artefacts: list[FetchedArtefact] = []
        page_token: str | None = None
        pages = 0
        while True:
            params: dict[str, Any] = {
                "pageSize": PAGE_SIZE,
                "countTotal": "true",
            }
            if self.query_term:
                params["query.term"] = self.query_term
            if page_token:
                params["pageToken"] = page_token

            payload = client.get_json(CTGOV_ENDPOINT, params=params)
            artefacts.append(FetchedArtefact(
                locator=f"{CTGOV_ENDPOINT}?pageToken={page_token or 'first'}"
                        + (f"&query.term={self.query_term}" if self.query_term else ""),
                content=json.dumps(payload, sort_keys=True).encode("utf-8"),
                media_type="application/json",
                http_status=200,
            ))
            pages += 1
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
            if self.max_pages is not None and pages >= self.max_pages:
                break
        return artefacts

    def load(self, doc: SourceDocument, artefact: FetchedArtefact) -> LoadResult:
        result = LoadResult()
        try:
            payload = json.loads(artefact.content)
        except ValueError as exc:
            self.quarantine("invalid_json", f"ClinicalTrials.gov page not JSON: {exc}")
            result.records_quarantined += 1
            return result

        studies = payload.get("studies")
        if studies is None:
            self.quarantine(
                "schema_mismatch",
                f"Response has no 'studies' key. Top-level keys: "
                f"{sorted(payload.keys())}",
                {"keys": sorted(payload.keys())},
            )
            result.records_quarantined += 1
            return result

        company_resolver = CompanyResolver(self.session)
        molecule_resolver = MoleculeResolver(self.session)

        for study in studies:
            result.records_processed += 1
            protocol = study.get("protocolSection") or {}
            ident = protocol.get("identificationModule") or {}
            nct_id = str(ident.get("nctId") or "").strip()
            if not nct_id:
                self.quarantine("incomplete_record", "Study without an NCT ID.", study)
                result.records_quarantined += 1
                continue

            status_mod = protocol.get("statusModule") or {}
            design = protocol.get("designModule") or {}
            sponsor_mod = protocol.get("sponsorCollaboratorsModule") or {}
            arms = protocol.get("armsInterventionsModule") or {}
            conditions_mod = protocol.get("conditionsModule") or {}
            locations_mod = protocol.get("contactsLocationsModule") or {}

            lead = (sponsor_mod.get("leadSponsor") or {})
            sponsor_literal = str(lead.get("name") or "").strip()
            sponsor_id = None
            if sponsor_literal:
                sponsor_id = company_resolver.resolve(
                    sponsor_literal, source_document_id=doc.id
                ).entity_id

            phases = design.get("phases") or []
            phase = ",".join(str(p) for p in phases) if phases else None
            overall_status = str(status_mod.get("overallStatus") or "").strip() or None
            start_date = parse_ctgov_date(
                (status_mod.get("startDateStruct") or {}).get("date")
            )
            primary_completion = parse_ctgov_date(
                (status_mod.get("primaryCompletionDateStruct") or {}).get("date")
            )
            # Publication is what made the record knowable.
            last_update_posted = parse_ctgov_date(
                (status_mod.get("lastUpdatePostDateStruct") or {}).get("date")
            )
            first_posted = parse_ctgov_date(
                (status_mod.get("studyFirstPostDateStruct") or {}).get("date")
            )
            published_date = first_posted or last_update_posted
            published_at = (
                dt.datetime.combine(published_date, dt.time.min, tzinfo=dt.UTC)
                if published_date else None
            )

            candidates = extract_molecule_candidates(arms.get("interventions") or [])
            molecule_id = None
            if candidates:
                molecule_id = molecule_resolver.resolve(
                    candidates[0], source_document_id=doc.id
                ).entity_id

            countries = sorted({
                str(loc.get("country")).strip()
                for loc in (locations_mod.get("locations") or [])
                if loc.get("country")
            })
            indication_id = self._ensure_indication(conditions_mod.get("conditions") or [])
            enrollment = (design.get("enrollmentInfo") or {}).get("count")

            evidence = self.make_evidence(
                doc,
                claim_text=(
                    f"ClinicalTrials.gov records study {nct_id} "
                    f"('{ident.get('briefTitle') or 'untitled'}') sponsored by "
                    f"{sponsor_literal or 'an unnamed sponsor'}, phase "
                    f"{phase or 'not stated'}, status {overall_status or 'not stated'}."
                ),
                structured_record={
                    "nctId": nct_id, "leadSponsor": sponsor_literal, "phases": phases,
                    "overallStatus": overall_status,
                    "startDate": start_date.isoformat() if start_date else None,
                    "startDateRaw": (status_mod.get("startDateStruct") or {}).get("date"),
                    "studyFirstPostDate": first_posted.isoformat() if first_posted else None,
                    "interventions": candidates,
                    "countries": countries,
                },
                event_date=start_date,
                published_at=published_at,
                source_url=f"https://clinicaltrials.gov/study/{nct_id}",
            )

            existing = self.session.scalar(
                select(ClinicalTrial).where(ClinicalTrial.nct_id == nct_id)
            )
            if existing is None:
                trial = ClinicalTrial(
                    nct_id=nct_id, brief_title=ident.get("briefTitle"),
                    sponsor_company_id=sponsor_id, sponsor_literal=sponsor_literal or None,
                    phase=phase, overall_status=overall_status,
                    study_type=str(design.get("studyType") or "") or None,
                    start_date=start_date, primary_completion_date=primary_completion,
                    last_update_posted_date=last_update_posted,
                    enrollment=int(enrollment) if isinstance(enrollment, int) else None,
                    molecule_id=molecule_id, indication_id=indication_id,
                    location_country_codes=countries or None,
                    evidence_id=evidence.id, event_date=start_date,
                    published_at=published_at, retrieved_at=doc.retrieved_at,
                )
                self.session.add(trial)
                self.session.flush()
                self.session.add(ExternalIdentifier(
                    scheme=IdentifierScheme.NCT_ID, value=nct_id,
                    entity_type=EntityType.CLINICAL_TRIAL, entity_id=trial.id,
                    first_seen_source_document_id=doc.id,
                ))
                result.records_inserted += 1

                if molecule_id is not None and published_date is not None:
                    self._emit(doc, EventType.NEW_TRIAL, trial.id, molecule_id, sponsor_id,
                               # Dated by publication: that is when it became knowable.
                               published_date,
                               f"New registered trial {nct_id} ({phase or 'phase not stated'}) "
                               f"sponsored by {sponsor_literal or 'an unnamed sponsor'}.",
                               {"nct_id": nct_id, "phase": phase,
                                "start_date": start_date.isoformat() if start_date else None},
                               evidence.id, published_at)
                    result.events_emitted += 1
            else:
                # Diff against held state: the API gives current state, not changes.
                phase_changed = phase is not None and existing.phase != phase
                status_changed = (
                    overall_status is not None and existing.overall_status != overall_status
                )
                previous_phase, previous_status = existing.phase, existing.overall_status

                existing.brief_title = ident.get("briefTitle") or existing.brief_title
                existing.sponsor_company_id = sponsor_id or existing.sponsor_company_id
                existing.phase = phase or existing.phase
                existing.overall_status = overall_status or existing.overall_status
                existing.start_date = start_date or existing.start_date
                existing.primary_completion_date = (
                    primary_completion or existing.primary_completion_date
                )
                existing.last_update_posted_date = (
                    last_update_posted or existing.last_update_posted_date
                )
                if molecule_id is not None:
                    existing.molecule_id = molecule_id
                if countries:
                    existing.location_country_codes = countries
                existing.evidence_id = evidence.id
                existing.retrieved_at = doc.retrieved_at
                if published_at is not None:
                    existing.published_at = published_at

                change_date = last_update_posted or published_date
                if (phase_changed or status_changed) and change_date is not None:
                    result.records_updated += 1
                    mol = existing.molecule_id
                    if mol is not None:
                        if phase_changed:
                            self._emit(
                                doc, EventType.TRIAL_PHASE_CHANGED, existing.id, mol,
                                sponsor_id, change_date,
                                f"Trial {nct_id} phase changed from "
                                f"{previous_phase or 'not stated'} to {phase}.",
                                {"from": previous_phase, "to": phase}, evidence.id,
                                published_at,
                            )
                            result.events_emitted += 1
                        if status_changed:
                            terminated = (overall_status or "").upper() in {
                                "TERMINATED", "WITHDRAWN", "SUSPENDED"
                            }
                            self._emit(
                                doc,
                                EventType.TRIAL_TERMINATED if terminated
                                else EventType.TRIAL_STATUS_CHANGED,
                                existing.id, mol, sponsor_id, change_date,
                                f"Trial {nct_id} status changed from "
                                f"{previous_status or 'not stated'} to {overall_status}.",
                                {"from": previous_status, "to": overall_status},
                                evidence.id, published_at,
                            )
                            result.events_emitted += 1
                else:
                    result.records_unchanged += 1

            if sponsor_id and molecule_id:
                self._ensure_sponsor_role(sponsor_id, molecule_id, evidence.id, doc)
            self.session.flush()

        return result

    def _ensure_indication(self, conditions: list[Any]) -> Any:
        if not conditions:
            return None
        name = str(conditions[0]).strip()
        if not name:
            return None
        normalized = name.lower()
        existing = self.session.scalar(
            select(Indication).where(Indication.normalized_name == normalized)
        )
        if existing is not None:
            return existing.id
        indication = Indication(name=name, normalized_name=normalized)
        self.session.add(indication)
        self.session.flush()
        return indication.id

    def _ensure_sponsor_role(
        self, company_id: Any, molecule_id: Any, evidence_id: Any,
        doc: SourceDocument,
    ) -> None:
        dedupe = f"ctgov_role:{company_id}:{molecule_id}"
        if self.session.scalar(
            select(CompanyRoleAssertion.id).where(
                CompanyRoleAssertion.dedupe_key == dedupe
            )
        ):
            return
        self.session.add(CompanyRoleAssertion(
            company_id=company_id, role=CompanyRole.TRIAL_SPONSOR,
            molecule_id=molecule_id, evidence_id=evidence_id, dedupe_key=dedupe,
            published_at=doc.published_at, retrieved_at=doc.retrieved_at,
        ))

    def _emit(
        self, doc: SourceDocument, event_type: EventType, trial_id: Any, molecule_id: Any,
        company_id: Any, event_date: dt.date, summary: str, payload: dict[str, Any],
        evidence_id: Any, published_at: dt.datetime | None,
    ) -> None:
        dedupe = f"{event_type.value}:trial:{trial_id}:{event_date}"
        if self.session.scalar(select(Event.id).where(Event.dedupe_key == dedupe)):
            return
        self.session.add(Event(
            event_type=event_type, subject_entity_type=EntityType.CLINICAL_TRIAL,
            subject_entity_id=trial_id, molecule_id=molecule_id, company_id=company_id,
            summary=summary, payload=payload, evidence_id=evidence_id,
            dedupe_key=dedupe, event_date=event_date,
            published_at=published_at or doc.published_at, retrieved_at=doc.retrieved_at,
        ))
