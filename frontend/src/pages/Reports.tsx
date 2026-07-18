import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  BookOpen,
  Download,
  FileText,
  GitCompare,
  Loader2,
  RefreshCw,
  Search,
  XCircle,
} from "lucide-react";
import { api, type DeepReportRecord, type RunListItem } from "@/lib/api";
import { formatMetricVal } from "@/lib/formatters";
import { cn } from "@/lib/utils";

const REPORT_SCAN_LIMIT = 100;

type SortMode = "created_desc" | "created_asc" | "return_desc" | "sharpe_desc";

export function Reports() {
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

  return (
    <div className="min-h-screen p-6 lg:p-8">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6">
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
                  穿透式单股深度研究
                </h2>
                <p className="mt-1 text-xs text-muted-foreground">独立于回测报告；每份报告都显示质量门控和数据缺口，仅在校验通过且产物可用时提供 PDF。</p>
              </div>
              <span className="rounded border px-2 py-0.5 font-mono text-xs text-muted-foreground">{deepReports.length}</span>
            </div>
            <div className="grid gap-2">
              {deepReports.slice(0, 12).map((report) => (
                <DeepReportRow key={report.report_id} report={report} />
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
                {status === "all" ? t("reports.allStatuses") : status}
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
          {t("reports.count", { shown: filtered.length, total: runs.length })}
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
          <section className="grid gap-3">
            {filtered.map((run) => (
              <ReportRow key={run.run_id} run={run} />
            ))}
          </section>
        ) : null}
      </div>
    </div>
  );
}

function DeepReportRow({ report }: { report: DeepReportRecord }) {
  const qualityTone = report.quality_status === "passed"
    ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
    : report.quality_status === "failed_validation"
      ? "bg-destructive/10 text-destructive"
      : "bg-amber-500/10 text-amber-700 dark:text-amber-300";
  const gapStatuses = new Set(["warning", "failed_validation", "insufficient_evidence", "not_requested"]);
  const inheritedGapModules = new Set(["executive_summary", "counter_thesis", "conclusion_watchlist"]);
  const gapModules = Object.entries(report.analysis_modules || {}).filter(([moduleId, item], index, entries) => {
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
  const canDownloadPdf = report.status === "completed" && !failedValidation && pdfAvailable;
  const automated = report.generation_source === "portfolio_monitor_autopilot";
  const reportTitle = `${report.security_name || report.symbol || "单股"}${report.symbol ? `（${report.symbol}）` : ""}穿透式深度研究`;
  return (
    <article className="flex flex-col gap-3 rounded-md border bg-background/80 p-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className={`rounded px-2 py-0.5 text-[11px] font-medium ${qualityTone}`}>
            {failedValidation ? "尚未形成正式报告" : report.quality_status === "passed" ? "证据完整，已通过校验" : "已完成，部分结论保留"}
          </span>
          <span className="rounded border border-primary/30 bg-primary/5 px-2 py-0.5 text-[11px] font-medium text-primary">
            穿透式深度研究
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
          {gapModules.length > 0 ? ` · ${gapModules.length} 项研究内容仍需补充` : ""}
        </div>
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
          <a
            href={api.deepReportArtifactUrl(
              report.report_id,
              failedValidation ? "diagnostic" : "markdown",
            )}
            className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium transition hover:bg-muted"
          >
            <FileText className="h-3.5 w-3.5" /> {failedValidation ? "查看未发布原因" : "阅读完整报告"}
          </a>
        ) : null}
        {diffAvailable ? (
          <a
            href={api.deepReportArtifactUrl(report.report_id, "diff")}
            className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium transition hover:bg-muted"
          >
            <FileText className="h-3.5 w-3.5" /> 版本差异
          </a>
        ) : null}
        {canDownloadPdf && (
          <a
            href={api.deepReportArtifactUrl(report.report_id, "pdf")}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition hover:opacity-90"
          >
            <Download className="h-3.5 w-3.5" /> PDF
          </a>
        )}
      </div>
    </article>
  );
}

const DEEP_REPORT_MODULE_LABELS: Record<string, string> = {
  executive_summary: "核心结论",
  business_position: "公司业务与产业位置",
  financial_quality: "三张报表与财务质量",
  accounting_review: "会计异常核查",
  implied_expectations: "市值隐含预期",
  terminal_narrative: "长期经营情景与叙事阶段",
  terminal_scenarios: "长期经营情景",
  counter_thesis: "反方、风险与催化剂",
  conclusion_watchlist: "结论与跟踪框架",
  report_gate: "整份报告门控",
  market_data: "市场数据",
  symbol_identity: "证券身份",
  latest_quarter: "最新季度",
};

function deepReportModuleLabel(moduleId: string): string {
  return DEEP_REPORT_MODULE_LABELS[moduleId] || moduleId;
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

function ReportRow({ run }: { run: RunListItem }) {
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
      {status || "unknown"}
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
  if (!Number.isFinite(parsed.getTime())) return value || "unknown";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}
