import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Activity,
  ArrowLeft,
  Bot,
  ChevronDown,
  CircleDollarSign,
  Database,
  Gauge,
  Globe2,
  HardDrive,
  RefreshCw,
  TriangleAlert,
  X,
} from "lucide-react";

import {
  api,
  type MonitorJobUsageSummary,
  type MonitorUsagePeriod,
  type MonitorUsageSummary,
  type SessionUsageAggregate,
  type UsageCostAggregate,
  type UsageCostCurrencyAggregate,
  type UsageEventItem,
  type UsageEventKind,
} from "@/lib/api";

interface MonitorUsagePanelProps {
  running: boolean;
  requestedJobId?: string | null;
  onRequestedJobHandled?: () => void;
}

type Tab = "overview" | "details";

const PERIOD_LABELS: Record<MonitorUsagePeriod, string> = {
  today: "今日",
  "7d": "近 7 日",
  "30d": "近 30 日",
};

const KIND_LABELS: Record<UsageEventKind, string> = {
  llm_call: "模型",
  tool_call: "工具",
  resource_call: "资源",
};

const CATEGORY_LABELS: Record<string, string> = {
  llm: "模型",
  web: "网页",
  market: "行情",
  financial: "财务数据",
  local_data: "本地数据",
  compute: "计算",
  file: "文件",
  mcp: "MCP",
  other: "其他",
};

const EMPTY_COST: UsageCostAggregate = {
  coverage: "not_applicable",
  priced_calls: 0,
  unpriced_calls: 0,
  total_calls: 0,
  currencies: [],
  catalog_version: "未加载",
  time_basis: "started_at",
};

