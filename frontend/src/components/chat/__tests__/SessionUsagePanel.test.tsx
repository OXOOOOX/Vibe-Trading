import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { api, type SessionUsageSummary } from "@/lib/api";
import { SessionUsagePanel } from "../SessionUsagePanel";

const aggregate: SessionUsageSummary["session"] = {
  tokens: {
    input_tokens: 120_000,
    output_tokens: 28_000,
    total_tokens: 148_000,
    cache_read_input_tokens: 72_000,
    cache_write_input_tokens: null,
    reasoning_tokens: 8_000,
    cache_hit_rate: 0.6,
    coverage: "complete",
    reported_calls: 4,
    total_calls: 4,
    unreported_calls: 0,
  },
  cost: {
    coverage: "complete",
    priced_calls: 4,
    unpriced_calls: 0,
    total_calls: 4,
    currencies: [{
      currency: "CNY",
      estimated_cost: 1.2,
      minimum_estimated_cost: 1.2,
      maximum_estimated_cost: 1.2,
      calls: 4,
      peak_calls: 1,
    }],
    catalog_version: "2026-07-17",
    time_basis: "started_at",
    sources: [{ label: "DeepSeek 官方价格", url: "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/" }],
  },
  calls: {
    llm_calls: 4,
    agent_tools: 12,
    external_requests: 7,
    cache_accesses: 3,
    failures: 1,
    running: 0,
  },
  models: [{ key: "gpt-5", count: 4 }],
  tools: [{ key: "web_search", count: 3 }],
  categories: [{ key: "web", count: 5 }, { key: "market", count: 4 }],
  providers: [{ key: "duckduckgo", count: 3 }, { key: "yahoo", count: 2 }],
};

const summary: SessionUsageSummary = {
  recording_status: "recording",
  scope_type: "session",
  scope_id: "s1",
  revision: 8,
  recording_started_at: "2026-07-17T01:00:00Z",
  current_attempt_id: "a1",
  session: aggregate,
  current_attempt: { ...aggregate, calls: { ...aggregate.calls, agent_tools: 2 } },
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SessionUsagePanel", () => {
  it("opens the accessible overview, switches scope/details, and restores focus", async () => {
    vi.spyOn(api, "getSessionUsage").mockResolvedValue(summary);
    vi.spyOn(api, "getSessionUsageEvents").mockResolvedValue({
      recording_status: "recording",
      revision: 8,
      next_cursor: null,
      items: [
        {
          sequence: 2,
          event_id: "resource-1",
          kind: "resource_call",
          category: "web",
          provider: "duckduckgo",
          status: "ok",
          started_at: "2026-07-17T01:00:02Z",
          elapsed_ms: 120,
          cache_mode: "network",
          network_request: true,
          cache_access: false,
          query_summary: "market news",
          metadata: {},
        },
      ],
    });
    const user = userEvent.setup();
    render(<SessionUsagePanel sessionId="s1" running={false} refreshSignal={0} forceRefreshSignal={0} />);

    const trigger = await screen.findByRole("button", { name: /打开 Session 资源用量/ });
    expect(trigger).toHaveTextContent("148K Token · ¥1.2 · 12 调用");
    await user.click(trigger);

    expect(screen.getByRole("dialog", { name: "Session 资源用量" })).toBeInTheDocument();
    expect(screen.getByText("缓存输入（输入子集）")).toBeInTheDocument();
    expect(screen.getByText("60.0%")).toBeInTheDocument();
    expect(screen.getByText("费用估算")).toBeInTheDocument();
    expect(screen.getByText("1 次高峰调用")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "本轮" }));
    expect((await screen.findAllByText("2")).length).toBeGreaterThan(0);
    await user.click(screen.getByRole("tab", { name: "调用明细" }));
    expect(await screen.findByText("market news")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("shows unrecorded for legacy sessions without inventing usage", async () => {
    vi.spyOn(api, "getSessionUsage").mockResolvedValue({
      ...summary,
      recording_status: "unrecorded",
      recording_started_at: null,
      revision: 0,
      session: {
        ...aggregate,
        tokens: { ...aggregate.tokens, total_tokens: null, input_tokens: null, output_tokens: null },
      },
    });
    vi.spyOn(api, "getSessionUsageEvents").mockResolvedValue({
      recording_status: "unrecorded",
      revision: 0,
      items: [],
      next_cursor: null,
    });
    const user = userEvent.setup();
    render(<SessionUsagePanel sessionId="legacy" running={false} refreshSignal={0} forceRefreshSignal={0} />);

    const trigger = await screen.findByRole("button", { name: /未记录/ });
    expect(trigger).toHaveTextContent("未记录");
    await user.click(trigger);
    expect(screen.getByText("这个旧 Session 未记录用量")).toBeInTheDocument();
    expect(screen.getByText(/不对历史 Token 或资源请求做估算与回填/)).toBeInTheDocument();
  });

  it("ignores a stale usage response after switching sessions", async () => {
    let resolveSlow: (value: SessionUsageSummary) => void = () => {};
    const slow = new Promise<SessionUsageSummary>((resolve) => { resolveSlow = resolve; });
    vi.spyOn(api, "getSessionUsage").mockImplementation((sid) => {
      if (sid === "slow") return slow;
      return Promise.resolve({
        ...summary,
        scope_id: "fast",
        session: { ...aggregate, tokens: { ...aggregate.tokens, total_tokens: 8_000 } },
      });
    });
    const view = render(<SessionUsagePanel sessionId="slow" running={false} refreshSignal={0} forceRefreshSignal={0} />);
    view.rerender(<SessionUsagePanel sessionId="fast" running={false} refreshSignal={0} forceRefreshSignal={0} />);

    expect(await screen.findByRole("button", { name: /8K Token/ })).toBeInTheDocument();
    resolveSlow(summary);
    await waitFor(() => expect(screen.getByRole("button", { name: /8K Token/ })).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /148K Token/ })).not.toBeInTheDocument();
  });
});
