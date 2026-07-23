import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { ChartCandlestick, Loader2, X } from "lucide-react";
import { api, type MarketCacheBar, type PriceBar, type VerifiedMarketCacheRow } from "@/lib/api";
import { cn } from "@/lib/utils";

const CachedCandlestickChart = lazy(async () => {
  const module = await import("@/components/charts/CandlestickChart");
  return { default: module.CandlestickChart };
});

const INTERVAL_ORDER = ["1D", "5m", "1m"];
const ADJUSTMENT_ORDER = ["qfq", "raw"];

interface Props {
  symbol: string;
  name?: string;
  cacheRows: VerifiedMarketCacheRow[];
  onClose: () => void;
}

function rowAdjustment(row: VerifiedMarketCacheRow): string {
  return String(row.actual_adjustment || row.requested_adjustment || "raw");
}

function uniqueSorted(values: string[], order: string[]): string[] {
  return [...new Set(values)].sort((a, b) => {
    const left = order.indexOf(a);
    const right = order.indexOf(b);
    return (left < 0 ? order.length : left) - (right < 0 ? order.length : right) || a.localeCompare(b);
  });
}

function initialVariant(rows: VerifiedMarketCacheRow[]): { interval: string; adjustment: string } {
  const variants = rows.map((row) => ({
    interval: String(row.interval || "1D"),
    adjustment: rowAdjustment(row),
  }));
  return [...variants].sort((a, b) => {
    const intervalDiff = INTERVAL_ORDER.indexOf(a.interval) - INTERVAL_ORDER.indexOf(b.interval);
    if (intervalDiff !== 0) return intervalDiff;
    return ADJUSTMENT_ORDER.indexOf(a.adjustment) - ADJUSTMENT_ORDER.indexOf(b.adjustment);
  })[0] || { interval: "1D", adjustment: "raw" };
}

function finiteBar(row: MarketCacheBar): row is MarketCacheBar & { open: number; high: number; low: number; close: number } {
  return [row.open, row.high, row.low, row.close].every((value) => typeof value === "number" && Number.isFinite(value));
}

function formatNumber(value: number | null | undefined, digits = 3): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString(undefined, { maximumFractionDigits: digits })
    : "-";
}

