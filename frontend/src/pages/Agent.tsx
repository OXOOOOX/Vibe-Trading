import { useTranslation } from 'react-i18next';
import { useEffect, useRef, useState, useMemo, useCallback, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { Send, Loader2, ArrowDown, Square, Download, Plus, Paperclip, X, Users, Target, ChevronDown, Pencil, Check, Play, OctagonX, Activity, Ban, CheckCircle2, Landmark, BookOpen, CalendarRange } from "lucide-react";
import { toast } from "sonner";
import { useAgentStore } from "@/stores/agent";
import { useSSE } from "@/hooks/useSSE";
import { ApiError, AUTH_REQUIRED_MESSAGE, api, isAuthRequiredError, type EquityResolutionOption, type GoalSnapshot, type MandateProposal, type MandateCommitted, type LiveAction, type LiveHalted, type LiveStatus } from "@/lib/api";
import { isReportWorthyRun } from "@/lib/runReports";
import { initialSessionTitle } from "@/lib/sessionTitle";
import { deepReportTitle, etfReadinessMessage } from "@/lib/deepReportPresentation";
import type { AgentMessage, ReportPreviewTarget, ToolCallEntry } from "@/types/agent";
import { AgentAvatar } from "@/components/chat/AgentAvatar";
import { WelcomeScreen, type WelcomeMode } from "@/components/chat/WelcomeScreen";
import { MessageBubble } from "@/components/chat/MessageBubble";
import type { DeepReportTaskStarted } from "@/components/chat/MessageBubble";
import { ThinkingTimeline } from "@/components/chat/ThinkingTimeline";
import { ConversationTimeline } from "@/components/chat/ConversationTimeline";
import { ToolProgressIndicator } from "@/components/chat/ToolProgressIndicator";
import { DeepReportEquityPicker } from "@/components/chat/DeepReportEquityPicker";
import { DeepReportMenuItem } from "@/components/chat/DeepReportMenuItem";
import { ReportPreviewPanel } from "@/components/chat/ReportPreviewPanel";
import { ResearchGoalMenuItem, SwarmTeamMenuItem } from "@/components/chat/ResearchModeMenuItem";
import { MandateProposalCard } from "@/components/chat/MandateProposalCard";
import { RunnerStatus } from "@/components/chat/RunnerStatus";
import { SwarmStatusCard } from "@/components/chat/SwarmStatusCard";
import { SessionUsagePanel } from "@/components/chat/SessionUsagePanel";
import {
  applySwarmEvent,
  buildSwarmStatusFromStarted,
  buildSwarmStatusFromToolResultPreview,
} from "@/lib/swarmStatus";

/* ---------- Message grouping ---------- */
type MsgGroup =
  | { kind: "single"; msg: AgentMessage }
  | { kind: "timeline"; msgs: AgentMessage[] };

function groupMessages(msgs: AgentMessage[]): MsgGroup[] {
  const out: MsgGroup[] = [];
  let buf: AgentMessage[] = [];
  const flush = () => { if (buf.length) { out.push({ kind: "timeline", msgs: [...buf] }); buf = []; } };
  for (const m of msgs) {
    if (["thinking", "tool_call", "tool_result", "compact"].includes(m.type)) {
      buf.push(m);
    } else {
      flush();
      out.push({ kind: "single", msg: m });
    }
  }
  flush();
  return out;
}

const act = () => useAgentStore.getState();

function runningToolCallId(
  toolName: string,
  eventCallId: unknown,
  preference: "first" | "last" = "last",
): string | undefined {
  const callId = typeof eventCallId === "string" ? eventCallId.trim() : "";
  if (callId) return callId;
  const matches = act().toolCalls.filter(
    (toolCall) => toolCall.tool === toolName && toolCall.status === "running",
  );
  return preference === "first" ? matches[0]?.id : matches[matches.length - 1]?.id;
}

// i18n hook for Agent component 鈥?used inside the component below
// (declared at module scope for helper usage is fine since t() reads from i18n singleton)

/** Poll cadence for the shared `GET /live/status` snapshot. */
const LIVE_STATUS_POLL_INTERVAL_MS = 15_000;
const CONNECTOR_CHECK_PROMPT =
  "List my trading connector profiles, show which one is selected, then check that selected connector. If it is not ready, tell me exactly what setup step is missing. Do not place or modify orders.";
const CONNECTOR_PORTFOLIO_PROMPT =
  "Use the selected trading connector profile to summarize my account, positions, concentration, cash, and portfolio risk. Do not place or modify orders.";
const ACTIVE_DEEP_REPORT_PHASES = new Set([
  "queued",
  "resolving",
  "financial_data",
  "financial_standardization",
  "industry_evidence",
  "deterministic_calculation",
  "chapter_writing",
  "monitoring_bundle",
  "compiling",
  "review",
  "repairing",
  "auditing",
  "pdf",
]);

function deepReportTaskStartedMessage(action: DeepReportTaskStarted["action"]): string {
  if (action === "refresh") return "正在使用最新可验证数据重新研究";
  if (action === "enrich") return "正在补齐缺失资料并重新生成报告";
  if (action === "revise") return "正在重写指定报告章节";
  if (action === "repair") return "正在复用现有证据修复失败报告";
  if (action === "archive") return "正在保存报告到 Obsidian";
  return "正在继续研究并补充证据";
}

/* ---------- Connector runtime channel ----------
 * Mandate proposals and live-action chips render as standalone timeline items,
 * never folded into the thinking timeline (SPEC Consent 搂2 grouping note). They
 * are driven by dedicated state rather than the chat message store because they
 * are privileged-surface artifacts, not chat messages, and the proposal card
 * needs commit/adjust callbacks the generic MessageBubble does not carry. */
interface ProposalItem {
  kind: "proposal";
  timestamp: number;
  proposal: MandateProposal;
}
interface LiveActionItem {
  kind: "live_action";
  timestamp: number;
  action: LiveAction;
}
type LiveItem = ProposalItem | LiveActionItem;

interface DeepReportSelection extends EquityResolutionOption {
  request: string;
}

function hasAvailableReportArtifact(value: unknown, artifactId: string): boolean {
  if (!Array.isArray(value)) return false;
  return value.some((artifact) => {
    if (!artifact || typeof artifact !== "object") return false;
    const item = artifact as Record<string, unknown>;
    return String(item.artifact_id || "") === artifactId && item.available === true;
  });
}

function normalizeBrokerScope(broker: string | null | undefined): string | null {
  const normalized = broker?.trim().toLowerCase();
  return normalized || null;
}

function isGlobalLiveHalt(halt: LiveHalted | null): boolean {
  return halt != null && normalizeBrokerScope(halt.broker) == null;
}

function haltScopeStillActive(halt: LiveHalted, status: LiveStatus): boolean {
  const broker = normalizeBrokerScope(halt.broker);
  if (!broker) return status.global_halted;
  return status.global_halted || status.brokers.some((item) => (
    normalizeBrokerScope(item.auth.broker) === broker && item.halted
  ));
}

function liveActionStyle(kind: string): { icon: typeof Activity; tone: string } {
  switch (kind) {
    case "order_rejected":
    case "breach":
      return { icon: Ban, tone: "border-amber-500/40 bg-amber-500/5 text-amber-600 dark:text-amber-400" };
    case "halt_tripped":
      return { icon: OctagonX, tone: "border-destructive/40 bg-destructive/5 text-destructive" };
    case "mandate_committed":
    case "halt_cleared":
      return { icon: CheckCircle2, tone: "border-emerald-500/40 bg-emerald-500/5 text-emerald-600 dark:text-emerald-400" };
    default:
      return { icon: Activity, tone: "border-sky-500/40 bg-sky-500/5 text-sky-600 dark:text-sky-400" };
  }
}

function liveActionLabel(action: LiveAction): string {
  return action.kind.replace(/_/g, " ");
}

function LiveActionChip({ action }: { action: LiveAction }) {
  const { icon: Icon, tone } = liveActionStyle(action.kind);
  return (
    <div className="flex gap-3">
      <AgentAvatar />
      <div className="flex-1 min-w-0">
        <div className={["inline-flex max-w-full flex-wrap items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs", tone].join(" ")}>
          <Icon className="h-3 w-3 shrink-0" />
          <span className="shrink-0 font-medium uppercase tracking-wide text-[10px]">RUNTIME</span>
          <span className="shrink-0 font-medium">{liveActionLabel(action)}</span>
          {action.intent_normalized && (
            <span className="truncate text-foreground/80">路 {action.intent_normalized}</span>
          )}
          {action.outcome && (
            <span className="shrink-0 font-mono text-[10px] text-muted-foreground">路 {action.outcome}</span>
          )}
          {action.remote_tool && (
            <span className="shrink-0 font-mono text-[10px] text-muted-foreground">路 {action.remote_tool}</span>
          )}
          {action.error && <span className="truncate text-destructive">路 {action.error}</span>}
        </div>
      </div>
    </div>
  );
}


function isCriterionStatusMet(status: string): boolean {
  return !["", "pending", "open", "unsatisfied"].includes(status.toLowerCase());
}

function getGoalProgress(snapshot: GoalSnapshot | null): {
  met: number;
  total: number;
  label: string;
  metLabel: string;
  evidenceTotal: number;
} {
  const total = snapshot?.criteria.length ?? 0;
  const met = snapshot?.criteria.filter((item) => criterionCovered(snapshot, item)).length ?? 0;
  const evidenceTotal = snapshot?.evidence_count ?? 0;
  return {
    met,
    total,
    label: total > 0 ? `${met}/${total}` : "",
    metLabel: total > 0 ? `${met}/${total} met` : "",
    evidenceTotal,
  };
}

function statusLabel(status: string): string {
  return status.replace(/_/g, " ");
}

function isTerminalGoalStatus(status: string): boolean {
  return ["complete", "cancelled", "blocked", "superseded", "usage_limited"].includes(status);
}

function criterionIndexLabel(index: number): string {
  return String(index + 1);
}

function criterionEvidenceCount(snapshot: GoalSnapshot, criterionId: string): number {
  return snapshot.evidence.filter((item) => item.criterion_id === criterionId).length;
}

function criterionCovered(snapshot: GoalSnapshot, criterion: GoalSnapshot["criteria"][number]): boolean {
  return isCriterionStatusMet(criterion.status) || criterionEvidenceCount(snapshot, criterion.criterion_id) > 0;
}

function latestGoalEvidence(snapshot: GoalSnapshot) {
  return [...snapshot.evidence]
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
    .slice(0, 2);
}

function goalKickoffPrompt(objective: string): string {
  return [
    "Start working on this research goal now.",
    "Keep it research-only, use available tools when evidence is needed, add concrete evidence to the goal ledger, and keep going until the goal is complete, blocked, waiting for user input, or budget-limited.",
    "",
    `Goal: ${objective}`,
  ].join("\n");
}

function goalContinuePrompt(snapshot: GoalSnapshot): string {
  const openCriteria = snapshot.criteria
    .filter((item) => item.required && !criterionCovered(snapshot, item))
    .map((item) => `- ${item.text}`)
    .join("\n");
  return [
    "Continue the active research goal.",
    "Use real available tools as needed, add evidence to the goal ledger, and only stop when the goal is complete, blocked, waiting for user input, or budget-limited.",
    "",
    `Goal: ${snapshot.goal.objective}`,
    openCriteria ? `Open criteria:\n${openCriteria}` : "All criteria appear covered; audit the ledger and update the goal status if completion is justified.",
  ].join("\n");
}

/* ---------- Component ---------- */
export function Agent() {
  const { t } = useTranslation();
  const [input, setInput] = useState("");
  const [searchParams, setSearchParams] = useSearchParams();
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const isComposingRef = useRef(false);
  const lastCompositionEndRef = useRef(0);
  const sseSessionRef = useRef<string | null>(null);
  const prevSseStatusRef = useRef<string>("disconnected");
  const genRef = useRef(0);
  const pendingGoalSessionRef = useRef<string | null>(null);
  const legacyToolCallSequenceRef = useRef(0);
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  const lastEventRef = useRef(0);
  const sseTimeoutMsRef = useRef(90_000);

  /* tool_progress coalescing — keep latest payload per invocation, flush once per rAF. */
  const pendingProgressRef = useRef<Map<string, NonNullable<ToolCallEntry["progress"]>>>(new Map());
  const progressRafRef = useRef(0);

  const [attachment, setAttachment] = useState<{ filename: string; filePath: string } | null>(null);
  const [uploading, setUploading] = useState(false);
  const [showUploadMenu, setShowUploadMenu] = useState(false);
  const uploadMenuRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [swarmPreset, setSwarmPreset] = useState<{ name: string; title: string } | null>(null);
  const [goalComposerActive, setGoalComposerActive] = useState(false);
  const [goalDetailsOpen, setGoalDetailsOpen] = useState(false);
  const [goalSnapshot, setGoalSnapshot] = useState<GoalSnapshot | null>(null);
  const [goalEditActive, setGoalEditActive] = useState(false);
  const [goalEditValue, setGoalEditValue] = useState("");
  const [deepReportMode, setDeepReportMode] = useState(false);
  const [deepReportResolution, setDeepReportResolution] = useState<DeepReportSelection | null>(null);
  const [deepReportCandidates, setDeepReportCandidates] = useState<EquityResolutionOption[]>([]);
  const [deepReportProgress, setDeepReportProgress] = useState<{
    reportId?: string;
    phase: string;
    message: string;
    qualityStatus?: string;
  } | null>(null);
  const [reportPreviewTarget, setReportPreviewTarget] = useState<ReportPreviewTarget | null>(null);
  const activeDeepReportAttemptRef = useRef<string | null>(null);

  /* Connector runtime channel state (SPEC Consent 搂1/搂4/搂5) */
  const [liveItems, setLiveItems] = useState<LiveItem[]>([]);
  const [committedMandates, setCommittedMandates] = useState<Record<string, MandateCommitted>>({});
  const [liveHalted, setLiveHalted] = useState<LiveHalted | null>(null);
  const [halting, setHalting] = useState(false);
  /* Bumped to force an immediate live-status re-poll on a live event
   * (commit / halt / resume / runner-affecting action) rather than waiting a tick. */
  const [liveStatusRefresh, setLiveStatusRefresh] = useState(0);
  /* Shared `GET /live/status` snapshot. Owned here (single poller) and passed down
   * to RunnerStatus, so the global kill switch can be shown whenever connector runtime
   * could be active out-of-band (CLI/another session), not only off in-session SSE
   * items (audit M2: always-available global halt 鈥?SPEC Consent 搂4). */
  const [liveStatus, setLiveStatus] = useState<LiveStatus | null>(null);
  const [reasoningActive, setReasoningActive] = useState(false);
  const [usageRefreshSignal, setUsageRefreshSignal] = useState(0);
  const [usageForceRefreshSignal, setUsageForceRefreshSignal] = useState(0);
  /* The status endpoint is not wired on every backend; a 404/501 hides the panel
   * and removes status from the kill-switch visibility condition. */
  const [liveStatusUnavailable, setLiveStatusUnavailable] = useState(false);

  const messages = useAgentStore(s => s.messages);
  const streamingText = useAgentStore(s => s.streamingText);
  const status = useAgentStore(s => s.status);
  const sessionId = useAgentStore(s => s.sessionId);
  const toolCalls = useAgentStore(s => s.toolCalls);
  const sessionLoading = useAgentStore(s => s.sessionLoading);

  const { connect, disconnect, onStatusChange } = useSSE();

  const urlSessionId = searchParams.get("session");

  useEffect(() => {
    setReportPreviewTarget(null);
    activeDeepReportAttemptRef.current = null;
  }, [urlSessionId]);

  /* Smart scroll 鈥?only auto-scroll when near bottom */
  const isNearBottom = useCallback(() => {
    const el = listRef.current;
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight < 100;
  }, []);

  const rafRef = useRef(0);
  const scrollToBottom = useCallback(() => {
    if (!isNearBottom()) {
      setShowScrollBtn(true);
      return;
    }
    cancelAnimationFrame(rafRef.current);
    rafRef.current = requestAnimationFrame(() => {
      if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
    });
  }, [isNearBottom]);

  const forceScrollToBottom = useCallback(() => {
    setShowScrollBtn(false);
    requestAnimationFrame(() => {
      if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
    });
  }, []);

  /* Track scroll position to show/hide scroll button */
  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const onScroll = () => {
      if (isNearBottom()) setShowScrollBtn(false);
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [isNearBottom]);

  useEffect(() => {
    onStatusChange((s) => {
      act().setSseStatus(s);
      if (s === "reconnecting" && prevSseStatusRef.current === "connected") toast.warning(t('agent.connectionLostReconnect'));
      else if (s === "connected" && prevSseStatusRef.current === "reconnecting") toast.success(t('agent.connectionRestored'));
      prevSseStatusRef.current = s;
    });
  }, [onStatusChange]);

  const doDisconnect = useCallback(() => {
    disconnect();
    sseSessionRef.current = null;
  }, [disconnect]);

  const loadGoalSnapshot = useCallback(async (sid?: string | null) => {
    const targetSession = sid || act().sessionId;
    if (!targetSession) {
      setGoalSnapshot(null);
      setGoalDetailsOpen(false);
      setGoalEditActive(false);
      return;
    }
    try {
      const snapshot = await api.getGoal(targetSession);
      if (act().sessionId !== targetSession) return;
      setGoalSnapshot(snapshot);
    } catch (error) {
      if (act().sessionId !== targetSession) return;
      if (error instanceof ApiError && error.status === 404) {
        setGoalSnapshot(null);
        setGoalDetailsOpen(false);
        setGoalEditActive(false);
      } else {
        toast.error(error instanceof Error ? error.message : t('agent.failedToLoadGoal'));
      }
    }
  }, []);

  const loadSessionMessages = useCallback(async (sid: string, gen: number) => {
    try {
      const msgs = await api.getSessionMessages(sid);
      if (genRef.current !== gen) return;
      const agentMsgs: AgentMessage[] = [];
      for (const m of msgs) {
        const meta = m.metadata as Record<string, unknown> | undefined;
        const runId = meta?.run_id as string | undefined;
        const metrics = meta?.metrics as Record<string, number> | undefined;
        const reportId = meta?.report_id as string | undefined;
        const reportQualityStatus = meta?.report_quality_status as AgentMessage["reportQualityStatus"];
        const reportProfile = meta?.report_profile as AgentMessage["reportProfile"];
        const reportEtfReadiness = meta?.report_etf_readiness as AgentMessage["reportEtfReadiness"];
        const reportSymbol = meta?.report_symbol as string | undefined;
        const reportSecurityName = meta?.report_security_name as string | undefined;
        const reportDataAsOf = meta?.report_data_as_of as string | undefined;
        const reportMissingModules = meta?.report_missing_modules as string[] | undefined;
        const reportPdfAvailable = hasAvailableReportArtifact(meta?.report_artifacts, "pdf");
        const reportMarkdownAvailable = hasAvailableReportArtifact(meta?.report_artifacts, "markdown");
        const reportDiagnosticAvailable = hasAvailableReportArtifact(meta?.report_artifacts, "diagnostic");
        const reportDiffAvailable = hasAvailableReportArtifact(meta?.report_artifacts, "diff");
        const reportGenerationSource = meta?.report_generation_source as string | undefined;
        const reportGenerationReason = meta?.report_generation_reason as string | undefined;
        const reportRevision = typeof meta?.report_revision === "number" ? meta.report_revision : undefined;
        const reportParentId = meta?.report_parent_id as string | undefined;
        const reportRevisionMode = meta?.report_revision_mode as AgentMessage["reportRevisionMode"];
        const reportDeliveryKind = meta?.report_delivery_kind as AgentMessage["reportDeliveryKind"];
        const reportResearchCoverage = meta?.report_research_coverage as AgentMessage["reportResearchCoverage"];
        const reportHistoryDelta = meta?.report_history_delta as AgentMessage["reportHistoryDelta"];
        const reportMessageMeta = {
          runId,
          reportId,
          reportQualityStatus,
          reportProfile,
          reportEtfReadiness,
          reportSymbol,
          reportSecurityName,
          reportDataAsOf,
          reportMissingModules,
          reportPdfAvailable,
          reportMarkdownAvailable,
          reportDiagnosticAvailable,
          reportDiffAvailable,
          reportGenerationSource,
          reportGenerationReason,
          reportRevision,
          reportParentId,
          reportRevisionMode,
          reportDeliveryKind,
          reportResearchCoverage,
          reportHistoryDelta,
        };
        const ts = new Date(m.created_at).getTime();
        if (m.role === "user") {
          agentMsgs.push({ id: m.message_id, sourceMessageId: m.message_id, type: "user", content: m.content, timestamp: ts });
        } else if (runId) {
          // Show text answer first (if non-empty), then chart card
          if (m.content && m.content !== "Strategy execution completed.") {
            agentMsgs.push({ id: m.message_id + "_ans", sourceMessageId: m.message_id, type: "answer", content: m.content, timestamp: ts, ...reportMessageMeta });
          }
          if (metrics && Object.keys(metrics).length > 0) {
            agentMsgs.push({ id: m.message_id, sourceMessageId: m.message_id, type: "run_complete", content: "", runId, metrics, timestamp: ts + 1 });
          } else {
            // Fetch run data to check report-worthiness; show fallback card if fetch fails
            let fetchedMetrics: Record<string, number> | undefined;
            let fetchedCurve: Array<{ time: string; equity: number }> | undefined;
            let showCard = false;
            try {
              const runData = await api.getRun(runId);
              if (isReportWorthyRun(runData)) {
                fetchedMetrics = runData.metrics;
                fetchedCurve = runData.equity_curve?.map((e) => ({ time: e.time, equity: Number(e.equity) }));
                showCard = true;
              }
              // succeeded but not report-worthy (plain chat turn) 鈫?skip card
            } catch {
              // fetch failed (auth/404/network) 鈫?can't tell, show link as fallback
              showCard = true;
            }
            if (showCard) {
              agentMsgs.push({
                id: m.message_id,
                sourceMessageId: m.message_id,
                type: "run_complete",
                content: "",
                runId,
                metrics: fetchedMetrics,
                equityCurve: fetchedCurve,
                timestamp: ts + 1,
              });
            }
          }
        } else {
          agentMsgs.push({ id: m.message_id, sourceMessageId: m.message_id, type: "answer", content: m.content, timestamp: ts, ...reportMessageMeta });
        }
      }
      if (genRef.current !== gen) return;
      act().loadHistory(agentMsgs);
      act().setSessionLoading(false);
      act().cacheSession(sid, agentMsgs);
      setTimeout(() => forceScrollToBottom(), 50);
    } catch {
      act().setSessionLoading(false);
    }
  }, [forceScrollToBottom]);

  const refreshSessionMessages = useCallback(async (sid: string) => {
    const gen = genRef.current + 1;
    genRef.current = gen;
    await loadSessionMessages(sid, gen);
  }, [loadSessionMessages]);

  const syncCompletedAttempt = useCallback(async (sid: string, attemptId?: string) => {
    if (!attemptId) return false;
    for (let i = 0; i < 3; i += 1) {
      try {
        const storedMessages = await api.getSessionMessages(sid);
        const completedMessage = storedMessages.find(
          (message) => message.role === "assistant" && message.linked_attempt_id === attemptId,
        );
        if (completedMessage) {
          if (act().sessionId !== sid) return true;
          const meta = completedMessage.metadata as Record<string, unknown> | undefined;
          const reportId = String(meta?.report_id || "");
          if (reportId) {
            const failedValidation = meta?.report_quality_status === "failed_validation";
            const profile = String(meta?.report_profile || "") || undefined;
            const readiness = meta?.report_etf_readiness as AgentMessage["reportEtfReadiness"];
            setReportPreviewTarget({
              reportId,
              artifactId: failedValidation ? "diagnostic" : "markdown",
              title: deepReportTitle(
                String(meta?.report_security_name || "") || undefined,
                String(meta?.report_symbol || "") || undefined,
                profile,
                String(meta?.report_quality_status || ""),
                readiness,
              ),
            });
          }
          setReasoningActive(false);
          act().clearStreaming();
          act().setStatus("idle");
          useAgentStore.setState({ toolCalls: [] });
          await refreshSessionMessages(sid);
          if (activeDeepReportAttemptRef.current === attemptId) {
            activeDeepReportAttemptRef.current = null;
          }
          return true;
        }
      } catch {
        return false;
      }
      await new Promise<void>((resolve) => window.setTimeout(resolve, 800));
    }
    return false;
  }, [refreshSessionMessages]);

  const setupSSE = useCallback((sid: string) => {
    if (sseSessionRef.current === sid) return;
    disconnect();
    sseSessionRef.current = sid;

    const touch = () => { lastEventRef.current = Date.now(); };

    connect(api.sseUrl(sid, { replay: "active" }), {
      text_delta: (d) => {
        touch();
        setReasoningActive(false);
        act().appendDelta(String(d.delta || ""));
        scrollToBottom();
      },
      reasoning_delta: () => {
        touch();
        setReasoningActive(true);
        if (act().status !== "streaming") act().setStatus("streaming");
        scrollToBottom();
      },
      stream_reset: () => {
        touch();
        setReasoningActive(false);
        act().clearStreaming();
        if (act().status !== "streaming") act().setStatus("streaming");
        scrollToBottom();
      },
      thinking_done: () => { touch(); /* don't flush 鈥?keep streaming text visible */ },

      tool_call: (d) => {
        touch();
        setReasoningActive(false);
        const toolName = String(d.tool || "");
        const backendCallId = String(d.tool_call_id || "").trim();
        const legacyCallId = [
          String(d.attempt_id || "attempt"),
          String(d.iter || "iteration"),
          toolName || "tool",
          String(++legacyToolCallSequenceRef.current),
        ].join(":");
        // Only update toolCalls tracker (no message creation during streaming)
        act().addToolCall({
          id: backendCallId || legacyCallId, tool: toolName,
          arguments: (d.arguments as Record<string, string>) ?? {},
          status: "running", timestamp: Date.now(),
        });
        scrollToBottom();
      },

      tool_result: (d) => {
        touch();
        const toolName = String(d.tool || "");
        const toolCallId = runningToolCallId(toolName, d.tool_call_id, "first");
        if (!toolCallId) return;
        // Drop any in-flight coalesced progress for this invocation.
        pendingProgressRef.current.delete(toolCallId);
        // Only update tracker (no message creation during streaming)
        act().updateToolCall(toolCallId, {
          status: d.status === "ok" ? "ok" : "error",
          preview: String(d.preview || ""),
          elapsed_ms: Number(d.elapsed_ms || 0),
          elapsed_s: undefined,
          progress: undefined,
        });
        if (toolName === "run_swarm") {
          const fallback = buildSwarmStatusFromToolResultPreview(String(d.preview || ""));
          if (fallback && !act().messages.some((m) => m.type === "swarm_status" && m.swarmRunId === fallback.runId)) {
            act().upsertSwarmStatus(fallback);
          }
        }
      },

      tool_heartbeat: (d) => {
        touch();
        // Keep streaming state alive during long-running tools (swarm, backtest)
        if (act().status !== "streaming") act().setStatus("streaming");
        const toolName = String(d.tool || "");
        if (!toolName) return;
        const toolCallId = runningToolCallId(toolName, d.tool_call_id);
        if (!toolCallId) return;
        act().updateToolCall(toolCallId, {
          elapsed_s: Number(d.elapsed_s || 0),
        });
      },

      tool_progress: (d) => {
        touch();
        const toolName = String(d.tool || "");
        if (!toolName) return;
        const toolCallId = runningToolCallId(toolName, d.tool_call_id);
        if (!toolCallId) return;
        const payload: NonNullable<ToolCallEntry["progress"]> = {};
        if (typeof d.stage === "string" && d.stage) payload.stage = d.stage;
        if (typeof d.message === "string" && d.message) payload.message = d.message;
        if (typeof d.current === "number") payload.current = d.current;
        if (typeof d.total === "number") payload.total = d.total;
        // Coalesce: keep latest payload per invocation, flush once per animation frame.
        pendingProgressRef.current.set(toolCallId, payload);
        if (progressRafRef.current) return;
        progressRafRef.current = requestAnimationFrame(() => {
          progressRafRef.current = 0;
          const pending = pendingProgressRef.current;
          if (pending.size === 0) return;
          const store = act();
          for (const [toolCallId, progress] of pending) {
            store.updateToolCall(toolCallId, { progress });
          }
          pending.clear();
        });
      },

      compact: () => { touch(); },

      "usage.updated": () => {
        touch();
        setUsageRefreshSignal((value) => value + 1);
      },

      "attempt.created": () => {
        touch();
        // Backend has created a new attempt 鈥?ensure streaming state is active
        // even if we connected mid-stream (SSE replay / page reload).
        if (act().status !== "streaming") act().setStatus("streaming");
      },

      "attempt.started": () => {
        touch();
        // Backend has begun executing the attempt. Re-affirm streaming state
        // so the UI shows a working indicator for reconnects and fresh loads.
        if (act().status !== "streaming") act().setStatus("streaming");
      },

      "report.started": (d) => {
        const eventAttemptId = String(d.attempt_id || "");
        if (activeDeepReportAttemptRef.current && eventAttemptId && activeDeepReportAttemptRef.current !== eventAttemptId) return;
        touch();
        setDeepReportProgress({
          reportId: String(d.report_id || "") || undefined,
          phase: String(d.phase || "financial_data"),
          message: "正在采集财务、市场和产业证据",
        });
      },

      "report.progress": (d) => {
        const eventAttemptId = String(d.attempt_id || "");
        if (activeDeepReportAttemptRef.current && eventAttemptId && activeDeepReportAttemptRef.current !== eventAttemptId) return;
        touch();
        setDeepReportProgress((current) => ({
          reportId: String(d.report_id || current?.reportId || "") || undefined,
          phase: String(d.phase || current?.phase || "research"),
          message: String(d.message || current?.message || "正在生成深度研究报告"),
        }));
      },

      "report.completed": (d) => {
        const eventAttemptId = String(d.attempt_id || "");
        if (activeDeepReportAttemptRef.current && eventAttemptId && activeDeepReportAttemptRef.current !== eventAttemptId) return;
        touch();
        const qualityStatus = String(d.quality_status || "passed_with_gaps");
        const failedValidation = qualityStatus === "failed_validation";
        const profile = String(d.profile || "") || undefined;
        const readiness = d.etf_readiness as AgentMessage["reportEtfReadiness"];
        const readinessMessage = readiness ? etfReadinessMessage(readiness) : undefined;
        setDeepReportProgress({
          reportId: String(d.report_id || "") || undefined,
          phase: failedValidation ? "failed_validation" : "completed",
          message: readinessMessage || (failedValidation
            ? "校验失败：仅生成诊断结果，未形成正式投资结论"
            : qualityStatus === "passed"
              ? "报告已完成并通过校验"
              : "报告已完成，存在已标明的数据缺口"),
          qualityStatus,
        });
        const reportId = String(d.report_id || "");
        if (reportId) {
          setReportPreviewTarget({
            reportId,
            artifactId: failedValidation ? "diagnostic" : "markdown",
            title: deepReportTitle(
              String(d.security_name || "") || undefined,
              String(d.symbol || "") || undefined,
              profile,
              qualityStatus,
              readiness,
            ),
          });
        }
        if (eventAttemptId && activeDeepReportAttemptRef.current === eventAttemptId) {
          activeDeepReportAttemptRef.current = null;
        }
        if (readiness && readiness.status !== "penetration_ready") toast.warning(readinessMessage);
        else if (qualityStatus === "failed_validation") toast.warning("报告尚未达到发布要求，可查看未发布原因");
        else if (qualityStatus === "passed_with_gaps") toast.warning("报告已完成，部分结论因公开证据不足而保留");
        else toast.success("穿透式深度研究已完成");
      },

      "attempt.completed": async (d) => {
        touch();
        setUsageForceRefreshSignal((value) => value + 1);
        setReasoningActive(false);
        const s = act();
        // Build ThinkingTimeline summary from accumulated toolCalls
        const completedTools = s.toolCalls;
        if (completedTools.length > 0) {
          for (const tc of completedTools) {
            s.addMessage({ id: tc.id + "_call", type: "tool_call", content: "", tool: tc.tool, args: tc.arguments, status: tc.status || "ok", timestamp: tc.timestamp });
            if (tc.elapsed_ms != null) {
              s.addMessage({ id: "", type: "tool_result", content: tc.preview || "", tool: tc.tool, status: tc.status || "ok", elapsed_ms: tc.elapsed_ms, timestamp: tc.timestamp + 1 });
            }
          }
        }

        // Clear streaming text (don't create thinking message)
        s.clearStreaming();

        // Add final answer
        const runDir = String(d.run_dir || "");
        const runId = runDir ? runDir.split(/[/\\]/).pop() : undefined;
        const isDeepReport = Boolean(d.report_id);
        const summary = String(
          isDeepReport
            ? d.delivery_message || "深度研究报告已生成，请打开 Markdown 产物查看。"
            : d.summary || "",
        );
        if (summary) s.addMessage({
          id: "",
          type: "answer",
          content: summary,
          timestamp: Date.now(),
          runId,
          reportId: String(d.report_id || "") || undefined,
          reportQualityStatus: (String(d.report_quality_status || "") || undefined) as AgentMessage["reportQualityStatus"],
          reportProfile: (String(d.report_profile || "") || undefined) as AgentMessage["reportProfile"],
          reportEtfReadiness: d.report_etf_readiness as AgentMessage["reportEtfReadiness"],
          reportSymbol: String(d.report_symbol || "") || undefined,
          reportSecurityName: String(d.report_security_name || "") || undefined,
          reportDataAsOf: String(d.report_data_as_of || "") || undefined,
          reportMissingModules: Array.isArray(d.report_missing_modules)
            ? d.report_missing_modules.map(String)
            : undefined,
          reportPdfAvailable: hasAvailableReportArtifact(d.report_artifacts, "pdf"),
          reportMarkdownAvailable: hasAvailableReportArtifact(d.report_artifacts, "markdown"),
          reportDiagnosticAvailable: hasAvailableReportArtifact(d.report_artifacts, "diagnostic"),
          reportDiffAvailable: hasAvailableReportArtifact(d.report_artifacts, "diff"),
          reportGenerationSource: String(d.report_generation_source || "") || undefined,
          reportGenerationReason: String(d.report_generation_reason || "") || undefined,
          reportRevision: Number.isFinite(Number(d.report_revision)) ? Number(d.report_revision) : undefined,
          reportParentId: String(d.report_parent_id || "") || undefined,
          reportRevisionMode: (String(d.report_revision_mode || "") || undefined) as AgentMessage["reportRevisionMode"],
          reportDeliveryKind: (String(d.report_delivery_kind || "") || undefined) as AgentMessage["reportDeliveryKind"],
          reportResearchCoverage: d.report_research_coverage as AgentMessage["reportResearchCoverage"],
          reportHistoryDelta: d.report_history_delta as AgentMessage["reportHistoryDelta"],
        });

        // Detect Shadow Account id if render_shadow_report fired successfully this turn
        const shadowCall = completedTools.find(
          (tc) => tc.tool === "render_shadow_report" && (tc.status || "ok") === "ok",
        );
        const shadowMatch = shadowCall?.preview?.match(/"shadow_id"\s*:\s*"(shadow_[A-Za-z0-9_]+)"/);
        const shadowId = shadowMatch?.[1];

        // Show RunCompleteCard when the turn produced backtest metrics or a shadow report
        if (runId && !isDeepReport) {
          let runMetrics: Record<string, number> | undefined;
          let runCurve: Array<{ time: string; equity: number }> | undefined;
          let showCard = false;
          try {
            const runData = await api.getRun(runId);
            if (isReportWorthyRun(runData)) {
              runMetrics = runData.metrics;
              runCurve = runData.equity_curve?.map(e => ({ time: e.time, equity: Number(e.equity) }));
              showCard = true;
            }
          } catch {
            showCard = true; // fetch failed 鈫?show link as fallback
          }
          if (showCard || shadowId) {
            s.addMessage({
              id: "", type: "run_complete", content: "", runId,
              metrics: showCard ? runMetrics : undefined,
              equityCurve: showCard ? runCurve : undefined,
              shadowId,
              timestamp: Date.now(),
            });
          }
        } else if (shadowId) {
          s.addMessage({ id: "", type: "run_complete", content: "", shadowId, timestamp: Date.now() });
        }

        // Reset
        s.setStatus("idle");
        const completedAttemptId = String(d.attempt_id || "");
        if (completedAttemptId && activeDeepReportAttemptRef.current === completedAttemptId) {
          activeDeepReportAttemptRef.current = null;
        }
        useAgentStore.setState({ toolCalls: [] });
        scrollToBottom();
      },

      "attempt.failed": (d) => {
        touch();
        setUsageForceRefreshSignal((value) => value + 1);
        setReasoningActive(false);
        act().clearStreaming();
        act().addMessage({ id: "", type: "error", content: String(d.error || "Execution failed"), timestamp: Date.now() });
        act().setStatus("idle");
        const failedAttemptId = String(d.attempt_id || "");
        if (failedAttemptId && activeDeepReportAttemptRef.current === failedAttemptId) {
          activeDeepReportAttemptRef.current = null;
        }
        // Clear stale toolCalls so the next turn's running indicator doesn't
        // briefly show the previous turn's progress before fresh events land.
        useAgentStore.setState({ toolCalls: [] });
        scrollToBottom();
      },

      "attempt.cancelled": () => {
        touch();
        setUsageForceRefreshSignal((value) => value + 1);
        setReasoningActive(false);
        act().clearStreaming();
        act().addMessage({ id: "", type: "answer", content: "Execution cancelled.", timestamp: Date.now() });
        act().setStatus("idle");
        activeDeepReportAttemptRef.current = null;
        useAgentStore.setState({ toolCalls: [] });
        scrollToBottom();
      },

      "goal.created": () => {
        touch();
        loadGoalSnapshot(sid);
      },

      "swarm.started": (d) => {
        touch();
        const status = buildSwarmStatusFromStarted(d);
        if (!status) return;
        act().upsertSwarmStatus(status);
        scrollToBottom();
      },

      "swarm.event": (d) => {
        touch();
        if (act().status !== "streaming") act().setStatus("streaming");
        const runId = String(d.run_id || "");
        const event = d.event;
        if (!runId || !event) return;
        act().updateSwarmStatus(runId, (current) => applySwarmEvent(current, event));
        scrollToBottom();
      },

      "goal.evidence": () => {
        touch();
        loadGoalSnapshot(sid);
      },

      "goal.updated": (d) => {
        touch();
        const snapshot = d.snapshot as GoalSnapshot | undefined;
        const goal = (d.goal as GoalSnapshot["goal"] | undefined) ?? snapshot?.goal;
        if (goal && isTerminalGoalStatus(goal.status)) {
          setGoalSnapshot(null);
          setGoalDetailsOpen(false);
          setGoalEditActive(false);
          return;
        }
        if (snapshot) {
          setGoalSnapshot(snapshot);
          return;
        }
        loadGoalSnapshot(sid);
      },

      "mandate.proposal": (d) => {
        touch();
        const proposal = d as unknown as MandateProposal;
        if (!proposal.proposal_id || !Array.isArray(proposal.profiles)) return;
        setLiveItems((items) => [...items, { kind: "proposal", timestamp: Date.now(), proposal }]);
        scrollToBottom();
      },

      "mandate.committed": (d) => {
        touch();
        const committed = d as unknown as MandateCommitted;
        if (!committed.proposal_id) return;
        setCommittedMandates((prev) => ({ ...prev, [committed.proposal_id as string]: committed }));
        // A fresh mandate may bring up the runner; refresh the runtime panel now.
        setLiveStatusRefresh((n) => n + 1);
        scrollToBottom();
      },

      "live.halted": (d) => {
        touch();
        const halted = d as unknown as LiveHalted;
        // Preemptive kill switch: the server has cancelled resting orders and may have
        // flattened positions (SPEC 搂7.5 #6). Reflect the halted state across surfaces;
        // the RunnerStatus panel re-polls so its per-broker rows show "halted".
        setLiveHalted(halted);
        setLiveStatusRefresh((n) => n + 1);
        toast.warning(t('agent.connectorHalted'));
      },

      "live.resumed": (d) => {
        touch();
        // Kill switch cleared via a privileged surface action (SPEC Consent 搂4);
        // clear the halted banner and re-poll runtime status.
        void d;
        setLiveHalted(null);
        setLiveStatusRefresh((n) => n + 1);
        toast.success(t('agent.connectionRestored'));
      },

      "live.action": (d) => {
        touch();
        const action = d as unknown as LiveAction;
        if (!action.kind) return;
        setLiveItems((items) => [...items, { kind: "live_action", timestamp: Date.now(), action }]);
        if (action.kind === "halt_tripped") setLiveHalted({ broker: action.broker, reason: action.intent_normalized });
        if (action.kind === "halt_cleared") setLiveHalted(null);
        // Mandate-affecting / runner-affecting actions should refresh the runtime panel.
        if (["mandate_committed", "halt_tripped", "halt_cleared"].includes(action.kind)) {
          setLiveStatusRefresh((n) => n + 1);
        }
        scrollToBottom();
      },

      heartbeat: () => {},
      reconnect: (d) => { act().setSseStatus("reconnecting", Number(d.attempt ?? 0)); },
    });
  }, [connect, disconnect, loadGoalSnapshot, scrollToBottom]);

  useEffect(() => {
    const { sessionId: curSid, messages: curMsgs, cacheSession, reset, getCachedSession, switchSession } = act();

    if (urlSessionId && urlSessionId !== curSid) {
      const gen = genRef.current + 1;
      genRef.current = gen;
      doDisconnect();
      // Live-channel timeline items are per-session; clear on switch.
      setLiveItems([]);
      setCommittedMandates({});
      setLiveHalted(null);
      setLiveStatusRefresh((n) => n + 1);
      if (curSid && curMsgs.length > 0) cacheSession(curSid, curMsgs);

      // Atomic switch: cache hit = instant, cache miss = show loading skeleton
      const cached = getCachedSession(urlSessionId);
      switchSession(urlSessionId, cached);
      if (cached) {
        setTimeout(() => forceScrollToBottom(), 50);
      } else {
        loadSessionMessages(urlSessionId, gen);
      }
      setupSSE(urlSessionId);
    } else if (urlSessionId && urlSessionId === curSid && sseSessionRef.current !== urlSessionId) {
      // #229: returning to the SAME session after the page was unmounted (user
      // navigated away and back). The store kept our messages, but the unmount
      // cleanup tore down the SSE stream, so a running attempt stopped updating
      // and the UI looked frozen until the safety timeout fired. Re-hydrate like
      // a reload: reset the transient streaming view first (so replay=active
      // rebuilds the in-flight turn from the backend ring buffer instead of
      // duplicating deltas onto the preserved text), refresh committed history
      // (covers an attempt that finished while we were away), then re-subscribe.
      const gen = genRef.current + 1;
      genRef.current = gen;
      const seed = curMsgs.length > 0 ? curMsgs : getCachedSession(urlSessionId);
      switchSession(urlSessionId, seed);
      loadSessionMessages(urlSessionId, gen);
      setupSSE(urlSessionId);
    } else if (!urlSessionId && curSid) {
      genRef.current += 1;
      doDisconnect();
      setLiveItems([]);
      setCommittedMandates({});
      setLiveHalted(null);
      setLiveStatusRefresh((n) => n + 1);
      if (curSid && curMsgs.length > 0) cacheSession(curSid, curMsgs);
      reset();
    }
  }, [urlSessionId, doDisconnect, loadSessionMessages, setupSSE, forceScrollToBottom]);

  /* Single shared poller for `GET /live/status`. RunnerStatus consumes this snapshot
   * as a prop rather than polling independently, and the global kill switch reads it
   * to stay available whenever connector runtime activity could be active out-of-band. */
  const refreshLiveStatus = useCallback(async () => {
    try {
      const next = await api.getLiveStatus();
      setLiveStatus(next);
      setLiveHalted((current) => (
        current && !haltScopeStillActive(current, next) ? null : current
      ));
      setLiveStatusUnavailable(false);
    } catch (error) {
      // A 404/501 means the runtime endpoint is not wired on this backend; treat the
      // status source as unavailable. Any other failure keeps the last snapshot.
      if (error instanceof ApiError && (error.status === 404 || error.status === 501)) {
        setLiveStatus(null);
        setLiveStatusUnavailable(true);
      }
    }
  }, []);

  useEffect(() => {
    refreshLiveStatus();
    const timer = setInterval(refreshLiveStatus, LIVE_STATUS_POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [refreshLiveStatus]);

  // Force an immediate re-poll when a live event bumps refreshKey (commit/halt/resume).
  useEffect(() => {
    if (liveStatusRefresh > 0) refreshLiveStatus();
  }, [liveStatusRefresh, refreshLiveStatus]);

  useEffect(() => {
    if (!sessionId) {
      setGoalSnapshot(null);
      setGoalDetailsOpen(false);
      return;
    }
    if (pendingGoalSessionRef.current === sessionId) {
      pendingGoalSessionRef.current = null;
      return;
    }
    loadGoalSnapshot(sessionId);
  }, [sessionId, loadGoalSnapshot]);

  useEffect(() => () => doDisconnect(), [doDisconnect]);

  useEffect(() => {
    api.getLLMSettings().then((s) => {
      sseTimeoutMsRef.current = s.sse_timeout_seconds * 1000;
    }).catch(() => {});
  }, []);

  /* Safety timeout: if streaming but no SSE event for sseTimeoutMsRef.current ms, reset to idle */
  useEffect(() => {
    if (status !== "streaming") return;
    // Arm the clock at the start of every streaming turn. Without this, a turn
    // whose very first event never arrives (e.g. the LLM provider hangs before
    // emitting a single token) left lastEventRef at its 0 / stale value, so the
    // guard below short-circuited and the UI hung on "Agent is working鈥?
    // forever. touch() refreshes this on every real event; the no-op heartbeat
    // deliberately does not, so a connection that only keep-alives still trips.
    lastEventRef.current = Date.now();
    const timer = setInterval(() => {
      if (lastEventRef.current && Date.now() - lastEventRef.current > sseTimeoutMsRef.current && act().status === "streaming") {
        setReasoningActive(false);
        act().setStatus("idle");
        toast.warning(t('agent.executionTimedOut'));
      }
    }, 10_000);
    return () => clearInterval(timer);
  }, [status]);

  const runPrompt = async (prompt: string, confirmedDeepReport?: DeepReportSelection) => {
    if (!prompt.trim() || status === "streaming") return;

    if (goalComposerActive) {
      setInput("");
      inputRef.current?.focus();
      try {
        const sid = await ensureGoalSession(prompt);
        const snapshot = await api.createGoal(sid, { objective: prompt });
        setGoalSnapshot(snapshot);
        setGoalComposerActive(false);
        setGoalDetailsOpen(true);
        toast.success(t('agent.researchGoalAttached'));
        const kickoff = goalKickoffPrompt(prompt);
        act().addMessage({ id: "", type: "user", content: kickoff, timestamp: Date.now() });
        act().setStatus("streaming");
        forceScrollToBottom();
        setupSSE(sid);
        const sent = await api.sendMessage(sid, kickoff);
        void syncCompletedAttempt(sid, sent.attempt_id);
      } catch (error) {
        act().setStatus("idle");
        toast.error(error instanceof Error ? error.message : t('agent.failedToStartGoal'));
      }
      return;
    }

    const selectedDeepReportMode = deepReportMode;
    const normalizedPrompt = prompt.trim();
    const selectedResolution = confirmedDeepReport ?? deepReportResolution;
    if (
      selectedDeepReportMode
      && (!selectedResolution || selectedResolution.request !== normalizedPrompt)
    ) {
      setDeepReportCandidates([]);
      setDeepReportProgress({ phase: "resolving", message: "正在确认上市公司与股票代码" });
      try {
        const resolution = await api.resolveDeepReportInstrument(normalizedPrompt);
        const candidates = resolution.status === "resolved" && resolution.symbol && resolution.security_name
          ? [{
              symbol: resolution.symbol,
              security_name: resolution.security_name,
              market: resolution.market,
              source: resolution.source,
              instrument_type: resolution.instrument_type,
            }]
          : resolution.options.slice(0, 5);
        if (candidates.length > 0) {
          setDeepReportCandidates(candidates);
          const message = candidates.length === 1
            ? `已找到 ${candidates[0].security_name}（${candidates[0].symbol}），请确认后开始研究`
            : `已找到 ${candidates.length} 个候选，请选择正确的证券`;
          setDeepReportProgress({ phase: "identity_candidates", message });
          return;
        }
        const message = "没有找到匹配的证券。请换用名称、常用简称或准确代码重试。";
        setDeepReportProgress({ phase: "identity_gap", message });
        toast.error(message);
      } catch (error) {
        const message = error instanceof Error ? error.message : "证券识别失败";
        setDeepReportProgress({ phase: "identity_gap", message });
        toast.error(message);
      }
      return;
    }
    let finalPrompt = prompt;
    if (selectedDeepReportMode && selectedResolution) {
      finalPrompt = (
        `研究对象已由用户确认：${selectedResolution.security_name}（${selectedResolution.symbol}）。\n`
        + `用户原始请求：${prompt}`
      );
    }

    // Swarm mode: let agent auto-select the right preset
    if (swarmPreset) {
      setSwarmPreset(null);
      finalPrompt = `[Swarm Team Mode] Use the swarm tool to assemble the best specialist team for this task. Auto-select the most appropriate preset.\n\n${prompt}`;
    }

    if (attachment) {
      finalPrompt = `[Uploaded file: ${attachment.filename}, path: ${attachment.filePath}]\n\n${finalPrompt}`;
      setAttachment(null);
    }
    setInput("");
    if (selectedDeepReportMode) {
      setDeepReportProgress({ phase: "queued", message: "穿透式深度研究已进入队列" });
    }
    act().addMessage({ id: "", type: "user", content: finalPrompt, timestamp: Date.now() });
    act().setStatus("streaming");
    forceScrollToBottom();
    inputRef.current?.focus();

    try {
      let sid = act().sessionId;
      if (!sid) {
        const session = await api.createSession(initialSessionTitle(
          prompt,
          selectedDeepReportMode && selectedResolution
            ? {
                securityName: selectedResolution.security_name,
                symbol: selectedResolution.symbol,
              }
            : undefined,
        ));
        sid = session.session_id;
        act().setSessionId(sid);
        setSearchParams({ session: sid }, { replace: true });
      }
      setupSSE(sid);
      const sent = await api.sendMessage(
        sid,
        finalPrompt,
        selectedDeepReportMode
          ? {
              responseMode: "deep_report",
              reportProfile: selectedResolution?.instrument_type === "etf"
                ? "etf_deep_research"
                : "equity_deep_research",
            }
          : { responseMode: "chat" },
      );
      if (selectedDeepReportMode) {
        activeDeepReportAttemptRef.current = sent.attempt_id;
        setDeepReportMode(false);
        setDeepReportResolution(null);
        setDeepReportCandidates([]);
      }
      void syncCompletedAttempt(sid, sent.attempt_id);
    } catch (error) {
      act().setStatus("error");
      const message = isAuthRequiredError(error) ? AUTH_REQUIRED_MESSAGE : t('agent.failedToSend');
      toast.error(message);
      act().addMessage({ id: "", type: "error", content: message, timestamp: Date.now() });
    }
  };

  const handleWelcomeDraft = useCallback((draft: string) => {
    setInput(draft);
    requestAnimationFrame(() => inputRef.current?.focus());
  }, []);

  const handleWeeklyReportDraft = useCallback(() => {
    setShowUploadMenu(false);
    setGoalComposerActive(false);
    setSwarmPreset(null);
    setDeepReportMode(false);
    setDeepReportProgress(null);
    setDeepReportResolution(null);
    setDeepReportCandidates([]);
    setInput(t("welcome.quickStart.actions.weeklyReport.prompt"));
    requestAnimationFrame(() => inputRef.current?.focus());
  }, [t]);

  const handleWelcomeModeSelect = useCallback((mode: WelcomeMode) => {
    setShowUploadMenu(false);
    setSwarmPreset(null);
    setGoalComposerActive(mode === "researchGoal");
    setDeepReportMode(mode === "deepReport");
    setDeepReportProgress(null);
    setDeepReportResolution(null);
    setDeepReportCandidates([]);
    requestAnimationFrame(() => inputRef.current?.focus());
  }, []);

  const confirmDeepReportCandidate = (candidate: EquityResolutionOption) => {
    const normalizedPrompt = input.trim();
    if (!normalizedPrompt || status === "streaming") return;
    if (candidate.instrument_type === "index") {
      const message = "已识别为指数；指数专用深度研究模板尚未开放，请改选上市公司或 ETF。";
      setDeepReportProgress({ phase: "identity_gap", message });
      toast.info(message);
      return;
    }
    const selection: DeepReportSelection = { request: normalizedPrompt, ...candidate };
    setDeepReportResolution(selection);
    setDeepReportCandidates([]);
    setDeepReportProgress({
      phase: "identity_confirmed",
      message: `已确认 ${candidate.security_name}（${candidate.symbol}），正在创建研究任务`,
    });
    void runPrompt(normalizedPrompt, selection);
  };

  const ensureGoalSession = useCallback(async (title: string): Promise<string> => {
    let sid = act().sessionId;
    if (sid) return sid;
    const session = await api.createSession(title.slice(0, 50));
    sid = session.session_id;
    pendingGoalSessionRef.current = sid;
    act().setSessionId(sid);
    setSearchParams({ session: sid }, { replace: true });
    setupSSE(sid);
    return sid;
  }, [setSearchParams, setupSSE]);

  const handleSubmit = (e: FormEvent) => { e.preventDefault(); runPrompt(input.trim()); };

  const handleCancel = async () => {
    setReasoningActive(false);
    if (!sessionId) {
      act().setStatus("idle");
      return;
    }
    try {
      await api.cancelSession(sessionId);
      act().setStatus("idle");
      act().clearStreaming();
      useAgentStore.setState({ toolCalls: [] });
      toast.info(t('agent.cancelRequestSent'));
    } catch {
      toast.error(t('agent.cancelFailed'));
    }
  };

  const handleHaltLive = useCallback(async () => {
    if (halting) return;
    setHalting(true);
    try {
      // The kill switch is global and must fire even with no active chat session
      // (e.g. a runner started from the CLI / another session). The backend scopes
      // the SSE broadcast by session_id when present; an empty string is a valid
      // global trip.
      await api.haltLive(sessionId ?? undefined);
      // Preemptive halt: the server trips the kill switch (cancel resting orders +
      // optional flatten per SPEC 搂7.5 #6) and broadcasts live.halted. Reflect
      // optimistically and re-poll the runtime panel so the runner shows stopped.
      setLiveHalted((cur) => cur ?? { broker: null, by: "frontend", tripped_at: new Date().toISOString() });
      setLiveStatusRefresh((n) => n + 1);
      toast.success(t('agent.connectorHalted'));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t('agent.failedToHaltConnector'));
    } finally {
      setHalting(false);
    }
  }, [sessionId, halting]);

  const handleCancelGoal = useCallback(async () => {
    if (!sessionId || !goalSnapshot) return;
    try {
      await api.updateGoalStatus(sessionId, {
        goal_id: goalSnapshot.goal.goal_id,
        expected_goal_id: goalSnapshot.goal.goal_id,
        status: "cancelled",
        recap: "Cancelled from Web UI.",
      });
      setGoalSnapshot(null);
      setGoalDetailsOpen(false);
      toast.success(t('agent.researchGoalCancelled'));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t('agent.failedToCancelGoal'));
    }
  }, [goalSnapshot, sessionId]);

  const handleStartGoalEdit = useCallback(() => {
    if (!goalSnapshot) return;
    setGoalEditValue(goalSnapshot.goal.objective);
    setGoalEditActive(true);
  }, [goalSnapshot]);

  const handleSaveGoalEdit = useCallback(async () => {
    const objective = goalEditValue.trim();
    if (!sessionId || !goalSnapshot || !objective) return;
    try {
      const response = await api.updateGoal(sessionId, {
        goal_id: goalSnapshot.goal.goal_id,
        expected_goal_id: goalSnapshot.goal.goal_id,
        objective,
      });
      setGoalSnapshot(response.snapshot);
      setGoalEditActive(false);
      toast.success(t('agent.researchGoalUpdated'));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t('agent.failedToUpdateGoal'));
    }
  }, [goalEditValue, goalSnapshot, sessionId]);

  const handleContinueGoal = useCallback(async () => {
    if (!sessionId || !goalSnapshot || status === "streaming") return;
    const prompt = goalContinuePrompt(goalSnapshot);
    act().addMessage({ id: "", type: "user", content: prompt, timestamp: Date.now() });
    act().setStatus("streaming");
    forceScrollToBottom();
    inputRef.current?.focus();
    try {
      setupSSE(sessionId);
      const sent = await api.sendMessage(sessionId, prompt);
      void syncCompletedAttempt(sessionId, sent.attempt_id);
    } catch (error) {
      act().setStatus("error");
      const message = isAuthRequiredError(error) ? AUTH_REQUIRED_MESSAGE : t('agent.failedToContinue');
      toast.error(message);
      act().addMessage({ id: "", type: "error", content: message, timestamp: Date.now() });
    }
  }, [forceScrollToBottom, goalSnapshot, sessionId, setupSSE, status, syncCompletedAttempt]);

  const handleRetry = useCallback((errorMsg: AgentMessage) => {
    if (status === "streaming") return;
    const msgs = act().messages;
    const errorIdx = msgs.findIndex(m => m.id === errorMsg.id);
    if (errorIdx === -1) return;
    // Find the most recent user message before this error
    let userContent: string | null = null;
    for (let i = errorIdx - 1; i >= 0; i--) {
      if (msgs[i].type === "user") {
        userContent = msgs[i].content;
        break;
      }
    }
    if (!userContent) return;
    runPrompt(userContent);
  }, [status]);

  const latestUserMessageId = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i].type === "user") return messages[i].id;
    }
    return null;
  }, [messages]);

  const handleEditUserMessage = useCallback(async (msg: AgentMessage, content: string) => {
    if (!sessionId || !msg.sourceMessageId || status === "streaming") return;
    try {
      setupSSE(sessionId);
      const msgIndex = messages.findIndex((candidate) => candidate.id === msg.id);
      act().loadHistory(messages.filter((item) => {
        const itemIndex = messages.findIndex((candidate) => candidate.id === item.id);
        return msgIndex < 0 || itemIndex <= msgIndex;
      }).map((item) => (item.id === msg.id ? { ...item, content, timestamp: Date.now() } : item)));
      act().setStatus("streaming");
      setReasoningActive(false);
      useAgentStore.setState({ toolCalls: [] });
      forceScrollToBottom();
      const sent = await api.editMessage(sessionId, msg.sourceMessageId, content, true);
      if (sent.attempt_id) void syncCompletedAttempt(sessionId, sent.attempt_id);
    } catch (error) {
      act().setStatus("error");
      const message = error instanceof Error ? error.message : t('agent.failedToSend');
      toast.error(message);
      act().addMessage({ id: "", type: "error", content: message, timestamp: Date.now() });
    }
  }, [forceScrollToBottom, messages, sessionId, setupSSE, status, syncCompletedAttempt, t]);

  const handleDeepReportTaskStarted = useCallback((task: DeepReportTaskStarted) => {
    const sid = act().sessionId || sessionId;
    if (!sid) return;
    const createsRevision = task.action === "refresh" || task.action === "enrich" || task.action === "revise" || task.action === "repair";
    activeDeepReportAttemptRef.current = createsRevision ? task.attemptId : null;
    const wasAlreadyStreaming = act().status === "streaming";
    setupSSE(sid);
    setReasoningActive(false);
    act().clearStreaming();
    useAgentStore.setState({ toolCalls: [] });
    act().setStatus("streaming");
    setDeepReportMode(false);
    setDeepReportResolution(null);
    setDeepReportCandidates([]);
    if (createsRevision) {
      setDeepReportProgress((current) => {
        if (wasAlreadyStreaming && current && ACTIVE_DEEP_REPORT_PHASES.has(current.phase)) {
          return current;
        }
        return {
          reportId: task.reportId,
          phase: "queued",
          message: deepReportTaskStartedMessage(task.action),
        };
      });
    }
    window.setTimeout(forceScrollToBottom, 0);
    void syncCompletedAttempt(sid, task.attemptId);
  }, [forceScrollToBottom, sessionId, setupSSE, syncCompletedAttempt]);

  const handleFork = useCallback(async (msg: AgentMessage) => {
    if (!sessionId || !msg.sourceMessageId) return;
    try {
      const fork = await api.forkSession(sessionId, msg.sourceMessageId);
      setSearchParams({ session: fork.session_id });
      toast.success(t('agent.conversationForked'));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t('agent.failedToForkConversation'));
    }
  }, [sessionId, setSearchParams, t]);

  const handleExport = () => {
    if (messages.length === 0) return;
    const lines: string[] = [`# Chat Export`, ``, `Export time: ${new Date().toLocaleString()}`, ``];
    for (const msg of messages) {
      const time = new Date(msg.timestamp).toLocaleString();
      if (msg.type === "user") {
        lines.push(`## User (${time})`, ``, msg.content, ``);
      } else if (msg.type === "answer") {
        lines.push(`## Assistant (${time})`, ``, msg.content, ``);
      } else if (msg.type === "error") {
        lines.push(`## Error (${time})`, ``, msg.content, ``);
      } else if (msg.type === "tool_call") {
        lines.push(`> Tool call: ${msg.tool || "unknown"}`, ``);
      } else if (msg.type === "swarm_status") {
        lines.push(`> Swarm status: ${msg.swarmStatus?.preset || "swarm"} ${msg.swarmStatus?.status || ""}`, ``);
      } else if (msg.type === "run_complete") {
        lines.push(`> Backtest complete: ${msg.runId || ""}`, ``);
      }
    }
    const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `chat_${new Date().toISOString().slice(0, 10)}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleFileSelect = useCallback(async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = "";
    const blockedExts = [
      ".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".app", ".dmg",
      ".so", ".dll", ".dylib",
      ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz",
    ];
    const lowered = file.name.toLowerCase();
    if (blockedExts.some((ext) => lowered.endsWith(ext))) {
      toast.error(t('agent.executablesNotAllowed'));
      return;
    }
    if (file.size > 50 * 1024 * 1024) {
      toast.error(t('agent.fileSizeExceeds'));
      return;
    }
    setUploading(true);
    setShowUploadMenu(false);
    try {
      const result = await api.uploadFile(file);
      setAttachment({ filename: result.filename, filePath: result.file_path });
      toast.success(t('agent.uploaded', { filename: result.filename }));
    } catch (err) {
      toast.error(t('agent.uploadFailed', { error: err instanceof Error ? err.message : 'Unknown error' }));
    } finally {
      setUploading(false);
    }
  }, []);

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (uploadMenuRef.current && !uploadMenuRef.current.contains(e.target as Node)) {
        setShowUploadMenu(false);
      }
    };
    if (showUploadMenu) {
      document.addEventListener("mousedown", handleClickOutside);
      return () => document.removeEventListener("mousedown", handleClickOutside);
    }
  }, [showUploadMenu]);

  const groups = useMemo(() => groupMessages(messages), [messages]);
  const goalProgress = useMemo(() => getGoalProgress(goalSnapshot), [goalSnapshot]);

  /* Merge message groups with live-channel items, ordered by timestamp, so a
   * mandate proposal / live-action chip renders inline at the point it arrived. */
  type TimelineRow =
    | { sort: number; render: "group"; group: MsgGroup; key: string }
    | { sort: number; render: "live"; item: LiveItem; key: string };
  const timelineRows = useMemo<TimelineRow[]>(() => {
    const rows: TimelineRow[] = groups.map((g, i) => {
      const ts = g.kind === "timeline" ? g.msgs[0].timestamp : g.msg.timestamp;
      const key = g.kind === "timeline" ? `g_${g.msgs[0].id || g.msgs[0].timestamp}` : `g_${g.msg.id || g.msg.timestamp}_${i}`;
      return { sort: ts, render: "group", group: g, key };
    });
    for (const item of liveItems) {
      const key = item.kind === "proposal" ? `lp_${item.proposal.proposal_id}` : `la_${item.action.audit_id || item.timestamp}`;
      rows.push({ sort: item.timestamp, render: "live", item, key });
    }
    return rows.sort((a, b) => a.sort - b.sort);
  }, [groups, liveItems]);

  /* Whether connector runtime activity could be active *anywhere* 鈥?the global kill switch must be
   * available whenever it could (audit M2 / SPEC Consent 搂4). Driven off both
   * in-session SSE artifacts AND the shared `/live/status` snapshot, so a runner
   * started from the CLI or another browser session still surfaces the halt button
   * in a freshly-loaded web session. */
  const liveStatusActive =
    liveStatus != null &&
    (liveStatus.global_halted ||
      liveStatus.brokers.some((b) => b.auth.oauth_token_present || b.runner?.alive || b.mandate != null));
  const liveActive =
    liveItems.length > 0 ||
    Object.keys(committedMandates).length > 0 ||
    liveHalted != null ||
    liveStatusActive;
  /* The global kill switch reflects only a global halt from either an in-session SSE
   * event or the polled status; broker-scoped halts stay on their broker row. */
  const liveIsHalted = isGlobalLiveHalt(liveHalted) || (liveStatus?.global_halted ?? false);

  return (
    <div className="relative flex h-full flex-1 min-w-0 overflow-hidden">
      <SessionUsagePanel
        sessionId={sessionId}
        running={status === "streaming"}
        refreshSignal={usageRefreshSignal}
        forceRefreshSignal={usageForceRefreshSignal}
      />
      <div className="flex h-full flex-1 min-w-0 flex-col overflow-hidden">
      <div ref={listRef} className="flex-1 overflow-auto p-6 scroll-smooth relative">
        <div className="max-w-3xl mx-auto space-y-4">
          {sessionLoading && (
            <div className="space-y-4 py-4">
              {[1, 2, 3].map(i => (
                <div key={i} className="flex gap-3 animate-pulse">
                  <div className="h-8 w-8 rounded-full bg-muted shrink-0" />
                  <div className="flex-1 space-y-2">
                    <div className="h-4 bg-muted rounded w-3/4" />
                    <div className="h-3 bg-muted/60 rounded w-1/2" />
                  </div>
                </div>
              ))}
            </div>
          )}
          {!sessionLoading && messages.length === 0 && (
            <WelcomeScreen
              onExample={runPrompt}
              onDraft={handleWelcomeDraft}
              onModeSelect={handleWelcomeModeSelect}
            />
          )}

          {timelineRows.map((row, rowIdx) => {
            if (row.render === "live") {
              if (row.item.kind === "proposal") {
                return (
                  <MandateProposalCard
                    key={row.key}
                    proposal={row.item.proposal}
                    committed={committedMandates[row.item.proposal.proposal_id] ?? null}
                    onAdjust={runPrompt}
                  />
                );
              }
              return <LiveActionChip key={row.key} action={row.item.action} />;
            }
            const g = row.group;
            if (g.kind === "timeline") {
              const isLastRow = rowIdx === timelineRows.length - 1;
              return (
                <ThinkingTimeline
                  key={row.key}
                  messages={g.msgs}
                  isLatest={isLastRow && status === "streaming"}
                />
              );
            }
            const msgIdx = messages.indexOf(g.msg);
            if (g.msg.type === "swarm_status" && g.msg.swarmStatus) {
              return (
                <div key={row.key} data-msg-idx={msgIdx}>
                  <SwarmStatusCard status={g.msg.swarmStatus} />
                </div>
              );
            }
            return (
              <div key={row.key} data-msg-idx={msgIdx}>
                <MessageBubble
                  msg={g.msg}
                  onRetry={g.msg.type === "error" ? handleRetry : undefined}
                  onFork={g.msg.type === "answer" ? handleFork : undefined}
                  canEdit={g.msg.type === "user" && g.msg.id === latestUserMessageId && status !== "streaming"}
                  onEdit={handleEditUserMessage}
                  onPreviewReport={setReportPreviewTarget}
                  deepReportBusy={status === "streaming"}
                  onDeepReportTaskStarted={handleDeepReportTaskStarted}
                />
              </div>
            );
          })}

          {/* Pre-stream placeholder: visible after Send, before first SSE event */}
          {status === "streaming" && !reasoningActive && !streamingText && toolCalls.length === 0 && !messages.some((m) => m.type === "swarm_status" && m.swarmStatus?.status === "running") && (
            <div className="flex gap-3">
              <AgentAvatar />
              <div className="flex-1 min-w-0 flex items-center gap-2 text-xs text-muted-foreground pt-1">
                <Loader2 className="h-3 w-3 animate-spin text-primary shrink-0" />
                <span>{t('agent.agentWorking')}</span>
              </div>
            </div>
          )}

          {/* Live streaming area: text + tool status */}
          {(streamingText || reasoningActive || (status === "streaming" && toolCalls.length > 0)) && (
            <div className="flex gap-3">
              <AgentAvatar />
              <div className="flex-1 min-w-0 space-y-1.5">
                {reasoningActive && !streamingText && (
                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <Loader2 className="h-3 w-3 animate-spin text-primary shrink-0" />
                    <span>{t('agent.reasoning')}</span>
                  </div>
                )}
                {streamingText && (
                  <div className="prose prose-sm dark:prose-invert max-w-none leading-relaxed">
                    {activeDeepReportAttemptRef.current
                      ? "正在收集并核验财务、行业和市场资料…"
                      : streamingText}
                    <span className="inline-block w-0.5 h-4 bg-primary ml-0.5 animate-pulse align-middle" />
                  </div>
                )}
                {status === "streaming" && toolCalls.length > 0 && (
                  <ToolProgressIndicator toolCalls={toolCalls} />
                )}
              </div>
            </div>
          )}

          {/* Persistent streaming pulse bar 鈥?always visible while agent is working */}
          {status === "streaming" && (
            <div className="flex items-center gap-2 px-1 pt-1">
              <div className="h-0.5 flex-1 rounded-full bg-primary/20 overflow-hidden">
                <div className="h-full w-1/3 bg-primary rounded-full animate-[pulse-slide_2s_ease-in-out_infinite]" />
              </div>
              <span className="text-[10px] text-muted-foreground shrink-0 tabular-nums">{t('agent.running')}</span>
            </div>
          )}

        </div>

        {/* Scroll to bottom button */}
        {showScrollBtn && (
          <button
            onClick={forceScrollToBottom}
            className="sticky bottom-4 left-1/2 -translate-x-1/2 flex items-center gap-1 px-3 py-1.5 rounded-full bg-primary text-primary-foreground text-xs font-medium shadow-lg hover:opacity-90 transition-opacity z-10"
          >
            <ArrowDown className="h-3 w-3" /> {t('agent.newMessages')}
          </button>
        )}
        <ConversationTimeline messages={messages} containerRef={listRef} />
      </div>

      <form onSubmit={handleSubmit} className="border-t p-4 bg-background/80 backdrop-blur-sm">
        <div className="max-w-3xl mx-auto space-y-2">
          {/* Swarm preset badge */}
          {swarmPreset && (
            <div className="flex items-center gap-1">
              <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-violet-500/10 text-violet-600 dark:text-violet-400 text-xs font-medium">
                <Users className="h-3 w-3" />
                {swarmPreset.title}
                <button type="button" onClick={() => setSwarmPreset(null)} className="hover:text-destructive transition-colors">
                  <X className="h-3 w-3" />
                </button>
              </span>
            </div>
          )}
          {(deepReportMode || deepReportProgress) && (
            <div className="flex items-center gap-1">
              <span className="inline-flex max-w-full items-center gap-1.5 rounded-lg bg-cyan-500/10 px-2.5 py-1 text-xs font-medium text-cyan-700 dark:text-cyan-300">
                {deepReportProgress && ["queued", "resolving", "financial_data", "financial_standardization", "industry_evidence", "deterministic_calculation", "chapter_writing", "monitoring_bundle", "compiling", "review", "repairing", "auditing", "pdf"].includes(deepReportProgress.phase) ? (
                  <Loader2 className="h-3 w-3 shrink-0 animate-spin" />
                ) : (
                  <BookOpen className="h-3 w-3 shrink-0" />
                )}
                <span className="truncate">
                  {deepReportProgress?.message || "单股穿透式深度研究"}
                </span>
                {deepReportProgress?.qualityStatus && (
                  <span className="shrink-0 font-mono text-[10px]">{deepReportProgress.qualityStatus}</span>
                )}
                <button
                  type="button"
                  onClick={() => { setDeepReportMode(false); setDeepReportProgress(null); setDeepReportResolution(null); setDeepReportCandidates([]); }}
                  className="shrink-0 hover:text-destructive"
                  aria-label="关闭穿透式研究状态"
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            </div>
          )}
          <DeepReportEquityPicker
            candidates={deepReportCandidates}
            disabled={status === "streaming"}
            onConfirm={confirmDeepReportCandidate}
          />
          {goalComposerActive && (
            <div className="flex items-center gap-1">
              <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-primary/10 text-primary text-xs font-medium">
                <Target className="h-3 w-3" />
                {t('agent.newResearchGoal')}
                <button type="button" onClick={() => setGoalComposerActive(false)} className="hover:text-destructive transition-colors">
                  <X className="h-3 w-3" />
                </button>
              </span>
            </div>
          )}
          {goalSnapshot && !goalComposerActive && (
            <div className="grid gap-2">
              <button
                type="button"
                onClick={() => setGoalDetailsOpen((open) => !open)}
                className="inline-flex max-w-full items-center gap-1.5 justify-self-start rounded-lg bg-primary/10 px-2.5 py-1 text-left text-xs font-medium text-primary transition-colors hover:bg-primary/15"
                title={goalSnapshot.goal.objective}
                aria-label="Active research goal"
                aria-expanded={goalDetailsOpen}
              >
                <Target className="h-3 w-3 shrink-0" />
                <span className="shrink-0">{t('agent.goal')}</span>
                <span className="truncate text-muted-foreground">
                  {goalSnapshot.goal.ui_summary || goalSnapshot.goal.objective}
                </span>
                {goalProgress.metLabel && (
                  <span className="shrink-0 font-mono text-[11px] text-emerald-600 dark:text-emerald-400">
                    {goalProgress.metLabel}
                  </span>
                )}
                {goalProgress.evidenceTotal > 0 && (
                  <span className="shrink-0 rounded bg-background px-1 font-mono text-[10px] text-primary" title="Evidence collected toward this research goal">
                    {goalProgress.evidenceTotal} evidence
                  </span>
                )}
                <ChevronDown
                  className={[
                    "h-3 w-3 shrink-0 transition-transform",
                    goalDetailsOpen ? "rotate-180" : "",
                  ].join(" ")}
                  aria-hidden="true"
                />
              </button>
              {goalDetailsOpen && (
                <div className="grid gap-3 rounded-xl border border-primary/20 bg-background/95 p-3 text-xs shadow-sm">
                  {goalEditActive ? (
                    <div className="grid gap-2">
                      <textarea
                        value={goalEditValue}
                        onChange={(event) => setGoalEditValue(event.target.value)}
                        rows={3}
                        className="w-full rounded-lg border bg-background px-3 py-2 text-xs leading-relaxed text-foreground outline-none focus:ring-2 focus:ring-primary/30"
                      />
                      <div className="flex justify-end gap-2">
                        <button
                          type="button"
                          onClick={() => setGoalEditActive(false)}
                          className="inline-flex items-center gap-1 rounded-lg border px-2 py-1 text-[11px] font-medium text-muted-foreground transition-colors hover:text-foreground"
                        >
                          <X className="h-3 w-3" />
                          Cancel
                        </button>
                        <button
                          type="button"
                          onClick={handleSaveGoalEdit}
                          disabled={!goalEditValue.trim()}
                          className="inline-flex items-center gap-1 rounded-lg bg-primary px-2 py-1 text-[11px] font-medium text-primary-foreground transition-opacity disabled:opacity-40"
                        >
                          <Check className="h-3 w-3" />
                          Save
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="rounded-lg border bg-muted/20 px-3 py-2 text-[11px] leading-relaxed text-muted-foreground">
                      {goalSnapshot.goal.objective}
                    </div>
                  )}
                  <div className="grid grid-cols-2 gap-2">
                    <div className="rounded-lg border bg-muted/20 p-2.5">
                      <div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                        Criteria
                      </div>
                      <div className="mt-1 font-mono text-base font-semibold text-foreground">
                        {goalProgress.label || "0/0"}
                      </div>
                    </div>
                    <div className="rounded-lg border bg-muted/20 p-2.5">
                      <div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                        Evidence
                      </div>
                      <div className="mt-1 font-mono text-base font-semibold text-foreground">
                        {goalProgress.evidenceTotal}
                      </div>
                    </div>
                  </div>
                  <div className="grid gap-1.5">
                    {goalSnapshot.criteria.map((criterion, index) => {
                      const evidenceCount = criterionEvidenceCount(goalSnapshot, criterion.criterion_id);
                      const displayStatus = criterionCovered(goalSnapshot, criterion) && !isCriterionStatusMet(criterion.status)
                        ? "covered"
                        : statusLabel(criterion.status);
                      return (
                        <div
                          key={criterion.criterion_id}
                          className="grid grid-cols-[1.25rem_minmax(0,1fr)_auto] items-start gap-2 rounded-lg border bg-muted/20 p-2"
                        >
                          <span className="flex h-5 w-5 items-center justify-center rounded-full bg-muted text-[10px] text-muted-foreground">
                            {criterionIndexLabel(index)}
                          </span>
                          <span className="min-w-0">
                            <span className="block truncate font-medium text-foreground">{criterion.text}</span>
                            <span className="block text-[11px] text-muted-foreground">
                              {displayStatus}
                            </span>
                          </span>
                          <span className="rounded-full border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                            {evidenceCount} ev
                          </span>
                        </div>
                      );
                    })}
                  </div>
                  {goalSnapshot.evidence.length > 0 && (
                    <div className="grid gap-1.5 border-t pt-2">
                      <div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                        Recent Evidence
                      </div>
                      {latestGoalEvidence(goalSnapshot).map((item) => (
                        <div key={item.evidence_id} className="rounded-lg bg-muted/20 px-2 py-1.5">
                          <div className="mb-0.5 flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
                            <span className="truncate">{item.source_provider || "evidence"}</span>
                            <span>{statusLabel(item.verification_status)}</span>
                          </div>
                          <div className="line-clamp-2 text-[11px] leading-relaxed text-foreground">
                            {item.text}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                  <div className="flex flex-wrap justify-end gap-2 border-t pt-2">
                    <button
                      type="button"
                      onClick={handleContinueGoal}
                      disabled={status === "streaming"}
                      className="inline-flex items-center gap-1 rounded-lg border px-2 py-1 text-[11px] font-medium text-muted-foreground transition-colors hover:text-foreground disabled:opacity-40"
                    >
                      <Play className="h-3 w-3" />
                      Continue
                    </button>
                    <button
                      type="button"
                      onClick={handleStartGoalEdit}
                      disabled={goalEditActive}
                      className="inline-flex items-center gap-1 rounded-lg border px-2 py-1 text-[11px] font-medium text-muted-foreground transition-colors hover:text-foreground disabled:opacity-40"
                    >
                      <Pencil className="h-3 w-3" />
                      Edit
                    </button>
                    <button
                      type="button"
                      onClick={handleCancelGoal}
                      className="inline-flex items-center gap-1 rounded-lg border px-2 py-1 text-[11px] font-medium text-muted-foreground transition-colors hover:border-destructive/40 hover:text-destructive"
                    >
                      <X className="h-3 w-3" />
                      Cancel Goal
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
          {/* Persistent live runtime status panel 鈥?sits alongside the goal/mandate
              badges (SPEC 搂7.5 + audit C2). Self-hides when no broker is configured. */}
          <RunnerStatus
            status={liveStatus}
            unavailable={liveStatusUnavailable}
            halted={liveIsHalted}
            onRefresh={refreshLiveStatus}
          />
          {/* Attachment badge */}
          {attachment && (
            <div className="flex items-center gap-1">
              <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-primary/10 text-primary text-xs font-medium">
                <Paperclip className="h-3 w-3" />
                {attachment.filename}
                <button type="button" onClick={() => setAttachment(null)} className="hover:text-destructive transition-colors">
                  <X className="h-3 w-3" />
                </button>
              </span>
            </div>
          )}
          {/* Uploading indicator */}
          {uploading && (
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />
              {t('agent.uploading')}
            </div>
          )}
          {/* Persistent kill switch 鈥?distinct from the per-turn Stop button
              above; disables all live order activity (SPEC Consent 搂4). */}
          {liveActive && (
            <div className="flex items-center gap-2">
              {liveIsHalted ? (
                <span className="inline-flex items-center gap-1.5 rounded-lg bg-destructive/10 px-2.5 py-1 text-xs font-medium text-destructive">
                  <OctagonX className="h-3 w-3" />
                  {t('agent.connectorRuntimeHalted')}
                </span>
              ) : (
                <button
                  type="button"
                  onClick={handleHaltLive}
                  disabled={halting}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-destructive/40 bg-destructive/5 px-2.5 py-1 text-xs font-medium text-destructive transition-colors hover:bg-destructive/10 disabled:opacity-40"
                  title={t('agent.haltConnectorRuntimeTitle')}
                >
                  {halting ? <Loader2 className="h-3 w-3 animate-spin" /> : <OctagonX className="h-3 w-3" />}
                  {t('agent.haltConnectorRuntime')}
                </button>
              )}
            </div>
          )}
          <div className="flex gap-2 items-end">
            {/* "+" menu: PDF upload + Swarm presets */}
            <div className="relative" ref={uploadMenuRef}>
              <button
                type="button"
                onClick={() => setShowUploadMenu(prev => !prev)}
                disabled={status === "streaming" || uploading}
                className="w-9 h-9 rounded-full border flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-muted transition-colors disabled:opacity-40 shrink-0"
                title={t('agent.moreOptions')}
              >
                <Plus className="h-4 w-4" />
              </button>
              {showUploadMenu && (
                <div className="absolute bottom-full left-0 mb-2 w-52 rounded-xl border bg-background/95 backdrop-blur-sm shadow-lg py-1 z-50">
                  <button
                    type="button"
                    onClick={() => { fileInputRef.current?.click(); setShowUploadMenu(false); }}
                    className="w-full px-3 py-2 text-left text-sm hover:bg-muted transition-colors flex items-center gap-2"
                  >
                    <Paperclip className="h-4 w-4" />
                    {t('agent.uploadPdf')}
                  </button>
                  <div className="border-t my-1" />
                  <ResearchGoalMenuItem
                    onSelect={() => {
                      setShowUploadMenu(false);
                      setSwarmPreset(null);
                      setDeepReportMode(false);
                      setDeepReportResolution(null);
                      setDeepReportCandidates([]);
                      setGoalComposerActive(true);
                      inputRef.current?.focus();
                    }}
                  />
                  <DeepReportMenuItem
                    onSelect={() => {
                      setShowUploadMenu(false);
                      setGoalComposerActive(false);
                      setSwarmPreset(null);
                      setDeepReportMode(true);
                      setDeepReportProgress(null);
                      setDeepReportResolution(null);
                      setDeepReportCandidates([]);
                      inputRef.current?.focus();
                    }}
                  />
                  <button
                    type="button"
                    onClick={handleWeeklyReportDraft}
                    className="w-full px-3 py-2 text-left text-sm hover:bg-muted transition-colors flex items-center gap-2"
                  >
                    <CalendarRange className="h-4 w-4" />
                    {t("agent.weeklyReport")}
                  </button>
                  <SwarmTeamMenuItem
                    onSelect={() => {
                      setShowUploadMenu(false);
                      setGoalComposerActive(false);
                      setDeepReportMode(false);
                      setDeepReportResolution(null);
                      setDeepReportCandidates([]);
                      setSwarmPreset({ name: "auto", title: "Agent Swarm" });
                      inputRef.current?.focus();
                    }}
                  />
                  <div className="border-t my-1" />
                  <button
                    type="button"
                    onClick={() => {
                      setShowUploadMenu(false);
                      void runPrompt(CONNECTOR_CHECK_PROMPT);
                    }}
                    className="w-full px-3 py-2 text-left text-sm hover:bg-muted transition-colors flex items-center gap-2"
                  >
                    <Landmark className="h-4 w-4" />
                    {t('agent.checkConnector')}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setShowUploadMenu(false);
                      void runPrompt(CONNECTOR_PORTFOLIO_PROMPT);
                    }}
                    className="w-full px-3 py-2 text-left text-sm hover:bg-muted transition-colors flex items-center gap-2"
                  >
                    <Landmark className="h-4 w-4" />
                    {t('agent.analyzePortfolio')}
                  </button>
                </div>
              )}
            </div>
            <input
              ref={fileInputRef}
              type="file"
              accept=".pdf,.docx,.xlsx,.xls,.pptx,.csv,.tsv,.txt,.md,.log,.json,.yaml,.yml,.toml,.html,.xml,.rst,.png,.jpg,.jpeg,.gif,.bmp,.webp,.tiff"
              onChange={handleFileSelect}
              className="hidden"
            />
            <textarea
              ref={inputRef}
              value={input}
              rows={1}
              onChange={(e) => {
                setInput(e.target.value);
                setDeepReportCandidates([]);
                if (deepReportResolution?.request !== e.target.value.trim()) {
                  setDeepReportResolution(null);
                  if (deepReportMode) setDeepReportProgress(null);
                }
              }}
              onCompositionStart={() => {
                isComposingRef.current = true;
              }}
              onCompositionEnd={() => {
                isComposingRef.current = false;
                lastCompositionEndRef.current = Date.now();
              }}
              onInput={(e) => {
                const el = e.target as HTMLTextAreaElement;
                el.style.height = "auto";
                el.style.height = el.scrollHeight + "px";
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  const nativeEvent = e.nativeEvent as KeyboardEvent & { isComposing?: boolean };
                  const justFinishedComposing = Date.now() - lastCompositionEndRef.current < 80;
                  if (isComposingRef.current || nativeEvent.isComposing || nativeEvent.keyCode === 229) {
                    return;
                  }
                  if (justFinishedComposing) {
                    e.preventDefault();
                    return;
                  }
                  e.preventDefault();
                  runPrompt(input.trim());
                }
              }}
              placeholder={
                goalComposerActive
                  ? t('agent.describeGoal')
                  : deepReportMode
                    ? "输入一家上市公司名称或准确股票代码，生成穿透式深度研究"
                  : t('agent.placeholder')
              }
              className="flex-1 px-4 py-2.5 rounded-xl border bg-background text-sm focus:outline-none focus:ring-2 focus:ring-primary/40 transition-shadow resize-none max-h-32 overflow-y-auto"
              disabled={status === "streaming"}
            />
            {messages.length > 0 && (
              <button
                type="button"
                onClick={handleExport}
                className="px-3 py-2.5 rounded-xl border text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
                title={t('agent.exportChat')}
              >
                <Download className="h-4 w-4" />
              </button>
            )}
            {status === "streaming" ? (
              <button
                type="button"
                onClick={handleCancel}
                className="px-4 py-2.5 rounded-xl bg-destructive text-destructive-foreground text-sm font-medium hover:opacity-90 transition-opacity"
                title={t('agent.stopGeneration')}
              >
                <Square className="h-4 w-4" />
              </button>
            ) : (
              <button
                type="submit"
                disabled={goalComposerActive ? !input.trim() : (!input.trim() && !attachment)}
                className="px-4 py-2.5 rounded-xl bg-primary text-primary-foreground text-sm font-medium disabled:opacity-40 hover:opacity-90 transition-opacity"
              >
                <Send className="h-4 w-4" />
              </button>
            )}
          </div>
        </div>
      </form>
      </div>
      {reportPreviewTarget && (
        <ReportPreviewPanel
          target={reportPreviewTarget}
          onClose={() => setReportPreviewTarget(null)}
        />
      )}
    </div>
  );
}
