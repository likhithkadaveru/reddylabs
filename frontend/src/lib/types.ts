/**
 * API types, mirroring backend/src/moleculeradar/api/schemas.py.
 *
 * Kept hand-written rather than generated so the two conventions the backend
 * enforces are visible here too: counts arrive as `ScopedCount` with the scope
 * statement attached, and absence is an explicit `DataAvailability` rather than a
 * null the UI might render as zero.
 */

export type DataAvailability =
  | "AVAILABLE"
  | "NO_QUALIFYING_SIGNALS"
  | "DATA_UNAVAILABLE"
  | "INSUFFICIENT_EVIDENCE";

export type PriorityBand =
  | "HIGH"
  | "WATCH"
  | "LOW_CONFIDENCE"
  | "INSUFFICIENT_EVIDENCE";

export type EvidenceConfidence = "HIGH" | "MEDIUM" | "LOW" | "INSUFFICIENT";

export type OpportunityType = "SELL" | "BUILD";

/** A count that must never be rendered without its scope note. */
export interface ScopedCount {
  value: number | null;
  scope_note: string;
  availability: DataAvailability;
}

export interface MoleculeRef {
  id: string;
  preferred_name: string;
  inn: string | null;
  unii: string | null;
  molecule_class: string;
  is_peptide: boolean;
}

export interface CompanyRef {
  id: string;
  canonical_name: string;
  country_code: string | null;
  is_focus_company: boolean;
}

export interface SignalDetail {
  signal_key: string;
  direction: "SUPPORTING" | "COUNTER";
  points: number;
  max_points: number;
  explanation: string;
  feature_values: Record<string, unknown> | null;
  evidence_ids: string[] | null;
}

export interface CounterThesisDetail {
  concern_key: string;
  severity: "HIGH" | "MEDIUM" | "LOW";
  explanation: string;
  feature_values: Record<string, unknown> | null;
  evidence_ids: string[] | null;
}

export interface OpportunitySummary {
  id: string;
  opportunity_type: OpportunityType;
  molecule: MoleculeRef;
  counterparty: CompanyRef | null;
  as_of_date: string;
  /** How interesting it looks. Never combined with evidence_confidence. */
  opportunity_score: number;
  /** How well evidenced that picture is. A separate measure. */
  evidence_confidence: EvidenceConfidence;
  evidence_confidence_score: number;
  priority_band: PriorityBand;
  headline: string;
  engine_version: string;
  signal_count: number;
  counter_thesis_count: number;
  top_signals: SignalDetail[];
}

export interface UnevaluatedSignal {
  signal_key: string;
  max_points: number;
  reason: "missing_data" | "did_not_fire";
  missing_features: string[];
  rationale: string;
}

export interface OpportunityDetail extends OpportunitySummary {
  signals: SignalDetail[];
  counter_theses: CounterThesisDetail[];
  features: Record<string, unknown>;
  coverage: Record<string, boolean>;
  unevaluated_signals: UnevaluatedSignal[];
}

export interface FeedBandCounts {
  high: number;
  watch: number;
  low_confidence: number;
  insufficient_evidence: number;
}

export interface SourceStatusLite {
  key: string;
  name: string;
  publisher: string;
  status: string;
  verification_level: string;
  licence: string;
  expected_update_cadence: string | null;
  last_retrieved_at: string | null;
  document_count: number;
  is_enabled: boolean;
}

export interface FeedResponse {
  availability: DataAvailability;
  as_of_date: string | null;
  last_intelligence_refresh: string | null;
  engine_version: string | null;
  total: number;
  bands: FeedBandCounts;
  sell: OpportunitySummary[];
  build: OpportunitySummary[];
  message: string | null;
  source_status: SourceStatusLite[];
}

export interface SourceDocumentRef {
  id: string;
  source_key: string | null;
  source_name: string | null;
  publisher: string | null;
  licence: string | null;
  locator: string;
  sha256: string;
  retrieved_at: string;
  published_at: string | null;
  source_last_updated: string | null;
  disclaimer_text: string | null;
  attribution_text: string | null;
}

export interface EvidenceDetail {
  id: string;
  claim_text: string;
  extract_text: string | null;
  structured_record: Record<string, unknown> | null;
  source_url: string | null;
  extraction_method: string;
  confidence: number;
  event_date: string | null;
  published_at: string | null;
  retrieved_at: string | null;
  observed_at: string | null;
  source_document: SourceDocumentRef | null;
  /** Present only for model-assisted extraction, so the reader can audit it. */
  llm_audit: Record<string, unknown> | null;
}

