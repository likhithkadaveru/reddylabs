"""Molecule Radar command line.

The operator interface. Commands are grouped by what they act on:

    moleculeradar sources  list | verify | ingest
    moleculeradar focus    bootstrap | show
    moleculeradar engine   run
    moleculeradar quality  check
    moleculeradar db       status

`sources verify` is the command to run first in any new environment: it reports
which source hosts are actually reachable instead of letting the product imply it
has data it has never been able to fetch.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from .. import ENGINE_VERSION
from ..core.config import get_settings
from ..core.db import session_scope
from ..core.enums import OpportunityType, PriorityBand
from ..core.models import (
    Company,
    CompanyCapability,
    DrugMasterFile,
    Event,
    Molecule,
    Opportunity,
    OpportunitySignal,
    ReviewQueueItem,
    Source,
    SourceDocument,
)
from ..core.temporal import TemporalContext, VisibilityMode
from ..quality.checks import SEVERITY_ERROR, has_blocking_failures, run_all_checks
from ..resolution.focus import bootstrap_focus_company, focus_company_ids
from ..signals.engine import OpportunityEngine
from ..sources.http import (
    IngestionHttpClient,
    IngestionNotPermittedError,
    SourceUnavailableError,
)
from ..sources.registry import PROBE_URLS, all_specs, build_connector, get_spec

app = typer.Typer(
    name="moleculeradar",
    help="Molecule Radar — pharmaceutical opportunity intelligence (Llama Labs).",
    no_args_is_help=True,
    add_completion=False,
)
sources_app = typer.Typer(help="Data source inspection and ingestion.", no_args_is_help=True)
focus_app = typer.Typer(help="Focus company (capability graph) management.", no_args_is_help=True)
engine_app = typer.Typer(help="Opportunity engine.", no_args_is_help=True)
quality_app = typer.Typer(help="Data quality.", no_args_is_help=True)
db_app = typer.Typer(help="Database status.", no_args_is_help=True)
app.add_typer(sources_app, name="sources")
app.add_typer(focus_app, name="focus")
app.add_typer(engine_app, name="engine")
app.add_typer(quality_app, name="quality")
app.add_typer(db_app, name="db")

console = Console()


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------


@sources_app.command("list")
def sources_list() -> None:
    """Show every registered source with its licence and verification level."""
    table = Table(title="Molecule Radar data sources", show_lines=False)
    table.add_column("Key", style="bold")
    table.add_column("Status")
    table.add_column("Verification")
    table.add_column("Access")
    table.add_column("Cadence")
    table.add_column("Hosts")

    with session_scope() as session:
        db_rows = {s.key: s for s in session.scalars(select(Source)).all()}
        for spec in all_specs():
            row = db_rows.get(spec.key)
            # The database records what we have actually achieved; the spec records
            # what the register claims. Showing the database value where it exists
            # is what keeps the two honest.
            verification = row.verification_level if row else spec.verification_level
            table.add_row(
                spec.key,
                spec.status.value,
                str(verification),
                spec.access_mode.value,
                spec.expected_update_cadence,
                ", ".join(spec.required_hosts),
            )
    console.print(table)
    console.print(
        "\n[dim]Verification shows LIVE_VERIFIED only after an actual retrieval. "
        "DOC_VERIFIED means the contract is documented but has never been observed.[/dim]"
    )


@sources_app.command("verify")
def sources_verify(
    source: str | None = typer.Option(None, help="Verify one source only."),
) -> None:
    """Probe each source host and report reachability truthfully.

    Exits non-zero if any required host is unreachable, so this can gate a
    deployment or a scheduled ingestion run.
    """
    keys = [source] if source else list(PROBE_URLS)
    try:
        client = IngestionHttpClient(allow_without_contact=True)
    except IngestionNotPermittedError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    table = Table(title="Source reachability")
    table.add_column("Source", style="bold")
    table.add_column("Host")
    table.add_column("Verdict")
    table.add_column("Detail")

    failures: list[str] = []
    try:
        for key in keys:
            spec = get_spec(key)
            url = PROBE_URLS[key]
            outcome = client.check_reachability(url)
            colour = "green" if outcome.reachable else "red"
            table.add_row(
                spec.key, outcome.host,
                f"[{colour}]{outcome.verdict}[/{colour}]",
                outcome.detail[:110],
            )
            if not outcome.reachable:
                failures.append(f"{spec.key} ({outcome.host}): {outcome.verdict}")
    finally:
        client.close()

    console.print(table)

    if failures:
        console.print("\n[bold red]Unreachable sources[/bold red]")
        for failure in failures:
            console.print(f"  • {failure}")
        hosts = sorted({h for spec in all_specs() for h in spec.required_hosts})
        console.print(
            "\n[yellow]Molecule Radar cannot ingest anything it cannot reach, and it "
            "will show DATA UNAVAILABLE rather than inventing content.[/yellow]\n"
            "Ingestion requires outbound HTTPS to:\n"
            + "\n".join(f"  • {h}" for h in hosts)
            + "\n\nIf these are denied by a network egress policy, allow them (or "
            "widen the policy) and re-run this command."
        )
        raise typer.Exit(1)

    console.print("\n[green]All probed source hosts are reachable.[/green]")


@sources_app.command("contract-test")
def sources_contract_test(
    source: str | None = typer.Option(None, help="Test one source only."),
    url: str | None = typer.Option(
        None, help="Override the probe URL (required for fda_dmf_list)."
    ),
    report_path: str | None = typer.Option(
        None, "--report", help="Write a Markdown report to this path."
    ),
    promote: bool = typer.Option(
        False,
        help="Promote passing sources to LIVE_VERIFIED. Only a passing contract may.",
    ),
) -> None:
    """Verify that live sources match what our connectors assume.

    `sources verify` only answers "can we reach it". This answers the question
    that actually matters: does the live response match our field mappings, date
    formats, identifiers, pagination and nullability assumptions?

    Exits non-zero if any contract fails or could not be fully evaluated.
    """
    from ..sources.contracts import CONTRACTS, ContractReport, ContractTester, render_markdown

    keys = [source] if source else list(CONTRACTS)
    try:
        client = IngestionHttpClient(allow_without_contact=True)
    except IngestionNotPermittedError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    tester = ContractTester(client)
    reports: list[ContractReport] = []
    try:
        for key in keys:
            if key not in CONTRACTS:
                console.print(f"[red]Unknown source {key!r}.[/red]")
                raise typer.Exit(2)
            console.print(f"[bold]Contract test: {key}[/bold]")
            reports.append(tester.run(CONTRACTS[key], url_override=url))
    finally:
        client.close()

    table = Table(title="Source contract tests")
    table.add_column("Source", style="bold")
    table.add_column("Reachable")
    table.add_column("Verdict")
    table.add_column("Checks")

    for r in reports:
        if r.passed:
            verdict, colour = "PASS", "green"
        elif r.failures:
            verdict, colour = "FAIL", "red"
        else:
            verdict, colour = "INCOMPLETE", "yellow"
        table.add_row(
            r.source_key,
            "[green]yes[/green]" if r.reachable else "[red]no[/red]",
            f"[{colour}]{verdict}[/{colour}]",
            r.summary() or "—",
        )
    console.print(table)

    for r in reports:
        problems = r.failures + r.unknowns
        if not problems:
            continue
        console.print(f"\n[bold]{r.source_key}[/bold]")
        for c in problems:
            colour = "red" if c.status == "FAIL" else "yellow"
            console.print(f"  [{colour}]{c.status}[/{colour}] {c.check} — {c.detail}")
        if r.unknown_fields:
            console.print(
                f"  [dim]undeclared fields observed: "
                f"{', '.join(r.unknown_fields[:8])}[/dim]"
            )

    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(render_markdown(reports))
        console.print(f"\nReport written to [bold]{report_path}[/bold]")

    if promote:
        promoted = _promote_verified([r for r in reports if r.passed])
        console.print(f"Promoted {promoted} source(s) to LIVE_VERIFIED.")
    elif any(r.passed for r in reports):
        console.print(
            "\n[dim]Passing contracts were NOT promoted to LIVE_VERIFIED. "
            "Re-run with --promote to record that verification.[/dim]"
        )

    if any(not r.passed for r in reports):
        console.print(
            "\n[yellow]One or more contracts did not fully pass. A source whose "
            "contract is unverified must not be treated as validated: its field "
            "mappings have not been checked against live data.[/yellow]"
        )
        raise typer.Exit(1)


def _promote_verified(passing: list) -> int:
    """Record LIVE_VERIFIED for sources whose contract passed.

    Only a passing contract may promote a source. Reachability alone must not, or
    the register starts claiming validation we have not performed.
    """
    from ..core.enums import VerificationLevel
    from ..sources.registry import build_connector

    count = 0
    with session_scope() as session:
        for report in passing:
            connector = build_connector(
                report.source_key, session,
                **({"artefact_url": report.probe_url}
                   if report.source_key == "fda_dmf_list" else {}),
            )
            source = connector.ensure_source()
            source.verification_level = VerificationLevel.LIVE_VERIFIED
            source.last_live_verified_at = dt.datetime.now(dt.UTC)
            count += 1
    return count


@sources_app.command("ingest")
def sources_ingest(
    source: str = typer.Argument(..., help="Source key, or 'all'."),
    url: str | None = typer.Option(
        None, help="Artefact URL, required for fda_dmf_list (the quarterly URL changes)."
    ),
    max_records: int | None = typer.Option(
        None, help="Cap records for API sources. Useful for a first smoke run."
    ),
    dry_run: bool = typer.Option(
        False, help="Fetch and parse but roll back instead of committing."
    ),
) -> None:
    """Run ingestion for one source or all of them.

    Each source runs in its own transaction, so one failing source does not
    discard another's work, and a failure leaves data unchanged rather than
    half-written.
    """
    from ..sources.registry import INGESTION_ORDER

    keys = list(INGESTION_ORDER) if source == "all" else [get_spec(source).key]

    try:
        client = IngestionHttpClient()
    except IngestionNotPermittedError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    exit_code = 0
    try:
        for key in keys:
            kwargs: dict = {}
            if key == "fda_dmf_list":
                if not url:
                    console.print(
                        f"[yellow]Skipping {key}: --url is required because the "
                        f"quarterly DMF list URL changes each edition.[/yellow]"
                    )
                    continue
                kwargs["artefact_url"] = url
            elif max_records is not None:
                kwargs["max_records"] = max_records

            console.print(f"\n[bold]Ingesting {key}[/bold]")
            try:
                with session_scope() as session:
                    connector = build_connector(key, session, **kwargs)
                    result = connector.ingest(client)
                    _print_load_result(result)
                    if dry_run:
                        console.print("[yellow]--dry-run: rolling back.[/yellow]")
                        session.rollback()
            except SourceUnavailableError as exc:
                console.print(f"[red]{key}: source unavailable — {exc}[/red]")
                console.print(
                    "[dim]Nothing was written. This is reported rather than treated "
                    "as an empty result, because 'we could not look' and 'there is "
                    "nothing there' are different facts.[/dim]"
                )
                exit_code = 1
            except Exception as exc:
                console.print(f"[red]{key}: ingestion failed — "
                              f"{type(exc).__name__}: {exc}[/red]")
                console.print("[dim]The transaction was rolled back.[/dim]")
                exit_code = 1
    finally:
        client.close()

    raise typer.Exit(exit_code)


def _print_load_result(result) -> None:
    table = Table(show_header=False, box=None)
    table.add_row("processed", str(result.records_processed))
    table.add_row("inserted", str(result.records_inserted))
    table.add_row("updated", str(result.records_updated))
    table.add_row("unchanged", str(result.records_unchanged))
    table.add_row("quarantined", str(result.records_quarantined))
    table.add_row("events emitted", str(result.events_emitted))
    console.print(table)
    for note in result.notes[:10]:
        console.print(f"  [dim]• {note}[/dim]")


# ---------------------------------------------------------------------------
# focus
# ---------------------------------------------------------------------------


@focus_app.command("bootstrap")
def focus_bootstrap() -> None:
    """Establish the focus company and its configured alias seeds."""
    with session_scope() as session:
        result = bootstrap_focus_company(session)
        if result.focus_company_id is None:
            console.print("[red]Focus company could not be established.[/red]")
            for note in result.notes:
                console.print(f"  • {note}")
            raise typer.Exit(1)
        console.print(
            f"[green]Focus company established[/green] "
            f"({'created' if result.created_company else 'existing'}): "
            f"{get_settings().focus_company_name}"
        )
        console.print(f"  aliases recorded     : {result.aliases_recorded}")
        console.print(f"  alias conflicts      : {result.aliases_conflicted}")
        console.print(f"  group members linked : {result.subsidiaries_linked}")
        console.print(f"  candidates to review : {result.candidates_queued}")
        for note in result.notes:
            console.print(f"  [yellow]• {note}[/yellow]")


@focus_app.command("show")
def focus_show() -> None:
    """Show the focus company's publicly evidenced capability graph."""
    with session_scope() as session:
        ids = focus_company_ids(session)
        if not ids:
            console.print(
                "[yellow]No focus company in the database. Run "
                "`moleculeradar focus bootstrap`.[/yellow]"
            )
            raise typer.Exit(1)

        companies = session.scalars(select(Company).where(Company.id.in_(ids))).all()
        console.print(f"[bold]Focus group:[/bold] {len(companies)} company row(s)")
        for company in companies:
            marker = "focus" if company.is_focus_company else "member"
            console.print(f"  • {company.canonical_name} [dim]({marker})[/dim]")

        caps = session.execute(
            select(
                CompanyCapability.capability_type,
                CompanyCapability.assertion_level,
                func.count(),
            )
            .where(CompanyCapability.company_id.in_(ids))
            .group_by(CompanyCapability.capability_type, CompanyCapability.assertion_level)
        ).all()

        if not caps:
            console.print(
                "\n[yellow]DATA UNAVAILABLE — no capability evidence ingested yet.[/yellow]\n"
                "[dim]Capabilities are derived from FDA DMF holdings; run "
                "`moleculeradar sources ingest fda_dmf_list --url ...` first.[/dim]"
            )
            return

        table = Table(title="Publicly evidenced capabilities")
        table.add_column("Capability")
        table.add_column("Assertion level")
        table.add_column("Count", justify="right")
        for cap_type, level, count in caps:
            table.add_row(str(cap_type), str(level), str(count))
        console.print(table)

        dmf_count = session.scalar(
            select(func.count()).select_from(DrugMasterFile).where(
                DrugMasterFile.holder_company_id.in_(ids),
                DrugMasterFile.dmf_type == "2",
            )
        ) or 0
        console.print(f"\nType II drug master files held: {dmf_count}")
        console.print(
            "[dim]FACT capabilities rest on an active filing. INFERENCE capabilities "
            "state their basis and are never presented as fact.[/dim]"
        )


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------


