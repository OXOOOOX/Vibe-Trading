import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  BookOpen,
  FileText,
  GitCompare,
  Loader2,
  RefreshCw,
  Search,
  XCircle,
} from "lucide-react";
import { api, type DeepReportRecord, type RunListItem } from "@/lib/api";
import {
  ReportLibraryPanel,
  type ReportLibraryMode,
} from "@/components/reports/ReportLibraryPanel";
import {
  ReportPreviewSplitLayout,
  type ReportArtifactPreviewTarget,
} from "@/components/reports/ReportArtifactPreviewPane";
import { ReportSubjectGroup } from "@/components/reports/ReportSubjectGroup";
import { formatMetricVal } from "@/lib/formatters";
import { cn } from "@/lib/utils";
import {
  deepReportModuleLabel,
  deepReportTitle,
  deepReportTypeLabel,
  etfReadinessMessage,
} from "@/lib/deepReportPresentation";

const REPORT_SCAN_LIMIT = 100;

type SortMode = "created_desc" | "created_asc" | "return_desc" | "sharpe_desc";

export function Reports() {
  const [activeView, setActiveView] = useState<ReportLibraryMode | "legacy">("dossiers");
  const [previewTarget, setPreviewTarget] = useState<ReportArtifactPreviewTarget | null>(null);
  const tabs: Array<{ id: ReportLibraryMode | "legacy"; label: string }> = [
    { id: "dossiers", label: "标的档案" },
    { id: "all", label: "全部新报告" },
    { id: "portfolio", label: "组合档案" },
    { id: "legacy", label: "旧版报告" },
  ];

  function selectView(view: ReportLibraryMode | "legacy") {
    setActiveView(view);
    setPreviewTarget(null);
  }

  return (
    <ReportPreviewSplitLayout target={previewTarget} onClose={() => setPreviewTarget(null)}>
      <div className="h-full min-h-0 overflow-y-auto overscroll-contain p-6 lg:p-8">
        <div className="mx-auto flex w-full max-w-[1600px] flex-col gap-6">
          <section className="space-y-5 border-b pb-6">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
            <div className="space-y-3">
              <div className="inline-flex items-center gap-2 rounded-md border px-2.5 py-1 text-xs font-medium text-muted-foreground">
                <BookOpen className="h-3.5 w-3.5" />
                统一研究档案
              </div>
              <div>
                <h1 className="text-3xl font-bold tracking-tight">统一报告中心</h1>
                <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
                  按标的、报告周期和数据时点整理深度研究、日报与监控报告；先展示确定性的观点变化，再按需生成 AI 解释。
                </p>
              </div>
            </div>
          </div>
          <nav aria-label="报告中心视图" className="flex flex-wrap gap-2">
            {tabs.map((tab) => (
              <button
                key={tab.id}
                type="button"
                onClick={() => selectView(tab.id)}
                aria-pressed={activeView === tab.id}
                className={cn(
                  "rounded-md border px-3 py-2 text-sm font-medium transition",
                  activeView === tab.id
                    ? "border-primary bg-primary text-primary-foreground"
                    : "bg-background hover:bg-muted",
                )}
              >
                {tab.label}
              </button>
            ))}
          </nav>
          </section>

          {activeView === "legacy" ? (
            <LegacyReports onPreview={setPreviewTarget} />
          ) : (
            <ReportLibraryPanel mode={activeView} onPreview={setPreviewTarget} />
          )}
        </div>
      </div>
    </ReportPreviewSplitLayout>
  );
}

