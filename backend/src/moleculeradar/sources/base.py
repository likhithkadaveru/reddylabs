"""Connector contract.

Every source connector implements the same four-stage shape:

    discover()  -> what artefacts exist and whether they changed
    fetch()     -> retrieve bytes, record a SourceDocument with a checksum
    parse()     -> typed staging records, quarantining anything malformed
    load()      -> resolve entities, write canonical rows and emit events

Properties the contract enforces:

* **Idempotent.** Every canonical write is keyed on a `dedupe_key`, so re-running
  a load updates rather than duplicates. A retried or overlapping job cannot
  inflate counts or the opportunity feed.
* **Transactional.** A failure rolls the whole run back. Partially-applied
  ingestion is the one failure mode that silently corrupts a dataset, and it is
  much worse than no ingestion.
* **Provenance-complete.** No canonical row is written without an `Evidence` row
  pointing at the `SourceDocument` it came from.
* **Quarantine rather than crash.** A record we cannot parse goes to
  `quarantined_record` with a reason and is counted; it does not abort the run or
  get silently dropped.
* **Checksum-gated.** If the artefact's SHA-256 matches the previous retrieval we
  skip the load entirely. Monthly and quarterly files are republished unchanged
  far more often than they change.
"""

from __future__ import annotations

import abc
import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.enums import (
    AccessMode,
    ExtractionMethod,
    RunStatus,
    SourceStatus,
    VerificationLevel,
)
from ..core.models import (
    Evidence,
    IngestionRun,
    QuarantinedRecord,
    Source,
    SourceDocument,
)