@engine_app.command("run")
def engine_run(
    as_of: str | None = typer.Option(
        None, "--as-of", help="ISO date for a leakage-safe historical run (YYYY-MM-DD)."
    ),
    observed: bool = typer.Option(
        False, help="Use OBSERVED visibility (only data we had actually recorded)."
    ),
    molecule: str | None = typer.Option(None, help="Limit to one molecule by name."),
) -> None:
    """Compute features and opportunities."""
    as_of_date = None
    if as_of:
        try:
            as_of_date = dt.date.fromisoformat(as_of)
        except ValueError as exc:
            console.print(f"[red]--as-of must be an ISO date: {exc}[/red]")
            raise typer.Exit(2) from exc

    ctx = TemporalContext(
        as_of_date=as_of_date,
        mode=VisibilityMode.OBSERVED if observed else VisibilityMode.KNOWABLE,
    )
    console.print(f"[bold]Engine run[/bold] — {ctx.describe()}, engine {ENGINE_VERSION}")

    with session_scope() as session:
        molecule_ids = None
        if molecule:
            from ..resolution.normalize import normalize_molecule_name

            normalized = normalize_molecule_name(molecule)
            found = session.scalars(
                select(Molecule.id).where(Molecule.normalized_name == normalized)
            ).all()
            if not found:
                console.print(f"[yellow]No molecule matching {molecule!r}.[/yellow]")
                raise typer.Exit(1)
            molecule_ids = list(found)

        total_molecules = session.scalar(select(func.count()).select_from(Molecule)) or 0
        if total_molecules == 0:
            console.print(
                "[yellow]NO QUALIFYING SIGNALS — the database holds no molecules.[/yellow]\n"
                "[dim]Ingest at least one source first; see "
                "`moleculeradar sources verify`.[/dim]"
            )
            raise typer.Exit(0)

        summary = OpportunityEngine(session, ctx).run(molecule_ids)

    console.print(f"  molecules evaluated : {summary.molecules_evaluated}")
    console.print(f"  BUILD opportunities : {summary.build_opportunities}")
    console.print(f"  SELL opportunities  : {summary.sell_opportunities}")
    console.print(f"  counter-theses      : {summary.counter_theses}")
    console.print(f"  below threshold     : {summary.below_threshold}")
    for note in summary.notes:
        console.print(f"  [yellow]• {note}[/yellow]")

    if summary.build_opportunities == 0 and summary.sell_opportunities == 0:
        console.print("\n[yellow]NO QUALIFYING SIGNALS at this as-of date.[/yellow]")


