/**
 * White Space explorer.
 *
 * Rows are molecules; columns are the deterministic features and the resulting
 * scores. Filters are plain query parameters so a view is a shareable URL, which
 * is what makes "saved views" a later convenience rather than a prerequisite.
 */

import Link from "next/link";
import { ApiUnavailable, api } from "@/lib/api";
import { formatDate, formatMonths, formatPercent } from "@/lib/format";
import type { WhiteSpaceResponse } from "@/lib/types";
import {
  ApiDownState,
  Badge,
  ConfidenceBadge,
  EmptyState,
  Unavailable,
} from "@/components/Primitives";

export const dynamic = "force-dynamic";

const FOCUS_STATUS_LABEL: Record<string, string> = {
  HOLDS_ACTIVE_DMF: "Holds active DMF",
  NO_DMF_IDENTIFIED: "None identified",
  UNKNOWN: "Unknown",
};

function Num({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) return <Unavailable>N/A</Unavailable>;
  return <>{value.toLocaleString("en-GB")}</>;
}

export default async function WhiteSpacePage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const sp = await searchParams;
  const pick = (key: string): string | undefined => {
    const value = sp[key];
    return Array.isArray(value) ? value[0] : value;
  };

  const filters = {
    opportunity_type: pick("opportunity_type"),
    min_score: pick("min_score"),
    max_suppliers: pick("max_suppliers"),
    focus_status: pick("focus_status"),
    sort_by: pick("sort_by") ?? "opportunity_score",
    limit: 200,
  };

  let data: WhiteSpaceResponse;
  try {
    data = await api.whitespace(filters);
  } catch (error) {
    if (error instanceof ApiUnavailable) {
      return (
        <>
          <h1>White Space</h1>
          <ApiDownState detail={error.message} />
        </>
      );
    }
    throw error;
  }

  const exportQuery = new URLSearchParams(
    Object.entries(filters)
      .filter(([, v]) => v !== undefined && v !== "")
      .map(([k, v]) => [k, String(v)]),
  );

  return (
    <>
      <h1>White Space</h1>
      <p className="lede">
        Molecule-level view of competition, exclusivity timing, API supply and
        capability fit. Every column is a deterministic feature; none is produced
        by a model.
      </p>

      <form className="filters" method="get">
        <div className="field">
          <label htmlFor="opportunity_type">Opportunity type</label>
          <select
            id="opportunity_type"
            name="opportunity_type"
            defaultValue={filters.opportunity_type ?? ""}
          >
            <option value="">Any</option>
            <option value="SELL">SELL</option>
            <option value="BUILD">BUILD</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="min_score">Min score</label>
          <input
            id="min_score"
            name="min_score"
            type="number"
            min={0}
            max={100}
            step={1}
            defaultValue={filters.min_score ?? ""}
          />
        </div>
        <div className="field">
          <label htmlFor="max_suppliers">Max API suppliers</label>
          <input
            id="max_suppliers"
            name="max_suppliers"
            type="number"
            min={0}
            step={1}
            defaultValue={filters.max_suppliers ?? ""}
          />
        </div>
        <div className="field">
          <label htmlFor="focus_status">Focus API status</label>
          <select
            id="focus_status"
            name="focus_status"
            defaultValue={filters.focus_status ?? ""}
          >
            <option value="">Any</option>
            <option value="HOLDS_ACTIVE_DMF">Holds active DMF</option>
            <option value="NO_DMF_IDENTIFIED">None identified</option>
            <option value="UNKNOWN">Unknown</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="sort_by">Sort by</label>
          <select id="sort_by" name="sort_by" defaultValue={filters.sort_by}>
            <option value="opportunity_score">Opportunity score</option>
            <option value="molecule">Molecule</option>
            <option value="developer_count">Developer count</option>
            <option value="observed_api_supplier_count">API suppliers</option>
            <option value="months_to_next_loe_event">Exclusivity runway</option>
            <option value="last_significant_event">Last event</option>
          </select>
        </div>
        <button className="btn" type="submit">
          Apply
        </button>
        <Link className="btn" data-variant="ghost" href="/whitespace">
          Reset
        </Link>
        <a
          className="btn"
          data-variant="ghost"
          href={`${process.env.MR_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1"}/whitespace/export.csv?${exportQuery.toString()}`}
        >
          Export CSV
        </a>
      </form>

      {data.availability !== "AVAILABLE" ? (
        <EmptyState
          availability={data.availability}
          message={data.message}
          commands={
            data.availability === "DATA_UNAVAILABLE"
              ? ["moleculeradar sources ingest all", "moleculeradar engine run"]
              : undefined
          }
        />
      ) : (
        <>
          <div
            className="mono"
            style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 8 }}
          >
            {data.total} molecule{data.total === 1 ? "" : "s"} · as of{" "}
            {formatDate(data.as_of_date)} · showing {data.rows.length}
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Molecule</th>
                  <th>Therapeutic area</th>
                  <th className="num">Developers</th>
                  <th className="num">Dev. growth</th>
                  <th className="num">API suppliers</th>
                  <th>Focus API status</th>
                  <th className="num">Capability fit</th>
                  <th className="num">Reg. events 12m</th>
                  <th className="num">Exclusivity runway</th>
                  <th className="num">Shortages 36m</th>
                  <th>Opportunity</th>
                  <th className="num">Score</th>
                  <th>Evidence</th>
                  <th>Last event</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map((row) => (
                  <tr key={row.molecule.id}>
                    <td>
                      <Link href={`/molecules/${row.molecule.id}`}>
                        {row.molecule.preferred_name}
                      </Link>
                      {row.molecule.is_peptide ? (
                        <>
                          {" "}
                          <Badge tone="caution">PEPTIDE</Badge>
                        </>
                      ) : null}
                    </td>
                    <td style={{ fontSize: 12 }}>
                      {row.therapeutic_area ?? <Unavailable>—</Unavailable>}
                    </td>
                    <td className="num">
                      <Num value={row.developer_count} />
                    </td>
                    <td className="num">
                      {row.developer_growth_12m === null ? (
                        <Unavailable>N/A</Unavailable>
                      ) : (
                        formatPercent(row.developer_growth_12m)
                      )}
                    </td>
                    <td className="num">
                      <Num value={row.observed_api_supplier_count} />
                    </td>
                    <td>
                      <Badge
                        tone={
                          row.focus_api_status === "HOLDS_ACTIVE_DMF"
                            ? "positive"
                            : row.focus_api_status === "UNKNOWN"
                              ? "muted"
                              : "watch"
                        }
                      >
                        {FOCUS_STATUS_LABEL[row.focus_api_status]}
                      </Badge>
                    </td>
                    <td className="num">
                      {row.focus_capability_adjacency === null ? (
                        <Unavailable>N/A</Unavailable>
                      ) : (
                        row.focus_capability_adjacency.toFixed(2)
                      )}
                    </td>
                    <td className="num">
                      <Num value={row.regulatory_events_12m} />
                    </td>
                    <td className="num">
                      {row.months_to_next_loe_event === null ? (
                        <Unavailable>N/A</Unavailable>
                      ) : (
                        formatMonths(row.months_to_next_loe_event)
                      )}
                    </td>
                    <td className="num">
                      <Num value={row.shortage_events_36m} />
                    </td>
                    <td>
                      {row.opportunity_type ? (
                        <Badge
                          tone={
                            row.opportunity_type === "SELL" ? "accent" : "caution"
                          }
                        >
                          {row.opportunity_type}
                        </Badge>
                      ) : (
                        <Unavailable>—</Unavailable>
                      )}
                    </td>
                    <td className="num">
                      {row.opportunity_score === null ? (
                        <Unavailable>—</Unavailable>
                      ) : (
                        row.opportunity_score.toFixed(1)
                      )}
                    </td>
                    <td>
                      {row.evidence_confidence ? (
                        <ConfidenceBadge level={row.evidence_confidence} />
                      ) : (
                        <Unavailable>—</Unavailable>
                      )}
                    </td>
                    <td className="mono" style={{ fontSize: 11 }}>
                      {formatDate(row.last_significant_event)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      <p className="scope-note">{data.scope_note}</p>
      <p className="scope-note">
        A cell reading <span className="unavailable">N/A</span> means the
        underlying source has not been ingested for that molecule — not that the
        value is zero. The CSV export writes these as{" "}
        <code className="cmd">DATA_UNAVAILABLE</code> for the same reason: an
        empty spreadsheet cell gets read as zero.
      </p>
    </>
  );
}
