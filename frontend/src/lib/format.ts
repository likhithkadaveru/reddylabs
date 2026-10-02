/**
 * Display helpers.
 *
 * `formatCount` is the important one: a null count renders as
 * "DATA UNAVAILABLE", never as 0 or an em dash that a reader might take for
 * zero. That rule is the UI half of the backend's ScopedCount contract.
 */

export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return "DATA UNAVAILABLE";
  return value.toLocaleString("en-GB");
}

export function formatScore(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toFixed(1);
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "DATA UNAVAILABLE";
  const sign = value > 0 ? "+" : "";
  return `${sign}${(value * 100).toFixed(0)}%`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleDateString("en-GB", {
    year: "numeric",
    month: "short",
    day: "2-digit",
  });
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "never";
  return date.toLocaleString("en-GB", {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
    timeZoneName: "short",
  });
}

export function formatMonths(value: number | null | undefined): string {
  if (value === null || value === undefined) return "DATA UNAVAILABLE";
  if (value < 0) return "already passed";
  if (value < 12) return `${value} months`;
  const years = Math.floor(value / 12);
  const months = value % 12;
  return months === 0 ? `${years}y` : `${years}y ${months}m`;
}

/** Turn a signal or check key into readable text: loe_window -> "Loe window". */
export function humanise(key: string): string {
  const words = key.replace(/_/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

const SIGNAL_LABELS: Record<string, string> = {
  loe_window: "Exclusivity runway",
  limited_observed_api_supply: "Limited observed API supply",
  developer_momentum: "Regulatory filing momentum",
  clinical_momentum: "Clinical development momentum",
  capability_adjacency: "Capability alignment",
  supply_stress: "Supply stress",
  technical_barrier: "Technical barrier",
  geographic_breadth: "Geographic breadth",
  focus_supplies_api: "Focus company supplies this API",
  counterparty_development_activity: "Counterparty development activity",
  counterparty_no_observed_api_capability: "No counterparty API filing observed",
  counterparty_recent_entry: "Recent counterparty activity",
};

export function signalLabel(key: string): string {
  return SIGNAL_LABELS[key] ?? humanise(key);
}

const CONCERN_LABELS: Record<string, string> = {
  crowded_api_supply: "Crowded API supply",
  crowded_generic_field: "Crowded generic field",
  declining_clinical_activity: "Declining clinical activity",
  trial_failures: "Trial terminations recorded",
  long_patent_runway: "Long patent runway",
  drug_substance_patent_barrier: "Drug-substance patent barrier",
  no_recent_development_activity: "No recent development activity",
  single_source_evidence: "Single-source evidence",
  stale_evidence: "Stale evidence",
  coverage_gaps: "Coverage gaps",
  weak_capability_fit: "Weak capability fit",
  counterparty_has_own_api_filing: "Counterparty holds its own API filing",
  counterparty_activity_stale: "Counterparty activity is stale",
};

export function concernLabel(key: string): string {
  return CONCERN_LABELS[key] ?? humanise(key);
}

const EVENT_LABELS: Record<string, string> = {
  NEW_TRIAL: "New trial",
  TRIAL_PHASE_CHANGED: "Trial phase changed",
  TRIAL_STATUS_CHANGED: "Trial status changed",
  TRIAL_TERMINATED: "Trial terminated",
  NEW_APPROVAL: "New approval",
  NEW_FILING: "New filing",
  NEW_PATENT_LISTING: "Patent listed",
  PATENT_DELISTED: "Patent delisted",
  NEW_EXCLUSIVITY: "Exclusivity recorded",
  EXCLUSIVITY_EXPIRED: "Exclusivity expired",
  NEW_COMPANY_ENTRY: "New company entry",
  SHORTAGE_OPENED: "Shortage reported",
  SHORTAGE_RESOLVED: "Shortage resolved",
  NEW_DMF: "Drug master file recorded",
  DMF_STATUS_CHANGED: "DMF status changed",
  NEW_MARKET_ENTRY: "Market entry",
  MARKETING_ENDED: "Marketing ended",
};

export function eventLabel(key: string): string {
  return EVENT_LABELS[key] ?? humanise(key);
}