@engine_app.command("feed")
def engine_feed(
    limit: int = typer.Option(20, help="Maximum rows."),
    opportunity_type: str | None = typer.Option(None, "--type", help="SELL or BUILD."),
) -> None:
    """Print the current opportunity feed with its decomposition."""
    with session_scope() as session:
        stmt = select(Opportunity).order_by(
            Opportunity.as_of_date.desc(), Opportunity.opportunity_score.desc()
        )
        if opportunity_type:
            stmt = stmt.where(
                Opportunity.opportunity_type == OpportunityType(opportunity_type.upper())
            )
        opportunities = session.scalars(stmt.limit(limit)).all()

        if not opportunities:
            console.print("[yellow]NO QUALIFYING SIGNALS.[/yellow]")
            return

        for opp in opportunities:
            molecule = session.get(Molecule, opp.molecule_id)
            band_colour = {
                PriorityBand.HIGH: "red",
                PriorityBand.WATCH: "yellow",
                PriorityBand.LOW_CONFIDENCE: "dim",
                PriorityBand.INSUFFICIENT_EVIDENCE: "dim",
            }.get(opp.priority_band, "white")
            console.print(
                f"\n[bold]{opp.opportunity_type}[/bold] "
                f"[{band_colour}]{opp.priority_band}[/{band_colour}] "
                f"score {opp.opportunity_score:.1f} / evidence "
                f"{opp.evidence_confidence}"
            )
            console.print(f"  {molecule.preferred_name if molecule else 'unknown'}")
            console.print(f"  [italic]{opp.headline}[/italic]")
            signals = session.scalars(
                select(OpportunitySignal).where(
                    OpportunitySignal.opportunity_id == opp.id
                ).order_by(OpportunitySignal.points.desc())
            ).all()
            for sig in signals:
                console.print(f"    +{sig.points:5.1f}  {sig.signal_key}")
            console.print(
                f"    [dim]= {sum(s.points for s in signals):.1f} "
                f"(decomposition sums to the score)[/dim]"
            )


