/**
 * Shared presentation primitives.
 *
 * `ScopedStat` and `EmptyState` are the two that carry product rules rather than
 * styling: a count cannot be rendered without its scope note, and an absence
 * must be rendered as the specific kind of absence it is.
 */

import type { DataAvailability, ScopedCount } from "@/lib/types";
import { formatCount } from "@/lib/format";

export function Badge({
  children,
  tone = "muted",
  title,
}: {
  children: React.ReactNode;
  tone?: "high" | "watch" | "accent" | "positive" | "caution" | "muted";
  title?: string;
}) {
  return (
    <span className="badge" data-tone={tone} title={title}>
      {children}
    </span>
  );
}

const BAND_TONE: Record<string, "high" | "watch" | "muted"> = {
  HIGH: "high",
  WATCH: "watch",
  LOW_CONFIDENCE: "muted",
  INSUFFICIENT_EVIDENCE: "muted",
};

export function PriorityBadge({ band }: { band: string }) {
  return (
    <Badge tone={BAND_TONE[band] ?? "muted"}>{band.replace(/_/g, " ")}</Badge>
  );
}

const CONFIDENCE_TONE: Record<string, "positive" | "watch" | "muted"> = {
  HIGH: "positive",
  MEDIUM: "watch",
  LOW: "muted",
  INSUFFICIENT: "muted",
};

export function ConfidenceBadge({ level }: { level: string }) {
  return (
    <Badge
      tone={CONFIDENCE_TONE[level] ?? "muted"}
      title="Evidence Confidence is a separate measure from the Opportunity Score and must not be combined with it."
    >
      EVIDENCE {level}
    </Badge>
  );
}

/**
 * A count rendered with its scope statement always attached.
 *
 * When the value is null the figure reads DATA UNAVAILABLE rather than 0, because
 * "we have not ingested this source" and "there are none" are different facts and
 * a zero would assert the wrong one.
 */
export function ScopedStat({
  label,
  count,
}: {
  label: string;
  count: ScopedCount;
}) {
  const unavailable = count.value === null;
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value mono" data-unavailable={unavailable}>
        {unavailable ? "DATA UNAVAILABLE" : formatCount(count.value)}
      </div>
      <div className="stat-note">{count.scope_note}</div>
    </div>
  );
}

export function Stat({
  label,
  value,
  note,
}: {
  label: string;
  value: string;
  note?: string;
}) {
  const unavailable = value === "DATA UNAVAILABLE";
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value mono" data-unavailable={unavailable}>
        {value}
      </div>
      {note ? <div className="stat-note">{note}</div> : null}
    </div>
  );
}

const EMPTY_CODES: Record<DataAvailability, string> = {
  AVAILABLE: "",
  NO_QUALIFYING_SIGNALS: "NO QUALIFYING SIGNALS",
  DATA_UNAVAILABLE: "DATA UNAVAILABLE",
  INSUFFICIENT_EVIDENCE: "INSUFFICIENT EVIDENCE",
};

/**
 * Renders an absence as the specific kind of absence it is.
 *
 * The distinction is the point: NO QUALIFYING SIGNALS means the engine looked and
 * found nothing, which is a real result. DATA UNAVAILABLE means we have not
 * ingested the source, which is not a result at all.
 */
export function EmptyState({
  availability,
  message,
  commands,
}: {
  availability: DataAvailability;
  message?: string | null;
  commands?: string[];
}) {
  return (
    <div className="empty">
      <div
        className="empty-code"
        data-tone={availability === "DATA_UNAVAILABLE" ? "muted" : undefined}
      >
        {EMPTY_CODES[availability] || "NO DATA"}
      </div>
      {message ? <p className="empty-message">{message}</p> : null}
      {commands?.length ? (
        <div className="empty-actions">
          {commands.map((cmd) => (
            <div key={cmd}>
              <code className="cmd">{cmd}</code>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function ApiDownState({ detail }: { detail: string }) {
  return (
    <div className="empty">
      <div className="empty-code">API UNAVAILABLE</div>
      <p className="empty-message">
        Molecule Radar could not reach its API, so it cannot tell you whether
        there are opportunities. This is deliberately shown as a failure rather
        than as an empty result — an empty dashboard would be indistinguishable
        from &ldquo;no opportunities exist&rdquo;.
      </p>
      <div className="empty-actions">
        <div>{detail}</div>
        <div>
          <code className="cmd">
            uvicorn moleculeradar.api.app:create_app --factory
          </code>
        </div>
      </div>
    </div>
  );
}

export function Panel({
  title,
  children,
  aside,
}: {
  title: string;
  children: React.ReactNode;
  aside?: React.ReactNode;
}) {
  return (
    <section className="panel section">
      <div className="panel-head">
        <h2 style={{ margin: 0 }}>{title}</h2>
        {aside ? <div style={{ marginLeft: "auto" }}>{aside}</div> : null}
      </div>
      <div className="panel-body">{children}</div>
    </section>
  );
}

export function Unavailable({ children }: { children?: React.ReactNode }) {
  return <span className="unavailable">{children ?? "DATA UNAVAILABLE"}</span>;
}
