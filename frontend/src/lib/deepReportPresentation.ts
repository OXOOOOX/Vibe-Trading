import type { ETFReportReadiness, ETFReadinessStatus } from "@/lib/api";

export const DEEP_REPORT_MODULE_LABELS: Record<string, string> = {
  executive_summary: "核心结论",
  business_position: "公司业务与产业位置",
  financial_quality: "三张报表与财务质量",
  accounting_review: "会计异常核查",
  implied_expectations: "市值隐含预期",
  terminal_narrative: "长期经营情景与叙事阶段",
  terminal_scenarios: "长期经营情景",
  counter_thesis: "反方、风险与催化剂",
  conclusion_watchlist: "结论与跟踪框架",
  report_gate: "整份报告门控",
  market_data: "市场数据",
  symbol_identity: "证券身份",
  identity: "ETF 与指数身份",
  product_profile: "ETF 产品画像",
  universe: "持仓权重覆盖",
  peer_flow: "同指数 ETF 份额跟踪",
  latest_quarter: "最新季度",
  index_and_product: "指数与产品",
  index_product: "指数与产品",
  exposure_structure: "暴露结构",
  aggregate_fundamentals: "聚合基本面",
  price_volume_structure: "量价结构",
  flow_liquidity_tracking: "份额、流动性与跟踪",
  liquidity_tracking: "份额、流动性与跟踪",
  holding_penetration: "关键持仓穿透",
  holding_selection: "关键持仓选择",
  component_research: "成分研究覆盖",
};

export function deepReportModuleLabel(moduleId: string): string {
  return DEEP_REPORT_MODULE_LABELS[moduleId] || moduleId;
}

export function normalizedEtfReadiness(
  profile: string | undefined,
  qualityStatus: string | undefined,
  readiness: ETFReportReadiness | undefined,
): ETFReadinessStatus | undefined {
  if (profile !== "etf_deep_research" && !readiness) return undefined;
  if (qualityStatus === "failed_validation") return "not_publishable";
  return readiness?.status || "structure_ready";
}

export function deepReportTypeLabel(
  profile: string | undefined,
  qualityStatus: string | undefined,
  readiness: ETFReportReadiness | undefined,
): string {
  const status = normalizedEtfReadiness(profile, qualityStatus, readiness);
  if (!status) return "穿透式深度研究";
  return {
    not_publishable: "ETF 研究诊断草稿",
    structure_ready: "ETF 结构研究",
    penetration_partial: "ETF 穿透研究（部分覆盖）",
    penetration_ready: "ETF 穿透式深度研究",
  }[status];
}

export function deepReportTitle(
  subject: string | undefined,
  symbol: string | undefined,
  profile: string | undefined,
  qualityStatus: string | undefined,
  readiness: ETFReportReadiness | undefined,
): string {
  const identity = subject || symbol || (profile === "etf_deep_research" ? "ETF" : "单股");
  const symbolText = symbol ? `（${symbol}）` : "";
  return `${identity}${symbolText}${deepReportTypeLabel(profile, qualityStatus, readiness)}`;
}

export function etfReadinessMessage(readiness: ETFReportReadiness | undefined): string {
  const status = readiness?.status || "structure_ready";
  return {
    not_publishable: "报告未通过正式发布审查",
    structure_ready: "结构报告已生成，尚未完成成分穿透",
    penetration_partial: "已完成部分成分穿透，仍有研究缺口",
    penetration_ready: "穿透式深度研究已完成",
  }[status];
}
