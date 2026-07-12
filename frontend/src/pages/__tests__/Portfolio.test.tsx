import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { api, type MarketCacheRun, type PortfolioAnalysisSession, type PortfolioReview } from "@/lib/api";
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
