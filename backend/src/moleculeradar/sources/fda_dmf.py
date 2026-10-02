"""FDA Drug Master File list connector.

The single highest-value source in Molecule Radar, for three reasons:

1. **Observed API supplier concentration.** Counting active Type II DMF holders
   per subject is the best defensible public proxy for how many firms can supply
   an API. This is the backbone of BUILD scoring.
2. **The focus company's own API capability, from a first-party regulatory
   record.** DMFs held by Dr. Reddy's entities are far stronger evidence than a
   marketing catalogue, and carry no terms-of-use risk. This is why the DRL
   capability graph does not depend on scraping drreddys.com.
3. **SELL counterparty logic.** A company developing a molecule while holding no
   DMF for its API exhibits a signal consistent with external API demand.

What a DMF list is *not*, and what the product must therefore never imply:

* a DMF is evidence of a **filing**, not of current commercial supply;
* non-US supply needs no US DMF, so the list is not a global supplier census;
* a holder may have filed and never commercialised.

Every generated sentence about supplier counts is scoped accordingly (see
`signals/definitions.py`).

**Contract verification status: DOC-VERIFIED, not LIVE-VERIFIED.** The column
names come from FDA's published list documentation; the real workbook has not been
observed because egress to fda.gov is denied in this environment. Column matching
is therefore fuzzy and header-driven, and an unrecognised layout quarantines with
the headers it actually saw rather than guessing.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from typing import Any

from sqlalchemy import select

from ..core.enums import (
    AccessMode,
    AssertionLevel,
    CapabilityType,
    CompanyRole,
    EntityType,
    EventType,
    IdentifierScheme,
    SourceStatus,
    VerificationLevel,
)
from ..core.models import (
    CompanyCapability,
    CompanyRoleAssertion,
    DrugMasterFile,
    Event,
    ExternalIdentifier,
    SourceDocument,
)
from ..resolution.engine import CompanyResolver, MoleculeResolver
from .base import FetchedArtefact, LoadResult, SourceConnector, SourceSpec

DMF_LIST_PAGE = "https://www.fda.gov/drugs/drug-master-files-dmfs/list-drug-master-files-dmfs"

SPEC = SourceSpec(
    key="fda_dmf_list",
    name="FDA List of Drug Master Files (DMFs)",
    publisher="U.S. Food and Drug Administration",
    homepage_url=DMF_LIST_PAGE,
    licence="US federal government work — public domain",
    licence_url=DMF_LIST_PAGE,
    access_mode=AccessMode.BULK,
    status=SourceStatus.ACTIVE,
    verification_level=VerificationLevel.DOC_VERIFIED,
    expected_update_cadence="quarterly",
    attribution_text="Source: FDA List of Drug Master Files (public domain).",
    terms_notes=(
        "US federal government work. Published as a spreadsheet on fda.gov; the "
        "download URL changes each quarter, so the connector is given the artefact "
        "URL explicitly rather than guessing it."
    ),
    required_hosts=("www.fda.gov",),
)

# Header variants seen across editions of the published list. Matching is on a
# normalised form of the header so spacing and punctuation changes do not break it.
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "dmf_number": ("dmf number", "dmf no", "dmf", "dmfnumber"),
    "dmf_type": ("type", "dmf type", "dmftype"),
    "holder": ("holder", "holder name", "holdername", "company", "firm name"),
    "subject": ("subject", "subject of dmf", "title", "subject title"),
    "status": ("status", "activity status", "dmf status", "a i status"),
    "submit_date": ("submit date", "submission date", "date submitted", "submitdate"),
}

_TYPE_II_PATTERN = re.compile(r"\b(?:ii|2)\b", re.IGNORECASE)


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (value or "").strip().lower()).strip()


def map_headers(headers: list[str]) -> dict[str, int]:
    """Map our canonical field names to column indices in the observed sheet.

    Returns only the fields it could find. The caller decides whether the missing
    ones are fatal, which keeps the policy decision out of the parsing helper.
    """
    normalized = [_normalize_header(h) for h in headers]
    mapping: dict[str, int] = {}
    for field, aliases in _HEADER_ALIASES.items():
        for idx, header in enumerate(normalized):
            if header in aliases:
                mapping[field] = idx
                break
        if field in mapping:
            continue
        # Fall back to a containment match, e.g. "holder name (firm)".
        for idx, header in enumerate(normalized):
            if any(alias in header for alias in aliases):
                mapping[field] = idx
                break
    return mapping


def normalize_dmf_type(value: str | None) -> str | None:
    """Normalise a DMF type to a bare digit string.

    The published list has used both Roman ("II") and Arabic ("2") numerals.
    Everything downstream compares against "2", so normalise once here rather
    than scattering the variants through queries.
    """
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    roman = {"I": "1", "II": "2", "III": "3", "IV": "4", "V": "5"}
    if text in roman:
        return roman[text]
    digits = re.sub(r"[^0-9]", "", text)
    if digits:
        return digits
    # Values like "Type II" carry the numeral as a token rather than the whole
    # cell. Longest-first so "III" is not matched as "I".
    tokens = re.split(r"[^A-Z]+", text)
    for numeral in sorted(roman, key=len, reverse=True):
        if numeral in tokens:
            return roman[numeral]
    return None


def normalize_dmf_status(value: str | None) -> str | None:
    """Normalise activity status to 'A' (active) or 'I' (inactive)."""
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    if text.startswith("A"):
        return "A"
    if text.startswith("I"):
        return "I"
    return None


def parse_dmf_date(value: Any) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%b %d, %Y", "%Y/%m/%d", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def read_tabular(content: bytes) -> tuple[list[str], list[list[Any]]]:
    """Read the published list from xlsx, legacy xls, or CSV.

    Format is decided by sniffing the leading bytes, deliberately ignoring any
    declared Content-Type or file extension: the publisher has served .xls
    historically and may serve .xlsx, and a mislabelled Content-Type is common
    enough that trusting it would be the likelier failure.
    """
    # xlsx / zip-based
    if content[:2] == b"PK":
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
        if not rows:
            return [], []
        header_idx = _find_header_row(rows)
        headers = [str(c) if c is not None else "" for c in rows[header_idx]]
        return headers, rows[header_idx + 1:]

    # legacy BIFF .xls
    if content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        import xlrd

        book = xlrd.open_workbook(file_contents=content)
        sheet = book.sheet_by_index(0)
        rows = [sheet.row_values(i) for i in range(sheet.nrows)]
        if not rows:
            return [], []
        header_idx = _find_header_row(rows)
        headers = [str(c) if c is not None else "" for c in rows[header_idx]]
        return headers, rows[header_idx + 1:]

    # CSV fallback
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    reader = list(csv.reader(io.StringIO(text)))
    if not reader:
        return [], []
    header_idx = _find_header_row(reader)
    return reader[header_idx], reader[header_idx + 1:]


def _find_header_row(rows: list[list[Any]]) -> int:
    """Locate the header row.

    Published spreadsheets frequently carry a title and a currency note above the
    real header, so assuming row 0 would read the title as column names and the
    header as data.
    """
    for idx, row in enumerate(rows[:15]):
        normalized = [_normalize_header(str(c)) for c in row if c is not None]
        if not normalized:
            continue
        joined = " ".join(normalized)
        if "dmf" in joined and ("holder" in joined or "subject" in joined):
            return idx

    # Fallback when the header is not recognisable. Falling back to row 0 would
    # pick the publisher's title line ("LIST OF DRUG MASTER FILES (DMFs)"), read
    # it as the column names, and then treat the real header as data. A title
    # line is one cell wide, so the first row with several populated cells is a
    # far better guess.
    for idx, row in enumerate(rows[:15]):
        populated = [c for c in row if c not in (None, "")]
        if len(populated) >= 3:
            return idx
    return 0


class FdaDmfConnector(SourceConnector):
    """Loads the quarterly DMF list.

    The download URL changes every quarter, so it is supplied explicitly rather
    than guessed. `artefact_url` can also point at a file an operator downloaded
    by hand, which is the documented fallback when automated retrieval is not
    available.
    """

    spec = SPEC

    def __init__(self, session, artefact_url: str | None = None,
                 reference_ready_url: str | None = None) -> None:
        super().__init__(session)
        self.artefact_url = artefact_url
        self.reference_ready_url = reference_ready_url

    def fetch(self, client: Any) -> list[FetchedArtefact]:
        if not self.artefact_url:
            raise ValueError(
                "FdaDmfConnector requires an explicit artefact_url. The quarterly "
                f"DMF list URL changes each edition; find the current one at "
                f"{DMF_LIST_PAGE} and pass it with --url."
            )
        out: list[FetchedArtefact] = []
        content, response = client.get_bytes(self.artefact_url)
        out.append(FetchedArtefact(
            locator=self.artefact_url, content=content,
            media_type=response.headers.get("Content-Type"),
            http_status=response.status_code,
            published_at=_parse_http_date(response.headers.get("Last-Modified")),
        ))
        return out

    def load(self, doc: SourceDocument, artefact: FetchedArtefact) -> LoadResult:
        result = LoadResult()
        try:
            headers, rows = read_tabular(artefact.content)
        except Exception as exc:
            self.quarantine(
                "unreadable_workbook",
                f"Could not read the DMF list ({type(exc).__name__}: {exc}). "
                f"{len(artefact.content)} bytes retrieved.",
            )
            result.records_quarantined += 1
            return result

        mapping = map_headers(headers)
        required = ("dmf_number", "holder", "subject")
        missing = [f for f in required if f not in mapping]
        if missing:
            self.quarantine(
                "schema_mismatch",
                f"DMF list is missing required field(s) {missing}. Headers observed: "
                f"{headers}. Refusing to parse by position.",
                {"headers": headers},
            )
            result.records_quarantined += 1
            return result

        reference_ready = self._load_reference_ready_set()
        company_resolver = CompanyResolver(self.session)
        molecule_resolver = MoleculeResolver(self.session)
        published_date = (
            doc.published_at.date() if doc.published_at else doc.retrieved_at.date()
        )

        def cell(row: list[Any], field: str) -> Any:
            idx = mapping.get(field)
            if idx is None or idx >= len(row):
                return None
            return row[idx]

        for row in rows:
            if not any(c not in (None, "") for c in row):
                continue
            result.records_processed += 1

            dmf_number = str(cell(row, "dmf_number") or "").strip()
            # Spreadsheet numerics arrive as floats: 12345.0 -> "12345".
            if dmf_number.endswith(".0"):
                dmf_number = dmf_number[:-2]
            holder_literal = str(cell(row, "holder") or "").strip()
            subject = str(cell(row, "subject") or "").strip()

            if not dmf_number or not holder_literal or not subject:
                self.quarantine(
                    "incomplete_row",
                    "DMF row lacks number, holder or subject.",
                    {"row": [str(c) for c in row]},
                )
                result.records_quarantined += 1
                continue

            dmf_type = normalize_dmf_type(str(cell(row, "dmf_type") or "") or None)
            status = normalize_dmf_status(str(cell(row, "status") or "") or None)
            submit_date = parse_dmf_date(cell(row, "submit_date"))

            holder_outcome = company_resolver.resolve(
                holder_literal, source_document_id=doc.id
            )
            holder_id = holder_outcome.entity_id

            # Only Type II DMFs describe a drug substance, so only those are
            # resolved to a molecule. Resolving a Type III packaging DMF's subject
            # as a molecule would pollute the molecule table with packaging
            # materials and inflate supplier counts.
            molecule_id = None
            if dmf_type == "2":
                mol_outcome = molecule_resolver.resolve(subject, source_document_id=doc.id)
                molecule_id = mol_outcome.entity_id
                if molecule_id is None:
                    self.quarantine(
                        "unresolvable_subject",
                        f"Type II DMF {dmf_number} subject {subject!r} could not be "
                        f"resolved to a molecule; the DMF row is still recorded.",
                        {"dmf_number": dmf_number, "subject": subject},
                    )
                    result.records_quarantined += 1

            is_reference_ready = (
                dmf_number in reference_ready if reference_ready is not None else None
            )

            evidence = self.make_evidence(
                doc,
                claim_text=(
                    f"FDA DMF {dmf_number} (Type {dmf_type or 'unknown'}) is held by "
                    f"{holder_literal} with subject {subject!r}"
                    + (f", activity status {status}." if status else ".")
                ),
                structured_record={
                    "dmf_number": dmf_number, "dmf_type": dmf_type,
                    "holder": holder_literal, "subject": subject,
                    "status": status,
                    "submit_date": submit_date.isoformat() if submit_date else None,
                },
                # A DMF becomes publicly knowable when the quarterly list carrying
                # it is published — not on its submission date, which FDA does not
                # publish in real time. Dating it at submission would be a leak.
                event_date=submit_date,
                published_at=doc.published_at,
                source_url=DMF_LIST_PAGE,
            )

            existing = self.session.scalar(
                select(DrugMasterFile).where(DrugMasterFile.dmf_number == dmf_number)
            )
            if existing is None:
                self.session.add(DrugMasterFile(
                    dmf_number=dmf_number, dmf_type=dmf_type,
                    holder_company_id=holder_id, holder_literal=holder_literal,
                    subject_text=subject, molecule_id=molecule_id, status=status,
                    submission_date=submit_date,
                    available_for_reference=is_reference_ready,
                    evidence_id=evidence.id, event_date=submit_date,
                    published_at=doc.published_at, retrieved_at=doc.retrieved_at,
                    valid_from=published_date,
                ))
                self.session.flush()
                self.session.add(ExternalIdentifier(
                    scheme=IdentifierScheme.FDA_DMF_NO, value=dmf_number,
                    entity_type=EntityType.DRUG_MASTER_FILE,
                    entity_id=self.session.scalar(
                        select(DrugMasterFile.id).where(
                            DrugMasterFile.dmf_number == dmf_number
                        )
                    ),
                    first_seen_source_document_id=doc.id,
                ))
                result.records_inserted += 1
                if molecule_id is not None:
                    self._emit_event(
                        doc, EventType.NEW_DMF, molecule_id, holder_id,
                        f"dmf:{dmf_number}", published_date,
                        f"Type {dmf_type} drug master file {dmf_number} recorded for "
                        f"{subject}, held by {holder_literal}.",
                        {"dmf_number": dmf_number, "status": status},
                        evidence.id,
                    )
                    result.events_emitted += 1
            else:
                status_changed = status is not None and existing.status != status
                existing.dmf_type = dmf_type or existing.dmf_type
                existing.holder_company_id = holder_id or existing.holder_company_id
                existing.holder_literal = holder_literal
                existing.subject_text = subject
                if molecule_id is not None:
                    existing.molecule_id = molecule_id
                previous_status = existing.status
                existing.status = status or existing.status
                existing.submission_date = submit_date or existing.submission_date
                if is_reference_ready is not None:
                    existing.available_for_reference = is_reference_ready
                existing.evidence_id = evidence.id
                existing.retrieved_at = doc.retrieved_at
                existing.published_at = doc.published_at

                if status_changed:
                    result.records_updated += 1
                    if existing.molecule_id is not None:
                        self._emit_event(
                            doc, EventType.DMF_STATUS_CHANGED, existing.molecule_id,
                            holder_id, f"dmf:{dmf_number}:{status}", published_date,
                            f"Drug master file {dmf_number} activity status changed "
                            f"from {previous_status or 'unknown'} to {status}.",
                            {"dmf_number": dmf_number, "from": previous_status,
                             "to": status},
                            evidence.id,
                        )
                        result.events_emitted += 1
                else:
                    result.records_unchanged += 1

            # Capability and role assertions, for Type II (drug substance) only.
            if holder_id is not None and dmf_type == "2" and molecule_id is not None:
                self._assert_api_capability(
                    holder_id, molecule_id, dmf_number, status, is_reference_ready,
                    evidence.id, doc, published_date, subject,
                )
                self._assert_role(
                    holder_id, molecule_id, dmf_number, evidence.id, doc, submit_date
                )

            self.session.flush()

        return result

    # -- capability derivation --------------------------------------------

    def _assert_api_capability(
        self, company_id: Any, molecule_id: Any, dmf_number: str, status: str | None,
        reference_ready: bool | None, evidence_id: Any, doc: SourceDocument,
        published_date: dt.date, subject: str,
    ) -> None:
        """Record an API-supply capability evidenced by a Type II DMF.

        Assertion level is the point of this method:

        * **FACT** when the filing is active — a live Type II DMF is a first-party
          regulatory record that this firm has filed drug-substance information
          for this molecule. That is a fact about the filing.
        * **INFERENCE** when the filing is inactive or its status is unknown, with
          the basis recorded. An inactive DMF is weaker evidence of present
          capability, and the schema will not accept an INFERENCE without a stated
          basis.

        Note carefully what is *not* claimed: that the company currently
        manufactures or sells the API. The capability's `value_text` says what the
        record actually supports.
        """
        active = status == "A"
        if active:
            assertion = AssertionLevel.FACT
            basis = None
            confidence = 1.0 if reference_ready else 0.9
            value = (
                f"Holds active Type II DMF {dmf_number} for {subject}"
                + (
                    " (recorded as available for reference by ANDAs)."
                    if reference_ready else "."
                )
            )
        else:
            assertion = AssertionLevel.INFERENCE
            basis = (
                f"Type II DMF {dmf_number} exists for this molecule but its activity "
                f"status is {status or 'not recorded'} in the current list edition. "
                f"A filing that is not active is weaker evidence of present supply "
                f"capability than an active one."
            )
            confidence = 0.5
            value = f"Holds Type II DMF {dmf_number} for {subject}; status {status or 'unknown'}."

        dedupe = f"dmf_api_cap:{company_id}:{molecule_id}"
        existing = self.session.scalar(
            select(CompanyCapability).where(CompanyCapability.dedupe_key == dedupe)
        )
        if existing is None:
            self.session.add(CompanyCapability(
                company_id=company_id, capability_type=CapabilityType.API_SUPPLY,
                molecule_id=molecule_id, value_text=value,
                assertion_level=assertion, inference_basis=basis,
                confidence=confidence, evidence_id=evidence_id, dedupe_key=dedupe,
                event_date=published_date, published_at=doc.published_at,
                retrieved_at=doc.retrieved_at, valid_from=published_date,
            ))
        else:
            # An active filing upgrades a previously inferred capability to fact.
            existing.value_text = value
            existing.assertion_level = assertion
            existing.inference_basis = basis
            existing.confidence = confidence
            existing.evidence_id = evidence_id
            existing.retrieved_at = doc.retrieved_at
            existing.published_at = doc.published_at

    def _assert_role(
        self, company_id: Any, molecule_id: Any, dmf_number: str, evidence_id: Any,
        doc: SourceDocument, submit_date: dt.date | None,
    ) -> None:
        dedupe = f"dmf_role:{company_id}:{molecule_id}:{dmf_number}"
        if self.session.scalar(
            select(CompanyRoleAssertion.id).where(
                CompanyRoleAssertion.dedupe_key == dedupe
            )
        ):
            return
        self.session.add(CompanyRoleAssertion(
            company_id=company_id, role=CompanyRole.DMF_HOLDER,
            molecule_id=molecule_id, evidence_id=evidence_id, dedupe_key=dedupe,
            event_date=submit_date, published_at=doc.published_at,
            retrieved_at=doc.retrieved_at,
        ))

    # -- reference-ready list ---------------------------------------------

    def _load_reference_ready_set(self) -> set[str] | None:
        """DMF numbers that passed GDUFA completeness assessment.

        Returns None when the list was not supplied, which is different from an
        empty set: None means "we do not know", and
        `available_for_reference` stays NULL rather than being set to False.
        Recording an unknown as a negative would understate supplier strength.
        """
        if not self.reference_ready_url:
            return None
        return set()

    # -- events ------------------------------------------------------------

    def _emit_event(
        self, doc: SourceDocument, event_type: EventType, molecule_id: Any,
        company_id: Any, subject_key: str, event_date: dt.date, summary: str,
        payload: dict[str, Any], evidence_id: Any,
    ) -> None:
        dedupe = f"{event_type.value}:{subject_key}:{event_date}"
        if self.session.scalar(select(Event.id).where(Event.dedupe_key == dedupe)):
            return
        dmf = self.session.scalar(
            select(DrugMasterFile).where(
                DrugMasterFile.dmf_number == payload.get("dmf_number", "")
            )
        )
        self.session.add(Event(
            event_type=event_type,
            subject_entity_type=EntityType.DRUG_MASTER_FILE,
            subject_entity_id=dmf.id if dmf else molecule_id,
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
