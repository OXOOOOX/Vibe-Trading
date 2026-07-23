import { useState } from "react";
import { ChevronDown, ChevronUp, ExternalLink, Layers3 } from "lucide-react";

import type { ETFUniverseProfile } from "@/lib/api";


const PERCENT_FORMAT = new Intl.NumberFormat("zh-CN", {
  style: "percent",
  minimumFractionDigits: 2,
  maximumFractionDigits: 3,
});

function formatPercent(value: number) {
  return Number.isFinite(value) ? PERCENT_FORMAT.format(value) : "—";
}

function formatDate(value?: string | null) {
  if (!value) return "—";
  return value.split("T", 1)[0];
}

function sourceLabel(sourceType: string) {
  if (sourceType === "official_index_weight") return "官方指数权重";
  if (sourceType === "index_weight") return "指数权重";
  if (sourceType === "quarterly_fund_holdings") return "基金定期持仓";
  return "成分权重快照";
}

function DefinitionItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border bg-muted/20 px-3 py-2.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-1 text-sm font-medium">{value}</dd>
    </div>
  );
}

export function ETFConstituentWeights({ universe }: { universe: ETFUniverseProfile }) {
  const [showAll, setShowAll] = useState(false);
  const components = universe.components || [];
  const visibleComponents = showAll ? components : components.slice(0, 10);
  const hasMore = components.length > 10;
  const isIndexWeight = universe.weight_semantics === "tracked_index_weight";
  const indexLabel = [universe.tracked_index_name, universe.tracked_index_code]
    .filter(Boolean)
    .join(" · ");

  return (
    <section aria-label="ETF成分股权重" className="overflow-hidden rounded-lg border bg-card">
      <div className="border-b px-5 py-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex min-w-0 items-start gap-3">
            <span className="mt-0.5 rounded-md bg-primary/10 p-2 text-primary">
              <Layers3 className="h-4 w-4" aria-hidden="true" />
            </span>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="font-semibold">ETF 成分股权重</h3>
                <span className="rounded-full border px-2 py-0.5 text-[11px] text-muted-foreground">
                  {isIndexWeight ? "跟踪指数口径" : "基金披露口径"}
                </span>
              </div>
              <p className="mt-1 text-sm text-muted-foreground">
                {indexLabel || universe.etf_name || universe.etf_symbol}
              </p>
            </div>
          </div>
          {universe.source_urls[0] ? (
            <a
              href={universe.source_urls[0]}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
            >
              查看数据来源 <ExternalLink className="h-3 w-3" aria-hidden="true" />
            </a>
          ) : null}
        </div>

        <dl className="mt-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          <DefinitionItem label="数据口径" value={sourceLabel(universe.source_type)} />
          <DefinitionItem
            label="成分覆盖"
            value={`${universe.observed_component_count} / ${universe.expected_component_count || universe.observed_component_count}`}
          />
          <DefinitionItem label="权重覆盖" value={formatPercent(universe.observed_weight_coverage)} />
          <DefinitionItem label="数据截至" value={formatDate(universe.data_as_of)} />
        </dl>

        {!universe.universe_complete ? (
          <div className="mt-3 rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-800 dark:text-amber-200">
            当前只展示已披露的部分成分，不能视为完整 ETF 成分结构。
          </div>
        ) : null}
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[520px] text-sm">
          <caption className="sr-only">{universe.etf_symbol} 成分股权重</caption>
          <thead className="bg-muted/30 text-left text-xs text-muted-foreground">
            <tr>
              <th scope="col" className="w-16 px-5 py-2.5 font-medium">排名</th>
              <th scope="col" className="px-3 py-2.5 font-medium">成分股</th>
              <th scope="col" className="px-3 py-2.5 font-medium">代码</th>
              <th scope="col" className="px-5 py-2.5 text-right font-medium">权重</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {visibleComponents.map((component, index) => (
              <tr key={component.symbol} className="hover:bg-muted/20">
                <td className="px-5 py-2.5 font-mono text-xs text-muted-foreground">{index + 1}</td>
                <td className="px-3 py-2.5 font-medium">{component.name || component.symbol}</td>
                <td className="px-3 py-2.5 font-mono text-xs text-muted-foreground">{component.symbol}</td>
                <td className="px-5 py-2.5 text-right font-mono font-medium tabular-nums">
                  {formatPercent(component.weight)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {hasMore ? (
        <div className="border-t bg-muted/10 px-5 py-3 text-center">
          <button
            type="button"
            onClick={() => setShowAll((value) => !value)}
            className="inline-flex items-center gap-1.5 text-sm text-primary hover:underline"
          >
            {showAll ? (
              <>
                收起至前 10 只成分股 <ChevronUp className="h-4 w-4" aria-hidden="true" />
              </>
            ) : (
              <>
                查看全部 {components.length} 只成分股 <ChevronDown className="h-4 w-4" aria-hidden="true" />
              </>
            )}
          </button>
        </div>
      ) : null}
    </section>
  );
}