function LegacyReports({ onPreview }: { onPreview: (target: ReportArtifactPreviewTarget) => void }) {
  const { t } = useTranslation();
  const [runs, setRuns] = useState<RunListItem[]>([]);
  const [deepReports, setDeepReports] = useState<DeepReportRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [sortMode, setSortMode] = useState<SortMode>("created_desc");
  const [error, setError] = useState<string | null>(null);

  async function loadReports(mode: "initial" | "refresh" = "refresh") {
    if (mode === "initial") setLoading(true);
    else setRefreshing(true);
    setError(null);
    try {
      const [list, deep] = await Promise.all([
        api.listRuns(REPORT_SCAN_LIMIT),
        api.listDeepReports(REPORT_SCAN_LIMIT).catch(() => [] as DeepReportRecord[]),
      ]);
      setRuns(Array.isArray(list) ? list.filter(isBacktestReportRun) : []);
      setDeepReports(Array.isArray(deep) ? deep : []);
    } catch (err) {
      setRuns([]);
      setError(err instanceof Error ? err.message : t("reports.loadError"));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  useEffect(() => {
    void loadReports("initial");
  }, []);

  const statusOptions = useMemo(() => {
    const values = Array.from(new Set(runs.map((run) => run.status || "unknown"))).sort();
    return ["all", ...values];
  }, [runs]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const startMs = startDate ? Date.parse(startDate) : Number.NEGATIVE_INFINITY;
    const endMs = endDate ? Date.parse(`${endDate}T23:59:59`) : Number.POSITIVE_INFINITY;

    return [...runs]
      .filter((run) => {
        if (statusFilter !== "all" && (run.status || "unknown") !== statusFilter) return false;
        const created = Date.parse(run.created_at);
        if (Number.isFinite(created) && (created < startMs || created > endMs)) return false;
        if (!needle) return true;
        const haystack = [
          run.run_id,
          run.status,
          run.prompt,
          ...(run.codes || []),
          run.start_date,
          run.end_date,
        ].filter(Boolean).join(" ").toLowerCase();
        return haystack.includes(needle);
      })
      .sort((left, right) => compareRuns(left, right, sortMode));
  }, [runs, query, statusFilter, startDate, endDate, sortMode]);

  const deepReportGroups = useMemo(() => groupDeepReports(deepReports), [deepReports]);
  const runGroups = useMemo(() => groupLegacyRuns(filtered), [filtered]);

  return (
    <div className="min-w-0 space-y-6">
      <section className="flex flex-col gap-4 border-b pb-6 lg:flex-row lg:items-end lg:justify-between">
          <div className="space-y-3">
            <div className="inline-flex items-center gap-2 rounded-md border px-2.5 py-1 text-xs font-medium text-muted-foreground">
              <FileText className="h-3.5 w-3.5" />
              {t("reports.badge")}
            </div>
            <div>
              <h1 className="text-3xl font-bold tracking-tight">{t("reports.title")}</h1>
              <p className="mt-2 max-w-2xl text-sm text-muted-foreground">{t("reports.subtitle")}</p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => void loadReports("refresh")}
            disabled={refreshing}
            className="inline-flex items-center gap-2 rounded-md border px-4 py-2 text-sm font-medium transition hover:bg-muted disabled:opacity-50"
          >
            {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            {t("reports.refresh")}
          </button>
        </section>

      {deepReports.length > 0 ? (
          <section className="space-y-3 rounded-md border border-cyan-500/30 bg-cyan-500/[0.02] p-4">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h2 className="flex items-center gap-2 text-base font-semibold">
                  <BookOpen className="h-4 w-4 text-cyan-600" />
                  结构与穿透式深度研究
                </h2>
                <p className="mt-1 text-xs text-muted-foreground">独立于回测报告；每份报告都显示质量门控和数据缺口，仅在校验通过且产物可用时提供 PDF。</p>
              </div>
              <span className="rounded border px-2 py-0.5 font-mono text-xs text-muted-foreground">{deepReports.length}</span>
            </div>
            <div className="grid gap-3" aria-label="旧版深度研究按标的分组">
              {deepReportGroups.map((group) => (
                <ReportSubjectGroup
                  key={group.subjectKey}
                  subjectName={group.securityName}
                  subjectKey={group.symbol}
                  reportCount={group.reports.length}
                  latestLabel={`最近生成 ${formatRunDate(group.latestCreatedAt)}`}
                  badges={["深度研究"]}
                >
                  {group.reports.map((report) => (
                    <DeepReportRow key={report.report_id} report={report} onPreview={onPreview} />
                  ))}
                </ReportSubjectGroup>
              ))}
            </div>
          </section>
        ) : null}

      <section className="grid gap-3 lg:grid-cols-[minmax(220px,1fr)_160px_150px_150px_170px]">
          <label className="relative block">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t("reports.searchPlaceholder")}
              className="w-full rounded-md border bg-background py-2 pl-9 pr-3 text-sm outline-none transition focus:border-primary"
            />
          </label>
          <select
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value)}
            className="rounded-md border bg-background px-3 py-2 text-sm"
          >
            {statusOptions.map((status) => (
              <option key={status} value={status}>
                {status === "all" ? t("reports.allStatuses") : legacyStatusLabel(status)}
              </option>
            ))}
          </select>
          <input
            type="date"
            value={startDate}
            onChange={(event) => setStartDate(event.target.value)}
            className="rounded-md border bg-background px-3 py-2 text-sm"
            aria-label={t("reports.startDate")}
          />
          <input
            type="date"
            value={endDate}
            onChange={(event) => setEndDate(event.target.value)}
            className="rounded-md border bg-background px-3 py-2 text-sm"
            aria-label={t("reports.endDate")}
          />
          <select
            value={sortMode}
            onChange={(event) => setSortMode(event.target.value as SortMode)}
            className="rounded-md border bg-background px-3 py-2 text-sm"
            aria-label={t("reports.sort")}
          >
            <option value="created_desc">{t("reports.sortNewest")}</option>
            <option value="created_asc">{t("reports.sortOldest")}</option>
            <option value="return_desc">{t("reports.sortReturn")}</option>
            <option value="sharpe_desc">{t("reports.sortSharpe")}</option>
          </select>
        </section>

      <div className="text-sm text-muted-foreground">
          {t("reports.count", { shown: filtered.length, total: runs.length })} · {runGroups.length} 个标的
        </div>

      {loading ? (
          <div className="grid gap-3">
            {[1, 2, 3, 4].map((item) => (
              <div key={item} className="h-28 animate-pulse rounded-md border bg-muted/40" />
            ))}
          </div>
        ) : null}

      {!loading && error ? (
          <section className="rounded-md border border-amber-500/30 bg-amber-500/5 p-5">
            <div className="flex items-center gap-2 font-medium text-amber-700 dark:text-amber-300">
              <AlertTriangle className="h-5 w-5" />
              {t("reports.unavailable")}
            </div>
            <p className="mt-2 text-sm text-muted-foreground">{error}</p>
          </section>
        ) : null}

      {!loading && !error && filtered.length === 0 ? (
          <section className="rounded-md border border-dashed p-8 text-center">
            <FileText className="mx-auto h-8 w-8 text-muted-foreground" />
            <h2 className="mt-3 font-medium">{runs.length === 0 ? t("reports.emptyTitle") : t("reports.noMatchesTitle")}</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              {runs.length === 0 ? t("reports.emptyBody") : t("reports.noMatchesBody")}
            </p>
          </section>
        ) : null}

      {!loading && !error && filtered.length > 0 ? (
          <section className="grid gap-3" aria-label="旧版回测报告按标的分组">
            {runGroups.map((group) => (
              <ReportSubjectGroup
                key={group.subjectKey}
                subjectName={group.subjectName}
                subjectKey={group.subjectLabel}
                reportCount={group.reports.length}
                latestLabel={`最近生成 ${formatRunDate(group.latestCreatedAt)}`}
              >
                {group.reports.map((run) => (
                  <ReportRow key={run.run_id} run={run} onPreview={onPreview} />
                ))}
              </ReportSubjectGroup>
            ))}
          </section>
        ) : null}
    </div>
  );
}

