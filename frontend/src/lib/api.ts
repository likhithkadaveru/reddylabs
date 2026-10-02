/**
 * Server-side API client.
 *
 * All data fetching happens in server components, so the API base URL and any
 * token never reach the browser bundle.
 *
 * The important behaviour here is `ApiUnavailable`: when the backend cannot be
 * reached, the UI must say so. Rendering an empty dashboard would be
 * indistinguishable from "there are no opportunities", which is a different and
 * much more consequential statement.
 */

import type {
  CompanyDetail,
  CompanyRef,
  DataHealthResponse,
  EvidenceDetail,
  FeedResponse,
  InvestigationResponse,
  MoleculeDetail,
  MoleculeRef,
  OpportunityDetail,
  SystemStatusResponse,
  TimelineEntry,
  WhiteSpaceResponse,
} from "./types";

const API_BASE =
  process.env.MR_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1";
const API_TOKEN = process.env.MR_API_TOKEN;

export class ApiUnavailable extends Error {
  constructor(
    readonly path: string,
    readonly reason: string,
    readonly status?: number,
  ) {
    super(`${path}: ${reason}`);
    this.name = "ApiUnavailable";
  }
}

async function get<T>(
  path: string,
  params?: Record<string, string | number | boolean | undefined>,
): Promise<T> {
  const url = new URL(`${API_BASE}${path}`);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== "") {
        url.searchParams.set(key, String(value));
      }
    }
  }

  let response: Response;
  try {
    response = await fetch(url, {
      headers: {
        Accept: "application/json",
        ...(API_TOKEN ? { Authorization: `Bearer ${API_TOKEN}` } : {}),
      },
      // Intelligence data changes on an ingestion cadence, not per request, but
      // staleness here would misrepresent the refresh timestamp shown to users.
      cache: "no-store",
    });
  } catch (error) {
    throw new ApiUnavailable(
      path,
      error instanceof Error ? error.message : "network error",
    );
  }

  if (response.status === 404) {
    throw new ApiUnavailable(path, "not found", 404);
  }
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiUnavailable(path, detail, response.status);
  }

  return (await response.json()) as T;
}

async function post<T>(path: string): Promise<T> {
  const url = new URL(`${API_BASE}${path}`);
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: {
        Accept: "application/json",
        ...(API_TOKEN ? { Authorization: `Bearer ${API_TOKEN}` } : {}),
      },
      cache: "no-store",
    });
  } catch (error) {
    throw new ApiUnavailable(
      path,
      error instanceof Error ? error.message : "network error",
    );
  }
  if (!response.ok) {
    throw new ApiUnavailable(path, `HTTP ${response.status}`, response.status);
  }
  return (await response.json()) as T;
}

export const api = {
  status: () => get<SystemStatusResponse>("/status"),
  feed: (params?: { as_of?: string; min_band?: string; limit?: number }) =>
    get<FeedResponse>("/feed", params),
  recentChanges: (days = 30, limit = 40) =>
    get<TimelineEntry[]>("/feed/changes", { days, limit }),
  opportunity: (id: string) =>
    get<OpportunityDetail>(`/feed/opportunity/${id}`),
  molecules: (q?: string, limit = 60) =>
    get<MoleculeRef[]>("/molecules", { q, limit }),
  molecule: (id: string) => get<MoleculeDetail>(`/molecules/${id}`),
  companies: (q?: string, limit = 60) =>
    get<CompanyRef[]>("/companies", { q, limit }),
  company: (id: string) => get<CompanyDetail>(`/companies/${id}`),
  whitespace: (params?: Record<string, string | number | undefined>) =>
    get<WhiteSpaceResponse>("/whitespace", params),
  evidence: (id: string) => get<EvidenceDetail>(`/evidence/${id}`),
  investigation: (opportunityId: string) =>
    get<InvestigationResponse>(`/investigations/${opportunityId}`),
  createInvestigation: (opportunityId: string) =>
    post<InvestigationResponse>(`/investigations/${opportunityId}`),
  dataHealth: (rerun = false) =>
    get<DataHealthResponse>("/data-health", { rerun }),
};

/** Fetch that resolves to null on an unavailable API, for optional panels. */
export async function tryFetch<T>(fn: () => Promise<T>): Promise<T | null> {
  try {
    return await fn();
  } catch (error) {
    if (error instanceof ApiUnavailable) return null;
    throw error;
  }
}
