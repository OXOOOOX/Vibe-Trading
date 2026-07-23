import { ExternalLink, Gauge } from "lucide-react";

import type {
  InstrumentHistoricalPercentileMetric,
  InstrumentHistoricalPercentileSnapshot,
} from "@/lib/api";


const TONE_STYLES: Record<string, {
  accent: string;
  badge: string;
  fill: string;
}> = {
  极冷: {
    accent: "border-t-teal-600",
    badge: "bg-teal-500/10 text-teal-700 dark:text-teal-300",
    fill: "bg-teal-600",
  },
  极低: {
    accent: "border-t-teal-600",
    badge: "bg-teal-500/10 text-teal-700 dark:text-teal-300",
    fill: "bg-teal-600",
  },
  偏冷: {
    accent: "border-t-emerald-600",
    badge: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
    fill: "bg-emerald-600",
  },
  偏低: {
    accent: "border-t-emerald-600",
    badge: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
    fill: "bg-emerald-600",
  },
  正常: {
    accent: "border-t-slate-500",
    badge: "bg-slate-500/10 text-slate-700 dark:text-slate-300",
    fill: "bg-slate-500",
  },
  中位: {
    accent: "border-t-slate-500",
    badge: "bg-slate-500/10 text-slate-700 dark:text-slate-300",
    fill: "bg-slate-500",
  },
  偏热: {
    accent: "border-t-amber-600",
    badge: "bg-amber-500/10 text-amber-700 dark:text-amber-300",
    fill: "bg-amber-600",
  },
  偏高: {
    accent: "border-t-amber-600",
    badge: "bg-amber-500/10 text-amber-700 dark:text-amber-300",
    fill: "bg-amber-600",
  },
  极热: {
    accent: "border-t-red-600",
    badge: "bg-red-500/10 text-red-700 dark:text-red-300",
    fill: "bg-red-600",
  },
  极高: {
    accent: "border-t-red-600",
    badge: "bg-red-500/10 text-red-700 dark:text-red-300",
    fill: "bg-red-600",
  },
};

const DEFAULT_TONE = {
  accent: "border-t-muted-foreground",
  badge: "bg-muted text-muted-foreground",
  fill: "bg-muted-foreground",
};


export function InstrumentHistoricalPercentile({
  snapshot,
  fallbackInstrumentType,
}: {
  snapshot?: InstrumentHistoricalPercentileSnapshot | null;
  fallbackInstrumentType?: "etf" | "index" | "company_equity";
}) {
  const title = percentileTitle(snapshot, fallbackInstrumentType);
  if (!snapshot) {
    return (
      <div className="mt-5 rounded-md border border-dashed p-4">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <Gauge className="h-4 w-4 text-primary" />
          {title}
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          暂无历史分位快照。点击“更新标的资料”后，系统会按标的类型选择可比口径并归档；数据不足时会明确标记，不会补造指标。
        </p>
      </div>
    );
  }

  if (snapshot.status !== "available" || !snapshot.metrics.length) {
    return (
      <div className="mt-5 rounded-md border border-dashed p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <Gauge className="h-4 w-4 text-primary" />
            {title}
          </div>
          <span className="rounded bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">暂未覆盖</span>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          {snapshot.unavailable_reason || "当前没有达到样本门槛的可比历史数据。"}
        </p>
        <SourceFooter snapshot={snapshot} />
      </div>
    );
  }

  return (
    <div className="mt-5 rounded-md border bg-background/40 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <Gauge className="h-4 w-4 text-primary" />
            <h4 className="text-sm font-semibold">{title}</h4>
          </div>
          <p className="mt-1 text-xs text-muted-foreground">{scopeLabel(snapshot)}</p>
        </div>
        <span className="rounded border px-2 py-0.5 text-[11px] text-muted-foreground">
          {windowLabel(snapshot)}
        </span>
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {snapshot.metrics.map((metric) => (
          <PercentileCard
            key={metric.key}
            metric={metric}
            lookbackYears={snapshot.lookback_years}
          />
        ))}
      </div>

      <p className="mt-3 text-xs text-muted-foreground">
        百分位按同一标的、同一指标定义的历史样本排序；数值表示样本中低于当前值的交易日占比。
      </p>
      {snapshot.warnings.length ? (
        <div className="mt-3 space-y-1 rounded border border-amber-500/20 bg-amber-500/5 p-2.5 text-[11px] text-amber-800 dark:text-amber-200">
          {snapshot.warnings.map((warning) => <div key={warning}>{warning}</div>)}
        </div>
      ) : null}
      <SourceFooter snapshot={snapshot} />
    </div>
  );
}


