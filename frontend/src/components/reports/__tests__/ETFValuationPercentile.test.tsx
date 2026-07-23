import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ETFValuationPercentile } from "@/components/reports/ETFValuationPercentile";
import type { ETFValuationPercentileSnapshot } from "@/lib/api";


const source = {
  source_id: "csindex-test",
  provider_id: "csindex_official_history",
  label: "中证指数 · 历史估值",
  publisher: "中证指数有限公司",
  verification_status: "official_primary",
  url: "https://www.csindex.com.cn/example",
  methodology_url: "https://www.csindex.com.cn/example",
  retrieved_at: "2026-07-19T12:00:00+00:00",
};


function snapshot(
  overrides: Partial<ETFValuationPercentileSnapshot> = {},
): ETFValuationPercentileSnapshot {
  return {
    schema_version: 1,
    snapshot_id: "etfvaluation-test",
    symbol: "512890.SH",
    tracked_index_code: "H30269",
    tracked_index_name: "中证红利低波动指数",
    status: "available",
    lookback_years: 10,
    data_as_of: "2026-07-17T00:00:00+08:00",
    retrieved_at: "2026-07-19T12:00:00+00:00",
    mapping_method: "tracked_index_code_exact_csindex",
    metrics: [{
      key: "pe",
      label: "PE · 滚动市盈率",
      value: 7.55,
      percentile: 65.0062,
      temperature: "正常",
    }],
    source,
    unavailable_reason: null,
    warnings: ["中证历史接口当前只提供滚动市盈率，PB、PS 百分位暂不补造。"],
    history_count: 1,
    ...overrides,
  };
}


describe("ETFValuationPercentile", () => {
  it("shows an official PE fallback without inventing PB or PS", () => {
    render(<ETFValuationPercentile snapshot={snapshot()} />);

    expect(screen.getByText("中证红利低波动指数 · H30269")).toBeInTheDocument();
    expect(screen.getByText("65.0%")).toBeInTheDocument();
    expect(screen.getByText(/PB、PS 百分位暂不补造/)).toBeInTheDocument();
    expect(screen.queryByText("PB · 市净率")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /中证指数 · 历史估值/ })).toHaveAttribute(
      "href",
      "https://www.csindex.com.cn/example",
    );
  });

  it("keeps an explicit not-covered state for every ETF dossier", () => {
    render(<ETFValuationPercentile snapshot={snapshot({
      status: "unavailable",
      metrics: [],
      unavailable_reason: "当前百分位数据源尚未覆盖跟踪指数测试指数（999999）。",
    })} />);

    expect(screen.getByText("暂未覆盖")).toBeInTheDocument();
    expect(screen.getByText(/测试指数（999999）/)).toBeInTheDocument();
  });
});
