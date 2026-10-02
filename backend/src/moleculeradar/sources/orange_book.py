"""FDA Orange Book connector.

Source: the published Orange Book data file (a ZIP containing three tilde-delimited
ASCII files), updated monthly, US federal government work in the public domain.

**Contract verification status: DOC-VERIFIED, not LIVE-VERIFIED.** The field names
below come from FDA's own published file documentation, read during Phase 0; no
response has been observed, because this environment's egress policy denies
fda.gov. See docs/KNOWN_LIMITATIONS.md.

Because the contract is unverified, this parser is defensive by design:

* columns are read **by header name, not position**, so a reordering does not
  silently shift every value one column across — the failure mode that would be
  hardest to notice and most damaging;
* a missing expected column is reported once as a quarantine entry naming the
  headers actually seen, rather than raising;
* individual malformed rows are quarantined with a reason and counted.

Orange Book is a *current-state snapshot*, not a history: it carries only
unexpired patent and exclusivity data. Our event history is therefore built by
diffing successive snapshots, which is one of the reasons the raw artefacts are
retained.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import zipfile
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
    DrugProduct,
    Event,
    Exclusivity,
    ExternalIdentifier,
    Patent,
    RegulatoryApplication,
    SourceDocument,
)
from ..resolution.engine import CompanyResolver, MoleculeResolver
from ..resolution.normalize import split_combination_ingredients
from .base import FetchedArtefact, LoadResult, SourceConnector, SourceSpec

# The published download location. Recorded as a constant so a change is a code
# change with a commit message, not a silent configuration drift.
ORANGE_BOOK_ZIP_URL = "https://www.fda.gov/media/76860/download?attachment"
ORANGE_BOOK_HOMEPAGE = (
    "https://www.fda.gov/drugs/drug-approvals-and-databases/orange-book-data-files"
)

SPEC = SourceSpec(
    key="fda_orange_book",
    name="FDA Orange Book data files",
    publisher="U.S. Food and Drug Administration",
    homepage_url=ORANGE_BOOK_HOMEPAGE,
    licence="US federal government work — public domain",
    licence_url=ORANGE_BOOK_HOMEPAGE,
    access_mode=AccessMode.BULK,
    status=SourceStatus.ACTIVE,
    verification_level=VerificationLevel.DOC_VERIFIED,
    expected_update_cadence="monthly (around the end of the second week)",
    attribution_text="Source: FDA Orange Book data files (public domain).",
    terms_notes=(
        "US federal government work; automated retrieval of the published bulk file "
        "is its intended use. No API key or registration required."
    ),
    required_hosts=("www.fda.gov",),
)

# Expected headers, from FDA's published file documentation.
PRODUCTS_COLUMNS = (
    "Ingredient", "DF;Route", "Trade_Name", "Applicant", "Strength", "Appl_Type",
    "Appl_No", "Product_No", "TE_Code", "Approval_Date", "RLD", "RS", "Type",
    "Applicant_Full_Name",
)
PATENT_COLUMNS = (
    "Appl_Type", "Appl_No", "Product_No", "Patent_No", "Patent_Expire_Date_Text",
    "Drug_Substance_Flag", "Drug_Product_Flag", "Patent_Use_Code", "Delist_Requested",
)
EXCLUSIVITY_COLUMNS = (
    "Appl_Type", "Appl_No", "Product_No", "Exclusivity_Code", "Exclusivity_Date",
)

_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y")
_APPROVED_PRIOR = re.compile(r"approved prior to", re.IGNORECASE)


def parse_ob_date(value: str | None) -> dt.date | None:
    """Parse an Orange Book date.

    Handles the literal "Approved Prior to Jan 1, 1982" marker, which is not a
    date but a statement that approval predates FDA's electronic records. Mapping
    it to 1982-01-01 would assert a precision the source does not have, so it
    returns None and the caller records the fact separately.
    """
    if not value:
        return None
    text = value.strip()
    if not text or text.upper() in {"N/A", "NA", "NONE"}:
        return None
    if _APPROVED_PRIOR.search(text):
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _flag(value: str | None) -> bool | None:
    """Orange Book uses 'Y'/'N'/blank for flags; blank means unknown, not False."""
    if value is None:
        return None
    text = value.strip().upper()
    if text in {"Y", "YES"}:
        return True
    if text in {"N", "NO"}:
        return False
    return None


def read_tilde_delimited(content: bytes) -> tuple[list[dict[str, str]], list[str]]:
    """Read a tilde-delimited Orange Book file into dicts keyed by header name.

    Returns (rows, headers). Decoded as latin-1 after utf-8 fails: these files are
    described as ASCII but have historically contained stray high bytes, and
    latin-1 cannot fail, so a single bad byte never costs us the whole file.
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text), delimiter="~")
    headers = [h.strip() for h in (reader.fieldnames or [])]
    rows = [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]
    return rows, headers


