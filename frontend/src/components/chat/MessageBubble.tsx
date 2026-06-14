import { memo, useState, useCallback } from "react";
import { User, XCircle, RefreshCw, Copy, Check, FileDown, Loader2 } from "lucide-react";
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
      title={copied ? "Copied" : "Copy"}
    >
      {copied ? <Check className="h-3.5 w-3.5 text-success" /> : <Copy className="h-3.5 w-3.5" />}
    </button>
  );
}

function PdfButton({ text }: { text: string }) {
  const [loading, setLoading] = useState(false);
  const pdfText = trimPostDisclaimerFollowUp(text);
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
    title.textContent = "Vibe-Trading Research Report";
    const content = doc.createElement("pre");
    content.textContent = pdfText;
    doc.head.innerHTML = `<title>Vibe-Trading Research Report</title><style>
      @page { size: A4; margin: 18mm; }
      body { font-family: "Microsoft YaHei", sans-serif; color: #172033; line-height: 1.6; }
      h1 { font-size: 22px; border-bottom: 2px solid #2563eb; padding-bottom: 8px; }
      pre { white-space: pre-wrap; font: inherit; }
    </style>`;
    doc.body.append(title, content);
    setTimeout(() => {
      frame.contentWindow?.print();
      setTimeout(() => frame.remove(), 1000);
    }, 50);
  }, [pdfText]);

  const handlePdf = useCallback(async () => {
    if (loading) return;
    setLoading(true);
    try {
      const blob = await api.generatePdf("Vibe-Trading Research Report", pdfText);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `research_report_${new Date().toISOString().slice(0, 10)}.pdf`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      if (error instanceof ApiError && error.status === 501) {
        printFallback();
        toast.info("PDF 渲染器不可用，已打开系统 PDF 保存窗口");
      } else {
        toast.error(error instanceof Error ? error.message : "PDF generation failed");
      }
    } finally {
      setLoading(false);
    }
  }, [loading, pdfText, printFallback]);
  return (
    <button
      onClick={handlePdf}
      disabled={loading}
      className="p-1.5 rounded-md bg-muted/80 hover:bg-muted text-muted-foreground hover:text-foreground disabled:opacity-50"
      title="生成 PDF"
    >
      {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FileDown className="h-3.5 w-3.5" />}
    </button>
  );
}

function getRetryHint(content: string): string {
  const lower = content.toLowerCase();
  if (lower.includes("timeout") || lower.includes("timed out")) {
    return "Execution timed out. Try simplifying the strategy or reducing the number of assets.";
  }
  if (lower.includes("api") || lower.includes("rate limit") || lower.includes("429") || lower.includes("500") || lower.includes("502") || lower.includes("503")) {
    return "API call failed. Please retry later.";
  }
  return "Execution failed. Click to retry.";
}

interface Props {
  msg: AgentMessage;
  onRetry?: (msg: AgentMessage) => void;
}

export const MessageBubble = memo(function MessageBubble({ msg, onRetry }: Props) {
  const ts = msg.timestamp ? formatTimestamp(msg.timestamp) : null;

  if (msg.type === "user") {
    return (
      <div className="flex justify-end gap-3 group">
        <div className="max-w-[72%] rounded-2xl rounded-tr-sm bg-primary text-primary-foreground px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap">
          {msg.content}
          {ts && <span className="block text-[9px] opacity-50 text-right mt-1">{ts}</span>}
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
