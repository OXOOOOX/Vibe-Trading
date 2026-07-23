/** Chat message types */
import type { ETFReportReadiness } from "@/lib/api";

export type AgentMessageType =
  | "user" | "thinking" | "tool_call" | "tool_result"
  | "answer" | "error" | "run_complete" | "compact" | "swarm_status";

export type SwarmAgentDisplayStatus =
  | "waiting"
  | "running"
  | "done"
  | "failed"
  | "blocked"
  | "retry"
  | "cancelled";

export interface SwarmAgentStatus {
  agentId: string;
  taskId?: string;
  role?: string;
  status: SwarmAgentDisplayStatus;
  tool?: string;
  elapsed_s?: number;
  iterations?: number;
  startedAt?: number;
  lastText?: string;
  error?: string;
  layer?: number;
}

export interface SwarmRunStatus {
  runId: string;
  preset: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled" | "unknown";
  currentLayer: number;
  totalLayers: number;
  startedAt: number;
  completedAt?: number;
  agents: SwarmAgentStatus[];
}

export interface AgentMessage {
  id: string;
  type: AgentMessageType;
  content: string;
  tool?: string;
  args?: Record<string, string>;
  status?: "running" | "ok" | "error";
  elapsed_ms?: number;
  timestamp: number;
  sourceMessageId?: string;
  runId?: string;
  swarmRunId?: string;
  swarmStatus?: SwarmRunStatus;
  metrics?: Record<string, number>;
  equityCurve?: Array<{ time: string; equity: number | string }>;
  /** Phase label for thinking entries */
  stage?: string;
  /** Shadow Account id if render_shadow_report fired in this turn (RunCompleteCard renders a "View Shadow Report" button). */
  shadowId?: string;
  /** Persisted equity Deep Report attached to this answer. */
  reportId?: string;
  reportQualityStatus?: "passed" | "passed_with_gaps" | "failed_validation";
  reportProfile?: "equity_deep_research" | "etf_deep_research" | string;
  reportEtfReadiness?: ETFReportReadiness;
  reportSymbol?: string;
  reportSecurityName?: string;
  reportDataAsOf?: string;
  reportMissingModules?: string[];
  /** True only when the persisted report manifest exposes an available PDF artifact. */
  reportPdfAvailable?: boolean;
  reportGenerationSource?: "manual" | "portfolio_monitor_autopilot" | string;
  reportGenerationReason?: string;
  reportRevision?: number;
  reportParentId?: string;
  reportRevisionMode?: "initial" | "full_refresh" | "section_revision" | "repair" | string;
  reportDeliveryKind?: "report" | "diagnostic";
  reportMarkdownAvailable?: boolean;
  reportDiagnosticAvailable?: boolean;
  reportDiffAvailable?: boolean;
  reportResearchCoverage?: {
    reused_fact_count?: number;
    refreshed_fact_count?: number;
    material_conflicts?: unknown[];
  };
  reportHistoryDelta?: {
    base_report_id?: string | null;
    added?: unknown[];
    updated?: unknown[];
    confirmed?: unknown[];
    stale?: unknown[];
    contradicted?: unknown[];
  };
}

export interface ReportPreviewTarget {
  runId?: string;
  reportId?: string;
  title?: string;
  artifactId?: "markdown" | "diagnostic" | "diff";
}

/** Tool call tracking entry */
export interface ToolCallEntry {
  id: string;
  tool: string;
  arguments: Record<string, string>;
  status: "running" | "ok" | "error";
  preview?: string;
  elapsed_ms?: number;
  /** Live elapsed seconds while the tool is running (heartbeat). */
  elapsed_s?: number;
  /**
   * Structured progress emitted from the tool. All fields optional —
   * presence of `current`/`total > 0` indicates a determinate progress signal.
   */
  progress?: {
    stage?: string;
    current?: number;
    total?: number;
    message?: string;
  };
  timestamp: number;
}
