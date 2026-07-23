import { authHeaders, withAuthQuery } from "@/lib/apiAuth";

const BASE = "";

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export const AUTH_REQUIRED_MESSAGE =
  "Remote API access requires an API key. Add it in Settings, or run the backend on localhost for local-only use.";

export function isAuthRequiredError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || error.status === 403);
}

async function errorFromResponse(res: Response): Promise<ApiError> {
  let detail = `HTTP ${res.status}`;
  try {
    const body = await res.json();
    const candidate = body.detail || body.message;
    detail = typeof candidate === "string"
      ? candidate
      : candidate?.message || (candidate ? JSON.stringify(candidate) : detail);
  } catch { /* ignore */ }
  if (res.status === 401 || res.status === 403) {
    detail = AUTH_REQUIRED_MESSAGE;
  }
  return new ApiError(detail, res.status);
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const { headers, ...rest } = options ?? {};
  const mergedHeaders: Record<string, string> = { "Content-Type": "application/json", ...authHeaders() };
  if (headers) {
    new Headers(headers).forEach((value, key) => {
      mergedHeaders[key] = value;
    });
  }
  const res = await fetch(`${BASE}${path}`, {
    ...rest,
    headers: mergedHeaders,
  });
  if (!res.ok) {
    throw await errorFromResponse(res);
  }
  const text = await res.text();
  return text ? JSON.parse(text) : ({} as T);
}

async function requestBlob(path: string): Promise<Blob> {
  const res = await fetch(`${BASE}${path}`, { headers: authHeaders() });
  if (!res.ok) {
    throw await errorFromResponse(res);
  }
  return res.blob();
}

export interface UploadResult {
  status: string;
  file_path: string;
  filename: string;
}

export type ResponseMode = "chat" | "deep_report";

export interface SendMessageOptions {
  responseMode?: ResponseMode;
  reportProfile?: "equity_deep_research" | "etf_deep_research";
  routingDecisionId?: string;
}

export interface DeepReportArtifact {
  artifact_id: "markdown" | "pdf" | string;
  artifact_type: string;
  filename: string;
  path: string;
  available: boolean;
  artifact_role?: "report" | "diagnostic" | "diff" | "pdf" | string;
  previewable?: boolean;
}

export interface DeepReportModule {
  status: "pending" | "running" | "passed" | "warning" | "failed_validation" | "insufficient_evidence" | "not_requested" | string;
  coverage?: number | null;
  reason?: string | null;
  details?: Record<string, unknown>;
}

export type ETFReadinessStatus =
  | "not_publishable"
  | "structure_ready"
  | "penetration_partial"
  | "penetration_ready";

export interface ETFReportReadiness {
  version?: string;
  status: ETFReadinessStatus;
  evidence_quality?: "passed" | "passed_with_gaps" | "failed_validation" | string;
  hard_gate_passed?: boolean;
  structure_checks?: Record<string, boolean>;
  penetration_checks?: Record<string, boolean>;
  metrics?: {
    holdings_weight_coverage?: number;
    selected_component_count?: number;
    selected_weight_coverage?: number;
    component_research_coverage?: number;
    fully_supported_etf_weight?: number;
    mandatory_incomplete_components?: string[];
    [key: string]: unknown;
  };
  thresholds?: Record<string, number>;
  reason_codes?: string[];
  missing_actions?: string[];
  input_fingerprint?: string;
}

export interface DeepReportRecord {
  schema_version?: number;
  report_id: string;
  session_id: string;
  attempt_id: string;
  profile: "equity_deep_research" | string;
  instrument_type?: "company_equity" | "etf" | "index";
  symbol: string;
  security_name: string;
  report_date: string;
  data_as_of: string;
  quality_status: "passed" | "passed_with_gaps" | "failed_validation";
  status: "running" | "completed" | "failed" | "cancelled" | string;
  analysis_modules: Record<string, DeepReportModule>;
  pipeline_checks?: Record<string, DeepReportModule>;
  report_sections?: Record<string, DeepReportModule>;
  etf_readiness?: ETFReportReadiness;
  artifacts: DeepReportArtifact[];
  validation_issues: string[];
  created_at: string;
  updated_at: string;
  revision: number;
  parent_report_id?: string | null;
  revision_mode?: "initial" | "full_refresh" | "section_revision" | "repair" | string;
  revision_sections?: string[];
  pipeline_state?: string;
  repair_round?: number;
  delivery_kind?: "report" | "diagnostic";
  latest_revision_id?: string | null;
  generation_source?: "manual" | "portfolio_monitor_autopilot" | string;
  generation_reason?: string;
  content?: string | null;
  content_role?: "report" | "diagnostic" | null;
  research_coverage?: ResearchCoverage;
  history_delta?: ResearchDelta;
  subject_profile?: ETFProductProfile | null;
}

export interface ResearchCoverageDomain {
  domain: string;
  required: boolean;
  preferred_source_classes: string[];
  minimum_independent_sources: number;
  freshness_policy: string;
  status: string;
  unresolved_questions: string[];
}

export interface ResearchCoverage {
  coverage_snapshot_id?: string;
  symbol?: string;
  profile?: string;
  as_of?: string;
  acquisition_budget?: string;
  prior_report_id?: string | null;
  domains: ResearchCoverageDomain[];
  material_conflicts?: unknown[];
  reused_fact_count?: number;
  refreshed_fact_count?: number;
}

export interface ResearchDelta {
  base_report_id?: string | null;
  added: Array<Record<string, unknown>>;
  updated: Array<Record<string, unknown>>;
  confirmed: Array<Record<string, unknown>>;
  superseded?: Array<Record<string, unknown>>;
  contradicted: Array<Record<string, unknown>>;
  stale: Array<Record<string, unknown>>;
  still_unverified?: Array<Record<string, unknown>>;
}

export interface ResearchKnowledgeSearchResult {
  facts: Array<Record<string, unknown>>;
  evidence: Array<Record<string, unknown>>;
  prior_claims: Array<Record<string, unknown>>;
  chunks: Array<Record<string, unknown>>;
}

export interface ResearchSymbolHistory {
  symbol: string;
  reports: Array<Record<string, unknown>>;
}

export type ReportHorizon = "intraday" | "daily" | "weekly" | "structural";
export type ReportKind =
  | "deep_research"
  | "daily_holding"
  | "daily_portfolio"
  | "weekly_review"
  | "monitor_research"
  | "component_research";

export interface ReportLibraryViewpoint {
  viewpoint_id: string;
  report_id: string;
  horizon: ReportHorizon;
  stance: "bullish" | "neutral" | "bearish" | "mixed" | "unknown";
  action: "observe" | "add" | "reduce" | "exit" | "not_applicable";
  confidence: "low" | "medium" | "high" | "unknown";
  summary_claim_id?: string | null;
  reason_claim_ids: string[];
  risk_claim_ids: string[];
  condition_claim_ids: string[];
  invalidation_claim_ids: string[];
  valid_from?: string | null;
  valid_until?: string | null;
}

export interface ReportLibraryArtifact {
  artifact_id: string;
  artifact_role: string;
  filename: string;
  media_type: string;
  source_locator: string;
  sha256?: string | null;
  available: boolean;
  revision: number;
  url?: string | null;
}

export interface ReportLibraryRecord {
  report_id: string;
  family_id: string;
  report_kind: ReportKind;
  subject_type: "symbol" | "portfolio";
  subject_key: string;
  symbol?: string | null;
  security_name: string;
  status: "published" | "diagnostic" | "archived";
  report_quality_status: "passed" | "passed_with_gaps" | "failed_validation";
  coverage_status: "complete" | "partial" | "insufficient" | "unknown";
  generated_at: string;
  data_as_of: string;
  report_period: {
    start_date?: string | null;
    end_date?: string | null;
    label?: string | null;
  };
  source_type: string;
  source_id: string;
  source_revision: number;
  knowledge_link: {
    coverage_snapshot_id?: string | null;
    evidence_ids?: string[];
    fact_ids?: string[];
    claim_ids?: string[];
    internal_reference_code?: string | null;
    profile?: string | null;
    instrument_type?: "company_equity" | "etf" | "index" | null;
    etf_readiness?: ETFReportReadiness;
    pipeline_checks?: Record<string, DeepReportModule>;
    report_sections?: Record<string, DeepReportModule>;
    etf_penetration?: {
      selection_status?: string;
      component_research_status?: string;
      selected_count?: number;
      selected_weight_coverage?: number;
      explanation_coverage?: number;
      research_coverage?: number;
      fully_supported_coverage?: number;
      reusable_count?: number;
      missing_count?: number;
      stale_count?: number;
      conflicted_count?: number;
    } | null;
    monitoring_bundle_artifact_id?: string | null;
    monitoring_bundle_source_locator?: string | null;
    monitoring_bundle_status?: "available" | "not_recommended" | "data_insufficient";
    monitoring_candidate_count?: number;
    monitoring_schema_version?: number;
  };
  monitoring_bundle?: MonitoringBundle | null;
  weekly_review?: WeeklyReviewSummary | null;
  viewpoints: ReportLibraryViewpoint[];
  artifacts: ReportLibraryArtifact[];
  relations: Array<Record<string, unknown>>;
  created_at: string;
  updated_at: string;
  sources?: ReportSourceLink[];
}

export interface ReportLibraryCurrentCandidate {
  report_id: string;
  report_kind: ReportLibraryRecord["report_kind"];
  symbol?: string | null;
  security_name: string;
  data_as_of: string;
  generated_at: string;
  report_quality_status: ReportLibraryRecord["report_quality_status"];
  coverage_status: ReportLibraryRecord["coverage_status"];
  viewpoint: ReportLibraryViewpoint;
  summary?: { claim_id: string; section_id?: string | null; text: string } | null;
  risks?: Array<{ claim_id: string; section_id?: string | null; text: string }>;
  pending_items?: Array<{ claim_id: string; section_id?: string | null; text: string }>;
}

export interface ETFUniverseComponent {
  symbol: string;
  name: string;
  weight: number;
  metadata: Record<string, unknown>;
}

export interface ETFUniverseProfile {
  snapshot_id?: string | null;
  etf_symbol: string;
  etf_name?: string | null;
  tracked_index_code?: string | null;
  tracked_index_name?: string | null;
  data_as_of: string;
  retrieved_at?: string | null;
  freshness_expires_at?: string | null;
  quality_status?: string | null;
  quality?: string | null;
  provider_id?: string | null;
  source_type: string;
  source_ids: string[];
  source_urls: string[];
  weight_scale: "fraction" | string;
  weight_semantics: "tracked_index_weight" | "disclosed_fund_holding_weight";
  expected_component_count: number;
  observed_component_count: number;
  observed_weight_coverage: number;
  required_field_coverage: number;
  universe_complete: boolean;
  partial_components_are_top_ranked: boolean;
  warnings: string[];
  components: ETFUniverseComponent[];
}

export interface ETFProductField {
  value: string | number | boolean | number[] | null;
  status: "available" | "missing" | "stale" | "conflict" | string;
  unit?: string | null;
  data_as_of: string;
  source_ids: string[];
  semantics: string;
  note?: string | null;
}

export interface ETFPeerFlowMember {
  symbol: string;
  name?: string | null;
  manager?: string | null;
  mapping_status: "official_index_code" | "name_alias_requires_cross_check" | string;
  data_as_of: string;
  current_units?: number | null;
  delta_1d?: number | null;
  delta_5d?: number | null;
  delta_20d?: number | null;
  current_price?: number | null;
  estimation_price?: number | null;
  estimation_price_type?: "exchange_market_price" | "exchange_published_nav_proxy" | string | null;
  estimated_net_flow_1d?: number | null;
  estimated_net_flow_semantics?: string | null;
  source_ids: string[];
}

export interface ETFProductProfile {
  schema_version: number;
  profile_snapshot_id: string;
  symbol: string;
  data_as_of: string;
  retrieved_at: string;
  snapshot_ids: Record<string, string>;
  identity: Record<string, ETFProductField>;
  index_methodology: Record<string, ETFProductField>;
  product_metrics: Record<string, ETFProductField>;
  share_history?: {
    current_units?: number | null;
    delta_1d?: number | null;
    delta_5d?: number | null;
    delta_20d?: number | null;
    estimated_net_flow_1d?: number | null;
    estimated_net_flow_semantics?: string | null;
    observations?: Array<{ data_as_of: string; fund_units: number; source_ids: string[] }>;
  } | null;
  peer_group?: {
    tracked_index_code?: string | null;
    tracked_index_name?: string | null;
    data_as_of: string;
    member_count: number;
    official_index_mapping_count: number;
    name_mapped_count: number;
    estimated_net_flow_1d?: number | null;
    estimated_net_flow_semantics?: string | null;
    inflow_member_ratio_1d?: number | null;
    flow_coverage_ratio: number;
    unit_change_coverage_ratio: number;
    market_price_flow_count?: number;
    nav_proxy_flow_count?: number;
    members: ETFPeerFlowMember[];
    warnings: string[];
  } | null;
  sources: Array<{
    source_id: string;
    kind: string;
    title: string;
    publisher: string;
    url?: string | null;
    content_hash: string;
    retrieved_at: string;
    published_at?: string | null;
    verification_status: SourceVerificationStatus | string;
  }>;
  hard_gate_status: "passed" | "failed_validation" | string;
  quality_status: "passed" | "passed_with_gaps" | "failed_validation" | string;
  missing_hard_fields: string[];
  missing_optional_fields: string[];
  conflicts: Array<Record<string, unknown>>;
  refresh_errors: unknown[];
  refresh_status: "completed" | "completed_with_gaps" | "cache_only" | string;
  source_policy?: {
    registry_version: string;
    rules: Array<{
      rule_id: string;
      label: string;
      phase: "product_profile" | "share_flow" | string;
      slot: string;
      source_kind: string;
      publisher: string;
      verification_status: SourceVerificationStatus | string;
      priority: number;
      parser_id: string;
      response_type: string;
      provides: string[];
      required_for_publish: boolean;
      freshness_days: number;
      refresh_trigger: string;
      failure_policy: string;
      status: "completed" | "completed_with_gaps" | "failed" | string;
      source_id?: string | null;
      url?: string | null;
      error?: string | null;
    }>;
  } | null;
  cache_reused_sections?: Array<{ section: string; snapshot_id: string }>;
  stale?: boolean;
}

export interface InstrumentHistoricalPercentileMetric {
  key: string;
  label: string;
  value: number | null;
  unit?: string | null;
  percentile: number | null;
  temperature: "极冷" | "偏冷" | "正常" | "偏热" | "极热"
    | "极低" | "偏低" | "中位" | "偏高" | "极高" | "暂无" | string;
  observation_count?: number | null;
  sample_start?: string | null;
  sample_end?: string | null;
  definition?: string | null;
}

export interface InstrumentHistoricalPercentileSnapshot {
  schema_version: number;
  snapshot_id: string;
  symbol: string;
  instrument_type?: "company_equity" | "etf" | "index" | string;
  instrument_name?: string | null;
  valuation_basis?: "company_valuation" | "tracked_index_valuation" | "index_valuation"
    | "adjusted_price_history" | string;
  scope_label?: string | null;
  tracked_index_code?: string | null;
  tracked_index_name?: string | null;
  status: "available" | "unavailable" | string;
  lookback_years: number;
  sample_start?: string | null;
  sample_end?: string | null;
  sample_count?: number | null;
  data_as_of: string;
  retrieved_at: string;
  mapping_method: string;
  percentile_method?: string | null;
  metrics: InstrumentHistoricalPercentileMetric[];
  source: {
    source_id: string;
    provider_id: string;
    label: string;
    publisher: string;
    verification_status: SourceVerificationStatus | string;
    url: string;
    methodology_url: string;
    retrieved_at: string;
  };
  unavailable_reason?: string | null;
  warnings: string[];
  history_count: number;
}

/** Backward-compatible names for ETF callers while the API rolls out generically. */
export type ETFValuationPercentileMetric = InstrumentHistoricalPercentileMetric;
export type ETFValuationPercentileSnapshot = InstrumentHistoricalPercentileSnapshot;

export type InstrumentProfileMetricStatus = "available" | "unavailable";

export interface InstrumentProfileMetric {
  key: string;
  label: string;
  value: number | null;
  unit: "CNY" | "pct" | "ratio" | "multiple" | "shares" | "fund_units" | "CNY_per_10_shares" | "CNY_per_fund_unit" | string;
  category: "market" | "scale" | "valuation" | "profitability" | "dividend" | string;
  status: InstrumentProfileMetricStatus;
  unavailable_reason?: string | null;
  source_id: string;
  data_as_of: string;
  raw_field?: string | null;
  semantics: string;
}

export interface InstrumentProfileSnapshot {
  schema_version: number;
  snapshot_id: string;
  symbol: string;
  instrument_type: "company_equity" | "etf" | "index";
  data_as_of: string;
  retrieved_at: string;
  quality_status: "complete" | "partial" | string;
  history_count: number;
  identity: {
    symbol: string;
    name: string;
    instrument_type: "company_equity" | "etf" | "index";
    exchange: string;
    currency: string;
    industry?: string | null;
    region?: string | null;
    concepts: string[];
    listing_date?: string | null;
  };
  metrics: InstrumentProfileMetric[];
  sources: Array<{
    source_id: string;
    provider_id: string;
    label: string;
    data_as_of: string;
    retrieved_at: string;
    url?: string | null;
    plan?: string | null;
    report_date?: string | null;
    ex_dividend_date?: string | null;
  }>;
  warnings: string[];
}

