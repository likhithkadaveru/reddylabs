/**
 * Opportunity Feed — the home screen.
 *
 * Answers: what changed, and why should I care?
 *
 * With no data this page shows DATA UNAVAILABLE plus the actual source status,
 * never a plausible-looking empty dashboard.
 */

import Link from "next/link";
import { ApiUnavailable, api } from "@/lib/api";
import { formatDate, formatDateTime, eventLabel } from "@/lib/format";
import type { FeedResponse, OpportunitySummary } from "@/lib/types";
import {
  ApiDownState,
  Badge,
  ConfidenceBadge,
  EmptyState,
  Panel,
  PriorityBadge,
} from "@/components/Primitives";
import { SignalDecomposition } from "@/components/SignalDecomposition";

export const dynamic = "force-dynamic";

function OpportunityCard({ opp }: { opp: OpportunitySummary }) {
  return (
    <article className="card" data-band={opp.priority_band}>
      <div className="card-top">
        <div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Badge tone={opp.opportunity_type === "SELL" ? "accent" : "caution"}>
              {opp.opportunity_type}
            </Badge>
            <PriorityBadge band={opp.priority_band} />
            <ConfidenceBadge level={opp.evidence_confidence} />
          </div>
          <h3 className="card-title" style={{ marginTop: 8 }}>
            <Link href={`/molecules/${opp.molecule.id}`}>
              {opp.molecule.preferred_name}
            </Link>
            {opp.counterparty ? (
              <>
                {" · "}
                <Link href={`/companies/${opp.counterparty.id}`}>
                  {opp.counterparty.canonical_name}
                </Link>
              </>
            ) : null}
          </h3>
        </div>
        <div className="card-scores">
          <div className="score-block">
            <div className="score-label">Opportunity</div>
            <div className="score-figure">
              {opp.opportunity_score.toFixed(1)}
            </div>
          </div>
          <div className="score-block">
            <div className="score-label">Evidence conf.</div>
            <div className="score-figure" style={{ fontSize: 13 }}>
              {opp.evidence_confidence}
            </div>
          </div>
        </div>
      </div>

      <p className="card-headline">{opp.headline}</p>

      <SignalDecomposition
        signals={opp.top_signals}
        score={opp.opportunity_score}
        totalSignalCount={opp.signal_count}
        compact
      />

      <div style={{ marginTop: 12, display: "flex", gap: 8, alignItems: "center" }}>
        <Link className="btn" href={`/opportunities/${opp.id}`}>
          Investigate
        </Link>
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
          {opp.signal_count} signal{opp.signal_count === 1 ? "" : "s"} ·{" "}
          {opp.counter_thesis_count} counter-argument
          {opp.counter_thesis_count === 1 ? "" : "s"} · as of{" "}
          {formatDate(opp.as_of_date)}
        </span>
      </div>
    </article>
  );
}

