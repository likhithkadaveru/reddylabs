/**
 * Data Health — the administrator's screen.
 *
 * The question it answers is "can I trust what the rest of the product is telling
 * me?". A failing ERROR-severity check means the product may be presenting
 * something false, which is why that case is a prominent banner rather than a row
 * in a table.
 */

import { ApiUnavailable, api } from "@/lib/api";
import { formatDateTime, humanise } from "@/lib/format";
import {
  ApiDownState,
  Badge,
  EmptyState,
  Panel,
} from "@/components/Primitives";

export const dynamic = "force-dynamic";

const SEVERITY_TONE = {
  ERROR: "high",
  WARN: "watch",
  INFO: "muted",
} as const;

export default async function DataHealthPage() {
  let health;
  try {
    health = await api.dataHealth(true);
  } catch (error) {
    if (error instanceof ApiUnavailable) {
      if (error.status === 403) {
        return (
          <>
            <h1>Data Health</h1>
            <EmptyState
              availability="DATA_UNAVAILABLE"
              message="Data Health requires an administrator role. It exposes operational detail that the intelligence surface deliberately does not show."
            />
          </>
        );
      }
      return (
        <>
          <h1>Data Health</h1>
          <ApiDownState detail={error.message} />
        </>
      );
    }
    throw error;
  }

  const failing = health.checks.filter((c) => !c.passed);
  const errors = failing.filter((c) => c.severity === "ERROR");

  return (
    <>
      <h1>Data Health</h1>
      <p className="lede">
        Generated {formatDateTime(health.generated_at)}. ERROR-severity checks
        assert the things that would make the product present something false.
      </p>

      {errors.length ? (
        <div className="banner" data-tone="error">
          <div className="banner-title">
            {errors.length} blocking failure{errors.length === 1 ? "" : "s"}
          </div>
          The opportunity feed should not be published until these are resolved.
          A failing ERROR check means a displayed claim may be unsupported,
          unexplainable or wrong.
        </div>
      ) : (
        <div className="banner" style={{ borderColor: "var(--positive)", background: "var(--positive-dim)" }}>
          <div className="banner-title">No blocking failures</div>
          Every ERROR-severity check passes: evidence links resolve, scores match
          their decompositions, and no duplicate or ambiguous entities were found.
        </div>
      )}

      <div className="stats section">
        <div className="stat">
          <div className="stat-label">Checks run</div>
          <div className="stat-value mono">{health.checks.length}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Failing</div>
          <div
            className="stat-value mono"
            style={{ color: failing.length ? "var(--watch)" : undefined }}
          >
            {failing.length}
          </div>
        </div>
        <div className="stat">
          <div className="stat-label">Pending entity reviews</div>
          <div className="stat-value mono">{health.pending_reviews}</div>
          <div className="stat-note">
            Pairs the system refused to merge on its own. Each is a decision a
            human still owes.
          </div>
        </div>
        <div className="stat">
          <div className="stat-label">Quarantined records</div>
          <div className="stat-value mono">{health.quarantined_records}</div>
          <div className="stat-note">
            Records refused rather than silently dropped.
          </div>
        </div>
      </div>

      <Panel title="Quality checks">
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Check</th>
                <th>Severity</th>
                <th>Result</th>
                <th className="num">Observed</th>
                <th>What it asserts</th>
              </tr>
            </thead>
            <tbody>
              {health.checks.map((check) => (
                <tr key={check.check_key}>
                  <td className="mono" style={{ fontSize: 11.5 }}>
                    {check.check_key}
                  </td>
                  <td>
                    <Badge tone={SEVERITY_TONE[check.severity]}>
                      {check.severity}
                    </Badge>
                  </td>
                  <td>
                    <Badge tone={check.passed ? "positive" : "high"}>
                      {check.passed ? "PASS" : "FAIL"}
                    </Badge>
                  </td>
                  <td className="num">{check.observed_count}</td>
                  <td style={{ fontSize: 12, color: "var(--text-secondary)" }}>
                    {check.detail}
                    {check.sample_ids?.length ? (
                      <div
                        className="mono"
                        style={{
                          marginTop: 4,
                          fontSize: 10,
                          color: "var(--text-muted)",
                        }}
                      >
                        Samples: {check.sample_ids.slice(0, 3).join(", ")}
                      </div>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="Sources">
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Source</th>
                <th>Status</th>
                <th>Verification</th>
                <th>Access</th>
                <th>Licence</th>
                <th>Cadence</th>
                <th className="num">Docs</th>
                <th>Last retrieved</th>
              </tr>
            </thead>
            <tbody>
              {health.sources.map((source) => (
                <tr key={source.key}>
                  <td>
                    <div>{source.name}</div>
                    <div style={{ fontSize: 10, color: "var(--text-muted)" }}>
                      {source.publisher}
                    </div>
                  </td>
                  <td>
                    <Badge
                      tone={source.status === "ACTIVE" ? "positive" : "muted"}
                    >
                      {source.status}
                    </Badge>
                  </td>
                  <td>
                    <Badge
                      tone={
                        source.verification_level === "LIVE_VERIFIED"
                          ? "positive"
                          : "watch"
                      }
                      title={
                        source.verification_level === "LIVE_VERIFIED"
                          ? "A real retrieval has been performed against this source."
                          : "The contract is documented but has never been observed. No data has been fetched."
                      }
                    >
                      {source.verification_level}
                    </Badge>
                  </td>
                  <td style={{ fontSize: 11 }}>{source.access_mode}</td>
                  <td style={{ fontSize: 11 }}>{source.licence}</td>
                  <td style={{ fontSize: 11 }}>
                    {source.expected_update_cadence ?? "—"}
                  </td>
                  <td className="num">{source.document_count}</td>
                  <td className="mono" style={{ fontSize: 11 }}>
                    {source.last_retrieved_at
                      ? formatDateTime(source.last_retrieved_at)
                      : "never"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="scope-note">
          Only an actual retrieval promotes a source to LIVE_VERIFIED.
          DOC_VERIFIED means the contract is documented but has never been
          observed in this environment.
        </p>
      </Panel>

      <Panel title="Recent ingestion runs">
        {health.recent_runs.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Source</th>
                  <th>Job</th>
                  <th>Status</th>
                  <th>Started</th>
                  <th className="num">Duration</th>
                  <th className="num">Processed</th>
                  <th className="num">Inserted</th>
                  <th className="num">Updated</th>
                  <th className="num">Quarantined</th>
                  <th className="num">Events</th>
                </tr>
              </thead>
              <tbody>
                {health.recent_runs.map((run, i) => (
                  <tr key={`${run.source_key}-${run.started_at}-${i}`}>
                    <td className="mono" style={{ fontSize: 11 }}>
                      {run.source_key}
                    </td>
                    <td style={{ fontSize: 11 }}>{run.job_name}</td>
                    <td>
                      <Badge
                        tone={
                          run.status === "succeeded"
                            ? "positive"
                            : run.status === "partial"
                              ? "watch"
                              : "high"
                        }
                      >
                        {run.status}
                      </Badge>
                    </td>
                    <td className="mono" style={{ fontSize: 11 }}>
                      {formatDateTime(run.started_at)}
                    </td>
                    <td className="num">
                      {run.duration_seconds?.toFixed(1) ?? "—"}s
                    </td>
                    <td className="num">{run.records_processed}</td>
                    <td className="num">{run.records_inserted}</td>
                    <td className="num">{run.records_updated}</td>
                    <td
                      className="num"
                      style={{
                        color: run.records_quarantined
                          ? "var(--watch)"
                          : undefined,
                      }}
                    >
                      {run.records_quarantined}
                    </td>
                    <td className="num">{run.events_emitted}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            availability="DATA_UNAVAILABLE"
            message="No ingestion run has been recorded."
            commands={["moleculeradar sources verify"]}
          />
        )}
        <p className="scope-note">
          A run marked <strong>partial</strong> quarantined at least one record,
          so it did not fully succeed. A <strong>failed</strong> run left the
          database unchanged rather than half-written.
        </p>
      </Panel>
    </>
  );
}
