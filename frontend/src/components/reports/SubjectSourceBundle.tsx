import { useMemo, useState } from "react";
import {
  BookMarked,
  CalendarRange,
  ChevronDown,
  ChevronUp,
  Database,
  ExternalLink,
  FileCheck2,
  FileSpreadsheet,
  Loader2,
  Newspaper,
  RefreshCw,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import type {
  AnnualReportBackfillJob,
  AnnualReportBackfillPhaseStatus,
  AnnualReportCoverage,
  ReportSourceBundle,
  ReportSourceDocument,
  SourceKind,
  SourceVerificationStatus,
} from "@/lib/api";
import { cn } from "@/lib/utils";

const DOMAIN_ICONS: Partial<Record<SourceKind, typeof FileCheck2>> = {
  official_filing: FileCheck2,
  company_disclosure: FileCheck2,
  index_constituents: FileSpreadsheet,
  market_data: Database,
  structured_financial: FileSpreadsheet,
  consensus_data: BookMarked,
  derived_analysis: FileSpreadsheet,
  fundamental: FileSpreadsheet,
  news: Newspaper,
  broker_research: BookMarked,
  report: BookMarked,
};

const STATUS_LABELS: Record<SourceVerificationStatus, string> = {
  official_primary: "官方原文",
  live_retrieved: "已实时抓取",
  source_recorded: "来源已记录",
  historical_context: "历史缓存",
};

const STATUS_STYLES: Record<SourceVerificationStatus, string> = {
  official_primary: "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  live_retrieved: "border-cyan-500/30 bg-cyan-500/10 text-cyan-700 dark:text-cyan-300",
  source_recorded: "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300",
  historical_context: "border-border bg-muted text-muted-foreground",
};

const STRUCTURED_STATUS = {
  validated: {
    label: "已结构化",
    style: "border-blue-500/30 bg-blue-500/10 text-blue-700 dark:text-blue-300",
  },
  needs_review: {
    label: "结构化待复核",
    style: "border-orange-500/30 bg-orange-500/10 text-orange-700 dark:text-orange-300",
  },
} as const;

const STRUCTURED_CHECK_LABELS: Record<string, string> = {
  reporting_period_present: "报告期识别",
  minimum_metric_count: "最低指标数量",
  numeric_values_replayable: "原文数值回放",
  metric_values_plausible: "指标合理性",
  balance_sheet_scale_consistent: "资产负债数量级",
  balance_sheet_reconciles: "资产负债表勾稽",
  cross_source_consistent: "跨来源一致性",
};

const ASSOCIATION_SCOPE_LABELS: Record<string, string> = {
  direct_subject: "直接标的",
  etf_product: "ETF 产品",
  tracking_index: "跟踪指数",
  industry_theme: "行业主题",
  key_constituent: "成分股延伸",
};

type VerificationFilter = "all" | SourceVerificationStatus;

const DEFAULT_DOMAIN_PREVIEW_LIMIT = 3;
const NEWS_DOMAIN_PREVIEW_LIMIT = 5;
const ANNUAL_PHASE_LABELS = {
  discovery: "发现",
  download: "下载",
  parsing: "解析",
  validation: "校验",
} as const;
const ANNUAL_JOB_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "后台执行中",
  completed: "已完成",
  completed_with_gaps: "完成但仍有缺口",
  failed: "执行失败",
  cancelled: "已取消",
  interrupted: "服务重启中断",
};
const ANNUAL_YEAR_STATUS_LABELS: Record<string, string> = {
  pending: "等待",
  running: "处理中",
  completed: "已通过",
  reused: "已复用",
  needs_review: "待复核",
  failed: "失败",
};
const OVERVIEW_SOURCE_KINDS = new Set<SourceKind>([
  "fund_product",
  "index_methodology",
  "index_constituents",
  "fund_share_scale",
  "market_data",
]);

function domainPreviewLimit(kind: SourceKind): number {
  return kind === "news" || kind === "broker_research" ? NEWS_DOMAIN_PREVIEW_LIMIT : DEFAULT_DOMAIN_PREVIEW_LIMIT;
}