export type SourceVerificationStatus =
  | "official_primary"
  | "live_retrieved"
  | "source_recorded"
  | "historical_context";

export type SourceKind =
  | "official_filing"
  | "company_disclosure"
  | "fund_product"
  | "index_methodology"
  | "index_constituents"
  | "fund_share_scale"
  | "market_data"
  | "structured_financial"
  | "consensus_data"
  | "derived_analysis"
  | "news"
  | "broker_research"
  | "fundamental"
  | "report"
  | "other";

export interface CollectedSourceSummary {
  observation_id?: string;
  document_ref: string;
  source_kind: SourceKind;
  title: string;
  publisher: string;
  provider_id?: string | null;
  published_at?: string | null;
  retrieved_at?: string | null;
  observed_at?: string | null;
  source_url?: string | null;
  source_locator?: string | null;
  verification_status: SourceVerificationStatus;
  body_status: "full_text" | "structured_payload" | "excerpt" | "metadata_only" | string;
  used_by_report_count: number;
  structured_status?: "validated" | "needs_review" | "not_applicable" | "failed" | "superseded" | null;
  structured_metrics_count?: number;
  ocr_performed?: boolean;
  structured_extractor_version?: string | null;
  structured_failed_checks?: string[];
  structured_error?: string | null;
  structured_auto_repair_available?: boolean;
  metadata?: Record<string, unknown>;
}

export interface ReportSourceLink extends CollectedSourceSummary {
  report_id: string;
  revision: number;
  relation_type: "cited" | "supporting" | "input" | string;
  evidence_ids: string[];
  fact_ids: string[];
  claim_ids: string[];
  section_ids: string[];
}

export interface ResearchNoteResolution {
  note_claim_id: string;
  report_id: string;
  report_claim_id: string;
  resolution_status: "confirmed" | "contradicted" | "superseded";
  resolved_at: string;
}

export interface ResearchNote {
  note_claim_id: string;
  subject_key: string;
  session_id: string;
  message_id: string;
  role: "user" | "assistant";
  text: string;
  claim_status: string;
  created_at: string;
  derived_status: "unverified" | ResearchNoteResolution["resolution_status"];
  resolutions: ResearchNoteResolution[];
}

export interface ReportSourceDocument {
  document_id: string;
  kind: SourceKind;
  title: string;
  summary?: string | null;
  publisher: string;
  provider?: string | null;
  analyst?: string | string[] | null;
  association_scope?: "direct_subject" | "etf_product" | "tracking_index" | "industry_theme" | "key_constituent" | string | null;
  related_symbol?: string | null;
  evidence_level?: "A" | "B" | "C" | "D" | null;
  published_at?: string | null;
  retrieved_at: string;
  source_url?: string | null;
  source_locator?: string | null;
  verification_status: SourceVerificationStatus;
  body_status?: string;
  used_by_report_count?: number;
  structured_status?: CollectedSourceSummary["structured_status"];
  structured_metrics_count?: number;
  ocr_performed?: boolean;
  structured_extractor_version?: string | null;
  structured_failed_checks?: string[];
  structured_error?: string | null;
  structured_auto_repair_available?: boolean;
  reporting_year?: number | null;
  filing_type?: string | null;
  metrics: Array<{ label: string; value: number; unit: string }>;
}

export interface ReportSourceBundle {
  symbol: string;
  generated_at?: string | null;
  traceable_count: number;
  excluded_count: number;
  verification_counts: Record<SourceVerificationStatus, number>;
  domains: Array<{
    kind: ReportSourceDocument["kind"];
    label: string;
    description: string;
    document_count: number;
    documents: ReportSourceDocument[];
  }>;
  verification_contract: Record<SourceVerificationStatus, string>;
}

export interface AnnualReportCoverage {
  symbol: string;
  requested_years: number[];
  covered_years: number[];
  archived_years?: number[];
  analysis_ready_years?: number[];
  needs_review_years?: number[];
  unusable_years?: number[];
  missing_years: number[];
  coverage_ratio: number;
  analysis_ready_ratio?: number;
  documents_by_year: Record<string, Array<{
    document_ref: string;
    title: string;
    source_url?: string | null;
    structured_status?: string | null;
  }>>;
  unusable_documents_by_year?: Record<string, Array<{
    document_ref: string;
    title: string;
    source_url?: string | null;
    structured_status?: string | null;
  }>>;
}

export interface AnnualReportBackfillResult {
  symbol: string;
  status: "completed" | "completed_with_gaps";
  collection_scope: "historical_annual_reports";
  refreshed: number;
  failed: number;
  document_refs: string[];
  coverage: AnnualReportCoverage;
  provider_attempts: Array<Record<string, unknown>>;
  structured: Record<string, unknown>;
}

export interface AnnualReportBackfillJobResult {
  symbol: string;
  status: "completed" | "completed_with_gaps";
  refreshed: number;
  failed: number;
  document_refs: string[];
  relevance_downgraded?: boolean;
  coverage: AnnualReportCoverage;
  structured: Record<string, unknown>;
}

export type AnnualReportBackfillJobStatus =
  | "queued"
  | "running"
  | "completed"
  | "completed_with_gaps"
  | "failed"
  | "cancelled"
  | "interrupted";

export type AnnualReportBackfillPhaseStatus =
  | "pending"
  | "running"
  | "completed"
  | "reused"
  | "failed";

export interface AnnualReportBackfillYearProgress {
  year: number;
  status: string;
  current_stage: string;
  message: string;
  provider_id?: string | null;
  document_ref?: string | null;
  error?: string | null;
  updated_at?: string | null;
  phases: Record<"discovery" | "download" | "parsing" | "validation", AnnualReportBackfillPhaseStatus>;
}

export interface AnnualReportBackfillJob {
  schema_version: number;
  job_id: string;
  symbol: string;
  years: number[];
  force: boolean;
  status: AnnualReportBackfillJobStatus;
  stage: string;
  message: string;
  progress_pct: number;
  year_progress: AnnualReportBackfillYearProgress[];
  result?: AnnualReportBackfillJobResult | null;
  error?: string | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  updated_at: string;
}

export interface AnnualReportBackfillJobAccepted {
  status: "accepted";
  job_id: string;
  deduplicated: boolean;
  job: AnnualReportBackfillJob;
}

export interface ReportLibrarySubject {
  subject_type?: "symbol" | "portfolio" | null;
  subject_key: string;
  symbol?: string | null;
  security_name?: string | null;
  report_count?: number;
  latest_generated_at?: string | null;
  current: Record<ReportHorizon, {
    latest: ReportLibraryCurrentCandidate | null;
    latest_complete: ReportLibraryCurrentCandidate | null;
  }>;
  timeline: ReportLibraryRecord[];
  instrument_profile?: InstrumentProfileSnapshot | null;
  etf_universe?: ETFUniverseProfile | null;
  etf_product?: ETFProductProfile | null;
  historical_percentile?: InstrumentHistoricalPercentileSnapshot | null;
  etf_valuation_percentile?: ETFValuationPercentileSnapshot | null;
  component_research?: Record<string, unknown> | null;
  source_bundle?: ReportSourceBundle | null;
  profile?: {
    etf?: {
      instrument?: InstrumentProfileSnapshot | null;
      universe?: ETFUniverseProfile | null;
      product?: ETFProductProfile | null;
      historical_percentile?: InstrumentHistoricalPercentileSnapshot | null;
      valuation_percentile?: ETFValuationPercentileSnapshot | null;
      component_research?: Record<string, unknown> | null;
    };
    equity?: {
      instrument?: InstrumentProfileSnapshot | null;
      historical_percentile?: InstrumentHistoricalPercentileSnapshot | null;
      component_research?: Record<string, unknown> | null;
    };
    index?: {
      instrument?: InstrumentProfileSnapshot | null;
      historical_percentile?: InstrumentHistoricalPercentileSnapshot | null;
      component_research?: Record<string, unknown> | null;
    };
  };
}

export interface ReportLibrarySubjectSummary {
  subject_key: string;
  symbol?: string | null;
  security_name: string;
  report_count: number;
  new_report_count: number;
  latest_generated_at: string;
  latest_data_as_of?: string | null;
  research_note_count: number;
  confirmed_note_count: number;
  broker_research_count: number;
  report_kinds: ReportKind[];
  current_viewpoint_summary?: string;
  quality_summary: { passed: number; complete: number };
  latest_report: Pick<
    ReportLibraryRecord,
    | "report_id"
    | "report_kind"
    | "subject_key"
    | "symbol"
    | "security_name"
    | "status"
    | "report_quality_status"
    | "coverage_status"
    | "generated_at"
    | "data_as_of"
    | "report_period"
  > | null;
}

export interface ReportLibraryComparison {
  selected: Array<{
    report_id: string;
    report_kind: ReportLibraryRecord["report_kind"];
    subject_key: string;
    symbol?: string | null;
    security_name?: string | null;
    data_as_of: string;
    generated_at: string;
    viewpoint: ReportLibraryViewpoint;
    claims: Array<{ claim_id: string; section_id?: string | null; text: string }>;
  }>;
  deltas: Array<{
    base_report_id: string;
    current_report_id: string;
    relation: "continued" | "updated" | "diverged" | "different_horizon" | "not_comparable";
    changes: Record<string, { before: unknown; after: unknown }>;
    research_delta: ResearchDelta | Record<string, unknown>;
  }>;
  ai_summary: {
    status: "not_requested" | "disabled" | "unavailable" | "completed";
    summary?: string;
    items?: Array<{
      text: string;
      citations: Array<{ report_id: string; claim_id: string; section_id?: string | null }>;
    }>;
  };
}

export interface RunReportPreview {
  run_id: string;
  title: string;
  filename: string;
  relative_path: string;
  content: string;
  updated_at: string;
  source: "run_artifact" | string;
}

export interface ReportArtifactDeliveryResult {
  status: string;
  filename: string;
  target_id: string;
  target_name?: string;
  provider?: string;
  remote_message_id?: string;
}

export interface EquityResolutionOption {
  symbol: string;
  security_name: string;
  market?: string | null;
  source?: string | null;
  instrument_type?: "company_equity" | "etf" | "index";
}

export interface EquityResolution {
  status: "resolved" | "ambiguous" | "not_found";
  query: string;
  symbol?: string;
  security_name?: string;
  market?: string | null;
  source?: string | null;
  instrument_type?: "company_equity" | "etf" | "index";
  options: EquityResolutionOption[];
  source_statuses?: Record<string, string>;
}

async function uploadFile(file: File): Promise<UploadResult> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${BASE}/upload`, { method: "POST", headers: authHeaders(), body: form });
  if (!res.ok) {
    throw await errorFromResponse(res);
  }
  return res.json();
}

async function generatePdf(title: string, content: string): Promise<Blob> {
  const res = await fetch(`${BASE}/reports/pdf`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ title, content }),
  });
  if (!res.ok) throw await errorFromResponse(res);
  return res.blob();
}

function appendQueryParam(url: string, key: string, value: string): string {
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}${encodeURIComponent(key)}=${encodeURIComponent(value)}`;
}

