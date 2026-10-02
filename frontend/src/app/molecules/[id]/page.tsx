/**
 * Molecule intelligence page.
 *
 * Every count on this page renders through ScopedStat, so its scope statement is
 * always attached and an unmeasured value reads DATA UNAVAILABLE rather than 0.
 */

import Link from "next/link";
import { notFound } from "next/navigation";
import { ApiUnavailable, api } from "@/lib/api";
import { eventLabel, formatDate, formatMonths } from "@/lib/format";
import {
  ApiDownState,
  Badge,
  ConfidenceBadge,
  EmptyState,
  Panel,
  PriorityBadge,
  ScopedStat,
  Stat,
  Unavailable,
} from "@/components/Primitives";

export const dynamic = "force-dynamic";

export default async function MoleculePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  let detail;
  try {
    detail = await api.molecule(id);
  } catch (error) {
    if (error instanceof ApiUnavailable) {
      if (error.status === 404) notFound();
      return <ApiDownState detail={error.message} />;
    }
    throw error;
  }

  const m = detail.molecule;
  const missingCoverage = Object.entries(detail.coverage)
    .filter(([, present]) => !present)
    .map(([key]) => key);

  return (
    <>
      <h1>{m.preferred_name}</h1>
      <div className="inline-list" style={{ marginBottom: 10 }}>
        <Badge tone="muted">{m.molecule_class.replace(/_/g, " ")}</Badge>
        {m.is_peptide ? <Badge tone="caution">PEPTIDE</Badge> : null}
        {m.unii ? <Badge tone="muted">UNII {m.unii}</Badge> : null}
        {m.inn ? <Badge tone="muted">INN {m.inn}</Badge> : null}
        {detail.therapeutic_area ? (
          <Badge tone="accent">{detail.therapeutic_area}</Badge>
        ) : null}
        {detail.focus_company_supplies_api === true ? (
          <Badge tone="positive">FOCUS COMPANY HOLDS ACTIVE API DMF</Badge>
        ) : detail.focus_company_supplies_api === false ? (
          <Badge tone="muted">NO FOCUS COMPANY API DMF IDENTIFIED</Badge>
        ) : null}
      </div>

      {missingCoverage.length ? (
        <div className="banner">
          <div className="banner-title">Partial coverage</div>
          No data has been ingested for: {missingCoverage.join(", ")}. Features
          depending on these were not evaluated, so this page is partial on those
          dimensions rather than negative.
        </div>
      ) : null}

      <div className="stats section">
        <ScopedStat
          label="Observed API suppliers"
          count={detail.observed_api_suppliers}
        />
        <ScopedStat label="Distinct developers" count={detail.distinct_developers} />
        <ScopedStat label="Active trials" count={detail.active_trials} />
        <ScopedStat
          label="Shortage events (36m)"
          count={detail.shortage_events_36m}
        />
        <Stat
          label="Next exclusivity/patent event"
          value={formatMonths(detail.next_loe_event_months)}
          note={
            detail.next_loe_event_date
              ? `Earliest recorded expiry ${formatDate(detail.next_loe_event_date)}. Orange Book listing does not establish enforceability.`
              : "No future expiry identified in current coverage."
          }
        />
      </div>

      <div className="two-col">
        <div>
          <Panel title="Opportunity signals">
            {detail.opportunities.length ? (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Type</th>
                      <th>Counterparty</th>
                      <th className="num">Score</th>
                      <th>Evidence</th>
                      <th>Band</th>
                      <th>As of</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {detail.opportunities.map((opp) => (
                      <tr key={opp.id}>
                        <td>
                          <Badge
                            tone={
                              opp.opportunity_type === "SELL"
                                ? "accent"
                                : "caution"
                            }
                          >
                            {opp.opportunity_type}
                          </Badge>
                        </td>
                        <td>
                          {opp.counterparty ? (
                            <Link href={`/companies/${opp.counterparty.id}`}>
                              {opp.counterparty.canonical_name}
                            </Link>
                          ) : (
                            <span style={{ color: "var(--text-muted)" }}>—</span>
                          )}
                        </td>
                        <td className="num">
                          {opp.opportunity_score.toFixed(1)}
                        </td>
                        <td>
                          <ConfidenceBadge level={opp.evidence_confidence} />
                        </td>
                        <td>
                          <PriorityBadge band={opp.priority_band} />
                        </td>
                        <td className="mono" style={{ fontSize: 11 }}>
                          {formatDate(opp.as_of_date)}
                        </td>
                        <td>
                          <Link className="btn" href={`/opportunities/${opp.id}`}>
                            Investigate
                          </Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState
                availability="NO_QUALIFYING_SIGNALS"
                message="No opportunity signals have fired for this molecule."
              />
            )}
          </Panel>

          <Panel title="Event timeline">
            {detail.timeline.length ? (
              <div className="timeline">
                {detail.timeline.slice(0, 80).map((event) => (
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
                      {event.company ? (
                        <>
                          {" · "}
                          <Link href={`/companies/${event.company.id}`}>
                            {event.company.canonical_name}
                          </Link>
                        </>
                      ) : null}
                    </div>
                    <div className="timeline-summary">{event.summary}</div>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState
                availability="NO_QUALIFYING_SIGNALS"
                message="No dated events recorded for this molecule."
              />
            )}
          </Panel>
        </div>

        <aside>
          <Panel title="API supply landscape">
            {detail.api_suppliers.length ? (
              <ul style={{ margin: 0, paddingLeft: 16, fontSize: 12.5 }}>
                {detail.api_suppliers.map((company) => (
                  <li key={company.id} style={{ marginBottom: 4 }}>
                    <Link href={`/companies/${company.id}`}>
                      {company.canonical_name}
                    </Link>
                    {company.is_focus_company ? (
                      <>
                        {" "}
                        <Badge tone="positive">FOCUS</Badge>
                      </>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : (
              <Unavailable>
                {detail.coverage.dmf
                  ? "NO ACTIVE TYPE II DMF HOLDERS IDENTIFIED"
                  : "DATA UNAVAILABLE — DMF LIST NOT INGESTED"}
              </Unavailable>
            )}
            <p className="scope-note">
              {detail.observed_api_suppliers.scope_note}
            </p>
          </Panel>

          <Panel title="Focus company capability">
            {detail.focus_capabilities.length ? (
              detail.focus_capabilities.map((cap, i) => (
                <div key={i} className="concern">
                  <div className="concern-head">
                    <Badge
                      tone={
                        cap.assertion_level === "FACT" ? "positive" : "watch"
                      }
                      title={
                        cap.assertion_level === "FACT"
                          ? "Rests on a first-party regulatory record."
                          : "An inference. Its basis is stated below."
                      }
                    >
                      {cap.assertion_level}
                    </Badge>
                    <strong style={{ fontSize: 12.5 }}>
                      {cap.capability_type.replace(/_/g, " ")}
                    </strong>
                  </div>
                  <div className="concern-text">{cap.value_text}</div>
                  {cap.inference_basis ? (
                    <div
                      className="concern-text"
                      style={{ marginTop: 4, color: "var(--text-muted)" }}
                    >
                      Basis: {cap.inference_basis}
                    </div>
                  ) : null}
                </div>
              ))
            ) : (
              <Unavailable>
                NO DIRECT CAPABILITY EVIDENCE FOR THIS MOLECULE
              </Unavailable>
            )}
            <p className="scope-note">
              FACT entries rest on a first-party regulatory record. INFERENCE
              entries state their basis and are never presented as fact.
            </p>
          </Panel>

          <Panel title="Developers">
            {detail.developers.length ? (
              <ul style={{ margin: 0, paddingLeft: 16, fontSize: 12.5 }}>
                {detail.developers.slice(0, 30).map((company) => (
                  <li key={company.id} style={{ marginBottom: 3 }}>
                    <Link href={`/companies/${company.id}`}>
                      {company.canonical_name}
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <Unavailable>NO DEVELOPERS IDENTIFIED</Unavailable>
            )}
          </Panel>

          {detail.markets.length ? (
            <Panel title="Observed development geographies">
              <div className="inline-list">
                {detail.markets.map((market) => (
                  <Badge key={market} tone="muted">
                    {market}
                  </Badge>
                ))}
              </div>
            </Panel>
          ) : null}
        </aside>
      </div>
    </>
  );
}