function sourceDocumentTimestamp(document: ReportSourceDocument, field: "published_at" | "retrieved_at"): number {
  const value = document[field];
  if (!value) return 0;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function compareSourceDocumentsNewestFirst(left: ReportSourceDocument, right: ReportSourceDocument): number {
  const publishedDelta = sourceDocumentTimestamp(right, "published_at") - sourceDocumentTimestamp(left, "published_at");
  if (publishedDelta !== 0) return publishedDelta;
  const retrievedDelta = sourceDocumentTimestamp(right, "retrieved_at") - sourceDocumentTimestamp(left, "retrieved_at");
  if (retrievedDelta !== 0) return retrievedDelta;
  return left.document_id.localeCompare(right.document_id);
}

export function SubjectSourceBundle({
  bundle,
  refreshing = false,
  repairing = false,
  annualReportsBackfilling = false,
  annualReportBackfillJob = null,
  annualReportCoverage = null,
  showOverviewSources = true,
  supplementaryOnly = false,
  onRefresh,
  onRepair,
  onBackfillAnnualReports,
}: {
  bundle: ReportSourceBundle;
  refreshing?: boolean;
  repairing?: boolean;
  annualReportsBackfilling?: boolean;
  annualReportBackfillJob?: AnnualReportBackfillJob | null;
  annualReportCoverage?: AnnualReportCoverage | null;
  showOverviewSources?: boolean;
  supplementaryOnly?: boolean;
  onRefresh?: () => void;
  onRepair?: () => void;
  onBackfillAnnualReports?: (years: number) => void;
}) {
  const [verificationFilter, setVerificationFilter] = useState<VerificationFilter>("all");
  const [usedOnly, setUsedOnly] = useState(false);
  const [recentBrokerOnly, setRecentBrokerOnly] = useState(false);
  const [brokerPublisher, setBrokerPublisher] = useState("");
  const [expandedDomains, setExpandedDomains] = useState<Set<SourceKind>>(() => new Set());
  const [annualYearCount, setAnnualYearCount] = useState(8);
  const sourceDomains = useMemo(() => bundle.domains
    .filter((domain) => (
      (showOverviewSources || !OVERVIEW_SOURCE_KINDS.has(domain.kind))
      && domain.documents.length > 0
    ))
    .map((domain) => ({
      ...domain,
      documents: [...domain.documents].sort(compareSourceDocumentsNewestFirst),
    })), [bundle.domains, showOverviewSources]);
  const visibleDomains = useMemo(() => sourceDomains.map((domain) => ({
    ...domain,
    documents: domain.documents.filter((document) => (
      (verificationFilter === "all" || document.verification_status === verificationFilter)
      && (!usedOnly || (document.used_by_report_count || 0) > 0)
      && (domain.kind !== "broker_research" || !recentBrokerOnly || isWithinLastYear(document.published_at))
      && (domain.kind !== "broker_research" || !brokerPublisher.trim() || document.publisher.toLowerCase().includes(brokerPublisher.trim().toLowerCase()))
    )),
  })), [brokerPublisher, recentBrokerOnly, sourceDomains, usedOnly, verificationFilter]);
  const counts = useMemo(() => {
    if (showOverviewSources) return bundle.verification_counts;
    const visibleCounts: Record<SourceVerificationStatus, number> = {
      official_primary: 0,
      live_retrieved: 0,
      source_recorded: 0,
      historical_context: 0,
    };
    sourceDomains.forEach((domain) => {
      domain.documents.forEach((document) => {
        visibleCounts[document.verification_status] += 1;
      });
    });
    return visibleCounts;
  }, [bundle.verification_counts, showOverviewSources, sourceDomains]);
  const traceableCount = showOverviewSources
    ? bundle.traceable_count
    : sourceDomains.reduce((total, domain) => total + domain.documents.length, 0);
  const repairableCount = useMemo(() => sourceDomains.reduce(
    (total, domain) => total + domain.documents.filter((document) => (
      document.structured_auto_repair_available
      || document.structured_status === "needs_review"
      || document.structured_status === "failed"
    )).length,
    0,
  ), [sourceDomains]);
  const annualCoverageDomainKind = useMemo(() => (
    sourceDomains.find((domain) => domain.kind === "official_filing")?.kind
    || sourceDomains.find((domain) => domain.kind === "company_disclosure")?.kind
    || null
  ), [sourceDomains]);
  const visibleAnnualBackfillJob = annualReportBackfillJob?.status === "completed"
    ? null
    : annualReportBackfillJob;

  return (
    <section className="rounded-lg border bg-card p-5" aria-labelledby="subject-source-bundle-title">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="flex items-center gap-2 text-xs font-medium text-emerald-700 dark:text-emerald-300">
            <ShieldCheck className="h-4 w-4" /> 分级取证
          </div>
          <h3 id="subject-source-bundle-title" className="mt-1 text-lg font-semibold">标的资料与证据</h3>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            {supplementaryOnly
              ? "产品、指数成分、份额和行情已在上方集中展示；此处只保留新闻、券商报告、年报/定期报告及其他补充证据。新闻可追溯不等于新闻内容已经事实认证。"
              : "标的档案保留全部可追溯资料；单篇报告只关联实际使用的来源。新闻可追溯不等于新闻内容已经事实认证。"}
          </p>
        </div>
        <div className="flex flex-wrap items-start gap-2">
          <SourceCount label="官方原文" value={counts.official_primary} />
          <SourceCount label="实时抓取" value={counts.live_retrieved} />
          <SourceCount label="来源记录" value={counts.source_recorded} />
          {onBackfillAnnualReports ? (
            <div className="inline-flex h-[52px] items-stretch overflow-hidden rounded-md border">
              <label className="sr-only" htmlFor="annual-report-year-count">历史年报范围</label>
              <select
                id="annual-report-year-count"
                value={annualYearCount}
                onChange={(event) => setAnnualYearCount(Number(event.target.value))}
                disabled={annualReportsBackfilling || refreshing}
                className="border-r bg-background px-2 text-xs outline-none disabled:opacity-50"
              >
                {[5, 8, 10, 12].map((years) => (
                  <option key={years} value={years}>近 {years} 年</option>
                ))}
              </select>
              <button
                type="button"
                onClick={() => onBackfillAnnualReports(annualYearCount)}
                disabled={annualReportsBackfilling || refreshing}
                className="inline-flex items-center gap-2 px-3 text-xs font-medium hover:bg-muted disabled:opacity-50"
              >
                {annualReportsBackfilling
                  ? <Loader2 className="h-4 w-4 animate-spin" />
                  : <CalendarRange className="h-4 w-4" />}
                {annualReportsBackfilling ? "补齐年报中" : "补齐历史年报"}
              </button>
            </div>
          ) : null}
          {onRepair && repairableCount > 0 ? (
            <button
              type="button"
              onClick={onRepair}
              disabled={repairing || refreshing}
              className="inline-flex h-[52px] items-center gap-2 rounded-md border border-orange-500/30 bg-orange-500/5 px-3 text-xs font-medium text-orange-700 hover:bg-orange-500/10 disabled:opacity-50 dark:text-orange-300"
              aria-label={`自动修复结构化（${repairableCount} 份）`}
            >
              {repairing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Wrench className="h-4 w-4" />}
              {repairing ? "自动修复中" : `自动修复结构化 · ${repairableCount}`}
            </button>
          ) : null}
          {onRefresh ? (
            <button
              type="button"
              onClick={onRefresh}
              disabled={refreshing}
              className="inline-flex h-[52px] items-center gap-2 rounded-md border px-3 text-xs font-medium hover:bg-muted disabled:opacity-50"
            >
              {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              刷新全部资料
            </button>
          ) : null}
        </div>
      </div>

      {visibleAnnualBackfillJob ? (
        <AnnualReportBackfillProgress job={visibleAnnualBackfillJob} />
      ) : null}

      <div className="mt-4 flex flex-wrap gap-2" aria-label="资料筛选">
        {(["all", "official_primary", "live_retrieved", "source_recorded", "historical_context"] as const).map((status) => (
          <button
            key={status}
            type="button"
            onClick={() => setVerificationFilter(status)}
            aria-pressed={verificationFilter === status}
            className={cn(
              "rounded-md border px-2.5 py-1.5 text-xs",
              verificationFilter === status ? "border-primary bg-primary/10 text-primary" : "text-muted-foreground hover:bg-muted",
            )}
          >
            {status === "all" ? "全部等级" : STATUS_LABELS[status]}
          </button>
        ))}
        <button
          type="button"
          onClick={() => setUsedOnly((current) => !current)}
          aria-pressed={usedOnly}
          className={cn(
            "rounded-md border px-2.5 py-1.5 text-xs",
            usedOnly ? "border-primary bg-primary/10 text-primary" : "text-muted-foreground hover:bg-muted",
          )}
        >
          仅看已被报告使用
        </button>
        {sourceDomains.some((domain) => domain.kind === "broker_research") ? (
          <>
            <button
              type="button"
              onClick={() => setRecentBrokerOnly((current) => !current)}
              aria-pressed={recentBrokerOnly}
              className={cn(
                "rounded-md border px-2.5 py-1.5 text-xs",
                recentBrokerOnly ? "border-violet-500 bg-violet-500/10 text-violet-700 dark:text-violet-300" : "text-muted-foreground hover:bg-muted",
              )}
            >
              券商研报仅看最近一年
            </button>
            <input
              value={brokerPublisher}
              onChange={(event) => setBrokerPublisher(event.target.value)}
              aria-label="按券商筛选"
              placeholder="按券商筛选"
              className="w-32 rounded-md border bg-background px-2.5 py-1.5 text-xs outline-none focus:border-primary"
            />
          </>
        ) : null}
      </div>

      <div className="mt-4 grid items-start gap-3 xl:grid-cols-2">
        {visibleDomains.map((domain) => {
          const Icon = DOMAIN_ICONS[domain.kind] || Database;
          const previewLimit = domainPreviewLimit(domain.kind);
          const expanded = expandedDomains.has(domain.kind);
          const hasMore = domain.documents.length > previewLimit;
          const visibleDocuments = expanded ? domain.documents : domain.documents.slice(0, previewLimit);
          const documentListId = `source-domain-${bundle.symbol.replace(/[^a-z0-9_-]/gi, "-")}-${domain.kind}`;
          return (
            <article key={domain.kind} className="min-w-0 rounded-md border bg-background/70 p-3">
              <div className="flex items-start justify-between gap-3">
                <div className="flex min-w-0 items-start gap-2">
                  <Icon className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                  <div>
                    <h4 className="text-sm font-semibold">{domain.label}</h4>
                    <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{domain.description}</p>
                    {annualReportCoverage && domain.kind === annualCoverageDomainKind ? (
                      <AnnualReportCoverageSummary coverage={annualReportCoverage} />
                    ) : null}
                  </div>
                </div>
                <span className="rounded border px-2 py-0.5 text-[11px] text-muted-foreground">{domain.documents.length}</span>
              </div>
              {domain.kind === "broker_research" ? (
                <div className="mt-3 rounded border border-violet-500/25 bg-violet-500/5 p-2 text-[11px] leading-4 text-violet-800 dark:text-violet-200">
                  券商观点 · B 级专业来源。目标价、盈利预测和策略判断不等于官方事实；与公告、交易所或基金公司资料冲突时，以官方资料为准并保留分歧。
                </div>
              ) : null}
              {domain.documents.length > 0 ? (
                <div id={documentListId} className="mt-3 divide-y">
                  {visibleDocuments.map((document) => (
                    <SourceDocumentRow key={document.document_id} document={document} />
                  ))}
                </div>
              ) : (
                <div className="mt-3 rounded border border-dashed p-3 text-xs text-muted-foreground">
                  当前筛选条件下暂无资料。
                </div>
              )}
              {hasMore ? (
                <div className="mt-2 border-t pt-2 text-center">
                  <button
                    type="button"
                    onClick={() => setExpandedDomains((current) => {
                      const next = new Set(current);
                      if (next.has(domain.kind)) next.delete(domain.kind);
                      else next.add(domain.kind);
                      return next;
                    })}
                    aria-expanded={expanded}
                    aria-controls={documentListId}
                    className="inline-flex min-h-9 items-center gap-1.5 px-2 text-xs font-medium text-primary hover:underline"
                  >
                    {expanded ? (
                      <>
                        收起，保留最近 {previewLimit} 条 <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" />
                      </>
                    ) : (
                      <>
                        展开全部 {domain.documents.length} 条 <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
                      </>
                    )}
                  </button>
                </div>
              ) : null}
            </article>
          );
        })}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
        <span className="inline-flex items-center gap-1"><Database className="h-3.5 w-3.5" /> 可追溯资料 {traceableCount} 条</span>
        <span>历史缓存 {counts.historical_context} 条</span>
        {bundle.excluded_count > 0 ? <span>另有 {bundle.excluded_count} 条因字段不完整未展示</span> : null}
        {bundle.generated_at ? <span>最近归档 {formatSourceTime(bundle.generated_at)}</span> : null}
      </div>
    </section>
  );
}

function SourceCount({ label, value }: { label: string; value: number }) {
  return (
    <div className="min-w-20 rounded-md border bg-background px-2.5 py-2 text-center text-xs">
      <div className="font-mono text-base font-semibold text-foreground">{value}</div>
      <div className="text-muted-foreground">{label}</div>
    </div>
  );
}

function AnnualReportCoverageSummary({ coverage }: { coverage: AnnualReportCoverage }) {
  return (
    <div className="mt-2 flex flex-wrap gap-1.5 text-[10px]" aria-label="年报档案覆盖">
      <span className="rounded border border-emerald-500/25 bg-emerald-500/5 px-1.5 py-0.5 text-emerald-700 dark:text-emerald-300">
        年报覆盖 {coverage.covered_years.length}/{coverage.requested_years.length} 年
      </span>
      {coverage.missing_years.length > 0 ? (
        <span className="rounded border border-amber-500/25 bg-amber-500/5 px-1.5 py-0.5 text-amber-700 dark:text-amber-300">
          缺 {coverage.missing_years.join("、")}
        </span>
      ) : null}
      {(coverage.needs_review_years || []).length > 0 ? (
        <span className="rounded border border-orange-500/25 bg-orange-500/5 px-1.5 py-0.5 text-orange-700 dark:text-orange-300">
          待复核 {(coverage.needs_review_years || []).join("、")}
        </span>
      ) : null}
    </div>
  );
}

function annualPhaseStyle(status: AnnualReportBackfillPhaseStatus): string {
  if (status === "running") return "border-blue-500 bg-blue-500 animate-pulse";
  if (status === "completed") return "border-emerald-500 bg-emerald-500";
  if (status === "reused") return "border-cyan-500 bg-cyan-500";
  if (status === "failed") return "border-red-500 bg-red-500";
  return "border-muted-foreground/30 bg-transparent";
}

function AnnualReportBackfillProgress({ job }: { job: AnnualReportBackfillJob }) {
  return (
    <section
      className="mt-4 rounded-md border border-cyan-500/25 bg-cyan-500/5 p-3"
      aria-label="历史年报补齐任务"
      aria-live="polite"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
        <div>
          <span className="font-medium">历史年报补齐任务</span>
          <span className="ml-2 font-mono text-[10px] text-muted-foreground">
            {job.job_id.slice(-8)}
          </span>
        </div>
        <span className="font-medium text-cyan-800 dark:text-cyan-200">
          {ANNUAL_JOB_STATUS_LABELS[job.status] || job.status} · {Math.round(job.progress_pct)}%
        </span>
      </div>
      <progress
        className="mt-2 h-1.5 w-full accent-cyan-600"
        aria-label="历史年报补齐总进度"
        max={100}
        value={Math.max(0, Math.min(100, job.progress_pct))}
      />
      <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        {job.year_progress.map((item) => (
          <div key={item.year} className="rounded border bg-background/70 px-2.5 py-2">
            <div className="flex items-center justify-between gap-2 text-xs">
              <span className="font-semibold">{item.year}</span>
              <span className="text-[10px] text-muted-foreground">
                {ANNUAL_YEAR_STATUS_LABELS[item.status] || item.status}
              </span>
            </div>
            <div className="mt-2 grid grid-cols-4 gap-1">
              {(Object.keys(ANNUAL_PHASE_LABELS) as Array<keyof typeof ANNUAL_PHASE_LABELS>).map((phase) => (
                <div key={phase} className="flex flex-col items-center gap-1 text-[9px] text-muted-foreground">
                  <span
                    className={cn("h-2 w-2 rounded-full border", annualPhaseStyle(item.phases[phase]))}
                    title={`${ANNUAL_PHASE_LABELS[phase]}：${item.phases[phase]}`}
                  />
                  {ANNUAL_PHASE_LABELS[phase]}
                </div>
              ))}
            </div>
            <div className="mt-2 line-clamp-2 min-h-7 text-[10px] leading-3.5 text-muted-foreground">
              {item.message}
            </div>
          </div>
        ))}
      </div>
      <div className="mt-2 text-[11px] text-muted-foreground">{job.message}</div>
      {job.error ? <div className="mt-1 text-[11px] text-red-600">{job.error}</div> : null}
    </section>
  );
}

function structuredStatusTitle(document: ReportSourceDocument): string {
  if (document.structured_status === "needs_review" || document.structured_status === "failed") {
    const failedChecks = (document.structured_failed_checks || [])
      .map((check) => STRUCTURED_CHECK_LABELS[check] || check)
      .join("、");
    const reason = failedChecks || document.structured_error || "自动校验未通过";
    return `${reason}。可使用“自动修复结构化”从已归档原文重新解析，不会重新下载或重复 OCR。`;
  }
  if (document.ocr_performed) {
    return "该快照执行过一次 OCR，后续按内容哈希直接复用";
  }
  const version = document.structured_extractor_version
    ? `（解析器 ${document.structured_extractor_version}）`
    : "";
  return `直接从原文文本层或官方结构化数据生成${version}，后续按内容哈希复用`;
}

export function SourceDocumentRow({ document }: { document: ReportSourceDocument }) {
  const content = (
    <>
      <div className="flex flex-wrap items-center gap-1.5">
        {document.evidence_level ? (
          <span className={cn(
            "rounded border px-1.5 py-0.5 text-[10px] font-semibold",
            document.evidence_level === "A" ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" :
              document.evidence_level === "B" ? "border-violet-500/30 bg-violet-500/10 text-violet-700 dark:text-violet-300" : "border-border bg-muted text-muted-foreground",
          )}>{document.evidence_level} 级{document.evidence_level === "B" ? "券商观点" : "来源"}</span>
        ) : null}
        <span className={cn("rounded border px-1.5 py-0.5 text-[10px] font-medium", STATUS_STYLES[document.verification_status])}>
          {STATUS_LABELS[document.verification_status]}
        </span>
        {document.structured_status === "validated" || document.structured_status === "needs_review" ? (
          <span
            className={cn(
              "rounded border px-1.5 py-0.5 text-[10px] font-medium",
              STRUCTURED_STATUS[document.structured_status].style,
            )}
            title={structuredStatusTitle(document)}
          >
            {STRUCTURED_STATUS[document.structured_status].label}
            {(document.structured_metrics_count || 0) > 0 ? ` · ${document.structured_metrics_count} 项` : ""}
          </span>
        ) : null}
        <span className="text-[11px] text-muted-foreground">{document.publisher}</span>
        {document.association_scope ? (
          <span className="rounded border px-1.5 py-0.5 text-[10px] text-muted-foreground">
            {ASSOCIATION_SCOPE_LABELS[document.association_scope] || document.association_scope}{document.related_symbol ? ` · ${document.related_symbol}` : ""}
          </span>
        ) : null}
        {(document.used_by_report_count || 0) > 0 ? (
          <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">
            被 {document.used_by_report_count} 份报告使用
          </span>
        ) : null}
      </div>
      <div className="mt-1 line-clamp-2 text-xs font-medium leading-5">{document.title}</div>
      <div className="mt-1 text-[11px] text-muted-foreground">
        {document.reporting_year ? `报告年度 ${document.reporting_year} · ` : ""}
        {document.published_at ? `发布 ${formatSourceTime(document.published_at)}` : `归档 ${formatSourceTime(document.retrieved_at)}`}
        {document.analyst ? ` · 分析师 ${Array.isArray(document.analyst) ? document.analyst.join("、") : document.analyst}` : ""}
        {document.published_at ? ` · 归档 ${formatSourceTime(document.retrieved_at)}` : ""}
      </div>
      {document.summary ? <p className="mt-1 line-clamp-2 text-[11px] leading-4 text-muted-foreground">{document.summary}</p> : null}
      {document.metrics.length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {document.metrics.map((metric) => (
            <span key={metric.label} className="rounded bg-muted px-1.5 py-1 text-[10px]">
              {metric.label} {formatMetric(metric.value, metric.unit)}
            </span>
          ))}
        </div>
      ) : null}
    </>
  );
  return document.source_url ? (
    <a
      href={document.source_url}
      target="_blank"
      rel="noreferrer"
      className="group block py-3 first:pt-0 last:pb-0"
      aria-label={`打开来源：${document.title}`}
    >
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">{content}</div>
        <ExternalLink className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground transition group-hover:text-primary" />
      </div>
    </a>
  ) : (
    <div className="py-3 first:pt-0 last:pb-0">{content}</div>
  );
}

function formatSourceTime(value?: string | null): string {
  if (!value) return "时间未记录";
  const parsed = new Date(value.includes("T") ? value : value.replace(" ", "T"));
  if (!Number.isFinite(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: value.includes(":") ? "2-digit" : undefined,
    minute: value.includes(":") ? "2-digit" : undefined,
    hour12: false,
  }).format(parsed);
}

function isWithinLastYear(value?: string | null): boolean {
  if (!value) return false;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) && timestamp >= Date.now() - 365 * 24 * 60 * 60 * 1000;
}

function formatMetric(value: number, unit: string): string {
  if (unit === "%") return `${value.toFixed(1)}%`;
  if (unit === "CNY") {
    return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(value);
  }
  return String(value);
}