function formatToken(value: number | null | undefined): string {
  if (value == null) return "未报告";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1).replace(/\.0$/, "")}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 100_000 ? 0 : 1).replace(/\.0$/, "")}K`;
  return value.toLocaleString("zh-CN");
}

function formatMoney(currency: string, amount: number): string {
  const prefix = currency === "CNY" ? "¥" : currency === "USD" ? "$" : `${currency} `;
  const absolute = Math.abs(amount);
  const digits = absolute >= 1 ? 2 : absolute >= 0.01 ? 4 : 6;
  return `${prefix}${amount.toLocaleString("zh-CN", { maximumFractionDigits: digits })}`;
}

function formatCost(item: UsageCostCurrencyAggregate): string {
  if (Math.abs(item.maximum_estimated_cost - item.minimum_estimated_cost) > 0.0000000001) {
    return `${formatMoney(item.currency, item.minimum_estimated_cost)}–${formatMoney(item.currency, item.maximum_estimated_cost)}`;
  }
  return formatMoney(item.currency, item.estimated_cost);
}

function formatCostSummary(cost?: UsageCostAggregate): string | null {
  if (!cost?.currencies.length) return null;
  return cost.currencies.map(formatCost).join(" + ");
}

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-xl border border-border/80 bg-background/40 p-3">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="mt-1 text-lg font-semibold tracking-tight">{value}</div>
      {hint ? <div className="mt-1 text-[10px] text-muted-foreground">{hint}</div> : null}
    </div>
  );
}

function UsageEventRow({ event }: { event: UsageEventItem }) {
  const label = event.tool_name || event.model || event.provider || KIND_LABELS[event.kind];
  const cost = event.cost?.currency && event.cost.maximum_estimated_cost != null
    ? formatCost({
        currency: event.cost.currency,
        estimated_cost: event.cost.estimated_cost ?? event.cost.maximum_estimated_cost,
        minimum_estimated_cost: event.cost.minimum_estimated_cost ?? event.cost.maximum_estimated_cost,
        maximum_estimated_cost: event.cost.maximum_estimated_cost,
        calls: 1,
        peak_calls: event.cost.tier === "peak" ? 1 : 0,
      })
    : null;
  return (
    <div className="grid gap-2 border-b border-border/60 py-3 last:border-b-0 sm:grid-cols-[minmax(0,1fr)_auto]">
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2">
          <span className={`h-1.5 w-1.5 rounded-full ${event.status === "error" ? "bg-red-400" : event.status === "running" ? "bg-primary" : "bg-emerald-400"}`} />
          <span className="truncate text-sm font-medium">{label}</span>
          <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">{KIND_LABELS[event.kind]}</span>
          {event.scope_type === "session" ? <span className="rounded bg-violet-500/10 px-1.5 py-0.5 text-[10px] text-violet-400">关联深研</span> : null}
          {event.cost?.tier === "peak" ? <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-400">高峰 2×</span> : null}
        </div>
        <div className="mt-1 truncate pl-3.5 text-xs text-muted-foreground">
          {event.query_summary || (event.total_tokens != null ? `${formatToken(event.total_tokens)} Token` : "未保存原始正文")}
        </div>
      </div>
      <div className="flex items-center gap-3 pl-3.5 text-[11px] text-muted-foreground sm:pl-0">
        {cost ? <span>{cost}</span> : null}
        <span>{event.elapsed_ms == null ? "—" : event.elapsed_ms < 1000 ? `${event.elapsed_ms} ms` : `${(event.elapsed_ms / 1000).toFixed(1)} s`}</span>
        <span>{new Date(event.started_at).toLocaleString("zh-CN", { hour12: false })}</span>
      </div>
    </div>
  );
}

export function MonitorUsagePanel({
  running,
  requestedJobId,
  onRequestedJobHandled,
}: MonitorUsagePanelProps) {
  const [period, setPeriod] = useState<MonitorUsagePeriod>("today");
  const [summary, setSummary] = useState<MonitorUsageSummary | null>(null);
  const [jobSummary, setJobSummary] = useState<MonitorJobUsageSummary | null>(null);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("overview");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [events, setEvents] = useState<UsageEventItem[]>([]);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [eventsError, setEventsError] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [kindFilter, setKindFilter] = useState<UsageEventKind | "">("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  const loadSummary = useCallback(async (targetPeriod: MonitorUsagePeriod) => {
    setLoading(true);
    try {
      const result = await api.getPortfolioMonitoringUsage(targetPeriod);
      setSummary((current) => current && current.period === targetPeriod && current.revision > result.revision ? current : result);
      setError(false);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  const openJob = useCallback(async (jobId: string) => {
    setSelectedJobId(jobId);
    setJobSummary(null);
    setOpen(true);
    setLoading(true);
    try {
      setJobSummary(await api.getPortfolioMonitorPlannerJobUsage(jobId));
      setError(false);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadEvents = useCallback(async (cursor?: string, append = false) => {
    setEventsLoading(true);
    setEventsError(false);
    try {
      const filters = {
        kind: kindFilter || undefined,
        category: categoryFilter || undefined,
        cursor,
        limit: 50,
      };
      const page = selectedJobId
        ? await api.getPortfolioMonitorPlannerJobUsageEvents(selectedJobId, filters)
        : await api.getPortfolioMonitoringUsageEvents(period, filters);
      setEvents((current) => append ? [...current, ...page.items] : page.items);
      setNextCursor(page.next_cursor);
    } catch {
      setEventsError(true);
    } finally {
      setEventsLoading(false);
    }
  }, [categoryFilter, kindFilter, period, selectedJobId]);

  useEffect(() => { void loadSummary("today"); }, [loadSummary]);

  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => void loadSummary(period), open ? 5_000 : 15_000);
    return () => window.clearInterval(timer);
  }, [loadSummary, open, period, running]);

  useEffect(() => {
    if (!requestedJobId) return;
    void openJob(requestedJobId);
    onRequestedJobHandled?.();
  }, [onRequestedJobHandled, openJob, requestedJobId]);

  useEffect(() => {
    if (!open) return;
    setEvents([]);
    setNextCursor(null);
    void loadEvents();
  }, [loadEvents, open, summary?.revision, jobSummary?.revision]);

  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
      ));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", onKeyDown);
      triggerRef.current?.focus();
    };
  }, [open]);

  const aggregate = useMemo<SessionUsageAggregate | null>(
    () => selectedJobId ? jobSummary?.session ?? null : summary?.session ?? null,
    [jobSummary, selectedJobId, summary],
  );
  const aggregateCost = aggregate?.cost ?? EMPTY_COST;
  const badgeCost = formatCostSummary(summary?.session.cost);
  const badgeToken = summary ? formatToken(summary.session.tokens.total_tokens) : error ? "不可用" : "加载中";
  const peakCalls = aggregateCost.currencies.reduce((total, item) => total + item.peak_calls, 0);

  const changePeriod = (next: MonitorUsagePeriod) => {
    setPeriod(next);
    setSelectedJobId(null);
    setJobSummary(null);
    void loadSummary(next);
  };

  const modal = open ? createPortal(
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/70 p-0 backdrop-blur-md sm:p-5"
      role="presentation"
      onMouseDown={(event) => { if (event.target === event.currentTarget) setOpen(false); }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="monitor-usage-title"
        className="flex h-[100dvh] w-full flex-col overflow-hidden bg-card shadow-2xl sm:h-auto sm:max-h-[88vh] sm:max-w-5xl sm:rounded-2xl sm:border"
      >
        <header className="flex items-start justify-between gap-4 border-b px-5 py-4 sm:px-6">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              {selectedJobId ? (
                <button type="button" aria-label="返回监控总账" onClick={() => { setSelectedJobId(null); setJobSummary(null); }} className="rounded p-1 hover:bg-muted">
                  <ArrowLeft className="h-4 w-4" />
                </button>
              ) : <Gauge className="h-4 w-4 text-primary" />}
              <h2 id="monitor-usage-title" className="font-semibold">
                {selectedJobId ? "自动分析任务用量" : "AI 监控 Token 总账"}
              </h2>
              {running ? <span className="rounded-full bg-primary/15 px-2 py-0.5 text-[10px] text-primary">实时记录中</span> : null}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {selectedJobId
                ? `${jobSummary?.job.requested_symbols.join("、") || selectedJobId} · Planner、行情、工具与关联深研完整归集`
                : "自动 Planner、Autopilot、外部数据请求和关联 Deep Report 使用同一账本"}
            </p>
          </div>
          <button ref={closeRef} type="button" aria-label="关闭 AI 监控用量" onClick={() => setOpen(false)} className="rounded-lg p-2 text-muted-foreground hover:bg-muted hover:text-foreground">
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="flex flex-wrap items-center justify-between gap-3 border-b px-5 py-3 sm:px-6">
          <div className="flex rounded-lg bg-muted/70 p-1" aria-label="统计范围">
            {(Object.keys(PERIOD_LABELS) as MonitorUsagePeriod[]).map((value) => (
              <button key={value} type="button" disabled={Boolean(selectedJobId)} onClick={() => changePeriod(value)} className={`rounded-md px-3 py-1.5 text-xs disabled:opacity-40 ${period === value ? "bg-background shadow-sm" : "text-muted-foreground hover:text-foreground"}`}>
                {PERIOD_LABELS[value]}
              </button>
            ))}
          </div>
          <div className="flex items-center" role="tablist" aria-label="用量视图">
            {(["overview", "details"] as Tab[]).map((value) => (
              <button key={value} type="button" role="tab" aria-selected={tab === value} onClick={() => setTab(value)} className={`border-b-2 px-3 py-2 text-xs ${tab === value ? "border-primary" : "border-transparent text-muted-foreground"}`}>
                {value === "overview" ? "概览" : "调用明细"}
              </button>
            ))}
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5 sm:px-6">
          {loading && !aggregate ? (
            <div className="flex min-h-64 items-center justify-center gap-2 text-sm text-muted-foreground"><RefreshCw className="h-4 w-4 animate-spin" />正在读取总账…</div>
          ) : error && !aggregate ? (
            <div className="flex min-h-64 flex-col items-center justify-center gap-3 text-sm text-red-400"><TriangleAlert className="h-6 w-6" />用量总账加载失败<button type="button" className="rounded border px-3 py-1.5" onClick={() => selectedJobId ? void openJob(selectedJobId) : void loadSummary(period)}>重试</button></div>
          ) : aggregate && tab === "overview" ? (
            <div className="space-y-5">
              <section>
                <div className="mb-2 flex items-center justify-between"><h3 className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground"><CircleDollarSign className="h-3.5 w-3.5 text-primary" />费用估算</h3><span className="text-[11px] text-muted-foreground">{aggregateCost.priced_calls}/{aggregateCost.total_calls} 次已计价</span></div>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  {aggregateCost.currencies.map((item) => <Metric key={item.currency} label={`${item.currency} 预估`} value={formatCost(item)} hint={item.peak_calls ? `${item.peak_calls} 次高峰调用` : "按原币种计算"} />)}
                  {!aggregateCost.currencies.length ? <Metric label="费用" value="未配置" hint="Token 未报告或价格未配置" /> : null}
                  <Metric label="高峰调用" value={peakCalls.toLocaleString("zh-CN")} hint="DeepSeek 北京时间高峰" />
                  <Metric label="任务数" value={selectedJobId ? "1" : String(summary?.scope_count ?? 0)} hint={selectedJobId ? `${jobSummary?.linked_scopes.length || 0} 个关联账本` : `${summary?.linked_scope_count || 0} 个关联深研账本`} />
                </div>
              </section>

              <section>
                <h3 className="mb-2 text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">Token 与调用</h3>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
                  <Metric label="总 Token" value={formatToken(aggregate.tokens.total_tokens)} />
                  <Metric label="输入" value={formatToken(aggregate.tokens.input_tokens)} />
                  <Metric label="输出" value={formatToken(aggregate.tokens.output_tokens)} />
                  <Metric label="缓存读取" value={formatToken(aggregate.tokens.cache_read_input_tokens)} />
                  <Metric label="模型调用" value={String(aggregate.calls.llm_calls)} />
                  <Metric label="Agent 工具" value={String(aggregate.calls.agent_tools)} />
                  <Metric label="外部请求" value={String(aggregate.calls.external_requests)} />
                  <Metric label="失败" value={String(aggregate.calls.failures)} />
                </div>
              </section>

              <section className="grid gap-3 md:grid-cols-3">
                {[
                  { title: "模型", icon: Bot, rows: aggregate.models },
                  { title: "工具类别", icon: Activity, rows: aggregate.categories },
                  { title: "数据来源", icon: Globe2, rows: aggregate.providers },
                ].map(({ title, icon: Icon, rows }) => (
                  <div key={title} className="rounded-xl border bg-background/30 p-4">
                    <div className="mb-3 flex items-center gap-2 text-xs font-medium"><Icon className="h-3.5 w-3.5 text-primary" />{title}</div>
                    <div className="space-y-2">
                      {!rows.length ? <span className="text-xs text-muted-foreground">暂无记录</span> : rows.slice(0, 6).map((row) => <div key={row.key} className="flex justify-between gap-3 text-xs"><span className="truncate text-muted-foreground">{CATEGORY_LABELS[row.key] || row.key}</span><strong>{row.count}</strong></div>)}
                    </div>
                  </div>
                ))}
              </section>

              {!selectedJobId ? (
                <section>
                  <h3 className="mb-2 text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">最近自动分析任务</h3>
                  <div className="rounded-xl border bg-background/30 px-3">
                    {summary?.recent_jobs.map((job) => (
                      <div key={job.job_id} className="grid gap-2 border-b py-3 last:border-b-0 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                        <div className="min-w-0"><div className="truncate text-sm font-medium">{job.requested_symbols.join("、") || job.job_id}</div><div className="mt-1 text-[11px] text-muted-foreground">{job.activation_mode === "autonomous" ? "自主分析" : "手动规划"} · {job.status} · {new Date(job.created_at || job.recording_started_at).toLocaleString("zh-CN")}</div></div>
                        <div className="flex items-center gap-3 text-xs"><span>{formatToken(job.usage.tokens.total_tokens)} Token</span><span>{formatCostSummary(job.usage.cost) || "未计价"}</span><button type="button" onClick={() => void openJob(job.job_id)} className="rounded border px-2.5 py-1.5 hover:bg-muted">查看全链路</button></div>
                      </div>
                    ))}
                    {!summary?.recent_jobs.length ? <div className="py-8 text-center text-xs text-muted-foreground">该时间范围内暂无新自动分析任务</div> : null}
                  </div>
                </section>
              ) : null}

              <section>
                <div className="mb-2 flex items-center justify-between"><h3 className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">最近调用</h3><button type="button" onClick={() => setTab("details")} className="text-xs text-primary hover:underline">查看全部</button></div>
                <div className="rounded-xl border bg-background/30 px-3">{events.slice(0, 5).map((event) => <UsageEventRow key={event.event_id} event={event} />)}{!eventsLoading && !events.length ? <div className="py-8 text-center text-xs text-muted-foreground">暂无调用</div> : null}</div>
              </section>
            </div>
          ) : aggregate ? (
            <div className="space-y-3">
              <div className="flex flex-wrap gap-2">
                <label className="relative"><span className="sr-only">调用类型</span><select value={kindFilter} onChange={(event) => setKindFilter(event.target.value as UsageEventKind | "")} className="appearance-none rounded-lg border bg-background py-2 pl-3 pr-8 text-xs"><option value="">全部类型</option><option value="llm_call">模型调用</option><option value="tool_call">Agent 工具</option><option value="resource_call">资源请求</option></select><ChevronDown className="pointer-events-none absolute right-2.5 top-2.5 h-3.5 w-3.5 text-muted-foreground" /></label>
                <label className="relative"><span className="sr-only">资源类别</span><select value={categoryFilter} onChange={(event) => setCategoryFilter(event.target.value)} className="appearance-none rounded-lg border bg-background py-2 pl-3 pr-8 text-xs"><option value="">全部类别</option>{Object.entries(CATEGORY_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><ChevronDown className="pointer-events-none absolute right-2.5 top-2.5 h-3.5 w-3.5 text-muted-foreground" /></label>
                <button type="button" onClick={() => void loadEvents()} className="inline-flex items-center gap-1.5 rounded-lg border px-3 py-2 text-xs"><RefreshCw className={`h-3.5 w-3.5 ${eventsLoading ? "animate-spin" : ""}`} />刷新</button>
              </div>
              <div className="rounded-xl border bg-background/30 px-3">{eventsError ? <div className="flex items-center justify-center gap-2 py-10 text-sm text-red-400"><TriangleAlert className="h-4 w-4" />调用明细加载失败</div> : events.map((event) => <UsageEventRow key={event.event_id} event={event} />)}{!eventsError && !eventsLoading && !events.length ? <div className="py-10 text-center text-sm text-muted-foreground">没有符合条件的调用</div> : null}</div>
              {nextCursor ? <button type="button" disabled={eventsLoading} onClick={() => void loadEvents(nextCursor, true)} className="w-full rounded-lg border py-2 text-xs disabled:opacity-50">{eventsLoading ? "加载中…" : "加载更多"}</button> : null}
            </div>
          ) : null}
        </div>

        <footer className="flex flex-wrap items-center justify-between gap-2 border-t px-5 py-3 text-[10px] text-muted-foreground sm:px-6">
          <span className="inline-flex items-center gap-1.5"><Database className="h-3 w-3" />{selectedJobId ? "任务全链路" : PERIOD_LABELS[period]} · 原始事件不重复复制</span>
          <span className="inline-flex items-center gap-1.5"><HardDrive className="h-3 w-3" />价格目录 {aggregateCost.catalog_version} · 人民币与美元分开</span>
        </footer>
      </div>
    </div>,
    document.body,
  ) : null;

  return (
    <>
      <button ref={triggerRef} type="button" onClick={() => { setOpen(true); void loadSummary(period); }} className="inline-flex items-center gap-2 rounded-full border bg-background/80 px-3 py-2 text-xs font-medium shadow-sm hover:border-primary/50" aria-label={`打开 AI 监控 Token 总账，${PERIOD_LABELS[period]} ${badgeToken} Token`}>
        <Gauge className={`h-3.5 w-3.5 text-primary ${running ? "animate-pulse motion-reduce:animate-none" : ""}`} />
        <span>{PERIOD_LABELS[period]} {badgeToken} Token{badgeCost ? ` · ${badgeCost}` : ""}</span>
      </button>
      {modal}
    </>
  );
}
