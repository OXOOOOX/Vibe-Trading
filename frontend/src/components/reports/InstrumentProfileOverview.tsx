import { AlertTriangle, Database, ExternalLink, Loader2, RefreshCw } from "lucide-react";

import { InstrumentHistoricalPercentile } from "@/components/reports/InstrumentHistoricalPercentile";
import type {
  InstrumentProfileMetric,
  InstrumentProfileSnapshot,
  InstrumentHistoricalPercentileSnapshot,
} from "@/lib/api";


const CATEGORY_LABELS: Record<string, string> = {
  market: "行情",
  scale: "规模与股份",
  valuation: "估值",
  profitability: "盈利质量",
  dividend: "分红",
};

const CATEGORY_ORDER = ["market", "scale", "valuation", "profitability", "dividend"];


export function InstrumentProfileOverview({
  snapshot,
  isEtf,
  historicalPercentile,
  refreshing,
  onRefresh,
}: {
  snapshot?: InstrumentProfileSnapshot | null;
  isEtf?: boolean;
  historicalPercentile?: InstrumentHistoricalPercentileSnapshot | null;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  const identity = snapshot?.identity;
  const grouped = new Map<string, InstrumentProfileMetric[]>();
  for (const metric of snapshot?.metrics || []) {
    const current = grouped.get(metric.category) || [];
    current.push(metric);
    grouped.set(metric.category, current);
  }

  return (
    <section aria-label="标的关键资料" className="rounded-lg border bg-card p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <Database className="h-4 w-4 text-primary" />
            <h3 className="font-semibold">标的概览</h3>
            {snapshot || isEtf ? (
              <span className="rounded border px-2 py-0.5 text-[11px] text-muted-foreground">
                {isEtf || snapshot?.instrument_type === "etf"
                  ? "ETF"
                  : snapshot?.instrument_type === "index" ? "指数" : "股票"}
              </span>
            ) : null}
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            统一归档行情、规模、估值与分红口径；每次更新都会保留一份历史快照。
          </p>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={refreshing}
          className="inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm font-medium hover:bg-muted disabled:opacity-50"
        >
          {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          {snapshot ? "更新标的资料" : "获取标的资料"}
        </button>
      </div>

      {!snapshot ? (
        <div className="mt-4 rounded-md border border-dashed p-5 text-sm text-muted-foreground">
          尚未生成标的快照。点击“获取标的资料”后，会从报告层统一数据源读取并归档；打开页面本身不会静默联网。
        </div>
      ) : (
        <>
          <div className="mt-4 flex flex-wrap gap-x-5 gap-y-2 rounded-md bg-muted/40 p-3 text-sm">
            <span className="font-medium">{identity?.name || snapshot.symbol}</span>
            <span className="font-mono text-muted-foreground">{snapshot.symbol}</span>
            {identity?.industry ? <span>行业：{identity.industry}</span> : null}
            {identity?.region ? <span>地区：{identity.region}</span> : null}
            {identity?.listing_date ? <span>上市：{formatDate(identity.listing_date)}</span> : null}
          </div>

          {identity?.concepts?.length ? (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {identity.concepts.map((concept) => (
                <span key={concept} className="rounded bg-primary/5 px-2 py-1 text-xs text-primary">
                  {concept}
                </span>
              ))}
            </div>
          ) : null}

          <div className="mt-5 space-y-5">
            {CATEGORY_ORDER.map((category) => {
              const metrics = grouped.get(category) || [];
              if (!metrics.length) return null;
              return (
                <div key={category}>
                  <h4 className="text-sm font-semibold">{CATEGORY_LABELS[category] || category}</h4>
                  <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                    {metrics.map((metric) => (
                      <MetricCard key={metric.key} metric={metric} />
                    ))}
                  </div>
                </div>
              );
            })}
          </div>

          {snapshot.warnings.length ? (
            <div className="mt-4 space-y-1 rounded-md border border-amber-500/20 bg-amber-500/5 p-3 text-xs text-amber-800 dark:text-amber-200">
              {snapshot.warnings.map((warning) => (
                <div key={warning} className="flex items-start gap-2">
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  <span>{warning}</span>
                </div>
              ))}
            </div>
          ) : null}

          <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 border-t pt-3 text-xs text-muted-foreground">
            <span>数据截至 {formatDateTime(snapshot.data_as_of)}</span>
            <span>归档于 {formatDateTime(snapshot.retrieved_at)}</span>
            <span>{snapshot.history_count} 份快照</span>
            {snapshot.sources.map((source) => source.url ? (
              <a
                key={source.source_id}
                href={source.url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 hover:text-foreground"
              >
                {source.label}<ExternalLink className="h-3 w-3" />
              </a>
            ) : <span key={source.source_id}>{source.label}</span>)}
          </div>
        </>
      )}
      <InstrumentHistoricalPercentile
        snapshot={historicalPercentile}
        fallbackInstrumentType={
          isEtf || snapshot?.instrument_type === "etf"
            ? "etf"
            : snapshot?.instrument_type === "index" ? "index" : "company_equity"
        }
      />
    </section>
  );
}


function MetricCard({ metric }: { metric: InstrumentProfileMetric }) {
  const unavailable = metric.status !== "available" || metric.value === null;
  return (
    <div
      className="rounded-md border bg-background/60 p-3"
      title={unavailable ? metric.unavailable_reason || undefined : metric.semantics}
    >
      <div className="text-xs text-muted-foreground">{metric.label}</div>
      <div className={`mt-1 text-lg font-semibold ${unavailable ? "text-muted-foreground" : ""}`}>
        {unavailable ? "暂缺" : formatMetric(metric)}
      </div>
      <div className="mt-1 text-[11px] text-muted-foreground">
        {unavailable ? metric.unavailable_reason : `截至 ${formatDate(metric.data_as_of)}`}
      </div>
    </div>
  );
}


function formatMetric(metric: InstrumentProfileMetric): string {
  const value = metric.value;
  if (value === null || !Number.isFinite(value)) return "暂缺";
  if (metric.unit === "ratio") return `${(value * 100).toFixed(2)}%`;
  if (metric.unit === "pct") return `${value.toFixed(2)}%`;
  if (metric.unit === "multiple") return `${value.toFixed(2)} 倍`;
  if (metric.unit === "CNY_per_10_shares") return `${value.toFixed(2)} 元`;
  if (metric.unit === "CNY_per_fund_unit") return `${value.toFixed(4)} 元/份`;
  if (metric.unit === "shares" || metric.unit === "fund_units") {
    return formatLargeNumber(value, metric.unit === "fund_units" ? "份" : "股");
  }
  if (["CNY", "USD", "HKD"].includes(metric.unit)) {
    const currency = currencyLabel(metric.unit);
    if (metric.key.includes("market_cap")) return formatLargeNumber(value, currency);
    return `${value.toLocaleString("zh-CN", { maximumFractionDigits: 3 })} ${currency}`;
  }
  if (metric.unit.endsWith("_per_share")) {
    return `${value.toFixed(3)} ${currencyLabel(metric.unit.replace("_per_share", ""))}/股`;
  }
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
}


function currencyLabel(currency: string): string {
  if (currency === "USD") return "美元";
  if (currency === "HKD") return "港元";
  return "元";
}


function formatLargeNumber(value: number, unit: string): string {
  if (Math.abs(value) >= 100_000_000) return `${(value / 100_000_000).toFixed(2)} 亿${unit}`;
  if (Math.abs(value) >= 10_000) return `${(value / 10_000).toFixed(2)} 万${unit}`;
  return `${value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })} ${unit}`;
}


function formatDate(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value.slice(0, 10);
  return parsed.toLocaleDateString("zh-CN");
}


function formatDateTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("zh-CN", { hour12: false });
}