# ---------------------------------------------------------------------------
# quality
# ---------------------------------------------------------------------------


@quality_app.command("check")
def quality_check(
    persist: bool = typer.Option(True, help="Store results for the Data Health screen."),
) -> None:
    """Run every data-quality check. Exits non-zero if an ERROR check fails."""
    with session_scope() as session:
        outcomes = run_all_checks(session, persist=persist)

    table = Table(title="Data quality")
    table.add_column("Check", style="bold")
    table.add_column("Severity")
    table.add_column("Result")
    table.add_column("Count", justify="right")

    for outcome in outcomes:
        colour = "green" if outcome.passed else (
            "red" if outcome.severity == SEVERITY_ERROR else "yellow"
        )
        table.add_row(
            outcome.check_key, outcome.severity,
            f"[{colour}]{'PASS' if outcome.passed else 'FAIL'}[/{colour}]",
            str(outcome.observed_count),
        )
    console.print(table)

    for outcome in outcomes:
        if not outcome.passed:
            console.print(f"\n[bold]{outcome.check_key}[/bold]: {outcome.detail}")
            for sample in outcome.sample_ids[:5]:
                console.print(f"  [dim]• {sample}[/dim]")

    if has_blocking_failures(outcomes):
        console.print(
            "\n[red]Blocking data-quality failures. The feed should not be published "
            "until these are resolved — the product may be presenting something "
            "false.[/red]"
        )
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------


