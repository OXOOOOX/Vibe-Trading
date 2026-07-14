import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  CheckCircle2,
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

export function DataCenter() {
  const [state, setState] = useState<LoadState>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [symbol, setSymbol] = useState("");
  const [adding, setAdding] = useState(false);

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

  const sourceSummary = useMemo(() => {
    let live = 0;
    let stale = 0;
    let degraded = 0;
    for (const source of state.sources) {
      if (source.stale) stale += 1;
      else if (!source.circuit_open && ["ok", "ok_with_transport_fallback"].includes(source.effective_status)) live += 1;
      else degraded += 1;
    }
    return { live, stale, degraded };
  }, [state.sources]);
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

  return (
    <div className="min-h-screen p-4 sm:p-6 lg:p-8">
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-5">
        <header className="flex flex-col gap-3 border-b pb-5 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <div className="inline-flex items-center gap-2 rounded-md border px-2.5 py-1 text-xs font-medium text-muted-foreground">
              <Database className="h-3.5 w-3.5" />统一数据层
            </div>
            <h1 className="mt-3 text-3xl font-bold tracking-tight">数据中心</h1>
            <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
              区分真实联通故障、依赖缺失、无标的覆盖、来源延迟和过期状态；旧故障不会继续冒充当前离线。
            </p>
          </div>
          <div className="flex gap-2">
            <button type="button" onClick={prewarm} disabled={refreshing} className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50">
              {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Radio className="h-4 w-4" />}立即预热
            </button>
            <button type="button" onClick={() => void load()} disabled={refreshing} className="inline-flex items-center justify-center gap-2 rounded-md border px-3 py-2 text-sm font-medium hover:bg-muted disabled:opacity-50">
              <RefreshCw className={cn("h-4 w-4", refreshing && "animate-spin")} />刷新
            </button>
          </div>
        </header>

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
            <Summary title="当前可用来源" value={`${sourceSummary.live}/${state.sources.length}`} description="最近一次有效探测仍在 TTL 内" tone={sourceSummary.live ? "good" : "muted"} icon={CheckCircle2} />
            <Summary title="当前降级" value={String(sourceSummary.degraded)} description="故障、缺依赖或无覆盖分别标记" tone={sourceSummary.degraded ? "warn" : "good"} icon={sourceSummary.degraded ? AlertTriangle : ServerCrash} />
            <Summary title="状态已过期" value={String(sourceSummary.stale)} description="市场源 30 分钟、研究源 6 小时" tone={sourceSummary.stale ? "warn" : "muted"} icon={RefreshCw} />
            <Summary title="存储使用" value={formatBytes(state.totalBytes)} description={`${storagePercent.toFixed(1)}% / ${formatBytes(state.softLimitBytes)}`} tone={storagePercent >= 90 ? "warn" : "muted"} icon={Database} />
          </section>
        )}

        <section className="rounded-md border p-4">
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
        </section>

        <section className="grid gap-5 xl:grid-cols-[0.85fr_1.15fr]">
          <TableCard title="K 线缓存覆盖" description="按标的、周期、复权口径和实际适配器统计。">
            <CoverageTable rows={state.coverage} />
          </TableCard>
          <TableCard title="数据源健康度" description="上游指纹用于判断来源是否真正独立；错误类别用于区分联通、依赖和覆盖问题。">
            <SourcesTable rows={state.sources} />
          </TableCard>
        </section>

        <section className="rounded-md border p-4">
          <h2 className="font-semibold">存储与保留</h2>
          <div className="mt-3 h-2 overflow-hidden rounded-full bg-muted" aria-label={`存储使用 ${storagePercent.toFixed(1)}%`}>
            <div className={cn("h-full rounded-full", storagePercent >= 90 ? "bg-amber-500" : "bg-primary")} style={{ width: `${storagePercent}%` }} />
          </div>
          <div className="mt-4 grid gap-2 text-sm sm:grid-cols-3">
            {state.storage.map((entry) => <div key={entry.kind} className="rounded-md border p-3"><div className="font-medium">{entry.kind}</div><div className="mt-1 text-muted-foreground">{formatBytes(entry.bytes)}</div></div>)}
          </div>
        </section>
      </div>
    </div>
  );
}

function Summary({ title, value, description, tone, icon: Icon }: { title: string; value: string; description: string; tone: "good" | "warn" | "muted"; icon: typeof Database }) {
  return <article className="rounded-md border p-4"><div className="flex items-center justify-between gap-3"><span className="text-xs font-medium uppercase text-muted-foreground">{title}</span><Icon className={cn("h-4 w-4", tone === "good" && "text-emerald-600", tone === "warn" && "text-amber-600", tone === "muted" && "text-muted-foreground")} /></div><div className="mt-3 text-2xl font-semibold">{value}</div><p className="mt-1 text-xs text-muted-foreground">{description}</p></article>;
}

