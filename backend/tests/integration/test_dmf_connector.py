"""FDA DMF connector tests.

The DMF list drives API supplier counts and the focus company's capability graph,
so its parsing and its FACT/INFERENCE discipline are tested closely.
"""

from __future__ import annotations

import datetime as dt
import io

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from moleculeradar.core.enums import AssertionLevel, CapabilityType, EventType
from moleculeradar.core.models import (
    Company,
    CompanyCapability,
    CompanyRoleAssertion,
    DrugMasterFile,
    Event,
    Molecule,
    QuarantinedRecord,
)
from moleculeradar.sources.base import FetchedArtefact
from moleculeradar.sources.fda_dmf import (
    FdaDmfConnector,
    map_headers,
    normalize_dmf_status,
    normalize_dmf_type,
    parse_dmf_date,
)

pytestmark = pytest.mark.integration

UTC = dt.UTC
PUBLISHED = dt.datetime(2026, 7, 19, tzinfo=UTC)

ROWS = [
    # DMF NUMBER, DMF TYPE, HOLDER NAME, SUBJECT, STATUS, SUBMIT DATE
    [31001, "II", "DR. REDDY'S LABORATORIES LTD", "TESTAZOLE", "A", "2015-06-01"],
    [31002, "II", "ALPHA SYNTHETICS INC", "TESTAZOLE", "A", "2017-02-15"],
    [31003, "II", "BETA CHEM LTD", "TESTAZOLE", "I", "2010-01-01"],
    [31004, "II", "DR. REDDY'S LABORATORIES LIMITED", "OTHERAZOLE", "A", "2018-09-09"],
    # A Type III (packaging) DMF: must NOT become a molecule or a supplier count.
    [31005, "III", "PACKAGING CO LLC", "HDPE BOTTLES", "A", "2012-03-03"],
]


def _workbook(rows=None, headers=None, title_rows: int = 2) -> bytes:
    wb = Workbook()
    ws = wb.active
    # Published spreadsheets carry title/currency notes above the real header.
    for _ in range(title_rows):
        ws.append(["LIST OF DRUG MASTER FILES (DMFs)"])
    ws.append(headers or ["DMF NUMBER", "DMF TYPE", "HOLDER NAME", "SUBJECT",
                          "STATUS", "SUBMIT DATE"])
    for row in (rows if rows is not None else ROWS):
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _artefact(content: bytes) -> FetchedArtefact:
    return FetchedArtefact(
        locator="https://www.fda.gov/media/testing/dmf-list.xlsx",
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        http_status=200, published_at=PUBLISHED,
    )


def _ingest(session, content: bytes):
    connector = FdaDmfConnector(session, artefact_url="https://example.invalid/dmf.xlsx")
    connector.start_run("test:dmf")
    artefact = _artefact(content)
    doc, is_new = connector.record_document(artefact)
    assert is_new
    result = connector.load(doc, artefact)
    connector.finish_run(result)
    session.flush()
    return connector, doc, result


class TestNormalisers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("II", "2"), ("2", "2"), ("ii", "2"), ("Type II", "2"), ("III", "3"),
         ("4", "4"), ("", None), (None, None)],
    )
    def test_dmf_type_roman_and_arabic(self, raw, expected) -> None:
        """The published list has used both numeral systems for the same thing."""
        assert normalize_dmf_type(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("A", "A"), ("Active", "A"), ("I", "I"), ("Inactive", "I"),
         ("", None), (None, None), ("unknown", None)],
    )
    def test_status_normalisation(self, raw, expected) -> None:
        assert normalize_dmf_status(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("2015-06-01", dt.date(2015, 6, 1)), ("06/01/2015", dt.date(2015, 6, 1)),
         ("1-Jun-2015", dt.date(2015, 6, 1)), (dt.date(2015, 6, 1), dt.date(2015, 6, 1)),
         ("", None), (None, None), ("not a date", None)],
    )
    def test_date_parsing(self, raw, expected) -> None:
        assert parse_dmf_date(raw) == expected

    def test_header_mapping_tolerates_variants(self) -> None:
        mapping = map_headers(["DMF No.", "Type", "Holder Name (Firm)", "Subject of DMF",
                               "Activity Status", "Date Submitted"])
        assert mapping["dmf_number"] == 0
        assert mapping["dmf_type"] == 1
        assert mapping["holder"] == 2
        assert mapping["subject"] == 3
        assert mapping["status"] == 4
        assert mapping["submit_date"] == 5


