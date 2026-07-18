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
  reportProfile?: "equity_deep_research";
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

export interface DeepReportRecord {
  schema_version?: number;
  report_id: string;
  session_id: string;
  attempt_id: string;
  profile: "equity_deep_research" | string;
  symbol: string;
  security_name: string;
  report_date: string;
  data_as_of: string;
  quality_status: "passed" | "passed_with_gaps" | "failed_validation";
  status: "running" | "completed" | "failed" | "cancelled" | string;
  analysis_modules: Record<string, DeepReportModule>;
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

export interface RunReportPreview {
  run_id: string;
  title: string;
  filename: string;
  relative_path: string;
  content: string;
  updated_at: string;
  source: "run_artifact" | string;
}

export interface EquityResolutionOption {
  symbol: string;
  security_name: string;
  market?: string | null;
  source?: string | null;
}

export interface EquityResolution {
  status: "resolved" | "ambiguous" | "not_found";
  query: string;
  symbol?: string;
  security_name?: string;
  market?: string | null;
  source?: string | null;
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
  deepReportArtifactUrl: (reportId: string, artifactId: "markdown" | "pdf" | "diagnostic" | "diff") =>
    withAuthQuery(`${BASE}/reports/${encodeURIComponent(reportId)}/artifacts/${artifactId}`),
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
  monitor_auto_deep_report_enabled: boolean;
  effective_monitor_auto_deep_report_enabled: boolean;
  enabled_profiles: string[];
  available_profiles: string[];
  env_path: string;
}

export interface UpdateResearchSettingsRequest {
  deep_report_enabled?: boolean;
  monitor_auto_deep_report_enabled?: boolean;
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
  notes?: string;
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
  watch_scenarios?: MonitorWatchScenario[];
  market_rules: MonitorRule[];
  news_topics: Array<{ semantic_description: string; [key: string]: unknown }>;
  fundamental_monitor: { enabled: boolean; capability_status?: string; [key: string]: unknown };
  hard_valid_until: string;
  evidence_notes?: string[];
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
  } | null;
  plans?: MonitorPlanVersion[];
  display_plan?: MonitorPlanVersion | null;
  watch_episodes?: MonitorWatchEpisode[];
}

export interface MonitorAnalysisRef {
  snapshot_id: string;
  report_ref: string;
  report_type: "single_stock_research" | "holding_analysis" | "daily_portfolio" | "monitor_research";
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
}

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
