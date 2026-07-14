import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import {
  api,
  type MarketCacheRun,
  type PortfolioAnalysisSession,
  type PortfolioMandate,
  type PortfolioReview,
} from "@/lib/api";
import { getMarketAnalysisLabel, getMarketAnalysisPhase, Portfolio } from "../Portfolio";

vi.mock("@/components/charts/CandlestickChart", () => ({
  CandlestickChart: ({ data }: { data: unknown[] }) => <div data-testid="candlestick-chart">{data.length} bars</div>,
}));

const review: PortfolioReview = {
  status: "ok",
  portfolio_path: "C:/portfolio.json",
  market_cache_db: "C:/market_cache.sqlite3",
  verified_cache_dir: "C:/verified",
  portfolio_state: {
    holdings: [{
      name: "科创50指",
      code: "588870",
      symbol: "588870.SH",
      quantity: 2100,
      cost_price: 1.975,
      last_price: 2.1,
      market_value: 4410,
      market_status: "verified",
      market_verified_at: "2026-07-10T06:00:00Z",
    }],
    recent_trades: [],
    cash: 1000,
    cash_currency: "CNY",
  },
  verified_market_cache: [{
    file_name: "588870.SH__1m__adj-raw.json",
    path: "sqlite://cache#588870.SH/1m/raw",
    symbol: "588870.SH",
    status: "verified",
    interval: "1m",
    actual_adjustment: "raw",
    consensus_close: 2.1,
    spread_pct: 0.02,
    volume: 10000,
    amount: 21000,
    source_count: 2,
    sources: ["eastmoney", "mootdx"],
    start_date: "2026-07-10T01:30:00Z",
    end_date: "2026-07-10T06:00:00Z",
    verified_at: "2026-07-10T06:00:00Z",
    observations: [{
      requested_source: "eastmoney",
      actual_source: "eastmoney",
      actual_adjustment: "raw",
      open: 2.09,
      high: 2.11,
      low: 2.08,
      close: 2.1,
      volume: 10000,
      volume_unit: "share",
      amount: 21000,
      vwap: 2.1,
      date: "2026-07-10T06:00:00Z",
      adjustment_confidence: "explicit_request",
      acquisition_mode: "network",
      included_in_consensus: true,
    }],
  }, {
    file_name: "588870.SH__1D__adj-qfq.json",
    path: "sqlite://cache#588870.SH/1D/qfq",
    symbol: "588870.SH",
    status: "single_source",
    interval: "1D",
    actual_adjustment: "qfq",
    consensus_close: 2.1,
    volume: 300000,
    amount: 630000,
    source_count: 1,
    sources: ["eastmoney"],
    start_date: "2024-01-02T07:00:00Z",
    end_date: "2026-07-10T07:00:00Z",
    verified_at: "2026-07-10T07:00:00Z",
    observations: [],
  }, {
    file_name: "510300.SH__1D__adj-raw.json",
    path: "sqlite://cache#510300.SH/1D/raw",
    symbol: "510300.SH",
    status: "verified",
    interval: "1D",
    actual_adjustment: "raw",
    consensus_close: 4.2,
    source_count: 2,
    sources: ["eastmoney", "tencent"],
    start_date: "2024-01-02T07:00:00Z",
    end_date: "2026-07-10T07:00:00Z",
    verified_at: "2026-07-10T07:00:00Z",
    observations: [],
  }],
};

const mandate: PortfolioMandate = {
  schema_version: 1,
  version: 2,
  suggestion_revision: 0,
  base_currency: "CNY",
  classification_policy: {},
  cash_policy: { configured: true, target_amount: 1000, min_amount: 500, max_amount: 2000 },
  sleeves: [
    {
      id: "offensive",
      name: "进攻型",
      configured: true,
      target_amount: 5000,
      min_amount: 3000,
      max_amount: 7000,
      rebalance_band_amount: 500,
      single_position_max_amount: null,
      sort_order: 10,
    },
    {
      id: "defensive",
      name: "防守型",
      configured: true,
      target_amount: 3000,
      min_amount: 2000,
      max_amount: 4000,
      rebalance_band_amount: 300,
      single_position_max_amount: null,
      sort_order: 20,
    },
  ],
  assignments: {
    "588870.SH": {
      active_sleeve_id: "offensive",
      assigned_by: "agent",
      confidence: 0.8,
      user_locked: false,
    },
  },
  classification_history: [],
  updated_at: "2026-07-13T09:00:00+08:00",
};

