"use client";

/**
 * Error boundary.
 *
 * Shows that something broke rather than rendering a blank page, because a blank
 * page is indistinguishable from "there is nothing to show" — a materially
 * different statement.
 */

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div className="empty">
      <div className="empty-code">SOMETHING WENT WRONG</div>
      <p className="empty-message">
        Molecule Radar could not render this view. It is showing a failure rather
        than an empty screen, because an empty screen would look like
        &ldquo;there is nothing here&rdquo;.
      </p>
      <div className="empty-actions">
        <div>{error.message}</div>
        {error.digest ? <div>digest {error.digest}</div> : null}
        <button className="btn" type="button" onClick={reset} style={{ marginTop: 10 }}>
          Try again
        </button>
      </div>
    </div>
  );
}
