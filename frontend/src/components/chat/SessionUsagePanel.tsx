import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Activity,
  Bot,
  ChevronDown,
  CircleDollarSign,
  Clock3,
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
  type SessionUsageAggregate,
  type SessionUsageSummary,
  type UsageEventItem,
  type UsageEventKind,
  type UsageCostAggregate,
  type UsageCostCurrencyAggregate,
} from "@/lib/api";

interface SessionUsagePanelProps {
  sessionId: string | null;
  running: boolean;
  refreshSignal: number;
  forceRefreshSignal: number;
}

type ScopeView = "session" | "attempt";
type TabView = "overview" | "details";

const CATEGORY_LABELS: Record<string, string> = {
  web: "网页",
  market: "行情",
  financial: "财务数据",
  local_data: "本地数据",
  compute: "计算",
  file: "文件",
  mcp: "MCP",
  other: "其他",
  llm: "模型",
};

const KIND_LABELS: Record<UsageEventKind, string> = {
  llm_call: "模型",
  tool_call: "工具",
  resource_call: "资源",
};

const CACHE_LABELS: Record<string, string> = {
  network: "联网",
  cache_hit: "缓存命中",
  cache_refresh: "刷新缓存",
  stale_fallback: "旧缓存兜底",
  unknown: "未知",
};

const EMPTY_COST: UsageCostAggregate = {
  coverage: "unreported",
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

function formatPercent(value: number | null | undefined): string {
  return value == null ? "未报告" : `${(value * 100).toFixed(1)}%`;
}

function formatElapsed(value: number | null | undefined): string {
  if (value == null) return "—";
  return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(1)} s`;
}

function formatMoney(currency: string, amount: number): string {
  const symbol = currency === "CNY" ? "¥" : currency === "USD" ? "$" : `${currency} `;
  const absolute = Math.abs(amount);
  const digits = absolute >= 1 ? 2 : absolute >= 0.01 ? 4 : 6;
  return `${symbol}${amount.toLocaleString("zh-CN", {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  })}`;
}

function formatCostCurrency(item: UsageCostCurrencyAggregate): string {
  const minimum = item.minimum_estimated_cost;
  const maximum = item.maximum_estimated_cost;
  if (Math.abs(maximum - minimum) > 0.0000000001) {
    return `${formatMoney(item.currency, minimum)}–${formatMoney(item.currency, maximum)}`;
  }
  return formatMoney(item.currency, item.estimated_cost);
}

function formatCostSummary(cost: UsageCostAggregate | undefined): string | null {
  if (!cost) return null;
  if (cost.currencies.length === 0) return null;
  return cost.currencies.map(formatCostCurrency).join(" + ");
}

function aggregateFor(summary: SessionUsageSummary, view: ScopeView): SessionUsageAggregate {
  return view === "session" ? summary.session : summary.current_attempt;
}

function metricCard(label: string, value: string, hint?: string) {
  return (
    <div className="rounded-xl border border-border/80 bg-background/40 p-3">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="mt-1 text-xl font-semibold tracking-tight text-foreground">{value}</div>
      {hint && <div className="mt-1 text-[10px] text-muted-foreground">{hint}</div>}
    </div>
  );
}

function EventRow({ event }: { event: UsageEventItem }) {
  const name = event.tool_name || event.model || event.provider || KIND_LABELS[event.kind];
  const isError = event.status === "error";
  const eventCost = event.cost?.currency && event.cost.maximum_estimated_cost != null
    ? formatCostCurrency({
        currency: event.cost.currency,
        estimated_cost: event.cost.estimated_cost ?? event.cost.maximum_estimated_cost,
        minimum_estimated_cost: event.cost.minimum_estimated_cost ?? event.cost.maximum_estimated_cost,
        maximum_estimated_cost: event.cost.maximum_estimated_cost,
        calls: 1,
        peak_calls: event.cost.tier === "peak" ? 1 : 0,
      })
    : null;
  return (
    <div className="grid gap-2 border-b border-border/60 px-1 py-3 last:border-b-0 sm:grid-cols-[minmax(0,1fr)_auto]">
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2">
          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${isError ? "bg-red-400" : event.status === "running" ? "bg-primary" : "bg-emerald-400"}`} />
          <span className="truncate text-sm font-medium text-foreground">{name}</span>
          <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
            {KIND_LABELS[event.kind]}
          </span>
          {event.category && (
            <span className="hidden shrink-0 text-[10px] text-muted-foreground sm:inline">
              {CATEGORY_LABELS[event.category] || event.category}
            </span>
          )}
        </div>
        <div className="mt-1 truncate pl-3.5 text-xs text-muted-foreground">
          {event.query_summary || (event.kind === "llm_call" && event.total_tokens != null ? `${formatToken(event.total_tokens)} Token` : "未保存原始参数或返回正文")}
        </div>
      </div>
      <div className="flex items-center gap-3 pl-3.5 text-[11px] text-muted-foreground sm:pl-0">
        {eventCost && <span title="按调用时间与价格目录估算">{eventCost}</span>}
        {event.cost?.tier === "peak" && <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-400">高峰 2×</span>}
        {event.cache_mode && <span>{CACHE_LABELS[event.cache_mode] || event.cache_mode}</span>}
        <span>{formatElapsed(event.elapsed_ms)}</span>
        <span>{new Date(event.started_at).toLocaleString("zh-CN", {
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false,
        })}</span>
      </div>
    </div>
  );
}

