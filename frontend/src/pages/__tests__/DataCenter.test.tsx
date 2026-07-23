import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { DataCenter } from "../DataCenter";
import { api, type MarketCacheRun } from "@/lib/api";
import { dataApi } from "@/lib/dataApi";

vi.mock("@/lib/api", () => ({
  api: {
    lookupPortfolioSecurity: vi.fn(),
    startMarketCacheRefresh: vi.fn(),
    getMarketCacheRun: vi.fn(),
  },
}));

vi.mock("@/lib/dataApi", () => ({
  dataApi: {
    coverage: vi.fn(), sources: vi.fn(), storage: vi.fn(), watchlist: vi.fn(),
    addWatchlist: vi.fn(), removeWatchlist: vi.fn(), prewarm: vi.fn(), prewarmStatus: vi.fn(),
  },
}));

describe("DataCenter", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(dataApi.coverage).mockResolvedValue({ status: "ok", coverage: [{ symbol: "588870.SH", actual_source: "eastmoney", interval: "1D", actual_adjustment: "qfq", min_bar_time: "2024-01-01T00:00:00Z", max_bar_time: "2026-07-13T00:00:00Z", row_count: 500, last_success_at: "2026-07-13T00:00:00Z" }], watchlist: [], retention: {} });
    vi.mocked(dataApi.sources).mockResolvedValue({ status: "ok", quorum: "two sources", sources: [{ source: "eastmoney:market", requested_source: "eastmoney", actual_source: "eastmoney", upstream_source: "eastmoney_push2his", capability: "market", consecutive_failures: 0, circuit_open: false, circuit_open_until: null, last_status: "ok", effective_status: "ok", stale: false, last_latency_ms: 42, error_category: null, last_error: null, updated_at: "2026-07-13T00:00:00Z" }] });
    vi.mocked(dataApi.storage).mockResolvedValue({ status: "ok", entries: [{ kind: "market_cache", path: "C:/cache", bytes: 1024 }], total_bytes: 1024, soft_limit_bytes: 10 * 1024 ** 3, evict_at_bytes: 9 * 1024 ** 3, retention: {} });
    vi.mocked(dataApi.watchlist).mockResolvedValue({ status: "ok", watchlist: [] });
    vi.mocked(dataApi.addWatchlist).mockResolvedValue({ status: "ok", entry: { symbol: "510300.SH", note: null, added_at: "2026-07-13T00:00:00Z" } });
    vi.mocked(dataApi.prewarm).mockResolvedValue({ status: "live" });
    vi.mocked(dataApi.prewarmStatus).mockResolvedValue({ enabled: true, running: true, timezone: "Asia/Shanghai", calendar_mode: "exchange_calendar", slots: [{ phase: "premarket", time: "09:10" }], last_run: null });
    vi.mocked(api.lookupPortfolioSecurity).mockResolvedValue({
      code: "AAPL",
      symbol: "AAPL.US",
      name: "Apple Inc.",
      market: "us",
      source: "yahoo",
    });
  });

  it("shows status coverage and supports adding a manual prewarm symbol", async () => {
    const user = userEvent.setup();
    render(<DataCenter />, { wrapper: MemoryRouter });
    expect(await screen.findByRole("heading", { name: "数据中心" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "展开 K 线缓存覆盖" }));
    expect(await screen.findByText("588870.SH")).toBeInTheDocument();
    expect(screen.getByText("可用")).toBeInTheDocument();
    await user.type(screen.getByLabelText("自选标的代码"), "510300.SH");
    await user.click(screen.getByRole("button", { name: "添加" }));
    await waitFor(() => expect(dataApi.addWatchlist).toHaveBeenCalledWith("510300.SH"));
  });

  it("defaults the K-line cache coverage card to collapsed and supports toggling it", async () => {
    const user = userEvent.setup();
    render(<DataCenter />, { wrapper: MemoryRouter });

    const expand = await screen.findByRole("button", { name: "展开 K 线缓存覆盖" });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("columnheader", { name: "价格调整口径" })).not.toBeInTheDocument();

    await user.click(expand);

    const collapse = screen.getByRole("button", { name: "收起 K 线缓存覆盖" });
    expect(collapse).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByRole("columnheader", { name: "价格调整口径" })).toBeInTheDocument();

    await user.click(collapse);

    expect(screen.getByRole("button", { name: "展开 K 线缓存覆盖" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("columnheader", { name: "价格调整口径" })).not.toBeInTheDocument();
  });

  it("localizes health semantics, adjustment names, and Beijing timestamps", async () => {
    vi.mocked(dataApi.coverage).mockResolvedValue({
      status: "ok",
      coverage: [{ symbol: "AAPL.US", actual_source: "yahoo", interval: "1D", actual_adjustment: "source_default", min_bar_time: "2026-07-15T00:00:00Z", max_bar_time: "2026-07-16T00:00:00Z", row_count: 2, last_success_at: "2026-07-16T15:40:00Z" }],
      watchlist: [],
      retention: {},
    });
    vi.mocked(dataApi.sources).mockResolvedValue({
      status: "ok",
      quorum: "two sources",
      sources: [
        {
          source: "yahoo:market",
          requested_source: "yahoo",
          actual_source: "yahoo",
          upstream_source: "yahoo_chart",
          capability: "market",
          consecutive_failures: 0,
          circuit_open: false,
          circuit_open_until: null,
          last_status: "basis_mismatch",
          effective_status: "basis_mismatch",
          stale: false,
          last_latency_ms: 756,
          error_category: "basis_mismatch",
          last_error: "requested qfq, provider returned source_default",
          updated_at: "2026-07-16T15:40:00Z",
        },
        {
          source: "eastmoney:report",
          requested_source: "eastmoney",
          actual_source: "eastmoney",
          upstream_source: "eastmoney",
          capability: "report",
          consecutive_failures: 0,
          circuit_open: false,
          circuit_open_until: null,
          last_status: "not_applicable",
          effective_status: "not_applicable",
          stale: false,
          last_latency_ms: null,
          error_category: "not_applicable",
          last_error: "research reports are China A-share only (.SH/.SZ/.BJ); got 'AAPL.US'",
          updated_at: "2026-07-16T15:40:00Z",
        },
      ],
    });

    render(<DataCenter />, { wrapper: MemoryRouter });

    expect(await screen.findAllByText("口径未分类（source_default）")).not.toHaveLength(0);
    expect(screen.getByText("行情")).toBeInTheDocument();
    expect(screen.getByText("研报")).toBeInTheDocument();
    expect(screen.getByText("价格口径不一致（已阻止混用）")).toBeInTheDocument();
    expect(screen.getByText("请求最新价锚定全复权（qfq），提供方返回口径未分类（source_default）；两者未被视为等价，数据已排除。")).toBeInTheDocument();
    expect(screen.getByText("不适用于该市场")).toBeInTheDocument();
    expect(screen.getByText("该研报源仅支持中国 A 股，AAPL.US 不适用。")).toBeInTheDocument();
    expect(screen.getAllByText("2026-07-16 23:40").length).toBeGreaterThan(0);
    expect(screen.queryByText("requested qfq, provider returned source_default")).not.toBeInTheDocument();
  });

  it("shows a symbol-focused data center from the portfolio link", async () => {
    vi.mocked(dataApi.coverage).mockResolvedValue({
      status: "ok",
      coverage: [
        { symbol: "588870.SH", actual_source: "eastmoney", interval: "1D", actual_adjustment: "qfq", min_bar_time: "2024-01-01T00:00:00Z", max_bar_time: "2026-07-13T00:00:00Z", row_count: 500, last_success_at: "2026-07-13T00:00:00Z" },
        { symbol: "AAPL.US", actual_source: "yahoo", interval: "1m", actual_adjustment: "raw", min_bar_time: "2026-07-13T13:30:00Z", max_bar_time: "2026-07-13T20:00:00Z", row_count: 390, last_success_at: "2026-07-13T20:02:00Z" },
        { symbol: "AAPL.US", actual_source: "nasdaq", interval: "1m", actual_adjustment: "raw", min_bar_time: "2026-07-13T13:30:00Z", max_bar_time: "2026-07-13T20:00:00Z", row_count: 390, last_success_at: "2026-07-13T20:02:00Z" },
      ],
      watchlist: [],
      retention: {},
    });

    render(
      <MemoryRouter initialEntries={["/data-center?symbol=AAPL.US"]}>
        <DataCenter />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      /Apple Inc\.\s*AAPL\.US\s*行情详情/,
    ));
    expect(screen.getByText("缓存来源").closest("article")).toHaveTextContent("2");
    expect(screen.queryByText("588870.SH")).not.toBeInTheDocument();
    expect(screen.getByText(/全局行情刷新会复用有效成功源/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看全部标的" })).toHaveAttribute("href", "/data-center");
    expect(api.lookupPortfolioSecurity).toHaveBeenCalledWith("AAPL.US");
  });

  it("refreshes the focused symbol and reports reused sources", async () => {
    const user = userEvent.setup();
    const completedRun: MarketCacheRun = {
      run_id: "run-1",
      profile: "symbol_detail",
      status: "completed",
      symbols: ["AAPL.US"],
      config: {},
      total_items: 1,
      completed_items: 1,
      conflict_items: 0,
      failed_items: 0,
      progress_pct: 100,
      created_at: "2026-07-15T01:00:00Z",
      completed_at: "2026-07-15T01:00:01Z",
      items: [
        {
          id: 1,
          run_id: "run-1",
          symbol: "AAPL.US",
          interval: "1m",
          adjustment: "raw",
          status: "verified",
          requested_sources: ["yahoo", "nasdaq"],
          actual_sources: ["yahoo", "nasdaq"],
          attempts: [
            { requested_source: "yahoo", status: "cache_fresh" },
            { requested_source: "nasdaq", status: "cache_fresh" },
          ],
          rows_written: 0,
        },
      ],
    };
    vi.mocked(api.startMarketCacheRefresh).mockResolvedValue({
      status: "accepted",
      run_id: "run-1",
      deduplicated: false,
      run: completedRun,
    });
    vi.mocked(api.getMarketCacheRun).mockResolvedValue(completedRun);

    render(
      <MemoryRouter initialEntries={["/data-center?symbol=AAPL.US&name=Apple%20Inc."]}>
        <DataCenter />
      </MemoryRouter>,
    );
    await screen.findByRole("heading", { name: /Apple Inc.*AAPL\.US.*行情详情/ });

    await user.click(screen.getByRole("button", { name: "刷新行情" }));

    await waitFor(() => expect(api.startMarketCacheRefresh).toHaveBeenCalledWith({
      symbols: ["AAPL.US"],
      profile: "symbol_detail",
    }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "刷新完成：复用 2 个有效来源，本次没有发起新的行情请求。",
    );
  });

  it("resolves the name for a qualified mainland index symbol", async () => {
    vi.mocked(api.lookupPortfolioSecurity).mockResolvedValue({
      code: "000905",
      symbol: "000905.SH",
      name: "中证500",
      market: "cn",
      source: "eastmoney",
    });
    vi.mocked(dataApi.coverage).mockResolvedValue({
      status: "ok",
      coverage: [{ symbol: "000905.SH", actual_source: "eastmoney", interval: "1D", actual_adjustment: "raw", min_bar_time: "2024-01-01T00:00:00Z", max_bar_time: "2026-07-14T00:00:00Z", row_count: 500, last_success_at: "2026-07-14T00:00:00Z" }],
      watchlist: [],
      retention: {},
    });

    render(
      <MemoryRouter initialEntries={["/data-center?symbol=000905.SH"]}>
        <DataCenter />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      /中证500\s*000905\.SH\s*行情详情/,
    ));
    expect(api.lookupPortfolioSecurity).toHaveBeenCalledWith("000905.SH");
  });
});
