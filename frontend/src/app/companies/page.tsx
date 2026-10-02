import Link from "next/link";
import { ApiUnavailable, api } from "@/lib/api";
import { ApiDownState, Badge, EmptyState } from "@/components/Primitives";

export const dynamic = "force-dynamic";

export default async function CompaniesPage({
  searchParams,
}: {
  searchParams: Promise<{ q?: string }>;
}) {
  const { q } = await searchParams;
  let companies;
  try {
    companies = await api.companies(q);
  } catch (error) {
    if (error instanceof ApiUnavailable) {
      return (
        <>
          <h1>Companies</h1>
          <ApiDownState detail={error.message} />
        </>
      );
    }
    throw error;
  }

  return (
    <>
      <h1>Companies</h1>
      <p className="lede">
        Canonical companies. Legal-form variants resolve to one row; a merge is
        only ever automatic on a deterministic identifier or an exact normalised
        match, and ambiguous pairs go to human review rather than being guessed.
      </p>
      <form className="filters" method="get">
        <div className="field">
          <label htmlFor="q">Search</label>
          <input
            id="q"
            name="q"
            defaultValue={q ?? ""}
            placeholder="e.g. Dr. Reddy's"
            style={{ minWidth: 260 }}
          />
        </div>
        <button className="btn" type="submit">
          Search
        </button>
      </form>

      {companies.length === 0 ? (
        <EmptyState
          availability={q ? "NO_QUALIFYING_SIGNALS" : "DATA_UNAVAILABLE"}
          message={
            q
              ? `No company matches "${q}".`
              : "No companies have been ingested yet."
          }
          commands={q ? undefined : ["moleculeradar sources ingest all"]}
        />
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Company</th>
                <th>Role</th>
              </tr>
            </thead>
            <tbody>
              {companies.map((c) => (
                <tr key={c.id}>
                  <td>
                    <Link href={`/companies/${c.id}`}>{c.canonical_name}</Link>
                  </td>
                  <td>
                    {c.is_focus_company ? (
                      <Badge tone="positive">FOCUS COMPANY</Badge>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