export default async function FeedPage() {
  let feed: FeedResponse;
  try {
    feed = await api.feed({ limit: 25 });
  } catch (error) {
    if (error instanceof ApiUnavailable) {
      return (
        <>
          <h1>Opportunity Feed</h1>
          <ApiDownState detail={error.message} />
        </>
      );
    }
    throw error;
  }

  const changes = await api
    .recentChanges(45, 25)
    .catch(() => [] as Awaited<ReturnType<typeof api.recentChanges>>);

  if (feed.availability !== "AVAILABLE") {
    return (
      <>
        <h1>Opportunity Feed</h1>
        <p className="lede">
          Molecule Radar surfaces pharmaceutical supply and portfolio
          opportunities from public sources, with every claim traceable to its
          source document.
        </p>
        <EmptyState
          availability={feed.availability}
          message={feed.message}
          commands={
            feed.availability === "DATA_UNAVAILABLE"
              ? [
                  "moleculeradar sources verify",
                  "moleculeradar sources ingest all",
                  "moleculeradar focus bootstrap",
                  "moleculeradar engine run",
                ]
              : ["moleculeradar engine run"]
          }
        />
        {feed.source_status.length ? (
          <Panel title="Source status">
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Source</th>
                    <th>Status</th>
                    <th>Verification</th>
                    <th>Cadence</th>
                    <th className="num">Documents</th>
                    <th>Last retrieved</th>
                  </tr>
                </thead>
                <tbody>
                  {feed.source_status.map((s) => (
                    <tr key={s.key}>
                      <td>
                        <div>{s.name}</div>
                        <div style={{ fontSize: 10, color: "var(--text-muted)" }}>
                          {s.publisher}
                        </div>
                      </td>
                      <td>
                        <Badge
                          tone={s.status === "ACTIVE" ? "positive" : "muted"}
                        >
                          {s.status}
                        </Badge>
                      </td>
                      <td>
                        <Badge
                          tone={
                            s.verification_level === "LIVE_VERIFIED"
                              ? "positive"
                              : "watch"
                          }
                          title={
                            s.verification_level === "LIVE_VERIFIED"
                              ? "A real retrieval has been performed."
                              : "Contract documented but never observed. No data has been fetched from this source."
                          }
                        >
                          {s.verification_level}
                        </Badge>
                      </td>
                      <td style={{ fontSize: 11 }}>
                        {s.expected_update_cadence ?? "—"}
                      </td>
                      <td className="num">{s.document_count}</td>
                      <td className="mono" style={{ fontSize: 11 }}>
                        {s.last_retrieved_at
                          ? formatDateTime(s.last_retrieved_at)
                          : "never"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="scope-note">
              A source marked DOC_VERIFIED has a documented contract that has
              never been observed in this environment — no data has been fetched
              from it. Only an actual retrieval promotes a source to
              LIVE_VERIFIED.
            </p>
          </Panel>
        ) : null}
      </>
    );
  }

  return (
    <>
      <div style={{ display: "flex", alignItems: "baseline", gap: 14, flexWrap: "wrap" }}>
        <h1>Opportunity Feed</h1>
        <span className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
          as of {formatDate(feed.as_of_date)} · engine {feed.engine_version}
        </span>
      </div>
      <p className="lede">
        {feed.total} signal{feed.total === 1 ? "" : "s"} in this run.
        Opportunity Score describes how interesting the situation looks; Evidence
        Confidence describes how well we can evidence it. They are deliberately
        separate.
      </p>

      <div className="stats section">
        <div className="stat">
          <div className="stat-label">High priority</div>
          <div className="stat-value mono" style={{ color: "var(--high)" }}>
            {feed.bands.high}
          </div>
        </div>
        <div className="stat">
          <div className="stat-label">Watch</div>
          <div className="stat-value mono" style={{ color: "var(--watch)" }}>
            {feed.bands.watch}
          </div>
        </div>
        <div className="stat">
          <div className="stat-label">Low confidence</div>
          <div className="stat-value mono">{feed.bands.low_confidence}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Insufficient evidence</div>
          <div className="stat-value mono">
            {feed.bands.insufficient_evidence}
          </div>
          <div className="stat-note">
            Shown but not promoted: the evidence base is too thin to rank these.
          </div>
        </div>
      </div>

      <div className="two-col">
        <div>
          <section className="section">
            <h2>Sell — supply opportunities</h2>
            {feed.sell.length ? (
              <div className="cards">
                {feed.sell.map((opp) => (
                  <OpportunityCard key={opp.id} opp={opp} />
                ))}
              </div>
            ) : (
              <EmptyState
                availability="NO_QUALIFYING_SIGNALS"
                message="No molecule in this run has both public evidence that the focus company supplies the API and a company exhibiting signals consistent with potential demand."
              />
            )}
          </section>

          <section className="section">
            <h2>Build — portfolio investigation</h2>
            {feed.build.length ? (
              <div className="cards">
                {feed.build.map((opp) => (
                  <OpportunityCard key={opp.id} opp={opp} />
                ))}
              </div>
            ) : (
              <EmptyState
                availability="NO_QUALIFYING_SIGNALS"
                message="No molecule in this run met the threshold for portfolio investigation."
              />
            )}
          </section>
        </div>

        <aside>
          <Panel title="What changed">
            {changes.length ? (
              <div className="timeline">
                {changes.map((event) => (
                  <div
                    className="timeline-item"
                    key={event.id}
                    data-tone={
                      event.event_type.includes("SHORTAGE") ||
                      event.event_type.includes("TERMINATED")
                        ? "high"
                        : undefined
                    }
                  >
                    <div className="timeline-date">
                      {formatDate(event.event_date)} ·{" "}
                      {eventLabel(event.event_type)}
                    </div>
                    <div className="timeline-summary">{event.summary}</div>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState
                availability="NO_QUALIFYING_SIGNALS"
                message="No dated events recorded in the last 45 days."
              />
            )}
          </Panel>
        </aside>
      </div>
    </>
  );
}