function DeepReportRow({
  report,
  onPreview,
}: {
  report: DeepReportRecord;
  onPreview: (target: ReportArtifactPreviewTarget) => void;
}) {
  const qualityTone = report.quality_status === "passed"
    ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
    : report.quality_status === "failed_validation"
      ? "bg-destructive/10 text-destructive"
      : "bg-amber-500/10 text-amber-700 dark:text-amber-300";
  const gapStatuses = new Set(["warning", "failed_validation", "insufficient_evidence", "not_requested"]);
  const inheritedGapModules = new Set(["executive_summary", "counter_thesis", "conclusion_watchlist"]);
  const moduleStatuses = Object.keys(report.report_sections || {}).length > 0
    ? report.report_sections || {}
    : report.analysis_modules || {};
  const gapModules = Object.entries(moduleStatuses).filter(([moduleId, item], index, entries) => {
    if (
      !gapStatuses.has(item.status)
      || (inheritedGapModules.has(moduleId) && item.status !== "failed_validation")
    ) return false;
    const label = deepReportModuleLabel(moduleId);
    return entries.findIndex(([candidateId, candidate]) => (
      gapStatuses.has(candidate.status)
      && (!inheritedGapModules.has(candidateId) || candidate.status === "failed_validation")
      && deepReportModuleLabel(candidateId) === label
    )) === index;
  });
  const failedValidation = report.quality_status === "failed_validation";
  const markdownAvailable = report.artifacts?.some(
    (artifact) => artifact.artifact_id === "markdown" && artifact.available === true,
  );
  const diagnosticAvailable = report.artifacts?.some(
    (artifact) => artifact.artifact_id === "diagnostic" && artifact.available === true,
  );
  const diffAvailable = report.artifacts?.some(
    (artifact) => artifact.artifact_id === "diff" && artifact.available === true,
  );
  const pdfAvailable = report.artifacts?.some(
    (artifact) => artifact.artifact_id === "pdf" && artifact.available === true,
  );
  const monitoringBundleAvailable = report.artifacts?.some(
    (artifact) => artifact.artifact_id === "monitoring_bundle" && artifact.available === true,
  );
  const canDownloadPdf = report.status === "completed" && !failedValidation && pdfAvailable;
  const automated = report.generation_source === "portfolio_monitor_autopilot";
  const reportTitle = deepReportTitle(
    report.security_name,
    report.symbol,
    report.profile,
    report.quality_status,
    report.etf_readiness,
  );
  const reportType = deepReportTypeLabel(report.profile, report.quality_status, report.etf_readiness);
  const componentCoverage = report.etf_readiness?.metrics?.component_research_coverage;
  return (
    <article className="flex flex-col gap-3 rounded-md border bg-background/80 p-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className={`rounded px-2 py-0.5 text-[11px] font-medium ${qualityTone}`}>
            {failedValidation ? "尚未形成正式报告" : report.quality_status === "passed" ? "证据完整，已通过校验" : "已完成，部分结论保留"}
          </span>
          <span className="rounded border border-primary/30 bg-primary/5 px-2 py-0.5 text-[11px] font-medium text-primary">
            {reportType}
          </span>
          {automated ? (
            <span className="rounded border border-violet-500/30 bg-violet-500/5 px-2 py-0.5 text-[11px] font-medium text-violet-700 dark:text-violet-300">
              AI 自主监控生成
            </span>
          ) : null}
          <span className="text-xs text-muted-foreground">{report.report_date}</span>
        </div>
        <div className="mt-1 font-medium">{reportTitle}</div>
        <div className="mt-1 text-xs text-muted-foreground">
          第 {report.revision} 版 · 数据更新至 {formatDeepReportDataTime(report.data_as_of)}
          {typeof componentCoverage === "number" ? ` · 成分研究覆盖 ${(componentCoverage * 100).toFixed(1)}%` : ""}
          {gapModules.length > 0 ? ` · ${gapModules.length} 项研究内容仍需补充` : ""}
        </div>
        {report.etf_readiness && report.etf_readiness.status !== "penetration_ready" ? (
          <div className="mt-2 rounded border border-amber-500/30 bg-amber-500/5 px-2.5 py-1.5 text-xs text-amber-700 dark:text-amber-300">
            {etfReadinessMessage(report.etf_readiness)}
          </div>
        ) : null}
        {failedValidation ? (
          <div role="alert" className="mt-2 rounded border border-destructive/30 bg-destructive/5 px-2.5 py-1.5 text-xs text-destructive">
            关键数据或内容没有通过发布前校验；当前只提供诊断结果，不会生成 PDF。
          </div>
        ) : null}
        {gapModules.length > 0 ? (
          <div className="mt-2 flex flex-wrap gap-1.5" aria-label="仍需补充的研究内容">
            {gapModules.map(([moduleId, module]) => (
              <span
                key={moduleId}
                className="rounded border bg-muted/30 px-2 py-0.5 text-[11px] text-muted-foreground"
                title={module.reason || undefined}
              >
                {deepReportModuleLabel(moduleId)} · {moduleStatusLabel(module.status)}
              </span>
            ))}
          </div>
        ) : null}
        {automated && report.generation_reason ? (
          <div className="mt-1 line-clamp-2 text-xs text-muted-foreground">
            自动触发原因：{report.generation_reason}
          </div>
        ) : null}
      </div>
      <div className="flex shrink-0 flex-wrap gap-2">
        {markdownAvailable || diagnosticAvailable ? (
          <button
            type="button"
            onClick={() => onPreview(deepReportPreviewTarget(
              report,
              failedValidation ? "diagnostic" : "markdown",
            ))}
            className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium transition hover:bg-muted"
          >
            <FileText className="h-3.5 w-3.5" /> {failedValidation ? "查看未发布原因" : "阅读完整报告"}
          </button>
        ) : null}
        {diffAvailable ? (
          <button
            type="button"
            onClick={() => onPreview(deepReportPreviewTarget(report, "diff"))}
            className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium transition hover:bg-muted"
          >
            <FileText className="h-3.5 w-3.5" /> 版本差异
          </button>
        ) : null}
        {monitoringBundleAvailable ? (
          <button
            type="button"
            onClick={() => onPreview(deepReportPreviewTarget(report, "monitoring_bundle"))}
            className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium transition hover:bg-muted"
          >
            <FileText className="h-3.5 w-3.5" /> 结构监控 JSON
          </button>
        ) : null}
        {canDownloadPdf && (
          <button
            type="button"
            onClick={() => onPreview(deepReportPreviewTarget(report, "pdf"))}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition hover:opacity-90"
          >
            <FileText className="h-3.5 w-3.5" /> 预览 PDF
          </button>
        )}
      </div>
    </article>
  );
}

function moduleStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    warning: "需关注",
    failed_validation: "校验失败",
    insufficient_evidence: "证据不足",
    not_requested: "未执行",
  };
  return labels[status] || status;
}

function formatDeepReportDataTime(value?: string | null): string {
  if (!value) return "尚未明确";
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

function deepReportPreviewTarget(
  report: DeepReportRecord,
  initialArtifactId: "markdown" | "pdf" | "diagnostic" | "diff" | "monitoring_bundle",
): ReportArtifactPreviewTarget {
  const supported = new Set(["markdown", "pdf", "diagnostic", "diff", "monitoring_bundle"]);
  const artifacts = (report.artifacts || [])
    .filter((artifact) => artifact.available && supported.has(artifact.artifact_id))
    .map((artifact) => {
      const artifactId = artifact.artifact_id as "markdown" | "pdf" | "diagnostic" | "diff" | "monitoring_bundle";
      return {
        artifactId,
        label: artifactId === "pdf" ? "PDF"
          : artifactId === "diagnostic" ? "诊断 Markdown"
            : artifactId === "diff" ? "版本差异"
              : artifactId === "monitoring_bundle" ? "结构监控 JSON"
              : "Markdown",
        filename: artifact.filename,
        mediaType: artifactId === "pdf" ? "application/pdf"
          : artifactId === "monitoring_bundle" ? "application/json"
            : "text/markdown",
        previewUrl: api.deepReportArtifactUrl(report.report_id, artifactId, "preview"),
        downloadUrl: api.deepReportArtifactUrl(report.report_id, artifactId, "download"),
      };
    });
  return {
    source: "deep_report",
    reportId: report.report_id,
    title: deepReportTitle(
      report.security_name,
      report.symbol,
      report.profile,
      report.quality_status,
      report.etf_readiness,
    ),
    subtitle: `第 ${report.revision} 版 · 数据截至 ${formatDeepReportDataTime(report.data_as_of)}`,
    initialArtifactId,
    artifacts,
  };
}

function runReportPreviewTarget(run: RunListItem): ReportArtifactPreviewTarget {
  return {
    source: "run",
    reportId: run.run_id,
    title: run.run_id,
    subtitle: run.prompt || "历史回测 Markdown 报告",
    initialArtifactId: "markdown",
    artifacts: [{
      artifactId: "markdown",
      label: "Markdown",
      filename: `${run.run_id}.md`,
      mediaType: "text/markdown",
      previewUrl: api.runReportArtifactUrl(run.run_id, "preview"),
      downloadUrl: api.runReportArtifactUrl(run.run_id, "download"),
    }],
  };
}

function ReportRow({
  run,
  onPreview,
}: {
  run: RunListItem;
  onPreview: (target: ReportArtifactPreviewTarget) => void;
}) {
  const { t } = useTranslation();
  return (
    <article className="rounded-md border p-4 transition hover:border-primary/40 hover:bg-muted/30">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status={run.status} />
            <Link to={`/runs/${run.run_id}`} className="truncate font-mono text-sm font-medium hover:text-primary">
              {run.run_id}
            </Link>
            <span className="text-xs text-muted-foreground">{formatRunDate(run.created_at)}</span>
          </div>
          <p className="line-clamp-2 text-sm text-muted-foreground">{run.prompt || t("reports.noPrompt")}</p>
          <div className="flex flex-wrap gap-1.5">
            {(run.codes || []).slice(0, 6).map((code) => (
              <span key={code} className="rounded border px-2 py-0.5 font-mono text-xs text-muted-foreground">
                {code}
              </span>
            ))}
            {run.start_date || run.end_date ? (
              <span className="rounded border px-2 py-0.5 text-xs text-muted-foreground">
                {run.start_date || "?"} {t("reports.to")} {run.end_date || "?"}
              </span>
            ) : null}
          </div>
        </div>

        <div className="flex flex-col gap-3 lg:items-end">
          <div className="grid grid-cols-2 gap-2 text-right sm:flex sm:flex-wrap sm:justify-end">
            <MetricPill label={t("reports.return")} value={formatOptionalMetric("total_return", run.total_return)} />
            <MetricPill label={t("reports.sharpe")} value={formatOptionalMetric("sharpe", run.sharpe)} />
          </div>
          <div className="flex flex-wrap gap-2 lg:justify-end">
            <button
              type="button"
              onClick={() => onPreview(runReportPreviewTarget(run))}
              className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium transition hover:bg-muted"
            >
              <FileText className="h-3.5 w-3.5" /> Markdown
            </button>
            <Link
              to={`/runs/${run.run_id}`}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition hover:opacity-90"
            >
              {t("reports.fullReport")} <ArrowRight className="h-3.5 w-3.5" />
            </Link>
            <Link
              to="/compare"
              className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium transition hover:bg-muted"
            >
              <GitCompare className="h-3.5 w-3.5" />
              {t("reports.compare")}
            </Link>
          </div>
        </div>
      </div>
    </article>
  );
}

function StatusBadge({ status }: { status: string }) {
  const normalized = status.toLowerCase();
  const ok = ["success", "done", "completed", "complete"].includes(normalized);
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs font-medium",
        ok ? "bg-success/10 text-success" : "bg-muted text-muted-foreground",
      )}
    >
      {ok ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
      {legacyStatusLabel(status)}
    </span>
  );
}