function formatDate(value: string | undefined): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function chartTime(row: MarketCacheBar, interval: string): string {
  if (interval === "1D") return row.session_date || row.bar_time.slice(0, 10);
  const date = new Date(row.bar_time);
  if (Number.isNaN(date.getTime())) return row.bar_time;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export default function MarketCacheKlineDialog({ symbol, name, cacheRows, onClose }: Props) {
  const first = useMemo(() => initialVariant(cacheRows), [cacheRows]);
  const [interval, setInterval] = useState(first.interval);
  const [adjustment, setAdjustment] = useState(first.adjustment);
  const [bars, setBars] = useState<MarketCacheBar[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const intervals = useMemo(
    () => uniqueSorted(cacheRows.map((row) => String(row.interval || "1D")), INTERVAL_ORDER),
    [cacheRows],
  );
  const adjustments = useMemo(
    () => uniqueSorted(
      cacheRows.filter((row) => String(row.interval || "1D") === interval).map(rowAdjustment),
      ADJUSTMENT_ORDER,
    ),
    [cacheRows, interval],
  );

  useEffect(() => {
    if (!adjustments.includes(adjustment)) setAdjustment(adjustments[0] || "raw");
  }, [adjustment, adjustments]);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [onClose]);

  useEffect(() => {
    if (!adjustments.includes(adjustment)) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    void api.getMarketCacheBars({ symbol, interval, adjustment, view: "consensus", limit: 20000 })
      .then((payload) => {
        if (!cancelled) setBars(payload.bars || []);
      })
      .catch((reason) => {
        if (!cancelled) {
          setBars([]);
          setError(reason instanceof Error ? reason.message : "读取缓存行情失败");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [symbol, interval, adjustment, adjustments]);

  const priceBars = useMemo<PriceBar[]>(
    () => bars.filter(finiteBar).map((row) => ({
      time: chartTime(row, interval),
      code: row.symbol,
      open: row.open,
      high: row.high,
      low: row.low,
      close: row.close,
      volume: typeof row.volume === "number" ? row.volume : 0,
    })),
    [bars, interval],
  );
  const latest = bars.length > 0 ? bars[bars.length - 1] : undefined;
  const displayName = name ? `${name}（${symbol.split(".")[0]}）` : symbol;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-0 backdrop-blur-sm sm:p-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="market-cache-chart-title"
        className="flex h-full w-full flex-col overflow-hidden border bg-background shadow-2xl sm:h-[min(90vh,820px)] sm:max-w-6xl sm:rounded-md"
      >
        <div className="flex items-start justify-between gap-3 border-b px-4 py-3 sm:px-5">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <ChartCandlestick className="h-4 w-4 shrink-0" />
              <h2 id="market-cache-chart-title" className="truncate text-sm font-semibold">
                {displayName} K线检阅
              </h2>
            </div>
            <p className="mt-1 truncate text-xs text-muted-foreground">
              {name ? symbol : "缓存行情"} · SQLite 共识数据
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
            title="关闭K线检阅"
            aria-label="关闭K线检阅"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex flex-wrap items-center gap-3 border-b px-4 py-3 sm:px-5">
          <div className="flex rounded-md border p-0.5" aria-label="K线周期">
            {intervals.map((value) => (
              <button
                key={value}
                type="button"
                onClick={() => setInterval(value)}
                className={cn(
                  "min-w-12 rounded px-3 py-1.5 font-mono text-xs",
                  interval === value ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
              >
                {value}
              </button>
            ))}
          </div>
          <div className="flex rounded-md border p-0.5" aria-label="复权口径">
            {adjustments.map((value) => (
              <button
                key={value}
                type="button"
                onClick={() => setAdjustment(value)}
                className={cn(
                  "min-w-14 rounded px-3 py-1.5 font-mono text-xs",
                  adjustment === value ? "bg-foreground text-background" : "text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
              >
                {value}
              </button>
            ))}
          </div>
          <span className="text-xs text-muted-foreground">共识视图 · {priceBars.length.toLocaleString()} 根</span>
        </div>

        <div className="grid grid-cols-2 gap-px border-b bg-border sm:grid-cols-5">
          <Metric label="最新收盘" value={formatNumber(latest?.close)} />
          <Metric label="最新状态" value={latest?.status ? statusLabel(latest.status) : "-"} />
          <Metric label="数据来源" value={(latest?.sources || []).join(", ") || "-"} />
          <Metric label="覆盖起点" value={formatDate(bars[0]?.bar_time)} />
          <Metric label="最新时间" value={formatDate(latest?.bar_time)} className="col-span-2 sm:col-span-1" />
        </div>

        <div className="min-h-0 flex-1 overflow-auto px-2 py-3 sm:px-5">
          {loading ? (
            <div className="flex h-full min-h-72 items-center justify-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              正在读取缓存K线
            </div>
          ) : error ? (
            <div className="flex h-full min-h-72 items-center justify-center text-sm text-destructive">{error}</div>
          ) : priceBars.length === 0 ? (
            <div className="flex h-full min-h-72 items-center justify-center text-sm text-muted-foreground">
              当前周期与复权口径没有可绘制的 OHLC 数据。
            </div>
          ) : (
            <Suspense fallback={<div className="h-[430px] animate-pulse rounded-md bg-muted/40 sm:h-[540px]" />}>
              <CachedCandlestickChart data={priceBars} height={window.innerWidth < 640 ? 430 : 540} />
            </Suspense>
          )}
        </div>
      </div>
    </div>
  );
}

function Metric({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div className={cn("min-w-0 bg-background px-4 py-2.5", className)}>
      <div className="text-[10px] text-muted-foreground">{label}</div>
      <div className="mt-0.5 truncate font-mono text-xs" title={value}>{value}</div>
    </div>
  );
}

function statusLabel(status: string): string {
  return ({
    verified: "已校核",
    single_source: "单一来源",
    source_lag: "来源延迟",
    provisional_mix: "盘中旧值已隔离",
    basis_mismatch: "复权口径不符",
    unresolved_conflict: "未解决冲突",
    stale: "已过期",
    conflict: "有冲突",
    unresolved: "未解析",
  } as Record<string, string>)[status] || status;
}