class TestDmfLoading:
    def test_loads_all_dmfs_including_non_type_two(self, session) -> None:
        _ingest(session, _workbook())
        dmfs = {d.dmf_number: d for d in session.scalars(select(DrugMasterFile)).all()}
        assert set(dmfs) == {"31001", "31002", "31003", "31004", "31005"}
        # Spreadsheet numerics must not arrive as "31001.0".
        assert all("." not in n for n in dmfs)

    def test_type_three_dmf_does_not_create_a_molecule(self, session) -> None:
        """A packaging DMF's subject is not a drug substance.

        Resolving it as a molecule would pollute the molecule table and inflate
        API supplier counts with packaging firms.
        """
        _ingest(session, _workbook())
        names = {m.normalized_name for m in session.scalars(select(Molecule)).all()}
        assert "testazole" in names
        assert "otherazole" in names
        assert not any("bottle" in n or "hdpe" in n for n in names)

        packaging = session.scalar(
            select(DrugMasterFile).where(DrugMasterFile.dmf_number == "31005")
        )
        assert packaging.molecule_id is None
        assert packaging.dmf_type == "3"

    def test_drl_name_variants_resolve_to_one_company(self, session) -> None:
        """"LTD" and "LIMITED" editions of the holder name are the same firm.

        If they did not collapse, DRL's capability graph would be split in two and
        the focus company would appear to hold half its actual filings.
        """
        _ingest(session, _workbook())
        drl_dmfs = session.scalars(
            select(DrugMasterFile).where(
                DrugMasterFile.dmf_number.in_(["31001", "31004"])
            )
        ).all()
        holder_ids = {d.holder_company_id for d in drl_dmfs}
        assert len(holder_ids) == 1, "DRL name variants must resolve to one company"

    def test_type_two_dmfs_resolve_to_a_shared_molecule(self, session) -> None:
        _ingest(session, _workbook())
        testazole_dmfs = session.scalars(
            select(DrugMasterFile).where(DrugMasterFile.subject_text == "TESTAZOLE")
        ).all()
        molecule_ids = {d.molecule_id for d in testazole_dmfs}
        assert len(molecule_ids) == 1
        assert None not in molecule_ids

    def test_new_dmf_events_are_dated_by_list_publication(self, session) -> None:
        """A DMF becomes knowable when the quarterly list carrying it is published.

        FDA does not publish submissions in real time, so dating the event at the
        submission date would claim we knew in 2015 something first published in
        2026 — a leak that would make any backtest look prescient.
        """
        _ingest(session, _workbook())
        events = session.scalars(
            select(Event).where(Event.event_type == EventType.NEW_DMF)
        ).all()
        assert events
        for e in events:
            assert e.event_date == PUBLISHED.date()

    def test_role_assertions_created_for_type_two_holders(self, session) -> None:
        _ingest(session, _workbook())
        roles = session.scalars(select(CompanyRoleAssertion)).all()
        assert len(roles) == 4  # four Type II rows, not the Type III
        assert all(r.role == "dmf_holder" for r in roles)


class TestCapabilityDerivation:
    def test_active_dmf_yields_a_fact_capability(self, session) -> None:
        """An active Type II DMF is a first-party regulatory record: a fact."""
        _ingest(session, _workbook())
        drl = session.scalar(
            select(Company).where(Company.normalized_name == "dr reddys")
        )
        caps = session.scalars(
            select(CompanyCapability).where(CompanyCapability.company_id == drl.id)
        ).all()
        assert len(caps) == 2
        for cap in caps:
            assert cap.capability_type == CapabilityType.API_SUPPLY
            assert cap.assertion_level == AssertionLevel.FACT
            assert cap.inference_basis is None
            assert cap.molecule_id is not None
            assert "active Type II DMF" in cap.value_text

    def test_inactive_dmf_yields_an_inference_with_a_stated_basis(self, session) -> None:
        """An inactive filing is weaker evidence and must not be presented as fact.

        The schema enforces that an INFERENCE records why.
        """
        _ingest(session, _workbook())
        beta = session.scalar(
            select(Company).where(Company.canonical_name == "BETA CHEM LTD")
        )
        cap = session.scalar(
            select(CompanyCapability).where(CompanyCapability.company_id == beta.id)
        )
        assert cap.assertion_level == AssertionLevel.INFERENCE
        assert cap.inference_basis is not None
        assert "not active" in cap.inference_basis
        assert cap.confidence < 1.0

    def test_capability_never_claims_current_manufacture(self, session) -> None:
        """The record supports a filing, not present commercial supply."""
        _ingest(session, _workbook())
        for cap in session.scalars(select(CompanyCapability)).all():
            text = (cap.value_text or "").lower()
            for banned in ("manufactures", "currently supplies", "sells", "is a supplier of"):
                assert banned not in text, banned

    def test_capability_is_upgraded_when_a_filing_becomes_active(self, session) -> None:
        """A status change from I to A must promote INFERENCE to FACT."""
        inactive = [[31003, "II", "BETA CHEM LTD", "TESTAZOLE", "I", "2010-01-01"]]
        _ingest(session, _workbook(rows=inactive))
        beta = session.scalar(
            select(Company).where(Company.canonical_name == "BETA CHEM LTD")
        )
        cap = session.scalar(
            select(CompanyCapability).where(CompanyCapability.company_id == beta.id)
        )
        assert cap.assertion_level == AssertionLevel.INFERENCE

        # A later edition shows it active.
        active = [[31003, "II", "BETA CHEM LTD", "TESTAZOLE", "A", "2010-01-01"]]
        _ingest(session, _workbook(rows=active, title_rows=3))
        session.expire_all()
        cap = session.scalar(
            select(CompanyCapability).where(CompanyCapability.company_id == beta.id)
        )
        assert cap.assertion_level == AssertionLevel.FACT
        assert cap.inference_basis is None

    def test_status_change_emits_an_event(self, session) -> None:
        _ingest(session, _workbook(rows=[
            [31003, "II", "BETA CHEM LTD", "TESTAZOLE", "I", "2010-01-01"]
        ]))
        _ingest(session, _workbook(rows=[
            [31003, "II", "BETA CHEM LTD", "TESTAZOLE", "A", "2010-01-01"]
        ], title_rows=3))
        events = session.scalars(
            select(Event).where(Event.event_type == EventType.DMF_STATUS_CHANGED)
        ).all()
        assert len(events) == 1
        assert events[0].payload["from"] == "I"
        assert events[0].payload["to"] == "A"

    def test_unknown_reference_ready_stays_null_not_false(self, session) -> None:
        """Not knowing must not be recorded as a negative.

        The completeness-assessment list was not supplied, so
        available_for_reference is unknown. Storing False would understate every
        supplier's strength.
        """
        _ingest(session, _workbook())
        for dmf in session.scalars(select(DrugMasterFile)).all():
            assert dmf.available_for_reference is None


