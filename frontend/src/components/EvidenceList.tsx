/**
 * Evidence list.
 *
 * Renders the full chain for each claim: the claim itself, the structured record
 * it was parsed from, the source document with its checksum and retrieval
 * timestamp, and the publisher's own licence and disclaimer.
 *
 * The publisher's disclaimer is reproduced rather than stripped. Reusing a
 * publisher's data while discarding its caveats would misrepresent it.
 */

import { api } from "@/lib/api";
import { formatDate, formatDateTime } from "@/lib/format";
import type { EvidenceDetail } from "@/lib/types";
import { Badge, EmptyState } from "./Primitives";

export async function EvidenceList({
  evidenceIds,
  limit = 40,
}: {
  evidenceIds: string[];
  limit?: number;
}) {
  if (evidenceIds.length === 0) {
    return (
      <EmptyState
        availability="INSUFFICIENT_EVIDENCE"
        message="No evidence records are cited here. A claim with no retrievable evidence must not be treated as supported."
      />
    );
  }

  const results = await Promise.all(
    evidenceIds.slice(0, limit).map(async (id) => {
      try {
        return await api.evidence(id);
      } catch {
        return null;
      }
    }),
  );
  const evidence = results.filter((e): e is EvidenceDetail => e !== null);
  const missing = results.length - evidence.length;

  return (
    <>
      {missing > 0 ? (
        <div className="banner" data-tone="error">
          <div className="banner-title">Broken evidence links</div>
          {missing} cited evidence record
          {missing === 1 ? "" : "s"} could not be retrieved. Claims resting on
          them must not be treated as supported.
        </div>
      ) : null}

      {evidence.map((item) => (
        <div className="evidence-item" key={item.id}>
          <div className="evidence-claim">{item.claim_text}</div>

          <div className="evidence-meta">
            <div>
              <b>Source:</b> {item.source_document?.source_name ?? "unknown"}
            </div>
            <div>
              <b>Publisher:</b> {item.source_document?.publisher ?? "unknown"}
            </div>
            <div>
              <b>Licence:</b> {item.source_document?.licence ?? "unknown"}
            </div>
            <div>
              <b>Event date:</b> {formatDate(item.event_date)}
            </div>
            <div>
              <b>Published:</b> {formatDate(item.published_at)}
            </div>
            <div>
              <b>Retrieved:</b> {formatDateTime(item.retrieved_at)}
            </div>
            <div>
              <b>Extraction:</b> {item.extraction_method}
            </div>
            <div>
              <b>Checksum:</b>{" "}
              {item.source_document
                ? `${item.source_document.sha256.slice(0, 16)}…`
                : "—"}
            </div>
          </div>

          {item.extraction_method === "llm_extraction" ? (
            <div style={{ marginTop: 8 }}>
              <Badge tone="caution">MODEL-ASSISTED EXTRACTION</Badge>
              {item.llm_audit ? (
                <pre className="evidence-record">
                  {JSON.stringify(item.llm_audit, null, 2)}
                </pre>
              ) : (
                <div className="banner" data-tone="error">
                  <div className="banner-title">Unauditable extraction</div>
                  This claim is recorded as model-derived but carries no audit
                  record, so it cannot be reproduced or reviewed.
                </div>
              )}
            </div>
          ) : null}

          {item.structured_record ? (
            <pre className="evidence-record">
              {JSON.stringify(item.structured_record, null, 2)}
            </pre>
          ) : null}

          {item.extract_text ? (
            <blockquote className="evidence-record">
              {item.extract_text}
            </blockquote>
          ) : null}

          {item.source_document?.disclaimer_text ? (
            <div className="disclaimer">
              <strong>Publisher&rsquo;s disclaimer:</strong>{" "}
              {item.source_document.disclaimer_text}
            </div>
          ) : null}

          {item.source_url ? (
            <div style={{ marginTop: 8, fontSize: 11 }}>
              <a href={item.source_url} target="_blank" rel="noreferrer noopener">
                Open the publisher&rsquo;s own record →
              </a>
            </div>
          ) : null}
        </div>
      ))}

      {evidenceIds.length > limit ? (
        <p className="scope-note">
          Showing {limit} of {evidenceIds.length} cited evidence records.
        </p>
      ) : null}
    </>
  );
}
