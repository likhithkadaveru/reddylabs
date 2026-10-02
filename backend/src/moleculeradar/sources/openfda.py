"""openFDA connectors: Drugs@FDA and Drug Shortages.

Both endpoints share one response envelope, so the paging, disclaimer capture and
identifier-harmonisation handling live here once:

    {"meta": {"disclaimer": ..., "license": ..., "last_updated": ...,
              "results": {"skip": N, "limit": N, "total": N}},
     "results": [...]}

Records in every openFDA category may carry a harmonised `openfda` block with
cross-identifiers (UNII, RxCUI, SPL IDs, NDCs, application_number). That block is
our main identifier bridge and is what lets a shortage record be joined to an
Orange Book molecule deterministically rather than by name.

**Contract verification status: DOC-VERIFIED, not LIVE-VERIFIED.** Field names come
from openFDA's published documentation. Every field is read defensively with
`.get()` and a missing one produces a quarantine entry, not an exception.

openFDA's own disclaimer — that the data is not for clinical decision-making and
may be incomplete — is captured onto the `SourceDocument` and carried through to
the evidence drawer, because stripping a publisher's caveat while reusing its data
would misrepresent it.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import select

from ..core.enums import (
    AccessMode,
    Agency,
    ApplicationType,
    CompanyRole,
    EntityType,
    EventType,
    IdentifierScheme,
    SourceStatus,
    VerificationLevel,
)
from ..core.models import (
    CompanyRoleAssertion,
    Event,
    ExternalIdentifier,
    RegulatoryApplication,
    RegulatorySubmission,
    SourceDocument,
)
from ..resolution.engine import CompanyResolver, MoleculeResolver
from .base import FetchedArtefact, LoadResult, SourceConnector, SourceSpec

OPENFDA_BASE = "https://api.fda.gov"
DRUGSFDA_ENDPOINT = f"{OPENFDA_BASE}/drug/drugsfda.json"
SHORTAGES_ENDPOINT = f"{OPENFDA_BASE}/drug/shortages.json"
OPENFDA_HOME = "https://open.fda.gov/"

# openFDA documents a maximum of 1000 records per call and paging via `skip`.
MAX_LIMIT = 1000
# Documented ceiling on skip-based paging; beyond this the API requires a
# different strategy (date-sliced queries or the bulk download).
MAX_SKIP = 25000


def _meta_last_updated(meta: dict[str, Any]) -> dt.date | None:
    raw = meta.get("last_updated")
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(str(raw).strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_openfda_date(value: Any) -> dt.date | None:
    """Parse an openFDA date, which is usually compact YYYYMMDD."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def first(value: Any) -> Any:
    """openFDA harmonised fields are arrays even when single-valued."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


class OpenFdaPagingMixin:
    """Shared skip/limit paging over an openFDA endpoint."""

    endpoint: str

    def _paged_fetch(
        self, client: Any, *, search: str | None = None, max_records: int | None = None
    ) -> list[FetchedArtefact]:
        artefacts: list[FetchedArtefact] = []
        skip = 0
        total: int | None = None
        settings = getattr(client, "settings", None)
        api_key = getattr(settings, "openfda_api_key", None) if settings else None

        while True:
            params: dict[str, Any] = {"limit": MAX_LIMIT, "skip": skip}
            if search:
                params["search"] = search
            if api_key:
                params["api_key"] = api_key

            payload = client.get_json(self.endpoint, params=params)
            meta = payload.get("meta") or {}
            results = payload.get("results") or []
            if total is None:
                total = ((meta.get("results") or {}).get("total")) or 0

            artefacts.append(FetchedArtefact(
                locator=f"{self.endpoint}?skip={skip}&limit={MAX_LIMIT}"
                        + (f"&search={search}" if search else ""),
                content=json.dumps(payload, sort_keys=True).encode("utf-8"),
                media_type="application/json",
                http_status=200,
                source_last_updated=_meta_last_updated(meta),
                disclaimer_text=meta.get("disclaimer"),
            ))

            skip += len(results)
            if not results or skip >= (total or 0):
                break
            if max_records is not None and skip >= max_records:
                break
            if skip >= MAX_SKIP:
                # Rather than silently truncate, stop and say so. A partial dataset
                # presented as complete would corrupt every count derived from it.
                artefacts.append(FetchedArtefact(
                    locator=f"{self.endpoint}#paging-limit-notice",
                    content=json.dumps({
                        "notice": "openFDA skip-based paging limit reached",
                        "records_retrieved": skip,
                        "total_reported": total,
                        "remedy": "Use date-sliced searches or the bulk download.",
                    }).encode("utf-8"),
                    media_type="application/json",
                ))
                break
        return artefacts


DRUGSFDA_SPEC = SourceSpec(
    key="openfda_drugsfda",
    name="openFDA Drugs@FDA",
    publisher="U.S. Food and Drug Administration (openFDA)",
    homepage_url="https://open.fda.gov/apis/drug/drugsfda/",
    licence="US federal government work — public domain",
    licence_url="https://open.fda.gov/license/",
    access_mode=AccessMode.API,
    status=SourceStatus.ACTIVE,
    verification_level=VerificationLevel.DOC_VERIFIED,
    expected_update_cadence="weekly to monthly; bulk partitions refreshed regularly",
    attribution_text="Source: openFDA Drugs@FDA (public domain).",
    terms_notes=(
        "No API key required for low volume; a key raises rate limits. openFDA "
        "disclaims completeness and clinical suitability; that disclaimer is stored "
        "on each source document and surfaced in the evidence drawer."
    ),
    required_hosts=("api.fda.gov",),
)


class DrugsAtFdaConnector(OpenFdaPagingMixin, SourceConnector):
    """Loads applications and their dated submission history.

    Submission history is the point: it gives dated filing and approval events per
    applicant, which is what `developer_momentum` and SELL counterparty discovery
    are built on.
    """

    spec = DRUGSFDA_SPEC
    endpoint = DRUGSFDA_ENDPOINT

    def __init__(self, session, max_records: int | None = None) -> None:
        super().__init__(session)
        self.max_records = max_records

    def fetch(self, client: Any) -> list[FetchedArtefact]:
        return self._paged_fetch(client, max_records=self.max_records)

    def load(self, doc: SourceDocument, artefact: FetchedArtefact) -> LoadResult:
        result = LoadResult()
        try:
            payload = json.loads(artefact.content)
        except ValueError as exc:
            self.quarantine("invalid_json", f"Drugs@FDA page was not valid JSON: {exc}")
            result.records_quarantined += 1
            return result

        if "notice" in payload:
            result.notes.append(str(payload))
            return result

        company_resolver = CompanyResolver(self.session)
        molecule_resolver = MoleculeResolver(self.session)

        for record in payload.get("results") or []:
            result.records_processed += 1
            appl_no = str(record.get("application_number") or "").strip()
            if not appl_no:
                self.quarantine("incomplete_record",
                                "Drugs@FDA record without application_number.", record)
                result.records_quarantined += 1
                continue

            sponsor_literal = str(record.get("sponsor_name") or "").strip()
            sponsor_id = None
            if sponsor_literal:
                sponsor_id = company_resolver.resolve(
                    sponsor_literal, source_document_id=doc.id
                ).entity_id

            application_type = _application_type(appl_no)
            openfda = record.get("openfda") or {}
            products = record.get("products") or []

            # Resolve the molecule from the first product's active ingredients,
            # preferring the harmonised UNII when present because an identifier
            # match is conclusive where a name match is not.
            molecule_id = None
            unii = first(openfda.get("unii"))
            rxcui = first(openfda.get("rxcui"))
            ingredient_literal = _ingredient_literal(products) or first(
                openfda.get("generic_name")
            )
            if ingredient_literal:
                molecule_id = molecule_resolver.resolve(
                    str(ingredient_literal), unii=unii, rxcui=rxcui,
                    source_document_id=doc.id,
                ).entity_id

            evidence = self.make_evidence(
                doc,
                claim_text=(
                    f"openFDA Drugs@FDA records application {appl_no} sponsored by "
                    f"{sponsor_literal or 'an unnamed sponsor'}."
                ),
                structured_record={
                    "application_number": appl_no,
                    "sponsor_name": sponsor_literal,
                    "openfda": {k: openfda.get(k) for k in
                                ("unii", "rxcui", "generic_name", "brand_name",
                                 "substance_name", "application_number")},
                    "product_count": len(products),
                },
                source_url=f"https://www.accessdata.fda.gov/scripts/cder/daf/"
                           f"index.cfm?event=overview.process&ApplNo={appl_no}",
            )

            app = self.session.scalar(
                select(RegulatoryApplication).where(
                    RegulatoryApplication.agency == Agency.FDA,
                    RegulatoryApplication.application_number == appl_no,
                )
            )
            if app is None:
                app = RegulatoryApplication(
                    agency=Agency.FDA, application_type=application_type,
                    application_number=appl_no, applicant_company_id=sponsor_id,
                    applicant_literal=sponsor_literal or None,
                    evidence_id=evidence.id, published_at=doc.published_at,
                    retrieved_at=doc.retrieved_at,
                )
                self.session.add(app)
                self.session.flush()
                self.session.add(ExternalIdentifier(
                    scheme=IdentifierScheme.FDA_APPL_NO, value=appl_no,
                    entity_type=EntityType.REGULATORY_APPLICATION, entity_id=app.id,
                    first_seen_source_document_id=doc.id,
                ))
                result.records_inserted += 1
            else:
                if sponsor_id and app.applicant_company_id is None:
                    app.applicant_company_id = sponsor_id
                if not app.applicant_literal and sponsor_literal:
                    app.applicant_literal = sponsor_literal
                result.records_updated += 1
            self.session.flush()

            if unii and molecule_id:
                self._ensure_identifier(IdentifierScheme.UNII, unii,
                                        EntityType.MOLECULE, molecule_id, doc)
            if rxcui and molecule_id:
                self._ensure_identifier(IdentifierScheme.RXCUI, str(rxcui),
                                        EntityType.MOLECULE, molecule_id, doc)

            result.merge(self._load_submissions(
                doc, app, record.get("submissions") or [], molecule_id, sponsor_id,
                appl_no, application_type,
            ))

            if sponsor_id and molecule_id:
                self._ensure_role(sponsor_id, molecule_id, application_type,
                                  evidence.id, appl_no, doc)
            self.session.flush()

        return result

    def _load_submissions(
        self, doc: SourceDocument, app: RegulatoryApplication, submissions: list[dict],
        molecule_id: Any, sponsor_id: Any, appl_no: str,
        application_type: ApplicationType,
    ) -> LoadResult:
        result = LoadResult()
        for sub in submissions:
            sub_type = str(sub.get("submission_type") or "").strip()
            sub_no = str(sub.get("submission_number") or "").strip()
            status = str(sub.get("submission_status") or "").strip()
            status_date = parse_openfda_date(sub.get("submission_status_date"))
            dedupe = f"openfda_sub:{appl_no}:{sub_type}:{sub_no}"

            if self.session.scalar(
                select(RegulatorySubmission.id).where(
                    RegulatorySubmission.dedupe_key == dedupe
                )
            ):
                result.records_unchanged += 1
                continue

            evidence = self.make_evidence(
                doc,
                claim_text=(
                    f"openFDA records submission {sub_type} {sub_no} on application "
                    f"{appl_no} with status {status or 'unknown'}"
                    + (f" dated {status_date.isoformat()}." if status_date else ".")
                ),
                structured_record=dict(sub),
                event_date=status_date,
            )
            self.session.add(RegulatorySubmission(
                regulatory_application_id=app.id, submission_type=sub_type or None,
                submission_number=sub_no or None, submission_status=status or None,
                submission_class_code=sub.get("submission_class_code"),
                review_priority=sub.get("review_priority"),
                evidence_id=evidence.id, dedupe_key=dedupe, event_date=status_date,
                published_at=doc.published_at, retrieved_at=doc.retrieved_at,
            ))
            result.records_inserted += 1

            # Only original submissions with a real date become events. A
            # supplement is not a new market entrant, and an undated submission
            # cannot be placed on a timeline.
            if status_date is None or molecule_id is None:
                continue
            is_original = sub_type.upper().startswith("ORIG")
            approved = status.upper().startswith("AP")
            if is_original and approved:
                event_type = EventType.NEW_APPROVAL
                summary = (
                    f"Original application {appl_no} ({application_type.value}) "
                    f"recorded as approved."
                )
            elif is_original:
                event_type = EventType.NEW_FILING
                summary = (
                    f"Original application {appl_no} ({application_type.value}) "
                    f"recorded with status {status}."
                )
            else:
                continue

            dedupe_evt = f"{event_type.value}:appl:{appl_no}:{sub_no}:{status_date}"
            if self.session.scalar(select(Event.id).where(Event.dedupe_key == dedupe_evt)):
                continue
            self.session.add(Event(
                event_type=event_type,
                subject_entity_type=EntityType.REGULATORY_APPLICATION,
                subject_entity_id=app.id, molecule_id=molecule_id,
                company_id=sponsor_id, summary=summary,
                payload={"application_number": appl_no, "submission_type": sub_type,
                         "submission_status": status},
                evidence_id=evidence.id, dedupe_key=dedupe_evt, event_date=status_date,
                published_at=doc.published_at, retrieved_at=doc.retrieved_at,
            ))
            result.events_emitted += 1
        return result

    def _ensure_identifier(
        self, scheme: IdentifierScheme, value: str, entity_type: EntityType,
        entity_id: Any, doc: SourceDocument,
    ) -> None:
        exists = self.session.scalar(
            select(ExternalIdentifier.id).where(
                ExternalIdentifier.scheme == scheme,
                ExternalIdentifier.value == value,
                ExternalIdentifier.entity_type == entity_type,
            )
        )
        if exists:
            return
        self.session.add(ExternalIdentifier(
            scheme=scheme, value=value, entity_type=entity_type, entity_id=entity_id,
            first_seen_source_document_id=doc.id,
        ))

    def _ensure_role(
        self, company_id: Any, molecule_id: Any, application_type: ApplicationType,
        evidence_id: Any, appl_no: str, doc: SourceDocument,
    ) -> None:
        role = (
            CompanyRole.ANDA_APPLICANT if application_type is ApplicationType.ANDA
            else CompanyRole.NDA_HOLDER
        )
        dedupe = f"openfda_role:{company_id}:{role.value}:{molecule_id}:{appl_no}"
        if self.session.scalar(
            select(CompanyRoleAssertion.id).where(
                CompanyRoleAssertion.dedupe_key == dedupe
            )
        ):
            return
        self.session.add(CompanyRoleAssertion(
            company_id=company_id, role=role, molecule_id=molecule_id,
            evidence_id=evidence_id, dedupe_key=dedupe,
            published_at=doc.published_at, retrieved_at=doc.retrieved_at,
        ))


SHORTAGES_SPEC = SourceSpec(
    key="openfda_shortages",
    name="openFDA Drug Shortages",
    publisher="U.S. Food and Drug Administration (openFDA)",
    homepage_url="https://open.fda.gov/apis/drug/drugshortages/",
    licence="US federal government work — public domain",
    licence_url="https://open.fda.gov/license/",
    access_mode=AccessMode.API,
    status=SourceStatus.ACTIVE,
    verification_level=VerificationLevel.DOC_VERIFIED,
    expected_update_cadence="frequent; status changes are the purpose of the dataset",
    attribution_text="Source: openFDA Drug Shortages (public domain).",
    terms_notes="US scope only. Absence of a shortage record does not imply adequate supply.",
    required_hosts=("api.fda.gov",),
)


class DrugShortagesConnector(OpenFdaPagingMixin, SourceConnector):
    """Loads current and resolved US drug shortage records as dated events."""

    spec = SHORTAGES_SPEC
    endpoint = SHORTAGES_ENDPOINT

    def __init__(self, session, max_records: int | None = None) -> None:
        super().__init__(session)
        self.max_records = max_records

    def fetch(self, client: Any) -> list[FetchedArtefact]:
        return self._paged_fetch(client, max_records=self.max_records)

    def load(self, doc: SourceDocument, artefact: FetchedArtefact) -> LoadResult:
        result = LoadResult()
        try:
            payload = json.loads(artefact.content)
        except ValueError as exc:
            self.quarantine("invalid_json", f"Shortages page was not valid JSON: {exc}")
            result.records_quarantined += 1
            return result
        if "notice" in payload:
            result.notes.append(str(payload))
            return result

        company_resolver = CompanyResolver(self.session)
        molecule_resolver = MoleculeResolver(self.session)

        for record in payload.get("results") or []:
            result.records_processed += 1
            openfda = record.get("openfda") or {}
            generic = (
                record.get("generic_name")
                or first(openfda.get("generic_name"))
                or first(openfda.get("substance_name"))
            )
            if not generic:
                self.quarantine("incomplete_record",
                                "Shortage record with no identifiable ingredient.", record)
                result.records_quarantined += 1
                continue

            unii = first(openfda.get("unii"))
            rxcui = first(openfda.get("rxcui"))
            molecule_id = molecule_resolver.resolve(
                str(generic), unii=unii, rxcui=rxcui, source_document_id=doc.id
            ).entity_id
            if molecule_id is None:
                self.quarantine("unresolvable_molecule",
                                f"Shortage ingredient {generic!r} unresolvable.", record)
                result.records_quarantined += 1
                continue

            manufacturer = (
                record.get("company_name") or first(openfda.get("manufacturer_name"))
            )
            company_id = None
            if manufacturer:
                company_id = company_resolver.resolve(
                    str(manufacturer), source_document_id=doc.id
                ).entity_id

            status = str(record.get("status") or "").strip()
            initial = parse_openfda_date(
                record.get("initial_posting_date") or record.get("date_first_reported")
            )
            updated = parse_openfda_date(
                record.get("update_date") or record.get("change_date")
            )
            resolved_marker = status.lower().startswith(("resolved", "discontinu"))

            # One shortage record keyed on its own identity so repeated loads do
            # not multiply events.
            record_key = (
                str(record.get("package_ndc") or first(openfda.get("package_ndc")) or "")
                or f"{generic}:{manufacturer or 'unknown'}:{initial or 'nodate'}"
            )

            event_type = (
                EventType.SHORTAGE_RESOLVED if resolved_marker else EventType.SHORTAGE_OPENED
            )
            event_date = (updated if resolved_marker else initial) or initial or updated
            if event_date is None:
                self.quarantine(
                    "undated_shortage",
                    f"Shortage record for {generic!r} carries no usable date, so it "
                    f"cannot be placed on a timeline.",
                    record,
                )
                result.records_quarantined += 1
                continue

            evidence = self.make_evidence(
                doc,
                claim_text=(
                    f"openFDA records a drug shortage entry for {generic} "
                    f"({manufacturer or 'manufacturer not stated'}) with status "
                    f"{status or 'unknown'}."
                ),
                structured_record={
                    "generic_name": generic, "company_name": manufacturer,
                    "status": status,
                    "initial_posting_date": initial.isoformat() if initial else None,
                    "update_date": updated.isoformat() if updated else None,
                    "availability": record.get("availability"),
                    "related_info": record.get("related_info"),
                    "shortage_reason": record.get("reason"),
                },
                event_date=event_date,
                source_url="https://www.accessdata.fda.gov/scripts/drugshortages/",
            )

            dedupe = f"{event_type.value}:shortage:{record_key}:{event_date}"
            if self.session.scalar(select(Event.id).where(Event.dedupe_key == dedupe)):
                result.records_unchanged += 1
                continue

            self.session.add(Event(
                event_type=event_type,
                subject_entity_type=EntityType.MOLECULE,
                subject_entity_id=molecule_id, molecule_id=molecule_id,
                company_id=company_id,
                summary=(
                    f"Shortage {'resolved' if resolved_marker else 'reported'} for "
                    f"{generic}"
                    + (f" ({manufacturer})" if manufacturer else "")
                    + (f" — reason recorded: {record.get('reason')}"
                       if record.get("reason") else "")
                ),
                payload={"status": status, "reason": record.get("reason"),
                         "availability": record.get("availability")},
                evidence_id=evidence.id, dedupe_key=dedupe, event_date=event_date,
                published_at=doc.published_at, retrieved_at=doc.retrieved_at,
            ))
            result.records_inserted += 1
            result.events_emitted += 1
            if company_id:
                self._ensure_shortage_role(company_id, molecule_id, evidence.id, doc)
            self.session.flush()
        return result

    def _ensure_shortage_role(
        self, company_id: Any, molecule_id: Any, evidence_id: Any,
        doc: SourceDocument,
    ) -> None:
        dedupe = f"shortage_role:{company_id}:{molecule_id}"
        if self.session.scalar(
            select(CompanyRoleAssertion.id).where(
                CompanyRoleAssertion.dedupe_key == dedupe
            )
        ):
            return
        self.session.add(CompanyRoleAssertion(
            company_id=company_id, role=CompanyRole.SHORTAGE_REPORTER,
            molecule_id=molecule_id, evidence_id=evidence_id, dedupe_key=dedupe,
            published_at=doc.published_at, retrieved_at=doc.retrieved_at,
        ))


def _application_type(appl_no: str) -> ApplicationType:
    upper = appl_no.upper()
    if upper.startswith("ANDA"):
        return ApplicationType.ANDA
    if upper.startswith("NDA"):
        return ApplicationType.NDA
    if upper.startswith("BLA"):
        return ApplicationType.BLA
    return ApplicationType.UNKNOWN


def _ingredient_literal(products: list[dict]) -> str | None:
    """Best ingredient string from a Drugs@FDA products array."""
    for product in products:
        ingredients = product.get("active_ingredients") or []
        names = [str(i.get("name")).strip() for i in ingredients if i.get("name")]
        if names:
            return "; ".join(names)
    return None
