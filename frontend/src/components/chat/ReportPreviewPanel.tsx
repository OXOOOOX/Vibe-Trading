import { useEffect, useMemo, useState } from "react";
import { AlertCircle, FileText, Loader2, RefreshCw, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";

import { ApiError, api } from "@/lib/api";
import type { ReportPreviewTarget } from "@/types/agent";

interface PreviewDocument {
  title: string;
  filename: string;
  relativePath: string;
  content: string;
  updatedAt?: string;
  source: "run" | "deep_report";
  artifactId?: PreviewView;
  availableArtifacts?: PreviewView[];
}

type PreviewView = "markdown" | "diagnostic" | "diff" | "sources" | "history_delta" | "history";

interface Props {
  target: ReportPreviewTarget;
  onClose: () => void;
}

const remarkPlugins = [remarkGfm];
const rehypePlugins = [rehypeHighlight];

function textCell(value: unknown): string {
  return String(value ?? "—").split("|").join("｜").split("\n").join(" ");
}

function deltaMarkdown(delta: Record<string, unknown> | undefined): string {
  if (!delta?.base_report_id) {
    return "# 与上次研究相比\n\n这是知识库中的首次正式研究，暂无可比较的历史正式报告。";
  }
  const groups: Array<[string, unknown]> = [
    ["新增", delta.added], ["更新", delta.updated], ["再次确认", delta.confirmed],
    ["已被替代", delta.superseded], ["存在冲突", delta.contradicted],
    ["已过期或本次未覆盖", delta.stale], ["仍待验证", delta.still_unverified],
  ];
  const lines = ["# 与上次研究相比", "", `对比基准：\`${textCell(delta.base_report_id)}\``];
  groups.forEach(([label, raw]) => {
    const items = Array.isArray(raw) ? raw : [];
    lines.push("", `## ${label}`, "");
    if (!items.length) {
      lines.push("- 无重大项目");
      return;
    }
    items.slice(0, 30).forEach((item) => {
      const value = item && typeof item === "object" ? item as Record<string, unknown> : {};
      const current = value.after && typeof value.after === "object"
        ? value.after as Record<string, unknown>
        : value;
      lines.push(`- ${textCell(current.metric || current.comparison_key || "研究事项")}：${textCell(current.value || current.resolution_status || "状态发生变化")} ${textCell(current.unit || "")}`);
    });
  });
  return lines.join("\n");
}

async function loadPreview(target: ReportPreviewTarget, selectedView?: PreviewView): Promise<PreviewDocument> {
  if (target.runId) {
    try {
      const preview = await api.getRunReportPreview(target.runId);
      return {
        title: preview.title,
        filename: preview.filename,
        relativePath: preview.relative_path,
        content: preview.content,
        updatedAt: preview.updated_at,
        source: "run",
      };
    } catch (error) {
      const authFailure = error instanceof ApiError && (error.status === 401 || error.status === 403);
      if (!target.reportId || authFailure) throw error;
    }
  }

  if (target.reportId) {
    const report = await api.getDeepReport(target.reportId, true);
    const artifactId: PreviewView = selectedView || target.artifactId
      || (report.delivery_kind === "diagnostic" ? "diagnostic" : "markdown");
    const artifact = report.artifacts.find((item) => item.artifact_id === artifactId);
    let content = report.content || "";
    if (artifactId === "diff") {
      const response = await fetch(api.deepReportArtifactUrl(target.reportId, "diff"));
      if (!response.ok) throw new Error("版本差异 Markdown 暂时不可用");
      content = await response.text();
    } else if (artifactId === "sources") {
      const knowledge = await api.searchResearchKnowledge({ symbol: report.symbol, limit: 50 });
      const coverage = report.research_coverage;
      const lines = [
        "# 本次使用的信息",
        "",
        `证券：${report.security_name}（${report.symbol}）`,
        `数据更新至：${report.data_as_of || "尚未明确"}`,
        "",
        "## 资料覆盖",
        "",
        "| 领域 | 状态 | 来源门槛 | 时效规则 |",
        "|---|---|---:|---|",
        ...(coverage?.domains || []).map((domain) =>
          `| ${textCell(domain.domain)} | ${textCell(domain.status)} | ${domain.minimum_independent_sources} 个独立来源 | ${textCell(domain.freshness_policy)} |`,
        ),
        "",
        "## 原始来源",
        "",
        "| 来源 | 类型 | 领域 | 发布时间 | 原文 |",
        "|---|---|---|---|---|",
        ...knowledge.evidence.map((item) =>
          `| ${textCell(item.publisher || "未登记发布者")} | ${textCell(item.source_class)} | ${textCell(item.domain)} | ${textCell(item.document_published_at || item.valid_from)} | ${item.canonical_url ? `[打开原文](${String(item.canonical_url)})` : "—"} |`,
        ),
        "",
        "## 已登记事实",
        "",
        "| 指标 | 数值 | 期间 | 口径 | 时效 |",
        "|---|---:|---|---|---|",
        ...knowledge.facts.map((item) =>
          `| ${textCell(item.metric)} | ${textCell(item.value)} ${textCell(item.unit)} | ${textCell(item.period)} | ${textCell(item.scope_key)} | ${textCell(item.freshness_status)} |`,
        ),
      ];
      content = lines.join("\n");
    } else if (artifactId === "history_delta") {
      content = deltaMarkdown(report.history_delta as Record<string, unknown> | undefined);
    } else if (artifactId === "history") {
      const history = await api.getResearchSymbolHistory(report.symbol, 50);
      content = [
        "# 历史研究",
        "",
        `证券：${report.security_name}（${report.symbol}）`,
        "",
        "| 报告 ID | Revision | 已验证 Fact | 覆盖快照 |",
        "|---|---:|---:|---|",
        ...history.reports.map((item) =>
          `| \`${textCell(item.report_id)}\` | ${textCell(item.revision)} | ${textCell(item.fact_count)} | \`${textCell(item.coverage_snapshot_id)}\` |`,
        ),
        "",
        "> 历史结论只作为“上次判断”保存，不能替代本次 Evidence。",
      ].join("\n");
    }
    if (!content) throw new Error("这份报告暂时没有可预览的 Markdown 内容");
    return {
      title: artifactId === "diff"
        ? `${report.security_name}（${report.symbol}）版本差异`
        : `${report.security_name}（${report.symbol}）穿透式深度研究`,
      filename: artifact?.filename || `${report.report_id}.md`,
      relativePath: artifact?.filename || `reports/${report.report_id}/${artifactId}.md`,
      content,
      updatedAt: report.updated_at,
      source: "deep_report",
      artifactId,
      availableArtifacts: [
        ...report.artifacts
        .filter((item) => item.available && ["markdown", "diagnostic", "diff"].includes(item.artifact_id))
          .map((item) => item.artifact_id as PreviewView),
        "sources",
        "history_delta",
        "history",
      ],
    };
  }

  throw new Error("这条消息没有关联可预览的报告");
}

function formatUpdatedAt(value?: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function ReportPreviewPanel({ target, onClose }: Props) {
  const [document, setDocument] = useState<PreviewDocument | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [selectedArtifact, setSelectedArtifact] = useState<PreviewView | undefined>(
    target.artifactId,
  );

  useEffect(() => {
    setSelectedArtifact(target.artifactId);
  }, [target.artifactId, target.reportId, target.runId]);

  useEffect(() => {
    let active = true;
    setDocument(null);
    setError(null);
    void loadPreview(target, selectedArtifact)
      .then((next) => {
        if (active) setDocument(next);
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setError(reason instanceof Error ? reason.message : "报告预览加载失败");
      });
    return () => {
      active = false;
    };
  }, [reloadToken, selectedArtifact, target.reportId, target.runId]);

  const updatedAt = useMemo(() => formatUpdatedAt(document?.updatedAt), [document?.updatedAt]);

  return (
    <>
      <button
        type="button"
        aria-label="关闭报告预览"
        onClick={onClose}
        className="fixed inset-0 z-40 bg-background/70 backdrop-blur-sm lg:hidden"
      />
      <aside
        aria-label="报告预览"
        className="fixed inset-y-0 right-0 z-50 flex w-[min(94vw,52rem)] flex-col border-l bg-background shadow-2xl lg:static lg:z-auto lg:h-full lg:min-w-[26rem] lg:w-[min(46vw,52rem)] lg:shadow-none"
      >
        <header className="flex min-h-16 items-start gap-3 border-b px-4 py-3">
          <div className="mt-0.5 rounded-lg bg-cyan-500/10 p-2 text-cyan-700 dark:text-cyan-300">
            <FileText className="h-4 w-4" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs font-medium text-cyan-700 dark:text-cyan-300">报告预览</span>
              {document?.source === "run" && (
                <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">完整运行产物</span>
              )}
              {document?.artifactId === "diagnostic" && (
                <span className="rounded bg-destructive/10 px-1.5 py-0.5 text-[10px] text-destructive">诊断产物</span>
              )}
              {document?.artifactId === "diff" && (
                <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">版本差异</span>
              )}
            </div>
            <h2 className="mt-0.5 truncate text-sm font-semibold" title={document?.title || target.title}>
              {document?.title || target.title || "加载报告中…"}
            </h2>
            {document && (
              <div className="mt-1 flex min-w-0 items-center gap-2 text-[10px] text-muted-foreground">
                <span className="min-w-0 truncate font-mono" title={document.relativePath}>{document.relativePath}</span>
                {updatedAt && <span className="shrink-0">更新 {updatedAt}</span>}
              </div>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            title="关闭预览"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        {document?.source === "deep_report" && document.availableArtifacts?.length ? (
          <nav aria-label="报告版本视图" className="flex gap-1 border-b bg-muted/20 px-4 py-2">
            {document.availableArtifacts.map((artifactId) => {
              const active = artifactId === (selectedArtifact || document.artifactId);
              const label = artifactId === "markdown" ? "当前报告"
                : artifactId === "diff" ? "与上一版差异"
                  : artifactId === "diagnostic" ? "诊断结果"
                    : artifactId === "sources" ? "本次使用的信息"
                      : artifactId === "history_delta" ? "与上次相比"
                        : "历史研究";
              return (
                <button
                  key={artifactId}
                  type="button"
                  aria-pressed={active}
                  onClick={() => setSelectedArtifact(artifactId)}
                  className={`rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors ${
                    active
                      ? "bg-background text-foreground shadow-sm ring-1 ring-border"
                      : "text-muted-foreground hover:bg-background/70 hover:text-foreground"
                  }`}
                >
                  {label}
                </button>
              );
            })}
          </nav>
        ) : null}

        <div className="min-h-0 flex-1 overflow-auto px-5 py-5">
          {!document && !error && (
            <div className="flex h-full min-h-48 items-center justify-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin text-primary" />
              正在加载完整 Markdown 报告…
            </div>
          )}
          {error && (
            <div role="alert" className="mx-auto mt-10 max-w-md rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm">
              <div className="flex items-start gap-2 text-destructive">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>{error}</span>
              </div>
              <button
                type="button"
                onClick={() => setReloadToken((value) => value + 1)}
                className="mt-3 inline-flex items-center gap-1.5 rounded-md border bg-background px-2.5 py-1.5 text-xs font-medium hover:bg-muted"
              >
                <RefreshCw className="h-3 w-3" />
                重新加载
              </button>
            </div>
          )}
          {document && (
            <article className="prose prose-sm max-w-none break-words dark:prose-invert prose-headings:scroll-mt-4 prose-table:border prose-table:border-border/50 prose-th:bg-muted/30 prose-th:px-3 prose-th:py-1.5 prose-td:px-3 prose-td:py-1.5 prose-th:text-left prose-th:text-xs prose-th:font-medium prose-td:text-xs [&_table]:block [&_table]:max-w-full [&_table]:overflow-x-auto">
              <ReactMarkdown remarkPlugins={remarkPlugins} rehypePlugins={rehypePlugins}>
                {document.content}
              </ReactMarkdown>
            </article>
          )}
        </div>
      </aside>
    </>
  );
}