function MetricPill({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border px-3 py-1.5">
      <div className="text-[11px] uppercase text-muted-foreground">{label}</div>
      <div className="font-mono text-sm font-medium">{value}</div>
    </div>
  );
}

function isBacktestReportRun(run: RunListItem): boolean {
  return Number.isFinite(run.total_return) || Number.isFinite(run.sharpe);
}

function compareRuns(left: RunListItem, right: RunListItem, mode: SortMode): number {
  if (mode === "created_asc") return dateMs(left.created_at) - dateMs(right.created_at);
  if (mode === "return_desc") return metric(right.total_return) - metric(left.total_return);
  if (mode === "sharpe_desc") return metric(right.sharpe) - metric(left.sharpe);
  return dateMs(right.created_at) - dateMs(left.created_at);
}

interface LegacyDeepReportGroup {
  subjectKey: string;
  symbol: string;
  securityName: string;
  latestCreatedAt: string;
  reports: DeepReportRecord[];
}

function groupDeepReports(reports: DeepReportRecord[]): LegacyDeepReportGroup[] {
  const grouped = new Map<string, DeepReportRecord[]>();
  for (const report of reports) {
    const subjectKey = report.symbol?.trim().toUpperCase() || report.security_name?.trim() || "未标注标的";
    const current = grouped.get(subjectKey);
    if (current) current.push(report);
    else grouped.set(subjectKey, [report]);
  }

  return Array.from(grouped.entries())
    .map(([subjectKey, subjectReports]) => {
      const sortedReports = [...subjectReports].sort((left, right) => dateMs(right.created_at) - dateMs(left.created_at));
      const latest = sortedReports[0];
      return {
        subjectKey,
        symbol: latest.symbol || subjectKey,
        securityName: latest.security_name || latest.symbol || "未标注标的",
        latestCreatedAt: latest.created_at,
        reports: sortedReports,
      };
    })
    .sort((left, right) => dateMs(right.latestCreatedAt) - dateMs(left.latestCreatedAt));
}

