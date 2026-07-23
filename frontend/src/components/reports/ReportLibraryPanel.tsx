import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock,
  FileText,
  GitCompare,
  Loader2,
  RefreshCw,
  Search,
  Sparkles,
} from "lucide-react";

import {
  api,
  type AnnualReportBackfillJob,
  type AnnualReportCoverage,
  type ReportHorizon,
  type ReportKind,
  type ReportLibraryArtifact,
  type ReportLibraryComparison,
  type ReportLibraryCurrentCandidate,
  type ReportLibraryRecord,
  type ReportLibrarySubject,
  type ReportLibrarySubjectSummary,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  type ReportArtifactPreviewTarget,
} from "@/components/reports/ReportArtifactPreviewPane";
import { ETFConstituentWeights } from "@/components/reports/ETFConstituentWeights";
import { ETFProductOverview } from "@/components/reports/ETFProductOverview";
import { InstrumentProfileOverview } from "@/components/reports/InstrumentProfileOverview";
import { SubjectSourceBundle } from "@/components/reports/SubjectSourceBundle";
import { ReportSubjectGroup } from "@/components/reports/ReportSubjectGroup";
import { ResearchNotesPanel } from "@/components/reports/ResearchNotesPanel";
import { ReportSourcesDisclosure } from "@/components/reports/ReportSourcesDisclosure";


export type ReportLibraryMode = "dossiers" | "all" | "portfolio";

interface Props {
  mode: ReportLibraryMode;
  onPreview: (target: ReportArtifactPreviewTarget) => void;
}

interface ComparisonSelection {
  reportId: string;
  horizon: ReportHorizon;
  label: string;
}

const HORIZONS: ReportHorizon[] = ["intraday", "daily", "weekly", "structural"];
const DEFAULT_ANNUAL_REPORT_YEARS = 8;
const ACTIVE_ANNUAL_BACKFILL_STATUSES = new Set(["queued", "running"]);

function recentCompleteYears(yearCount = DEFAULT_ANNUAL_REPORT_YEARS): number[] {
  const lastCompleteYear = new Date().getFullYear() - 1;
  return Array.from(
    { length: Math.max(2, Math.min(yearCount, 12)) },
    (_, index) => lastCompleteYear - index,
  );
}
const HORIZON_LABELS: Record<ReportHorizon, string> = {
  intraday: "盘中",
  daily: "当日",
  weekly: "本周",
  structural: "结构性",
};

const KIND_LABELS: Record<ReportLibraryRecord["report_kind"], string> = {
  deep_research: "深度研究",
  daily_holding: "个股日更",
  daily_portfolio: "组合日报",
  weekly_review: "周度复盘",
  monitor_research: "监控研究",
  component_research: "成分研究",
};

const ACTION_LABELS: Record<string, string> = {
  observe: "观察",
  add: "加仓",
  reduce: "减仓",
  exit: "退出",
  not_applicable: "不适用",
};

const STANCE_LABELS: Record<string, string> = {
  bullish: "偏多",
  neutral: "中性",
  bearish: "偏空",
  mixed: "混合",
  unknown: "未归类",
};

function subjectIsEtf(subject: ReportLibrarySubject): boolean {
  const instrumentProfile = subject.instrument_profile
    || subject.profile?.etf?.instrument
    || subject.profile?.equity?.instrument
    || subject.profile?.index?.instrument;

  if (instrumentProfile?.instrument_type) {
    return instrumentProfile.instrument_type === "etf";
  }

  return Boolean(
    subject.etf_universe
    || subject.etf_product
    || subject.etf_valuation_percentile
    || subject.profile?.etf?.universe
    || subject.profile?.etf?.product
    || subject.profile?.etf?.valuation_percentile
    || subject.timeline.some((record) => (
      record.knowledge_link.instrument_type === "etf"
      || record.knowledge_link.profile === "etf_deep_research"
    )),
  );
}

const RELATION_LABELS: Record<string, string> = {
  continued: "观点延续",
  updated: "观点更新",
  diverged: "同周期分化",
  different_horizon: "不同周期",
  not_comparable: "不可直接比较",
};

const INTERNAL_LABELS: Record<string, string> = {
  daily: "当日",
  weekly: "本周",
  intraday: "盘中",
  structural: "长期结构",
  unknown: "未归类",
  forbidden: "禁止",
  manual_confirmation_required: "需人工确认",
  unsupported: "暂不支持",
  awaiting_data: "等待数据",
  mapped: "已映射",
  new: "新增",
  raised: "上调",
  lowered: "下调",
  approached: "已接近",
  confirmed: "已确认",
  rejected: "已否定",
  unresolved: "待确认",
  price_cross_above: "价格向上突破",
  price_cross_below: "价格向下跌破",
  price_enter_range: "价格进入区间",
};

function internalLabel(value?: string | null): string {
  if (!value) return "待补充";
  return INTERNAL_LABELS[value] || value;
}


