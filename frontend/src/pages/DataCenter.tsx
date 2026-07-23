import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type MarketCacheRun } from "@/lib/api";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Database,
  Loader2,
  Plus,
  Radio,
  RefreshCw,
  ServerCrash,
  Trash2,
} from "lucide-react";
import {
  dataApi,
  type CoverageRow,
  type PrewarmStatus,
  type SourceHealth,
  type StorageEntry,
  type WatchlistEntry,
} from "@/lib/dataApi";
import { cn } from "@/lib/utils";

type LoadState = {
  coverage: CoverageRow[];
  sources: SourceHealth[];
  storage: StorageEntry[];
  totalBytes: number;
  softLimitBytes: number;
  watchlist: WatchlistEntry[];
  prewarm: PrewarmStatus | null;
};

const EMPTY: LoadState = {
  coverage: [],
  sources: [],
  storage: [],
  totalBytes: 0,
  softLimitBytes: 10 * 1024 ** 3,
  watchlist: [],
  prewarm: null,
};
const TERMINAL_REFRESH_STATUSES = new Set(["completed", "partial", "failed", "interrupted"]);
const PRICE_BASIS_GUIDE = [
  {
    key: "raw",
    title: "原始成交价（raw）",
    detail: "保留历史当日真实成交价，不处理拆股、分红、配股等公司行为。",
  },
  {
    key: "qfq",
    title: "最新价锚定全复权（qfq）",
    detail: "拆股和现金分配都纳入调整，最新一根与原始收盘价一致；海外 Adj Close 只有按同一因子缩放完整 OHLC 后才归入这里。",
  },
  {
    key: "hfq",
    title: "历史起点锚定全复权（hfq）",
    detail: "同样处理公司行为，但保持完整历史起点的价格基准；仅有区间数据时不能擅自推导。",
  },
  {
    key: "split_adjusted",
    title: "仅拆股调整（split_adjusted）",
    detail: "只统一拆股后的股本单位，不处理现金分红；不能与 raw 或 qfq 混合验证。",
  },
  {
    key: "source_default",
    title: "口径未分类（source_default）",
    detail: "提供方调整规则尚未被适配器证明，允许诊断查看，但禁止进入跨来源价格共识。",
  },
] as const;