interface LegacyRunGroup {
  subjectKey: string;
  subjectName: string;
  subjectLabel?: string;
  latestCreatedAt: string;
  reports: RunListItem[];
}

function groupLegacyRuns(runs: RunListItem[]): LegacyRunGroup[] {
  const grouped = new Map<string, RunListItem[]>();
  for (const run of runs) {
    const codes = normalizedRunCodes(run);
    const subjectKey = codes.length > 0 ? codes.join("+") : "__unlabeled__";
    const current = grouped.get(subjectKey);
    if (current) current.push(run);
    else grouped.set(subjectKey, [run]);
  }

  return Array.from(grouped.entries()).map(([subjectKey, reports]) => {
    const codes = normalizedRunCodes(reports[0]);
    let latestCreatedAt = reports[0]?.created_at || "";
    for (const report of reports) {
      if (dateMs(report.created_at) > dateMs(latestCreatedAt)) latestCreatedAt = report.created_at;
    }
    return {
      subjectKey,
      subjectName: codes.length === 0 ? "未标注标的" : codes.length === 1 ? codes[0] : `多标的组合（${codes.length}）`,
      subjectLabel: codes.length > 1 ? codes.join(" / ") : undefined,
      latestCreatedAt,
      reports,
    };
  });
}

function normalizedRunCodes(run: RunListItem): string[] {
  return Array.from(new Set((run.codes || []).map((code) => code.trim().toUpperCase()).filter(Boolean))).sort();
}

function metric(value: number | undefined): number {
  return Number.isFinite(value) ? Number(value) : Number.NEGATIVE_INFINITY;
}

function formatOptionalMetric(key: string, value: number | undefined): string {
  return Number.isFinite(value) ? formatMetricVal(key, value as number) : "-";
}

function dateMs(value: string): number {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function formatRunDate(value: string): string {
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return value || "时间未记录";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function legacyStatusLabel(value?: string | null): string {
  const normalized = String(value || "unknown").toLowerCase();
  return ({
    success: "成功",
    done: "已完成",
    completed: "已完成",
    complete: "已完成",
    failed: "失败",
    error: "错误",
    running: "运行中",
    pending: "等待中",
    unknown: "状态未记录",
  } as Record<string, string>)[normalized] || value || "状态未记录";
}