def sanitize_for_storage(value: Any) -> Any:
    """Strip characters Postgres cannot store in text or JSONB columns.

    Postgres rejects NUL (U+0000) in both `text` and `jsonb`. Source artefacts are
    sometimes not what their Content-Type claims, and reading binary as text
    produces strings full of NUL and other control characters. Without this, a
    malformed download raises a DataError *while being quarantined*, which aborts
    the whole ingestion run — exactly the opposite of what quarantine is for.

    Applied recursively so nested payloads are safe too.
    """
    if isinstance(value, str):
        return value.replace("\x00", "").translate(_CONTROL_CHAR_MAP)
    if isinstance(value, dict):
        return {sanitize_for_storage(k): sanitize_for_storage(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_for_storage(v) for v in value]
    return value


# Control characters other than tab, newline and carriage return carry no meaning
# in source data and break downstream rendering.
_CONTROL_CHAR_MAP = {
    c: None for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)
}
_CONTROL_CHAR_MAP[0x7F] = None


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Static metadata about a source, mirroring docs/DATA_SOURCE_REGISTER.md.

    Kept in code so the register and the running system cannot drift: the
    `moleculeradar sources list` command prints this, and a reviewer can diff it
    against the document.
    """

    key: str
    name: str
    publisher: str
    homepage_url: str
    licence: str
    licence_url: str | None
    access_mode: AccessMode
    status: SourceStatus
    verification_level: VerificationLevel
    expected_update_cadence: str
    attribution_text: str | None
    terms_notes: str
    # The hosts this connector needs. Printed by `sources verify` so an operator
    # can see exactly what to allow through an egress policy.
    required_hosts: tuple[str, ...]


@dataclass(slots=True)
class FetchedArtefact:
    """Raw bytes plus everything needed to record provenance."""

    locator: str
    content: bytes
    media_type: str | None = None
    http_status: int | None = None
    published_at: dt.datetime | None = None
    source_last_updated: dt.date | None = None
    disclaimer_text: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(slots=True)
class ParsedRecord:
    """One normalized staging record, with the natural key for idempotency."""

    dedupe_key: str
    payload: dict[str, Any]
    event_date: dt.date | None = None
    published_at: dt.datetime | None = None


@dataclass(slots=True)
class LoadResult:
    records_processed: int = 0
    records_inserted: int = 0
    records_updated: int = 0
    records_unchanged: int = 0
    records_quarantined: int = 0
    events_emitted: int = 0
    notes: list[str] = field(default_factory=list)

    def merge(self, other: LoadResult) -> None:
        self.records_processed += other.records_processed
        self.records_inserted += other.records_inserted
        self.records_updated += other.records_updated
        self.records_unchanged += other.records_unchanged
        self.records_quarantined += other.records_quarantined
        self.events_emitted += other.events_emitted
        self.notes.extend(other.notes)


class SourceConnector(abc.ABC):
    """Base class for all connectors."""

    spec: SourceSpec

    def __init__(self, session: Session) -> None:
        self.session = session
        self._run: IngestionRun | None = None
        self._source: Source | None = None

    # -- source row --------------------------------------------------------

    def ensure_source(self) -> Source:
        """Upsert the `source` row from the connector's spec.

        The spec is authoritative: editing it updates the database, so licence
        and cadence metadata cannot silently drift from the register.
        """
        if self._source is not None:
            return self._source
        source = self.session.scalar(select(Source).where(Source.key == self.spec.key))
        if source is None:
            source = Source(key=self.spec.key)
            self.session.add(source)
        source.name = self.spec.name
        source.publisher = self.spec.publisher
        source.homepage_url = self.spec.homepage_url
        source.licence = self.spec.licence
        source.licence_url = self.spec.licence_url
        source.access_mode = self.spec.access_mode
        source.status = self.spec.status
        source.verification_level = self.spec.verification_level
        source.expected_update_cadence = self.spec.expected_update_cadence
        source.attribution_text = self.spec.attribution_text
        source.terms_notes = self.spec.terms_notes
        source.is_enabled = self.spec.status is SourceStatus.ACTIVE
        self.session.flush()
        self._source = source
        return source

    # -- run bookkeeping ---------------------------------------------------

    def start_run(self, job_name: str, as_of_date: dt.date | None = None) -> IngestionRun:
        run = IngestionRun(
            source_key=self.spec.key,
            job_name=job_name,
            status=RunStatus.RUNNING,
            started_at=dt.datetime.now(dt.UTC),
            as_of_date=as_of_date,
        )
        self.session.add(run)
        self.session.flush()
        self._run = run
        return run

    def finish_run(self, result: LoadResult, status: RunStatus | None = None) -> None:
        """Close out a run, deriving its status from the result.

        The derivation lives here rather than in `ingest()` so that every caller
        gets it right: a run that quarantined records did not fully succeed and
        must not report SUCCEEDED, whoever closed it.
        """
        if self._run is None:
            return
        if status is None:
            status = (
                RunStatus.PARTIAL if result.records_quarantined else RunStatus.SUCCEEDED
            )
        now = dt.datetime.now(dt.UTC)
        self._run.finished_at = now
        self._run.duration_seconds = (now - self._run.started_at).total_seconds()
        self._run.status = status
        self._run.records_processed = result.records_processed
        self._run.records_inserted = result.records_inserted
        self._run.records_updated = result.records_updated
        self._run.records_unchanged = result.records_unchanged
        self._run.records_quarantined = result.records_quarantined
        self._run.events_emitted = result.events_emitted
        self._run.metrics = {"notes": result.notes}
        self.session.flush()

    def fail_run(self, error: BaseException) -> None:
        if self._run is None:
            return
        now = dt.datetime.now(dt.UTC)
        self._run.finished_at = now
        self._run.duration_seconds = (now - self._run.started_at).total_seconds()
        self._run.status = RunStatus.FAILED
        self._run.error_count = 1
        self._run.error_text = f"{type(error).__name__}: {error}"
        self.session.flush()

    # -- provenance --------------------------------------------------------

    def record_document(self, artefact: FetchedArtefact) -> tuple[SourceDocument, bool]:
        """Record a retrieval. Returns (document, is_new).

        `is_new` is False when the checksum matches a previous retrieval, which
        tells the caller it can skip parsing and loading entirely.
        """
        source = self.ensure_source()
        existing = self.session.scalar(
            select(SourceDocument).where(
                SourceDocument.source_id == source.id,
                SourceDocument.sha256 == artefact.sha256,
            )
        )
        if existing is not None:
            return existing, False

        doc = SourceDocument(
            source_id=source.id,
            locator=artefact.locator,
            sha256=artefact.sha256,
            retrieved_at=dt.datetime.now(dt.UTC),
            published_at=artefact.published_at,
            source_last_updated=artefact.source_last_updated,
            byte_size=len(artefact.content),
            media_type=artefact.media_type,
            http_status=artefact.http_status,
            disclaimer_text=artefact.disclaimer_text,
            ingestion_run_id=self._run.id if self._run else None,
        )
        self.session.add(doc)
        self.session.flush()

        source.last_live_verified_at = dt.datetime.now(dt.UTC)
        # A live retrieval is the only thing that may promote a source's
        # verification level. Documentation alone never does.
        source.verification_level = VerificationLevel.LIVE_VERIFIED
        self.session.flush()
        return doc, True

    def make_evidence(
        self,
        doc: SourceDocument,
        claim_text: str,
        structured_record: dict[str, Any],
        *,
        event_date: dt.date | None = None,
        published_at: dt.datetime | None = None,
        source_url: str | None = None,
        confidence: float = 1.0,
        extraction_method: ExtractionMethod = ExtractionMethod.DETERMINISTIC_PARSE,
        llm_extraction_id: uuid.UUID | None = None,
    ) -> Evidence:
        ev = Evidence(
            claim_text=sanitize_for_storage(claim_text),
            source_document_id=doc.id,
            structured_record=sanitize_for_storage(structured_record),
            source_url=source_url,
            extraction_method=extraction_method,
            llm_extraction_id=llm_extraction_id,
            confidence=confidence,
            event_date=event_date,
            # Publication is what made the fact knowable; fall back to the
            # artefact's own publication date.
            published_at=published_at or doc.published_at,
            retrieved_at=doc.retrieved_at,
            observed_at=dt.datetime.now(dt.UTC),
        )
        self.session.add(ev)
        self.session.flush()
        return ev

    def quarantine(
        self, reason_code: str, reason_detail: str, raw_record: dict[str, Any] | None = None
    ) -> None:
        """Record a rejected record. Must never itself raise.

        Text is sanitised because quarantining malformed binary input is precisely
        the case where unstorable characters appear, and a quarantine write that
        fails would abort the run it was meant to let continue. Detail is truncated
        so one pathological record cannot bloat the table.
        """
        self.session.add(
            QuarantinedRecord(
                ingestion_run_id=self._run.id if self._run else None,
                source_key=self.spec.key,
                reason_code=reason_code,
                reason_detail=sanitize_for_storage(reason_detail)[:4000],
                raw_record=sanitize_for_storage(raw_record),
            )
        )

    # -- the pipeline ------------------------------------------------------

    @abc.abstractmethod
    def fetch(self, client: Any) -> list[FetchedArtefact]:
        """Retrieve the artefacts for this source."""

    @abc.abstractmethod
    def load(self, doc: SourceDocument, artefact: FetchedArtefact) -> LoadResult:
        """Parse and promote one artefact to canonical rows and events."""

    def ingest(self, client: Any, *, job_name: str | None = None) -> LoadResult:
        """Run the full pipeline for this source.

        The caller owns the transaction. On exception the run row is marked FAILED
        and the exception propagates so the surrounding transaction rolls back —
        the database is left exactly as it was.
        """
        result = LoadResult()
        self.start_run(job_name or f"{self.spec.key}:full")
        try:
            artefacts = self.fetch(client)
            if not artefacts:
                result.notes.append("No artefacts returned by fetch().")
            for artefact in artefacts:
                doc, is_new = self.record_document(artefact)
                if not is_new:
                    result.notes.append(
                        f"{artefact.locator}: checksum unchanged since last retrieval; "
                        f"load skipped."
                    )
                    continue
                result.merge(self.load(doc, artefact))
            self.finish_run(result)
            return result
        except BaseException as exc:
            self.fail_run(exc)
            raise