export interface CapabilityDetail {
  capability_type: string;
  molecule: MoleculeRef | null;
  value_text: string | null;
  /** FACT / INFERENCE / UNKNOWN — never flattened into one claim. */
  assertion_level: "FACT" | "INFERENCE" | "UNKNOWN";
  inference_basis: string | null;
  confidence: number;
  evidence_id: string | null;
}

export interface TimelineEntry {
  id: string;
  event_type: string;
  event_date: string | null;
  published_at: string | null;
  summary: string;
  company: CompanyRef | null;
  payload: Record<string, unknown> | null;
  evidence_id: string | null;
}

export interface MoleculeDetail {
  molecule: MoleculeRef;
  therapeutic_area: string | null;
  observed_api_suppliers: ScopedCount;
  focus_company_supplies_api: boolean | null;
  distinct_developers: ScopedCount;
  active_trials: ScopedCount;
  next_loe_event_months: number | null;
  next_loe_event_date: string | null;
  shortage_events_36m: ScopedCount;
  markets: string[];
  api_suppliers: CompanyRef[];
  developers: CompanyRef[];
  focus_capabilities: CapabilityDetail[];
  opportunities: OpportunitySummary[];
  timeline: TimelineEntry[];
  features: Record<string, unknown>;
  coverage: Record<string, boolean>;
}

export interface CompanyOverlap {
  molecule: MoleculeRef;
  focus_company_supplies_api: boolean;
  company_holds_own_api_dmf: boolean | null;
  company_activity_summary: string;
  strength: "STRONG" | "MODERATE" | "INSUFFICIENT_EVIDENCE";
}

export interface CompanyDetail {
  company: CompanyRef;
  countries: string[];
  roles: string[];
  molecules: MoleculeRef[];
  trials: ScopedCount;
  regulatory_events: ScopedCount;
  observed_api_filings: ScopedCount;
  capabilities: CapabilityDetail[];
  overlaps: CompanyOverlap[];
  overlap_counts: Record<string, number>;
  opportunities: OpportunitySummary[];
  timeline: TimelineEntry[];
}

export interface WhiteSpaceRow {
  molecule: MoleculeRef;
  therapeutic_area: string | null;
  developer_count: number | null;
  developer_growth_12m: number | null;
  observed_api_supplier_count: number | null;
  focus_api_status: "HOLDS_ACTIVE_DMF" | "NO_DMF_IDENTIFIED" | "UNKNOWN";
  focus_capability_adjacency: number | null;
  regulatory_events_12m: number | null;
  months_to_next_loe_event: number | null;
  shortage_events_36m: number | null;
  opportunity_type: OpportunityType | null;
  opportunity_score: number | null;
  evidence_confidence: EvidenceConfidence | null;
  last_significant_event: string | null;
}

export interface WhiteSpaceResponse {
  availability: DataAvailability;
  as_of_date: string | null;
  total: number;
  rows: WhiteSpaceRow[];
  message: string | null;
  scope_note: string;
}

export interface InvestigationSection {
  key: string;
  title: string;
  body: string;
  evidence_ids: string[];
  availability: DataAvailability;
}

export interface InvestigationResponse {
  id: string | null;
  opportunity_id: string;
  as_of_date: string;
  engine_version: string;
  synthesis_method: string;
  sections: InvestigationSection[];
  created_at: string | null;
}

export interface SystemStatusResponse {
  environment: string;
  engine_version: string;
  focus_company: string;
  auth_enabled: boolean;
  has_any_data: boolean;
  molecule_count: number;
  company_count: number;
  event_count: number;
  opportunity_count: number;
  last_intelligence_refresh: string | null;
  sources_live_verified: number;
  sources_total: number;
  message: string | null;
}

export interface DataHealthCheck {
  check_key: string;
  severity: "ERROR" | "WARN" | "INFO";
  passed: boolean;
  observed_count: number;
  threshold: number | null;
  detail: string | null;
  sample_ids: string[] | null;
  created_at: string | null;
}

export interface SourceStatusRow extends SourceStatusLite {
  access_mode: string;
  last_live_verified_at: string | null;
  reachable: boolean | null;
}

export interface IngestionRunSummary {
  source_key: string;
  job_name: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  duration_seconds: number | null;
  records_processed: number;
  records_inserted: number;
  records_updated: number;
  records_quarantined: number;
  events_emitted: number;
  error_count: number;
  error_text: string | null;
}

export interface DataHealthResponse {
  generated_at: string;
  blocking_failures: number;
  checks: DataHealthCheck[];
  sources: SourceStatusRow[];
  recent_runs: IngestionRunSummary[];
  pending_reviews: number;
  quarantined_records: number;
}