function makeRun(status: string): MarketCacheRun {
  const terminal = status === "completed";
  return {
    run_id: "run-12345678",
    profile: "portfolio_default",
    status,
    symbols: ["588870.SH"],
    config: {},
    total_items: 4,
    completed_items: terminal ? 4 : 0,
    conflict_items: 0,
    failed_items: 0,
    current_symbol: terminal ? null : "588870.SH",
    current_source: terminal ? null : "eastmoney",
    progress_pct: terminal ? 100 : 0,
    created_at: "2026-07-10T06:00:00Z",
    items: [{
      id: 1,
      run_id: "run-12345678",
      symbol: "588870.SH",
      interval: "1m",
      adjustment: "raw",
      status: terminal ? "verified" : "fetching",
      requested_sources: ["eastmoney", "mootdx"],
      actual_sources: terminal ? ["eastmoney", "mootdx"] : [],
      rows_written: terminal ? 240 : 0,
    }],
  };
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}{location.search}</output>;
}

describe("Portfolio market cache", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
    vi.spyOn(api, "getPortfolioReview").mockResolvedValue(review);
    vi.spyOn(api, "getPortfolioMandate").mockResolvedValue(mandate);
    vi.spyOn(api, "listPortfolioDailyRuns").mockResolvedValue({ runs: [] });
    vi.spyOn(api, "lookupPortfolioSecurity").mockResolvedValue({
      code: "588870",
      symbol: "588870.SH",
      name: "科创50ETF汇添富",
      market: "cn",
      source: "eastmoney",
    });
  });

  it("starts the one-click portfolio morning meeting", async () => {
    vi.spyOn(api, "startPortfolioDailyRun").mockResolvedValue({
      run_id: "dpr-1",
      market_date: "2026-07-13",
      status: "queued",
      stage: "queued",
      progress: { completed: 0, total: 1, percent: 0 },
      refresh_policy: "ensure_fresh",
      report_profile: "master_with_holding_appendices",
      created_at: "2026-07-13T09:00:00+08:00",
    });
    render(<Portfolio />, { wrapper: MemoryRouter });

    await userEvent.setup().click(await screen.findByRole("button", { name: "一键生成今日组合晨会" }));

    expect(api.startPortfolioDailyRun).toHaveBeenCalledWith({ refresh_policy: "ensure_fresh" });
    expect(await screen.findByText("2026-07-13 · queued")).toBeInTheDocument();
  });

  it("persists the visible AI allocation before starting when sleeves are unconfigured", async () => {
    const unconfiguredMandate: PortfolioMandate = {
      ...mandate,
      sleeves: mandate.sleeves.map((sleeve) => ({
        ...sleeve,
        configured: false,
        target_amount: 0,
        min_amount: 0,
        max_amount: null,
      })),
    };
    vi.mocked(api.getPortfolioMandate).mockResolvedValueOnce(unconfiguredMandate);
    const updateSpy = vi.spyOn(api, "updatePortfolioMandate").mockImplementation(async (next) => ({
      ...next,
      version: next.version + 1,
    }));
    const startSpy = vi.spyOn(api, "startPortfolioDailyRun").mockResolvedValue({
      run_id: "dpr-configured",
      market_date: "2026-07-14",
      status: "queued",
      stage: "queued",
      progress: { completed: 0, total: 1, percent: 0 },
      refresh_policy: "ensure_fresh",
      report_profile: "master_with_holding_appendices",
      created_at: "2026-07-14T09:00:00+08:00",
    });
    render(<Portfolio />, { wrapper: MemoryRouter });

    await userEvent.setup().click(await screen.findByRole("button", { name: "一键生成今日组合晨会" }));

    await waitFor(() => expect(startSpy).toHaveBeenCalledTimes(1));
    expect(updateSpy).toHaveBeenCalledTimes(1);
    expect(updateSpy.mock.invocationCallOrder[0]).toBeLessThan(startSpy.mock.invocationCallOrder[0]);
    const saved = updateSpy.mock.calls[0][0];
    expect(saved.sleeves.every((sleeve) => sleeve.configured)).toBe(true);
    expect(saved.sleeves.reduce((total, sleeve) => total + sleeve.target_amount, 0)).toBe(4910);
  });

  it("shows the hard data gate and retries without presenting a fake completed report", async () => {
    vi.mocked(api.listPortfolioDailyRuns).mockResolvedValue({
      runs: [{
        run_id: "dpr-skipped",
        market_date: "2026-07-14",
        status: "completed_with_warnings",
        stage: "skipped_data_unavailable",
        progress: { completed: 0, total: 11, percent: 0 },
        refresh_policy: "ensure_fresh",
        report_profile: "master_with_holding_appendices",
        data_status: "limited",
        analysis_gate: {
          decision: "skip_report",
          minimum_coverage_ratio: 0.5,
          coverage_ratio: 0,
          eligible_count: 0,
          total_count: 11,
          eligible_symbols: [],
          missing_symbols: ["588870.SH"],
          missing_market_symbols: ["588870.SH"],
          missing_research_symbols: ["588870.SH"],
          model_sessions_started: 0,
        },
        warnings: ["数据不足，已停止。"],
        artifacts: [],
        created_at: "2026-07-14T09:00:00+08:00",
      }],
    });
    vi.spyOn(api, "retryPortfolioDailyRun").mockResolvedValue({
      run_id: "dpr-retry",
      market_date: "2026-07-14",
      status: "queued",
      stage: "queued",
      progress: { completed: 0, total: 11, percent: 0 },
      refresh_policy: "ensure_fresh",
      report_profile: "master_with_holding_appendices",
      created_at: "2026-07-14T09:05:00+08:00",
    });
    render(<Portfolio />, { wrapper: MemoryRouter });

    expect(await screen.findByText("2026-07-14 · 已跳过（数据不足）")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "报告因数据不足已跳过" })).toHaveTextContent(
      "未启动个股研究模型 Session，也未生成 PDF",
    );
    expect(screen.queryByText("综合报告 PDF")).not.toBeInTheDocument();

    await userEvent.setup().click(screen.getByRole("button", { name: "数据恢复后重试" }));

    expect(api.retryPortfolioDailyRun).toHaveBeenCalledWith("dpr-skipped");
    expect(await screen.findByText("2026-07-14 · queued")).toBeInTheDocument();
  });

  it("retries one holding report into a new revision", async () => {
    vi.mocked(api.listPortfolioDailyRuns).mockResolvedValue({
      runs: [{
        run_id: "dpr-completed",
        market_date: "2026-07-14",
        status: "completed",
        stage: "completed",
        progress: { completed: 1, total: 1, percent: 100 },
        refresh_policy: "ensure_fresh",
        report_profile: "master_with_holding_appendices",
        artifacts: [{
          artifact_id: "holding-pdf",
          kind: "holding_daily_pdf",
          symbol: "588870.SH",
          filename: "holding.pdf",
          media_type: "application/pdf",
          size_bytes: 100,
          sha256: "abc",
          revision: 1,
        }],
        created_at: "2026-07-14T09:00:00+08:00",
      }],
    });
    vi.spyOn(api, "retryPortfolioDailyRun").mockResolvedValue({
      run_id: "dpr-revision-2",
      market_date: "2026-07-14",
      status: "queued",
      stage: "queued",
      progress: { completed: 0, total: 1, percent: 0 },
      refresh_policy: "ensure_fresh",
      report_profile: "master_with_holding_appendices",
      revision: 2,
      parent_run_id: "dpr-completed",
      retry_symbol: "588870.SH",
      created_at: "2026-07-14T09:05:00+08:00",
    });
    render(<Portfolio />, { wrapper: MemoryRouter });

    await userEvent.setup().click(
      await screen.findByRole("button", { name: "重试 588870.SH 个股日报" }),
    );

    expect(api.retryPortfolioDailyRun).toHaveBeenCalledWith("dpr-completed", "588870.SH");
    expect(await screen.findByText("2026-07-14 · queued")).toBeInTheDocument();
  });

  it("marks a legacy limited run as unusable and reruns it through the new gate", async () => {
    vi.mocked(api.listPortfolioDailyRuns).mockResolvedValue({
      runs: [{
        run_id: "dpr-legacy-limited",
        market_date: "2026-07-13",
        status: "completed",
        stage: "completed",
        progress: { completed: 11, total: 11, percent: 100 },
        refresh_policy: "ensure_fresh",
        report_profile: "master_with_holding_appendices",
        data_status: "limited",
        artifacts: [{
          artifact_id: "legacy-pdf",
          kind: "master_pdf",
          filename: "legacy.pdf",
          media_type: "application/pdf",
          size_bytes: 100,
          sha256: "abc",
        }],
        created_at: "2026-07-13T09:00:00+08:00",
      }],
    });
    vi.spyOn(api, "startPortfolioDailyRun").mockResolvedValue({
      run_id: "dpr-legacy-retry",
      market_date: "2026-07-14",
      status: "queued",
      stage: "queued",
      progress: { completed: 0, total: 11, percent: 0 },
      refresh_policy: "ensure_fresh",
      report_profile: "master_with_holding_appendices",
      created_at: "2026-07-14T09:05:00+08:00",
    });
    render(<Portfolio />, { wrapper: MemoryRouter });

    expect(await screen.findByText("2026-07-13 · 历史报告（数据受限）")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "历史报告数据受限" })).toHaveTextContent("PDF 已隐藏");
    expect(screen.queryByText("综合报告 PDF")).not.toBeInTheDocument();

    await userEvent.setup().click(screen.getByRole("button", { name: "按新门禁重新获取" }));

    expect(api.startPortfolioDailyRun).toHaveBeenCalledWith({
      refresh_policy: "ensure_fresh",
      force_new: true,
    });
  });

  it("updates current cash independently from the holdings import", async () => {
    const updatedReview: PortfolioReview = {
      ...review,
      portfolio_state: { ...review.portfolio_state, cash: 2500 },
    };
    vi.spyOn(api, "updatePortfolioCash").mockResolvedValue(updatedReview);
    const holdingsSpy = vi.spyOn(api, "updatePortfolioHoldings");
    const user = userEvent.setup();
    render(<Portfolio />, { wrapper: MemoryRouter });

    const cashInput = await screen.findByLabelText("当前可用现金");
    await user.clear(cashInput);
    await user.type(cashInput, "2500");
    await user.click(screen.getByRole("button", { name: "更新现金" }));

    expect(api.updatePortfolioCash).toHaveBeenCalledWith({ cash: 2500, cash_currency: "CNY" });
    expect(holdingsSpy).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByLabelText("当前可用现金")).toHaveValue(2500));
  });

  it("starts from the AI ratio and saves a dragged red-blue allocation", async () => {
    vi.spyOn(api, "updatePortfolioMandate").mockImplementation(async (next) => next);
    const user = userEvent.setup();
    render(<Portfolio />, { wrapper: MemoryRouter });

    const slider = await screen.findByRole("slider", { name: "进攻型目标比例" });
    expect(slider).toHaveValue("63");
    await user.click(screen.getByRole("button", { name: "采用 AI 建议 65/35" }));
    expect(slider).toHaveValue("65");
    await waitFor(() => expect(api.updatePortfolioMandate).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.updatePortfolioMandate).mock.calls[0][0].sleeves.every((item) => item.configured)).toBe(true);

    fireEvent.change(slider, { target: { value: "30" } });
    expect(slider).toHaveValue("30");
    await user.click(screen.getByRole("button", { name: "保存组合设置" }));

    await waitFor(() => expect(api.updatePortfolioMandate).toHaveBeenCalledTimes(2));
    const saved = vi.mocked(api.updatePortfolioMandate).mock.calls[1][0];
    expect(saved.sleeves.find((item) => item.id === "offensive")?.target_amount).toBe(1473);
    expect(saved.sleeves.find((item) => item.id === "defensive")?.target_amount).toBe(3437);
  });

  it("drags an individual holding from the offensive zone to the defensive zone", async () => {
    const savedMandate: PortfolioMandate = {
      ...mandate,
      version: mandate.version + 1,
      assignments: {
        ...mandate.assignments,
        "588870.SH": {
          ...mandate.assignments["588870.SH"],
          active_sleeve_id: "defensive",
          assigned_by: "user",
          confidence: 1,
          user_locked: true,
        },
      },
    };
    vi.spyOn(api, "updatePortfolioAssignment").mockResolvedValue(savedMandate);
    render(<Portfolio />, { wrapper: MemoryRouter });

    const offensiveZone = await screen.findByRole("region", { name: "进攻型持仓分区" });
    const defensiveZone = screen.getByRole("region", { name: "防守型持仓分区" });
    const card = within(offensiveZone).getByTestId("holding-card-588870.SH");
    const payload = new Map<string, string>();
    const dataTransfer = {
      effectAllowed: "none",
      dropEffect: "none",
      setData: (type: string, value: string) => payload.set(type, value),
      getData: (type: string) => payload.get(type) || "",
    };

    fireEvent.dragStart(card, { dataTransfer });
    fireEvent.dragEnter(defensiveZone, { dataTransfer });
    fireEvent.dragOver(defensiveZone, { dataTransfer });
    fireEvent.drop(defensiveZone, { dataTransfer });

    await waitFor(() => {
      expect(api.updatePortfolioAssignment).toHaveBeenCalledWith("588870.SH", "defensive");
      expect(within(defensiveZone).getByTestId("holding-card-588870.SH")).toHaveAttribute(
        "data-sleeve",
        "defensive",
      );
    });
    expect(within(defensiveZone).getByText("你的分类")).toBeInTheDocument();
  });

  it("renders layered cache metadata and source details in Chinese", async () => {
    render(<Portfolio />, { wrapper: MemoryRouter });

    expect(await screen.findByText("校核行情缓存")).toBeInTheDocument();
    expect(screen.getAllByText("588870.SH").length).toBeGreaterThan(0);
    expect(screen.getAllByText("raw").length).toBeGreaterThan(0);
    expect(document.querySelectorAll('[data-cache-group="588870.SH"]')).toHaveLength(1);
    expect(document.querySelectorAll('[data-cache-group="510300.SH"]')).toHaveLength(1);
    await userEvent.setup().click(screen.getAllByTitle("展开/收起详情")[0]);
    expect(screen.getByText("实际来源")).toBeInTheDocument();
    expect(screen.getAllByText("eastmoney").length).toBeGreaterThan(0);
    expect(screen.getByText("10,000 share")).toBeInTheDocument();
  });

  it("updates the holdings matrix with the trade response", async () => {
    const updatedReview: PortfolioReview = {
      ...review,
      portfolio_state: {
        ...review.portfolio_state,
        holdings: [{ ...review.portfolio_state.holdings[0], quantity: 2200, cost_price: 1.9806818181818182 }],
        recent_trades: [{
          trade_id: "trade-1",
          code: "588870",
          symbol: "588870.SH",
          name: "科创50ETF汇添富",
          side: "buy",
          quantity: 100,
          price: 2.1,
          applied_to_holdings: true,
          recorded_at: "2026-07-12T13:00:00Z",
        }],
      },
    };
    vi.spyOn(api, "recordPortfolioTrade").mockResolvedValue(updatedReview);
    const user = userEvent.setup();
    render(<Portfolio />, { wrapper: MemoryRouter });

    await user.type(await screen.findByLabelText("6位证券代码"), "588870");
    await waitFor(() => expect(screen.getByLabelText("自动补全的证券名称")).toHaveValue("科创50ETF汇添富"));
    expect(screen.getByLabelText("自动补全的证券标识")).toHaveValue("588870.SH");
    await user.type(screen.getByPlaceholderText("数量"), "100");
    await user.type(screen.getByPlaceholderText("价格"), "2.1");
    await user.click(screen.getByRole("button", { name: "保存交易" }));

    expect(api.recordPortfolioTrade).toHaveBeenCalledWith(expect.objectContaining({
      code: "588870",
      symbol: "588870.SH",
      name: "科创50ETF汇添富",
      side: "buy",
      quantity: 100,
      price: 2.1,
    }));
    expect(api.lookupPortfolioSecurity).toHaveBeenCalledWith("588870", expect.any(AbortSignal));
    await waitFor(() => {
      const holdingRow = within(screen.getByRole("table", { name: "持仓矩阵" })).getByText("科创50指").closest("tr");
      expect(holdingRow).not.toBeNull();
      expect(within(holdingRow as HTMLTableRowElement).getByText("2,200")).toBeInTheDocument();
    });
    expect(within(screen.getByRole("table", { name: "最近交易记录" })).getByText("买入")).toBeInTheDocument();
  });

  it("edits current holding quantity and cost inline", async () => {
    const updatedReview: PortfolioReview = {
      ...review,
      portfolio_state: {
        ...review.portfolio_state,
        holdings: [{ ...review.portfolio_state.holdings[0], quantity: 3000, cost_price: 2.05 }],
      },
    };
    vi.spyOn(api, "editPortfolioHolding").mockResolvedValue(updatedReview);
    const user = userEvent.setup();
    render(<Portfolio />, { wrapper: MemoryRouter });

    await user.click(await screen.findByRole("button", { name: "编辑 588870.SH 持仓" }));
    const quantityInput = screen.getByLabelText("588870.SH 当前持有股数");
    const costInput = screen.getByLabelText("588870.SH 当前成本价");
    await user.clear(quantityInput);
    await user.type(quantityInput, "3000");
    await user.clear(costInput);
    await user.type(costInput, "2.05");
    await user.click(screen.getByRole("button", { name: "保存 588870.SH 持仓修改" }));

    expect(api.editPortfolioHolding).toHaveBeenCalledWith("588870.SH", { quantity: 3000, cost_price: 2.05 });
    await waitFor(() => expect(screen.queryByLabelText("588870.SH 当前持有股数")).not.toBeInTheDocument());
    const holdingRow = within(screen.getByRole("table", { name: "持仓矩阵" })).getByText("科创50指").closest("tr");
    expect(within(holdingRow as HTMLTableRowElement).getByText("3,000")).toBeInTheDocument();
    expect(within(holdingRow as HTMLTableRowElement).getByText("2.05")).toBeInTheDocument();
  });

  it("deletes one trade record without changing the holding returned by the API", async () => {
    const tradeReview: PortfolioReview = {
      ...review,
      portfolio_state: {
        ...review.portfolio_state,
        holdings: [{ ...review.portfolio_state.holdings[0], quantity: 2200 }],
        recent_trades: [{
          trade_id: "trade-delete-1",
          code: "588870",
          symbol: "588870.SH",
          name: "科创50ETF汇添富",
          side: "buy",
          quantity: 100,
          price: 2.1,
          trade_date: "2026-07-12",
          recorded_at: "2026-07-12T13:00:00Z",
        }],
      },
    };
    vi.mocked(api.getPortfolioReview).mockResolvedValue(tradeReview);
    vi.spyOn(api, "deletePortfolioTrade").mockResolvedValue({
      ...tradeReview,
      portfolio_state: { ...tradeReview.portfolio_state, recent_trades: [] },
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<Portfolio />, { wrapper: MemoryRouter });

    await user.click(await screen.findByRole("button", { name: "删除交易 588870.SH 2026-07-12" }));

    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("不会撤销它已经造成的持仓变化"));
    expect(api.deletePortfolioTrade).toHaveBeenCalledWith("trade-delete-1");
    await waitFor(() => expect(within(screen.getByRole("table", { name: "最近交易记录" })).getByText("暂无最近交易记录。")).toBeInTheDocument());
    const holdingRow = within(screen.getByRole("table", { name: "持仓矩阵" })).getByText("科创50指").closest("tr");
    expect(within(holdingRow as HTMLTableRowElement).getByText("2,200")).toBeInTheDocument();
  });

  it("uses red for gains and green for losses in the holdings matrix", async () => {
    vi.mocked(api.getPortfolioReview).mockResolvedValue({
      ...review,
      portfolio_state: {
        ...review.portfolio_state,
        holdings: [
          { ...review.portfolio_state.holdings[0], pnl: 120, pnl_pct: 3.2 },
          {
            name: "测试亏损标的",
            code: "159842",
            symbol: "159842.SZ",
            quantity: 1000,
            cost_price: 1.2,
            last_price: 1.1,
            market_value: 1100,
            pnl: -100,
            pnl_pct: -8.33,
            market_status: "verified",
          },
        ],
      },
    });
    render(<Portfolio />, { wrapper: MemoryRouter });

    await screen.findByText("测试亏损标的");
    const gainCells = document.querySelectorAll('[data-pnl-tone="gain"]');
    const lossCells = document.querySelectorAll('[data-pnl-tone="loss"]');
    expect(gainCells).toHaveLength(2);
    expect(lossCells).toHaveLength(2);
    expect(gainCells[0]).toHaveClass("text-red-500");
    expect(lossCells[0]).toHaveClass("text-emerald-600");
  });

  it("sorts the holdings matrix from low to high, then high to low", async () => {
    vi.mocked(api.getPortfolioReview).mockResolvedValue({
      ...review,
      portfolio_state: {
        ...review.portfolio_state,
        holdings: [
          { ...review.portfolio_state.holdings[0], symbol: "588870.SH", quantity: 2100 },
          { ...review.portfolio_state.holdings[0], name: "低数量标的", code: "159842", symbol: "159842.SZ", quantity: 300 },
          { ...review.portfolio_state.holdings[0], name: "中数量标的", code: "510300", symbol: "510300.SH", quantity: 1200 },
        ],
      },
    });
    const user = userEvent.setup();
    render(<Portfolio />, { wrapper: MemoryRouter });

    const table = await screen.findByRole("table", { name: "持仓矩阵" });
    const rowSymbols = () => within(table)
      .getAllByRole("row")
      .slice(1)
      .map((row) => within(row).getAllByRole("cell")[1].textContent);

    await user.click(screen.getByRole("button", { name: "按数量升序排序" }));
    expect(rowSymbols()).toEqual(["159842.SZ", "510300.SH", "588870.SH"]);
    expect(within(table).getByRole("columnheader", { name: /数量/ })).toHaveAttribute("aria-sort", "ascending");

    await user.click(screen.getByRole("button", { name: "按数量降序排序" }));
    expect(rowSymbols()).toEqual(["588870.SH", "510300.SH", "159842.SZ"]);
    expect(within(table).getByRole("columnheader", { name: /数量/ })).toHaveAttribute("aria-sort", "descending");
  });

  it("opens cached K-lines by symbol and switches interval", async () => {
    const getBars = vi.spyOn(api, "getMarketCacheBars").mockImplementation(async ({ interval, adjustment }) => ({
      status: "ok",
      symbol: "588870.SH",
      interval,
      adjustment,
      view: "consensus",
      bars: [{
        symbol: "588870.SH",
        interval,
        adjustment,
        bar_time: interval === "1D" ? "2026-07-10T07:00:00Z" : "2026-07-10T06:00:00Z",
        open: 2.09,
        high: 2.12,
        low: 2.08,
        close: 2.1,
        volume: 10000,
        amount: 21000,
        status: "verified",
        source_count: 2,
        sources: ["eastmoney", "tencent"],
      }],
    }));
    render(<Portfolio />, { wrapper: MemoryRouter });

    await userEvent.setup().click(await screen.findByRole("button", { name: "查看 588870.SH K线" }));
    expect(await screen.findByRole("dialog", { name: "588870.SH K线检阅" })).toBeInTheDocument();
    await waitFor(() => expect(getBars).toHaveBeenCalledWith({
      symbol: "588870.SH",
      interval: "1D",
      adjustment: "qfq",
      view: "consensus",
      limit: 20000,
    }));
    expect(await screen.findByTestId("candlestick-chart")).toHaveTextContent("1 bars");

    await userEvent.setup().click(screen.getByRole("button", { name: "1m" }));
    await waitFor(() => expect(getBars).toHaveBeenLastCalledWith({
      symbol: "588870.SH",
      interval: "1m",
      adjustment: "raw",
      view: "consensus",
      limit: 20000,
    }));
  });

  it("starts a background refresh, polls it, and reloads the review", async () => {
    const running = makeRun("running");
    const completed = makeRun("completed");
    vi.spyOn(api, "startMarketCacheRefresh").mockResolvedValue({
      status: "accepted",
      run_id: running.run_id,
      deduplicated: false,
      run: running,
    });
    vi.spyOn(api, "getMarketCacheRun").mockResolvedValue(completed);
    render(<Portfolio />, { wrapper: MemoryRouter });

    await userEvent.setup().click(await screen.findByRole("button", { name: "刷新持仓行情" }));
    expect(api.startMarketCacheRefresh).toHaveBeenCalledWith({ profile: "portfolio_default" });
    expect(await screen.findByText("行情缓存刷新")).toBeInTheDocument();
    await waitFor(() => expect(api.getMarketCacheRun).toHaveBeenCalledWith(running.run_id), { timeout: 2000 });
    await waitFor(() => expect(api.getPortfolioReview).toHaveBeenCalledTimes(2), { timeout: 2000 });
    expect(screen.getByText("已完成")).toBeInTheDocument();
  });

  it("starts a holding analysis in the background, then opens its completed session", async () => {
    const queued: PortfolioAnalysisSession = {
      analysis_id: "analysis-123",
      session_id: "session-123",
      scope: "holding",
      symbol: "588870.SH",
      status: "queued",
      created_at: "2026-07-11T01:00:00Z",
    };
    vi.spyOn(api, "startPortfolioAnalysis").mockResolvedValue(queued);
    vi.spyOn(api, "getPortfolioAnalysis").mockResolvedValue({ ...queued, status: "completed" });
    const user = userEvent.setup();

    render(
      <MemoryRouter initialEntries={["/portfolio"]}>
        <LocationProbe />
        <Portfolio />
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole("button", { name: "分析 588870.SH 启动" }));
    expect(api.startPortfolioAnalysis).toHaveBeenCalledWith({ scope: "holding", symbol: "588870.SH" });
    expect(screen.getByTestId("location")).toHaveTextContent("/portfolio");
    await waitFor(() => expect(screen.getByRole("button", { name: "分析 588870.SH 查看报告" })).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "分析 588870.SH 重新分析" })).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem("vibe-trading:portfolio-analysis-runs:v1") || "{}")["holding:588870.SH"].analysis_id).toBe("analysis-123");

    await user.click(screen.getByRole("button", { name: "分析 588870.SH 重新分析" }));
    await waitFor(() => expect(api.startPortfolioAnalysis).toHaveBeenCalledTimes(2));
    expect(api.startPortfolioAnalysis).toHaveBeenLastCalledWith({ scope: "holding", symbol: "588870.SH" });

    await user.click(screen.getByRole("button", { name: "分析 588870.SH 查看报告" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/agent?session=session-123");
  });

  it("starts the full portfolio report without leaving the holdings page", async () => {
    const queued: PortfolioAnalysisSession = {
      analysis_id: "portfolio-analysis-123",
      session_id: "portfolio-session-123",
      scope: "portfolio",
      status: "queued",
      created_at: "2026-07-11T01:00:00Z",
    };
    vi.spyOn(api, "startPortfolioAnalysis").mockResolvedValue(queued);
    vi.spyOn(api, "getPortfolioAnalysis").mockResolvedValue({ ...queued, status: "running" });
    const user = userEvent.setup();

    render(
      <MemoryRouter initialEntries={["/portfolio"]}>
        <LocationProbe />
        <Portfolio />
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole("button", { name: "全持仓报告 启动" }));
    expect(api.startPortfolioAnalysis).toHaveBeenCalledWith({ scope: "portfolio", symbol: undefined });
    expect(screen.getByTestId("location")).toHaveTextContent("/portfolio");
    await waitFor(() => expect(screen.getByRole("button", { name: "全持仓报告 分析中" })).toBeInTheDocument());
  });

  it("switches the timed portfolio analysis from premarket to intraday at 11:30 Shanghai", () => {
    expect(getMarketAnalysisPhase(new Date("2026-07-13T03:29:00Z"))).toBe("premarket");
    expect(getMarketAnalysisLabel(new Date("2026-07-13T03:29:00Z"))).toBe("盘前分析");
    expect(getMarketAnalysisPhase(new Date("2026-07-13T03:30:00Z"))).toBe("intraday");
    expect(getMarketAnalysisLabel(new Date("2026-07-13T03:30:00Z"))).toBe("盘中分析");
    expect(getMarketAnalysisPhase(new Date("2026-07-13T07:00:00Z"))).toBe("premarket");
    expect(getMarketAnalysisLabel(new Date("2026-07-12T04:00:00Z"))).toBe("盘前分析");
  });

  it("starts the time-sensitive market analysis without replacing the full report", async () => {
    const phase = getMarketAnalysisPhase();
    const label = getMarketAnalysisLabel();
    const queued: PortfolioAnalysisSession = {
      analysis_id: "market-analysis-123",
      session_id: "market-session-123",
      scope: "market",
      analysis_phase: phase,
      status: "queued",
      created_at: "2026-07-13T03:30:00Z",
    };
    vi.spyOn(api, "startPortfolioAnalysis").mockResolvedValue(queued);
    const user = userEvent.setup();

    render(<Portfolio />, { wrapper: MemoryRouter });

    expect(await screen.findByRole("button", { name: "全持仓报告 启动" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: `${label} 启动` }));
    expect(api.startPortfolioAnalysis).toHaveBeenCalledWith({ scope: "market", symbol: undefined });
  });
});
