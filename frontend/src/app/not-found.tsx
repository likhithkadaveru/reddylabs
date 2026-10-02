import Link from "next/link";

export default function NotFound() {
  return (
    <div className="empty">
      <div className="empty-code" data-tone="muted">
        NOT FOUND
      </div>
      <p className="empty-message">
        This record does not exist in Molecule Radar&rsquo;s current coverage.
      </p>
      <div className="empty-actions">
        <Link className="btn" data-variant="ghost" href="/">
          Back to the feed
        </Link>
      </div>
    </div>
  );
}
