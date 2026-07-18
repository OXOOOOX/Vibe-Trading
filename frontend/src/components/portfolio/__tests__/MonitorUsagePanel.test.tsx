import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { MonitorUsagePanel } from "@/components/portfolio/MonitorUsagePanel";
import {
  api,
  type MonitorJobUsageSummary,
  type MonitorUsageSummary,
  type SessionUsageAggregate,
} from "@/lib/api";

const aggregate: SessionUsageAggregate = {
  tokens: {
    input_tokens: 800,
    output_tokens: 200,
    total_tokens: 1_000,
    cache_read_input_tokens: 400,
    cache_write_input_tokens: 0,
    reasoning_tokens: 0,
    cache_hit_rate: 0.5,
    coverage: "complete",
    reported_calls: 1,
    total_calls: 1,
    unreported_calls: 0,
  },
  cost: {
    coverage: "complete",
    priced_calls: 1,
    unpriced_calls: 0,
    total_calls: 1,
    currencies: [{
      currency: "CNY",
      estimated_cost: 0.1,
      minimum_estimated_cost: 0.1,
      maximum_estimated_cost: 0.1,
      calls: 1,
      peak_calls: 0,
    }],
    catalog_version: "2026-07-17",
    time_basis: "started_at",
  },
  calls: {
    llm_calls: 1,
    agent_tools: 2,
    external_requests: 3,
    cache_accesses: 1,
    failures: 0,
    running: 0,
  },
  models: [{ key: "deepseek-v4-pro", count: 1 }],
  tools: [{ key: "monitor_planner_item", count: 1 }],
  categories: [{ key: "market", count: 3 }],
  providers: [{ key: "eastmoney", count: 3 }],
};

const summary: MonitorUsageSummary = {
  recording_status: "recording",
  scope_type: "monitor_job",
  scope_id: "monitor_job:today",
  revision: 3,
  recording_started_at: "2026-07-17T00:00:00Z",
  current_attempt_id: null,
  session: aggregate,
  current_attempt: aggregate,
  period: "today",
  started_at: "2026-07-16T16:00:00Z",
  completed_at: "2026-07-17T08:00:00Z",
  scope_count: 1,
  linked_scope_count: 1,
  recent_jobs: [{
    job_id: "job-1",
    status: "ready",
    requested_symbols: ["600000.SH"],
    activation_mode: "autonomous",
    trigger_type: "report_ready",
    created_at: "2026-07-17T01:00:00Z",
    completed_at: "2026-07-17T01:05:00Z",
    revision: 3,
    recording_started_at: "2026-07-17T01:00:00Z",
    updated_at: "2026-07-17T01:05:00Z",
    usage: aggregate,
    linked_scopes: [{
      scope_type: "session",
      scope_id: "deep-session",
      relationship: "auto_deep_report",
    }],
  }],
};

const jobSummary: MonitorJobUsageSummary = {
  ...summary,
  scope_id: "job-1",
  direct: aggregate,
  linked_scopes: summary.recent_jobs[0].linked_scopes,
  job: {
    job_id: "job-1",
    status: "ready",
    requested_symbols: ["600000.SH"],
    activation_mode: "autonomous",
    trigger_type: "report_ready",
    created_at: "2026-07-17T01:00:00Z",
    completed_at: "2026-07-17T01:05:00Z",
  },
};

describe("MonitorUsagePanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "getPortfolioMonitoringUsage").mockResolvedValue(summary);
    vi.spyOn(api, "getPortfolioMonitoringUsageEvents").mockResolvedValue({
      recording_status: "recording",
      revision: 3,
      items: [],
      next_cursor: null,
    });
    vi.spyOn(api, "getPortfolioMonitorPlannerJobUsage").mockResolvedValue(jobSummary);
    vi.spyOn(api, "getPortfolioMonitorPlannerJobUsageEvents").mockResolvedValue({
      recording_status: "recording",
      revision: 3,
      items: [],
      next_cursor: null,
    });
  });

  it("shows today's total and opens a linked task ledger", async () => {
    const user = userEvent.setup();
    render(<MonitorUsagePanel running={false} />);

    expect(await screen.findByRole("button", { name: /今日 1K Token/ })).toHaveTextContent("¥0.1");
    await user.click(screen.getByRole("button", { name: /打开 AI 监控 Token 总账/ }));
    expect(await screen.findByRole("heading", { name: "AI 监控 Token 总账" })).toBeInTheDocument();
    expect(screen.getByText("最近自动分析任务")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "查看全链路" }));
    expect(await screen.findByRole("heading", { name: "自动分析任务用量" })).toBeInTheDocument();
    expect(screen.getByText("1 个关联账本")).toBeInTheDocument();
    expect(api.getPortfolioMonitorPlannerJobUsage).toHaveBeenCalledWith("job-1");
  });

  it("opens a task directly from an autopilot run entry", async () => {
    const handled = vi.fn();
    render(
      <MonitorUsagePanel
        running={false}
        requestedJobId="job-1"
        onRequestedJobHandled={handled}
      />,
    );

    expect(await screen.findByRole("heading", { name: "自动分析任务用量" })).toBeInTheDocument();
    await waitFor(() => expect(handled).toHaveBeenCalledTimes(1));
  });
});