function PercentileCard({
  metric,
  lookbackYears,
}: {
  metric: InstrumentHistoricalPercentileMetric;
  lookbackYears: number;
}) {
  const tone = TONE_STYLES[metric.temperature] || DEFAULT_TONE;
  const percentile = metric.percentile === null
    ? null
    : Math.min(100, Math.max(0, metric.percentile));
  return (
    <div className={`rounded-md border border-t-[3px] bg-card p-3 ${tone.accent}`}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-muted-foreground">{metric.label}</span>
        <span className={`rounded px-2 py-0.5 text-[11px] font-medium ${tone.badge}`}>
          {metric.temperature}
        </span>
      </div>
      <div className="mt-2 text-2xl font-semibold tabular-nums">
        {formatMetricValue(metric)}
      </div>
      <div className="mt-3 flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
        <span>
          {metric.observation_count ? "样本内百分位" : `近 ${lookbackYears} 年百分位`}
        </span>
        <strong className="text-foreground">
          {percentile === null ? "暂缺" : `${percentile.toFixed(1)}%`}
        </strong>
      </div>
      <div
        role="progressbar"
        aria-label={`${metric.label}近 ${lookbackYears} 年百分位`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percentile ?? undefined}
        className="mt-1.5 h-2 overflow-hidden rounded-full bg-muted"
      >
        <span
          aria-hidden="true"
          className={`block h-full rounded-full ${tone.fill}`}
          style={{ width: `${percentile ?? 0}%` }}
        />
      </div>
      {metric.observation_count ? (
        <div className="mt-2 text-[10px] text-muted-foreground">
          {metric.observation_count.toLocaleString("zh-CN")} 个有效交易日
          {metric.sample_start && metric.sample_end
            ? ` · ${formatDate(metric.sample_start)}—${formatDate(metric.sample_end)}`
            : ""}
        </div>
      ) : null}
    </div>
  );
}


function SourceFooter({
  snapshot,
}: {
  snapshot: InstrumentHistoricalPercentileSnapshot;
}) {
  return (
    <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 border-t pt-3 text-[11px] text-muted-foreground">
      <span>数据日 {formatDate(snapshot.data_as_of)}</span>
      <span>{snapshot.history_count} 份快照</span>
      <a
        href={snapshot.source.url}
        target="_blank"
        rel="noreferrer"
        className="inline-flex items-center gap-1 hover:text-foreground"
      >
        {snapshot.source.label}<ExternalLink className="h-3 w-3" />
      </a>
      {snapshot.source.methodology_url ? (
        <a
          href={snapshot.source.methodology_url}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 hover:text-foreground"
        >
          数据口径<ExternalLink className="h-3 w-3" />
        </a>
      ) : null}
    </div>
  );
}


function percentileTitle(
  snapshot?: InstrumentHistoricalPercentileSnapshot | null,
  fallbackInstrumentType?: "etf" | "index" | "company_equity",
): string {
  const basis = snapshot?.valuation_basis;
  if (basis === "adjusted_price_history") return "价格历史分位（非估值）";
  if (basis === "company_valuation") return "公司估值历史分位";
  if (basis === "index_valuation" || snapshot?.instrument_type === "index") return "指数估值历史分位";
  if (basis === "tracked_index_valuation" || snapshot?.tracked_index_code || fallbackInstrumentType === "etf") {
    return "跟踪指数估值百分位";
  }
  return "历史分位";
}


function scopeLabel(snapshot: InstrumentHistoricalPercentileSnapshot): string {
  if (snapshot.scope_label) {
    return snapshot.tracked_index_code && !snapshot.scope_label.includes(snapshot.tracked_index_code)
      ? `${snapshot.scope_label} · ${snapshot.tracked_index_code}`
      : snapshot.scope_label;
  }
  const name = snapshot.tracked_index_name || snapshot.instrument_name || snapshot.symbol;
  return snapshot.tracked_index_code ? `${name} · ${snapshot.tracked_index_code}` : name;
}


function windowLabel(snapshot: InstrumentHistoricalPercentileSnapshot): string {
  if (snapshot.sample_start && snapshot.sample_end) {
    return `${formatDate(snapshot.sample_start)}—${formatDate(snapshot.sample_end)}`;
  }
  return `近 ${snapshot.lookback_years} 年`;
}


function formatMetricValue(metric: InstrumentHistoricalPercentileMetric): string {
  if (metric.value === null || !Number.isFinite(metric.value)) return "暂缺";
  if (metric.unit === "multiple") return `${metric.value.toFixed(2)} 倍`;
  if (metric.unit && metric.unit !== "price") return `${metric.value.toFixed(2)} ${metric.unit}`;
  return metric.value.toFixed(2);
}


function formatDate(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value.slice(0, 10);
  return parsed.toLocaleDateString("zh-CN");
}