export function SessionUsagePanel({
  sessionId,
  running,
  refreshSignal,
  forceRefreshSignal,
}: SessionUsagePanelProps) {
  const [summary, setSummary] = useState<SessionUsageSummary | null>(null);
  const [summaryError, setSummaryError] = useState(false);
  const [open, setOpen] = useState(false);
  const [scopeView, setScopeView] = useState<ScopeView>("session");
  const [tab, setTab] = useState<TabView>("overview");
  const [events, setEvents] = useState<UsageEventItem[]>([]);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [eventsError, setEventsError] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [kindFilter, setKindFilter] = useState<UsageEventKind | "">("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const triggerRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const activeSessionRef = useRef(sessionId);
  activeSessionRef.current = sessionId;

  const loadSummary = useCallback(async () => {
    if (!sessionId) return;
    const requestedSession = sessionId;
    try {
      const next = await api.getSessionUsage(sessionId);
      if (activeSessionRef.current !== requestedSession) return;
      setSummary((current) => current && next.revision < current.revision ? current : next);
      setSummaryError(false);
    } catch {
      if (activeSessionRef.current !== requestedSession) return;
      setSummaryError(true);
    }
  }, [sessionId]);

  const loadEvents = useCallback(async (cursor?: string, append = false) => {
    if (!sessionId) return;
    if (scopeView === "attempt" && !summary?.current_attempt_id) {
      setEvents([]);
      setNextCursor(null);
      setEventsError(false);
      return;
    }
    setEventsLoading(true);
    setEventsError(false);
    const requestedSession = sessionId;
    try {
      const page = await api.getSessionUsageEvents(sessionId, {
        kind: kindFilter || undefined,
        category: categoryFilter || undefined,
        attemptId: scopeView === "attempt" ? summary?.current_attempt_id || undefined : undefined,
        cursor,
        limit: 50,
      });
      if (activeSessionRef.current !== requestedSession) return;
      setEvents((current) => append ? [...current, ...page.items] : page.items);
      setNextCursor(page.next_cursor);
    } catch {
      if (activeSessionRef.current !== requestedSession) return;
      setEventsError(true);
    } finally {
      if (activeSessionRef.current === requestedSession) setEventsLoading(false);
    }
  }, [categoryFilter, kindFilter, scopeView, sessionId, summary?.current_attempt_id]);

  useEffect(() => {
    setSummary(null);
    setSummaryError(false);
    setOpen(false);
    setEvents([]);
    setNextCursor(null);
    if (sessionId) void loadSummary();
  }, [loadSummary, sessionId]);

  useEffect(() => {
    if (!sessionId || refreshSignal === 0) return;
    const timer = window.setTimeout(() => void loadSummary(), 500);
    return () => window.clearTimeout(timer);
  }, [loadSummary, refreshSignal, sessionId]);

  useEffect(() => {
    if (!sessionId || forceRefreshSignal === 0) return;
    void loadSummary();
  }, [forceRefreshSignal, loadSummary, sessionId]);

  useEffect(() => {
    if (!open) return;
    void loadEvents();
  }, [loadEvents, open, summary?.revision]);

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
      if (focusable.length === 0) return;
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

  const aggregate = useMemo(
    () => summary ? aggregateFor(summary, scopeView) : null,
    [scopeView, summary],
  );
  const aggregateCost = aggregate?.cost ?? EMPTY_COST;

  if (!sessionId) return null;

  const unrecorded = summary?.recording_status === "unrecorded";
  const badgeToken = summaryError
    ? "统计不可用"
    : !summary
      ? "加载中"
      : unrecorded
        ? "未记录"
        : `${formatToken(summary.session.tokens.total_tokens)} Token`;
  const badgeCalls = summary && !unrecorded ? summary.session.calls.agent_tools : null;
  const badgeCost = summary && !unrecorded ? formatCostSummary(summary.session.cost) : null;

  const modal = open && summary && aggregate ? createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-0 backdrop-blur-md sm:p-5"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) setOpen(false);
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="session-usage-title"
        className="flex h-[100dvh] w-full flex-col overflow-hidden border-border bg-card shadow-2xl sm:h-auto sm:max-h-[88vh] sm:max-w-4xl sm:rounded-2xl sm:border"
      >
        <header className="flex items-start justify-between gap-4 border-b border-border px-5 py-4 sm:px-6">
          <div>
            <div className="flex items-center gap-2">
              <Gauge className="h-4 w-4 text-primary" />
              <h2 id="session-usage-title" className="text-base font-semibold text-foreground">Session 资源用量</h2>
              {running && <span className="rounded-full bg-primary/15 px-2 py-0.5 text-[10px] font-medium text-primary">实时记录中</span>}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">Token、Agent 工具、外部请求与缓存访问分开统计</p>
          </div>
          <button ref={closeRef} type="button" aria-label="关闭资源用量" onClick={() => setOpen(false)} className="rounded-lg p-2 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground">
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-5 py-3 sm:px-6">
          <div className="flex rounded-lg bg-muted/70 p-1" aria-label="统计范围">
            {(["session", "attempt"] as ScopeView[]).map((view) => (
              <button key={view} type="button" onClick={() => setScopeView(view)} className={`rounded-md px-3 py-1.5 text-xs transition-colors ${scopeView === view ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground"}`}>
                {view === "session" ? "整个 Session" : "本轮"}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-1 border-b border-border" role="tablist" aria-label="用量视图">
            {(["overview", "details"] as TabView[]).map((view) => (
              <button key={view} type="button" role="tab" aria-selected={tab === view} onClick={() => setTab(view)} className={`border-b-2 px-3 py-2 text-xs transition-colors ${tab === view ? "border-primary text-foreground" : "border-transparent text-muted-foreground hover:text-foreground"}`}>
                {view === "overview" ? "概览" : "调用明细"}
              </button>
            ))}
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5 sm:px-6">
          {unrecorded ? (
            <div className="flex min-h-64 flex-col items-center justify-center text-center">
              <Clock3 className="mb-3 h-8 w-8 text-muted-foreground" />
              <div className="font-medium text-foreground">这个旧 Session 未记录用量</div>
              <p className="mt-2 max-w-md text-sm text-muted-foreground">统计从功能上线后的新调用开始，不对历史 Token 或资源请求做估算与回填。</p>
            </div>
          ) : tab === "overview" ? (
            <div className="space-y-5">
              <section>
                <div className="mb-2 flex items-center justify-between gap-3">
                  <h3 className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                    <CircleDollarSign className="h-3.5 w-3.5 text-primary" />费用估算
                  </h3>
                  <span className="text-right text-[11px] text-muted-foreground">
                    覆盖：{aggregateCost.coverage === "complete" ? "完整" : aggregateCost.coverage === "partial" ? "部分计价" : aggregateCost.coverage === "unreported" ? "未配置或未报告" : "暂无调用"}
                  </span>
                </div>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  {aggregateCost.currencies.length > 0
                    ? aggregateCost.currencies.map((item) => (
                        <div key={item.currency} className="contents">
                          {metricCard(
                            `${item.currency} 预估`,
                            formatCostCurrency(item),
                            item.peak_calls ? `${item.peak_calls} 次高峰调用` : "按原币种计算",
                          )}
                        </div>
                      ))
                    : metricCard("预估费用", "未配置", "未找到可靠价格或 Token 未报告")}
                  {metricCard("已计价调用", `${aggregateCost.priced_calls}/${aggregateCost.total_calls}`)}
                  {metricCard(
                    "高峰调用",
                    aggregateCost.currencies.reduce((sum, item) => sum + item.peak_calls, 0).toLocaleString("zh-CN"),
                    "DeepSeek 北京时间 9–12、14–18 点",
                  )}
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-muted-foreground">
                  <span>基于每次调用开始时间；人民币与美元不做汇率换算。</span>
                  {aggregateCost.currencies.some((item) => item.minimum_estimated_cost !== item.maximum_estimated_cost) && (
                    <span>区间表示提供商未报告缓存命中 Token。</span>
                  )}
                  {aggregateCost.sources?.map((source) => (
                    <a key={source.url} href={source.url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
                      {source.label}
                    </a>
                  ))}
                </div>
              </section>

              <section>
                <div className="mb-2 flex items-center justify-between">
                  <h3 className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">Token</h3>
                  <span className="text-[11px] text-muted-foreground">覆盖：{aggregate.tokens.coverage === "complete" ? "完整" : aggregate.tokens.coverage === "partial" ? "部分报告" : aggregate.tokens.coverage === "unreported" ? "未报告" : "暂无调用"}</span>
                </div>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
                  {metricCard("总 Token", formatToken(aggregate.tokens.total_tokens))}
                  {metricCard("输入", formatToken(aggregate.tokens.input_tokens))}
                  {metricCard("输出", formatToken(aggregate.tokens.output_tokens))}
                  {metricCard("缓存读取", formatToken(aggregate.tokens.cache_read_input_tokens), "属于输入子集")}
                  {metricCard("缓存命中率", formatPercent(aggregate.tokens.cache_hit_rate))}
                </div>
                {aggregate.tokens.input_tokens != null && aggregate.tokens.output_tokens != null && (aggregate.tokens.input_tokens + aggregate.tokens.output_tokens) > 0 && (
                  <div className="mt-3 overflow-hidden rounded-full bg-muted" aria-label="Token 构成">
                    <div className="flex h-2 w-full">
                      <div className="bg-primary" style={{ width: `${Math.max(0, ((aggregate.tokens.input_tokens - (aggregate.tokens.cache_read_input_tokens || 0)) / (aggregate.tokens.input_tokens + aggregate.tokens.output_tokens)) * 100)}%` }} title="非缓存输入" />
                      <div className="bg-cyan-400" style={{ width: `${Math.max(0, (((aggregate.tokens.cache_read_input_tokens || 0) / (aggregate.tokens.input_tokens + aggregate.tokens.output_tokens)) * 100))}%` }} title="缓存输入" />
                      <div className="bg-violet-400" style={{ width: `${Math.max(0, ((aggregate.tokens.output_tokens / (aggregate.tokens.input_tokens + aggregate.tokens.output_tokens)) * 100))}%` }} title="输出" />
                    </div>
                  </div>
                )}
                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-muted-foreground">
                  <span><i className="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-primary" />非缓存输入</span>
                  <span><i className="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-cyan-400" />缓存输入（输入子集）</span>
                  <span><i className="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-violet-400" />输出</span>
                  {aggregate.tokens.cache_write_input_tokens != null && <span>缓存写入 {formatToken(aggregate.tokens.cache_write_input_tokens)}</span>}
                  {aggregate.tokens.reasoning_tokens != null && <span>推理 {formatToken(aggregate.tokens.reasoning_tokens)}</span>}
                </div>
              </section>

              <section>
                <h3 className="mb-2 text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">调用</h3>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
                  {metricCard("模型调用", aggregate.calls.llm_calls.toLocaleString("zh-CN"))}
                  {metricCard("Agent 工具", aggregate.calls.agent_tools.toLocaleString("zh-CN"))}
                  {metricCard("外部请求", aggregate.calls.external_requests.toLocaleString("zh-CN"))}
                  {metricCard("缓存访问", aggregate.calls.cache_accesses.toLocaleString("zh-CN"))}
                  {metricCard("失败", aggregate.calls.failures.toLocaleString("zh-CN"), aggregate.calls.running ? `${aggregate.calls.running} 个运行中` : undefined)}
                </div>
              </section>

              <section className="grid gap-3 md:grid-cols-3">
                {[
                  { title: "模型", icon: Bot, rows: aggregate.models },
                  { title: "工具类别", icon: Activity, rows: aggregate.categories },
                  { title: "网页与行情来源", icon: Globe2, rows: aggregate.providers },
                ].map(({ title, icon: Icon, rows }) => (
                  <div key={title} className="rounded-xl border border-border/80 bg-background/30 p-4">
                    <div className="mb-3 flex items-center gap-2 text-xs font-medium text-foreground"><Icon className="h-3.5 w-3.5 text-primary" />{title}</div>
                    <div className="space-y-2">
                      {rows.length === 0 ? <div className="text-xs text-muted-foreground">暂无记录</div> : rows.slice(0, 6).map((row) => (
                        <div key={row.key} className="flex items-center justify-between gap-3 text-xs">
                          <span className="truncate text-muted-foreground">{title === "工具类别" ? CATEGORY_LABELS[row.key] || row.key : row.key}</span>
                          <span className="font-medium text-foreground">{row.count}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </section>

              <section>
                <div className="mb-2 flex items-center justify-between">
                  <h3 className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">最近调用</h3>
                  <button type="button" onClick={() => setTab("details")} className="text-xs text-primary hover:underline">查看全部</button>
                </div>
                <div className="rounded-xl border border-border/80 bg-background/30 px-3">
                  {eventsLoading && events.length === 0 ? <div className="py-8 text-center text-xs text-muted-foreground">正在加载调用记录…</div> : events.slice(0, 5).map((event) => <EventRow key={event.event_id} event={event} />)}
                  {!eventsLoading && events.length === 0 && <div className="py-8 text-center text-xs text-muted-foreground">暂无调用记录</div>}
                </div>
              </section>
            </div>
          ) : (
            <div className="space-y-3">
              <div className="flex flex-wrap gap-2">
                <label className="relative">
                  <span className="sr-only">调用类型</span>
                  <select value={kindFilter} onChange={(event) => setKindFilter(event.target.value as UsageEventKind | "")} className="appearance-none rounded-lg border border-border bg-background py-2 pl-3 pr-8 text-xs text-foreground outline-none focus:border-primary">
                    <option value="">全部类型</option>
                    <option value="llm_call">模型调用</option>
                    <option value="tool_call">Agent 工具</option>
                    <option value="resource_call">资源请求</option>
                  </select>
                  <ChevronDown className="pointer-events-none absolute right-2.5 top-2.5 h-3.5 w-3.5 text-muted-foreground" />
                </label>
                <label className="relative">
                  <span className="sr-only">资源类别</span>
                  <select value={categoryFilter} onChange={(event) => setCategoryFilter(event.target.value)} className="appearance-none rounded-lg border border-border bg-background py-2 pl-3 pr-8 text-xs text-foreground outline-none focus:border-primary">
                    <option value="">全部类别</option>
                    {Object.entries(CATEGORY_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                  </select>
                  <ChevronDown className="pointer-events-none absolute right-2.5 top-2.5 h-3.5 w-3.5 text-muted-foreground" />
                </label>
                <button type="button" onClick={() => void loadEvents()} className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-xs text-muted-foreground hover:bg-muted hover:text-foreground">
                  <RefreshCw className={`h-3.5 w-3.5 ${eventsLoading ? "animate-spin motion-reduce:animate-none" : ""}`} />刷新
                </button>
              </div>
              <div className="rounded-xl border border-border/80 bg-background/30 px-3">
                {eventsError ? (
                  <div className="flex items-center justify-center gap-2 py-10 text-sm text-red-400"><TriangleAlert className="h-4 w-4" />调用明细加载失败</div>
                ) : events.map((event) => <EventRow key={event.event_id} event={event} />)}
                {!eventsError && !eventsLoading && events.length === 0 && <div className="py-10 text-center text-sm text-muted-foreground">没有符合条件的调用</div>}
              </div>
              {nextCursor && (
                <button type="button" disabled={eventsLoading} onClick={() => void loadEvents(nextCursor, true)} className="w-full rounded-lg border border-border py-2 text-xs text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-50">
                  {eventsLoading ? "加载中…" : "加载更多"}
                </button>
              )}
            </div>
          )}
        </div>

        <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-5 py-3 text-[10px] text-muted-foreground sm:px-6">
          <span className="inline-flex items-center gap-1.5"><Database className="h-3 w-3" />统计版本 {summary.revision} · 从 {summary.recording_started_at ? new Date(summary.recording_started_at).toLocaleString("zh-CN") : "现在"} 开始</span>
          <span className="inline-flex items-center gap-1.5"><HardDrive className="h-3 w-3" />费用为估算 · 价格目录 {aggregateCost.catalog_version} · 未报告字段不按字符数推算</span>
        </footer>
      </div>
    </div>,
    document.body,
  ) : null;

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => summary && setOpen(true)}
        aria-label={`打开 Session 资源用量：${badgeToken}${badgeCost ? `，费用 ${badgeCost}` : ""}${badgeCalls != null ? `，${badgeCalls} 次工具调用` : ""}`}
        className={`absolute right-3 top-3 z-30 inline-flex h-9 items-center gap-2 rounded-full border border-border/80 bg-card/90 px-3 text-xs font-medium text-foreground shadow-lg backdrop-blur-md transition-colors hover:border-primary/50 hover:bg-card sm:right-5 sm:top-4 ${running ? "ring-1 ring-primary/40" : ""}`}
      >
        <Gauge className={`h-3.5 w-3.5 text-primary ${running ? "animate-pulse motion-reduce:animate-none" : ""}`} />
        <span className="sm:hidden">{badgeToken.replace(" Token", "")}</span>
        <span className="hidden sm:inline">{badgeToken}{badgeCost ? ` · ${badgeCost}` : ""}{badgeCalls != null ? ` · ${badgeCalls} 调用` : ""}</span>
      </button>
      {modal}
    </>
  );
}