export const api = {
  uploadFile,
  generatePdf,
  listRuns: (limit?: number) => request<RunListItem[]>(`/runs${limit ? `?limit=${encodeURIComponent(String(limit))}` : ""}`),
  getRun: (id: string, params: RunDetailParams = {}) => {
    const q = new URLSearchParams();
    if (params.chart_payload) q.set("chart_payload", params.chart_payload);
    if (params.chart_symbol) q.set("chart_symbol", params.chart_symbol);
    const qs = q.toString();
    return request<RunData>(`/runs/${id}${qs ? `?${qs}` : ""}`);
  },
  getRunReportPreview: (id: string) =>
    request<RunReportPreview>(`/runs/${encodeURIComponent(id)}/report-preview`),
  getRunCode: (id: string) => request<Record<string, string>>(`/runs/${id}/code`),
  getRunPine: (id: string) => request<PineScriptResult>(`/runs/${id}/pine`),
  listSessions: () => request<SessionItem[]>("/sessions"),
  createSession: (title?: string) => request<SessionItem>("/sessions", { method: "POST", body: JSON.stringify({ title: title || "" }) }),
  forkSession: (sid: string, afterMessageId: string, title?: string) =>
    request<SessionItem>(`/sessions/${sid}/fork`, {
      method: "POST",
      body: JSON.stringify({ after_message_id: afterMessageId, title: title || "" }),
    }),
  deleteSession: (sid: string) => request<{ status: string }>(`/sessions/${sid}`, { method: "DELETE" }),
  renameSession: (sid: string, title: string) => request<{ status: string }>(`/sessions/${sid}`, { method: "PATCH", body: JSON.stringify({ title }) }),
  continueSessionOnFeishu: (sid: string) =>
    request<{ status: string; session_id: string; target_id: string; chat_type: string }>(`/sessions/${sid}/continue-on-feishu`, { method: "POST" }),
  sendMessage: (sid: string, content: string, options: SendMessageOptions = {}) =>
    request<{ message_id: string; attempt_id: string }>(`/sessions/${sid}/messages`, {
      method: "POST",
      body: JSON.stringify({
        content,
        response_mode: options.responseMode ?? "chat",
        report_profile: options.reportProfile,
        routing_decision_id: options.routingDecisionId,
      }),
    }),
  editMessage: (sid: string, messageId: string, content: string, rerun = true) =>
    request<{ message_id: string; attempt_id?: string }>(`/sessions/${sid}/messages/${messageId}`, {
      method: "PATCH",
      body: JSON.stringify({ content, rerun }),
    }),
  cancelSession: (sid: string) => request<{ status: string }>(`/sessions/${sid}/cancel`, { method: "POST" }),
  getSessionMessages: (sid: string) => request<MessageItem[]>(`/sessions/${sid}/messages`),
  getSessionUsage: (sid: string) =>
    request<SessionUsageSummary>(`/sessions/${encodeURIComponent(sid)}/usage`),
  getSessionUsageEvents: (sid: string, filters: UsageEventFilters = {}) => {
    const query = new URLSearchParams();
    if (filters.kind) query.set("kind", filters.kind);
    if (filters.category) query.set("category", filters.category);
    if (filters.attemptId) query.set("attempt_id", filters.attemptId);
    if (filters.cursor) query.set("cursor", filters.cursor);
    if (filters.limit) query.set("limit", String(filters.limit));
    const suffix = query.toString();
    return request<UsageEventsPage>(
      `/sessions/${encodeURIComponent(sid)}/usage/events${suffix ? `?${suffix}` : ""}`,
    );
  },
  listDeepReports: (limit = 100) => request<DeepReportRecord[]>(`/reports?limit=${encodeURIComponent(String(limit))}`),
  resolveDeepReportEquity: (query: string) =>
    request<EquityResolution>("/reports/resolve-equity", {
      method: "POST",
      body: JSON.stringify({ query }),
    }),
  resolveDeepReportInstrument: (query: string) =>
    request<EquityResolution>("/reports/resolve-instrument", {
      method: "POST",
      body: JSON.stringify({ query }),
    }),
  getDeepReport: (reportId: string, includeContent = false) =>
    request<DeepReportRecord>(`/reports/${encodeURIComponent(reportId)}${includeContent ? "?include_content=true" : ""}`),
  searchResearchKnowledge: (params: {
    query?: string;
    symbol?: string;
    domains?: string[];
    metrics?: string[];
    asOf?: string;
    limit?: number;
  }) => {
    const query = new URLSearchParams();
    if (params.query) query.set("query", params.query);
    if (params.symbol) query.set("symbol", params.symbol);
    params.domains?.forEach((value) => query.append("domains", value));
    params.metrics?.forEach((value) => query.append("metrics", value));
    if (params.asOf) query.set("as_of", params.asOf);
    if (params.limit) query.set("limit", String(params.limit));
    return request<ResearchKnowledgeSearchResult>(`/research/knowledge/search?${query.toString()}`);
  },
  getResearchSymbolHistory: (symbol: string, limit = 20) =>
    request<ResearchSymbolHistory>(
      `/research/symbols/${encodeURIComponent(symbol)}/history?limit=${encodeURIComponent(String(limit))}`,
    ),
  listReportLibrary: (params: {
    query?: string;
    subjectType?: "symbol" | "portfolio";
    reportKind?: string;
    horizon?: ReportHorizon;
    status?: string;
    quality?: string;
    startAt?: string;
    endAt?: string;
    limit?: number;
    cursor?: string;
  } = {}) => {
    const query = new URLSearchParams();
    if (params.query) query.set("query", params.query);
    if (params.subjectType) query.set("subject_type", params.subjectType);
    if (params.reportKind) query.set("report_kind", params.reportKind);
    if (params.horizon) query.set("horizon", params.horizon);
    if (params.status) query.set("status", params.status);
    if (params.quality) query.set("quality", params.quality);
    if (params.startAt) query.set("start_at", params.startAt);
    if (params.endAt) query.set("end_at", params.endAt);
    if (params.limit) query.set("limit", String(params.limit));
    if (params.cursor) query.set("cursor", params.cursor);
    const suffix = query.toString();
    return request<{ reports: ReportLibraryRecord[]; next_cursor?: string | null; total_count: number }>(
      `/report-library/reports${suffix ? `?${suffix}` : ""}`,
    );
  },
  listReportLibrarySubjects: (params: {
    query?: string;
    reportKind?: ReportKind;
    quality?: string;
    startAt?: string;
    endAt?: string;
    limit?: number;
    cursor?: string;
  } = {}) => {
    const query = new URLSearchParams();
    if (params.query) query.set("query", params.query);
    if (params.reportKind) query.set("report_kind", params.reportKind);
    if (params.quality) query.set("quality", params.quality);
    if (params.startAt) query.set("start_at", params.startAt);
    if (params.endAt) query.set("end_at", params.endAt);
    if (params.limit) query.set("limit", String(params.limit));
    if (params.cursor) query.set("cursor", params.cursor);
    const suffix = query.toString();
    return request<{ subjects: ReportLibrarySubjectSummary[]; next_cursor?: string | null; total_count: number }>(
      `/report-library/subjects${suffix ? `?${suffix}` : ""}`,
    );
  },
  getReportLibrarySubject: (
    subjectKey: string,
    limit = 100,
    options: { includeTimeline?: boolean; includeSourceDocuments?: boolean } = {},
  ) => {
    const query = new URLSearchParams({ limit: String(limit) });
    if (typeof options.includeTimeline === "boolean") query.set("include_timeline", String(options.includeTimeline));
    if (typeof options.includeSourceDocuments === "boolean") query.set("include_source_documents", String(options.includeSourceDocuments));
    return (
    request<ReportLibrarySubject>(
      `/report-library/subjects/${encodeURIComponent(subjectKey)}?${query.toString()}`,
    ));
  },
  getReportLibrarySubjectReports: (
    subjectKey: string,
    params: { limit?: number; cursor?: string } = {},
  ) => {
    const query = new URLSearchParams();
    if (params.limit) query.set("limit", String(params.limit));
    if (params.cursor) query.set("cursor", params.cursor);
    const suffix = query.toString();
    return request<{ reports: ReportLibraryRecord[]; next_cursor?: string | null; total_count: number }>(
      `/report-library/subjects/${encodeURIComponent(subjectKey)}/reports${suffix ? `?${suffix}` : ""}`,
    );
  },
  getReportLibrarySubjectSources: (
    subjectKey: string,
    params: {
      sourceKind?: SourceKind;
      verificationStatus?: SourceVerificationStatus;
      usedByReport?: boolean;
      publisher?: string;
      publishedSince?: string;
      limit?: number;
      cursor?: string;
    } = {},
  ) => {
    const query = new URLSearchParams();
    if (params.sourceKind) query.set("source_kind", params.sourceKind);
    if (params.verificationStatus) query.set("verification_status", params.verificationStatus);
    if (typeof params.usedByReport === "boolean") query.set("used_by_report", String(params.usedByReport));
    if (params.publisher) query.set("publisher", params.publisher);
    if (params.publishedSince) query.set("published_since", params.publishedSince);
    if (params.limit) query.set("limit", String(params.limit));
    if (params.cursor) query.set("cursor", params.cursor);
    const suffix = query.toString();
    return request<{ subject_key: string; sources: CollectedSourceSummary[]; next_cursor?: string | null }>(
      `/report-library/subjects/${encodeURIComponent(subjectKey)}/sources${suffix ? `?${suffix}` : ""}`,
    );
  },
  getReportLibraryResearchNotes: (
    subjectKey: string,
    params: { status?: ResearchNote["derived_status"]; limit?: number; cursor?: string } | number = {},
  ) => {
    const normalized = typeof params === "number" ? { limit: params } : params;
    const query = new URLSearchParams();
    if (normalized.status) query.set("status", normalized.status);
    if (normalized.limit) query.set("limit", String(normalized.limit));
    if (normalized.cursor) query.set("cursor", normalized.cursor);
    return request<{
      subject_key: string;
      notes: ResearchNote[];
      counts: Record<ResearchNote["derived_status"], number>;
      total_count: number;
      next_cursor?: string | null;
    }>(`/report-library/subjects/${encodeURIComponent(subjectKey)}/research-notes?${query.toString()}`);
  },
  refreshReportLibrarySources: (subjectKey: string, force = true) =>
    request<Record<string, unknown>>(
      `/report-library/subjects/${encodeURIComponent(subjectKey)}/sources/refresh?force=${force ? "true" : "false"}`,
      { method: "POST" },
    ),
  getReportLibraryAnnualReportCoverage: (subjectKey: string, startYear: number, endYear: number) =>
    request<AnnualReportCoverage>(
      `/report-library/subjects/${encodeURIComponent(subjectKey)}/annual-reports/coverage?start_year=${encodeURIComponent(String(startYear))}&end_year=${encodeURIComponent(String(endYear))}`,
    ),
  backfillReportLibraryAnnualReports: (subjectKey: string, years: number[], force = false) =>
    request<AnnualReportBackfillResult>(
      `/report-library/subjects/${encodeURIComponent(subjectKey)}/annual-reports/backfill`,
      {
        method: "POST",
        body: JSON.stringify({ years, force }),
      },
    ),
  startReportLibraryAnnualReportBackfill: (subjectKey: string, years: number[], force = false) =>
    request<AnnualReportBackfillJobAccepted>(
      `/report-library/subjects/${encodeURIComponent(subjectKey)}/annual-reports/backfill-jobs`,
      {
        method: "POST",
        body: JSON.stringify({ years, force }),
      },
    ),
  getReportLibraryAnnualReportBackfillJob: (subjectKey: string, jobId: string) =>
    request<AnnualReportBackfillJob>(
      `/report-library/subjects/${encodeURIComponent(subjectKey)}/annual-reports/backfill-jobs/${encodeURIComponent(jobId)}`,
    ),
  getLatestReportLibraryAnnualReportBackfillJob: (subjectKey: string) =>
    request<{ job: AnnualReportBackfillJob | null }>(
      `/report-library/subjects/${encodeURIComponent(subjectKey)}/annual-reports/backfill-jobs/latest`,
    ),
  rebuildReportLibraryFinancialSnapshots: (subjectKey: string, force = true) =>
    request<Record<string, unknown>>(
      `/report-library/subjects/${encodeURIComponent(subjectKey)}/financial-snapshots/rebuild?force=${force ? "true" : "false"}`,
      { method: "POST" },
    ),
  refreshReportLibraryInstrumentProfile: (subjectKey: string) =>
    request<InstrumentProfileSnapshot>(
      `/report-library/subjects/${encodeURIComponent(subjectKey)}/instrument-profile/refresh`,
      { method: "POST" },
    ),
  refreshReportLibraryHistoricalPercentile: (subjectKey: string) =>
    request<InstrumentHistoricalPercentileSnapshot>(
      `/report-library/subjects/${encodeURIComponent(subjectKey)}/historical-percentile/refresh`,
      { method: "POST" },
    ),
  refreshReportLibraryETFProfile: (subjectKey: string) =>
    request<{
      status: "completed" | "completed_with_gaps";
      symbol: string;
      profile: ETFProductProfile;
      valuation_percentile?: ETFValuationPercentileSnapshot | null;
      sources: Record<string, string>;
      errors: unknown[];
    }>(
      `/report-library/subjects/${encodeURIComponent(subjectKey)}/etf-profile/refresh`,
      { method: "POST" },
    ),
  getReportLibraryItem: (reportId: string) =>
    request<ReportLibraryRecord>(`/report-library/reports/${encodeURIComponent(reportId)}`),
  getReportLibrarySources: (reportId: string, limit = 100) =>
    request<{ report_id: string; sources: ReportSourceLink[] }>(
      `/report-library/reports/${encodeURIComponent(reportId)}/sources?limit=${encodeURIComponent(String(limit))}`,
    ),
  compareReportLibrary: (
    items: Array<{ report_id: string; horizon: ReportHorizon }>,
    includeAiSummary = false,
  ) => request<ReportLibraryComparison>("/report-library/comparisons", {
    method: "POST",
    body: JSON.stringify({ items, include_ai_summary: includeAiSummary }),
  }),
  reconcileReportLibrary: () => request<Record<string, unknown>>("/report-library/reconcile", {
    method: "POST",
  }),
  reportLibraryArtifactUrl: (
    artifact: ReportLibraryArtifact,
    mode: "preview" | "download" = "preview",
  ) => artifact.url
    ? withAuthQuery(appendQueryParam(`${BASE}${artifact.url}`, "download", mode === "download" ? "1" : "0"))
    : "#",
  deepReportArtifactUrl: (
    reportId: string,
    artifactId: "markdown" | "pdf" | "diagnostic" | "diff" | "monitoring_bundle",
    mode: "preview" | "download" = "download",
  ) => withAuthQuery(appendQueryParam(
    `${BASE}/reports/${encodeURIComponent(reportId)}/artifacts/${artifactId}`,
    "download",
    mode === "download" ? "1" : "0",
  )),
  runReportArtifactUrl: (runId: string, mode: "preview" | "download" = "preview") =>
    withAuthQuery(appendQueryParam(
      `${BASE}/runs/${encodeURIComponent(runId)}/report-artifact`,
      "download",
      mode === "download" ? "1" : "0",
    )),
  sendReportArtifactToFeishu: (payload: {
    source: "report_library" | "deep_report" | "run";
    report_id: string;
    artifact_id: string;
    target_id?: string;
  }) => request<ReportArtifactDeliveryResult>("/reports/send-to-feishu", {
    method: "POST",
    body: JSON.stringify(payload),
  }),
  followUpDeepReport: (reportId: string, content?: string) =>
    request<{ message_id: string; attempt_id: string; parent_report_id: string }>(`/reports/${encodeURIComponent(reportId)}/followups`, {
      method: "POST",
      body: JSON.stringify({ content: content || "继续研究这份报告，并优先回答尚未解决的证据缺口。" }),
    }),
  resumeDeepReport: (reportId: string, content?: string) =>
    request<{ message_id: string; attempt_id: string; parent_report_id: string }>(`/reports/${encodeURIComponent(reportId)}/resume`, {
      method: "POST",
      body: JSON.stringify({ content: content || "继续完善这份穿透式深度研究报告。" }),
    }),
  refreshDeepReport: (reportId: string, instructions?: string) =>
    request<{ message_id: string; attempt_id: string; parent_report_id: string; revision_mode: "full_refresh" }>(`/reports/${encodeURIComponent(reportId)}/refresh`, {
      method: "POST",
      body: JSON.stringify({ instructions: instructions || "使用最新可验证数据重新研究。" }),
    }),
  enrichDeepReport: (reportId: string, instructions?: string) =>
    request<{
      message_id: string;
      attempt_id: string;
      parent_report_id: string;
      revision_mode: "full_refresh";
      research_depth: "extended";
      token_notice_acknowledged: true;
    }>(`/reports/${encodeURIComponent(reportId)}/refresh`, {
      method: "POST",
      body: JSON.stringify({
        instructions: instructions || "用户已明确同意扩展资料搜集，并知悉这会增加研究耗时和 Token 消耗；请优先补齐当前报告中缺少的往年数据、可比期间和关键来源。",
        research_depth: "extended",
        consent_to_extended_research: true,
      }),
    }),
  reviseDeepReport: (reportId: string, sectionIds: string[], instructions: string) =>
    request<{ message_id: string; attempt_id: string; parent_report_id: string }>(`/reports/${encodeURIComponent(reportId)}/revisions`, {
      method: "POST",
      body: JSON.stringify({ section_ids: sectionIds, instructions }),
    }),
  repairDeepReport: (reportId: string, instructions?: string) =>
    request<{ message_id: string; attempt_id: string; parent_report_id: string; revision_mode: "repair" }>(`/reports/${encodeURIComponent(reportId)}/repair`, {
      method: "POST",
      body: JSON.stringify({
        instructions: instructions || "使用现有 FinancialSnapshot、Fact 和 Evidence 修复未通过校验的章节。",
      }),
    }),
  archiveDeepReport: (reportId: string) =>
    request<{ status: string; path: string; bytes_written: number }>(`/reports/${encodeURIComponent(reportId)}/archive`, {
      method: "POST",
    }),
  createGoal: (sid: string, body: CreateGoalRequest) =>
    request<GoalSnapshot>(`/sessions/${sid}/goal`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getGoal: (sid: string) => request<GoalSnapshot>(`/sessions/${sid}/goal`),
  updateGoal: (sid: string, body: UpdateGoalRequest) =>
    request<UpdateGoalResponse>(`/sessions/${sid}/goal`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  addGoalEvidence: (sid: string, body: AddGoalEvidenceRequest) =>
    request<AddGoalEvidenceResponse>(`/sessions/${sid}/goal/evidence`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateGoalStatus: (sid: string, body: UpdateGoalStatusRequest) =>
    request<UpdateGoalStatusResponse>(`/sessions/${sid}/goal/status`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  sseUrl: (sid: string, options?: { replay?: "active" }) => {
    let url = withAuthQuery(`${BASE}/sessions/${sid}/events`);
    if (options?.replay) url = appendQueryParam(url, "replay", options.replay);
    return url;
  },

  // Swarm API
  listSwarmPresets: () => request<SwarmPreset[]>("/swarm/presets"),
  createSwarmRun: (preset_name: string, user_vars: Record<string, string>) =>
    request<{ id: string; status: string }>("/swarm/runs", {
      method: "POST",
      body: JSON.stringify({ preset_name, user_vars }),
    }),
  listSwarmRuns: () => request<SwarmRunSummary[]>("/swarm/runs"),
  getSwarmRun: (id: string) => request<Record<string, unknown>>(`/swarm/runs/${id}`),
  swarmSseUrl: (id: string) => withAuthQuery(`${BASE}/swarm/runs/${id}/events`),
  cancelSwarmRun: (id: string) =>
    request<{ status: string }>(`/swarm/runs/${id}/cancel`, { method: "POST" }),
  retrySwarmRun: (id: string) =>
    request<{ id: string; status: string; preset_name: string }>(`/swarm/runs/${id}/retry`, { method: "POST" }),
  getLLMSettings: () => request<LLMSettings>("/settings/llm"),
  getLLMModels: (provider: string) =>
    request<LLMModelsResponse>(`/settings/llm/models?provider=${encodeURIComponent(provider)}`),
  updateLLMSettings: (settings: UpdateLLMSettingsRequest) =>
    request<LLMSettings>("/settings/llm", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),
  getDataSourceSettings: () => request<DataSourceSettings>("/settings/data-sources"),
  updateDataSourceSettings: (settings: UpdateDataSourceSettingsRequest) =>
    request<DataSourceSettings>("/settings/data-sources", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),
  getResearchSettings: () => request<ResearchSettings>("/settings/research"),
  updateResearchSettings: (settings: UpdateResearchSettingsRequest) =>
    request<ResearchSettings>("/settings/research", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),
  getFeishuDeliverySettings: () =>
    request<FeishuDeliverySettings>("/settings/feishu-delivery"),
  updateFeishuDeliverySettings: (defaultTargetId?: string | null) =>
    request<FeishuDeliverySettings>("/settings/feishu-delivery", {
      method: "PUT",
      body: JSON.stringify({ default_target_id: defaultTargetId || null }),
    }),
  createFeishuDeliveryBindingCode: () =>
    request<MonitorDeliveryBindingAttempt>("/settings/feishu-delivery/binding-codes", {
      method: "POST",
    }),
  getFeishuDeliveryBindingCode: (bindingId: string) =>
    request<MonitorDeliveryBindingAttempt>(
      `/settings/feishu-delivery/binding-codes/${encodeURIComponent(bindingId)}`,
    ),
  revokeFeishuDeliveryTarget: (targetId: string) =>
    request<MonitorDeliveryTarget>(
      `/settings/feishu-delivery/targets/${encodeURIComponent(targetId)}/revoke`,
      { method: "POST" },
    ),
  getCodexCliStatus: () => request<CodexCliStatus>("/settings/codex-cli/status"),
  openCodexCliLogin: () => request<CodexCliLoginResult>("/settings/codex-cli/login", {
    method: "POST",
  }),
  getChannelStatus: () => request<ChannelRuntimeStatus>("/channels/status"),
  startChannels: () => request<ChannelRuntimeActionResponse>("/channels/start", { method: "POST" }),
  stopChannels: () => request<ChannelRuntimeActionResponse>("/channels/stop", { method: "POST" }),
  runChannelPairingCommand: (body: ChannelPairingCommandRequest) =>
    request<ChannelPairingCommandResponse>("/channels/pairing/command", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getPortfolioReview: (limit?: number) =>
    request<PortfolioReview>(`/portfolio/review${limit ? `?limit=${encodeURIComponent(String(limit))}` : ""}`),
  updatePortfolioCash: (body: UpdatePortfolioCashRequest) =>
    request<PortfolioReview>("/portfolio/cash", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  getPortfolioMandate: () => request<PortfolioMandate>("/portfolio/mandate"),
  updatePortfolioMandate: (mandate: PortfolioMandate) =>
    request<PortfolioMandate>("/portfolio/mandate", {
      method: "PUT",
      body: JSON.stringify({ mandate }),
    }),
  updatePortfolioAssignment: (symbol: string, sleeveId: string) =>
    request<PortfolioMandate>(`/portfolio/mandate/assignments/${encodeURIComponent(symbol)}`, {
      method: "PUT",
      body: JSON.stringify({ sleeve_id: sleeveId, user_locked: true }),
    }),
  listPortfolioDailyRuns: (limit = 30) =>
    request<{ runs: PortfolioDailyRun[] }>(`/portfolio/daily-runs?limit=${encodeURIComponent(String(limit))}`),
  startPortfolioDailyRun: (body: StartPortfolioDailyRunRequest = {}) =>
    request<PortfolioDailyRun>("/portfolio/daily-runs", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getPortfolioDailyRun: (runId: string) =>
    request<PortfolioDailyRun>(`/portfolio/daily-runs/${encodeURIComponent(runId)}`),
  cancelPortfolioDailyRun: (runId: string) =>
    request<PortfolioDailyRun>(`/portfolio/daily-runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" }),
  retryPortfolioDailyRun: (runId: string, symbol?: string) =>
    request<PortfolioDailyRun>(`/portfolio/daily-runs/${encodeURIComponent(runId)}/retry`, {
      method: "POST",
      body: JSON.stringify(symbol ? { symbol } : {}),
    }),
  listPortfolioWeeklyRuns: (limit = 30) =>
    request<{ runs: PortfolioWeeklyRun[] }>(`/portfolio/weekly-runs?limit=${encodeURIComponent(String(limit))}`),
  startPortfolioWeeklyRuns: (body: StartPortfolioWeeklyRunsRequest = {}) =>
    request<{ runs: PortfolioWeeklyRun[] }>("/portfolio/weekly-runs", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getPortfolioWeeklyRun: (runId: string) =>
    request<PortfolioWeeklyRun>(`/portfolio/weekly-runs/${encodeURIComponent(runId)}`),
  cancelPortfolioWeeklyRun: (runId: string) =>
    request<PortfolioWeeklyRun>(`/portfolio/weekly-runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" }),
  retryPortfolioWeeklyRun: (runId: string) =>
    request<PortfolioWeeklyRun>(`/portfolio/weekly-runs/${encodeURIComponent(runId)}/retry`, { method: "POST" }),
  listPortfolioMonitorDeliveryTargets: () =>
    request<{ targets: MonitorDeliveryTarget[] }>("/portfolio/monitor-delivery-targets"),
  bindPortfolioMonitorDeliveryTarget: (body: BindMonitorDeliveryTargetRequest) =>
    request<MonitorDeliveryTarget>("/portfolio/monitor-delivery-targets/bind", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  createPortfolioMonitorDeliveryBindingCode: () =>
    request<MonitorDeliveryBindingAttempt>("/portfolio/monitor-delivery-targets/binding-codes", {
      method: "POST",
    }),
  getPortfolioMonitorDeliveryBindingCode: (bindingId: string) =>
    request<MonitorDeliveryBindingAttempt>(
      `/portfolio/monitor-delivery-targets/binding-codes/${encodeURIComponent(bindingId)}`,
    ),
  createPortfolioMonitorDraftBatch: (body: CreateMonitorDraftBatchRequest) =>
    request<MonitorDraftBatch>("/portfolio/monitor-draft-batches", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getPortfolioMonitorDraftBatch: (batchId: string) =>
    request<MonitorDraftBatch>(`/portfolio/monitor-draft-batches/${encodeURIComponent(batchId)}`),
  listPortfolioMonitorReportCandidates: (symbol: string) =>
    request<{ symbol: string; candidates: MonitorReportCandidate[] }>(
      `/portfolio/monitor-report-candidates?symbol=${encodeURIComponent(symbol)}`,
    ),
  createPortfolioMonitorPlannerJob: (body: CreateMonitorPlannerJobRequest) =>
    request<MonitorPlannerJob>("/portfolio/monitor-planner-jobs", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getPortfolioMonitorPlannerJob: (jobId: string) =>
    request<MonitorPlannerJob>(`/portfolio/monitor-planner-jobs/${encodeURIComponent(jobId)}`),
  cancelPortfolioMonitorPlannerJob: (jobId: string) =>
    request<MonitorPlannerJob>(`/portfolio/monitor-planner-jobs/${encodeURIComponent(jobId)}/cancel`, {
      method: "POST",
    }),
  retryPortfolioMonitorPlannerItem: (jobId: string, symbol: string) =>
    request<MonitorPlannerJob>(
      `/portfolio/monitor-planner-jobs/${encodeURIComponent(jobId)}/items/${encodeURIComponent(symbol)}/retry`,
      { method: "POST" },
    ),
  listPortfolioMonitors: () => request<{ profiles: MonitorProfile[] }>("/portfolio/monitors"),
  getPortfolioMonitor: (profileId: string) =>
    request<MonitorProfile>(`/portfolio/monitors/${encodeURIComponent(profileId)}`),
  getPortfolioMonitorPlan: (profileId: string, version: number) =>
    request<MonitorPlanVersion>(`/portfolio/monitors/${encodeURIComponent(profileId)}/plans/${version}`),
  updatePortfolioMonitorPlan: (profileId: string, version: number, plan: MonitorPlan, expectedRevision: number) =>
    request<MonitorPlanVersion>(`/portfolio/monitors/${encodeURIComponent(profileId)}/plans/${version}`, {
      method: "PATCH",
      headers: { "If-Match": String(expectedRevision) },
      body: JSON.stringify({ plan, expected_revision: expectedRevision }),
    }),
  saveAndActivatePortfolioMonitorPlan: (
    profileId: string,
    version: number,
    plan: MonitorPlan,
    expectedRevision: number,
  ) =>
    request<MonitorProfile>(
      `/portfolio/monitors/${encodeURIComponent(profileId)}/plans/${version}/save-and-activate`,
      {
        method: "POST",
        headers: { "If-Match": String(expectedRevision) },
        body: JSON.stringify({ plan, expected_revision: expectedRevision }),
      },
    ),
  activatePortfolioMonitorPlan: (profileId: string, version: number) =>
    request<MonitorProfile>(`/portfolio/monitors/${encodeURIComponent(profileId)}/plans/${version}/activate`, {
      method: "POST",
    }),
  reanalyzePortfolioMonitor: (profileId: string, allowSingleSource = false) =>
    request<MonitorDraftBatch>(`/portfolio/monitors/${encodeURIComponent(profileId)}/reanalyze`, {
      method: "POST",
      body: JSON.stringify({ allow_single_source: allowSingleSource }),
    }),
  reopenPortfolioMonitor: (profileId: string, deliveryTargetId: string, allowSingleSource = false) =>
    request<MonitorDraftBatch>(`/portfolio/monitors/${encodeURIComponent(profileId)}/reopen`, {
      method: "POST",
      body: JSON.stringify({
        delivery_target_id: deliveryTargetId,
        allow_single_source: allowSingleSource,
      }),
    }),
  pausePortfolioMonitor: (profileId: string, durationHours?: number) =>
    request<MonitorProfile>(`/portfolio/monitors/${encodeURIComponent(profileId)}/pause`, {
      method: "POST",
      body: JSON.stringify(durationHours ? { duration_hours: durationHours } : {}),
    }),
  resumePortfolioMonitor: (profileId: string) =>
    request<MonitorProfile>(`/portfolio/monitors/${encodeURIComponent(profileId)}/resume`, { method: "POST" }),
  closePortfolioMonitor: (profileId: string) =>
    request<MonitorProfile>(`/portfolio/monitors/${encodeURIComponent(profileId)}/close`, { method: "POST" }),
  listPortfolioMonitorEvents: (limit = 50, symbol?: string) =>
    request<{ events: MonitorEvent[] }>(
      `/portfolio/monitor-events?limit=${limit}${symbol ? `&symbol=${encodeURIComponent(symbol)}` : ""}`,
    ),
  acknowledgePortfolioMonitorEvent: (eventId: string) =>
    request<MonitorEvent>(`/portfolio/monitor-events/${encodeURIComponent(eventId)}/acknowledge`, { method: "POST" }),
  portfolioMonitorEventsSseUrl: () => withAuthQuery(`${BASE}/portfolio/monitor-events/stream`),
  getPortfolioMonitorYmcaAudio: () => requestBlob("/portfolio/monitor-effects/ymca_v1/audio"),
  getPortfolioMonitoringStatus: () => request<PortfolioMonitoringStatus>("/portfolio/monitoring/status"),
  listPortfolioMonitoringTargets: () =>
    request<{ targets: MonitorTargetMonitoringCard[] }>("/portfolio/monitoring/targets"),
  getPortfolioMonitoringDecision: (symbol: string) =>
    request<MonitorTargetMonitoringCard>(
      `/portfolio/monitoring/targets/${encodeURIComponent(symbol)}/decision`,
    ),
  setPortfolioMonitoringRiskPreference: (symbol: string, body: MonitorRiskPreferenceInput) =>
    request<MonitorRiskPreference>(
      `/portfolio/monitoring/risk-preferences/${encodeURIComponent(symbol)}`,
      { method: "PUT", body: JSON.stringify(body) },
    ),
  choosePortfolioMonitoringDecision: (
    decisionId: string,
    choiceId: string,
    body: MonitorDecisionChoiceRequest,
  ) => request<Record<string, unknown>>(
    `/portfolio/monitoring/decisions/${encodeURIComponent(decisionId)}/choices/${encodeURIComponent(choiceId)}`,
    { method: "POST", body: JSON.stringify(body) },
  ),
  createPortfolioMonitoringConditionDraft: (
    decisionId: string,
    body: MonitorConditionDraftRequest,
  ) => request<MonitorConditionOrderDraft>(
    `/portfolio/monitoring/decisions/${encodeURIComponent(decisionId)}/condition-order-drafts`,
    { method: "POST", body: JSON.stringify(body) },
  ),
  validatePortfolioMonitoringConditionDraft: (draftId: string) =>
    request<MonitorConditionOrderDraft>(
      `/portfolio/monitoring/condition-order-drafts/${encodeURIComponent(draftId)}/validate`,
      { method: "POST" },
    ),
  cancelPortfolioMonitoringConditionDraft: (draftId: string) =>
    request<MonitorConditionOrderDraft>(
      `/portfolio/monitoring/condition-order-drafts/${encodeURIComponent(draftId)}/cancel`,
      { method: "POST" },
    ),
  getPortfolioMonitoringAutopilot: () =>
    request<MonitorAutopilotConfig>("/portfolio/monitoring/autopilot"),
  configurePortfolioMonitoringAutopilot: (body: MonitorAutopilotUpdate) =>
    request<MonitorAutopilotConfig>("/portfolio/monitoring/autopilot", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  listPortfolioMonitoringAutopilotRuns: (limit = 100) =>
    request<{ runs: MonitorAutopilotRun[] }>(
      `/portfolio/monitoring/autopilot/runs?limit=${encodeURIComponent(String(limit))}`,
    ),
  getPortfolioMonitoringUsage: (period: MonitorUsagePeriod = "today") =>
    request<MonitorUsageSummary>(
      `/portfolio/monitoring/usage?period=${encodeURIComponent(period)}`,
    ),
  getPortfolioMonitoringUsageEvents: (
    period: MonitorUsagePeriod = "today",
    filters: UsageEventFilters = {},
  ) => {
    const query = new URLSearchParams({ period });
    if (filters.kind) query.set("kind", filters.kind);
    if (filters.category) query.set("category", filters.category);
    if (filters.cursor) query.set("cursor", filters.cursor);
    if (filters.limit) query.set("limit", String(filters.limit));
    return request<UsageEventsPage>(`/portfolio/monitoring/usage/events?${query.toString()}`);
  },
  getPortfolioMonitorPlannerJobUsage: (jobId: string) =>
    request<MonitorJobUsageSummary>(
      `/portfolio/monitor-planner-jobs/${encodeURIComponent(jobId)}/usage`,
    ),
  getPortfolioMonitorPlannerJobUsageEvents: (
    jobId: string,
    filters: UsageEventFilters = {},
  ) => {
    const query = new URLSearchParams();
    if (filters.kind) query.set("kind", filters.kind);
    if (filters.category) query.set("category", filters.category);
    if (filters.attemptId) query.set("attempt_id", filters.attemptId);
    if (filters.cursor) query.set("cursor", filters.cursor);
    if (filters.limit) query.set("limit", String(filters.limit));
    const suffix = query.toString();
    return request<UsageEventsPage>(
      `/portfolio/monitor-planner-jobs/${encodeURIComponent(jobId)}/usage/events${suffix ? `?${suffix}` : ""}`,
    );
  },
  listPortfolioMonitorRecommendations: (limit = 100) =>
    request<{ recommendations: MonitorRecommendation[] }>(
      `/portfolio/monitor-recommendations?limit=${encodeURIComponent(String(limit))}`,
    ),
  acknowledgePortfolioMonitorRecommendation: (
    recommendationId: string,
    feedbackStatus: MonitorRecommendationFeedback,
  ) => request<MonitorRecommendation>(
    `/portfolio/monitor-recommendations/${encodeURIComponent(recommendationId)}/acknowledge`,
    {
      method: "POST",
      body: JSON.stringify({ feedback_status: feedbackStatus }),
    },
  ),
  configurePortfolioMonitoringRuntime: (enabled: boolean, mode?: "shadow" | "deliver") =>
    request<PortfolioMonitoringStatus>("/admin/portfolio/monitoring/config", {
      method: "PUT",
      body: JSON.stringify({ enabled, ...(mode ? { mode } : {}) }),
    }),
  startPortfolioMonitoringRuntime: () =>
    request<PortfolioMonitoringStatus>("/admin/portfolio/monitoring/start", { method: "POST" }),
  portfolioDailyRunArtifactUrl: (runId: string, artifactId: string) =>
    withAuthQuery(`${BASE}/portfolio/daily-runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}`),
  lookupPortfolioSecurity: (code: string, signal?: AbortSignal) =>
    request<PortfolioSecurityLookup>(`/portfolio/security-lookup?code=${encodeURIComponent(code)}`, { signal }),
  updatePortfolioHoldings: (body: UpdatePortfolioHoldingsRequest) =>
    request<PortfolioReview>("/portfolio/holdings", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  editPortfolioHolding: (symbol: string, body: EditPortfolioHoldingRequest) =>
    request<PortfolioReview>(`/portfolio/holdings/${encodeURIComponent(symbol)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  recordPortfolioTrade: (body: RecordPortfolioTradeRequest) =>
    request<PortfolioReview>("/portfolio/trades", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deletePortfolioTrade: (tradeId: string) =>
    request<PortfolioReview>(`/portfolio/trades/${encodeURIComponent(tradeId)}`, { method: "DELETE" }),
  reversePortfolioTrade: (tradeId: string) =>
    request<PortfolioReview>(`/portfolio/trades/${encodeURIComponent(tradeId)}/reversal`, { method: "POST" }),
  previewPortfolioReconciliation: (body: PortfolioReconciliationPreviewRequest) =>
    request<PortfolioReconciliation>("/portfolio/reconciliation/preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  commitPortfolioReconciliation: (reconciliationId: string, expectedRevision: number) =>
    request<PortfolioReconciliationCommit>(
      `/portfolio/reconciliation/${encodeURIComponent(reconciliationId)}/commit`,
      { method: "POST", body: JSON.stringify({ expected_revision: expectedRevision }) },
    ),
  refreshPortfolioMarketData: (body: PortfolioMarketRefreshRequest = {}) =>
    request<PortfolioReview>("/portfolio/refresh-market-data", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  startPortfolioAnalysis: (body: PortfolioAnalysisSessionRequest) =>
    request<PortfolioAnalysisSession>("/portfolio/analysis-sessions", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getPortfolioAnalysis: (analysisId: string) =>
    request<PortfolioAnalysisSession>(`/portfolio/analysis-sessions/${encodeURIComponent(analysisId)}`),
  startMarketCacheRefresh: (body: MarketCacheRefreshRequest = {}) =>
    request<MarketCacheRefreshAccepted>("/market-cache/refresh", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getMarketCacheRun: (runId: string) =>
    request<MarketCacheRun>(`/market-cache/runs/${encodeURIComponent(runId)}`),
  getMarketCacheQuotes: (symbols?: string[]) =>
    request<{ status: string; quotes: MarketCacheQuote[] }>(
      `/market-cache/quotes${symbols?.length ? `?symbols=${encodeURIComponent(symbols.join(","))}` : ""}`,
    ),
  getMarketCacheCoverage: (symbols?: string[]) =>
    request<{ status: string; coverage: MarketCacheCoverage[] }>(
      `/market-cache/coverage${symbols?.length ? `?symbols=${encodeURIComponent(symbols.join(","))}` : ""}`,
    ),
  getMarketCacheBars: (params: MarketCacheBarsParams) => {
    const query = new URLSearchParams({
      symbol: params.symbol,
      interval: params.interval,
      adjustment: params.adjustment,
      view: params.view || "consensus",
      limit: String(params.limit || 20000),
    });
    if (params.start_date) query.set("start_date", params.start_date);
    if (params.end_date) query.set("end_date", params.end_date);
    return request<MarketCacheBarsResponse>(`/market-cache/bars?${query.toString()}`);
  },

  // Alpha Zoo API
  listAlphas: (params: AlphaListParams = {}) => {
    const q = new URLSearchParams();
    if (params.zoo) q.set("zoo", params.zoo);
    if (params.theme) q.set("theme", params.theme);
    if (params.universe) q.set("universe", params.universe);
    if (params.limit !== undefined) q.set("limit", String(params.limit));
    const qs = q.toString();
    return request<AlphaListResponse>(`/alpha/list${qs ? `?${qs}` : ""}`);
  },
  getAlpha: (alphaId: string) =>
    request<AlphaDetailResponse>(`/alpha/${encodeURIComponent(alphaId)}`),
  createAlphaBench: (body: AlphaBenchRequest) =>
    request<{ status: string; job_id: string }>("/alpha/bench", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  alphaBenchStreamUrl: (jobId: string) =>
    withAuthQuery(`${BASE}/alpha/bench/${encodeURIComponent(jobId)}/stream`),
  createAlphaCompare: (body: AlphaCompareRequest) =>
    request<{ status: string; job_id: string }>("/alpha/compare", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  alphaCompareStreamUrl: (jobId: string) =>
    withAuthQuery(`${BASE}/alpha/compare/${encodeURIComponent(jobId)}/stream`),

  // Connector runtime channel — privileged surface actions (NOT agent tools).
  // commit is the ONLY action that writes a mandate; halt trips the kill switch.
  commitMandate: (body: CommitMandateRequest) =>
    request<CommitMandateResponse>("/mandate/commit", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  haltLive: (session_id?: string, broker?: string, reason?: string) =>
    request<HaltLiveResponse>("/live/halt", {
      method: "POST",
      body: JSON.stringify({ session_id, broker, reason }),
    }),
  // Read the persistent runtime status across all authorized brokers (SPEC §7.5).
  // Polled by the RunnerStatus panel; a plain authenticated GET, never a chat message.
  getLiveStatus: () => request<LiveStatus>("/live/status"),
  authorizeLive: (broker: string) =>
    request<LiveAuthorizeResponse>("/live/authorize", {
      method: "POST",
      body: JSON.stringify({ broker }),
    }),
  // Start/stop the persistent runner (SPEC §7.5). Privileged surface actions, not agent tools.
  startLiveRunner: (broker: string) =>
    request<LiveRunnerResponse>("/live/runner/start", {
      method: "POST",
      body: JSON.stringify({ broker }),
    }),
  stopLiveRunner: (broker: string) =>
    request<LiveRunnerResponse>("/live/runner/stop", {
      method: "POST",
      body: JSON.stringify({ broker }),
    }),
};

// --- Swarm types ---

export interface SwarmPreset {
  name: string;
  title: string;
  description: string;
  agent_count: number;
  variables: { name: string; description: string; required: boolean }[];
}

export interface SwarmRunSummary {
  id: string;
  preset_name: string;
  status: string;
  created_at: string;
  task_count: number;
  completed_count: number;
}

export interface LLMProviderOption {
  name: string;
  label: string;
  api_key_env?: string | null;
  base_url_env: string;
  default_model: string;
  default_base_url: string;
  api_key_required: boolean;
  auth_type?: string;
  login_command?: string | null;
  model_discovery?: "codex_oauth" | null;
  models?: LLMModelOption[];
}

export interface LLMModelOption {
  id: string;
  label: string;
  description?: string;
  default_reasoning_effort?: string;
  reasoning_efforts?: string[];
}

export interface LLMModelsResponse {
  provider: string;
  models: LLMModelOption[];
  source: "remote" | "configured";
  refreshed_at?: string | null;
  warning?: string | null;
}

export interface LLMSettings {
  provider: string;
  model_name: string;
  base_url: string;
  api_key_env?: string | null;
  api_key_configured: boolean;
  api_key_hint?: string | null;
  api_key_required: boolean;
  temperature: number;
  timeout_seconds: number;
  max_retries: number;
  reasoning_effort: string;
  sse_timeout_seconds: number;
  env_path: string;
  providers: LLMProviderOption[];
}

export interface UpdateLLMSettingsRequest {
  provider: string;
  model_name: string;
  base_url: string;
  api_key?: string;
  clear_api_key?: boolean;
  temperature: number;
  timeout_seconds: number;
  max_retries: number;
  reasoning_effort?: string;
}

export interface DataSourceSettings {
  tushare_token_configured: boolean;
  tushare_token_hint?: string | null;
  baostock_supported: boolean;
  baostock_installed: boolean;
  baostock_message: string;
  env_path: string;
}

export interface UpdateDataSourceSettingsRequest {
  tushare_token?: string;
  clear_tushare_token?: boolean;
}

export interface ResearchSettings {
  deep_report_enabled: boolean;
  equity_deep_research_enabled: boolean;
  etf_deep_research_enabled: boolean;
  monitor_auto_deep_report_enabled: boolean;
  effective_monitor_auto_deep_report_enabled: boolean;
  deep_research_engine: "provider" | "codex_cli";
  codex_cli_enabled: boolean;
  codex_cli_ready: boolean;
  effective_codex_cli_enabled: boolean;
  codex_cli_model: string;
  codex_cli_reasoning_effort: string;
  enabled_profiles: string[];
  available_profiles: string[];
  env_path: string;
}

export interface UpdateResearchSettingsRequest {
  deep_report_enabled?: boolean;
  monitor_auto_deep_report_enabled?: boolean;
  deep_research_engine?: "provider" | "codex_cli";
  /** @deprecated Use deep_research_engine. */
  codex_cli_enabled?: boolean;
  codex_cli_model?: string;
  codex_cli_reasoning_effort?: string;
}

export interface CodexCliStatus {
  installed: boolean;
  version?: string | null;
  latest_version?: string | null;
  minimum_version: string;
  version_supported: boolean;
  auth_state: "authenticated" | "unauthenticated" | "error" | "unavailable";
  ready: boolean;
  environment: "native" | "container" | "remote";
  command_shell: "powershell" | "terminal";
  can_launch_login: boolean;
  login_command: string;
  install_command: string;
  message: string;
}

export interface CodexCliLoginResult {
  launched: boolean;
  manual_required: boolean;
  command: string;
  message: string;
}

export interface ChannelAdapterStatus {
  name: string;
  display_name: string;
  configured: boolean;
  enabled: boolean;
  available: boolean;
  loaded: boolean;
  running: boolean;
  error?: string;
  install_hint?: string;
}

export interface ChannelRuntimeStatus {
  running: boolean;
  inbound_queue: number;
  outbound_queue: number;
  session_count: number;
  channels: Record<string, ChannelAdapterStatus>;
}

export interface ChannelRuntimeActionResponse extends ChannelRuntimeStatus {
  status: string;
}

export interface ChannelPairingCommandRequest {
  channel: string;
  command: string;
}

export interface ChannelPairingCommandResponse {
  channel: string;
  reply: string;
}

export interface UpdatePortfolioHoldingsRequest {
  raw_text: string;
  cash?: number | null;
  cash_currency?: string;
}

export interface UpdatePortfolioCashRequest {
  cash: number;
  cash_currency?: string;
}

export interface RecordPortfolioTradeRequest {
  code: string;
  symbol: string;
  name: string;
  side: "buy" | "sell";
  quantity: number;
  price: number;
  trade_date?: string;
  fees?: number;
  taxes?: number;
  broker_reported_pnl?: number;
  idempotency_key?: string;
  notes?: string;
}

export interface PortfolioReconciliationPreviewRequest {
  raw_text?: string;
  holdings?: Array<Record<string, unknown>>;
  trades?: Array<Record<string, unknown>>;
  cash?: number | null;
  cash_currency?: string;
  broker_reported_pnl?: number | null;
  source_label?: string;
}

export interface PortfolioReconciliationDiff {
  symbol: string;
  status: "added" | "removed" | "changed" | string;
  changes: Record<string, { current?: number | null; broker?: number | null }>;
}

export interface PortfolioReconciliationPreview {
  base_revision: number;
  holding_diffs: PortfolioReconciliationDiff[];
  missing_ledger_event_ids: string[];
  extra_ledger_event_ids: string[];
  suspicious_events: Array<Record<string, unknown>>;
  broker_reported_pnl?: number | null;
  computed_realized_pnl?: number | null;
  unexplained_pnl?: number | null;
  pnl_status: string;
  requires_explicit_commit: boolean;
  target_state: PortfolioStateSnapshot;
}

export interface PortfolioReconciliation {
  reconciliation_id: string;
  status: string;
  base_revision: number;
  request: PortfolioReconciliationPreviewRequest;
  preview: PortfolioReconciliationPreview;
  created_at: string;
  committed_at?: string | null;
}

export interface PortfolioReconciliationCommit extends PortfolioReconciliation {
  state: PortfolioStateSnapshot;
  deduplicated: boolean;
}

export interface EditPortfolioHoldingRequest {
  quantity?: number;
  cost_price?: number;
}

export interface PortfolioSecurityLookup {
  code: string;
  symbol: string;
  name: string;
  market: string;
  source: string;
}

export interface PortfolioMarketRefreshRequest {
  start_date?: string;
  end_date?: string;
  sources?: string[];
  adjustment?: "source_default" | "raw" | "qfq" | "hfq" | "unknown";
  tolerance_pct?: number;
  max_rows?: number;
}

export interface MarketCacheRefreshRequest {
  symbols?: string[];
  profile?: string;
  sources?: string[];
  force?: boolean;
  start_date?: string;
  end_date?: string;
}

export interface MarketCacheRefreshItem {
  id: number;
  run_id: string;
  symbol: string;
  interval: "1m" | "5m" | "1D" | string;
  adjustment: "raw" | "qfq" | string;
  status: string;
  requested_sources: string[];
  actual_sources: string[];
  attempts?: Array<{
    requested_source: string;
    actual_source?: string | null;
    upstream_source?: string | null;
    status: string;
    error_category?: string | null;
    error?: string | null;
    latest_bar_time?: string | null;
    latest_close?: number | null;
    latency_ms?: number | null;
  }>;
  rows_written: number;
  message?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface MarketCacheRun {
  run_id: string;
  profile: string;
  status: string;
  symbols: string[];
  config: Record<string, unknown>;
  total_items: number;
  completed_items: number;
  conflict_items: number;
  failed_items: number;
  current_symbol?: string | null;
  current_source?: string | null;
  progress_pct: number;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  error?: string | null;
  items: MarketCacheRefreshItem[];
}

export interface MarketCacheRefreshAccepted {
  status: string;
  run_id: string;
  deduplicated: boolean;
  run: MarketCacheRun;
}

export interface MarketCacheCoverage {
  symbol: string;
  actual_source: string;
  interval: string;
  actual_adjustment: string;
  min_bar_time: string;
  max_bar_time: string;
  row_count: number;
  last_success_at: string;
}

export interface MarketCacheQuote {
  symbol: string;
  interval: string;
  bar_time: string;
  adjustment: string;
  last_price?: number | null;
  volume?: number | null;
  amount?: number | null;
  vwap?: number | null;
  status: string;
  sources: string[];
  verified_at: string;
}

export interface MarketCacheBarsParams {
  symbol: string;
  interval: "1m" | "5m" | "1D" | string;
  adjustment: "raw" | "qfq" | string;
  view?: "consensus" | "source";
  start_date?: string;
  end_date?: string;
  limit?: number;
}

export interface MarketCacheBar {
  symbol: string;
  interval: string;
  bar_time: string;
  session_date?: string;
  adjustment?: string;
  actual_adjustment?: string;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  close?: number | null;
  volume?: number | null;
  amount?: number | null;
  vwap?: number | null;
  status?: string;
  source_count?: number;
  sources?: string[];
  verified_at?: string;
  batch_id?: string;
  quality_flags?: string[];
}

export interface MarketCacheBarsResponse {
  status: string;
  symbol: string;
  interval: string;
  adjustment: string;
  view: "consensus" | "source";
  bars: MarketCacheBar[];
}

export interface PortfolioHolding {
  name?: string;
  code?: string;
  symbol?: string;
  quantity?: number | null;
  cost_price?: number | null;
  last_price?: number | null;
  market_value?: number | null;
  pnl?: number | null;
  pnl_pct?: number | null;
  updated_at?: string;
  [key: string]: unknown;
}

export interface PortfolioStateSnapshot {
  holdings: PortfolioHolding[];
  recent_trades: PortfolioTrade[];
  cash?: number | null;
  cash_currency?: string;
  updated_at?: string | null;
  schema_version?: number;
  revision?: number;
  provenance?: Record<string, unknown>;
  performance?: {
    realized_pnl?: number | null;
    cash_dividends?: number | null;
    fees_and_taxes?: number | null;
    broker_reported_pnl?: number | null;
    status?: "broker_reported" | "exact" | "estimated" | "unavailable" | string;
    unexplained_difference?: number | null;
  };
}

export interface PortfolioTrade extends Record<string, unknown> {
  trade_id: string;
  code?: string;
  symbol?: string;
  name?: string;
  side?: string;
  quantity?: number | null;
  price?: number | null;
  trade_date?: string;
  notes?: string;
  recorded_at?: string;
}

export interface VerifiedMarketCacheRow {
  file_name: string;
  path: string;
  symbol?: string;
  status?: string;
  consensus_close?: number | null;
  spread_pct?: number | null;
  requested_adjustment?: string | null;
  actual_adjustment?: string | null;
  source_adjustments?: Record<string, { adjustment?: string; confidence?: string; note?: string }>;
  sources?: string[];
  observations?: Array<Record<string, unknown>>;
  quality_flags?: string[];
  batch_id?: string;
  interval?: string;
  bar_time?: string;
  source_count?: number;
  volume?: number | null;
  amount?: number | null;
  vwap?: number | null;
  volume_spread_pct?: number | null;
  amount_spread_pct?: number | null;
  verified_at?: string;
  start_date?: string;
  end_date?: string;
  modified_at?: string;
  error?: string;
}

export interface PortfolioReview {
  status: string;
  portfolio_path: string;
  portfolio_state: PortfolioStateSnapshot;
  verified_cache_dir: string;
  verified_market_cache: VerifiedMarketCacheRow[];
  market_cache_db?: string | null;
  market_cache_coverage?: MarketCacheCoverage[];
  active_market_refresh?: MarketCacheRun | null;
  market_refresh?: Record<string, unknown> | null;
}

export interface PortfolioMandateBand {
  configured: boolean;
  target_amount: number;
  min_amount: number;
  max_amount: number | null;
}

export interface PortfolioSleeve extends PortfolioMandateBand {
  id: string;
  name: string;
  parent_id?: string | null;
  rebalance_band_amount: number;
  single_position_max_amount: number | null;
  sort_order: number;
}

export interface PortfolioAssignment {
  active_sleeve_id: string;
  assigned_by: "agent" | "user";
  confidence: number;
  rationale?: string;
  user_locked: boolean;
  suggested_sleeve_id?: string;
  suggested_rationale?: string;
  suggestion_run_count?: number;
  needs_user_review?: boolean;
  updated_at?: string;
}

export interface PortfolioMandate {
  schema_version: number;
  version: number;
  suggestion_revision: number;
  base_currency: string;
  classification_policy: Record<string, unknown>;
  cash_policy: PortfolioMandateBand;
  sleeves: PortfolioSleeve[];
  assignments: Record<string, PortfolioAssignment>;
  classification_history: Array<Record<string, unknown>>;
  updated_at: string;
}

export interface PortfolioDailyRunArtifact {
  artifact_id: string;
  kind: "master_pdf" | "holding_daily_pdf" | "master_markdown" | string;
  symbol?: string | null;
  security_name?: string | null;
  filename: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  revision?: number;
  superseded?: boolean;
  expired?: boolean;
}

export interface PortfolioDailyRunAnalysisGate {
  decision: "proceed" | "skip_report" | string;
  minimum_coverage_ratio: number;
  coverage_ratio: number;
  eligible_count: number;
  total_count: number;
  eligible_symbols: string[];
  missing_symbols: string[];
  missing_market_symbols: string[];
  missing_research_symbols: string[];
  model_sessions_started: number;
}

export interface PortfolioDailyRun {
  run_id: string;
  market_date: string;
  status: "queued" | "running" | "cancelling" | "completed" | "completed_with_warnings" | "failed" | "cancelled" | string;
  stage: string;
  progress: { completed: number; total: number; percent: number };
  refresh_policy: "ensure_fresh" | "force" | "reuse";
  report_profile: string;
  data_status?: string;
  analysis_gate?: PortfolioDailyRunAnalysisGate;
  warnings?: string[];
  error?: string | null;
  summary?: { exit?: number; reduce?: number; add?: number; observe?: number };
  artifacts?: PortfolioDailyRunArtifact[];
  created_at: string;
  completed_at?: string | null;
  deduplicated?: boolean;
  revision?: number;
  artifact_revision?: number;
  parent_run_id?: string | null;
  retry_symbol?: string | null;
  data_batch_id?: string;
  reused_data_batch?: boolean;
  input_outdated?: boolean;
  input_outdated_reasons?: string[];
}

export interface PortfolioWeeklyRun {
  run_id: string;
  report_id: string;
  run_key: string;
  symbol: string;
  security_name?: string;
  week_start: string;
  week_end: string;
  status: "queued" | "running" | "cancelling" | "completed" | "completed_with_warnings" | "failed" | "cancelled" | string;
  stage: string;
  progress: { completed: number; total: number; percent: number };
  refresh_policy: "ensure_fresh" | "force" | "reuse";
  report_profile: string;
  report_audience?: "user";
  quality_status?: "passed" | "passed_with_gaps" | "failed_validation";
  coverage_status?: "complete" | "partial" | "insufficient";
  analysis_gate?: Record<string, unknown>;
  warnings?: string[];
  error?: string | null;
  artifacts?: PortfolioDailyRunArtifact[];
  action_ready_count?: number;
  watch_only_count?: number;
  previous_validation_count?: number;
  scenario_change_count?: number;
  valid_from?: string;
  valid_until?: string;
  review_due_at?: string;
  source_valid_until?: string;
  catalog_status?: string;
  created_at: string;
  completed_at?: string | null;
  deduplicated?: boolean;
  revision?: number;
}

export type MonitorProfileStatus = "drafting" | "pending_review" | "active" | "paused" | "expired" | "closed";
export type MonitorAlertCue = "none" | "ymca_v1";
export type MonitorRuleKind =
  | "price_cross_above"
  | "price_cross_below"
  | "price_zone_enter"
  | "price_zone_exit"
  | "intraday_pct_change_above"
  | "intraday_pct_change_below"
  | "volume_ratio_above";
export type MonitorTargetIntent =
  | "buy_point"
  | "add_position"
  | "stop_loss"
  | "take_profit"
  | "watch"
  | "breakout";

export interface MonitorPriceVolumePolicy {
  enabled: boolean;
  interval: "5m";
  baseline_method: "same_time_bucket_median";
  baseline_sessions: number;
  min_samples: number;
  contraction_ratio: number;
  expansion_ratio: number;
  flat_return_bps: number;
  acceleration_multiplier: number;
}

export type MonitorPriceVolumeStatus = "ready" | "insufficient_data" | "disabled";

export interface MonitorPriceVolumeSnapshot {
  status: MonitorPriceVolumeStatus;
  regime: string | null;
  volume_state: string | null;
  volume_ratio: number | null;
  baseline_samples: number;
  three_bar_return_bps: number | null;
  latest_return_bps: number | null;
  close_location: number | null;
  accelerated_decline: boolean;
  reason_codes: string[];
  analysis_scope?: "live" | "historical";
  data_as_of?: string | null;
  volume_quality?: "verified" | "single_source" | "conflict" | "unavailable" | string;
  volume_source_count?: number;
  volume_sources?: string[];
  volume_unit?: string | null;
  interpretation?: {
    bias: "bullish" | "bearish" | "mixed" | "neutral" | string;
    meaning: string;
    risk: string;
    next_confirmation: string;
    confidence: "high" | "medium" | "low" | string;
  };
}

export type MonitorTargetAssessmentDecision =
  | "supports_action"
  | "no_confirmation"
  | "opposes_add"
  | "insufficient_data";

export interface MonitorTargetAssessment {
  client_rule_id: string;
  target_intent: MonitorTargetIntent;
  target_level: number;
  phase: "approaching" | "reached";
  decision: MonitorTargetAssessmentDecision;
  distance_bps: number;
  message: string;
  reason_codes: string[];
}

export interface MonitorRule {
  client_rule_id: string;
  kind: MonitorRuleKind;
  severity: "info" | "warning" | "critical";
  enabled: boolean;
  /** Absent on legacy schema v1/v2 plans; schema v3 persists an explicit value. */
  alert_cue?: MonitorAlertCue;
  target_intent?: MonitorTargetIntent;
  target_level?: number;
  parameters: {
    threshold?: number;
    lower?: number;
    upper?: number;
    threshold_pct?: number;
    ratio?: number;
    clear_ratio?: number;
    baseline_method?: "same_time_bucket_median";
    baseline_sessions?: number;
    min_samples?: number;
    interval: "1m" | "5m" | "1D";
    adjustment: "raw";
    confirmation_count: number;
    cooldown_minutes: number;
    clear_hysteresis_bps: number;
  };
  valid_until?: string | null;
  rationale?: string;
  calculation_basis?: {
    method: string;
    method_label: string;
    formula: string;
    summary: string;
    recommended_value: number;
    references: Array<{
      label: string;
      value?: number;
      date?: string;
    }>;
  };
}

export interface MonitorPlan {
  schema_version: number;
  symbol: string;
  data_mode?: "verified" | "single_source";
  summary: string;
  quote_tier: "low" | "normal" | "active";
  near_trigger_tier: "low" | "normal" | "active";
  near_trigger_distance_bps: number;
  price_volume_policy?: MonitorPriceVolumePolicy;
  analysis_ref?: MonitorAnalysisRef;
  source_horizon?: "daily" | "weekly" | "structural";
  source_report_id?: string;
  source_period?: Record<string, string>;
  source_valid_until?: string;
  review_due_at?: string;
  watch_scenarios?: MonitorWatchScenario[];
  market_rules: MonitorRule[];
  news_topics: Array<{ semantic_description: string; [key: string]: unknown }>;
  fundamental_monitor: { enabled: boolean; capability_status?: string; [key: string]: unknown };
  hard_valid_until: string;
  evidence_notes?: string[];
  automation_policy?: {
    activation_mode: "autonomous" | "manual_confirmation_required";
    activated_by: "autopilot" | "daily_report" | "weekly_report" | "structural_report" | "report";
    evidence_fingerprint?: string;
    trade_execution: "forbidden";
    trigger_type?: string;
  };
}

export interface MonitorPlanVersion {
  profile_id: string;
  version: number;
  status: string;
  schema_version: number;
  plan: MonitorPlan;
  evidence_manifest: Record<string, unknown>;
  model_id: string;
  data_as_of?: string | null;
  created_at: string;
  activated_at?: string | null;
  created_by?: "autopilot" | "monitor_planner" | string;
}

export interface MonitorProfile {
  profile_id: string;
  symbol: string;
  market: string;
  instrument_type: "company_equity" | "etf";
  status: MonitorProfileStatus;
  active_plan_version?: number | null;
  profile_revision: number;
  delivery_target_id?: string | null;
  input_outdated: boolean;
  blocked_reasons: string[];
  updated_at: string;
  last_quote_check_at?: string | null;
  last_success_at?: string | null;
  next_quote_run_at?: string | null;
  last_quote?: {
    price: number | null;
    observed_at: string;
    data_as_of?: string | null;
    status: string;
    interval?: string | null;
    sources: string[];
    session_open?: number | null;
    session_high?: number | null;
    session_low?: number | null;
    session_date?: string | null;
    previous_price?: number | null;
    previous_data_as_of?: string | null;
    price_change?: number | null;
    price_change_pct?: number | null;
    trend?: "up" | "down" | "flat" | "unknown";
    price_volume?: MonitorPriceVolumeSnapshot | null;
    historical_price_volume?: MonitorPriceVolumeSnapshot | null;
    price_volume_backfill?: {
      status: "queued" | "running" | "completed" | "failed" | string;
      market_date?: string;
      started_at?: string;
      completed_at?: string;
      refresh_status?: string;
      deduplicated?: boolean;
      error?: string;
    } | null;
  } | null;
  plans?: MonitorPlanVersion[];
  display_plan?: MonitorPlanVersion | null;
  watch_episodes?: MonitorWatchEpisode[];
}

export interface MonitorAnalysisRef {
  snapshot_id: string;
  report_ref: string;
  report_type: "single_stock_research" | "holding_analysis" | "daily_portfolio" | "weekly_review" | "monitor_research";
  title: string;
  revision: number;
  body_sha256: string;
  quality_status: "ready" | "data_limited" | "conflicted" | "invalidated";
  generated_at: string;
  data_as_of: string;
  research_snapshot_id?: string;
}

export interface MonitorWatchScenario {
  scenario_id: string;
  client_rule_id: string;
  label: string;
  intent: MonitorTargetIntent;
  evidence_refs: string[];
  original_level: {
    kind: "price" | "zone";
    value?: number;
    lower?: number;
    upper?: number;
    unit: string;
    adjustment: "raw";
    source_text?: string;
  };
  trigger: {
    kind: "price_cross_above" | "price_cross_below" | "price_zone_enter" | "price_zone_exit";
    threshold?: number;
    lower?: number;
    upper?: number;
    interval: "1m" | "5m";
    confirmation_count: number;
  };
  approach_policy: {
    distance_bps: number;
    source: "report" | "atr20_default" | "user";
    check_interval: "1m";
  };
  volume_confirmation: {
    metric: "same_bucket_5m_volume_ratio" | "same_clock_cumulative_volume_ratio" | "absolute_cumulative_volume";
    comparator: "gte" | "lte";
    threshold: number;
    min_samples: number;
    mode: "classify_only";
    unit: string;
  };
  resolution_policy: {
    rejection_hysteresis_bps: number;
    max_observation_bars: number;
    close_action: "unresolved";
  };
  invalidation?: { kind: "price_cross_above" | "price_cross_below"; level: number };
  rationale: string;
  source_conditions?: Array<{
    condition_id: string;
    source_text: string;
    role: "required" | "supportive" | "invalidation";
    coverage_status: "mapped" | "awaiting_data" | "ambiguous" | "unsupported";
    reason?: string;
    evidence_refs: string[];
    research_condition?: {
      source_text: string;
      kind: string;
      operator: string;
      interval: "1d" | "1w";
      value?: number;
      lower?: number;
      upper?: number;
      baseline?: string;
      threshold?: number;
      consecutive?: number;
      lookback?: number;
      metric?: string;
      unit?: string;
    };
    executable_mapping?: {
      coverage_status: "mapped" | "awaiting_data" | "ambiguous" | "unsupported";
      reason?: string;
    };
  }>;
  entry_conditions?: MonitorConditionGroup;
  confirmation_conditions?: MonitorConditionGroup;
  invalidation_conditions?: MonitorConditionGroup;
  sequence_policy?: { enabled: boolean; max_wait_bars: number; reset_on_invalidation: boolean };
  action_template?: {
    action: "observe" | "add" | "reduce" | "exit";
    sizing: { kind: string; value?: number; unit?: string; source: string };
    confidence_floor: "low" | "medium" | "high";
  };
  automation_status?: "action_ready" | "watch_only";
  scenario_fingerprint?: string;
  candidate_id?: string;
  scenario_family_id?: string;
  priority?: "normal" | "high";
  calculation_basis?: {
    method: string;
    method_label: string;
    formula: string;
    summary: string;
    recommended_value: number;
    references: Array<{ label: string; value?: number; date?: string }>;
  };
  claim_ids?: string[];
  interpretation?: {
    price_only: string;
    confirmed: string;
    divergence: string;
    invalidated: string;
    insufficient_data: string;
    bullish_case: string;
    bearish_case: string;
  };
  mapping_status?: "mapped" | "partial";
  change_type?: "new" | "unchanged" | "raised" | "lowered" | "modified" | "withdrawn" | "expired";
  previous_candidate_id?: string | null;
  change_details?: {
    previous_level?: number;
    current_level?: number;
    delta?: number;
    summary?: string;
  };
}

export interface ReportMonitoringBundle {
  schema_version: 1;
  symbol: string;
  instrument_type: "etf" | "company_equity";
  horizon: "daily" | "weekly";
  generated_at: string;
  data_as_of: string;
  valid_from: string;
  valid_until: string;
  review_due_at: string;
  source_valid_until?: string;
  expired_reason?: string | null;
  early_invalidation_conditions?: string[];
  price_basis: { adjustment: "raw"; currency: "CNY"; tick_size: number };
  monitoring_status: "available" | "not_recommended" | "data_insufficient";
  price_volume_context: {
    policy: MonitorPriceVolumePolicy;
    data_mode: "verified" | "single_source";
    source_count: number;
    sources: string[];
    single_source_authorized: boolean;
    warnings: string[];
    refresh_attempted: boolean;
    refresh_succeeded: boolean;
  };
  candidates: MonitorWatchScenario[];
  scenario_changes: Array<{
    scenario_family_id: string;
    candidate_id?: string | null;
    previous_candidate_id?: string | null;
    change_type: "new" | "unchanged" | "raised" | "lowered" | "modified" | "withdrawn" | "expired";
    change_details: Record<string, unknown>;
    field_changes?: Array<{ field: string; before: unknown; after: unknown }>;
    reason_claim_ids?: string[];
  }>;
  validation_errors: string[];
  source: "structured_daily_report" | "structured_weekly_report";
  source_report_id?: string;
  source_period?: Record<string, string>;
  activation_policy: "manual_confirmation_required";
  trade_execution: "forbidden";
}

export type DailyMonitoringBundle = ReportMonitoringBundle & {
  horizon: "daily";
  source: "structured_daily_report";
};

export type WeeklyMonitoringBundle = ReportMonitoringBundle & {
  horizon: "weekly";
  source: "structured_weekly_report";
};

export interface WeeklyReviewSummary {
  week_start: string;
  week_end: string;
  generated_at: string;
  data_as_of: string;
  valid_from: string;
  valid_until: string;
  review_due_at: string;
  source_valid_until: string;
  quality_status: "passed" | "passed_with_gaps" | "failed_validation";
  coverage_status: "complete" | "partial" | "insufficient";
  weekly_view: {
    trend_stage: string;
    trend_direction: string;
    trend_strength: string;
    week_return_pct: number;
    relative_strength?: string;
    volume_state?: string;
    volatility_state?: string;
    location_context?: string;
    summary?: string;
  };
  previous_week_validation: Array<{
    scenario_family_id: string;
    outcome: string;
    first_approach_at?: string | null;
    first_trigger_at?: string | null;
    invalidation_at?: string | null;
    summary: string;
  }>;
  key_levels: Array<Record<string, unknown>>;
  scenario_changes: ReportMonitoringBundle["scenario_changes"];
  data_gaps: string[];
}

export interface StructuralMonitoringCandidate {
  scenario_id: string;
  label: string;
  intent:
    | "structural_invalidation"
    | "major_support"
    | "major_resistance"
    | "breakout_confirmation"
    | "trend_recovery"
    | "research_review";
  level?:
    | { kind: "point"; price: number }
    | { kind: "range"; low: number; high: number }
    | null;
  proximity_conditions: string[];
  price_trigger_conditions: string[];
  confirmation_conditions: string[];
  volume_conditions: string[];
  invalidation_conditions: string[];
  observation_window: string;
  recommended_action: string;
  source_text: string;
  section_id: string;
  machine_expressible: boolean;
  actionability: "action_ready" | "watch_only";
  lineage: {
    status: "complete" | "claim_not_resolved" | "claim_support_insufficient";
    claim_support_status: "verified" | "triangulated" | "weak" | "conflicted" | "insufficient";
    claim_ids: string[];
    fact_ids: string[];
    evidence_ids: string[];
    reference_numbers: number[];
  };
}

export interface StructuralMonitoringBundle {
  schema_version: 1;
  report_id: string;
  report_revision: number;
  symbol: string;
  instrument_type: "etf" | "company_equity" | "index";
  report_profile: string;
  horizon: "structural";
  generated_at: string;
  data_as_of: string;
  valid_from: string;
  valid_until: string;
  review_due_at: string;
  price_basis: { adjustment: "raw"; currency: "CNY"; tick_size: number };
  report_quality_status: "passed" | "passed_with_gaps";
  monitoring_status: "available" | "not_recommended" | "data_insufficient";
  activation_policy: "manual_confirmation_required";
  trade_execution: "forbidden";
  structural_context: {
    trend_stage: "下降" | "震荡" | "筑底" | "上升" | "unknown";
    trend_direction: "向上" | "向下" | "横盘" | "unknown";
    trend_strength: "强" | "中" | "弱" | "unknown";
    thesis_state: "intact" | "weakening" | "invalidated" | "unknown";
    structural_levels: Array<Record<string, unknown>>;
    thesis_invalidation_conditions: string[];
    review_triggers: string[];
  };
  candidates: StructuralMonitoringCandidate[];
  integrity: {
    report_sha256: string;
    references_sha256: string;
    bundle_sha256: string;
  };
}

export type MonitoringBundle = ReportMonitoringBundle | StructuralMonitoringBundle;

export interface MonitorConditionGroup {
  operator: "all" | "any";
  conditions: Array<{
    condition_id: string;
    source_condition_id: string;
    kind: string;
    operator: string;
    value?: number;
    lower?: number;
    upper?: number;
    unit?: string;
    interval: "1m" | "5m" | "30m" | "1d";
    consecutive: number;
    lookback_bars: number;
    freshness_seconds: number;
    metric?: string;
    direction?: string;
  }>;
}

export interface MonitorWatchEpisode {
  episode_id: string;
  profile_id: string;
  plan_version: number;
  client_rule_id: string;
  session_date: string;
  state: "approaching" | "testing" | "confirmed" | "rejected" | "unresolved";
  phase: string;
  outcome?: "confirmed_breakout" | "false_breakout" | "approach_withdrawn" | "unresolved" | null;
  started_at: string;
  first_cross_at?: string | null;
  resolved_at?: string | null;
  observed_bars: number;
  approach_notified: boolean;
  result_notified: boolean;
  volume_verdict?: string | null;
  facts: Record<string, unknown>;
}

export interface MonitorDeliveryTarget {
  target_id: string;
  channel: "feishu";
  chat_id: string;
  chat_type: "p2p" | "group";
  session_key: string;
  status: "active" | "revoked";
  created_at: string;
}

export interface FeishuDeliverySettings {
  targets: MonitorDeliveryTarget[];
  default_target_id?: string | null;
  effective_target_id?: string | null;
  requires_selection: boolean;
}

export interface BindMonitorDeliveryTargetRequest {
  channel: "feishu";
  chat_id: string;
  chat_type: "p2p" | "group";
  session_key?: string;
}

export interface MonitorDeliveryBindingAttempt {
  binding_id: string;
  code?: string;
  command?: string;
  status: "pending" | "claimed" | "expired";
  created_at: string;
  expires_at: string;
  claimed_at?: string | null;
  claimed_sender_id?: string | null;
  claimed_chat_id?: string | null;
  target_id?: string | null;
  target?: MonitorDeliveryTarget;
}

export interface CreateMonitorDraftBatchRequest {
  symbols: string[];
  delivery_target_id?: string;
  force_fresh?: boolean;
  allow_single_source?: boolean;
}

export interface MonitorDraftBatchItem {
  symbol: string;
  status: "generating" | "ready" | "blocked" | "failed";
  profile_id?: string | null;
  plan_version?: number | null;
  blocked_reasons: string[];
  error?: string | null;
}

export interface MonitorDraftBatch {
  batch_id: string;
  status: string;
  requested_symbols: string[];
  delivery_target_id?: string | null;
  created_at: string;
  completed_at?: string | null;
  items: MonitorDraftBatchItem[];
}

export interface MonitorReportCandidate {
  report_ref: string;
  report_type: "single_stock_research" | "holding_analysis" | "daily_portfolio" | "monitor_research";
  symbol: string;
  title: string;
  source_id: string;
  source_message_id?: string | null;
  artifact_id?: string | null;
  revision: number;
  body_sha256: string;
  quality_status: "ready" | "data_limited" | "conflicted" | "invalidated";
  generated_at: string;
  data_as_of: string;
  trading_day_age: number;
  stale: boolean;
  research_reasons: string[];
  excerpt: string;
}

export interface CreateMonitorPlannerJobRequest {
  symbols: string[];
  report_refs?: Record<string, string>;
  research_policy: "if_needed";
  delivery_target_id?: string;
  force_fresh: true;
  activation_mode?: "manual" | "autonomous";
  trigger_type?: MonitorAutopilotTriggerType;
}

export type MonitorPlannerJobStatus =
  | "queued"
  | "researching"
  | "planning"
  | "validating"
  | "ready"
  | "blocked"
  | "failed"
  | "cancelled";

export interface MonitorPlannerJobItem {
  symbol: string;
  status: MonitorPlannerJobStatus;
  report_ref?: string | null;
  report_snapshot_id?: string | null;
  research_snapshot_id?: string | null;
  profile_id?: string | null;
  plan_version?: number | null;
  blocked_reasons: string[];
  validation_errors: string[];
  progress: Record<string, unknown>;
  error?: string | null;
  attempt: number;
}

export interface MonitorPlannerJob {
  job_id: string;
  status: MonitorPlannerJobStatus;
  requested_symbols: string[];
  report_refs: Record<string, string>;
  research_policy: "if_needed";
  delivery_target_id?: string | null;
  force_fresh: boolean;
  activation_mode?: "manual" | "autonomous";
  trigger_type?: MonitorAutopilotTriggerType | null;
  evidence_fingerprint?: string | null;
  cancel_requested: boolean;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  items: MonitorPlannerJobItem[];
}

export type MonitorAutopilotTriggerType =
  | "report_ready"
  | "holdings_changed"
  | "scheduled_close"
  | "approaching"
  | "invalidated"
  | "material_evidence_changed";

export interface MonitorAutopilotConfig {
  config_id: "default";
  enabled: boolean;
  selected_symbols: string[];
  activation_mode: "autonomous";
  research_policy: "if_needed";
  trigger_types: MonitorAutopilotTriggerType[];
  daily_close_enabled: boolean;
  delivery_target_id?: string | null;
  runtime_mode: "shadow" | "deliver";
  revision: number;
  updated_at?: string | null;
  automatic_trading: "forbidden";
}

export interface MonitorAutopilotUpdate {
  enabled: boolean;
  selected_symbols: string[];
  change_source?: "user_toggle" | "holding_selection";
  trigger_types?: MonitorAutopilotTriggerType[];
  daily_close_enabled?: boolean;
  delivery_target_id?: string | null;
  runtime_mode?: "shadow" | "deliver";
}

export interface MonitorAutopilotRun {
  trigger_id: string;
  symbol: string;
  trigger_type: MonitorAutopilotTriggerType;
  status: "queued" | "running" | "completed" | "blocked" | "failed" | "cancelled";
  payload: Record<string, unknown>;
  planner_job_id?: string | null;
  evidence_fingerprint?: string | null;
  created_at: string;
  completed_at?: string | null;
  error?: string | null;
  blocked_reasons?: string[];
  validation_errors?: string[];
  detail_error?: string | null;
  build_state?: {
    status: "building" | "active" | "blocked" | "failed" | "cancelled";
    stage: string;
    stage_label: string;
    progress_percent: number;
    planner_status?: string;
    item_status?: string;
    attempt: number;
    profile_id?: string | null;
    plan_version?: number | null;
    updated_at?: string | null;
    terminal: boolean;
    self_repair: {
      policy: "bounded";
      infrastructure_retry_limit: number;
      infrastructure_retries_used: number;
      agent_iteration_limit: number;
      agent_token_budget: number;
      strategy: "verified_market_first_no_model" | "continuity_then_multi_method";
      full_report_retry_enabled: boolean;
      circuit_open: boolean;
      token_spend_allowed: boolean;
    };
  };
}

export type MonitorTargetProfileStatus =
  | "building"
  | "active"
  | "watch_only"
  | "blocked"
  | "superseded";

export interface MonitorTargetMonitoringCard {
  symbol: string;
  name: string;
  profile_id?: string | null;
  profile_status: MonitorTargetProfileStatus;
  build_state: NonNullable<MonitorAutopilotRun["build_state"]>;
  blockers: Array<{
    code:
      | "price_series_discontinuity_unverified"
      | "adjustment_factor_unverified"
      | "insufficient_post_event_history"
      | "volume_unit_conflict"
      | "no_qualified_level"
      | "ai_selection_invalid"
      | "recovery_circuit_open";
    retryable: boolean;
    detail?: string;
  }>;
  continuity: Record<string, unknown>;
  level_summary: Array<Record<string, unknown>> | Record<string, Record<string, unknown>>;
  volume_gate: Record<string, unknown>;
  self_repair: Record<string, unknown>;
  decision_id: string;
  decision_revision: number;
  evidence_fingerprint: string;
  level_snapshot_id?: string | null;
  decision_brief: MonitorDecisionBrief;
  risk_assessment: MonitorRiskAssessment;
  level_ladder: {
    support: MonitorDecisionLevel[];
    resistance: MonitorDecisionLevel[];
  };
  action_playbook: MonitorActionPlaybook;
  available_choices: MonitorDecisionChoice[];
  monitoring_thesis?: Record<string, unknown>;
  scenario_comparison?: Record<string, unknown>;
  risk_preference?: MonitorRiskPreference | null;
  latest_draft?: MonitorConditionOrderDraft | null;
  thesis_changed_at?: string | null;
  selection_mode?: string;
  selected: boolean;
  updated_at?: string | null;
}

export interface MonitorDecisionChoice {
  choice_id: string;
  label: string;
  description: string;
  recommended: boolean;
  eligible_draft_type?: "add" | "reduce";
}

export interface MonitorDecisionBrief {
  headline: string;
  market_state: string;
  risk_level: string;
  risk_direction: string;
  recommended_choice_id: string;
  recommended_action: string;
  summary: string;
  why_now: string[];
  counter_evidence: string[];
  next_confirmation: string;
  invalidation: string;
  data_status: "verified" | "partial" | "blocked";
  confidence: "high" | "medium" | "low";
  choices: MonitorDecisionChoice[];
}

export interface MonitorRiskAssessment {
  risk_level: string;
  risk_direction: string;
  risk_probability?: number | null;
  probability_status?: string;
  risk_impact: string;
  estimated_impact_pct?: number | null;
  estimated_impact_amount?: number | null;
  data_confidence: "high" | "medium" | "low";
  basis?: Record<string, unknown>;
}

export interface MonitorDecisionLevel extends Record<string, unknown> {
  candidate_id?: string;
  role?: "T0" | "S1" | "S2" | "R1" | "R2" | string;
  lower?: number;
  upper?: number;
  score?: number;
  confidence?: string;
  automation_status?: string;
}

export interface MonitorActionPlaybook {
  do_now: string;
  why: string;
  if_holds: string;
  if_breaks: string;
  do_not: string;
  review_deadline: string;
  eligible_draft_types: string[];
}

export interface MonitorRiskPreferenceInput {
  holding_period: "short_term" | "swing" | "long_term";
  max_risk_amount?: number | null;
  max_risk_pct?: number | null;
  max_add_amount?: number | null;
  max_position_amount?: number | null;
  minimum_reward_risk?: number | null;
  confirmation_intervals?: Array<"5m" | "30m" | "1d">;
  max_buy_price?: number | null;
  min_sell_price?: number | null;
  slippage_bps?: number | null;
  draft_valid_minutes?: number;
  condition_order_permission: "only_alert" | "local_draft" | "broker_export";
  sellable_quantity?: number | null;
  intraday_added_quantity?: number | null;
  default_reduce_fraction?: number | null;
}

export interface MonitorRiskPreference extends MonitorRiskPreferenceInput {
  symbol: string;
  revision: number;
  configured_for_sizing: boolean;
  updated_at: string;
}

export interface MonitorDecisionChoiceRequest {
  decision_id: string;
  choice_id: string;
  decision_revision: number;
  evidence_fingerprint: string;
  idempotency_key: string;
}

export interface MonitorConditionDraftRequest {
  choice_id: string;
  decision_revision: number;
  evidence_fingerprint: string;
}

export interface MonitorConditionOrderDraft extends Record<string, unknown> {
  draft_id: string;
  decision_id: string;
  symbol: string;
  side: "buy" | "sell";
  status: "draft" | "validated" | "needs_risk_preferences" | "constraints_failed" | "stale" | "expired" | "cancelled" | string;
  quantity?: number | null;
  valid_until: string;
  trade_execution: "forbidden";
  order_submission: "forbidden";
}

export type MonitorRecommendationFeedback = "handled" | "continue_observing" | "ignored";

export interface MonitorRecommendation {
  recommendation_id: string;
  profile_id?: string | null;
  plan_version?: number | null;
  episode_id?: string | null;
  symbol: string;
  scenario_id?: string | null;
  status: "ready" | "evidence_pending" | string;
  action: "observe" | "add" | "reduce" | "exit";
  requested_quantity?: number | null;
  constrained_quantity?: number | null;
  current_price?: number | null;
  estimated_amount?: number | null;
  confidence: "low" | "medium" | "high";
  valid_until: string;
  feedback_status: "pending" | MonitorRecommendationFeedback;
  notes?: string[];
  created_at: string;
  trade_execution: "forbidden";
}

export interface MonitorEventFacts extends Record<string, unknown> {
  client_rule_id?: string;
  direction?: "above" | "below" | "enter" | "exit" | null;
  threshold?: number | null;
  target_intent?: MonitorTargetIntent | null;
  target_level?: number | null;
  confirmation_count?: number;
  alert_cue?: MonitorAlertCue;
  delivery_mode?: "shadow" | "deliver";
  last_price?: number;
  quality_status?: string;
  price_volume?: MonitorPriceVolumeSnapshot | null;
  target_assessment?: MonitorTargetAssessment | null;
}

export interface MonitorEvent {
  event_id: string;
  profile_id: string;
  symbol: string;
  plan_version: number;
  kind: string;
  status: string;
  severity: "info" | "warning" | "critical";
  title: string;
  summary: string;
  facts: MonitorEventFacts;
  episode_id?: string | null;
  phase?: string | null;
  outcome?: string | null;
  volume_verdict?: string | null;
  first_seen_at: string;
  acknowledged_at?: string | null;
  deliveries?: Array<{
    delivery_id: string;
    status: string;
    delivery_mode?: "shadow" | "deliver";
    would_deliver?: boolean;
    suppression_reason?: string | null;
    remote_message_id?: string | null;
    error?: string | null;
  }>;
}

export interface MonitorEffectAvailability {
  audio_ready: boolean;
  /** Legacy aggregate; current servers expose it as up && down for compatibility. */
  sticker_ready?: boolean;
  up_sticker_ready?: boolean;
  down_sticker_ready?: boolean;
  available: boolean;
}

export interface PortfolioMonitoringStatus {
  enabled_by_config: boolean;
  effective_mode: "off" | "shadow" | "deliver";
  runtime: {
    enabled?: boolean;
    running?: boolean;
    leader?: boolean;
    mode?: "off" | "shadow" | "deliver";
    requested_mode?: string;
    mode_valid?: boolean;
    mode_reason?: string | null;
    current_tick_started_at?: string | null;
    last_error?: string | null;
    last_tick?: Record<string, unknown> | null;
    price_volume?: {
      requested_mode: string;
      mode: "off" | "shadow" | "deliver";
      mode_valid: boolean;
      mode_reason?: string | null;
    };
    calendar?: {
      mode?: string;
      market_date?: string | null;
      is_trading_day?: boolean | null;
      session?: string;
      open?: boolean;
      checked_at?: string;
      error?: string | null;
    };
  };
  capabilities: Record<string, string>;
  effects?: {
    ymca_v1: MonitorEffectAvailability;
  };
  profiles: number;
  active_profiles: number;
  events: number;
  pending_deliveries: number;
  uncertain_deliveries: number;
  shadow_suppressed_deliveries: number;
  blocked_profiles: number;
  database_size_bytes: number;
  database_max_bytes: number;
  database_utilization: number;
  delivery_status_counts: Record<string, number>;
  observation_status_counts: Record<string, number>;
  price_volume_quality: {
    window_hours: number;
    observation_count: number;
    evidence_count: number;
    disabled_count: number;
    status_counts: Record<string, number>;
    reason_counts: Record<string, number>;
    insufficient_rate: number;
    conflict_rate: number;
  };
  runtime_health: {
    window_hours: number;
    tick_count: number;
    error_tick_count: number;
    events_created: number;
    duplicate_event_count: number;
    event_attempt_count: number;
    duplicate_event_rate: number;
    duration_ms: { p50?: number | null; p95?: number | null; p99?: number | null; max?: number | null };
    schedule_lag_ms: { p50?: number | null; p95?: number | null; p99?: number | null; max?: number | null };
    closed_session_backlog?: {
      due_profile_ticks: number;
      lag_ms: { p50?: number | null; p95?: number | null; p99?: number | null; max?: number | null };
    };
    bar_lag_ms: { p50?: number | null; p95?: number | null; p99?: number | null; max?: number | null };
    database_growth_bytes: number;
    latest_tick?: Record<string, unknown> | null;
    counters: Record<string, { value: number; updated_at: string }>;
  };
  profile_health: Array<{
    symbol: string;
    status: string;
    last_quote_check_at?: string | null;
    last_success_at?: string | null;
    next_quote_run_at?: string | null;
    blocked_reasons: string[];
    input_outdated: boolean;
  }>;
  maintenance?: {
    status: string;
    started_at: string;
    finished_at?: string | null;
    details?: Record<string, unknown>;
    error?: string | null;
  } | null;
}

export interface StartPortfolioDailyRunRequest {
  market_date?: string;
  refresh_policy?: "ensure_fresh" | "force" | "reuse";
  report_profile?: "master_with_holding_appendices";
  force_new?: boolean;
}

export interface StartPortfolioWeeklyRunsRequest {
  week_end?: string;
  symbols?: string[];
  refresh_policy?: "ensure_fresh" | "force" | "reuse";
  report_profile?: "weekly_review_v1";
  report_audience?: "user";
  force_new?: boolean;
  single_source_authorized?: boolean;
}

export type PortfolioAnalysisScope = "holding" | "portfolio" | "market";
export type PortfolioAnalysisPhase = "premarket" | "intraday";

export interface PortfolioAnalysisSessionRequest {
  scope: PortfolioAnalysisScope;
  symbol?: string;
}

export interface PortfolioAnalysisSession {
  analysis_id: string;
  session_id: string;
  scope: PortfolioAnalysisScope;
  symbol?: string | null;
  analysis_phase?: PortfolioAnalysisPhase | null;
  status: "queued" | "running" | "completed" | "failed" | "cancelled" | string;
  queue_position?: number | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  error?: string | null;
}

// --- Types matching backend API contracts ---

export interface RunListItem {
  run_id: string;
  status: string;
  created_at: string;
  prompt?: string;
  total_return?: number;
  sharpe?: number;
  codes?: string[];
  start_date?: string;
  end_date?: string;
}

export interface RunDetailParams {
  chart_payload?: "summary";
  chart_symbol?: string;
}

export interface PriceBar {
  time: string;
  timestamp?: string;
  code?: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface TradeMarker {
  time: string;
  timestamp?: string;
  code?: string;
  side: "BUY" | "SELL";
  price: number;
  qty?: number;
  reason?: string;
  text?: string;
}

export interface EquityPoint {
  time: string;
  equity: string | number;
  drawdown: string | number;
}

export interface ValidationData {
  monte_carlo?: {
    actual_sharpe: number;
    actual_max_dd: number;
    p_value_sharpe: number;
    p_value_max_dd: number;
    simulated_sharpe_mean: number;
    simulated_sharpe_std: number;
    simulated_sharpe_p5: number;
    simulated_sharpe_p95: number;
    n_simulations: number;
    n_trades: number;
    error?: string;
  };
  bootstrap?: {
    observed_sharpe: number;
    ci_lower: number;
    ci_upper: number;
    median_sharpe: number;
    prob_positive: number;
    confidence: number;
    n_bootstrap: number;
    error?: string;
  };
  walk_forward?: {
    n_windows: number;
    windows: Array<{
      window: number;
      start: string;
      end: string;
      return: number;
      sharpe: number;
      max_dd: number;
      trades: number;
      win_rate: number;
    }>;
    profitable_windows: number;
    consistency_rate: number;
    return_mean: number;
    return_std: number;
    sharpe_mean: number;
    sharpe_std: number;
    error?: string;
  };
}

export interface RunData {
  status: string;
  run_id: string;
  prompt?: string;
  elapsed_seconds?: number;
  run_directory?: string;
  run_stage?: string;
  run_context?: Record<string, unknown>;

  metrics?: BacktestMetrics;
  artifacts?: ArtifactInfo[];
  run_card?: RunCard;
  validation?: ValidationData;

  chart_symbols?: string[];
  price_series?: Record<string, PriceBar[]>;
  indicator_series?: Record<string, Record<string, IndicatorPoint[]>>;
  trade_markers?: TradeMarker[];
  equity_curve?: EquityPoint[];
  trade_log?: Array<Record<string, string>>;
  run_logs?: Array<{ source?: string; line_number?: number; message?: string }>;
}

export interface RunCard {
  schema_version?: string;
  generated_at?: string;
  run_dir?: string;
  backtest?: Record<string, unknown>;
  reproducibility?: Record<string, unknown>;
  data_sources?: string[];
  metrics?: Record<string, unknown>;
  validation?: unknown;
  warnings?: string[];
  artifacts?: RunCardArtifact[];
  [key: string]: unknown;
}

export interface RunCardArtifact {
  path: string;
  size_bytes: number;
  sha256: string;
}

export interface BacktestMetrics {
  final_value: number;
  total_return: number;
  annual_return: number;
  max_drawdown: number;
  sharpe: number;
  win_rate: number;
  trade_count: number;
  [key: string]: number;
}


export interface IndicatorPoint {
  time: string;
  value: number;
}

export interface ArtifactInfo {
  name: string;
  path: string;
  type: string;
  size: number;
  exists: boolean;
}

export interface PineScriptResult {
  exists: boolean;
  content: string | null;
}

export interface SessionItem {
  session_id: string;
  title?: string;
  status?: string;
  created_at?: string;
  updated_at?: string;
  last_attempt_id?: string;
}

export type UsageEventKind = "llm_call" | "tool_call" | "resource_call";

export interface UsageTokenBreakdown {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  cache_read_input_tokens: number | null;
  cache_write_input_tokens: number | null;
  reasoning_tokens: number | null;
  cache_hit_rate: number | null;
  coverage: "complete" | "partial" | "unreported" | "not_applicable" | string;
  coverage_by_field?: Record<string, string>;
  reported_calls: number;
  total_calls: number;
  unreported_calls: number;
}

export interface UsageCallAggregate {
  llm_calls: number;
  agent_tools: number;
  external_requests: number;
  cache_accesses: number;
  failures: number;
  running: number;
}

export interface UsageDistributionItem {
  key: string;
  count: number;
}

export interface UsageCostCurrencyAggregate {
  currency: "CNY" | "USD" | string;
  estimated_cost: number;
  minimum_estimated_cost: number;
  maximum_estimated_cost: number;
  calls: number;
  peak_calls: number;
}

export interface UsageCostAggregate {
  coverage: "complete" | "partial" | "unreported" | "not_applicable" | string;
  priced_calls: number;
  unpriced_calls: number;
  total_calls: number;
  currencies: UsageCostCurrencyAggregate[];
  catalog_version: string;
  time_basis: "started_at" | string;
  sources?: Array<{ label: string; url: string }>;
}

export interface UsageEventCostEstimate {
  status: "complete" | "partial" | "unreported" | "unpriced" | "not_applicable" | string;
  reason?: string;
  currency?: "CNY" | "USD" | string;
  estimated_cost?: number;
  minimum_estimated_cost?: number;
  maximum_estimated_cost?: number;
  tier?: "standard" | "peak" | string;
  multiplier?: number;
  pricing_timezone?: string;
  local_started_at?: string;
  cache_tokens_reported?: boolean;
  rates_per_million?: {
    input: number;
    cache_read_input: number | null;
    output: number;
  };
  rule_id?: string;
  catalog_version: string;
  source_url?: string;
  source_label?: string;
  sources?: Array<{ label: string; url: string }>;
}

export interface SessionUsageAggregate {
  tokens: UsageTokenBreakdown;
  cost?: UsageCostAggregate;
  calls: UsageCallAggregate;
  models: UsageDistributionItem[];
  tools: UsageDistributionItem[];
  categories: UsageDistributionItem[];
  providers: UsageDistributionItem[];
}

export interface SessionUsageSummary {
  recording_status: "recording" | "unrecorded";
  scope_type: "session" | "monitor_job" | string;
  scope_id: string;
  revision: number;
  recording_started_at: string | null;
  current_attempt_id: string | null;
  session: SessionUsageAggregate;
  current_attempt: SessionUsageAggregate;
}

export type MonitorUsagePeriod = "today" | "7d" | "30d";

export interface MonitorUsageRecentJob {
  job_id: string;
  status: MonitorPlannerJobStatus;
  requested_symbols: string[];
  activation_mode: "manual" | "autonomous" | string;
  trigger_type?: MonitorAutopilotTriggerType | null;
  created_at?: string | null;
  completed_at?: string | null;
  revision: number;
  recording_started_at: string;
  updated_at: string;
  usage: SessionUsageAggregate;
  linked_scopes: Array<{
    scope_type: string;
    scope_id: string;
    attempt_id?: string | null;
    relationship: string;
  }>;
}

export interface MonitorUsageSummary extends SessionUsageSummary {
  period: MonitorUsagePeriod;
  started_at: string;
  completed_at: string;
  scope_count: number;
  linked_scope_count: number;
  recent_jobs: MonitorUsageRecentJob[];
}

export interface MonitorJobUsageSummary extends SessionUsageSummary {
  direct: SessionUsageAggregate;
  linked_scopes: Array<{
    scope_type: string;
    scope_id: string;
    attempt_id?: string | null;
    relationship: string;
  }>;
  job: {
    job_id: string;
    status: MonitorPlannerJobStatus;
    requested_symbols: string[];
    activation_mode: "manual" | "autonomous" | string;
    trigger_type?: MonitorAutopilotTriggerType | null;
    created_at?: string | null;
    completed_at?: string | null;
  };
}

export interface UsageEventItem {
  sequence: number;
  event_id: string;
  scope_type?: string;
  scope_id?: string;
  session_id?: string | null;
  attempt_id?: string | null;
  parent_tool_call_id?: string | null;
  kind: UsageEventKind;
  category?: string | null;
  provider?: string | null;
  model?: string | null;
  tool_name?: string | null;
  status: "running" | "ok" | "error" | "cancelled" | string;
  started_at: string;
  completed_at?: string | null;
  elapsed_ms?: number | null;
  cache_mode?: "network" | "cache_hit" | "cache_refresh" | "stale_fallback" | "unknown" | string | null;
  network_request: boolean;
  cache_access: boolean;
  query_summary?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  cache_read_input_tokens?: number | null;
  cache_write_input_tokens?: number | null;
  reasoning_tokens?: number | null;
  cost?: UsageEventCostEstimate;
  metadata: Record<string, unknown>;
}

export interface UsageEventsPage {
  recording_status: "recording" | "unrecorded";
  revision: number;
  items: UsageEventItem[];
  next_cursor: string | null;
}

export interface UsageEventFilters {
  kind?: UsageEventKind;
  category?: string;
  attemptId?: string;
  cursor?: string;
  limit?: number;
}

// --- Goal types ---

export type GoalStatus =
  | "active"
  | "paused"
  | "waiting_user"
  | "needs_refresh"
  | "insufficient_evidence"
  | "compliance_blocked"
  | "blocked"
  | "budget_limited"
  | "usage_limited"
  | "complete"
  | "cancelled"
  | "superseded";

export type GoalRiskTier =
  | "research_general"
  | "market_specific_short_term"
  | "personalized_advice_or_position_sizing";

export interface GoalRecord {
  goal_id: string;
  session_id: string;
  status: GoalStatus;
  objective: string;
  ui_summary: string;
  source: string;
  protocol: string;
  risk_tier: GoalRiskTier;
  token_budget?: number | null;
  tokens_used: number;
  turn_budget?: number | null;
  turns_used: number;
  time_budget_seconds?: number | null;
  time_used_seconds: number;
  budget_wrapup_sent: boolean;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  recap?: string | null;
}

export interface GoalClaim {
  claim_id: string;
  goal_id: string;
  session_id: string;
  claim_type: string;
  text: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface GoalCriterion {
  criterion_id: string;
  goal_id: string;
  session_id: string;
  text: string;
  required: boolean;
  status: string;
  freshness_requirement?: string | null;
  protocol_step?: string | null;
  created_at: string;
  updated_at: string;
}

export interface GoalEvidence {
  evidence_id: string;
  goal_id: string;
  session_id: string;
  text: string;
  criterion_id?: string | null;
  claim_id?: string | null;
  evidence_type: string;
  tool_call_id?: string | null;
  run_id?: string | null;
  source_provider?: string | null;
  source_type?: string | null;
  source_uri?: string | null;
  symbol_universe: string[];
  benchmark: string[];
  timeframe?: string | null;
  method?: string | null;
  assumptions: Record<string, unknown>;
  artifact_path?: string | null;
  artifact_hash?: string | null;
  retrieved_at: string;
  data_as_of?: string | null;
  freshness_status: string;
  verification_status: string;
  confidence?: string | null;
  caveat?: string | null;
  contradicts_claim_ids: string[];
  created_at: string;
}

export interface GoalSnapshot {
  goal: GoalRecord;
  claims: GoalClaim[];
  criteria: GoalCriterion[];
  evidence: GoalEvidence[];
  evidence_count: number;
}

export interface CreateGoalRequest {
  objective: string;
  criteria?: string[];
  ui_summary?: string;
  protocol?: string;
  risk_tier?: GoalRiskTier;
  token_budget?: number;
  turn_budget?: number;
  time_budget_seconds?: number;
}

export interface AddGoalEvidenceRequest {
  goal_id: string;
  expected_goal_id: string;
  text: string;
  criterion_id?: string | null;
  claim_id?: string | null;
  evidence_type?: string;
  tool_call_id?: string | null;
  run_id?: string | null;
  source_provider?: string | null;
  source_type?: string | null;
  source_uri?: string | null;
  symbol_universe?: string[];
  benchmark?: string[];
  timeframe?: string | null;
  method?: string | null;
  assumptions?: Record<string, unknown>;
  artifact_path?: string | null;
  artifact_hash?: string | null;
  data_as_of?: string | null;
  confidence?: string | null;
  caveat?: string | null;
  contradicts_claim_ids?: string[];
}

export interface UpdateGoalRequest {
  goal_id: string;
  expected_goal_id: string;
  objective?: string;
  ui_summary?: string;
}

export interface UpdateGoalResponse {
  goal: GoalRecord;
  snapshot: GoalSnapshot;
}

export interface AddGoalEvidenceResponse {
  evidence: GoalEvidence;
  snapshot: GoalSnapshot;
}

export interface GoalAuditRowRequest {
  criterion_id: string;
  result: string;
  evidence_ids?: string[];
  notes?: string;
}

export interface UpdateGoalStatusRequest {
  goal_id: string;
  expected_goal_id: string;
  status: GoalStatus;
  audit?: GoalAuditRowRequest[];
  recap?: string | null;
}

export interface UpdateGoalStatusResponse {
  goal: GoalRecord;
  snapshot: GoalSnapshot;
}

// --- Alpha Zoo types ---

export interface AlphaListParams {
  zoo?: string;
  theme?: string;
  universe?: string;
  limit?: number;
}

export interface AlphaSummary {
  id: string;
  zoo: string;
  theme: string[];
  universe: string[];
  nickname?: string;
  decay_horizon?: number | null;
  min_warmup_bars?: number | null;
  requires_sector?: boolean;
}

export interface AlphaListResponse {
  status: string;
  alphas: AlphaSummary[];
  total: number;
  returned: number;
  truncated: boolean;
}

export interface AlphaDetail {
  id: string;
  zoo: string;
  module_path?: string;
  meta: Record<string, unknown>;
}

export interface AlphaDetailResponse {
  status: string;
  alpha: AlphaDetail;
  source_code: string;
}

export interface AlphaBenchRequest {
  zoo: string;
  universe: string;
  period: string;
  top?: number;
}

export interface AlphaBenchTopRow {
  id: string;
  ic_mean: number;
  ir: number;
  theme: string[];
  formula_latex: string;
  category: "alive" | "reversed" | "dead";
}

export interface AlphaBenchResult {
  alive: number;
  reversed: number;
  dead: number;
  skipped?: number;
  top5_by_ir: AlphaBenchTopRow[];
  dead_examples: AlphaBenchTopRow[];
  by_theme: Record<string, { alive: number; reversed: number; dead: number }>;
}

export interface AlphaCompareRequest {
  alpha_ids: string[];
  universe: string;
  period: string;
  /** One of: ir | ic_mean | ic_positive_ratio | ic_count (default ir). */
  sort?: string;
}

export interface AlphaCompareRow {
  rank: number;
  id: string;
  zoo: string;
  ic_mean: number;
  ic_std: number;
  ir: number;
  ic_positive_ratio: number;
  ic_count: number;
  /** `delta_<sort>_vs_best` — gap to the top-ranked alpha on the active metric. */
  [deltaKey: string]: number | string;
}

export interface AlphaCompareSkip {
  id: string;
  reason: string;
}

export interface AlphaCompareResult {
  universe: string;
  period: string;
  sort: string;
  n_compared: number;
  n_skipped: number;
  winner: string;
  ranking: AlphaCompareRow[];
  skipped: AlphaCompareSkip[];
}

// --- Connector runtime channel types ---

/** One mandate profile inside a `mandate.proposal` event (SPEC Consent §1). */
export interface MandateProfile {
  ordinal: number;
  label: string;
  /** Concrete ticker list, or a structural universe descriptor (e.g. "tech_sector"). */
  universe: string[] | string;
  max_order_usd: number;
  daily_trade_cap: number;
  /** "none" for cash-only, otherwise a leverage descriptor/multiple. */
  leverage: string | number;
  instruments: string[];
  notes?: string;
}

/** Account block of a `mandate.proposal` event. */
export interface MandateProposalAccount {
  broker: string;
  type: string;
  funded_by: string;
}

/** Payload of the `mandate.proposal` SSE event (SPEC Consent §1). */
export interface MandateProposal {
  type?: string;
  proposal_id: string;
  session_id?: string;
  intent_normalized?: string;
  account?: MandateProposalAccount;
  ceilings_ref?: string;
  profiles: MandateProfile[];
  funding_note?: string;
  halt_note?: string;
  /** Present only when this proposal was triggered by a mandate breach (SPEC Consent §3). */
  reauth_for?: { breach_id?: string } | null;
}

/** Payload of the `mandate.committed` SSE event (SPEC Consent §1 COMMIT). */
export interface MandateCommitted {
  proposal_id?: string;
  mandate_id?: string;
  consent_record_id?: string;
  selected_ordinal?: number;
  broker?: string;
  /** Resolved limits, surfaced for the compact active-mandate badge. */
  max_order_usd?: number;
  daily_trade_cap?: number;
  expires_at?: string;
}

/** Payload of the `live.halted` SSE event (SPEC Consent §4). */
export interface LiveHalted {
  broker?: string | null;
  tripped_at?: string;
  by?: string;
  reason?: string;
}

/** Payload of the `live.action` SSE event (SPEC Consent §5 audit notify). */
export interface LiveAction {
  audit_id?: string;
  ts?: string;
  kind: string;
  intent_normalized?: string;
  outcome?: string;
  broker?: string;
  remote_tool?: string;
  error?: string | null;
}

export interface CommitMandateRequest {
  broker: string;
  proposal_id: string;
  selected_ordinal: number;
  /** Present only on the adjust path (SPEC Consent §3); null otherwise. */
  adjustments?: Record<string, unknown> | null;
  /** Explicit affirmative consent; the surface sets it on the user's click. */
  consent_ack: boolean;
  session_id?: string;
  account_ref?: string;
  lifetime_days?: number;
}

export interface CommitMandateResponse {
  mandate_id: string;
  consent_record_id: string;
  selected_ordinal?: number;
  broker?: string;
  max_order_usd?: number;
  daily_trade_cap?: number;
  expires_at?: string;
}

export interface HaltLiveResponse {
  halted: boolean;
  broker?: string | null;
  reason: string;
  sentinel: string;
}

export interface LiveAuthorizeRequest {
  broker: string;
}

export interface LiveAuthorizeResponse {
  broker: string;
  connector_profile: string;
  oauth_token_present: boolean;
  instruction: string;
  note?: string;
}

/** Mandate limits surfaced inside a `GET /live/status` broker entry (SPEC §7.5). */
export interface LiveMandateLimits {
  max_order_notional_usd?: number;
  max_total_exposure_usd?: number;
  max_leverage?: number;
  max_trades_per_day?: number;
  allowed_instruments?: string[];
  account_funding_usd?: number;
  [key: string]: unknown;
}

/** Active mandate block of a `GET /live/status` broker entry. */
export interface LiveMandateStatus {
  broker?: string;
  mandate_id?: string;
  account_ref?: string;
  created_at?: string;
  limits?: LiveMandateLimits;
  /** ISO timestamp the mandate auto-expires (SPEC §7.5 #7 proactive expiry). */
  expires_at?: string;
  expires_in_seconds?: number | null;
  expired?: boolean;
}

/** Runner liveness block of a `GET /live/status` broker entry (SPEC §7.5 #3). */
export interface LiveRunnerLiveness {
  broker?: string;
  alive: boolean;
  /** Unix epoch seconds of the last heartbeat tick; null if the runner never started. */
  last_tick?: number | string | null;
  last_tick_age_seconds?: number | null;
}

export interface LiveBrokerAuthStatus {
  broker: string;
  oauth_token_present: boolean;
  is_live_broker: boolean;
}

/** One broker entry in the `GET /live/status` response. */
export interface LiveBrokerStatus {
  auth: LiveBrokerAuthStatus;
  mandate?: LiveMandateStatus | null;
  runner: LiveRunnerLiveness;
  halted: boolean;
}

/** Response of `GET /live/status` (SPEC §7.5 runner status panel + C2). */
export interface LiveStatus {
  brokers: LiveBrokerStatus[];
  global_halted: boolean;
}

/** Response of `POST /live/runner/start|stop`. */
export interface LiveRunnerResponse {
  broker: string;
  started?: boolean;
  already_running?: boolean;
  stopped?: boolean;
  was_running?: boolean;
}

export interface MessageItem {
  message_id: string;
  session_id: string;
  role: string;
  content: string;
  created_at: string;
  linked_attempt_id?: string;
  metadata?: Record<string, unknown>;
}
