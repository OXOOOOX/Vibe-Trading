import {
  localizeToolName,
  localizeToolProgressMessage,
  localizeToolStage,
  TOOL_LABELS,
} from "../tools";

describe("TOOL_LABELS", () => {
  it("maps known tool names to user-facing labels", () => {
    expect(TOOL_LABELS["run_backtest"]).toBe("运行策略回测");
    expect(TOOL_LABELS["web_search"]).toBe("检索公开资料");
    expect(TOOL_LABELS["report_workspace"]).toBe("整理报告章节");
    expect(TOOL_LABELS["compact"]).toBe("整理对话上下文");
  });

  it("contains all trading connector tools", () => {
    const tradingKeys = Object.keys(TOOL_LABELS).filter((k) => k.startsWith("trading_"));
    expect(tradingKeys.length).toBeGreaterThanOrEqual(6);
  });
});

describe("localizeToolName", () => {
  it("returns label for known tools", () => {
    expect(localizeToolName("run_backtest")).toBe("运行策略回测");
  });

  it("returns fallback for unknown tools when fallback provided", () => {
    expect(localizeToolName("unknown_tool", "My Fallback")).toBe("My Fallback");
  });

  it("does not expose raw names for unknown tools", () => {
    expect(localizeToolName("some_new_tool")).toBe("执行研究步骤");
  });

  it("prefers TOOL_LABELS over fallback", () => {
    expect(localizeToolName("bash", "ignored")).toBe("执行数据处理");
  });
});

describe("user-facing progress language", () => {
  it("translates internal stages and hides raw request URLs", () => {
    expect(localizeToolStage("fetching")).toBe("获取资料");
    expect(localizeToolStage("financial_data")).toBe("处理中");
    expect(localizeToolProgressMessage("GET http://example.test/report", "read_url"))
      .toBe("正在读取所需资料");
  });
});