class OrangeBookConnector(SourceConnector):
    spec = SPEC

    def fetch(self, client: Any) -> list[FetchedArtefact]:
        content, response = client.get_bytes(ORANGE_BOOK_ZIP_URL)
        return [
            FetchedArtefact(
                locator=ORANGE_BOOK_ZIP_URL,
                content=content,
                media_type=response.headers.get("Content-Type"),
                http_status=response.status_code,
                published_at=_parse_http_date(response.headers.get("Last-Modified")),
            )
        ]

    def load(self, doc: SourceDocument, artefact: FetchedArtefact) -> LoadResult:
        result = LoadResult()
        try:
            archive = zipfile.ZipFile(io.BytesIO(artefact.content))
        except zipfile.BadZipFile as exc:
            self.quarantine(
                "bad_archive",
                f"Orange Book download was not a readable ZIP: {exc}. "
                f"{len(artefact.content)} bytes retrieved.",
            )
            result.records_quarantined += 1
            return result

        members = {name.lower(): name for name in archive.namelist()}

        def member(stem: str) -> bytes | None:
            for lower, actual in members.items():
                if lower.endswith(f"{stem}.txt"):
                    return archive.read(actual)
            return None

        products_raw = member("products")
        if products_raw is None:
            self.quarantine(
                "missing_member",
                f"products.txt not found in Orange Book archive. Members: "
                f"{sorted(members.values())}",
            )
            result.records_quarantined += 1
            return result

        result.merge(self._load_products(doc, products_raw))
        patent_raw = member("patent")
        if patent_raw is not None:
            result.merge(self._load_patents(doc, patent_raw))
        else:
            result.notes.append("patent.txt absent from archive; patent features unavailable.")
        excl_raw = member("exclusivity")
        if excl_raw is not None:
            result.merge(self._load_exclusivity(doc, excl_raw))
        else:
            result.notes.append(
                "exclusivity.txt absent from archive; exclusivity features unavailable."
            )
        return result

    # -- products ----------------------------------------------------------

    def _load_products(self, doc: SourceDocument, raw: bytes) -> LoadResult:
        result = LoadResult()
        rows, headers = read_tilde_delimited(raw)
        missing = [c for c in ("Ingredient", "Appl_No", "Product_No") if c not in headers]
        if missing:
            self.quarantine(
                "schema_mismatch",
                f"products.txt is missing required column(s) {missing}. "
                f"Headers observed: {headers}. Refusing to parse by position.",
                {"headers": headers},
            )
            result.records_quarantined += 1
            return result

        company_resolver = CompanyResolver(self.session)
        molecule_resolver = MoleculeResolver(self.session)

        for row in rows:
            result.records_processed += 1
            appl_no = row.get("Appl_No", "").strip()
            product_no = row.get("Product_No", "").strip()
            ingredient = row.get("Ingredient", "").strip()
            if not appl_no or not ingredient:
                self.quarantine(
                    "incomplete_row",
                    "products.txt row lacks Appl_No or Ingredient.", row,
                )
                result.records_quarantined += 1
                continue

            appl_type_raw = (row.get("Appl_Type") or "").strip().upper()
            application_type = {
                "N": ApplicationType.NDA,
                "A": ApplicationType.ANDA,
            }.get(appl_type_raw, ApplicationType.UNKNOWN)

            applicant_literal = (
                row.get("Applicant_Full_Name") or row.get("Applicant") or ""
            ).strip()
            applicant_id = None
            if applicant_literal:
                outcome = company_resolver.resolve(
                    applicant_literal, source_document_id=doc.id
                )
                applicant_id = outcome.entity_id

            # A combination product is its own commercial unit, so the product row
            # is attached to the combination literal; the components are resolved
            # separately so competition can be assessed per moiety.
            components = split_combination_ingredients(ingredient)
            primary_literal = ingredient if len(components) > 1 else components[0]
            mol_outcome = molecule_resolver.resolve(
                primary_literal, source_document_id=doc.id
            )
            molecule_id = mol_outcome.entity_id
            if molecule_id is None:
                self.quarantine(
                    "unresolvable_molecule",
                    f"Ingredient {ingredient!r} could not be resolved to a molecule.",
                    row,
                )
                result.records_quarantined += 1
                continue

            approval_date = parse_ob_date(row.get("Approval_Date"))
            approved_prior = bool(_APPROVED_PRIOR.search(row.get("Approval_Date") or ""))

            application = self._upsert_application(
                doc, application_type, appl_no, applicant_id, applicant_literal,
                approval_date, row,
            )

            dedupe = f"ob_product:{appl_no}:{product_no}"
            existing = self.session.scalar(
                select(DrugProduct).where(DrugProduct.dedupe_key == dedupe)
            )
            df_route = row.get("DF;Route", "")
            dosage_form, _, route = df_route.partition(";")
            evidence = self.make_evidence(
                doc,
                claim_text=(
                    f"FDA Orange Book lists product {appl_no}-{product_no} "
                    f"({ingredient}) held by {applicant_literal or 'an unnamed applicant'}."
                ),
                structured_record=dict(row),
                event_date=approval_date,
                source_url=ORANGE_BOOK_HOMEPAGE,
            )

            fields = {
                "regulatory_application_id": application.id,
                "molecule_id": molecule_id,
                "brand_name": row.get("Trade_Name") or None,
                "product_number": product_no or None,
                "dosage_form": dosage_form.strip() or None,
                "route": route.strip() or None,
                "strength_text": row.get("Strength") or None,
                "te_code": row.get("TE_Code") or None,
                "reference_listed_drug": _flag(row.get("RLD")),
                "approval_date": approval_date,
                "marketing_status": row.get("Type") or None,
                "evidence_id": evidence.id,
                "event_date": approval_date,
                "published_at": doc.published_at,
                "retrieved_at": doc.retrieved_at,
            }
            if existing is None:
                self.session.add(DrugProduct(dedupe_key=dedupe, **fields))
                result.records_inserted += 1
                if approval_date is not None:
                    self._emit_event(
                        doc, EventType.NEW_APPROVAL, molecule_id, applicant_id,
                        EntityType.DRUG_PRODUCT, application.id, approval_date,
                        f"Approval recorded for {ingredient} "
                        f"({row.get('Trade_Name') or 'no trade name'}), "
                        f"application {appl_no}-{product_no}.",
                        {"appl_no": appl_no, "product_no": product_no,
                         "approved_prior_to_1982": approved_prior},
                        evidence_id=evidence.id,
                    )
                    result.events_emitted += 1
            else:
                changed = any(
                    getattr(existing, k) != v
                    for k, v in fields.items()
                    if k not in {"evidence_id", "retrieved_at", "published_at"}
                )
                for k, v in fields.items():
                    setattr(existing, k, v)
                if changed:
                    result.records_updated += 1
                else:
                    result.records_unchanged += 1

            if applicant_id is not None:
                self._upsert_company_role(
                    applicant_id,
                    CompanyRole.ANDA_APPLICANT if application_type is ApplicationType.ANDA
                    else CompanyRole.NDA_HOLDER,
                    molecule_id, evidence.id, appl_no, approval_date, doc,
                )
            self.session.flush()

        return result

    def _upsert_application(
        self, doc: SourceDocument, application_type: ApplicationType, appl_no: str,
        applicant_id: Any, applicant_literal: str, approval_date: dt.date | None,
        row: dict[str, str],
    ) -> RegulatoryApplication:
        app = self.session.scalar(
            select(RegulatoryApplication).where(
                RegulatoryApplication.agency == Agency.FDA,
                RegulatoryApplication.application_number == appl_no,
            )
        )
        if app is None:
            evidence = self.make_evidence(
                doc,
                claim_text=f"FDA application {appl_no} ({application_type.value}).",
                structured_record={"Appl_No": appl_no, "Appl_Type": row.get("Appl_Type")},
                event_date=approval_date,
                source_url=ORANGE_BOOK_HOMEPAGE,
            )
            app = RegulatoryApplication(
                agency=Agency.FDA,
                application_type=application_type,
                application_number=appl_no,
                applicant_company_id=applicant_id,
                applicant_literal=applicant_literal or None,
                first_approval_date=approval_date,
                evidence_id=evidence.id,
                event_date=approval_date,
                published_at=doc.published_at,
                retrieved_at=doc.retrieved_at,
            )
            self.session.add(app)
            self.session.flush()
            self.session.add(ExternalIdentifier(
                scheme=IdentifierScheme.FDA_APPL_NO, value=appl_no,
                entity_type=EntityType.REGULATORY_APPLICATION, entity_id=app.id,
                first_seen_source_document_id=doc.id,
            ))
            self.session.flush()
        else:
            # Keep the earliest approval date we have ever seen.
            if approval_date and (
                app.first_approval_date is None or approval_date < app.first_approval_date
            ):
                app.first_approval_date = approval_date
            if applicant_id and app.applicant_company_id is None:
                app.applicant_company_id = applicant_id
        return app

    def _upsert_company_role(
        self, company_id: Any, role: CompanyRole, molecule_id: Any, evidence_id: Any,
        appl_no: str, event_date: dt.date | None, doc: SourceDocument,
    ) -> None:
        dedupe = f"ob_role:{company_id}:{role.value}:{molecule_id}:{appl_no}"
        existing = self.session.scalar(
            select(CompanyRoleAssertion).where(CompanyRoleAssertion.dedupe_key == dedupe)
        )
        if existing is not None:
            return
        self.session.add(CompanyRoleAssertion(
            company_id=company_id, role=role, molecule_id=molecule_id,
            evidence_id=evidence_id, dedupe_key=dedupe, event_date=event_date,
            published_at=doc.published_at, retrieved_at=doc.retrieved_at,
        ))

    # -- patents -----------------------------------------------------------

    def _load_patents(self, doc: SourceDocument, raw: bytes) -> LoadResult:
        result = LoadResult()
        rows, headers = read_tilde_delimited(raw)
        if "Patent_No" not in headers or "Appl_No" not in headers:
            self.quarantine(
                "schema_mismatch",
                f"patent.txt missing Patent_No/Appl_No. Headers: {headers}",
                {"headers": headers},
            )
            result.records_quarantined += 1
            return result

        for row in rows:
            result.records_processed += 1
            appl_no = row.get("Appl_No", "").strip()
            patent_no = row.get("Patent_No", "").strip()
            product_no = row.get("Product_No", "").strip()
            if not appl_no or not patent_no:
                self.quarantine("incomplete_row", "patent.txt row lacks keys.", row)
                result.records_quarantined += 1
                continue

            app = self.session.scalar(
                select(RegulatoryApplication).where(
                    RegulatoryApplication.agency == Agency.FDA,
                    RegulatoryApplication.application_number == appl_no,
                )
            )
            product = self.session.scalar(
                select(DrugProduct).where(
                    DrugProduct.dedupe_key == f"ob_product:{appl_no}:{product_no}"
                )
            )
            if app is None and product is None:
                # A patent for an application absent from products.txt. Recorded
                # rather than dropped: it is a genuine inconsistency in the source
                # and the Data Health screen should show it.
                self.quarantine(
                    "orphan_patent",
                    f"Patent {patent_no} references application {appl_no} which was "
                    f"not present in products.txt.",
                    row,
                )
                result.records_quarantined += 1
                continue

            expiry = parse_ob_date(row.get("Patent_Expire_Date_Text"))
            dedupe = f"ob_patent:{appl_no}:{product_no}:{patent_no}"
            existing = self.session.scalar(select(Patent).where(Patent.dedupe_key == dedupe))
            evidence = self.make_evidence(
                doc,
                claim_text=(
                    f"FDA Orange Book lists patent {patent_no} against application "
                    f"{appl_no}-{product_no}"
                    + (f", expiring {expiry.isoformat()}." if expiry else ".")
                ),
                structured_record=dict(row),
                event_date=expiry,
                source_url=ORANGE_BOOK_HOMEPAGE,
            )
            fields = {
                "patent_number": patent_no,
                "jurisdiction": "US",
                "regulatory_application_id": app.id if app else None,
                "drug_product_id": product.id if product else None,
                "molecule_id": product.molecule_id if product else None,
                "expiry_date": expiry,
                "drug_substance_flag": _flag(row.get("Drug_Substance_Flag")),
                "drug_product_flag": _flag(row.get("Drug_Product_Flag")),
                "use_code": row.get("Patent_Use_Code") or None,
                "delist_requested": _flag(row.get("Delist_Requested")),
                "evidence_id": evidence.id,
                "event_date": expiry,
                "published_at": doc.published_at,
                "retrieved_at": doc.retrieved_at,
            }
            if existing is None:
                self.session.add(Patent(dedupe_key=dedupe, **fields))
                result.records_inserted += 1
                if product is not None and product.molecule_id is not None:
                    self._emit_event(
                        doc, EventType.NEW_PATENT_LISTING, product.molecule_id,
                        app.applicant_company_id if app else None,
                        EntityType.PATENT, product.id,
                        # The listing became knowable when this file was published,
                        # not when the patent expires.
                        doc.published_at.date() if doc.published_at
                        else doc.retrieved_at.date(),
                        f"Patent {patent_no} listed against application "
                        f"{appl_no}-{product_no}.",
                        {"patent_no": patent_no, "expiry": expiry.isoformat() if expiry else None},
                        evidence_id=evidence.id,
                    )
                    result.events_emitted += 1
            else:
                for k, v in fields.items():
                    setattr(existing, k, v)
                result.records_updated += 1
            self.session.flush()
        return result

    # -- exclusivity -------------------------------------------------------

    def _load_exclusivity(self, doc: SourceDocument, raw: bytes) -> LoadResult:
        result = LoadResult()
        rows, headers = read_tilde_delimited(raw)
        if "Exclusivity_Code" not in headers:
            self.quarantine(
                "schema_mismatch",
                f"exclusivity.txt missing Exclusivity_Code. Headers: {headers}",
                {"headers": headers},
            )
            result.records_quarantined += 1
            return result

        for row in rows:
            result.records_processed += 1
            appl_no = row.get("Appl_No", "").strip()
            product_no = row.get("Product_No", "").strip()
            code = row.get("Exclusivity_Code", "").strip()
            if not appl_no or not code:
                self.quarantine("incomplete_row", "exclusivity.txt row lacks keys.", row)
                result.records_quarantined += 1
                continue

            product = self.session.scalar(
                select(DrugProduct).where(
                    DrugProduct.dedupe_key == f"ob_product:{appl_no}:{product_no}"
                )
            )
            app = self.session.scalar(
                select(RegulatoryApplication).where(
                    RegulatoryApplication.agency == Agency.FDA,
                    RegulatoryApplication.application_number == appl_no,
                )
            )
            if product is None and app is None:
                self.quarantine(
                    "orphan_exclusivity",
                    f"Exclusivity {code} references application {appl_no} absent from "
                    f"products.txt.",
                    row,
                )
                result.records_quarantined += 1
                continue

            expiry = parse_ob_date(row.get("Exclusivity_Date"))
            dedupe = f"ob_excl:{appl_no}:{product_no}:{code}"
            existing = self.session.scalar(
                select(Exclusivity).where(Exclusivity.dedupe_key == dedupe)
            )
            evidence = self.make_evidence(
                doc,
                claim_text=(
                    f"FDA Orange Book records exclusivity {code} for application "
                    f"{appl_no}-{product_no}"
                    + (f", expiring {expiry.isoformat()}." if expiry else ".")
                ),
                structured_record=dict(row),
                event_date=expiry,
                source_url=ORANGE_BOOK_HOMEPAGE,
            )
            fields = {
                "exclusivity_code": code,
                "regulatory_application_id": app.id if app else None,
                "drug_product_id": product.id if product else None,
                "molecule_id": product.molecule_id if product else None,
                "expiry_date": expiry,
                "evidence_id": evidence.id,
                "event_date": expiry,
                "published_at": doc.published_at,
                "retrieved_at": doc.retrieved_at,
            }
            if existing is None:
                self.session.add(Exclusivity(dedupe_key=dedupe, **fields))
                result.records_inserted += 1
                if product is not None and product.molecule_id is not None:
                    self._emit_event(
                        doc, EventType.NEW_EXCLUSIVITY, product.molecule_id,
                        app.applicant_company_id if app else None,
                        EntityType.EXCLUSIVITY, product.id,
                        doc.published_at.date() if doc.published_at
                        else doc.retrieved_at.date(),
                        f"Exclusivity {code} recorded for application "
                        f"{appl_no}-{product_no}.",
                        {"code": code, "expiry": expiry.isoformat() if expiry else None},
                        evidence_id=evidence.id,
                    )
                    result.events_emitted += 1
            else:
                for k, v in fields.items():
                    setattr(existing, k, v)
                result.records_updated += 1
            self.session.flush()
        return result

    # -- events ------------------------------------------------------------

    def _emit_event(
        self, doc: SourceDocument, event_type: EventType, molecule_id: Any,
        company_id: Any, subject_type: EntityType, subject_id: Any,
        event_date: dt.date, summary: str, payload: dict[str, Any], *, evidence_id: Any,
    ) -> None:
        dedupe = f"{event_type.value}:{subject_type.value}:{subject_id}:{event_date}"
        if self.session.scalar(select(Event.id).where(Event.dedupe_key == dedupe)):
            return
        self.session.add(Event(
            event_type=event_type,
            subject_entity_type=subject_type,
            subject_entity_id=subject_id,
            molecule_id=molecule_id,
            company_id=company_id,
            summary=summary,
            payload=payload,
            evidence_id=evidence_id,
            dedupe_key=dedupe,
            event_date=event_date,
            published_at=doc.published_at,
            retrieved_at=doc.retrieved_at,
        ))


def _parse_http_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