@db_app.command("status")
def db_status() -> None:
    """Row counts and freshness, for a quick look at what the system actually holds."""
    with session_scope() as session:
        counts = [
            ("molecules", session.scalar(select(func.count()).select_from(Molecule))),
            ("companies", session.scalar(select(func.count()).select_from(Company))),
            ("drug master files",
             session.scalar(select(func.count()).select_from(DrugMasterFile))),
            ("events", session.scalar(select(func.count()).select_from(Event))),
            ("source documents",
             session.scalar(select(func.count()).select_from(SourceDocument))),
            ("opportunities", session.scalar(select(func.count()).select_from(Opportunity))),
            ("pending reviews", session.scalar(
                select(func.count()).select_from(ReviewQueueItem).where(
                    ReviewQueueItem.status == "pending"
                )
            )),
        ]
        latest = session.scalar(select(func.max(SourceDocument.retrieved_at)))

    table = Table(title="Database status", show_header=False)
    for label, value in counts:
        table.add_row(label, str(value or 0))
    console.print(table)
    console.print(
        f"\nLast source retrieval: "
        f"{latest.isoformat() if latest else '[yellow]never[/yellow]'}"
    )
    if not latest:
        console.print(
            "[dim]No source has been retrieved. Run `moleculeradar sources verify` "
            "to check reachability.[/dim]"
        )


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
