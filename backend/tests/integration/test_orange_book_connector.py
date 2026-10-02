"""Orange Book connector tests.

These exercise the parser against synthetic archives built to FDA's documented
tilde-delimited contract. They cannot prove the real file matches that contract —
nothing in this environment can, because egress to fda.gov is denied — but they do
prove that *given* the documented shape, the connector produces correct canonical
rows, resolves entities, emits dated events, and quarantines rather than crashes.

The schema-mismatch tests matter most: because the contract is unverified, the
connector's behaviour when the real file differs is the behaviour we are most
likely to actually depend on.
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile

import pytest
from sqlalchemy import select

from moleculeradar.core.enums import ApplicationType, EventType, RunStatus
from moleculeradar.core.models import (
    Company,
    DrugProduct,
    Event,
    Evidence,
    Exclusivity,
    IngestionRun,
    Molecule,
    Patent,
    QuarantinedRecord,
    RegulatoryApplication,
    Source,
    SourceDocument,
)
from moleculeradar.sources.base import FetchedArtefact
from moleculeradar.sources.orange_book import OrangeBookConnector, parse_ob_date

pytestmark = pytest.mark.integration

UTC = dt.UTC
PUBLISHED = dt.datetime(2026, 9, 12, tzinfo=UTC)

PRODUCTS = """Ingredient~DF;Route~Trade_Name~Applicant~Strength~Appl_Type~Appl_No~Product_No~TE_Code~Approval_Date~RLD~RS~Type~Applicant_Full_Name
TESTAZOLE~TABLET;ORAL~TESTABRAND~DRLAB~10MG~N~021111~001~~Jan 15, 2015~Yes~Yes~RX~DR. REDDY'S LABORATORIES LTD
TESTAZOLE~TABLET;ORAL~TESTAGEN~ALPHAG~10MG~A~077222~001~AB~Mar 3, 2021~No~No~RX~ALPHA GENERICS INC
OLDAZOLE~TABLET;ORAL~OLDBRAND~BETAPH~5MG~N~012345~001~~Approved Prior to Jan 1, 1982~Yes~Yes~RX~BETA PHARMA LLC
COMBOAZOLE; COMBOSTATIN~TABLET;ORAL~COMBOBRAND~GAMMAP~10MG;20MG~N~020999~001~~Jun 1, 2018~Yes~Yes~RX~GAMMA PHARMA LTD
"""

PATENTS = """Appl_Type~Appl_No~Product_No~Patent_No~Patent_Expire_Date_Text~Drug_Substance_Flag~Drug_Product_Flag~Patent_Use_Code~Delist_Requested
N~021111~001~9111111~Dec 25, 2028~Y~N~U-1~N
N~021111~001~9222222~Jun 30, 2031~N~Y~~Y
N~099999~001~9333333~Jan 1, 2030~Y~N~~N
"""

EXCLUSIVITY = """Appl_Type~Appl_No~Product_No~Exclusivity_Code~Exclusivity_Date
N~021111~001~NCE~Jan 15, 2030
N~020999~001~ODE~Jun 1, 2029
"""


def _zip(products: str = PRODUCTS, patents: str | None = PATENTS,
         exclusivity: str | None = EXCLUSIVITY) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("products.txt", products)
        if patents is not None:
            z.writestr("patent.txt", patents)
        if exclusivity is not None:
            z.writestr("exclusivity.txt", exclusivity)
    return buf.getvalue()


def _artefact(content: bytes) -> FetchedArtefact:
    return FetchedArtefact(
        locator="https://www.fda.gov/media/76860/download?attachment",
        content=content, media_type="application/zip", http_status=200,
        published_at=PUBLISHED,
    )


def _ingest(session, content: bytes):
    connector = OrangeBookConnector(session)
    connector.start_run("test:orange_book")
    artefact = _artefact(content)
    doc, is_new = connector.record_document(artefact)
    assert is_new
    result = connector.load(doc, artefact)
    connector.finish_run(result)
    session.flush()
    return connector, doc, result


class TestDateParsing:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Jan 15, 2015", dt.date(2015, 1, 15)),
            ("Mar 3, 2021", dt.date(2021, 3, 3)),
            ("December 25, 2028", dt.date(2028, 12, 25)),
            ("2028-12-25", dt.date(2028, 12, 25)),
            ("", None),
            ("N/A", None),
            (None, None),
        ],
    )
    def test_parses_documented_formats(self, text, expected) -> None:
        assert parse_ob_date(text) == expected

    def test_approved_prior_marker_is_not_coerced_to_a_date(self) -> None:
        """"Approved Prior to Jan 1, 1982" is a statement, not a date.

        Mapping it to 1982-01-01 would assert precision the source does not have
        and would place a fake dated event in the timeline.
        """
        assert parse_ob_date("Approved Prior to Jan 1, 1982") is None


class TestProductLoading:
    def test_creates_products_applications_and_companies(self, session) -> None:
        _ingest(session, _zip())

        products = session.scalars(select(DrugProduct)).all()
        assert len(products) == 4

        apps = {a.application_number: a for a in session.scalars(
            select(RegulatoryApplication)
        ).all()}
        assert apps["021111"].application_type == ApplicationType.NDA
        assert apps["077222"].application_type == ApplicationType.ANDA

        names = {c.canonical_name for c in session.scalars(select(Company)).all()}
        assert any("REDDY" in n.upper() for n in names)
        assert any("ALPHA GENERICS" in n.upper() for n in names)

    def test_uses_full_applicant_name_when_present(self, session) -> None:
        """Applicant_Full_Name is the better identity string than the short code.

        'DRLAB' carries no identity; the full name does, and resolution depends
        on it.
        """
        _ingest(session, _zip())
        app = session.scalar(
            select(RegulatoryApplication).where(
                RegulatoryApplication.application_number == "021111"
            )
        )
        assert app.applicant_literal is not None
        assert "LABORATORIES" in app.applicant_literal.upper()

    def test_dosage_form_and_route_are_split(self, session) -> None:
        _ingest(session, _zip())
        product = session.scalar(
            select(DrugProduct).where(DrugProduct.brand_name == "TESTABRAND")
        )
        assert product.dosage_form == "TABLET"
        assert product.route == "ORAL"

    def test_approval_events_are_emitted_with_real_dates(self, session) -> None:
        _ingest(session, _zip())
        events = session.scalars(
            select(Event).where(Event.event_type == EventType.NEW_APPROVAL)
        ).all()
        dates = {e.event_date for e in events}
        assert dt.date(2015, 1, 15) in dates
        assert dt.date(2021, 3, 3) in dates
        # The "Approved Prior to" row has no parseable date, so it must not
        # produce a dated approval event.
        assert all(e.event_date is not None for e in events)
        assert len(events) == 3

    def test_approved_prior_product_is_still_loaded(self, session) -> None:
        """No usable approval date must not mean the product is discarded."""
        _ingest(session, _zip())
        product = session.scalar(
            select(DrugProduct).where(DrugProduct.brand_name == "OLDBRAND")
        )
        assert product is not None
        assert product.approval_date is None

    def test_combination_product_resolves_to_a_combination_molecule(self, session) -> None:
        """A combination is its own commercial unit, not silently split.

        Attaching the product to one component would misattribute its approval,
        patents and exclusivity to that single moiety.
        """
        _ingest(session, _zip())
        product = session.scalar(
            select(DrugProduct).where(DrugProduct.brand_name == "COMBOBRAND")
        )
        molecule = session.get(Molecule, product.molecule_id)
        assert "combo" in molecule.normalized_name
        assert "comboazole" in molecule.normalized_name

    def test_every_product_has_evidence_pointing_at_the_document(self, session) -> None:
        _, doc, _ = _ingest(session, _zip())
        for product in session.scalars(select(DrugProduct)).all():
            evidence = session.get(Evidence, product.evidence_id)
            assert evidence is not None
            assert evidence.source_document_id == doc.id
            assert evidence.structured_record
            assert evidence.claim_text


class TestPatentAndExclusivityLoading:
    def test_patents_link_to_products_and_molecules(self, session) -> None:
        _ingest(session, _zip())
        patents = {p.patent_number: p for p in session.scalars(select(Patent)).all()}
        assert "9111111" in patents
        assert patents["9111111"].expiry_date == dt.date(2028, 12, 25)
        assert patents["9111111"].drug_substance_flag is True
        assert patents["9111111"].molecule_id is not None

    def test_delist_requested_flag_is_captured(self, session) -> None:
        """A delisted patent is not a live barrier and the engine excludes it."""
        _ingest(session, _zip())
        patent = session.scalar(select(Patent).where(Patent.patent_number == "9222222"))
        assert patent.delist_requested is True

    def test_orphan_patent_is_quarantined_not_dropped(self, session) -> None:
        """A patent whose application is absent is a real source inconsistency.

        Dropping it silently would hide a data-quality problem; quarantining makes
        it visible on the Data Health screen.
        """
        _, _, result = _ingest(session, _zip())
        quarantined = session.scalars(select(QuarantinedRecord)).all()
        assert any(q.reason_code == "orphan_patent" for q in quarantined)
        assert any("099999" in q.reason_detail for q in quarantined)
        assert result.records_quarantined >= 1

    def test_exclusivity_loaded_with_expiry(self, session) -> None:
        _ingest(session, _zip())
        excl = {e.exclusivity_code: e for e in session.scalars(select(Exclusivity)).all()}
        assert excl["NCE"].expiry_date == dt.date(2030, 1, 15)
        assert excl["NCE"].molecule_id is not None

    def test_patent_and_exclusivity_events_are_dated_by_publication(self, session) -> None:
        """A listing becomes knowable when the file is published, not at expiry.

        Dating these events at the expiry date would place them years in the
        future and corrupt every time-windowed feature.
        """
        _ingest(session, _zip())
        events = session.scalars(
            select(Event).where(
                Event.event_type.in_([EventType.NEW_PATENT_LISTING, EventType.NEW_EXCLUSIVITY])
            )
        ).all()
        assert events
        for e in events:
            assert e.event_date == PUBLISHED.date()


class TestIdempotency:
    def test_reingesting_identical_content_is_skipped_by_checksum(self, session) -> None:
        content = _zip()
        connector = OrangeBookConnector(session)
        connector.start_run("test:first")
        artefact = _artefact(content)
        doc1, new1 = connector.record_document(artefact)
        assert new1 is True
        connector.load(doc1, artefact)
        session.flush()
        product_count = len(session.scalars(select(DrugProduct.id)).all())

        connector2 = OrangeBookConnector(session)
        connector2.start_run("test:second")
        doc2, new2 = connector2.record_document(_artefact(content))
        assert new2 is False, "identical checksum must be recognised"
        assert doc2.id == doc1.id
        assert len(session.scalars(select(DrugProduct.id)).all()) == product_count

    def test_reloading_same_content_does_not_duplicate_rows(self, session) -> None:
        """Even if the load is forced, dedupe keys must prevent duplication."""
        content = _zip()
        _ingest(session, content)
        counts_before = (
            len(session.scalars(select(DrugProduct.id)).all()),
            len(session.scalars(select(Patent.id)).all()),
            len(session.scalars(select(Exclusivity.id)).all()),
            len(session.scalars(select(Molecule.id)).all()),
            len(session.scalars(select(Company.id)).all()),
        )

        connector = OrangeBookConnector(session)
        connector.start_run("test:forced-reload")
        artefact = _artefact(content)
        doc = session.scalar(select(SourceDocument))
        connector.load(doc, artefact)
        session.flush()

        counts_after = (
            len(session.scalars(select(DrugProduct.id)).all()),
            len(session.scalars(select(Patent.id)).all()),
            len(session.scalars(select(Exclusivity.id)).all()),
            len(session.scalars(select(Molecule.id)).all()),
            len(session.scalars(select(Company.id)).all()),
        )
        assert counts_before == counts_after


class TestDefensiveParsing:
    def test_columns_are_read_by_name_not_position(self, session) -> None:
        """A reordered file must still parse correctly.

        Positional parsing would shift every value one column across and produce
        confidently wrong data — the worst possible failure for this product.
        """
        reordered = (
            "Appl_No~Product_No~Ingredient~Applicant_Full_Name~Appl_Type~Trade_Name~"
            "Approval_Date~DF;Route~Strength~TE_Code~RLD~RS~Type~Applicant\n"
            "021111~001~TESTAZOLE~DR. REDDY'S LABORATORIES LTD~N~TESTABRAND~"
            "Jan 15, 2015~TABLET;ORAL~10MG~~Yes~Yes~RX~DRLAB\n"
        )
        _ingest(session, _zip(products=reordered, patents=None, exclusivity=None))
        product = session.scalar(select(DrugProduct))
        assert product.brand_name == "TESTABRAND"
        assert product.dosage_form == "TABLET"
        assert product.approval_date == dt.date(2015, 1, 15)
        molecule = session.get(Molecule, product.molecule_id)
        assert molecule.normalized_name == "testazole"

    def test_missing_required_column_is_quarantined_not_crashed(self, session) -> None:
        broken = "Foo~Bar\nx~y\n"
        _, _, result = _ingest(session, _zip(products=broken, patents=None, exclusivity=None))
        quarantined = session.scalars(select(QuarantinedRecord)).all()
        assert any(q.reason_code == "schema_mismatch" for q in quarantined)
        mismatch = next(q for q in quarantined if q.reason_code == "schema_mismatch")
        # The message must name the headers actually seen, or an operator cannot
        # diagnose the change.
        assert "Foo" in mismatch.reason_detail
        assert result.records_inserted == 0

    def test_incomplete_rows_are_quarantined_individually(self, session) -> None:
        partial = (
            "Ingredient~DF;Route~Trade_Name~Applicant~Strength~Appl_Type~Appl_No~"
            "Product_No~TE_Code~Approval_Date~RLD~RS~Type~Applicant_Full_Name\n"
            "GOODAZOLE~TABLET;ORAL~GOODBRAND~G~1MG~N~021000~001~~Jan 1, 2020~Yes~Yes~RX~GOOD PHARMA LTD\n"
            "~TABLET;ORAL~NOINGREDIENT~X~1MG~N~021001~001~~Jan 1, 2020~Yes~Yes~RX~BAD PHARMA LTD\n"
        )
        _, _, result = _ingest(session, _zip(products=partial, patents=None, exclusivity=None))
        # The good row loads...
        assert result.records_inserted == 1
        # ...and the bad one is recorded with a reason.
        assert result.records_quarantined == 1
        assert any(
            q.reason_code == "incomplete_row"
            for q in session.scalars(select(QuarantinedRecord)).all()
        )

    def test_bad_zip_is_quarantined(self, session) -> None:
        _, _, result = _ingest(session, b"this is not a zip file")
        assert result.records_quarantined == 1
        q = session.scalar(select(QuarantinedRecord))
        assert q.reason_code == "bad_archive"

    def test_missing_optional_members_are_noted_not_fatal(self, session) -> None:
        _, _, result = _ingest(session, _zip(patents=None, exclusivity=None))
        assert result.records_inserted > 0
        assert any("patent.txt absent" in n for n in result.notes)
        assert any("exclusivity.txt absent" in n for n in result.notes)

    def test_latin1_fallback_handles_stray_high_bytes(self, session) -> None:
        """A single undecodable byte must not cost us the entire monthly file."""
        raw = PRODUCTS.replace("ALPHA GENERICS INC", "ALPHA GEN\xe9RICS INC")
        content = raw.encode("latin-1")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("products.txt", content)
        _, _, result = _ingest(session, buf.getvalue())
        assert result.records_inserted == 4


class TestObservability:
    def test_run_records_counts_and_status(self, session) -> None:
        _ingest(session, _zip())
        run = session.scalar(select(IngestionRun))
        assert run.source_key == "fda_orange_book"
        assert run.records_processed > 0
        assert run.records_inserted > 0
        assert run.duration_seconds is not None
        # One orphan patent means the run is PARTIAL, not SUCCEEDED: a run that
        # quarantined records did not fully succeed and should not claim to.
        assert run.status == RunStatus.PARTIAL

    def test_source_row_is_upserted_from_the_spec(self, session) -> None:
        _ingest(session, _zip())
        source = session.scalar(select(Source).where(Source.key == "fda_orange_book"))
        assert source.licence == "US federal government work — public domain"
        assert source.expected_update_cadence.startswith("monthly")

    def test_live_retrieval_promotes_verification_level(self, session) -> None:
        """Only an actual retrieval may promote a source to LIVE_VERIFIED.

        Documentation alone never does, which is what keeps the register honest.
        """
        _ingest(session, _zip())
        source = session.scalar(select(Source).where(Source.key == "fda_orange_book"))
        assert source.verification_level == "LIVE_VERIFIED"
        assert source.last_live_verified_at is not None