export function ReportLibraryPanel({ mode, onPreview }: Props) {
  const activeSubjectKeyRef = useRef<string | null>(null);
  const [records, setRecords] = useState<ReportLibraryRecord[]>([]);
  const [subjects, setSubjects] = useState<ReportLibrarySubjectSummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [totalCount, setTotalCount] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [subject, setSubject] = useState<ReportLibrarySubject | null>(null);
  const initialQuery = typeof window === "undefined" ? "" : new URL(window.location.href).searchParams.get("report_query") || "";
  const initialReportKind = typeof window === "undefined"
    ? "all"
    : new URL(window.location.href).searchParams.get("report_kind") || "all";
  const [query, setQuery] = useState(initialQuery);
  const [submittedQuery, setSubmittedQuery] = useState(initialQuery);
  const [reportKind, setReportKind] = useState<"all" | ReportKind>(initialReportKind as "all" | ReportKind);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selection, setSelection] = useState<ComparisonSelection[]>([]);
  const [comparison, setComparison] = useState<ReportLibraryComparison | null>(null);
  const [comparing, setComparing] = useState(false);
  const [profileRefreshing, setProfileRefreshing] = useState(false);
  const [sourceRefreshing, setSourceRefreshing] = useState(false);
  const [sourceRepairing, setSourceRepairing] = useState(false);
  const [annualReportBackfillStarting, setAnnualReportBackfillStarting] = useState(false);
  const [annualReportBackfillJob, setAnnualReportBackfillJob] = useState<AnnualReportBackfillJob | null>(null);
  const [annualReportCoverage, setAnnualReportCoverage] = useState<AnnualReportCoverage | null>(null);
  const annualReportsBackfilling = annualReportBackfillStarting || Boolean(
    annualReportBackfillJob
    && ACTIVE_ANNUAL_BACKFILL_STATUSES.has(annualReportBackfillJob.status),
  );

  async function load(initial = false, cursor?: string) {
    if (initial) setLoading(true);
    else if (cursor) setLoadingMore(true);
    else setRefreshing(true);
    setError(null);
    try {
      if (mode === "portfolio") {
        const result = await api.listReportLibrary({
          query: submittedQuery || undefined,
          reportKind: reportKind === "all" ? undefined : reportKind,
          subjectType: "portfolio",
          limit: 100,
          cursor,
        });
        setRecords((current) => cursor ? [...current, ...result.reports] : result.reports);
        setSubjects([]);
        setNextCursor(result.next_cursor || null);
        setTotalCount(result.total_count ?? result.reports.length);
      } else {
        const result = await api.listReportLibrarySubjects({
          query: submittedQuery || undefined,
          reportKind: reportKind === "all" ? undefined : reportKind,
          limit: 30,
          cursor,
        });
        setSubjects((current) => cursor ? [...current, ...result.subjects] : result.subjects);
        setRecords([]);
        setNextCursor(result.next_cursor || null);
        setTotalCount(result.total_count);
      }
      if (cursor && typeof window !== "undefined") {
        const url = new URL(window.location.href);
        url.searchParams.set("report_cursor", cursor);
        window.history.replaceState(window.history.state, "", url);
      }
    } catch (reason) {
      if (!cursor) {
        setRecords([]);
        setSubjects([]);
      }
      setError(reason instanceof Error ? reason.message : "统一报告目录暂时不可用");
    } finally {
      setLoading(false);
      setRefreshing(false);
      setLoadingMore(false);
    }
  }

  useEffect(() => {
    activeSubjectKeyRef.current = null;
    setSubject(null);
    setAnnualReportCoverage(null);
    setAnnualReportBackfillJob(null);
    setSelection([]);
    setComparison(null);
    void load(true);
  }, [mode, submittedQuery, reportKind]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    url.searchParams.set("report_view", mode);
    url.searchParams.set("report_sort", "latest_generated_at_desc");
    if (submittedQuery) url.searchParams.set("report_query", submittedQuery);
    else url.searchParams.delete("report_query");
    if (reportKind !== "all") url.searchParams.set("report_kind", reportKind);
    else url.searchParams.delete("report_kind");
    url.searchParams.delete("report_cursor");
    window.history.replaceState(window.history.state, "", url);
  }, [mode, reportKind, submittedQuery]);

  useEffect(() => {
    if (subject) return undefined;
    const timer = window.setInterval(() => {
      void load();
    }, 60_000);
    return () => window.clearInterval(timer);
  }, [mode, reportKind, submittedQuery, subject]);

  async function openSubject(subjectKey: string) {
    activeSubjectKeyRef.current = subjectKey;
    setLoading(true);
    setError(null);
    setAnnualReportCoverage(null);
    setAnnualReportBackfillJob(null);
    try {
      const nextSubject = await api.getReportLibrarySubject(subjectKey, 200, {
        includeTimeline: false,
        includeSourceDocuments: false,
      });
      setSubject(nextSubject);
      if (!subjectIsEtf(nextSubject)) {
        const years = recentCompleteYears();
        const symbol = nextSubject.symbol || nextSubject.subject_key;
        const [coverageResult, latestJobResult] = await Promise.allSettled([
          api.getReportLibraryAnnualReportCoverage(
            symbol,
            years[years.length - 1],
            years[0],
          ),
          api.getLatestReportLibraryAnnualReportBackfillJob(symbol),
        ]);
        if (activeSubjectKeyRef.current === subjectKey) {
          setAnnualReportCoverage(
            coverageResult.status === "fulfilled" ? coverageResult.value : null,
          );
          setAnnualReportBackfillJob(
            latestJobResult.status === "fulfilled" ? latestJobResult.value.job : null,
          );
        }
      }
      setSelection([]);
      setComparison(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "标的档案加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    const jobId = annualReportBackfillJob?.job_id;
    const subjectKey = subject?.symbol || subject?.subject_key;
    const activeSubjectIdentity = activeSubjectKeyRef.current;
    if (
      !jobId
      || !subjectKey
      || !annualReportBackfillJob
      || !ACTIVE_ANNUAL_BACKFILL_STATUSES.has(annualReportBackfillJob.status)
    ) {
      return undefined;
    }
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const nextJob = await api.getReportLibraryAnnualReportBackfillJob(subjectKey, jobId);
        if (cancelled || activeSubjectKeyRef.current !== activeSubjectIdentity) return;
        setAnnualReportBackfillJob(nextJob);
        if (ACTIVE_ANNUAL_BACKFILL_STATUSES.has(nextJob.status)) {
          timer = window.setTimeout(poll, 1000);
          return;
        }
        if (nextJob.result?.coverage) {
          setAnnualReportCoverage(nextJob.result.coverage);
        }
        const nextSubject = await api.getReportLibrarySubject(subjectKey, 200, {
          includeTimeline: false,
          includeSourceDocuments: false,
        });
        if (!cancelled && activeSubjectKeyRef.current === activeSubjectIdentity) {
          setSubject(nextSubject);
        }
      } catch {
        if (!cancelled) timer = window.setTimeout(poll, 2500);
      }
    };

    timer = window.setTimeout(poll, 500);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [annualReportBackfillJob?.job_id, subject?.subject_key, subject?.symbol]);

  async function refreshInstrumentProfile() {
    if (!subject) return;
    setProfileRefreshing(true);
    setError(null);
    try {
      const subjectKey = subject.symbol || subject.subject_key;
      const isEtf = subjectIsEtf(subject);
      if (isEtf) {
        await api.refreshReportLibraryETFProfile(subjectKey);
        setSubject(await api.getReportLibrarySubject(subjectKey, 200, { includeTimeline: false, includeSourceDocuments: false }));
      } else {
        await api.refreshReportLibraryInstrumentProfile(subjectKey);
        await api.refreshReportLibraryHistoricalPercentile(subjectKey);
        setSubject(await api.getReportLibrarySubject(subjectKey, 200, { includeTimeline: false, includeSourceDocuments: false }));
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "标的资料更新失败");
    } finally {
      setProfileRefreshing(false);
    }
  }

  async function refreshSubjectSources() {
    if (!subject) return;
    const subjectKey = subject.symbol || subject.subject_key;
    setSourceRefreshing(true);
    setError(null);
    try {
      await api.refreshReportLibrarySources(subjectKey, true);
      setSubject(await api.getReportLibrarySubject(subjectKey, 200, { includeTimeline: false, includeSourceDocuments: false }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "资料刷新失败");
    } finally {
      setSourceRefreshing(false);
    }
  }

  async function repairSubjectFinancialSnapshots() {
    if (!subject) return;
    const subjectKey = subject.symbol || subject.subject_key;
    setSourceRepairing(true);
    setError(null);
    try {
      await api.rebuildReportLibraryFinancialSnapshots(subjectKey, true);
      setSubject(await api.getReportLibrarySubject(subjectKey, 200, { includeTimeline: false, includeSourceDocuments: false }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "结构化财报自动修复失败");
    } finally {
      setSourceRepairing(false);
    }
  }

  async function backfillSubjectAnnualReports(yearCount: number) {
    if (!subject) return;
    const subjectKey = subject.symbol || subject.subject_key;
    const years = recentCompleteYears(yearCount);
    setAnnualReportBackfillStarting(true);
    setError(null);
    try {
      const accepted = await api.startReportLibraryAnnualReportBackfill(
        subjectKey,
        years,
        false,
      );
      setAnnualReportBackfillJob(accepted.job);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "历史年报补齐失败");
    } finally {
      setAnnualReportBackfillStarting(false);
    }
  }

  function toggleSelection(report: ReportLibraryRecord, horizon: ReportHorizon) {
    const key = `${report.report_id}:${horizon}`;
    setComparison(null);
    setSelection((current) => {
      const exists = current.some((item) => `${item.reportId}:${item.horizon}` === key);
      if (exists) return current.filter((item) => `${item.reportId}:${item.horizon}` !== key);
      if (current.length >= 4) return current;
      return [
        ...current,
        {
          reportId: report.report_id,
          horizon,
          label: `${report.security_name || report.symbol || report.subject_key} · ${HORIZON_LABELS[horizon]} · ${formatDate(report.data_as_of)}`,
        },
      ];
    });
  }

  async function runComparison(includeAiSummary = false) {
    if (selection.length < 2) return;
    setComparing(true);
    setError(null);
    try {
      const result = await api.compareReportLibrary(
        selection.map((item) => ({ report_id: item.reportId, horizon: item.horizon })),
        includeAiSummary,
      );
      setComparison(result);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "报告比较失败");
    } finally {
      setComparing(false);
    }
  }

  async function reconcile() {
    setRefreshing(true);
    setError(null);
    try {
      await api.reconcileReportLibrary();
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "报告目录修复失败");
      setRefreshing(false);
    }
  }

  return (
    <div className="min-w-0 space-y-5">
      {!subject ? (
      <section className="flex flex-col gap-3 rounded-lg border bg-card/60 p-4 md:flex-row md:items-center">
        <form
          className="relative flex-1"
          onSubmit={(event) => {
            event.preventDefault();
            setSubmittedQuery(query.trim());
          }}
        >
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索证券代码或名称"
            className="w-full rounded-md border bg-background py-2 pl-9 pr-20 text-sm outline-none focus:border-primary"
          />
          <button type="submit" className="absolute right-1 top-1/2 -translate-y-1/2 rounded px-2.5 py-1 text-xs font-medium text-primary hover:bg-primary/10">
            搜索
          </button>
        </form>
        <label className="flex items-center gap-2 text-sm text-muted-foreground">
          类型
          <select
            aria-label="报告类型筛选"
            value={reportKind}
            onChange={(event) => setReportKind(event.target.value as "all" | ReportKind)}
            className="rounded-md border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-primary"
          >
            <option value="all">全部报告</option>
            <option value="deep_research">深度研究</option>
            <option value="daily_holding">个股日更</option>
            <option value="daily_portfolio">组合日报</option>
            <option value="weekly_review">周度复盘</option>
            <option value="monitor_research">监控研究</option>
            <option value="component_research">成分研究</option>
          </select>
        </label>
        <button
          type="button"
          onClick={() => void load()}
          disabled={refreshing}
          className="inline-flex items-center justify-center gap-2 rounded-md border px-3 py-2 text-sm font-medium hover:bg-muted disabled:opacity-50"
        >
          {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          刷新
        </button>
        <details className="relative">
          <summary className="cursor-pointer list-none rounded-md border px-3 py-2 text-sm text-muted-foreground hover:bg-muted">目录健康</summary>
          <div className="absolute right-0 z-20 mt-2 w-64 rounded-md border bg-popover p-3 shadow-lg">
            <p className="text-xs leading-5 text-muted-foreground">仅在目录数量异常或历史报告遗漏时执行重建。</p>
            <button
              type="button"
              onClick={() => void reconcile()}
              disabled={refreshing}
              className="mt-2 inline-flex w-full items-center justify-center gap-2 rounded-md border px-3 py-2 text-sm hover:bg-muted disabled:opacity-50"
            >
              <RefreshCw className="h-4 w-4" /> 修复遗漏索引
            </button>
          </div>
        </details>
      </section>
      ) : null}

      {error ? (
        <section role="alert" className="rounded-md border border-amber-500/30 bg-amber-500/5 p-4 text-sm">
          <div className="flex items-center gap-2 font-medium text-amber-700 dark:text-amber-300">
            <AlertTriangle className="h-4 w-4" /> 报告目录提示
          </div>
          <p className="mt-1 text-muted-foreground">{error}</p>
          <button type="button" onClick={() => void load()} className="mt-3 inline-flex items-center gap-1.5 rounded border px-2.5 py-1.5 text-xs hover:bg-muted">
            <RefreshCw className="h-3.5 w-3.5" /> 就地重试
          </button>
        </section>
      ) : null}

      {selection.length > 0 ? (
        <ComparisonBar
          selection={selection}
          comparing={comparing}
          onClear={() => { setSelection([]); setComparison(null); }}
          onCompare={(includeAi) => void runComparison(includeAi)}
        />
      ) : null}

      {comparison ? <ComparisonPanel comparison={comparison} /> : null}

      {loading ? <LoadingCards /> : null}

      {!loading && mode === "dossiers" && !subject ? (
        <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {subjects.map((item) => (
            <button
              key={item.subject_key}
              type="button"
              onClick={() => void openSubject(item.subject_key)}
              className="rounded-lg border bg-card p-4 text-left transition hover:border-primary/40 hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="font-semibold">{item.security_name || item.symbol || item.subject_key}</div>
                  <div className="mt-0.5 font-mono text-xs text-muted-foreground">{item.symbol || item.subject_key}</div>
                </div>
                <span className="rounded border px-2 py-0.5 text-xs text-muted-foreground">{item.report_count} 份</span>
              </div>
              <div className="mt-3 flex flex-wrap gap-1.5">
                {item.report_kinds.map((kind) => (
                  <span key={kind} className="rounded bg-primary/5 px-2 py-1 text-xs text-primary">
                    {KIND_LABELS[kind]}
                  </span>
                ))}
                {item.broker_research_count > 0 ? <span className="rounded bg-violet-500/10 px-2 py-1 text-xs text-violet-700 dark:text-violet-300">券商研报 {item.broker_research_count}</span> : null}
              </div>
              <p className="mt-3 line-clamp-2 text-sm leading-5">{item.current_viewpoint_summary || "暂无可用观点摘要"}</p>
              <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                <span>更新 {formatDateTime(item.latest_generated_at)}</span>
                <span>数据截至 {formatDateTime(item.latest_data_as_of)}</span>
                <span>研究笔记 {item.research_note_count}</span>
              </div>
            </button>
          ))}
        </section>
      ) : null}

      {!loading && mode === "dossiers" && subject ? (
        <Dossier
          subject={subject}
          summary={subjects.find((item) => item.subject_key === subject.subject_key)}
          selection={selection}
          profileRefreshing={profileRefreshing}
          sourceRefreshing={sourceRefreshing}
          sourceRepairing={sourceRepairing}
          annualReportsBackfilling={annualReportsBackfilling}
          annualReportBackfillJob={annualReportBackfillJob}
          annualReportCoverage={annualReportCoverage}
          onBack={() => {
            activeSubjectKeyRef.current = null;
            setSubject(null);
            setAnnualReportBackfillJob(null);
            setAnnualReportCoverage(null);
            setSelection([]);
            setComparison(null);
          }}
          onRefreshProfile={() => void refreshInstrumentProfile()}
          onRefreshSources={() => void refreshSubjectSources()}
          onRepairSources={() => void repairSubjectFinancialSnapshots()}
          onBackfillAnnualReports={(years) => void backfillSubjectAnnualReports(years)}
          onToggle={toggleSelection}
          onPreview={onPreview}
        />
      ) : null}

      {!loading && mode === "all" ? (
        <section className="grid gap-3" aria-label="全部新报告按标的分组">
          {subjects.map((item) => (
            <LazySubjectReportGroup
              key={item.subject_key}
              summary={item}
              selection={selection}
              onToggle={toggleSelection}
              onPreview={onPreview}
            />
          ))}
        </section>
      ) : null}

      {!loading && mode === "portfolio" ? (
        <PortfolioArchive records={records} selection={selection} onToggle={toggleSelection} onPreview={onPreview} />
      ) : null}

      {!loading && !subject && nextCursor ? (
        <button
          type="button"
          onClick={() => void load(false, nextCursor)}
          disabled={loadingMore}
          className="flex w-full items-center justify-center gap-2 rounded-md border py-2.5 text-sm font-medium hover:bg-muted disabled:opacity-50"
        >
          {loadingMore ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
          加载更多（已显示 {mode === "portfolio" ? records.length : subjects.length} / {totalCount}）
        </button>
      ) : null}

      {!loading && !subject && (mode === "portfolio" ? records.length === 0 : subjects.length === 0) && !error ? (
        <section className="rounded-lg border border-dashed p-10 text-center">
          <FileText className="mx-auto h-8 w-8 text-muted-foreground" />
          <h2 className="mt-3 font-medium">{submittedQuery || reportKind !== "all" ? "没有符合筛选条件的报告" : "暂无新体系报告"}</h2>
          <p className="mt-1 text-sm text-muted-foreground">{submittedQuery || reportKind !== "all" ? "请调整搜索词或报告类型。" : "新目录只收录功能启用后正式发布的报告；历史报告仍在旧版入口。"}</p>
        </section>
      ) : null}
    </div>
  );
}

function LazySubjectReportGroup({
  summary,
  selection,
  onToggle,
  onPreview,
}: {
  summary: ReportLibrarySubjectSummary;
  selection: ComparisonSelection[];
  onToggle: (report: ReportLibraryRecord, horizon: ReportHorizon) => void;
  onPreview: (target: ReportArtifactPreviewTarget) => void;
}) {
  const [reports, setReports] = useState<ReportLibraryRecord[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  async function load(cursor?: string) {
    setLoading(true);
    setError(null);
    try {
      const result = await api.getReportLibrarySubjectReports(summary.subject_key, { limit: 10, cursor });
      setReports((current) => cursor ? [...current, ...result.reports] : result.reports);
      setNextCursor(result.next_cursor || null);
      setLoaded(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "标的报告加载失败");
      if (!cursor) setReports([]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <ReportSubjectGroup
      subjectName={summary.security_name || summary.symbol || summary.subject_key}
      subjectKey={summary.symbol || summary.subject_key}
      reportCount={summary.report_count}
      latestLabel={`最新报告 · ${summary.latest_report ? KIND_LABELS[summary.latest_report.report_kind] : "类型未知"} · ${formatDateTime(summary.latest_generated_at)} · 新增 ${summary.new_report_count}`}
      badges={summary.report_kinds.map((kind) => KIND_LABELS[kind])}
      onExpandedChange={(expanded) => {
        if (expanded && !loaded && !loading) void load();
      }}
    >
      <div className="rounded-md border bg-card/70 p-3 text-xs text-muted-foreground">
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <span>数据截至 {formatDateTime(summary.latest_data_as_of)}</span>
          <span>完整报告 {summary.quality_summary.complete} / {summary.report_count}</span>
          <span>券商研报 {summary.broker_research_count}</span>
          <span>待确认笔记 {Math.max(0, summary.research_note_count - summary.confirmed_note_count)}</span>
        </div>
        <p className="mt-2 text-sm text-foreground">{summary.current_viewpoint_summary || "暂无可用观点摘要"}</p>
      </div>
      {error ? <RegionError message={error} onRetry={() => void load()} /> : null}
      {reports.map((record) => (
        <LibraryReportCard key={record.report_id} record={record} selection={selection} onToggle={onToggle} onPreview={onPreview} />
      ))}
      {loading ? <div className="flex items-center justify-center gap-2 py-5 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" />加载标的报告…</div> : null}
      {nextCursor && !loading ? <button type="button" onClick={() => void load(nextCursor)} className="w-full rounded-md border py-2 text-sm hover:bg-muted">加载更多</button> : null}
    </ReportSubjectGroup>
  );
}

function PortfolioArchive({
  records,
  selection,
  onToggle,
  onPreview,
}: {
  records: ReportLibraryRecord[];
  selection: ComparisonSelection[];
  onToggle: (report: ReportLibraryRecord, horizon: ReportHorizon) => void;
  onPreview: (target: ReportArtifactPreviewTarget) => void;
}) {
  const portfolios = useMemo(() => {
    const grouped = new Map<string, ReportLibraryRecord[]>();
    records.forEach((record) => grouped.set(record.subject_key, [...(grouped.get(record.subject_key) || []), record]));
    return Array.from(grouped.entries()).map(([key, items]) => {
      const byDate = new Map<string, ReportLibraryRecord>();
      items
        .slice()
        .sort((left, right) => right.generated_at.localeCompare(left.generated_at))
        .forEach((record) => {
          const date = record.report_period?.end_date || record.data_as_of.slice(0, 10);
          if (!byDate.has(date)) byDate.set(date, record);
        });
      const latest = items.slice().sort((left, right) => right.generated_at.localeCompare(left.generated_at))[0];
      return {
        key,
        name: latest?.security_name && latest.security_name !== key ? latest.security_name : "默认组合",
        latest,
        dates: Array.from(byDate.entries()).sort(([left], [right]) => right.localeCompare(left)),
      };
    }).sort((left, right) => (right.latest?.generated_at || "").localeCompare(left.latest?.generated_at || ""));
  }, [records]);

  return (
    <section className="grid gap-3" aria-label="组合归档">
      {portfolios.map((portfolio) => (
        <ReportSubjectGroup
          key={portfolio.key}
          subjectName={portfolio.name}
          reportCount={portfolio.dates.length}
          latestLabel={`最新晨会摘要 ${formatDateTime(portfolio.latest?.generated_at)}`}
          badges={["按报告日期归档"]}
        >
          {portfolio.dates.map(([date, record]) => (
            <section key={date} className="rounded-lg border bg-background p-3">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <div>
                  <h3 className="font-semibold">{date} 晨会摘要</h3>
                  <p className="mt-1 text-xs text-muted-foreground">持仓数量 {record.knowledge_link.etf_penetration?.selected_count ?? "待确认"} · 数据截至 {formatDateTime(record.data_as_of)}</p>
                </div>
                <QualityBadge quality={record.report_quality_status} coverage={record.coverage_status} />
              </div>
              <LibraryReportCard record={record} selection={selection} onToggle={onToggle} onPreview={onPreview} />
            </section>
          ))}
        </ReportSubjectGroup>
      ))}
    </section>
  );
}

function MetricCard({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <article className="rounded-lg border bg-card p-4">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 text-lg font-semibold">{value}</div>
      <div className="mt-1 text-xs text-muted-foreground">{detail}</div>
    </article>
  );
}

function RegionError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div role="alert" className="mb-3 flex flex-wrap items-center justify-between gap-3 rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-sm">
      <span className="flex items-center gap-2 text-amber-700 dark:text-amber-300"><AlertTriangle className="h-4 w-4" />{message}</span>
      <button type="button" onClick={onRetry} className="inline-flex items-center gap-1.5 rounded border px-2 py-1 text-xs hover:bg-muted"><RefreshCw className="h-3.5 w-3.5" />就地重试</button>
    </div>
  );
}


function Dossier({
  subject,
  summary,
  selection,
  profileRefreshing,
  sourceRefreshing,
  sourceRepairing,
  annualReportsBackfilling,
  annualReportBackfillJob,
  annualReportCoverage,
  onBack,
  onRefreshProfile,
  onRefreshSources,
  onRepairSources,
  onBackfillAnnualReports,
  onToggle,
  onPreview,
}: {
  subject: ReportLibrarySubject;
  summary?: ReportLibrarySubjectSummary;
  selection: ComparisonSelection[];
  profileRefreshing: boolean;
  sourceRefreshing: boolean;
  sourceRepairing: boolean;
  annualReportsBackfilling: boolean;
  annualReportBackfillJob: AnnualReportBackfillJob | null;
  annualReportCoverage: AnnualReportCoverage | null;
  onBack: () => void;
  onRefreshProfile: () => void;
  onRefreshSources: () => void;
  onRepairSources: () => void;
  onBackfillAnnualReports: (years: number) => void;
  onToggle: (report: ReportLibraryRecord, horizon: ReportHorizon) => void;
  onPreview: (target: ReportArtifactPreviewTarget) => void;
}) {
  const subjectKey = subject.symbol || subject.subject_key;
  const [reportsExpanded, setReportsExpanded] = useState(false);
  const [timeline, setTimeline] = useState<ReportLibraryRecord[]>([]);
  const [timelineTotal, setTimelineTotal] = useState(subject.report_count || summary?.report_count || 0);
  const [timelineCursor, setTimelineCursor] = useState<string | null>(null);
  const [timelineLoading, setTimelineLoading] = useState(false);
  const [timelineError, setTimelineError] = useState<string | null>(null);
  const [sourcesExpanded, setSourcesExpanded] = useState(true);
  const [sourceBundle, setSourceBundle] = useState(subject.source_bundle || null);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const requestedSourceKeyRef = useRef<string | null>(null);

  useEffect(() => {
    setReportsExpanded(false);
    setTimeline([]);
    setTimelineTotal(subject.report_count || summary?.report_count || 0);
    setTimelineCursor(null);
    setTimelineError(null);
    setSourcesExpanded(true);
    requestedSourceKeyRef.current = null;
    setSourceBundle(subject.source_bundle || null);
    setSourceError(null);
  }, [subject.subject_key, subject.report_count, subject.source_bundle, summary?.report_count]);

  async function loadTimeline(cursor?: string) {
    setTimelineLoading(true);
    setTimelineError(null);
    try {
      const result = await api.getReportLibrarySubjectReports(subjectKey, { limit: 10, cursor });
      setTimeline((current) => cursor ? [...current, ...result.reports] : result.reports);
      setTimelineTotal(result.total_count);
      setTimelineCursor(result.next_cursor || null);
    } catch (reason) {
      setTimelineError(reason instanceof Error ? reason.message : "历次报告加载失败");
      if (!cursor) setTimeline([]);
    } finally {
      setTimelineLoading(false);
    }
  }

  async function loadSources() {
    requestedSourceKeyRef.current = subjectKey;
    setSourceLoading(true);
    setSourceError(null);
    try {
      const detailed = await api.getReportLibrarySubject(subjectKey, 1, {
        includeTimeline: false,
        includeSourceDocuments: true,
      });
      setSourceBundle(detailed.source_bundle || null);
    } catch (reason) {
      setSourceError(reason instanceof Error ? reason.message : "详细资料加载失败");
    } finally {
      setSourceLoading(false);
    }
  }

  useEffect(() => {
    const hasDetailedDocuments = sourceBundle?.domains.some((domain) => domain.documents.length > 0);
    if (
      !sourcesExpanded
      || hasDetailedDocuments
      || sourceLoading
      || requestedSourceKeyRef.current === subjectKey
    ) return;
    void loadSources();
  }, [sourceBundle, sourceLoading, sourcesExpanded, subjectKey]);

  function toggleReports() {
    const next = !reportsExpanded;
    setReportsExpanded(next);
    if (next && timeline.length === 0 && !timelineLoading) void loadTimeline();
  }

  function toggleSources() {
    const next = !sourcesExpanded;
    setSourcesExpanded(next);
    if (next && !sourceLoading && !sourceBundle?.domains.some((domain) => domain.documents.length > 0)) void loadSources();
  }

  const etfUniverse = subject.profile?.etf?.universe || subject.etf_universe;
  const etfProduct = subject.profile?.etf?.product || subject.etf_product;
  const historicalPercentile = subject.historical_percentile
    || subject.profile?.etf?.historical_percentile
    || subject.profile?.etf?.valuation_percentile
    || subject.profile?.equity?.historical_percentile
    || subject.profile?.index?.historical_percentile
    || subject.etf_valuation_percentile;
  const instrumentProfile = subject.instrument_profile
    || subject.profile?.etf?.instrument
    || subject.profile?.equity?.instrument
    || subject.profile?.index?.instrument;
  const isEtf = subjectIsEtf(subject);
  const latestDate = summary?.latest_generated_at || subject.latest_generated_at;
  const traceableCount = sourceBundle?.traceable_count || 0;

  return (
    <div className="space-y-5">
      <button type="button" onClick={onBack} className="inline-flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-4 w-4" /> 返回标的列表
      </button>
      <section className="rounded-lg border bg-card p-5">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <div className="text-xs font-medium text-primary">标的档案</div>
            <h2 className="mt-1 text-2xl font-semibold">{subject.security_name || subject.symbol || subject.subject_key}</h2>
            <div className="mt-1 font-mono text-sm text-muted-foreground">{subject.symbol || subject.subject_key}</div>
            <div className="mt-2 text-xs text-muted-foreground">档案更新于 {formatDateTime(latestDate)}</div>
          </div>
          <span className="rounded border px-2.5 py-1 text-xs text-muted-foreground">{timelineTotal} 份正式报告</span>
        </div>
      </section>

      <section aria-labelledby="current-viewpoint-title">
        <div className="mb-3">
          <h3 id="current-viewpoint-title" className="font-semibold">当前观点、主要风险与待确认事项</h3>
          <p className="mt-1 text-sm text-muted-foreground">按观察周期展示最近有效观点；部分缺口不会覆盖最近完整观点。</p>
        </div>
        <button
          type="button"
          aria-label="在移动端快速打开报告列表"
          onClick={() => {
            if (!reportsExpanded) toggleReports();
            window.setTimeout(() => document.getElementById("report-history-section")?.scrollIntoView({ behavior: "smooth", block: "start" }), 0);
          }}
          className="mb-3 flex w-full items-center justify-between rounded-lg border border-primary/30 bg-primary/5 p-3 text-left text-sm font-semibold text-primary md:hidden"
        >
          <span>历次报告 · 共 {timelineTotal} 份</span><ChevronDown className="h-4 w-4" />
        </button>
        <div className="grid auto-cols-[minmax(250px,86vw)] grid-flow-col gap-3 overflow-x-auto pb-1 md:auto-cols-auto md:grid-flow-row md:grid-cols-2 md:overflow-visible xl:grid-cols-4">
          {HORIZONS.map((horizon) => (
            <CurrentViewpointCard key={horizon} horizon={horizon} value={subject.current[horizon]} />
          ))}
        </div>
      </section>

      <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4" aria-label="核心档案指标">
        <MetricCard label="正式报告" value={`${timelineTotal} 份`} detail={`最新 ${formatDate(latestDate || "")}`} />
        <MetricCard label="研究笔记" value={`${summary?.research_note_count || 0} 条`} detail={`${summary?.confirmed_note_count || 0} 条已确认`} />
        <MetricCard label="券商研报" value={`${summary?.broker_research_count || 0} 份`} detail="券商观点 · B 级专业来源" />
        <MetricCard label="来源覆盖" value={`${traceableCount} 条可追溯`} detail={sourceBundle?.excluded_count ? `${sourceBundle.excluded_count} 条待补充` : "暂无排除项"} />
      </section>

      <section className="rounded-lg border bg-card p-4" aria-label="来源覆盖摘要">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="font-semibold">来源覆盖摘要</h3>
            <p className="mt-1 text-sm text-muted-foreground">结构化输入、外部引用与券商观点分级记录，官方资料发生冲突时优先。</p>
          </div>
          <div className="flex flex-wrap gap-2 text-xs">
            <span className="rounded border px-2 py-1">官方 / 结构化 {sourceBundle?.verification_counts.official_primary || 0}</span>
            <span className="rounded border px-2 py-1">外部引用 {sourceBundle?.verification_counts.live_retrieved || 0}</span>
            {summary?.broker_research_count ? <span className="rounded border border-violet-500/30 bg-violet-500/5 px-2 py-1 text-violet-700 dark:text-violet-300">券商观点 {summary.broker_research_count}</span> : null}
          </div>
        </div>
      </section>

      <section id="report-history-section" className="scroll-mt-4 overflow-hidden rounded-lg border bg-card" aria-labelledby="report-history-title">
        <button
          type="button"
          onClick={toggleReports}
          aria-expanded={reportsExpanded}
          aria-controls="report-history-content"
          className="flex w-full items-center gap-3 p-4 text-left transition hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-inset"
        >
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary"><FileText className="h-4 w-4" /></span>
          <span className="min-w-0 flex-1">
            <span id="report-history-title" className="font-semibold">历次报告 · 共 {timelineTotal} 份 · 最新 {formatDate(latestDate || "")}</span>
            <span className="mt-1 block text-xs text-muted-foreground">按发布时间查看该标的的历次正式报告，可选择两份进行比较。</span>
          </span>
          <ChevronDown className={cn("h-4 w-4 shrink-0 text-muted-foreground transition-transform", reportsExpanded && "rotate-180")} />
        </button>
        {reportsExpanded ? (
          <div id="report-history-content" className="border-t p-3">
            {timelineError ? <RegionError message={timelineError} onRetry={() => void loadTimeline()} /> : null}
            <div className="grid gap-3">
              {timeline.map((record) => (
                <LibraryReportCard key={record.report_id} record={record} selection={selection} onToggle={onToggle} onPreview={onPreview} />
              ))}
            </div>
            {timelineLoading ? <div className="flex items-center justify-center gap-2 py-5 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" />加载历次报告…</div> : null}
            {timelineCursor && !timelineLoading ? <button type="button" onClick={() => void loadTimeline(timelineCursor)} className="mt-3 w-full rounded-md border py-2 text-sm hover:bg-muted">加载更多历次报告</button> : null}
          </div>
        ) : null}
      </section>

      <ResearchNotesPanel
        subjectKey={subjectKey}
        totalHint={summary?.research_note_count || 0}
        confirmedHint={summary?.confirmed_note_count || 0}
      />

      {isEtf && (subject.profile?.etf || etfUniverse || etfProduct) ? (
        <ETFProductOverview
          profile={etfProduct}
          refreshing={profileRefreshing}
          onRefresh={onRefreshProfile}
        />
      ) : null}
      <InstrumentProfileOverview
        snapshot={instrumentProfile}
        isEtf={isEtf}
        historicalPercentile={historicalPercentile}
        refreshing={profileRefreshing}
        onRefresh={onRefreshProfile}
      />
      {isEtf && etfUniverse ? <ETFConstituentWeights universe={etfUniverse} /> : null}

      <section className="overflow-hidden rounded-lg border bg-card" aria-labelledby="source-details-title">
        <button
          type="button"
          onClick={toggleSources}
          aria-expanded={sourcesExpanded}
          aria-controls="source-details-content"
          className="flex w-full items-center gap-3 p-4 text-left transition hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-inset"
        >
          <span className="min-w-0 flex-1">
            <span id="source-details-title" className="font-semibold">详细资料与证据档案</span>
            <span className="mt-1 block text-xs text-muted-foreground">官方资料、结构化数据、外部引用和券商观点分类查看。</span>
          </span>
          <ChevronDown className={cn("h-4 w-4 shrink-0 text-muted-foreground transition-transform", sourcesExpanded && "rotate-180")} />
        </button>
        {sourcesExpanded ? (
          <div id="source-details-content" className="border-t p-3">
            {sourceError ? <RegionError message={sourceError} onRetry={() => void loadSources()} /> : null}
            {sourceLoading ? <div className="flex items-center justify-center gap-2 py-5 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" />加载证据档案…</div> : null}
            {!sourceLoading && sourceBundle ? (
              <SubjectSourceBundle
                bundle={sourceBundle}
                refreshing={sourceRefreshing}
                repairing={sourceRepairing}
                annualReportsBackfilling={isEtf ? false : annualReportsBackfilling}
                annualReportBackfillJob={isEtf ? null : annualReportBackfillJob}
                annualReportCoverage={isEtf ? null : annualReportCoverage}
                showOverviewSources={false}
                supplementaryOnly={isEtf}
                onRefresh={onRefreshSources}
                onRepair={onRepairSources}
                onBackfillAnnualReports={isEtf ? undefined : onBackfillAnnualReports}
              />
            ) : null}
          </div>
        ) : null}
      </section>

      <details className="rounded-lg border bg-card p-4 text-sm">
        <summary className="cursor-pointer font-medium">技术详情</summary>
        <dl className="mt-3 grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
          <div><dt className="font-medium text-foreground">档案键</dt><dd className="mt-0.5 break-all font-mono">{subject.subject_key}</dd></div>
          <div><dt className="font-medium text-foreground">标的类型</dt><dd className="mt-0.5">{isEtf ? "ETF" : instrumentProfile?.instrument_type === "index" ? "指数" : "股票"}</dd></div>
        </dl>
      </details>
    </div>
  );
}


function CurrentViewpointCard({
  horizon,
  value,
}: {
  horizon: ReportHorizon;
  value: { latest: ReportLibraryCurrentCandidate | null; latest_complete: ReportLibraryCurrentCandidate | null };
}) {
  const latest = value.latest;
  const baseline = value.latest_complete;
  const hasSeparateBaseline = latest && baseline && latest.report_id !== baseline.report_id;
  return (
    <article className="rounded-lg border bg-card p-4">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-semibold">{HORIZON_LABELS[horizon]}</span>
        {latest ? <QualityBadge quality={latest.report_quality_status} coverage={latest.coverage_status} /> : null}
      </div>
      {latest ? (
        <>
          <div className="mt-4 flex flex-wrap gap-1.5">
            <span className="rounded bg-primary/10 px-2 py-1 text-xs text-primary">{STANCE_LABELS[latest.viewpoint.stance]}</span>
            <span className="rounded bg-muted px-2 py-1 text-xs">{ACTION_LABELS[latest.viewpoint.action]}</span>
          </div>
          <div className="mt-3 text-xs text-muted-foreground">数据截至 {formatDateTime(latest.data_as_of)}</div>
          {latest.summary?.text ? <p className="mt-3 line-clamp-3 text-xs leading-5">{latest.summary.text}</p> : null}
          {latest.risks?.length ? <p className="mt-2 text-xs leading-5 text-amber-700 dark:text-amber-300">主要风险：{latest.risks.map((item) => item.text).join("；")}</p> : null}
          {latest.pending_items?.length ? <p className="mt-2 text-xs leading-5 text-muted-foreground">待确认：{latest.pending_items.map((item) => item.text).join("；")}</p> : null}
          {hasSeparateBaseline ? (
            <div className="mt-3 rounded border border-amber-500/20 bg-amber-500/5 p-2 text-xs text-amber-800 dark:text-amber-200">
              最新观点存在缺口；最近完整观点截至 {formatDateTime(baseline.data_as_of)}。
            </div>
          ) : null}
        </>
      ) : (
        <div className="mt-6 text-sm text-muted-foreground">暂无有效观点</div>
      )}
    </article>
  );
}


function LibraryReportCard({
  record,
  selection,
  onToggle,
  onPreview,
}: {
  record: ReportLibraryRecord;
  selection: ComparisonSelection[];
  onToggle: (report: ReportLibraryRecord, horizon: ReportHorizon) => void;
  onPreview: (target: ReportArtifactPreviewTarget) => void;
}) {
  const [detailsExpanded, setDetailsExpanded] = useState(false);
  const previewArtifacts = previewableLibraryArtifacts(record.artifacts);
  const periodLabel = record.report_period?.label
    || [record.report_period?.start_date, record.report_period?.end_date].filter(Boolean).join(" 至 ")
    || "未标注";
  return (
    <article className="rounded-lg border bg-card p-4">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded bg-primary/5 px-2 py-1 text-xs font-medium text-primary">{KIND_LABELS[record.report_kind]}</span>
            <QualityBadge quality={record.report_quality_status} coverage={record.coverage_status} />
            {record.status === "diagnostic" ? <span className="rounded bg-destructive/10 px-2 py-1 text-xs text-destructive">诊断产物</span> : null}
          </div>
          <h3 className="mt-2 font-semibold">{record.security_name || record.symbol || record.subject_key}</h3>
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <span className="font-mono">{record.symbol || record.subject_key}</span>
            <span>报告周期 {periodLabel}</span>
            <span>生成于 {formatDateTime(record.generated_at)}</span>
            <span>数据截至 {formatDateTime(record.data_as_of)}</span>
            <span>状态 {record.status === "published" ? "已发布" : record.status === "archived" ? "已归档" : "诊断产物"}</span>
          </div>
          {record.viewpoints[0] ? (
            <p className="mt-2 text-sm text-muted-foreground">
              {HORIZON_LABELS[record.viewpoints[0].horizon]}：{STANCE_LABELS[record.viewpoints[0].stance]}，建议{ACTION_LABELS[record.viewpoints[0].action]}。
            </p>
          ) : null}
          <button
            type="button"
            onClick={() => setDetailsExpanded((current) => !current)}
            aria-expanded={detailsExpanded}
            className="mt-3 inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs font-medium hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
          >
            {detailsExpanded ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
            {detailsExpanded ? "收起报告详情" : "展开报告详情"}
          </button>
          {detailsExpanded ? <>
          {record.knowledge_link.etf_penetration ? (
            <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-muted-foreground">
              <span className="rounded border px-2 py-1">
                入选 {record.knowledge_link.etf_penetration.selected_count ?? 0} 只
              </span>
              <span className="rounded border px-2 py-1">
                权重覆盖 {formatCoverage(record.knowledge_link.etf_penetration.selected_weight_coverage)}
              </span>
              <span className="rounded border px-2 py-1">
                研究覆盖 {formatCoverage(record.knowledge_link.etf_penetration.research_coverage)}
              </span>
              <span className="rounded border px-2 py-1">
                完整支持 {formatCoverage(record.knowledge_link.etf_penetration.fully_supported_coverage)}
              </span>
            </div>
          ) : null}
          <div className="mt-3 flex flex-wrap gap-2">
            {record.viewpoints.map((viewpoint) => {
              const selected = selection.some((item) => item.reportId === record.report_id && item.horizon === viewpoint.horizon);
              return (
                <button
                  key={viewpoint.viewpoint_id}
                  type="button"
                  onClick={() => onToggle(record, viewpoint.horizon)}
                  aria-pressed={selected}
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs transition",
                    selected ? "border-primary bg-primary/10 text-primary" : "hover:bg-muted",
                  )}
                >
                  <GitCompare className="h-3.5 w-3.5" />
                  {HORIZON_LABELS[viewpoint.horizon]} · {STANCE_LABELS[viewpoint.stance]} · {ACTION_LABELS[viewpoint.action]}
                </button>
              );
            })}
          </div>
          {record.weekly_review ? <WeeklyReviewSummary review={record.weekly_review} /> : null}
          {record.monitoring_bundle ? (
            <MonitoringBundleSummary bundle={record.monitoring_bundle} />
          ) : null}
          <ReportSourcesDisclosure reportId={record.report_id} />
          {record.knowledge_link.internal_reference_code ? (
            <details className="mt-3 text-[11px] text-muted-foreground">
              <summary className="cursor-pointer">技术索引</summary>
              <div className="mt-1 break-all font-mono">{record.knowledge_link.internal_reference_code}</div>
            </details>
          ) : null}
          </> : null}
        </div>
        {detailsExpanded ? <div className="flex shrink-0 flex-wrap gap-2">
          {previewArtifacts.map((artifact) => (
            <button
              key={artifact.artifact_id}
              type="button"
              onClick={() => onPreview(libraryPreviewTarget(record, artifact.artifact_id))}
              aria-label={`预览 ${artifactLabel(artifact)}`}
              className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium hover:bg-muted"
            >
              <BookOpen className="h-3.5 w-3.5" /> {artifactLabel(artifact)}
            </button>
          ))}
        </div> : null}
      </div>
    </article>
  );
}

function formatCoverage(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `${(value * 100).toFixed(1)}%`
    : "待补充";
}


function WeeklyReviewSummary({
  review,
}: {
  review: NonNullable<ReportLibraryRecord["weekly_review"]>;
}) {
  const view = review.weekly_view || {};
  const due = Date.parse(review.review_due_at) <= Date.now();
  return (
    <section className="mt-4 rounded-md border border-blue-500/20 bg-blue-500/5 p-3" aria-label="周度复盘摘要">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-semibold">{review.week_start} 至 {review.week_end}</span>
        <span className="rounded bg-background px-2 py-0.5">{view.trend_stage} · {view.trend_direction} · {view.trend_strength}</span>
        <span className={cn("rounded px-2 py-0.5", Number(view.week_return_pct) >= 0 ? "text-emerald-700 dark:text-emerald-300" : "text-red-700 dark:text-red-300")}>
          本周 {Number(view.week_return_pct) >= 0 ? "+" : ""}{view.week_return_pct}%
        </span>
        <span className={cn("rounded px-2 py-0.5", due ? "bg-amber-500/10 text-amber-700 dark:text-amber-300" : "bg-background text-muted-foreground")}>
          {due ? "已到复核时间" : `复核于 ${formatDateTime(review.review_due_at)}`}
        </span>
      </div>
      {view.summary ? <p className="mt-2 text-xs leading-5 text-muted-foreground">{view.summary}</p> : null}
      {review.previous_week_validation.length > 0 ? (
        <div className="mt-3 grid gap-1.5 text-xs">
          <div className="font-medium">上周场景验证</div>
          {review.previous_week_validation.map((item) => (
            <div key={item.scenario_family_id} className="flex flex-wrap gap-x-2 rounded border bg-background/70 px-2 py-1.5">
              <span className="font-mono text-[11px]">{item.scenario_family_id}</span>
              <span>{internalLabel(item.outcome)}</span>
              <span className="text-muted-foreground">{item.summary}</span>
            </div>
          ))}
        </div>
      ) : (
        <p className="mt-2 text-xs text-muted-foreground">首份正式周报，暂无上一周正式场景可验证。</p>
      )}
      {review.scenario_changes.length > 0 ? (
        <div className="mt-2 text-xs text-muted-foreground">
          场景变化：{review.scenario_changes.map((item) => `${item.scenario_family_id} ${internalLabel(item.change_type)}`).join("；")}
        </div>
      ) : null}
      {review.data_gaps.length > 0 ? (
        <div className="mt-2 text-xs text-amber-700 dark:text-amber-300">数据缺口：{review.data_gaps.join("；")}</div>
      ) : null}
    </section>
  );
}


function MonitoringBundleSummary({
  bundle,
}: {
  bundle: NonNullable<ReportLibraryRecord["monitoring_bundle"]>;
}) {
  if (bundle.horizon === "structural") {
    const context = bundle.structural_context;
    return (
      <section className="mt-4 rounded-md border bg-muted/20 p-3" aria-label="深度研究结构监控候选">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="font-semibold">结构层监控依据</span>
          <span className="rounded border px-2 py-0.5 font-mono">长期结构</span>
          <span className={cn(
            "rounded px-2 py-0.5",
            bundle.monitoring_status === "available"
              ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
              : "bg-amber-500/10 text-amber-700 dark:text-amber-300",
          )}>
            {bundle.monitoring_status === "available"
              ? `${bundle.candidates.length} 个候选`
              : bundle.monitoring_status === "data_insufficient" ? "数据不足" : "暂无合格点位"}
          </span>
          <span className="text-muted-foreground">数据截至 {formatDateTime(bundle.data_as_of)}</span>
        </div>
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span>阶段：{context.trend_stage}</span>
          <span>方向：{context.trend_direction}</span>
          <span>强度：{context.trend_strength}</span>
          <span>逻辑：{context.thesis_state}</span>
          <span>复核：{formatDateTime(bundle.review_due_at)}</span>
        </div>
        {bundle.candidates.length > 0 ? (
          <div className="mt-3 grid gap-2">
            {bundle.candidates.map((candidate) => {
              const level = candidate.level?.kind === "point"
                ? candidate.level.price
                : candidate.level?.kind === "range"
                  ? `${candidate.level.low}–${candidate.level.high}`
                  : "事件触发";
              return (
                <div key={candidate.scenario_id} className="rounded border bg-background/70 p-2.5 text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{candidate.label}</span>
                    <span className="font-mono text-muted-foreground">{level}</span>
                    <span className={cn(
                      "rounded px-1.5 py-0.5",
                      candidate.actionability === "action_ready"
                        ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                        : "bg-muted text-muted-foreground",
                    )}>
                      {candidate.actionability === "action_ready" ? "可转人工监控" : "仅观察"}
                    </span>
                  </div>
                  {candidate.price_trigger_conditions[0] ? (
                    <div className="mt-1 text-muted-foreground">确认：{candidate.price_trigger_conditions[0]}</div>
                  ) : null}
                  {candidate.invalidation_conditions[0] ? (
                    <div className="mt-1 text-muted-foreground">失效：{candidate.invalidation_conditions[0]}</div>
                  ) : null}
                </div>
              );
            })}
          </div>
        ) : (
          <p className="mt-2 text-xs text-muted-foreground">
            本报告仍可正式发布；没有合格点位不会被转换成自动监控或买卖指令。
          </p>
        )}
        <div className="mt-2 text-[11px] text-muted-foreground">
          来源：深度研究结构 JSON · 需人工确认 · 交易执行禁止
        </div>
      </section>
    );
  }
  const warnings = bundle.price_volume_context.warnings || [];
  const actionReadyCount = bundle.candidates.filter((candidate) => candidate.automation_status === "action_ready").length;
  const watchOnlyCount = bundle.candidates.filter((candidate) => candidate.automation_status !== "action_ready").length;
  const sourceLabel = bundle.source === "structured_weekly_report" ? "周报结构化 JSON" : "日报结构化 JSON";
  return (
    <section className="mt-4 rounded-md border bg-muted/20 p-3" aria-label="结构化监控候选">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-semibold">结构化监控</span>
        <span className="rounded border px-2 py-0.5">{internalLabel(bundle.horizon)}</span>
        <span className={cn(
          "rounded px-2 py-0.5",
          bundle.monitoring_status === "available"
            ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
            : "bg-amber-500/10 text-amber-700 dark:text-amber-300",
        )}>
          {bundle.monitoring_status === "available"
            ? `${bundle.candidates.length} 个候选`
            : bundle.monitoring_status === "data_insufficient" ? "数据不足" : "暂不建议"}
        </span>
        <span className="text-muted-foreground">数据截至 {formatDateTime(bundle.data_as_of)}</span>
        <span className="text-muted-foreground">可复核 {actionReadyCount} · 仅观察 {watchOnlyCount}</span>
      </div>
      {warnings.length > 0 ? (
        <div className="mt-2 space-y-1 text-xs text-amber-700 dark:text-amber-300">
          {warnings.slice(0, 3).map((warning) => (
            <div key={warning} className="flex items-start gap-1.5">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{warning}</span>
            </div>
          ))}
        </div>
      ) : null}
      {bundle.candidates.length > 0 ? (
        <div className="mt-3 grid gap-2">
          {bundle.candidates.map((candidate) => {
            const trigger = candidate.trigger;
            const threshold = trigger.threshold ?? `${trigger.lower}–${trigger.upper}`;
            const unsupported = (candidate.source_conditions || []).filter(
              (condition) => condition.coverage_status !== "mapped",
            );
            return (
              <div key={candidate.candidate_id || candidate.scenario_id} className="rounded border bg-background/70 p-2.5 text-xs">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{candidate.label}</span>
                  <span className={cn(
                    "rounded px-1.5 py-0.5",
                    candidate.automation_status === "action_ready"
                      ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                      : "bg-muted text-muted-foreground",
                  )}>
                    {candidate.automation_status === "action_ready" ? "人工复核候选" : "仅观察"}
                  </span>
                  <span className="text-muted-foreground">{internalLabel(candidate.change_type || "new")}</span>
                </div>
                <div className="mt-1 text-muted-foreground">
                  {internalLabel(trigger.kind)} {threshold} · {trigger.interval} × {trigger.confirmation_count} 根闭合 K 线
                </div>
                {unsupported.map((condition) => (
                  <div key={condition.condition_id} className="mt-1 text-amber-700 dark:text-amber-300">
                    {internalLabel(condition.coverage_status)}：{condition.reason || condition.source_text}
                  </div>
                ))}
              </div>
            );
          })}
        </div>
      ) : null}
      <div className="mt-2 text-[11px] text-muted-foreground">
        来源：{sourceLabel}{bundle.source_period?.label ? ` · ${bundle.source_period.label}` : ""} · 复核于 {formatDateTime(bundle.review_due_at)} · {internalLabel(bundle.activation_policy)} · 交易执行 {internalLabel(bundle.trade_execution)}
      </div>
    </section>
  );
}


function previewableLibraryArtifacts(artifacts: ReportLibraryArtifact[]): ReportLibraryArtifact[] {
  return artifacts.filter((artifact) => {
    if (!artifact.available || !artifact.url) return false;
    const mediaType = artifact.media_type.toLowerCase();
    const filename = artifact.filename.toLowerCase();
    return mediaType.includes("markdown") || mediaType.includes("pdf") || mediaType.includes("json")
      || filename.endsWith(".md") || filename.endsWith(".markdown")
      || filename.endsWith(".pdf") || filename.endsWith(".json");
  }).sort((left, right) => artifactOrder(left) - artifactOrder(right));
}

function artifactOrder(artifact: ReportLibraryArtifact): number {
  const label = artifactLabel(artifact);
  if (label.includes("Markdown")) return 0;
  if (label === "PDF") return 1;
  if (label.includes("JSON")) return 2;
  if (label.includes("差异")) return 3;
  return 4;
}


function artifactLabel(artifact: ReportLibraryArtifact): string {
  const value = `${artifact.artifact_role} ${artifact.media_type} ${artifact.filename}`.toLowerCase();
  if (value.includes("pdf")) return "PDF";
  if (value.includes("diff")) return "版本差异";
  if (value.includes("diagnostic")) return "诊断 Markdown";
  if (value.includes("monitoring") || value.includes("json")) return "结构监控 JSON";
  return "Markdown";
}


function libraryPreviewTarget(
  record: ReportLibraryRecord,
  initialArtifactId?: string,
): ReportArtifactPreviewTarget {
  const artifacts = previewableLibraryArtifacts(record.artifacts).map((artifact) => ({
    artifactId: artifact.artifact_id,
    label: artifactLabel(artifact),
    filename: artifact.filename,
    mediaType: artifact.media_type,
    previewUrl: api.reportLibraryArtifactUrl(artifact, "preview"),
    downloadUrl: api.reportLibraryArtifactUrl(artifact, "download"),
  }));
  return {
    source: "report_library",
    reportId: record.report_id,
    title: record.security_name || record.symbol || record.subject_key,
    subtitle: `${KIND_LABELS[record.report_kind]} · 数据截至 ${formatDateTime(record.data_as_of)}`,
    initialArtifactId,
    artifacts,
  };
}


function ComparisonBar({
  selection,
  comparing,
  onClear,
  onCompare,
}: {
  selection: ComparisonSelection[];
  comparing: boolean;
  onClear: () => void;
  onCompare: (includeAi: boolean) => void;
}) {
  return (
    <section className="sticky top-3 z-20 rounded-lg border border-primary/30 bg-background/95 p-4 shadow-lg backdrop-blur">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <div className="text-sm font-semibold">已选择 {selection.length}/4 个周期观点</div>
          <div className="mt-1 line-clamp-1 text-xs text-muted-foreground">{selection.map((item) => item.label).join("；")}</div>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={onClear} className="rounded-md border px-3 py-2 text-xs hover:bg-muted">清空</button>
          <button
            type="button"
            disabled={selection.length < 2 || comparing}
            onClick={() => onCompare(false)}
            className="inline-flex items-center gap-1.5 rounded-md border px-3 py-2 text-xs font-medium hover:bg-muted disabled:opacity-50"
          >
            <GitCompare className="h-3.5 w-3.5" /> 结构化对照
          </button>
          <button
            type="button"
            disabled={selection.length < 2 || comparing}
            onClick={() => onCompare(true)}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground disabled:opacity-50"
          >
            {comparing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
            对照并解释
          </button>
        </div>
      </div>
    </section>
  );
}


function ComparisonPanel({ comparison }: { comparison: ReportLibraryComparison }) {
  return (
    <section className="space-y-4 rounded-lg border bg-card p-5">
      <div>
        <h2 className="flex items-center gap-2 font-semibold"><GitCompare className="h-4 w-4 text-primary" /> 报告差异</h2>
        <p className="mt-1 text-sm text-muted-foreground">结构化关系是确定性结果；AI说明不会覆盖它。</p>
      </div>
      <div className="grid gap-3">
        {comparison.deltas.map((delta) => (
          <article key={`${delta.base_report_id}:${delta.current_report_id}`} className="rounded-md border p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className={cn(
                "rounded px-2 py-1 text-xs font-medium",
                delta.relation === "diverged" ? "bg-destructive/10 text-destructive" :
                  delta.relation === "different_horizon" ? "bg-blue-500/10 text-blue-700 dark:text-blue-300" :
                    "bg-primary/10 text-primary",
              )}>
                {RELATION_LABELS[delta.relation]}
              </span>
              <span className="font-mono text-[11px] text-muted-foreground">{delta.base_report_id} → {delta.current_report_id}</span>
            </div>
            {Object.keys(delta.changes).length > 0 ? (
              <div className="mt-3 overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead><tr className="border-b text-muted-foreground"><th className="py-2">字段</th><th>之前</th><th>现在</th></tr></thead>
                  <tbody>
                    {Object.entries(delta.changes).map(([field, value]) => (
                      <tr key={field} className="border-b last:border-0">
                        <td className="py-2 pr-3 font-medium">{changeLabel(field)}</td>
                        <td className="max-w-xs py-2 pr-3 text-muted-foreground">{displayValue(value.before)}</td>
                        <td className="max-w-xs py-2">{displayValue(value.after)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <p className="mt-3 text-sm text-muted-foreground">没有检测到结构化观点字段变化。</p>}
          </article>
        ))}
      </div>
      {comparison.ai_summary.status === "completed" ? (
        <section className="rounded-md border border-violet-500/20 bg-violet-500/5 p-4">
          <h3 className="flex items-center gap-2 text-sm font-semibold"><Sparkles className="h-4 w-4 text-violet-600" /> AI变化说明</h3>
          <p className="mt-2 text-sm">{comparison.ai_summary.summary}</p>
          <ul className="mt-3 space-y-2 text-sm">
            {comparison.ai_summary.items?.map((item, index) => (
              <li key={index} className="rounded border bg-background/70 p-3">
                <div>{item.text}</div>
                <div className="mt-1 text-[11px] text-muted-foreground">
                  {item.citations.map((citation) => `${citation.report_id} / ${citation.claim_id}`).join("；")}
                </div>
              </li>
            ))}
          </ul>
        </section>
      ) : comparison.ai_summary.status !== "not_requested" ? (
        <div className="rounded border border-dashed p-3 text-sm text-muted-foreground">AI说明状态：{comparison.ai_summary.status}；结构化结果仍然有效。</div>
      ) : null}
    </section>
  );
}


function QualityBadge({ quality, coverage }: { quality: string; coverage: string }) {
  const complete = quality === "passed" && coverage === "complete";
  return (
    <span className={cn(
      "inline-flex items-center gap-1 rounded px-2 py-1 text-xs",
      complete ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" :
        quality === "failed_validation" ? "bg-destructive/10 text-destructive" :
          "bg-amber-500/10 text-amber-700 dark:text-amber-300",
    )}>
      {complete ? <CheckCircle2 className="h-3 w-3" /> : <Clock className="h-3 w-3" />}
      {complete ? "完整" : quality === "failed_validation" ? "校验失败" : "部分缺口"}
    </span>
  );
}


function LoadingCards() {
  return <div className="grid gap-3 md:grid-cols-2">{[1, 2, 3, 4].map((item) => <div key={item} className="h-32 animate-pulse rounded-lg border bg-muted/40" />)}</div>;
}


function changeLabel(value: string): string {
  return ({
    stance: "观点倾向",
    action: "行动意见",
    confidence: "置信度",
    summary: "核心摘要",
    reason: "判断依据",
    risk: "主要风险",
    condition: "观察条件",
    invalidation: "失效条件",
  } as Record<string, string>)[value] || value;
}


function displayValue(value: unknown): string {
  if (Array.isArray(value)) return value.length ? value.join("；") : "—";
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}


function formatDate(value?: string | null): string {
  if (!value) return "待补充";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit" }).format(date);
}


function formatDateTime(value?: string | null): string {
  if (!value) return "待补充";
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
