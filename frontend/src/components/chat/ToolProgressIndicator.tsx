import { useRef, type JSX } from "react";
import { useTranslation } from "react-i18next";
import { Loader2, CheckCircle2, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";
import { ProgressBar } from "@/components/chat/ProgressBar";
import { localizeToolName, localizeToolProgressMessage, localizeToolStage } from "@/lib/tools";
import type { ToolCallEntry } from "@/types/agent";

/* ---------- ETA tracking (per-tool) ---------- */
interface EtaSample {
  stage: string;
  current: number;
  suppressed: boolean;
}

/* ---------- Determinate progress ring ---------- */
interface RingProps {
  current: number;
  total: number;
}

function ProgressRing({ current, total }: RingProps): JSX.Element {
  // h-3 w-3 = 12px; viewBox 24, r=10, circumference = 2*PI*10 ≈ 62.83
  const pct = Math.min(1, Math.max(0, current / total));
  const c = 2 * Math.PI * 10;
  const dash = c * pct;
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-3 w-3 text-primary shrink-0"
      aria-hidden="true"
    >
      <circle
        cx="12"
        cy="12"
        r="10"
        fill="none"
        stroke="currentColor"
        strokeOpacity="0.2"
        strokeWidth="3"
      />
      <circle
        cx="12"
        cy="12"
        r="10"
        fill="none"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinecap="round"
        strokeDasharray={`${dash} ${c - dash}`}
        transform="rotate(-90 12 12)"
        style={{ transition: "stroke-dasharray 200ms ease" }}
      />
    </svg>
  );
}

/* ---------- Single tool row ---------- */
interface RowProps {
  entry: ToolCallEntry;
  stepIndex: number;
  connector?: "branch" | "end" | "none";
  eta: number | null;
}

function ToolRow({ entry, stepIndex, connector = "none", eta }: RowProps): JSX.Element {
  const { t } = useTranslation();
  const progress = entry.progress;
  const hasDeterminate = !!(progress && typeof progress.current === "number" && typeof progress.total === "number" && progress.total > 0);
  const stage = progress?.stage || "";
  const message = progress?.message || "";

  const icon = entry.status === "error"
    ? <XCircle className="h-3 w-3 text-danger shrink-0" />
    : entry.status === "ok"
      ? <CheckCircle2 className="h-3 w-3 text-success shrink-0" />
      : hasDeterminate
        ? <ProgressRing current={progress!.current!} total={progress!.total!} />
        : <Loader2 className="h-3 w-3 animate-spin text-primary shrink-0" />;

  const localized = localizeToolName(entry.tool);
  const sequenceLabel = t("thinking.toolSequence", { index: stepIndex });
  const stageLabel = localizeToolStage(stage);
  const messageLabel = localizeToolProgressMessage(message, entry.tool);

  return (
    <div className="grid min-w-0 gap-y-1 text-xs">
      {/* Primary row */}
      <div className="flex min-w-0 items-center gap-2">
        {connector !== "none" && (
          <span className="text-border/60 shrink-0 w-3 text-center" aria-hidden="true">
            {connector === "branch" ? "├" : "└"}
          </span>
        )}
        {icon}
        <span className="min-w-0 flex-1 truncate text-foreground">
          <span className="font-medium">{sequenceLabel}</span>
          <span className="text-muted-foreground"> · {localized}</span>
        </span>
        {entry.elapsed_s != null && (
          <span className="shrink-0 tabular-nums text-[10px] text-muted-foreground/70">
            {entry.elapsed_s.toFixed(0)}s
          </span>
        )}
      </div>
      {/* Phase progress always owns a separate row from the tool sequence. */}
      {(progress && (hasDeterminate || stage)) && (
        <div className="flex min-w-0 flex-wrap items-center gap-2 pl-5 sm:pl-7">
          {stage && (
            <span
              className="max-w-full shrink-0 truncate rounded bg-muted/60 px-1.5 py-0.5 text-[10px] text-muted-foreground sm:max-w-[45%]"
              title={stage}
            >
              {t("thinking.phaseLabel")}: {stageLabel}
            </span>
          )}
          {hasDeterminate && (
            <ProgressBar
              current={progress!.current!}
              total={progress!.total!}
              height="xs"
              showCount
              ariaLabel={t("thinking.phaseProgress", { phase: stageLabel || localized })}
              className="min-w-[8rem] basis-[10rem] flex-1 text-muted-foreground"
            />
          )}
          {eta != null && (
            <span className="text-[10px] text-muted-foreground/70 tabular-nums shrink-0">
              {t("thinking.remainingSeconds", { count: eta })}
            </span>
          )}
        </div>
      )}
      {/* Detail is deliberately separate so it cannot compete with counts. */}
      {messageLabel && (
        <div
          className="line-clamp-2 min-w-0 break-words pl-5 text-[11px] leading-4 text-muted-foreground/70 sm:pl-7"
          title={messageLabel}
        >
          {messageLabel}
        </div>
      )}
    </div>
  );
}

