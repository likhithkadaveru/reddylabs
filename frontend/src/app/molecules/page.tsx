import Link from "next/link";
import { ApiUnavailable, api } from "@/lib/api";
import { ApiDownState, Badge, EmptyState } from "@/components/Primitives";

export const dynamic = "force-dynamic";

export default async function MoleculesPage({
  searchParams,
}: {
  searchParams: Promise<{ q?: string }>;
}) {
  const { q } = await searchParams;
  let molecules;
  try {
    molecules = await api.molecules(q);
  } catch (error) {
    if (error instanceof ApiUnavailable) {
      return (
        <>
          <h1>Molecules</h1>
          <ApiDownState detail={error.message} />
        </>
      );
    }
    throw error;
  }

  return (
    <>
      <h1>Molecules</h1>
      <p className="lede">
        Canonical active moieties. Salt and dosage-form variants resolve to one
        row; stereoisomers deliberately do not, because they are separate products
        with separate exclusivity.
      </p>
      <form className="filters" method="get">
        <div className="field">
          <label htmlFor="q">Search</label>
          <input
            id="q"
            name="q"
            defaultValue={q ?? ""}
            placeholder="e.g. metformin"
            style={{ minWidth: 260 }}
          />
        </div>
        <button className="btn" type="submit">
          Search
        </button>
      </form>

      {molecules.length === 0 ? (
        <EmptyState
          availability={q ? "NO_QUALIFYING_SIGNALS" : "DATA_UNAVAILABLE"}
          message={
            q
              ? `No molecule matches "${q}".`
              : "No molecules have been ingested yet."
          }
          commands={q ? undefined : ["moleculeradar sources ingest all"]}
        />
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Molecule</th>
                <th>Class</th>
                <th>UNII</th>
                <th>INN</th>
              </tr>
            </thead>
            <tbody>
              {molecules.map((m) => (
                <tr key={m.id}>
                  <td>
                    <Link href={`/molecules/${m.id}`}>{m.preferred_name}</Link>
                  </td>
                  <td>
                    <Badge tone="muted">
                      {m.molecule_class.replace(/_/g, " ")}
                    </Badge>
                    {m.is_peptide ? (
                      <>
                        {" "}
                        <Badge tone="caution">PEPTIDE</Badge>
                      </>
                    ) : null}
                  </td>
                  <td className="mono" style={{ fontSize: 11 }}>
                    {m.unii ?? "—"}
                  </td>
                  <td style={{ fontSize: 12 }}>{m.inn ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
