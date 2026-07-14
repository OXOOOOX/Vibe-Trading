import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DataCenter } from "../DataCenter";
import { dataApi } from "@/lib/dataApi";

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
  });

  it("shows status coverage and supports adding a manual prewarm symbol", async () => {
    const user = userEvent.setup();
    render(<DataCenter />);
    expect(await screen.findByRole("heading", { name: "数据中心" })).toBeInTheDocument();
    expect(screen.getByText("588870.SH")).toBeInTheDocument();
    expect(screen.getByText("可用")).toBeInTheDocument();
    await user.type(screen.getByLabelText("自选标的代码"), "510300.SH");
    await user.click(screen.getByRole("button", { name: "添加" }));
    await waitFor(() => expect(dataApi.addWatchlist).toHaveBeenCalledWith("510300.SH"));
  });
});
