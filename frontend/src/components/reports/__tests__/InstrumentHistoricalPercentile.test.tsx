import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InstrumentHistoricalPercentile } from "@/components/reports/InstrumentHistoricalPercentile";
import type { InstrumentHistoricalPercentileSnapshot } from "@/lib/api";


function snapshot(
  overrides: Partial<InstrumentHistoricalPercentileSnapshot> = {},
): InstrumentHistoricalPercentileSnapshot {
  return {
    schema_version: 2,
    snapshot_id: "historicalpct-test",
    symbol: "600036.SH",
    instrument_type: "company_equity",
    instrument_name: "招商银行",
    valuation_basis: "company_valuation",
    scope_label: "招商银行 · 公司估值",
    status: "available",
    lookback_years: 10,
    sample_start: "2016-07-19",
    sample_end: "2026-07-17",
    sample_count: 2427,
    data_as_of: "2026-07-17T00:00:00+08:00",
    retrieved_at: "2026-07-19T12:00:00+00:00",
    mapping_method: "symbol_exact_baostock_daily_valuation",
    percentile_method: "strict_lower_empirical_cdf",
    metrics: [{
      key: "pb_mrq",
      label: "PB · 市净率",
      value: 0.846774,
      unit: "multiple",
      percentile: 12.34,
      temperature: "偏冷",
      observation_count: 2427,
      sample_start: "2016-07-19",
      sample_end: "2026-07-17",
    }],
    source: {
      source_id: "baostock-test",
      provider_id: "baostock_daily_valuation",
      label: "BaoStock · 历史估值",
      publisher: "BaoStock",
      verification_status: "public_secondary",
      url: "https://www.baostock.com/",
      methodology_url: "https://www.baostock.com/mainContent?file=pythonAPI.md",
      retrieved_at: "2026-07-19T12:00:00+00:00",
    },
    unavailable_reason: null,
    warnings: ["PE 只使用正值样本。"],
    history_count: 1,
    ...overrides,
  };
}


describe("InstrumentHistoricalPercentile", () => {
  it("renders a company valuation percentile with its real sample window", () => {
    render(<InstrumentHistoricalPercentile snapshot={snapshot()} />);

    expect(screen.getByText("公司估值历史分位")).toBeInTheDocument();
    expect(screen.getByText("招商银行 · 公司估值")).toBeInTheDocument();
    expect(screen.getByText("0.85 倍")).toBeInTheDocument();
    expect(screen.getByText("12.3%")).toBeInTheDocument();
    expect(screen.getByText(/2,427 个有效交易日/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /BaoStock · 历史估值/ })).toHaveAttribute(
      "href",
      "https://www.baostock.com/",
    );
  });

  it("labels the overseas fallback as price history rather than valuation", () => {
    render(<InstrumentHistoricalPercentile snapshot={snapshot({
      symbol: "AAPL.US",
      instrument_name: "Apple Inc.",
      valuation_basis: "adjusted_price_history",
      scope_label: "Apple Inc. · 价格位置（非估值）",
      metrics: [{
        key: "adjusted_close",
        label: "复权收盘价",
        value: 333.74,
        unit: "USD",
        percentile: 99.1,
        temperature: "极高",
        observation_count: 2514,
      }],
      warnings: ["展示复权价格分位，不把它冒充 PE/PB/PS 估值分位。"],
    })} />);

    expect(screen.getByText("价格历史分位（非估值）")).toBeInTheDocument();
    expect(screen.getByText("333.74 USD")).toBeInTheDocument();
    expect(screen.getByText(/不把它冒充 PE\/PB\/PS/)).toBeInTheDocument();
  });
});