class TestDefensiveParsing:
    def test_header_row_is_found_below_title_rows(self, session) -> None:
        """Assuming row 0 is the header would read the title as column names."""
        _, _, result = _ingest(session, _workbook(title_rows=5))
        assert result.records_inserted == 5

    def test_missing_required_column_quarantines_with_observed_headers(self, session) -> None:
        _, _, result = _ingest(session, _workbook(headers=["Foo", "Bar", "Baz"]))
        q = session.scalar(
            select(QuarantinedRecord).where(
                QuarantinedRecord.reason_code == "schema_mismatch"
            )
        )
        assert q is not None
        assert "Foo" in q.reason_detail
        assert result.records_inserted == 0

    def test_incomplete_rows_quarantined_individually(self, session) -> None:
        rows = [
            [31001, "II", "GOOD CHEM LTD", "GOODAZOLE", "A", "2015-06-01"],
            [None, "II", "NO NUMBER LTD", "BADAZOLE", "A", "2015-06-01"],
        ]
        _, _, result = _ingest(session, _workbook(rows=rows))
        assert result.records_inserted == 1
        assert result.records_quarantined == 1

    def test_unresolvable_subject_still_records_the_dmf(self, session) -> None:
        """A subject that is only a salt token cannot be a molecule.

        The DMF row is still recorded, because losing it would understate the
        holder's filing count; the failure is surfaced as a quarantine entry.
        """
        rows = [[31009, "II", "SALT CHEM LTD", "HYDROCHLORIDE", "A", "2015-06-01"]]
        _ingest(session, _workbook(rows=rows))
        dmf = session.scalar(
            select(DrugMasterFile).where(DrugMasterFile.dmf_number == "31009")
        )
        assert dmf is not None
        assert dmf.molecule_id is None
        assert any(
            q.reason_code == "unresolvable_subject"
            for q in session.scalars(select(QuarantinedRecord)).all()
        )

    def test_unreadable_content_is_quarantined(self, session) -> None:
        _, _, result = _ingest(session, b"\x00\x01\x02 definitely not a workbook")
        assert result.records_quarantined >= 1

    def test_fetch_requires_an_explicit_url(self, session) -> None:
        """The quarterly URL changes, so guessing it would silently fetch a stale file."""
        connector = FdaDmfConnector(session)
        with pytest.raises(ValueError, match="requires an explicit artefact_url"):
            connector.fetch(client=None)


class TestSupplierCountingEndToEnd:
    def test_supplier_count_reflects_active_type_two_holders_only(self, session) -> None:
        """The feature that BUILD scoring depends on, computed from real loaded rows."""
        from moleculeradar.core.temporal import TemporalContext, VisibilityMode
        from moleculeradar.signals.features import FeatureComputer

        _ingest(session, _workbook())
        molecule = session.scalar(
            select(Molecule).where(Molecule.normalized_name == "testazole")
        )
        drl = session.scalar(
            select(Company).where(Company.normalized_name == "dr reddys")
        )

        ctx = TemporalContext(as_of_date=dt.date(2026, 8, 1),
                              mode=VisibilityMode.KNOWABLE)
        fs = FeatureComputer(session, ctx).compute(molecule.id, {drl.id})

        # Three Type II DMFs for TESTAZOLE, but one (BETA CHEM) is inactive.
        assert fs.values["observed_api_supplier_count"] == 2
        # Excluding the focus company leaves one competing supplier.
        assert fs.values["observed_competing_api_supplier_count"] == 1
        assert fs.values["focus_company_holds_api_dmf"] is True
