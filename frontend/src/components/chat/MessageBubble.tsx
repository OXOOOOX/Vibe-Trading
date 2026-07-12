import i18n from '@/i18n';
import { memo, useState, useCallback } from "react";
import { User, XCircle, RefreshCw, Copy, Check, FileDown, Loader2, GitBranch, Pencil, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { formatTimestamp } from "@/lib/formatters";
import type { AgentMessage } from "@/types/agent";
import { AgentAvatar } from "./AgentAvatar";
import { RunCompleteCard } from "./RunCompleteCard";
import { ApiError, api } from "@/lib/api";
import { toast } from "sonner";

const remarkPlugins = [remarkGfm];
const rehypePlugins = [rehypeHighlight];

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
}

export const MessageBubble = memo(function MessageBubble({ msg, onRetry, onFork, canEdit, onEdit }: Props) {
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
    return (
      <div className="flex gap-3 group">
        <AgentAvatar />
        <div className="flex-1 min-w-0 relative">
          <div className="absolute top-2 right-2 flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
            {forkable && <ForkButton onFork={() => onFork?.(msg)} />}
            <PdfButton text={msg.content} />
            <CopyButton text={msg.content} />
          </div>
          <div className="prose prose-sm dark:prose-invert max-w-none leading-relaxed prose-table:border prose-table:border-border/50 prose-th:bg-muted/30 prose-th:px-3 prose-th:py-1.5 prose-td:px-3 prose-td:py-1.5 prose-th:text-left prose-th:text-xs prose-th:font-medium prose-td:text-xs prose-hr:hidden">
            <ReactMarkdown remarkPlugins={remarkPlugins} rehypePlugins={rehypePlugins}>{msg.content}</ReactMarkdown>
          </div>
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