/* ---------- Public component ---------- */
interface Props {
  /** Full toolCalls slice for the active turn. Keep every call visible until the turn ends. */
  toolCalls: ToolCallEntry[];
}

export function ToolProgressIndicator({ toolCalls }: Props): JSX.Element | null {
  const { t } = useTranslation();
  // Per-tool ETA samples (mutable across renders, not state to avoid re-renders).
  const etaSamplesRef = useRef<Map<string, EtaSample>>(new Map());

  const running = toolCalls.filter((tc) => tc.status === "running");
  if (toolCalls.length === 0) return null;

  const totalSoFar = toolCalls.length;

  /* ---------- compute ETA for each running tool ---------- */
  const computeEta = (tc: ToolCallEntry): number | null => {
    const p = tc.progress;
    if (!p || typeof p.current !== "number" || typeof p.total !== "number") return null;
    if (p.total <= 0) return null;
    const stage = p.stage || "";
    const samples = etaSamplesRef.current;
    const prev = samples.get(tc.id);

    // A stage transition legitimately resets current/total; start a fresh ETA sample.
    if (!prev || prev.stage !== stage) {
      samples.set(tc.id, { stage, current: p.current, suppressed: false });
      return null;
    }

    // Out-of-order: current decreased → suppress for the rest of the run.
    if (p.current < prev.current) {
      samples.set(tc.id, { stage, current: p.current, suppressed: true });
      return null;
    }
    if (prev.suppressed) {
      // Update tracking but keep suppressed.
      samples.set(tc.id, { stage, current: p.current, suppressed: true });
      return null;
    }
    samples.set(tc.id, { stage, current: p.current, suppressed: false });

    // Need a stable stage and enough samples to extrapolate.
    if (p.current < 3) return null;
    if (p.current < p.total * 0.1) return null;
    if (tc.elapsed_s == null || tc.elapsed_s <= 0) return null;

    const eta = (tc.elapsed_s / p.current) * (p.total - p.current);
    if (!isFinite(eta) || eta < 0) return null;
    return Math.round(eta);
  };

  /* ---------- aggregate icon state for the header row ---------- */
  // (Used when 2+ tools are running — header shows multi-tool aggregate.)
  // Note: filtered list is `running` so all are still running by construction.
  // We still inspect entire toolCalls so an earlier error in this turn shows
  // through the aggregate.
  const anyError = toolCalls.some((tc) => tc.status === "error");
  const aggregateIcon = anyError
    ? <XCircle className="h-3 w-3 text-danger shrink-0" />
    : running.length > 0
      ? <Loader2 className="h-3 w-3 animate-spin text-primary shrink-0" />
      : <CheckCircle2 className="h-3 w-3 text-success shrink-0" />;

  /* ---------- render ---------- */
  if (toolCalls.length === 1) {
    const only = toolCalls[0];
    const eta = computeEta(only);
    return (
      <div
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="min-w-0"
      >
        <ToolRow
          entry={only}
          stepIndex={totalSoFar}
          eta={eta}
        />
      </div>
    );
  }

  // Multiple calls in the active turn: header + persistent indented rows.
  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className={cn("min-w-0 space-y-1")}
    >
      {/* Header row */}
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        {aggregateIcon}
        <span className="text-foreground">
          {running.length > 0
            ? t("thinking.toolsRunning", { count: running.length })
            : t("thinking.toolCalls", { count: toolCalls.length })}
        </span>
      </div>
      {/* Indented rows */}
      <div className="pl-4 space-y-1">
        {toolCalls.map((tc, i) => (
          <ToolRow
            key={tc.id}
            entry={tc}
            stepIndex={i + 1}
            connector={i === toolCalls.length - 1 ? "end" : "branch"}
            eta={computeEta(tc)}
          />
        ))}
      </div>
    </div>
  );
}