function TableCard({ title, description, children }: { title: string; description: string; children: ReactNode }) {
  return <section className="rounded-md border p-4"><h2 className="font-semibold">{title}</h2><p className="mt-1 text-sm text-muted-foreground">{description}</p><div className="mt-4 overflow-x-auto">{children}</div></section>;
}

function CoverageTable({ rows }: { rows: CoverageRow[] }) {
  if (!rows.length) return <p className="py-5 text-sm text-muted-foreground">暂无 K 线缓存。</p>;
  return <table className="w-full min-w-[530px] text-left text-sm"><thead className="border-b text-xs text-muted-foreground"><tr><th className="pb-2 font-medium">标的</th><th className="pb-2 font-medium">周期</th><th className="pb-2 font-medium">复权</th><th className="pb-2 font-medium">范围</th><th className="pb-2 font-medium">适配器</th></tr></thead><tbody>{rows.map((row) => <tr key={`${row.symbol}-${row.actual_source}-${row.interval}-${row.actual_adjustment}`} className="border-b last:border-0"><td className="py-2 font-mono">{row.symbol}</td><td className="py-2">{row.interval}</td><td className="py-2">{row.actual_adjustment}</td><td className="py-2 text-xs text-muted-foreground">{shortDate(row.min_bar_time)} → {shortDate(row.max_bar_time)}</td><td className="py-2">{row.actual_source}</td></tr>)}</tbody></table>;
}

function SourcesTable({ rows }: { rows: SourceHealth[] }) {
  if (!rows.length) return <p className="py-5 text-sm text-muted-foreground">尚无数据源探测记录。</p>;
  return (
    <table className="w-full min-w-[900px] text-left text-sm">
      <thead className="border-b text-xs text-muted-foreground"><tr><th className="pb-2 font-medium">请求 / 能力</th><th className="pb-2 font-medium">实际 / 上游</th><th className="pb-2 font-medium">状态</th><th className="pb-2 font-medium">最近尝试</th><th className="pb-2 font-medium">延迟</th><th className="pb-2 font-medium">具体原因</th></tr></thead>
      <tbody>{rows.map((row) => (
        <tr key={row.source} className="border-b align-top last:border-0">
          <td className="py-2"><div className="font-medium">{row.requested_source}</div><div className="text-xs text-muted-foreground">{row.capability}</div></td>
          <td className="py-2"><div>{row.actual_source || "-"}</div><div className="font-mono text-xs text-muted-foreground">{row.upstream_source || "-"}</div></td>
          <td className="py-2"><SourceStatus row={row} /></td>
          <td className="py-2 text-xs text-muted-foreground">{shortDate(row.updated_at)}</td>
          <td className="py-2">{row.last_latency_ms == null ? "-" : `${row.last_latency_ms.toFixed(0)} ms`}</td>
          <td className="max-w-[320px] py-2"><div className="text-xs font-medium">{errorCategoryLabel(row.error_category)}</div><div className="mt-0.5 break-words text-xs text-muted-foreground">{row.last_error || "-"}</div></td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function SourceStatus({ row }: { row: SourceHealth }) {
  const status = row.circuit_open ? "circuit_open" : row.effective_status;
  const good = ["ok", "ok_with_transport_fallback"].includes(status);
  const warning = ["stale", "degraded", "no_coverage", "basis_mismatch", "duplicate_upstream", "ok_with_transport_fallback"].includes(status);
  const labels: Record<string, string> = {
    ok: "可用",
    ok_with_transport_fallback: "可用（已切换传输）",
    degraded: "部分请求降级",
    stale: "状态已过期",
    no_coverage: "无该标的覆盖",
    basis_mismatch: "复权口径不符",
    duplicate_upstream: "重复上游",
    dependency_missing: "依赖缺失",
    failed: "请求失败",
    circuit_open: "熔断冷却",
  };
  return <span className={cn("rounded-full px-2 py-0.5 text-xs", good && !warning && "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300", warning && "bg-amber-500/15 text-amber-700 dark:text-amber-300", !good && !warning && "bg-destructive/10 text-destructive")}>{labels[status] || status}</span>;
}

function errorCategoryLabel(value: string | null): string {
  const labels: Record<string, string> = {
    transport_error: "网络/传输故障",
    transport_fallback: "requests 失败，urllib 成功",
    rate_limited: "提供方限流",
    dependency_missing: "运行依赖缺失",
    no_coverage: "提供方无覆盖",
    basis_mismatch: "复权口径不兼容",
    duplicate_upstream: "非独立来源",
    provider_error: "提供方错误",
  };
  return value ? labels[value] || value : "正常";
}

function shortDate(value: string): string { return value ? value.replace("T", " ").slice(0, 16) : "-"; }
function formatBytes(bytes: number): string { if (bytes < 1024) return `${bytes} B`; const units = ["KB", "MB", "GB", "TB"]; let value = bytes / 1024; let index = 0; while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; } return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[index]}`; }
