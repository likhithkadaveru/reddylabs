/**
 * Company intelligence page.
 *
 * The overlaps panel is the commercially interesting part and the easiest to
 * overstate, so its language is fixed: an overlap is an *observable* product/API
 * overlap, never a claim that a relationship exists or should.
 */

import Link from "next/link";
import { notFound } from "next/navigation";
import { ApiUnavailable, api } from "@/lib/api";
import { eventLabel, formatDate } from "@/lib/format";
import {
  ApiDownState,
  Badge,
  ConfidenceBadge,
  EmptyState,
  Panel,
  PriorityBadge,
  ScopedStat,
  Unavailable,
} from "@/components/Primitives";

export const dynamic = "force-dynamic";

const STRENGTH_TONE = {
  STRONG: "positive",
  MODERATE: "watch",
  INSUFFICIENT_EVIDENCE: "muted",
} as const;

export default async function CompanyPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  let detail;
  try {
    detail = await api.company(id);
  } catch (error) {
    if (error instanceof ApiUnavailable) {
      if (error.status === 404) notFound();
      return <ApiDownState detail={error.message} />;
    }
    throw error;
  }

  const counts = detail.overlap_counts;

  return (
    <>
      <h1>{detail.company.canonical_name}</h1>
      <div className="inline-list" style={{ marginBottom: 10 }}>
        {detail.company.is_focus_company ? (
          <Badge tone="positive">FOCUS COMPANY</Badge>
        ) : null}
        {detail.roles.map((role) => (
          <Badge key={role} tone="muted">
            {role.replace(/_/g, " ")}
          </Badge>
        ))}
        {detail.countries.map((c) => (
          <Badge key={c} tone="accent">
            {c}
          </Badge>
        ))}
      </div>

      <div className="stats section">
        <ScopedStat label="Registered trials" count={detail.trials} />
        <ScopedStat label="Regulatory events" count={detail.regulatory_events} />
        <ScopedStat
          label="Own active API filings"
          count={detail.observed_api_filings}
        />
      </div>

      {!detail.company.is_focus_company ? (
        <Panel
          title="Potential focus-company matches"
          aside={
            <span className="mono" style={{ fontSize: 10, color: "var(--text-muted)" }}>
              {counts.total ?? 0} OBSERVABLE OVERLAP
              {(counts.total ?? 0) === 1 ? "" : "S"}
            </span>
          }
        >
          {detail.overlaps.length ? (
            <>
              <div className="stats section" style={{ marginBottom: 14 }}>
                <div className="stat">
                  <div className="stat-label">Strong signals</div>
                  <div className="stat-value mono" style={{ color: "var(--positive)" }}>
                    {counts.strong ?? 0}
                  </div>
                  <div className="stat-note">
                    Focus company holds an active API filing; this company holds
                    none of its own for the molecule.
                  </div>
                </div>
                <div className="stat">
                  <div className="stat-label">Moderate</div>
                  <div className="stat-value mono" style={{ color: "var(--watch)" }}>
                    {counts.moderate ?? 0}
                  </div>
                  <div className="stat-note">
                    Both hold an active API filing, so external sourcing is less
                    likely.
                  </div>
                </div>
                <div className="stat">
                  <div className="stat-label">Insufficient evidence</div>
                  <div className="stat-value mono">
                    {counts.insufficient_evidence ?? 0}
                  </div>
                  <div className="stat-note">
                    This company&rsquo;s own API position could not be established.
                  </div>
                </div>
              </div>

              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Molecule</th>
                      <th>Strength</th>
                      <th>This company&rsquo;s own API filing</th>
                      <th>Observed activity</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.overlaps.map((overlap) => (
                      <tr key={overlap.molecule.id}>
                        <td>
                          <Link href={`/molecules/${overlap.molecule.id}`}>
                            {overlap.molecule.preferred_name}
                          </Link>
                        </td>
                        <td>
                          <Badge tone={STRENGTH_TONE[overlap.strength]}>
                            {overlap.strength.replace(/_/g, " ")}
                          </Badge>
                        </td>
                        <td>
                          {overlap.company_holds_own_api_dmf === null ? (
                            <Unavailable />
                          ) : overlap.company_holds_own_api_dmf ? (
                            "Yes — active Type II DMF identified"
                          ) : (
                            "None identified in current coverage"
                          )}
                        </td>
                        <td style={{ fontSize: 12, color: "var(--text-secondary)" }}>
                          {overlap.company_activity_summary}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="scope-note">
                These are observable product and API overlaps derived from public
                records. Nothing here asserts that this company requires any API,
                sources it externally, or has any relationship with the focus
                company. Absence of an API filing in our coverage is consistent
                with — but not proof of — external sourcing: captive manufacture
                and non-US sourcing need not appear.
              </p>
            </>
          ) : (
            <EmptyState
              availability="NO_QUALIFYING_SIGNALS"
              message="No observable product or API overlaps with the focus company were identified for this company."
            />
          )}
        </Panel>
      ) : null}

      <div className="two-col">
        <div>
          <Panel title="Opportunity signals naming this company">
            {detail.opportunities.length ? (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Molecule</th>
                      <th>Type</th>
                      <th className="num">Score</th>
                      <th>Evidence</th>
                      <th>Band</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {detail.opportunities.map((opp) => (
                      <tr key={opp.id}>
                        <td>
                          <Link href={`/molecules/${opp.molecule.id}`}>
                            {opp.molecule.preferred_name}
                          </Link>
                        </td>
                        <td>
                          <Badge tone="accent">{opp.opportunity_type}</Badge>
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
                message="No opportunity signals name this company."
              />
            )}
          </Panel>

          <Panel title="Recent events">
            {detail.timeline.length ? (
              <div className="timeline">
                {detail.timeline.slice(0, 50).map((event) => (
                  <div className="timeline-item" key={event.id}>
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
                message="No dated events recorded for this company."
              />
            )}
          </Panel>
        </div>

        <aside>
          <Panel title="Observed capabilities">
            {detail.capabilities.length ? (
              detail.capabilities.slice(0, 40).map((cap, i) => (
                <div className="concern" key={i}>
                  <div className="concern-head">
                    <Badge
                      tone={cap.assertion_level === "FACT" ? "positive" : "watch"}
                    >
                      {cap.assertion_level}
                    </Badge>
                    <strong style={{ fontSize: 12 }}>
                      {cap.capability_type.replace(/_/g, " ")}
                    </strong>
                  </div>
                  {cap.molecule ? (
                    <div className="concern-text">
                      <Link href={`/molecules/${cap.molecule.id}`}>
                        {cap.molecule.preferred_name}
                      </Link>
                    </div>
                  ) : null}
                  <div className="concern-text">{cap.value_text}</div>
                  {cap.inference_basis ? (
                    <div
                      className="concern-text"
                      style={{ marginTop: 3, color: "var(--text-muted)" }}
                    >
                      Basis: {cap.inference_basis}
                    </div>
                  ) : null}
                </div>
              ))
            ) : (
              <Unavailable>NO CAPABILITY EVIDENCE INGESTED</Unavailable>
            )}
          </Panel>

          <Panel title="Molecules">
            {detail.molecules.length ? (
              <ul style={{ margin: 0, paddingLeft: 16, fontSize: 12.5 }}>
                {detail.molecules.slice(0, 60).map((mol) => (
                  <li key={mol.id} style={{ marginBottom: 3 }}>
                    <Link href={`/molecules/${mol.id}`}>
                      {mol.preferred_name}
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <Unavailable>NO MOLECULES LINKED</Unavailable>
            )}
          </Panel>
        </aside>
      </div>
    </>
  );
}