export function DataCenter() {
  const [searchParams] = useSearchParams();
  const focusSymbol = (searchParams.get("symbol") || "").trim().toUpperCase();
  const queryName = (searchParams.get("name") || "").trim().slice(0, 100);
  const [state, setState] = useState<LoadState>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [symbol, setSymbol] = useState("");
  const [adding, setAdding] = useState(false);
  const [resolvedName, setResolvedName] = useState(queryName);
  const [refreshRun, setRefreshRun] = useState<MarketCacheRun | null>(null);
  const [refreshMessage, setRefreshMessage] = useState<string | null>(null);

  const load = useCallback(async (mode: "initial" | "refresh" = "refresh") => {
    if (mode === "initial") setLoading(true);
    else setRefreshing(true);
    setError(null);
    try {
      const [coverage, sources, storage, watchlist, prewarm] = await Promise.all([
        dataApi.coverage(),
        dataApi.sources(),
        dataApi.storage(),
        dataApi.watchlist(),
        dataApi.prewarmStatus(),
      ]);
      setState({
        coverage: coverage.coverage,
        sources: sources.sources,
        storage: storage.entries,
        totalBytes: storage.total_bytes,
        softLimitBytes: storage.soft_limit_bytes,
        watchlist: watchlist.watchlist,
        prewarm,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "数据中心暂不可用");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load("initial");
  }, [load]);

  const visibleCoverage = useMemo(
    () => focusSymbol
      ? state.coverage.filter((row) => row.symbol.toUpperCase() === focusSymbol)
      : state.coverage,
    [focusSymbol, state.coverage],
  );
  const coverageName = String(
    visibleCoverage.find((row) => String(row.name || "").trim())?.name || "",
  ).trim();

  useEffect(() => {
    const knownName = queryName || coverageName;
    if (knownName) {
      setResolvedName(knownName);
      return;
    }
    if (!focusSymbol || loading) {
      setResolvedName("");
      return;
    }
    let cancelled = false;
    void api.lookupPortfolioSecurity(focusSymbol)
      .then((result) => {
        if (!cancelled) setResolvedName(result.name);
      })
      .catch(() => {
        if (!cancelled) setResolvedName("");
      });
    return () => { cancelled = true; };
  }, [coverageName, focusSymbol, loading, queryName]);
  const visibleSources = useMemo(() => {
    if (!focusSymbol) return state.sources;
    const names = new Set(visibleCoverage.map((row) => row.actual_source.toLowerCase()));
    return state.sources.filter((source) => names.has(
      String(source.actual_source || source.requested_source).toLowerCase(),
    ));
  }, [focusSymbol, state.sources, visibleCoverage]);
  const sourceSummary = useMemo(() => {
    let live = 0;
    let stale = 0;
    let degraded = 0;
    let applicable = 0;
    for (const source of visibleSources) {
      if (source.effective_status === "not_applicable") continue;
      applicable += 1;
      if (source.stale) stale += 1;
      else if (!source.circuit_open && ["ok", "ok_with_transport_fallback"].includes(source.effective_status)) live += 1;
      else degraded += 1;
    }
    return { live, stale, degraded, applicable };
  }, [visibleSources]);
  const focusedIntervals = Array.from(new Set(visibleCoverage.map((row) => row.interval))).join(" / ") || "-";
  const focusedLatestSuccess = visibleCoverage.reduce(
    (latest, row) => row.last_success_at > latest ? row.last_success_at : latest,
    "",
  );
  const storagePercent = state.softLimitBytes
    ? Math.min(100, (state.totalBytes / state.softLimitBytes) * 100)
    : 0;

  const addWatchlist = async () => {
    const value = symbol.trim().toUpperCase();
    if (!value) return;
    setAdding(true);
    try {
      await dataApi.addWatchlist(value);
      setSymbol("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "无法添加自选标的");
    } finally {
      setAdding(false);
    }
  };

  const removeWatchlist = async (target: string) => {
    try {
      await dataApi.removeWatchlist(target);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "无法移除自选标的");
    }
  };

  const prewarm = async () => {
    setRefreshing(true);
    setError(null);
    try {
      await dataApi.prewarm("premarket");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "预热未完成");
    } finally {
      setRefreshing(false);
    }
  };

  const refreshFocusedSymbol = async () => {
    if (!focusSymbol) {
      await load();
      return;
    }
    setRefreshing(true);
    setError(null);
    setRefreshMessage(`正在启动 ${focusSymbol} 行情刷新…`);
    try {
      const accepted = await api.startMarketCacheRefresh({
        symbols: [focusSymbol],
        profile: "symbol_detail",
      });
      let run = accepted.run;
      setRefreshRun(run);
      for (let poll = 0; !TERMINAL_REFRESH_STATUSES.has(run.status) && poll < 240; poll += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 500));
        run = await api.getMarketCacheRun(accepted.run_id);
        setRefreshRun(run);
      }
      if (!TERMINAL_REFRESH_STATUSES.has(run.status)) {
        setRefreshMessage("刷新仍在后台运行，可稍后再次查看。");
        return;
      }
      const attempts = run.items.flatMap((item) => item.attempts || []);
      const reused = attempts.filter((attempt) => attempt.status === "cache_fresh").length;
      const requested = attempts.filter(
        (attempt) => !["cache_fresh", "retry_backoff"].includes(attempt.status),
      ).length;
      const failed = attempts.filter((attempt) => attempt.status === "failed").length;
      await load();
      setRefreshMessage(
        requested === 0
          ? `刷新完成：复用 ${reused} 个有效来源，本次没有发起新的行情请求。`
          : `刷新完成：复用 ${reused} 个有效来源，实际请求 ${requested} 个来源${failed ? `，其中 ${failed} 个失败` : ""}。`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "行情刷新失败");
      setRefreshMessage(null);
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="min-h-screen p-4 sm:p-6 lg:p-8">
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-5">
        <header className="flex flex-col gap-3 border-b pb-5 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <div className="inline-flex items-center gap-2 rounded-md border px-2.5 py-1 text-xs font-medium text-muted-foreground">
              <Database className="h-3.5 w-3.5" />{focusSymbol ? "单标的行情" : "统一数据层"}
            </div>
            <h1 className="mt-3 text-3xl font-bold tracking-tight">
              {focusSymbol ? (
                <>
                  {resolvedName ? <span>{resolvedName} </span> : <span className="text-muted-foreground">名称解析中… </span>}
                  <span className="font-mono">{focusSymbol}</span>
                  <span> 行情详情</span>
                </>
              ) : "数据中心"}
            </h1>
            <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
              {focusSymbol
                ? "这里只展示该标的的缓存覆盖和相关来源状态。持仓页的全局行情刷新会复用有效成功源，只重试到期的失败源。"
                : "区分真实联通故障、依赖缺失、无标的覆盖、来源延迟和过期状态；旧故障不会继续冒充当前离线。"}
            </p>
          </div>
          <div className="flex gap-2">
            {focusSymbol ? (
              <Link to="/data-center" className="inline-flex items-center justify-center rounded-md border px-3 py-2 text-sm font-medium hover:bg-muted">
                查看全部标的
              </Link>
            ) : (
              <button type="button" onClick={prewarm} disabled={refreshing} className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50">
                {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Radio className="h-4 w-4" />}立即预热
              </button>
            )}
            <button type="button" onClick={() => void (focusSymbol ? refreshFocusedSymbol() : load())} disabled={refreshing} className="inline-flex items-center justify-center gap-2 rounded-md border px-3 py-2 text-sm font-medium hover:bg-muted disabled:opacity-50">
              {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              {focusSymbol ? (refreshing ? `刷新中 ${Math.round(refreshRun?.progress_pct || 0)}%` : "刷新行情") : "刷新状态"}
            </button>
          </div>
        </header>

        {refreshMessage ? (
          <section role="status" className="rounded-md border border-primary/25 bg-primary/5 px-4 py-3 text-sm">
            {refreshMessage}
          </section>
        ) : null}

        {error ? (
          <section role="alert" className="rounded-md border border-amber-500/30 bg-amber-500/5 p-4 text-sm text-amber-700 dark:text-amber-300">
            <AlertTriangle className="mr-2 inline h-4 w-4" />{error}
          </section>
        ) : null}

        {loading ? (
          <div className="grid gap-3 sm:grid-cols-2 2xl:grid-cols-4">
            {[1, 2, 3, 4].map((key) => <div key={key} className="h-28 animate-pulse rounded-md border bg-muted/40" />)}
          </div>
        ) : (
          <section className="grid gap-3 sm:grid-cols-2 2xl:grid-cols-4" aria-label="数据层状态摘要">
            {focusSymbol ? (
              <>
                <Summary title="缓存来源" value={String(new Set(visibleCoverage.map((row) => row.actual_source)).size)} description="该标的已有数据的实际来源" tone={visibleCoverage.length ? "good" : "muted"} icon={CheckCircle2} />
                <Summary title="缓存路径" value={String(visibleCoverage.length)} description="来源、周期和复权口径组合" tone={visibleCoverage.length ? "good" : "muted"} icon={Database} />
                <Summary title="已覆盖周期" value={focusedIntervals} description="当前已落库的 K 线周期" tone="muted" icon={Radio} />
                <Summary title="最近成功" value={focusedLatestSuccess ? shortDate(focusedLatestSuccess) : "-"} description="最近一次成功写入缓存" tone={focusedLatestSuccess ? "good" : "muted"} icon={RefreshCw} />
              </>
            ) : (
              <>
                <Summary title="当前可用来源" value={`${sourceSummary.live}/${sourceSummary.applicable}`} description="最近一次有效探测仍在 TTL 内；不适用项不计入分母" tone={sourceSummary.live ? "good" : "muted"} icon={CheckCircle2} />
                <Summary title="当前降级" value={String(sourceSummary.degraded)} description="故障、缺依赖和无覆盖分别标记；市场不适用不计为故障" tone={sourceSummary.degraded ? "warn" : "good"} icon={sourceSummary.degraded ? AlertTriangle : ServerCrash} />
                <Summary title="状态已过期" value={String(sourceSummary.stale)} description="市场源 30 分钟、研究源 6 小时" tone={sourceSummary.stale ? "warn" : "muted"} icon={RefreshCw} />
                <Summary title="存储使用" value={formatBytes(state.totalBytes)} description={`${storagePercent.toFixed(1)}% / ${formatBytes(state.softLimitBytes)}`} tone={storagePercent >= 90 ? "warn" : "muted"} icon={Database} />
              </>
            )}
          </section>
        )}

        <section className="rounded-md border p-4" aria-labelledby="price-basis-title">
          <div className="max-w-4xl">
            <h2 id="price-basis-title" className="font-semibold">统一价格口径</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              国内外叫法可以不同，但只有公司行为范围和锚点完全一致的数据才会合并；名称相似不代表口径相同。
            </p>
          </div>
          <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-5">
            {PRICE_BASIS_GUIDE.map((item) => (
              <article key={item.key} className="rounded-md border bg-muted/20 p-3">
                <h3 className="text-sm font-medium">{item.title}</h3>
                <p className="mt-1 text-xs leading-5 text-muted-foreground">{item.detail}</p>
              </article>
            ))}
          </div>
        </section>

        {!focusSymbol ? <section className="rounded-md border p-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h2 className="font-semibold">持仓与手动自选预热</h2>
              <p className="mt-1 text-sm text-muted-foreground">只有持仓和这里的自选会进入盘前/盘中预热。</p>
              {state.prewarm ? (
                <p className="mt-2 text-xs text-muted-foreground">
                  计划：{state.prewarm.slots.map((slot) => `${slot.time} ${slot.phase === "premarket" ? "盘前" : "盘中"}`).join(" · ")}
                  {state.prewarm.last_run?.at ? `；最近运行 ${shortDate(state.prewarm.last_run.at)}` : ""}
                </p>
              ) : null}
            </div>
            <div className="flex gap-2">
              <input aria-label="自选标的代码" value={symbol} onChange={(event) => setSymbol(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void addWatchlist(); }} placeholder="例如 510300.SH" className="w-40 rounded-md border bg-background px-3 py-2 text-sm" />
              <button type="button" onClick={() => void addWatchlist()} disabled={adding || !symbol.trim()} className="inline-flex items-center gap-1 rounded-md border px-3 py-2 text-sm hover:bg-muted disabled:opacity-50">
                {adding ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}添加
              </button>
            </div>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            {state.watchlist.length ? state.watchlist.map((item) => (
              <span key={item.symbol} className="inline-flex items-center gap-1 rounded-full border bg-muted/40 px-2.5 py-1 text-sm">
                <span className="font-mono">{item.symbol}</span>
                <button type="button" aria-label={`移除 ${item.symbol}`} onClick={() => void removeWatchlist(item.symbol)} className="rounded p-0.5 text-muted-foreground hover:bg-background hover:text-foreground">
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </span>
            )) : <span className="text-sm text-muted-foreground">尚无手动自选。</span>}
          </div>
        </section> : null}

        <section className="grid gap-5 xl:grid-cols-[0.85fr_1.15fr]">
          <TableCard title={focusSymbol ? `${focusSymbol} K 线缓存覆盖` : "K 线缓存覆盖"} description="按标的、周期、统一价格口径和实际适配器统计。" collapsible>
            <CoverageTable rows={visibleCoverage} />
          </TableCard>
          <TableCard title={focusSymbol ? "相关数据源健康度" : "数据源健康度"} description="上游指纹用于判断来源是否真正独立；价格口径不同会被拦截，不会为了凑来源而混合。">
            <SourcesTable rows={visibleSources} />
          </TableCard>
        </section>

        {!focusSymbol ? <section className="rounded-md border p-4">
          <h2 className="font-semibold">存储与保留</h2>
          <div className="mt-3 h-2 overflow-hidden rounded-full bg-muted" aria-label={`存储使用 ${storagePercent.toFixed(1)}%`}>
            <div className={cn("h-full rounded-full", storagePercent >= 90 ? "bg-amber-500" : "bg-primary")} style={{ width: `${storagePercent}%` }} />
          </div>
          <div className="mt-4 grid gap-2 text-sm sm:grid-cols-3">
            {state.storage.map((entry) => <div key={entry.kind} className="rounded-md border p-3"><div className="font-medium">{entry.kind}</div><div className="mt-1 text-muted-foreground">{formatBytes(entry.bytes)}</div></div>)}
          </div>
        </section> : null}
      </div>
    </div>
  );
}

function Summary({ title, value, description, tone, icon: Icon }: { title: string; value: string; description: string; tone: "good" | "warn" | "muted"; icon: typeof Database }) {
  return <article className="rounded-md border p-4"><div className="flex items-center justify-between gap-3"><span className="text-xs font-medium uppercase text-muted-foreground">{title}</span><Icon className={cn("h-4 w-4", tone === "good" && "text-emerald-600", tone === "warn" && "text-amber-600", tone === "muted" && "text-muted-foreground")} /></div><div className="mt-3 text-2xl font-semibold">{value}</div><p className="mt-1 text-xs text-muted-foreground">{description}</p></article>;
}

function TableCard({ title, description, children, collapsible = false }: { title: string; description: string; children: ReactNode; collapsible?: boolean }) {
  const [expanded, setExpanded] = useState(() => !collapsible);
  const toggleLabel = `${expanded ? "收起" : "展开"} ${title}`;

  return (
    <section className="rounded-md border p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="font-semibold">{title}</h2>
          {expanded ? <p className="mt-1 text-sm text-muted-foreground">{description}</p> : null}
        </div>
        {collapsible ? (
          <button
            type="button"
            aria-expanded={expanded}
            aria-label={toggleLabel}
            title={toggleLabel}
            onClick={() => setExpanded((current) => !current)}
            className="inline-flex shrink-0 items-center gap-1 rounded-md border px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <span>{expanded ? "收起" : "展开"}</span>
            <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", expanded && "rotate-180")} aria-hidden="true" />
          </button>
        ) : null}
      </div>
      {expanded ? <div className="mt-4 overflow-x-auto">{children}</div> : null}
    </section>
  );
}

function CoverageTable({ rows }: { rows: CoverageRow[] }) {
  if (!rows.length) return <p className="py-5 text-sm text-muted-foreground">暂无 K 线缓存。</p>;
  return <table className="w-full min-w-[530px] text-left text-sm"><thead className="border-b text-xs text-muted-foreground"><tr><th className="pb-2 font-medium">标的</th><th className="pb-2 font-medium">周期</th><th className="pb-2 font-medium">价格调整口径</th><th className="pb-2 font-medium">范围（北京时间）</th><th className="pb-2 font-medium">适配器</th></tr></thead><tbody>{rows.map((row) => <tr key={`${row.symbol}-${row.actual_source}-${row.interval}-${row.actual_adjustment}`} className="border-b last:border-0"><td className="py-2 font-mono">{row.symbol}</td><td className="py-2">{row.interval}</td><td className="py-2">{adjustmentLabel(row.actual_adjustment)}</td><td className="py-2 text-xs text-muted-foreground">{shortDate(row.min_bar_time)} → {shortDate(row.max_bar_time)}</td><td className="py-2">{row.actual_source}</td></tr>)}</tbody></table>;
}

function SourcesTable({ rows }: { rows: SourceHealth[] }) {
  if (!rows.length) return <p className="py-5 text-sm text-muted-foreground">尚无数据源探测记录。</p>;
  return (
    <table className="w-full min-w-[900px] text-left text-sm">
      <thead className="border-b text-xs text-muted-foreground"><tr><th className="pb-2 font-medium">请求 / 能力</th><th className="pb-2 font-medium">实际 / 上游</th><th className="pb-2 font-medium">状态</th><th className="pb-2 font-medium">最近尝试（北京时间）</th><th className="pb-2 font-medium">延迟</th><th className="pb-2 font-medium">具体原因</th></tr></thead>
      <tbody>{rows.map((row) => (
        <tr key={row.source} className="border-b align-top last:border-0">
          <td className="py-2"><div className="font-medium">{row.requested_source}</div><div className="text-xs text-muted-foreground">{capabilityLabel(row.capability)}</div></td>
          <td className="py-2"><div>{row.actual_source || "-"}</div><div className="font-mono text-xs text-muted-foreground">{row.upstream_source || "-"}</div></td>
          <td className="py-2"><SourceStatus row={row} /></td>
          <td className="py-2 text-xs text-muted-foreground">{shortDate(row.updated_at)}</td>
          <td className="py-2">{row.last_latency_ms == null ? "-" : `${row.last_latency_ms.toFixed(0)} ms`}</td>
          <td className="max-w-[320px] py-2"><div className="text-xs font-medium">{errorCategoryLabel(row)}</div><div className="mt-0.5 break-words text-xs text-muted-foreground">{sourceErrorDetail(row)}</div></td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function SourceStatus({ row }: { row: SourceHealth }) {
  const status = row.circuit_open ? "circuit_open" : row.effective_status;
  const good = ["ok", "ok_with_transport_fallback"].includes(status);
  const neutral = status === "not_applicable";
  const warning = ["stale", "degraded", "no_coverage", "basis_mismatch", "duplicate_upstream", "ok_with_transport_fallback", "unavailable"].includes(status);
  const labels: Record<string, string> = {
    ok: "可用",
    ok_with_transport_fallback: "可用（已切换传输）",
    degraded: "部分请求降级",
    stale: "状态已过期",
    no_coverage: "无该标的覆盖",
    basis_mismatch: "价格口径不一致",
    duplicate_upstream: "重复上游",
    dependency_missing: "运行依赖缺失",
    unavailable: "提供方暂不可用",
    not_applicable: "不适用于该市场",
    failed: "请求失败",
    circuit_open: "熔断冷却",
  };
  return <span className={cn("rounded-full px-2 py-0.5 text-xs", good && !warning && "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300", warning && "bg-amber-500/15 text-amber-700 dark:text-amber-300", neutral && "bg-muted text-muted-foreground", !good && !warning && !neutral && "bg-destructive/10 text-destructive")}>{labels[status] || status}</span>;
}

function capabilityLabel(value: string): string {
  const labels: Record<string, string> = {
    market: "行情",
    fundamental: "基本面",
    news: "新闻",
    report: "研报",
    general: "通用",
  };
  return labels[value] || value;
}

function adjustmentLabel(value: string): string {
  const labels: Record<string, string> = {
    raw: "原始成交价（raw）",
    qfq: "最新价锚定全复权（qfq）",
    hfq: "历史起点锚定全复权（hfq）",
    split_adjusted: "仅拆股调整（split_adjusted）",
    source_default: "口径未分类（source_default）",
    unknown: "口径未知（unknown）",
  };
  return labels[value] || value;
}

function errorCategoryLabel(row: SourceHealth): string {
  const labels: Record<string, string> = {
    transport_error: "网络/传输故障",
    transport_fallback: "主请求通道失败，备用通道成功",
    rate_limited: "提供方限流",
    dependency_missing: "运行依赖缺失",
    no_coverage: "提供方无覆盖",
    basis_mismatch: "价格口径不一致（已阻止混用）",
    duplicate_upstream: "非独立来源",
    provider_unavailable: "提供方暂不可用",
    not_applicable: "市场不适用",
    provider_error: "提供方错误",
  };
  if (row.error_category) return labels[row.error_category] || row.error_category;
  return ["ok", "ok_with_transport_fallback"].includes(row.effective_status)
    ? "正常"
    : "提供方请求失败";
}

function sourceErrorDetail(row: SourceHealth): string {
  const value = String(row.last_error || "");
  if (!value) return "-";
  if (value.includes("provider returned no usable bars")) {
    return "本次请求未返回该标的的可用行情。";
  }
  const basisMatch = value.match(/requested ([a-z_]+), provider returned ([a-z_]+)/i);
  if (basisMatch) {
    return `请求${adjustmentLabel(basisMatch[1])}，提供方返回${adjustmentLabel(basisMatch[2])}；两者未被视为等价，数据已排除。`;
  }
  const marketMatch = value.match(/research reports are China A-share only .*; got '([^']+)'/i);
  if (marketMatch) {
    return `该研报源仅支持中国 A 股，${marketMatch[1]} 不适用。`;
  }
  if (value.startsWith("provider declared unavailable")) {
    return "该提供方本次未返回可用能力。";
  }
  if (value.includes("Data source") && value.includes("is unavailable")) {
    return "运行环境未能加载该数据源依赖。";
  }
  if (value.includes("Remote end closed connection")) {
    return "上游服务主动关闭了连接，请稍后重试。";
  }
  if (value.includes("Unknown data source: auto")) {
    return "自动路由不是独立数据源，不能作为上游参与健康探测。";
  }
  return value;
}

function shortDate(value: string): string {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value.replace("T", " ").slice(0, 16);
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(parsed);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day} ${values.hour}:${values.minute}`;
}
function formatBytes(bytes: number): string { if (bytes < 1024) return `${bytes} B`; const units = ["KB", "MB", "GB", "TB"]; let value = bytes / 1024; let index = 0; while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; } return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[index]}`; }
