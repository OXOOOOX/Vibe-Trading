import i18n from '@/i18n';
import { memo, useId, useState, useCallback, type ReactNode } from "react";
import { User, XCircle, RefreshCw, Copy, Check, FileDown, Loader2, GitBranch, Pencil, X, FileText, PanelRightOpen } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { formatTimestamp } from "@/lib/formatters";
import type { AgentMessage, ReportPreviewTarget } from "@/types/agent";
import { AgentAvatar } from "./AgentAvatar";
import { RunCompleteCard } from "./RunCompleteCard";
import { ApiError, api } from "@/lib/api";
import { toast } from "sonner";

const remarkPlugins = [remarkGfm];
const rehypePlugins = [rehypeHighlight];
const DEEP_REPORT_SECTIONS = [
  ["executive_summary", "核心结论"],
  ["business_position", "公司业务与产业位置"],
  ["financial_quality", "三张报表与财务质量"],
  ["accounting_review", "会计异常核查"],
  ["implied_expectations", "市值隐含预期"],
  ["terminal_narrative", "长期经营情景"],
  ["counter_thesis", "反方、风险与催化剂"],
  ["conclusion_watchlist", "结论与跟踪框架"],
] as const;

export type DeepReportAction = "continue" | "refresh" | "revise" | "repair" | "archive";

export interface DeepReportTaskStarted {
  action: DeepReportAction;
  reportId: string;
  parentReportId: string;
  attemptId: string;
  messageId: string;
}

export function trimPostDisclaimerFollowUp(text: string): string {
  const disclaimerIndex = text.indexOf("免责声明");
  if (disclaimerIndex === -1) return text;

  const suffix = text.slice(disclaimerIndex);
  const followUpMarkers = ["有什么需要", "如有需要", "如果需要", "如果你需要", "需要我"];
  const markerIndex = followUpMarkers.reduce((earliest, marker) => {
    const index = suffix.indexOf(marker);
    return index !== -1 && (earliest === -1 || index < earliest) ? index : earliest;
  }, -1);

  if (markerIndex === -1) return text;
  return text.slice(0, disclaimerIndex + markerIndex).trimEnd();
}

function localIsoDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function cleanReportTitleLine(line: string): string {
  return line
    .trim()
    .replace(/^#{1,6}\s+/, "")
    .replace(/^[^\p{L}\p{N}]+/u, "")
    .replace(/(?:\*\*|__|`)+$/g, "")
    .trim();
}

export function buildPdfReportTitle(text: string, now = new Date()): string {
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const plainText = text.replace(/[*_`]/g, "");
  const dateMatch = plainText.match(/(?:分析日期|报告日期)\s*[：:]\s*(\d{4}-\d{2}-\d{2})/);
  const date = dateMatch?.[1] ?? localIsoDate(now);
  const metadataLine = /^(?:分析日期|报告日期|数据截止|声明|免责声明)\s*[：:]/;
  const isMetadataLine = (line: string) => metadataLine.test(
    cleanReportTitleLine(line).replace(/[*_`]/g, ""),
  );

  const markdownHeading = lines.find((line) => /^#{1,6}\s+\S/.test(line));
  const decoratedHeading = lines.find((line) => {
    if (isMetadataLine(line) || !/^[\p{Extended_Pictographic}\p{Emoji_Presentation}\p{So}]/u.test(line)) return false;
    const cleaned = cleanReportTitleLine(line);
    return cleaned.length >= 4 && cleaned.length <= 120;
  });
  const descriptiveHeading = lines.find((line) => {
    const cleaned = cleanReportTitleLine(line);
    return !isMetadataLine(line)
      && cleaned.length >= 4
      && cleaned.length <= 120
      && /(?:分析|报告|复盘|研究|策略|展望|总结)$/.test(cleaned);
  });
  const reportTitle = cleanReportTitleLine(
    markdownHeading ?? decoratedHeading ?? descriptiveHeading ?? "Vibe-Trading Research Report",
  ).replace(/^\d{4}-\d{2}-\d{2}[_\s-]+/, "");

  return `${date}_${reportTitle}`;
}

export function sanitizePdfFilename(title: string): string {
  return title
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_")
    .replace(/\s+/g, " ")
    .replace(/[. ]+$/g, "")
    .slice(0, 160)
    || "research_report";
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }, [text]);
  return (
    <button
      onClick={handleCopy}
      className="p-1.5 rounded-md bg-muted/80 hover:bg-muted text-muted-foreground hover:text-foreground"
      title={copied ? i18n.t("messageBubble.copied") : i18n.t("messageBubble.copy")}
    >
      {copied ? <Check className="h-3.5 w-3.5 text-success" /> : <Copy className="h-3.5 w-3.5" />}
    </button>
  );
}

function ForkButton({ onFork }: { onFork: () => void }) {
  return (
    <button
      onClick={onFork}
      className="p-1.5 rounded-md bg-muted/80 hover:bg-muted text-muted-foreground hover:text-foreground"
      title={i18n.t("messageBubble.forkConversation")}
    >
      <GitBranch className="h-3.5 w-3.5" />
    </button>
  );
}

function PdfButton({ text }: { text: string }) {
  const [loading, setLoading] = useState(false);
  const pdfText = trimPostDisclaimerFollowUp(text);
  const pdfTitle = buildPdfReportTitle(pdfText);
  const printFallback = useCallback(() => {
    const frame = document.createElement("iframe");
    frame.style.position = "fixed";
    frame.style.width = "0";
    frame.style.height = "0";
    frame.style.border = "0";
    document.body.appendChild(frame);
    const doc = frame.contentDocument;
    if (!doc) {
      frame.remove();
      throw new Error("Unable to open the PDF print view");
    }
    const title = doc.createElement("h1");
    title.textContent = pdfTitle;
    const content = doc.createElement("pre");
    content.textContent = pdfText;
    doc.head.innerHTML = `<title></title><style>
      @page { size: A4; margin: 18mm; }
      body { font-family: "Microsoft YaHei", sans-serif; color: #172033; line-height: 1.6; }
      h1 { font-size: 22px; border-bottom: 2px solid #2563eb; padding-bottom: 8px; }
      pre { white-space: pre-wrap; font: inherit; }
    </style>`;
    doc.title = pdfTitle;
    doc.body.append(title, content);
    setTimeout(() => {
      frame.contentWindow?.print();
      setTimeout(() => frame.remove(), 1000);
    }, 50);
  }, [pdfText, pdfTitle]);

  const handlePdf = useCallback(async () => {
    if (loading) return;
    setLoading(true);
    try {
      const blob = await api.generatePdf(pdfTitle, pdfText);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${sanitizePdfFilename(pdfTitle)}.pdf`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      if (error instanceof ApiError && error.status === 501) {
        printFallback();
        toast.info(i18n.t("messageBubble.pdfRendererUnavailable"));
      } else {
        toast.error(error instanceof Error ? error.message : i18n.t("messageBubble.pdfGenerationFailed"));
      }
    } finally {
      setLoading(false);
    }
  }, [loading, pdfText, pdfTitle, printFallback]);
  return (
    <button
      onClick={handlePdf}
      disabled={loading}
      className="p-1.5 rounded-md bg-muted/80 hover:bg-muted text-muted-foreground hover:text-foreground disabled:opacity-50"
      title={i18n.t("messageBubble.generatePdf")}
    >
      {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FileDown className="h-3.5 w-3.5" />}
    </button>
  );
}

export function extractMarkdownReportFilename(content: string): string | null {
  const match = content.match(/agent[\\/]+runs[\\/]+[A-Za-z0-9_-]+[\\/]+([^\r\n`]+?\.md)(?=\s|$)/i);
  return match?.[1]?.trim() || null;
}

function DeepReportPdfButton({ reportId }: { reportId: string }) {
  return (
    <a
      href={api.deepReportArtifactUrl(reportId, "pdf")}
      className="p-1.5 rounded-md bg-muted/80 hover:bg-muted text-muted-foreground hover:text-foreground"
      title="下载已校验的穿透式深度研究 PDF"
    >
      <FileDown className="h-3.5 w-3.5" />
    </a>
  );
}

const DEEP_REPORT_MODULE_LABELS: Record<string, string> = {
  executive_summary: "核心结论",
  business_position: "公司业务与产业位置",
  financial_quality: "三张报表与财务质量",
  accounting_review: "会计异常核查",
  implied_expectations: "市值隐含预期",
  terminal_narrative: "长期经营情景",
  terminal_scenarios: "长期经营情景",
  counter_thesis: "反方、风险与催化剂",
  conclusion_watchlist: "结论与跟踪框架",
};

function deepReportModuleLabel(moduleId: string): string {
  return DEEP_REPORT_MODULE_LABELS[moduleId] || moduleId;
}

function deepReportReaderGaps(moduleIds?: string[], includeInherited = false): string[] {
  if (!moduleIds?.length) return [];
  const inherited = new Set(["executive_summary", "counter_thesis", "conclusion_watchlist"]);
  const labels = moduleIds
    .filter((moduleId) => includeInherited || !inherited.has(moduleId))
    .map(deepReportModuleLabel);
  return [...new Set(labels.length > 0 ? labels : ["部分研究结论"])];
}

function deepReportQualityLabel(status?: AgentMessage["reportQualityStatus"]): string {
  if (status === "passed") return "证据完整，已通过校验";
  if (status === "failed_validation") return "尚未形成正式报告";
  return "已完成，部分结论保留";
}

function formatReportDataTime(value?: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function DeepReportActionHint({
  id,
  text,
  align = "left",
  children,
}: {
  id: string;
  text: string;
  align?: "left" | "right";
  children: ReactNode;
}) {
  return (
    <div className="group/action relative">
      {children}
      <div
        id={id}
        role="tooltip"
        className={`pointer-events-none absolute bottom-full z-30 mb-2 w-64 max-w-[calc(100vw-2rem)] rounded-md border border-background/20 bg-foreground/95 px-3 py-2 text-[11px] leading-relaxed text-background opacity-0 shadow-xl backdrop-blur-md transition-opacity group-hover/action:opacity-100 group-focus-within/action:opacity-100 ${align === "right" ? "right-0" : "left-0"}`}
      >
        {text}
      </div>
    </div>
  );
}

function DeepReportActionBar({
  reportId,
  canArchive,
  canRepair,
  busy = false,
  onTaskStarted,
}: {
  reportId: string;
  canArchive: boolean;
  canRepair: boolean;
  busy?: boolean;
  onTaskStarted?: (task: DeepReportTaskStarted) => void;
}) {
  const hintPrefix = useId();
  const [sectionId, setSectionId] = useState<(typeof DEEP_REPORT_SECTIONS)[number][0]>("counter_thesis");
  const [pending, setPending] = useState<DeepReportAction | null>(null);
  const selectedSectionLabel = DEEP_REPORT_SECTIONS.find(([id]) => id === sectionId)?.[1] || sectionId;

  const runAction = useCallback(async (action: DeepReportAction) => {
    setPending(action);
    try {
      let result: { message_id: string; attempt_id: string; parent_report_id: string };
      if (action === "continue") {
        result = await api.followUpDeepReport(reportId, "继续研究这份报告，并优先回答尚未解决的证据缺口。");
      } else if (action === "refresh") {
        result = await api.refreshDeepReport(reportId, "使用最新可验证数据重新研究，并生成新的不可变 revision。");
      } else if (action === "archive") {
        const archived = await api.archiveDeepReport(reportId);
        toast.success(`已保存到 Obsidian：${archived.path}`);
        return;
      } else if (action === "repair") {
        result = await api.repairDeepReport(
          reportId,
          "复用现有 FinancialSnapshot、Fact 和 Evidence，修复全部未通过校验的标准章节。",
        );
      } else {
        result = await api.reviseDeepReport(reportId, [sectionId], `只更新“${selectedSectionLabel}”，保留未变化的确定性事实。`);
      }
      onTaskStarted?.({
        action,
        reportId,
        parentReportId: result.parent_report_id,
        attemptId: result.attempt_id,
        messageId: result.message_id,
      });
      toast.success(action === "continue" ? "已继续当前报告对话" : "报告更新已开始，可在页面中查看实时进度");
    } catch (error) {
      toast.error(
        error instanceof Error
          ? error.message
          : action === "archive"
            ? "Obsidian 归档任务创建失败"
            : "报告更新任务创建失败",
      );
    } finally {
      setPending(null);
    }
  }, [onTaskStarted, reportId, sectionId, selectedSectionLabel]);

  const actionDisabled = pending !== null || busy;

  return (
    <div className="mt-3 flex flex-wrap items-center gap-2 rounded-md border bg-muted/20 p-2 text-xs">
      <DeepReportActionHint
        id={`${hintPrefix}-continue`}
        text="基于当前报告继续追问和补证，不整份重跑；适合深挖尚未解决的数据缺口。"
      >
        <button
          type="button"
          disabled={actionDisabled}
          aria-describedby={`${hintPrefix}-continue`}
          onClick={() => void runAction("continue")}
          className="rounded border bg-background px-2.5 py-1.5 font-medium hover:bg-muted disabled:opacity-50"
        >
          {pending === "continue" ? "处理中…" : "继续研究"}
        </button>
      </DeepReportActionHint>
      {canRepair ? (
        <DeepReportActionHint
          id={`${hintPrefix}-repair`}
          text="沿用本版本已经核实的数据和资料，只修复未通过的章节；旧版本会继续保留。"
        >
          <button
            type="button"
            disabled={actionDisabled}
            aria-describedby={`${hintPrefix}-repair`}
            onClick={() => void runAction("repair")}
            className="rounded border border-amber-500/40 bg-amber-500/5 px-2.5 py-1.5 font-medium text-amber-700 hover:bg-amber-500/10 disabled:opacity-50 dark:text-amber-300"
          >
            {pending === "repair" ? "修复中…" : "修复报告"}
          </button>
        </DeepReportActionHint>
      ) : null}
      <DeepReportActionHint
        id={`${hintPrefix}-refresh`}
        text="重新获取最新可验证数据并生成新版本；适合报告数据日期已过或市场信息发生变化。"
      >
        <button
          type="button"
          disabled={actionDisabled}
          aria-describedby={`${hintPrefix}-refresh`}
          onClick={() => void runAction("refresh")}
          className="rounded border bg-background px-2.5 py-1.5 font-medium hover:bg-muted disabled:opacity-50"
        >
          {pending === "refresh" ? "处理中…" : "用新数据更新"}
        </button>
      </DeepReportActionHint>
      <DeepReportActionHint
        id={`${hintPrefix}-section`}
        text={`选择“重写此章节”要作用的目标模块；当前选中“${selectedSectionLabel}”。只选择，不会立即开始。`}
      >
        <select
          value={sectionId}
          onChange={(event) => setSectionId(event.target.value as (typeof DEEP_REPORT_SECTIONS)[number][0])}
          disabled={actionDisabled}
          aria-label="选择要重写的报告章节"
          aria-describedby={`${hintPrefix}-section`}
          className="rounded border bg-background px-2 py-1.5"
        >
          {DEEP_REPORT_SECTIONS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select>
      </DeepReportActionHint>
      <DeepReportActionHint
        id={`${hintPrefix}-revise`}
        text="仅重写左侧选中的章节，尽量保留其他模块和未变化的确定性事实。"
      >
        <button
          type="button"
          disabled={actionDisabled}
          aria-describedby={`${hintPrefix}-revise`}
          onClick={() => void runAction("revise")}
          className="rounded border bg-background px-2.5 py-1.5 font-medium hover:bg-muted disabled:opacity-50"
        >
          {pending === "revise" ? "处理中…" : "重写此章节"}
        </button>
      </DeepReportActionHint>
      {canArchive ? (
        <DeepReportActionHint
          id={`${hintPrefix}-archive`}
          text="把这份已通过校验的报告按既定命名与分类规则归档到 Obsidian 投资目录，不覆盖同名笔记。"
          align="right"
        >
          <button
            type="button"
            disabled={actionDisabled}
            aria-describedby={`${hintPrefix}-archive`}
            onClick={() => void runAction("archive")}
            className="rounded border border-primary/30 bg-primary/5 px-2.5 py-1.5 font-medium text-primary hover:bg-primary/10 disabled:opacity-50"
          >
            {pending === "archive" ? "保存中…" : "保存到 Obsidian"}
          </button>
        </DeepReportActionHint>
      ) : null}
    </div>
  );
}

function getRetryHint(content: string): string {
  const lower = content.toLowerCase();
  if (lower.includes("timeout") || lower.includes("timed out")) {
    return i18n.t("messageBubble.timeoutHint");
  }
  if (lower.includes("api") || lower.includes("rate limit") || lower.includes("429") || lower.includes("500") || lower.includes("502") || lower.includes("503")) {
    return i18n.t("messageBubble.apiFailedHint");
  }
  return i18n.t("messageBubble.executionFailedHint");
}

interface Props {
  msg: AgentMessage;
  onRetry?: (msg: AgentMessage) => void;
  onFork?: (msg: AgentMessage) => void;
  canEdit?: boolean;
  onEdit?: (msg: AgentMessage, content: string) => Promise<void> | void;
  onPreviewReport?: (target: ReportPreviewTarget) => void;
  deepReportBusy?: boolean;
  onDeepReportTaskStarted?: (task: DeepReportTaskStarted) => void;
}

export const MessageBubble = memo(function MessageBubble({
  msg,
  onRetry,
  onFork,
  canEdit,
  onEdit,
  onPreviewReport,
  deepReportBusy,
  onDeepReportTaskStarted,
}: Props) {
  const ts = msg.timestamp ? formatTimestamp(msg.timestamp) : null;
  const forkable = Boolean(msg.type === "answer" && onFork && msg.sourceMessageId);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(msg.content);
  const [savingEdit, setSavingEdit] = useState(false);

  const startEdit = useCallback(() => {
    setDraft(msg.content);
    setEditing(true);
  }, [msg.content]);

  const saveEdit = useCallback(async () => {
    const content = draft.trim();
    if (!content || content === msg.content) {
      setEditing(false);
      return;
    }
    setSavingEdit(true);
    try {
      await onEdit?.(msg, content);
      setEditing(false);
    } finally {
      setSavingEdit(false);
    }
  }, [draft, msg, onEdit]);

  if (msg.type === "user") {
    return (
      <div className="flex justify-end gap-3 group">
        <div className="max-w-[72%] flex flex-col items-end gap-1">
          {canEdit && !editing && (
            <div className="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
              <button
                onClick={startEdit}
                className="p-1.5 rounded-md bg-muted/80 hover:bg-muted text-muted-foreground hover:text-foreground"
                title={i18n.t("agent.edit")}
              >
                <Pencil className="h-3.5 w-3.5" />
              </button>
            </div>
          )}
          {editing ? (
            <div className="w-[min(42rem,72vw)] rounded-2xl rounded-tr-sm border bg-background p-2 shadow-sm">
              <textarea
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                className="min-h-24 w-full resize-y rounded-md border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary/30"
                autoFocus
              />
              <div className="mt-2 flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setEditing(false)}
                  disabled={savingEdit}
                  className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs disabled:opacity-50"
                >
                  <X className="h-3 w-3" />
                  {i18n.t("agent.cancel")}
                </button>
                <button
                  type="button"
                  onClick={saveEdit}
                  disabled={savingEdit}
                  className="inline-flex items-center gap-1.5 rounded-md bg-primary px-2.5 py-1.5 text-xs text-primary-foreground disabled:opacity-50"
                >
                  {savingEdit ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
                  {i18n.t("agent.saveAndSend")}
                </button>
              </div>
            </div>
          ) : (
            <div className="rounded-2xl rounded-tr-sm bg-primary text-primary-foreground px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap">
              {msg.content}
              {ts && <span className="block text-[9px] opacity-50 text-right mt-1">{ts}</span>}
            </div>
          )}
        </div>
        <div className="h-8 w-8 rounded-full bg-muted flex items-center justify-center shrink-0 mt-0.5">
          <User className="h-4 w-4 text-muted-foreground" />
        </div>
      </div>
    );
  }

  if (msg.type === "answer") {
    const failedDeepReport = msg.reportQualityStatus === "failed_validation";
    const reportGapLabels = deepReportReaderGaps(msg.reportMissingModules, failedDeepReport);
    const fullRefreshRequired = Boolean(
      failedDeepReport
      && msg.reportMissingModules?.some((moduleId) =>
        ["report_gate", "market_data", "symbol_identity", "financial_quality"].includes(moduleId),
      ),
    );
    const canDownloadDeepReportPdf = Boolean(
      msg.reportId && msg.reportPdfAvailable === true && !failedDeepReport,
    );
    const explicitReportFilename = extractMarkdownReportFilename(msg.content);
    const reportPreviewFilename = explicitReportFilename
      || (msg.reportSecurityName ? `${msg.reportSecurityName}穿透式深度研究.md` : "穿透式深度研究.md");
    const reportPreviewArtifact = failedDeepReport ? "diagnostic" : "markdown";
    const canPreviewReport = Boolean(
      onPreviewReport
      && (
        (msg.reportId && (failedDeepReport ? msg.reportDiagnosticAvailable !== false : msg.reportMarkdownAvailable !== false))
        || (msg.runId && explicitReportFilename)
      ),
    );
    return (
      <div className="flex gap-3 group">
        <AgentAvatar />
        <div className="flex-1 min-w-0 relative">
          <div className="absolute top-2 right-2 flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
            {forkable && <ForkButton onFork={() => onFork?.(msg)} />}
            {canDownloadDeepReportPdf && msg.reportId
              ? <DeepReportPdfButton reportId={msg.reportId} />
              : !msg.reportId
                ? <PdfButton text={msg.content} />
                : null}
            <CopyButton text={msg.content} />
          </div>
          {msg.reportId && (
            <div className="mb-3 flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
              <span className="rounded border border-primary/30 bg-primary/5 px-2 py-0.5 font-medium text-primary">
                穿透式深度研究
              </span>
              {msg.reportGenerationSource === "portfolio_monitor_autopilot" ? (
                <span className="rounded border border-violet-500/30 bg-violet-500/5 px-2 py-0.5 font-medium text-violet-700 dark:text-violet-300">
                  AI 自主监控生成
                </span>
              ) : null}
              <span className={`rounded px-2 py-0.5 font-medium ${failedDeepReport ? "bg-destructive/10 text-destructive" : "bg-muted"}`}>
                {deepReportQualityLabel(msg.reportQualityStatus)}
              </span>
              {msg.reportSymbol && <span className="font-mono">{msg.reportSymbol}</span>}
              {msg.reportRevision != null && <span>第 {msg.reportRevision} 版</span>}
              {msg.reportParentId && <span title={msg.reportParentId}>来自上一版本</span>}
              {msg.reportDataAsOf && <span>数据更新至 {formatReportDataTime(msg.reportDataAsOf)}</span>}
              {msg.reportId && !canDownloadDeepReportPdf && <span>暂不提供 PDF</span>}
              {msg.reportGenerationSource === "portfolio_monitor_autopilot" && msg.reportGenerationReason ? (
                <span>自动触发原因：{msg.reportGenerationReason}</span>
              ) : null}
            </div>
          )}
          {msg.reportId && failedDeepReport && (
            <div role="alert" className="mb-3 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
              {fullRefreshRequired
                ? "本次缺少可核验的价格、市值、股票身份或财务数据。单独修改章节无法解决，请点击“用新数据更新”。"
                : "关键内容没有通过发布前校验。本次只保留诊断结果，不应视为正式投资研究。"}
            </div>
          )}
          {msg.reportId && reportGapLabels.length > 0 && (
            <div aria-label="仍需补充的研究内容" className="mb-3 rounded-md border bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
              <div className="font-medium text-foreground">仍需补充的研究内容（{reportGapLabels.length}）</div>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {reportGapLabels.map((label) => (
                  <span key={label} className="rounded border bg-background px-2 py-0.5">
                    {label}
                  </span>
                ))}
              </div>
            </div>
          )}
          <div className="prose prose-sm dark:prose-invert max-w-none leading-relaxed prose-table:border prose-table:border-border/50 prose-th:bg-muted/30 prose-th:px-3 prose-th:py-1.5 prose-td:px-3 prose-td:py-1.5 prose-th:text-left prose-th:text-xs prose-th:font-medium prose-td:text-xs prose-hr:hidden">
            <ReactMarkdown remarkPlugins={remarkPlugins} rehypePlugins={rehypePlugins}>{msg.content}</ReactMarkdown>
          </div>
          {canPreviewReport && (
            <button
              type="button"
              onClick={() => onPreviewReport?.({
                runId: msg.runId,
                  reportId: msg.reportId,
                  artifactId: msg.reportId ? reportPreviewArtifact : undefined,
                  title: msg.reportSecurityName
                  ? `${msg.reportSecurityName}穿透式深度研究`
                  : reportPreviewFilename.replace(/\.md$/i, ""),
              })}
              aria-label={`在右侧预览报告 ${reportPreviewFilename}`}
              className="mt-3 flex w-full items-center gap-3 rounded-xl border border-cyan-500/25 bg-cyan-500/5 px-3 py-2.5 text-left transition-colors hover:border-cyan-500/45 hover:bg-cyan-500/10"
            >
              <span className="rounded-lg bg-background p-2 text-cyan-700 shadow-sm dark:text-cyan-300">
                <FileText className="h-4 w-4" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-xs font-medium text-foreground">
                  {failedDeepReport ? "查看未发布原因" : "阅读完整报告"}
                </span>
                <span className="block truncate text-[11px] text-muted-foreground" title={reportPreviewFilename}>
                  {reportPreviewFilename}
                </span>
              </span>
              <span className="inline-flex shrink-0 items-center gap-1 text-[11px] font-medium text-cyan-700 dark:text-cyan-300">
                <PanelRightOpen className="h-3.5 w-3.5" />
                右侧打开
              </span>
            </button>
          )}
          {msg.reportId && msg.reportDiffAvailable && onPreviewReport && (
            <button
              type="button"
              onClick={() => onPreviewReport({
                reportId: msg.reportId,
                artifactId: "diff",
                title: `${msg.reportSecurityName || msg.reportSymbol || "报告"}版本差异`,
              })}
              className="mt-2 flex w-full items-center gap-3 rounded-xl border bg-muted/20 px-3 py-2.5 text-left transition-colors hover:bg-muted/40"
            >
              <span className="rounded-lg bg-background p-2 text-muted-foreground shadow-sm">
                <GitBranch className="h-4 w-4" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-xs font-medium text-foreground">查看与上一版差异</span>
                <span className="block truncate text-[11px] text-muted-foreground">按章节比较当前版本与上一版本</span>
              </span>
              <PanelRightOpen className="h-3.5 w-3.5 text-muted-foreground" />
            </button>
          )}
          {msg.reportId && (
            <DeepReportActionBar
              reportId={msg.reportId}
              canArchive={!failedDeepReport && msg.reportMarkdownAvailable !== false}
              canRepair={failedDeepReport && !fullRefreshRequired}
              busy={deepReportBusy}
              onTaskStarted={onDeepReportTaskStarted}
            />
          )}
          {ts && <span className="text-[9px] text-muted-foreground/30 mt-1 opacity-0 group-hover:opacity-100 transition-opacity">{ts}</span>}
        </div>
      </div>
    );
  }

  if (msg.type === "run_complete" && msg.runId) {
    return <RunCompleteCard msg={msg} />;
  }

  if (msg.type === "error") {
    const hint = getRetryHint(msg.content);
    return (
      <div className="flex gap-3">
        <AgentAvatar />
        <div className="space-y-2">
          <div className="flex items-start gap-2 rounded-xl border border-danger/30 bg-danger/5 px-4 py-3">
            <XCircle className="h-4 w-4 text-danger shrink-0 mt-0.5" />
            <p className="text-sm text-danger leading-relaxed">{msg.content}</p>
          </div>
          {onRetry && (
            <button
              onClick={() => onRetry(msg)}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-muted-foreground hover:text-foreground hover:bg-muted/80 border border-transparent hover:border-border transition-all"
              title={hint}
            >
              <RefreshCw className="h-3 w-3" />
              <span>{hint}</span>
            </button>
          )}
        </div>
      </div>
    );
  }

  // Fallback: show content for any unhandled message type
  if (msg.content) {
    return (
      <div className="flex gap-3">
        <AgentAvatar />
        <p className="text-sm text-muted-foreground leading-relaxed">{msg.content}</p>
      </div>
    );
  }

  return null;
});
