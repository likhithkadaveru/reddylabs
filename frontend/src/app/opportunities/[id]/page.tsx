/**
 * Opportunity investigation view.
 *
 * Thesis and counter-thesis are shown side by side, deliberately not netted off:
 * averaging them would hide exactly the disagreement an analyst needs to see.
 *
 * The page also lists the signals that could NOT be evaluated, so a reader can
 * distinguish "we looked and this was weak" from "we have no data on this".
 */

import Link from "next/link";
import { notFound } from "next/navigation";
import { ApiUnavailable, api, tryFetch } from "@/lib/api";
import { concernLabel, formatDate, humanise, signalLabel } from "@/lib/format";
import {
  ApiDownState,
  Badge,
  ConfidenceBadge,
  EmptyState,
  Panel,
  PriorityBadge,
} from "@/components/Primitives";
import { SignalDecomposition } from "@/components/SignalDecomposition";
import { EvidenceList } from "@/components/EvidenceList";

export const dynamic = "force-dynamic";

export default async function OpportunityPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  let detail;
  try {
    detail = await api.opportunity(id);
  } catch (error) {
    if (error instanceof ApiUnavailable) {
      if (error.status === 404) notFound();
      return <ApiDownState detail={error.message} />;
    }
    throw error;
  }

  // The investigation is created on demand; POST is idempotent for a given
  // opportunity, as-of date and engine version.
  const investigation =
    (await tryFetch(() => api.investigation(id))) ??
    (await tryFetch(() => api.createInvestigation(id)));

  const evidenceIds = Array.from(
    new Set(detail.signals.flatMap((s) => s.evidence_ids ?? [])),
  );

  return (
    <>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <Badge tone={detail.opportunity_type === "SELL" ? "accent" : "caution"}>
          {detail.opportunity_type}
        </Badge>
        <PriorityBadge band={detail.priority_band} />
        <ConfidenceBadge level={detail.evidence_confidence} />
        <span className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>
          as of {formatDate(detail.as_of_date)} · engine {detail.engine_version}
        </span>
      </div>

      <h1 style={{ marginTop: 10 }}>
        <Link href={`/molecules/${detail.molecule.id}`}>
          {detail.molecule.preferred_name}
        </Link>
        {detail.counterparty ? (
          <>
            {" · "}
            <Link href={`/companies/${detail.counterparty.id}`}>
              {detail.counterparty.canonical_name}
            </Link>
          </>
        ) : null}
      </h1>
      <p className="lede">{detail.headline}</p>

      <div className="stats section">
        <div className="stat">
          <div className="stat-label">Opportunity Score</div>
          <div className="stat-value mono">
            {detail.opportunity_score.toFixed(1)}
          </div>
          <div className="stat-note">
            Out of 100. The sum of the signal points below.
          </div>
        </div>
        <div className="stat">
          <div className="stat-label">Evidence Confidence</div>
          <div className="stat-value mono" style={{ fontSize: 16 }}>
            {detail.evidence_confidence}
          </div>
          <div className="stat-note">
            {detail.evidence_confidence_score.toFixed(2)} of 1.00. A separate
            measure — never combined with the score.
          </div>
        </div>
        <div className="stat">
          <div className="stat-label">Signals fired</div>
          <div className="stat-value mono">{detail.signals.length}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Counter-arguments</div>
          <div className="stat-value mono" style={{ color: "var(--high)" }}>
            {detail.counter_theses.length}
          </div>
        </div>
      </div>

      <Panel title="Thesis and counter-thesis">
        <div className="thesis-grid">
          <div>
            <div className="thesis-col-head" data-side="thesis">
              Thesis — why this fired
            </div>
            <SignalDecomposition
              signals={detail.signals}
              score={detail.opportunity_score}
            />
          </div>
          <div>
            <div className="thesis-col-head" data-side="counter">
              Counter-thesis — why it might not be an opportunity
            </div>
            {detail.counter_theses.length ? (
              detail.counter_theses.map((concern) => (
                <div className="concern" key={concern.concern_key}>
                  <div className="concern-head">
                    <Badge
                      tone={
                        concern.severity === "HIGH"
                          ? "high"
                          : concern.severity === "MEDIUM"
                            ? "watch"
                            : "muted"
                      }
                    >
                      {concern.severity}
                    </Badge>
                    <strong style={{ fontSize: 12.5 }}>
                      {concernLabel(concern.concern_key)}
                    </strong>
                  </div>
                  <div className="concern-text">{concern.explanation}</div>
                </div>
              ))
            ) : (
              <p className="concern-text">
                The skeptic engine found no counter-arguments in current
                evidence. With thin coverage that is more likely to reflect what
                we cannot see than a genuinely unopposed thesis.
              </p>
            )}
            <p className="scope-note">
              Counter-arguments do not reduce the Opportunity Score. They are
              shown beside it so the disagreement stays visible rather than being
              averaged away.
            </p>
          </div>
        </div>
      </Panel>

      {detail.unevaluated_signals.length ? (
        <Panel title="Signals that could not be evaluated">
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Signal</th>
                  <th className="num">Max points</th>
                  <th>Reason</th>
                  <th>Why it would matter</th>
                </tr>
              </thead>
              <tbody>
                {detail.unevaluated_signals.map((u) => (
                  <tr key={u.signal_key}>
                    <td>{signalLabel(u.signal_key)}</td>
                    <td className="num">{u.max_points.toFixed(0)}</td>
                    <td>
                      {u.reason === "missing_data" ? (
                        <Badge tone="muted" title={u.missing_features.join(", ")}>
                          NO DATA
                        </Badge>
                      ) : (
                        <Badge tone="muted">DID NOT FIRE</Badge>
                      )}
                    </td>
                    <td style={{ color: "var(--text-secondary)", fontSize: 12 }}>
                      {u.rationale}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="scope-note">
            A signal marked NO DATA was not scored as zero. The distinction
            matters: scoring absent data as zero would make a thinly-covered
            molecule look like a cleanly contested one.
          </p>
        </Panel>
      ) : null}

      {investigation ? (
        <Panel
          title="Investigation"
          aside={
            <span className="mono" style={{ fontSize: 10, color: "var(--text-muted)" }}>
              {investigation.synthesis_method === "deterministic_from_evidence"
                ? "ASSEMBLED FROM STORED EVIDENCE"
                : investigation.synthesis_method.toUpperCase()}
            </span>
          }
        >
          {investigation.sections.map((section) => (
            <div className="investigation-section" key={section.key}>
              <div
                style={{
                  display: "flex",
                  gap: 8,
                  alignItems: "center",
                  marginBottom: 6,
                }}
              >
                <h3 style={{ margin: 0 }}>{section.title}</h3>
                {section.availability !== "AVAILABLE" ? (
                  <Badge tone="muted">
                    {section.availability.replace(/_/g, " ")}
                  </Badge>
                ) : null}
                {section.evidence_ids.length ? (
                  <span
                    className="mono"
                    style={{ fontSize: 10, color: "var(--text-muted)" }}
                  >
                    {section.evidence_ids.length} evidence record
                    {section.evidence_ids.length === 1 ? "" : "s"}
                  </span>
                ) : null}
              </div>
              <div className="investigation-body">{section.body}</div>
            </div>
          ))}
        </Panel>
      ) : (
        <Panel title="Investigation">
          <EmptyState
            availability="DATA_UNAVAILABLE"
            message="The investigation could not be assembled."
          />
        </Panel>
      )}

      <Panel title={`Evidence (${evidenceIds.length})`}>
        <EvidenceList evidenceIds={evidenceIds} />
      </Panel>

      <Panel title="Computed features">
        <p className="scope-note" style={{ marginTop: 0, marginBottom: 10 }}>
          Every feature below is a count, a date difference or a bounded ratio
          computed from canonical records under the temporal gate. None is
          produced by a model. A value of{" "}
          <span className="unavailable">DATA UNAVAILABLE</span> means the source
          was not ingested — not that the value is zero.
        </p>
        <dl className="kv">
          {Object.entries(detail.features)
            .filter(([key]) => !key.startsWith("_"))
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([key, value]) => (
              <div key={key} style={{ display: "contents" }}>
                <dt>{humanise(key)}</dt>
                <dd className="mono">
                  {value === null || value === undefined ? (
                    <span className="unavailable">DATA UNAVAILABLE</span>
                  ) : Array.isArray(value) ? (
                    value.length ? value.join(", ") : "—"
                  ) : (
                    String(value)
                  )}
                </dd>
              </div>
            ))}
        </dl>
      </Panel>
    </>
  );
}
