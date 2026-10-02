/**
 * The signal decomposition.
 *
 * This component exists to make one promise visible: the Opportunity Score is
 * nothing more than the sum of its signal points. The explicit total row lets a
 * reader add the column up and get the score back.
 *
 * Compact mode shows only the highest-contributing signals, so the points on
 * screen do NOT add up to the score on their own. That case is handled by
 * showing the remainder as an explicit "further signals" row — never by
 * presenting a partial sum as though it were the total, and never by comparing a
 * partial sum against the stored score, which would make the UI report a data
 * problem that does not exist.
 *
 * The integrity check only runs in full mode, where every signal is present and a
 * mismatch would be a genuine finding.
 */

import type { SignalDetail } from "@/lib/types";
import { signalLabel } from "@/lib/format";

export function SignalDecomposition({
  signals,
  score,
  compact = false,
  totalSignalCount,
}: {
  /** In compact mode this may be a subset of the opportunity's signals. */
  signals: SignalDetail[];
  /** The authoritative stored Opportunity Score. */
  score: number;
  compact?: boolean;
  /** Total number of signals on the opportunity, when `signals` is a subset. */
  totalSignalCount?: number;
}) {
  if (signals.length === 0) {
    return (
      <div className="why">
        <div className="why-label">Why it fired</div>
        <p className="signal-text">
          No contributing signals recorded. A score without a decomposition cannot
          be explained and should not be relied on.
        </p>
      </div>
    );
  }

  const shownTotal = signals.reduce(
    (sum, s) => (s.direction === "SUPPORTING" ? sum + s.points : sum),
    0,
  );
  const hiddenCount = Math.max((totalSignalCount ?? signals.length) - signals.length, 0);
  const remainder = score - shownTotal;
  const isPartial = hiddenCount > 0;

  // Only meaningful when every signal is on screen.
  const mismatch = !isPartial && Math.abs(shownTotal - score) > 0.05;

  return (
    <div className="why">
      <div className="why-label">Why it fired</div>
      {signals.map((signal) => (
        <div className="signal-row" key={signal.signal_key}>
          <div className="signal-points" data-direction={signal.direction}>
            {signal.points >= 0 ? "+" : ""}
            {signal.points.toFixed(1)}
          </div>
          <div className="signal-text">
            <span className="signal-name">{signalLabel(signal.signal_key)}</span>
            {compact ? null : <> — {signal.explanation}</>}
          </div>
        </div>
      ))}

      {isPartial ? (
        <div className="signal-row">
          <div className="signal-points" style={{ color: "var(--text-muted)" }}>
            +{remainder.toFixed(1)}
          </div>
          <div className="signal-text" style={{ color: "var(--text-muted)" }}>
            {hiddenCount} further signal{hiddenCount === 1 ? "" : "s"} — open the
            investigation for the full decomposition
          </div>
        </div>
      ) : null}

      <div className="signal-sum">
        <div className="signal-sum-value">{score.toFixed(1)}</div>
        <div>
          Opportunity Score, out of 100 — the sum of the points above, so it can
          be checked by hand.
          {mismatch
            ? ` Warning: the signals shown total ${shownTotal.toFixed(1)}, which does not match the stored score. That is a data problem.`
            : ""}
        </div>
      </div>
    </div>
  );
}
