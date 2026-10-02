import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import { api, tryFetch } from "@/lib/api";
import { formatDateTime } from "@/lib/format";

export const metadata: Metadata = {
  title: "Molecule Radar — Global Pharma Intelligence",
  description:
    "Pharmaceutical opportunity intelligence from public sources, by Llama Labs.",
};

const NAV = [
  { href: "/", label: "Feed" },
  { href: "/whitespace", label: "White Space" },
  { href: "/molecules", label: "Molecules" },
  { href: "/companies", label: "Companies" },
  { href: "/data-health", label: "Data Health" },
];

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // Status is fetched here so the masthead can always state the real refresh
  // timestamp, or say plainly that there has never been one.
  const status = await tryFetch(() => api.status());

  return (
    <html lang="en">
      <body>
        <div className="shell">
          <header className="masthead">
            <div className="masthead-inner">
              <Link
                href="/"
                className="brand"
                style={{ textDecoration: "none" }}
              >
                <span className="brand-name">Molecule Radar</span>
                <span className="brand-sub">by Llama Labs</span>
              </Link>
              <nav className="nav">
                {NAV.map((item) => (
                  <Link key={item.href} href={item.href}>
                    {item.label}
                  </Link>
                ))}
              </nav>
              <div className="masthead-meta">
                {status ? (
                  <>
                    <div>
                      Last intelligence refresh:{" "}
                      {status.last_intelligence_refresh
                        ? formatDateTime(status.last_intelligence_refresh)
                        : "never"}
                    </div>
                    <div>
                      Focus: {status.focus_company} · engine{" "}
                      {status.engine_version} · {status.environment}
                    </div>
                    <div>
                      Sources live-verified: {status.sources_live_verified}/
                      {status.sources_total}
                    </div>
                  </>
                ) : (
                  <div>API unavailable</div>
                )}
              </div>
            </div>
          </header>
          <main className="page">{children}</main>
          <footer className="footer">
            Molecule Radar by Llama Labs. Built entirely on publicly accessible
            or appropriately licensed external data. All counts reflect current
            public-source coverage and are not complete global censuses.
            Opportunity Score and Evidence Confidence are separate measures and
            must not be combined. Nothing here asserts a commercial relationship
            between any companies, or constitutes legal, regulatory or investment
            advice.
          </footer>
        </div>
      </body>
    </html>
  );
}
