import { describe, expect, it } from "vitest";

import {
  deepReportModuleLabel,
  deepReportTitle,
  deepReportTypeLabel,
} from "@/lib/deepReportPresentation";

describe("ETF deep report presentation", () => {
  it("does not label zero-coverage ETF research as penetration ready", () => {
    const readiness = {
      status: "structure_ready" as const,
      metrics: { component_research_coverage: 0 },
    };

    expect(deepReportTypeLabel("etf_deep_research", "passed_with_gaps", readiness))
      .toBe("ETF 结构研究");
    expect(deepReportTitle(
      "半导体设备ETF国泰",
      "159516.SZ",
      "etf_deep_research",
      "passed_with_gaps",
      readiness,
    )).toBe("半导体设备ETF国泰（159516.SZ）ETF 结构研究");
  });

  it("uses the complete penetration label only for penetration_ready", () => {
    expect(deepReportTypeLabel("etf_deep_research", "passed", {
      status: "penetration_ready",
    })).toBe("ETF 穿透式深度研究");
  });

  it("keeps backend and legacy module aliases on one shared label", () => {
    expect(deepReportModuleLabel("index_and_product")).toBe("指数与产品");
    expect(deepReportModuleLabel("index_product")).toBe("指数与产品");
    expect(deepReportModuleLabel("flow_liquidity_tracking")).toBe("份额、流动性与跟踪");
    expect(deepReportModuleLabel("liquidity_tracking")).toBe("份额、流动性与跟踪");
  });
});
