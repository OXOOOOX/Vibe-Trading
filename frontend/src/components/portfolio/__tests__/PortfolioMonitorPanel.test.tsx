import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";

import PortfolioMonitorPanel from "@/components/portfolio/PortfolioMonitorPanel";
import {
  api,
  type MonitorPlan,
  type MonitorPlanVersion,
  type MonitorProfile,
  type MonitorTargetMonitoringCard,
  type PortfolioHolding,
  type PortfolioMonitoringStatus,
} from "@/lib/api";

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function makePlan(overrides: Partial<MonitorPlan> = {}): MonitorPlan {
  const validUntil = new Date(Date.now() + 90 * 24 * 60 * 60 * 1_000).toISOString();
  return {
    schema_version: 3,
    symbol: "588870.SH",
    summary: "规则策略生成的待审核计划。",
    quote_tier: "normal",
    near_trigger_tier: "active",
    near_trigger_distance_bps: 100,
    price_volume_policy: {
      enabled: true,
      interval: "5m",
      baseline_method: "same_time_bucket_median",
      baseline_sessions: 10,
      min_samples: 5,
      contraction_ratio: 0.8,
      expansion_ratio: 1.5,
      flat_return_bps: 10,
      acceleration_multiplier: 1.2,
    },
    market_rules: [{
      client_rule_id: "up-l1",
      kind: "price_cross_above",
      severity: "warning",
      enabled: true,
      target_intent: "take_profit",
      target_level: 1,
      parameters: {
        threshold: 2.2,
        interval: "5m",
        adjustment: "raw",
        confirmation_count: 2,
        cooldown_minutes: 120,
        clear_hysteresis_bps: 30,
      },
      valid_until: validUntil,
    }, {
      client_rule_id: "up-l2",
      kind: "price_cross_above",
      severity: "warning",
      enabled: true,
      target_intent: "take_profit",
      target_level: 2,
      parameters: {
        threshold: 2.4,
        interval: "5m",
        adjustment: "raw",
        confirmation_count: 2,
        cooldown_minutes: 120,
        clear_hysteresis_bps: 30,
      },
      valid_until: validUntil,
    }],
    news_topics: [],
    fundamental_monitor: { enabled: false },
    hard_valid_until: validUntil,
    ...overrides,
  };
}

function makeVersion(
  version: number,
  status: string,
  plan: MonitorPlan,
): MonitorPlanVersion {
  return {
    profile_id: "profile-1",
    version,
    status,
    schema_version: plan.schema_version,
    plan,
    evidence_manifest: {},
    model_id: "evidence-policy-v3",
    data_as_of: "2026-07-15T06:00:00Z",
    created_at: `2026-07-15T06:0${version}:00Z`,
  };
}

function makeProfile(
  status: MonitorProfile["status"] = "pending_review",
  plans: MonitorPlanVersion[] = [makeVersion(1, "pending_review", makePlan())],
): MonitorProfile {
  const activeVersion = plans.find((version) => version.status === "active");
  return {
    profile_id: "profile-1",
    symbol: "588870.SH",
    market: "SH",
    instrument_type: "etf",
    status,
    active_plan_version: activeVersion?.version ?? null,
    profile_revision: 4,
    delivery_target_id: "target-1",
    input_outdated: false,
    blocked_reasons: [],
    updated_at: "2026-07-15T06:00:00Z",
    last_quote_check_at: "2026-07-15T06:00:00Z",
    next_quote_run_at: "2099-12-31T15:00:00Z",
    last_quote: {
      price: 2.5,
      observed_at: "2026-07-15T06:00:00Z",
      data_as_of: "2026-07-15T06:00:00Z",
      status: "verified",
      interval: "5m",
      sources: ["eastmoney", "mootdx"],
      session_open: 2.1,
      trend: "up",
      price_change_pct: 0.5,
    },
    plans,
    display_plan: activeVersion || plans[0],
  };
}

function makeStatus(overrides: Partial<PortfolioMonitoringStatus> = {}): PortfolioMonitoringStatus {
  return {
    enabled_by_config: true,
    effective_mode: "shadow",
    runtime: {
      enabled: true,
      running: true,
      leader: true,
      mode: "shadow",
      calendar: { mode: "cached_exchange_calendar", session: "afternoon", open: true },
    },
    capabilities: { market_rules: "available", automatic_trading: "forbidden" },
    profiles: 1,
    active_profiles: 1,
    events: 0,
    pending_deliveries: 0,
    uncertain_deliveries: 0,
    shadow_suppressed_deliveries: 0,
    blocked_profiles: 0,
    database_size_bytes: 0,
    database_max_bytes: 536_870_912,
    database_utilization: 0,
    delivery_status_counts: {},
    observation_status_counts: {},
    price_volume_quality: {
      window_hours: 24,
      observation_count: 0,
      evidence_count: 0,
      disabled_count: 0,
      status_counts: {},
      reason_counts: {},
      insufficient_rate: 0,
      conflict_rate: 0,
    },
    runtime_health: {
      window_hours: 24,
      tick_count: 0,
      error_tick_count: 0,
      events_created: 0,
      duplicate_event_count: 0,
      event_attempt_count: 0,
      duplicate_event_rate: 0,
      duration_ms: {},
      schedule_lag_ms: {},
      bar_lag_ms: {},
      database_growth_bytes: 0,
      counters: {},
    },
    profile_health: [],
    maintenance: null,
    ...overrides,
  };
}

function renderPanel(
  holdings: PortfolioHolding[] = [],
  selectedSymbols: Set<string> = new Set(),
  selectionRevision = 0,
) {
  return render(
    <PortfolioMonitorPanel
      holdings={holdings}
      selectedSymbols={selectedSymbols}
      selectionRevision={selectionRevision}
    />,
  );
}

describe("PortfolioMonitorPanel reliable controls", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    const profile = makeProfile();
    vi.spyOn(api, "listPortfolioMonitors").mockResolvedValue({ profiles: [profile] });
    vi.spyOn(api, "listPortfolioMonitorEvents").mockResolvedValue({ events: [] });
    vi.spyOn(api, "listPortfolioMonitorDeliveryTargets").mockResolvedValue({ targets: [] });
    vi.spyOn(api, "getPortfolioMonitoringStatus").mockResolvedValue(makeStatus());
    vi.spyOn(api, "getPortfolioMonitor").mockResolvedValue(profile);
    vi.spyOn(api, "getPortfolioMonitoringAutopilot").mockResolvedValue({
      config_id: "default",
      enabled: false,
      selected_symbols: [],
      activation_mode: "autonomous",
      research_policy: "if_needed",
      trigger_types: [
        "report_ready", "holdings_changed", "scheduled_close", "approaching",
        "invalidated", "material_evidence_changed",
      ],
      daily_close_enabled: true,
      delivery_target_id: null,
      runtime_mode: "shadow",
      revision: 1,
      automatic_trading: "forbidden",
    });
    vi.spyOn(api, "listPortfolioMonitoringAutopilotRuns").mockResolvedValue({ runs: [] });
    vi.spyOn(api, "listPortfolioMonitoringTargets").mockResolvedValue({ targets: [] });
    vi.spyOn(api, "listPortfolioMonitorRecommendations").mockResolvedValue({ recommendations: [] });
    vi.spyOn(api, "listPortfolioMonitorReportCandidates").mockImplementation(async (symbol) => ({
      symbol,
      candidates: [],
    }));
    vi.spyOn(api, "configurePortfolioMonitoringAutopilot");
  });

  it("explains weekly report provenance, review deadline, and watch-only degradation", async () => {
    const weeklyPlan = makePlan({
      source_horizon: "weekly",
      source_report_id: "weekly_588870_20260717",
      source_period: { week_start: "2026-07-13", week_end: "2026-07-17", label: "2026-07-13 至 2026-07-17" },
      source_valid_until: "2026-07-24T07:30:00+00:00",
      review_due_at: "2026-07-24T07:30:00+00:00",
      analysis_ref: {
        snapshot_id: "weekly-snapshot",
        report_ref: "weekly:run:json",
        report_type: "weekly_review",
        title: "科创板芯片ETF周度复盘",
        revision: 1,
        body_sha256: "a".repeat(64),
        quality_status: "data_limited",
        generated_at: "2026-07-18T01:00:00+00:00",
        data_as_of: "2026-07-17T07:00:00+00:00",
      },
      watch_scenarios: [{
        scenario_id: "weekly-watch",
        client_rule_id: "weekly-rule",
        label: "周度阻力突破",
        intent: "breakout",
        evidence_refs: ["claim-1"],
        original_level: { kind: "price", value: 1.85, unit: "CNY", adjustment: "raw" },
        trigger: { kind: "price_cross_above", threshold: 1.85, interval: "1m", confirmation_count: 1 },
        approach_policy: { distance_bps: 100, source: "report", check_interval: "1m" },
        volume_confirmation: { metric: "same_bucket_5m_volume_ratio", comparator: "gte", threshold: 1.5, min_samples: 5, mode: "classify_only", unit: "ratio" },
        resolution_policy: { rejection_hysteresis_bps: 30, max_observation_bars: 6, close_action: "unresolved" },
        rationale: "等待日线确认",
        source_conditions: [{
          condition_id: "daily-close",
          source_text: "日线收盘确认",
          role: "required",
          coverage_status: "awaiting_data",
          reason: "分钟价格不能替代日线收盘",
          evidence_refs: ["claim-1"],
        }],
        automation_status: "watch_only",
      }],
    });
    const profile = makeProfile("active", [makeVersion(1, "active", weeklyPlan)]);
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({ profiles: [profile] });
    vi.mocked(api.getPortfolioMonitor).mockResolvedValue(profile);

    renderPanel();

    expect(await screen.findByText("来源：正式周报")).toBeInTheDocument();
    expect(screen.getByText("科创板芯片ETF周度复盘")).toBeInTheDocument();
    expect(screen.getByText("2026-07-13 至 2026-07-17")).toBeInTheDocument();
    expect(screen.getByText(/action_ready 0 · watch_only 1 · 未自动映射条件 1/)).toBeInTheDocument();
    expect(screen.getByText(/计划仍须人工确认/)).toBeInTheDocument();
  });

  it("shows that an AI-approved weekly plan is active in shadow without implying auto-trading", async () => {
    const weeklyPlan = makePlan({
      source_horizon: "weekly",
      source_report_id: "weekly_588870_20260717",
      source_valid_until: "2026-07-24T07:30:00+00:00",
      review_due_at: "2026-07-24T07:30:00+00:00",
      automation_policy: {
        activation_mode: "autonomous",
        activated_by: "autopilot",
        trade_execution: "forbidden",
        trigger_type: "weekly_monitoring_bundle",
      },
    });
    const profile = makeProfile("active", [makeVersion(2, "active", weeklyPlan)]);
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({ profiles: [profile] });
    vi.mocked(api.getPortfolioMonitor).mockResolvedValue(profile);

    renderPanel();

    expect(await screen.findByText(/AI 已完成证据门禁判断并自动启用 shadow 监测/)).toBeInTheDocument();
    expect(screen.getByText(/所有进退仍由人工决定/)).toBeInTheDocument();
    expect(screen.queryByText(/该计划仍须人工确认后启用/)).not.toBeInTheDocument();
  });

  it("shows a masked semantic explanation for the current price-volume regime", async () => {
    const profile = makeProfile();
    if (!profile.last_quote) throw new Error("fixture must include a quote");
    profile.last_quote.price_volume = {
      status: "ready",
      regime: "bearish_expansion",
      volume_state: "expanded",
      volume_ratio: 3.11,
      baseline_samples: 10,
      three_bar_return_bps: -196.52,
      latest_return_bps: -34.25,
      close_location: 0.1429,
      accelerated_decline: false,
      reason_codes: ["bearish_expansion", "volume_expanded"],
      interpretation: {
        bias: "bearish",
        meaning: "价格下行且成交量显著高于同时间基准，主动卖盘占优。",
        risk: "单根放量也可能包含恐慌换手。",
        next_confirmation: "观察后续K线是否止跌并收回支撑位。",
        confidence: "high",
      },
    };
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({ profiles: [profile] });
    vi.mocked(api.getPortfolioMonitor).mockResolvedValue(profile);

    renderPanel();

    const trigger = await screen.findByRole("button", { name: "下跌放量，查看量价含义" });
    fireEvent.mouseEnter(trigger);
    const tooltip = await screen.findByTestId("price-volume-meaning-tooltip");
    expect(tooltip).toHaveClass("backdrop-blur-md");
    expect(within(tooltip).getByText(/当前偏向：/)).toBeInTheDocument();
    expect(within(tooltip).getByText("偏空")).toBeInTheDocument();
    expect(within(tooltip).getByText(/主动卖盘占优/)).toBeInTheDocument();
    expect(within(tooltip).getByText(/不会自动改写报告点位或触发交易/)).toBeInTheDocument();
  });

  it("enables autonomous monitoring once while keeping the runtime in shadow", async () => {
    const user = userEvent.setup();
    const disabled = await api.getPortfolioMonitoringAutopilot();
    const enabled = { ...disabled, enabled: true, selected_symbols: ["588870.SH"], revision: 2 };
    vi.mocked(api.getPortfolioMonitoringAutopilot)
      .mockResolvedValueOnce(disabled)
      .mockResolvedValue(enabled);
    vi.mocked(api.configurePortfolioMonitoringAutopilot).mockResolvedValue(enabled);

    renderPanel(
      [{ name: "科创50指", code: "588870", symbol: "588870.SH", quantity: 2100 }],
      new Set(["588870.SH"]),
    );
    const toggle = await screen.findByRole("switch", { name: "开启自主监控" });
    await user.click(toggle);

    expect(api.configurePortfolioMonitoringAutopilot).toHaveBeenCalledWith({
      enabled: true,
      selected_symbols: ["588870.SH"],
      change_source: "user_toggle",
      daily_close_enabled: true,
      delivery_target_id: null,
      runtime_mode: "shadow",
    });
    expect(await screen.findByRole("switch", { name: "关闭自主监控" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("submits only eligible A-share symbols while preserving mixed-market selections", async () => {
    const user = userEvent.setup();
    const disabled = await api.getPortfolioMonitoringAutopilot();
    const enabled = { ...disabled, enabled: true, selected_symbols: ["588870.SH"], revision: 2 };
    vi.mocked(api.getPortfolioMonitoringAutopilot)
      .mockResolvedValueOnce(disabled)
      .mockResolvedValue(enabled);
    vi.mocked(api.configurePortfolioMonitoringAutopilot).mockResolvedValue(enabled);

    renderPanel([
      { name: "科创50指", code: "588870", symbol: "588870.SH", quantity: 2100 },
      { name: "苹果", code: "AAPL", symbol: "AAPL.US", quantity: 10 },
    ], new Set(["588870.SH", "AAPL.US"]));

    const unsupported = await screen.findByLabelText("自主监控暂不支持的标的");
    expect(unsupported).toHaveTextContent("AAPL");
    expect(unsupported).toHaveTextContent("仍保留在持仓矩阵选择中");
    expect(screen.getByText(/自主服务可纳入 1 只/)).toBeInTheDocument();

    await user.click(screen.getByRole("switch", { name: "开启自主监控" }));

    expect(api.configurePortfolioMonitoringAutopilot).toHaveBeenCalledWith({
      enabled: true,
      selected_symbols: ["588870.SH"],
      change_source: "user_toggle",
      daily_close_enabled: true,
      delivery_target_id: null,
      runtime_mode: "shadow",
    });
    await waitFor(() => {
      expect(api.listPortfolioMonitorReportCandidates).toHaveBeenCalledWith("588870.SH");
      expect(api.listPortfolioMonitorReportCandidates).toHaveBeenCalledWith("AAPL.US");
    });
  });

  it("closes autonomous monitoring when only unsupported symbols remain selected", async () => {
    const toastSuccess = vi.spyOn(toast, "success");
    const initial = {
      ...await api.getPortfolioMonitoringAutopilot(),
      enabled: true,
      selected_symbols: ["588870.SH"],
    };
    const disabled = { ...initial, enabled: false, selected_symbols: [], revision: 2 };
    vi.mocked(api.getPortfolioMonitoringAutopilot).mockResolvedValue(initial);
    vi.mocked(api.configurePortfolioMonitoringAutopilot).mockResolvedValue(disabled);

    const holdings = [
      { name: "科创50指", code: "588870", symbol: "588870.SH", quantity: 2100 },
      { name: "苹果", code: "AAPL", symbol: "AAPL.US", quantity: 10 },
    ];
    const view = renderPanel(holdings, new Set(["588870.SH"]));
    expect(await screen.findByRole("switch", { name: "关闭自主监控" })).toBeInTheDocument();

    view.rerender(
      <PortfolioMonitorPanel
        holdings={holdings}
        selectedSymbols={new Set(["AAPL.US"])}
        selectionRevision={1}
      />,
    );

    await waitFor(() => expect(api.configurePortfolioMonitoringAutopilot).toHaveBeenCalledWith(expect.objectContaining({
      enabled: false,
      selected_symbols: [],
      change_source: "holding_selection",
    })));
    expect(await screen.findByRole("switch", { name: "开启自主监控" })).toBeDisabled();
    expect(screen.getByLabelText("自主监控暂不支持的标的")).toHaveTextContent("AAPL");
    expect(toastSuccess).toHaveBeenCalledWith("所选标的暂不支持自主监控，自主监控已自动关闭。");
  });

  it("does not resubmit an active scope when an unsupported symbol is added", async () => {
    const initial = {
      ...await api.getPortfolioMonitoringAutopilot(),
      enabled: true,
      selected_symbols: ["588870.SH"],
    };
    vi.mocked(api.getPortfolioMonitoringAutopilot).mockResolvedValue(initial);
    const holdings = [
      { name: "科创50指", code: "588870", symbol: "588870.SH", quantity: 2100 },
      { name: "苹果", code: "AAPL", symbol: "AAPL.US", quantity: 10 },
    ];
    const view = renderPanel(holdings, new Set(["588870.SH"]));
    expect(await screen.findByRole("switch", { name: "关闭自主监控" })).toBeInTheDocument();
    vi.mocked(api.configurePortfolioMonitoringAutopilot).mockClear();

    view.rerender(
      <PortfolioMonitorPanel
        holdings={holdings}
        selectedSymbols={new Set(["588870.SH", "AAPL.US"])}
        selectionRevision={1}
      />,
    );

    expect(await screen.findByLabelText("自主监控暂不支持的标的")).toHaveTextContent("AAPL");
    await act(async () => { await Promise.resolve(); });
    expect(api.configurePortfolioMonitoringAutopilot).not.toHaveBeenCalled();
  });

  it("uses the existing holding-matrix selection without rendering a second selector", async () => {
    renderPanel([
      { name: "科创50指", code: "588870", symbol: "588870.SH", quantity: 2100 },
      { name: "军工ETF", code: "512660", symbol: "512660.SH", quantity: 3000 },
    ], new Set(["588870.SH"]));

    const toggle = await screen.findByRole("switch", { name: "开启自主监控" });
    expect(toggle).toBeEnabled();
    expect(screen.getByText(/当前已设普通监控 1 只、AI 自主 1 只 \/ 共 2 只；自主服务可纳入 1 只：科创50指/)).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(api.configurePortfolioMonitoringAutopilot).not.toHaveBeenCalled();
  });

  it("automatically syncs holding-matrix changes while autonomous monitoring is active", async () => {
    const initial = {
      ...await api.getPortfolioMonitoringAutopilot(),
      enabled: true,
      selected_symbols: ["588870.SH"],
    };
    const saved = { ...initial, selected_symbols: ["588870.SH", "512660.SH"], revision: 2 };
    vi.mocked(api.getPortfolioMonitoringAutopilot).mockResolvedValue(initial);
    vi.mocked(api.configurePortfolioMonitoringAutopilot).mockResolvedValue(saved);

    const holdings = [
      { name: "科创50指", code: "588870", symbol: "588870.SH", quantity: 2100 },
      { name: "军工ETF", code: "512660", symbol: "512660.SH", quantity: 3000 },
    ];
    const view = renderPanel(holdings, new Set(["588870.SH"]));
    expect(await screen.findByRole("switch", { name: "关闭自主监控" })).toBeInTheDocument();

    view.rerender(
      <PortfolioMonitorPanel
        holdings={holdings}
        selectedSymbols={new Set(["588870.SH", "512660.SH"])}
        selectionRevision={1}
      />,
    );

    await waitFor(() => expect(api.configurePortfolioMonitoringAutopilot).toHaveBeenCalledWith(expect.objectContaining({
      enabled: true,
      selected_symbols: ["512660.SH", "588870.SH"],
      change_source: "holding_selection",
    })));
    expect(await screen.findByText("已同步 2 只")).toBeInTheDocument();
  });

  it("automatically closes autonomous monitoring when the holding-matrix selection is cleared", async () => {
    const initial = {
      ...await api.getPortfolioMonitoringAutopilot(),
      enabled: true,
      selected_symbols: ["588870.SH"],
    };
    const disabled = { ...initial, enabled: false, selected_symbols: [], revision: 2 };
    vi.mocked(api.getPortfolioMonitoringAutopilot).mockResolvedValue(initial);
    vi.mocked(api.configurePortfolioMonitoringAutopilot).mockResolvedValue(disabled);
    const holdings = [{ name: "科创50指", code: "588870", symbol: "588870.SH", quantity: 2100 }];
    const view = renderPanel(holdings, new Set(["588870.SH"]));
    expect(await screen.findByRole("switch", { name: "关闭自主监控" })).toBeInTheDocument();

    view.rerender(
      <PortfolioMonitorPanel
        holdings={holdings}
        selectedSymbols={new Set()}
        selectionRevision={1}
      />,
    );

    await waitFor(() => expect(api.configurePortfolioMonitoringAutopilot).toHaveBeenCalledWith(expect.objectContaining({
      enabled: false,
      selected_symbols: [],
      change_source: "holding_selection",
    })));
    expect(await screen.findByRole("switch", { name: "开启自主监控" })).toBeDisabled();
  });

  it("does not let an old page snapshot overwrite an enabled server scope", async () => {
    const enabled = {
      ...await api.getPortfolioMonitoringAutopilot(),
      enabled: true,
      selected_symbols: ["588870.SH"],
    };
    vi.mocked(api.getPortfolioMonitoringAutopilot).mockResolvedValue(enabled);

    renderPanel(
      [{ name: "科创50指", code: "588870", symbol: "588870.SH", quantity: 2100 }],
      new Set(),
    );

    expect(await screen.findByRole("switch", { name: "关闭自主监控" })).toBeInTheDocument();
    await act(async () => { await Promise.resolve(); });
    expect(api.configurePortfolioMonitoringAutopilot).not.toHaveBeenCalled();
    expect(await screen.findByRole("switch", { name: "关闭自主监控" })).toHaveAttribute("aria-checked", "true");
  });

  it("shows holding names in autonomous runs and explains that manual drafts share the active plan", async () => {
    const enabled = {
      ...await api.getPortfolioMonitoringAutopilot(),
      enabled: true,
      selected_symbols: ["588870.SH"],
    };
    vi.mocked(api.getPortfolioMonitoringAutopilot).mockResolvedValue(enabled);
    vi.mocked(api.listPortfolioMonitoringAutopilotRuns).mockResolvedValue({
      runs: [{
        trigger_id: "run-1",
        symbol: "588870.SH",
        trigger_type: "holdings_changed",
        status: "queued",
        payload: {},
        created_at: "2026-07-16T14:25:06Z",
      }],
    });
    vi.mocked(api.listPortfolioMonitorRecommendations).mockResolvedValue({
      recommendations: [{
        recommendation_id: "recommendation-1",
        symbol: "588870.SH",
        status: "ready",
        action: "observe",
        current_price: 1.162,
        confidence: "high",
        valid_until: "2026-07-16T15:00:00+08:00",
        feedback_status: "pending",
        created_at: "2026-07-16T14:25:06Z",
        trade_execution: "forbidden",
      }],
    });

    renderPanel(
      [{ name: "科创50指", code: "588870", symbol: "588870.SH", quantity: 2100 }],
      new Set(["588870.SH"]),
    );

    expect(await screen.findByLabelText("科创50指（588870） · holdings_changed")).toBeInTheDocument();
    expect((await screen.findAllByText("科创50指（588870）")).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/蓝色普通监控标的在这里选择报告并生成草案/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "手动生成覆盖草案" })).toBeInTheDocument();
  });

  it("keeps a selected autonomous holding visible while its first monitor profile is being built", async () => {
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({ profiles: [] });
    vi.mocked(api.getPortfolioMonitoringAutopilot).mockResolvedValue({
      config_id: "default",
      enabled: true,
      selected_symbols: ["000651.SZ"],
      activation_mode: "autonomous",
      research_policy: "if_needed",
      trigger_types: [
        "report_ready", "holdings_changed", "scheduled_close", "approaching",
        "invalidated", "material_evidence_changed",
      ],
      daily_close_enabled: true,
      delivery_target_id: null,
      runtime_mode: "shadow",
      revision: 2,
      automatic_trading: "forbidden",
    });
    vi.mocked(api.listPortfolioMonitoringAutopilotRuns).mockResolvedValue({
      runs: [{
        trigger_id: "run-gree-building",
        symbol: "000651.SZ",
        trigger_type: "holdings_changed",
        status: "running",
        payload: { holding_name: "格力电器" },
        planner_job_id: "planner-gree-building",
        created_at: "2026-07-22T03:30:56Z",
        build_state: {
          status: "building",
          stage: "structural_report_refresh_requested",
          stage_label: "刷新结构化研究",
          progress_percent: 50,
          planner_status: "researching",
          item_status: "researching",
          attempt: 1,
          profile_id: null,
          plan_version: null,
          updated_at: "2026-07-22T03:31:00Z",
          terminal: false,
          self_repair: {
            policy: "bounded",
            infrastructure_retry_limit: 1,
            infrastructure_retries_used: 0,
            agent_iteration_limit: 0,
            agent_token_budget: 0,
            strategy: "verified_market_first_no_model",
            full_report_retry_enabled: false,
            circuit_open: false,
            token_spend_allowed: false,
          },
        },
      }],
    });

    renderPanel(
      [{ name: "格力电器", code: "000651", symbol: "000651.SZ", quantity: 100 }],
      new Set(["000651.SZ"]),
    );

    expect(await screen.findByRole("article", {
      name: "格力电器 000651 自动监控准备中",
    })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "格力电器 000651，正在建档" })).toBeInTheDocument();
    expect(screen.getByText("已纳入 AI 自主监控范围")).toBeInTheDocument();
    expect(screen.getByText(/不会假装已经开始行情监控/)).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "监控档案建立进度" })).toHaveAttribute("aria-valuenow", "50");
    expect(screen.getAllByText("刷新结构化研究").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/多周期量价结构引擎重算候选/)).toBeInTheDocument();
  });

  it("shows the active AI decision brief and records a dynamic choice", async () => {
    const user = userEvent.setup();
    const profile = makeProfile("active", [makeVersion(1, "active", makePlan())]);
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({ profiles: [profile] });
    const card: MonitorTargetMonitoringCard = {
      symbol: "588870.SH",
      name: "科创50指",
      profile_id: profile.profile_id,
      profile_status: "active",
      build_state: {
        status: "active",
        stage: "ready",
        stage_label: "监控档案已建立",
        progress_percent: 100,
        attempt: 1,
        terminal: true,
        self_repair: {
          policy: "bounded",
          infrastructure_retry_limit: 1,
          infrastructure_retries_used: 0,
          agent_iteration_limit: 2,
          agent_token_budget: 12000,
          strategy: "continuity_then_multi_method",
          full_report_retry_enabled: false,
          circuit_open: false,
          token_spend_allowed: false,
        },
      },
      blockers: [],
      continuity: { status: "continuous" },
      level_summary: [],
      volume_gate: { status: "ready" },
      self_repair: {},
      decision_id: "decision-1234567890",
      decision_revision: 1,
      evidence_fingerprint: "a".repeat(64),
      decision_brief: {
        headline: "支撑测试中，尚未确认失效",
        market_state: "testing_support",
        risk_level: "medium",
        risk_direction: "downside",
        recommended_choice_id: "wait_confirmation",
        recommended_action: "observe",
        summary: "AI建议：等待确认。触达点位本身不是买卖指令。",
        why_now: ["价格已进入支撑区。", "日线尚未确认失效。", "量价确认不足。"],
        counter_evidence: ["仍可能属于正常波动。"],
        next_confirmation: "收复S1上沿并通过量价门禁。",
        invalidation: "日线结构变化后重算。",
        data_status: "verified",
        confidence: "high",
        choices: [
          { choice_id: "wait_confirmation", label: "等待确认", description: "等待完成K线。", recommended: true },
          { choice_id: "inspect_structural_risk", label: "查看结构风险", description: "展开风险。", recommended: false },
        ],
      },
      risk_assessment: {
        risk_level: "medium",
        risk_direction: "downside",
        risk_probability: null,
        risk_impact: "medium",
        estimated_impact_pct: 0.05,
        estimated_impact_amount: 500,
        data_confidence: "high",
      },
      level_ladder: {
        support: [{ candidate_id: "s1", role: "S1", lower: 2.3, upper: 2.4, score: 82 }],
        resistance: [{ candidate_id: "r1", role: "R1", lower: 2.8, upper: 2.9, score: 78 }],
      },
      action_playbook: {
        do_now: "等待确认",
        why: "尚未完成确认",
        if_holds: "评估机会草稿",
        if_breaks: "等待日线确认",
        do_not: "不要机械加仓",
        review_deadline: "下一根完成K线",
        eligible_draft_types: [],
      },
      available_choices: [
        { choice_id: "wait_confirmation", label: "等待确认", description: "等待完成K线。", recommended: true },
        { choice_id: "inspect_structural_risk", label: "查看结构风险", description: "展开风险。", recommended: false },
      ],
      selected: true,
    };
    vi.mocked(api.listPortfolioMonitoringTargets).mockResolvedValue({ targets: [card] });
    const choose = vi.spyOn(api, "choosePortfolioMonitoringDecision").mockResolvedValue({ status: "recorded" });

    renderPanel([{ name: "科创50指", code: "588870", symbol: "588870.SH", quantity: 2100 }]);

    expect(await screen.findByLabelText("科创50指 AI 决策摘要")).toBeInTheDocument();
    expect(screen.getByText("支撑测试中，尚未确认失效")).toBeInTheDocument();
    expect(screen.getByText("不输出伪精确概率")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "AI 推荐 · 等待确认" }));
    await waitFor(() => expect(choose).toHaveBeenCalledWith(
      card.decision_id,
      "wait_confirmation",
      expect.objectContaining({ evidence_fingerprint: card.evidence_fingerprint }),
    ));
  });

  it("limits the run summary to the current scope and explains planner gate failures", async () => {
    const enabled = {
      ...await api.getPortfolioMonitoringAutopilot(),
      enabled: true,
      selected_symbols: ["588870.SH"],
    };
    vi.mocked(api.getPortfolioMonitoringAutopilot).mockResolvedValue(enabled);
    vi.mocked(api.listPortfolioMonitoringAutopilotRuns).mockResolvedValue({
      runs: [
        {
          trigger_id: "run-blocked-current",
          symbol: "588870.SH",
          trigger_type: "material_evidence_changed",
          status: "blocked",
          payload: { holding_name: "科创50指" },
          created_at: "2026-07-16T15:45:06Z",
          blocked_reasons: ["planner_validation_failed"],
          validation_errors: [
            "watch_scenarios[0] mapped source conditions must have an executable condition",
          ],
          detail_error: "watch_scenarios[0] mapped source conditions must have an executable condition",
        },
        {
          trigger_id: "run-old-outside-scope",
          symbol: "600036.SH",
          trigger_type: "report_ready",
          status: "completed",
          payload: { holding_name: "招商银行" },
          created_at: "2026-07-16T14:25:06Z",
        },
      ],
    });

    renderPanel(
      [{ name: "科创50指", code: "588870", symbol: "588870.SH", quantity: 2100 }],
      new Set(["588870.SH"]),
    );

    expect(await screen.findByText("范围内记录 1 条 · 已显示 1 条")).toBeInTheDocument();
    expect(screen.getByText("报告条件没有完整映射成可执行观察条件，本次没有启用。")).toBeInTheDocument();
    expect(screen.queryByText("招商银行")).not.toBeInTheDocument();
  });

  it("expands every automatic run in the current scope and links to the holding selector", async () => {
    const user = userEvent.setup();
    const enabled = {
      ...await api.getPortfolioMonitoringAutopilot(),
      enabled: true,
      selected_symbols: ["588870.SH"],
    };
    vi.mocked(api.getPortfolioMonitoringAutopilot).mockResolvedValue(enabled);
    vi.mocked(api.listPortfolioMonitoringAutopilotRuns).mockResolvedValue({
      runs: [
        "report_ready",
        "holdings_changed",
        "scheduled_close",
        "approaching",
        "invalidated",
        "material_evidence_changed",
      ].map((triggerType, index) => ({
        trigger_id: `run-${index + 1}`,
        symbol: "588870.SH",
        trigger_type: triggerType as "report_ready" | "holdings_changed" | "scheduled_close" | "approaching" | "invalidated" | "material_evidence_changed",
        status: "completed",
        payload: { holding_name: "科创50指" },
        created_at: `2026-07-16T1${index}:45:06Z`,
      })),
    });

    renderPanel(
      [{ name: "科创50指", code: "588870", symbol: "588870.SH", quantity: 2100 }],
      new Set(["588870.SH"]),
    );

    expect(await screen.findByText("范围内记录 6 条 · 已显示 2 条")).toBeInTheDocument();
    expect(screen.getByLabelText("最近自动运行列表")).not.toHaveTextContent("收盘复核");
    expect(screen.getByRole("link", { name: "去持仓矩阵选择" })).toHaveAttribute("href", "#portfolio-holdings");

    await user.click(screen.getByRole("button", { name: "查看全部 6 条" }));

    expect(screen.getByText("范围内记录 6 条 · 已显示 6 条")).toBeInTheDocument();
    expect(screen.getByLabelText("最近自动运行列表")).toHaveTextContent("收盘复核");
    expect(screen.getByRole("button", { name: "收起，仅看最新 2 条" })).toHaveAttribute("aria-expanded", "true");
  });

  it("shows holding names in recent monitoring events", async () => {
    vi.mocked(api.listPortfolioMonitorEvents).mockResolvedValue({
      events: [{
        event_id: "event-recovered-1",
        profile_id: "profile-1",
        symbol: "588870.SH",
        plan_version: 1,
        kind: "data_source_recovered",
        status: "resolved",
        severity: "info",
        title: "588870.SH 数据源已恢复",
        summary: "行情数据已恢复。",
        facts: {},
        first_seen_at: "2026-07-16T06:21:16Z",
      }],
    });

    renderPanel([{ name: "科创50指", code: "588870", symbol: "588870.SH", quantity: 2100 }]);

    expect(await screen.findByText("科创50指（588870） 数据源已恢复")).toBeInTheDocument();
    expect(screen.getByText("科创50指（588870）")).toBeInTheDocument();
  });

  it("shows only the latest two events by default and expands or collapses the full list", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listPortfolioMonitorEvents).mockResolvedValue({
      events: [
        {
          event_id: "event-latest",
          profile_id: "profile-1",
          symbol: "588870.SH",
          plan_version: 1,
          kind: "data_source_recovered",
          status: "resolved",
          severity: "info",
          title: "最新事件标题",
          summary: "最新事件摘要",
          facts: {},
          first_seen_at: "2026-07-16T08:00:00Z",
        },
        {
          event_id: "event-second",
          profile_id: "profile-1",
          symbol: "588870.SH",
          plan_version: 1,
          kind: "data_source_unavailable",
          status: "resolved",
          severity: "warning",
          title: "第二条事件标题",
          summary: "第二条事件摘要",
          facts: {},
          first_seen_at: "2026-07-16T07:00:00Z",
        },
        {
          event_id: "event-oldest",
          profile_id: "profile-1",
          symbol: "588870.SH",
          plan_version: 1,
          kind: "data_source_unavailable",
          status: "resolved",
          severity: "warning",
          title: "折叠中的旧事件",
          summary: "折叠中的旧事件摘要",
          facts: {},
          first_seen_at: "2026-07-16T06:00:00Z",
        },
      ],
    });

    renderPanel([{ name: "科创50指", code: "588870", symbol: "588870.SH", quantity: 2100 }]);

    expect(await screen.findByText("最新事件摘要")).toBeInTheDocument();
    expect(screen.getByText("第二条事件摘要")).toBeInTheDocument();
    expect(screen.getByLabelText("最近事件列表")).not.toHaveTextContent("折叠中的旧事件摘要");

    await user.click(screen.getByRole("button", { name: "展开全部 3 条" }));
    expect(screen.getByLabelText("最近事件列表")).toHaveTextContent("折叠中的旧事件摘要");
    expect(screen.getByRole("button", { name: "收起，仅看最新 2 条" })).toHaveAttribute("aria-expanded", "true");

    await user.click(screen.getByRole("button", { name: "收起，仅看最新 2 条" }));
    expect(screen.getByLabelText("最近事件列表")).not.toHaveTextContent("折叠中的旧事件摘要");
  });

  it("keeps numeric blanks as drafts, blocks activation, and protects unsaved close", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const saveAndActivate = vi.spyOn(api, "saveAndActivatePortfolioMonitorPlan");
    const user = userEvent.setup();
    renderPanel();

    const card = await screen.findByRole("article", { name: "588870.SH 588870 监控标的" });
    await user.click(within(card).getByRole("button", { name: "计划与审核" }));
    const drawer = await screen.findByRole("dialog", { name: "588870.SH 监控计划" });
    const thresholdInputs = within(drawer).getAllByLabelText("价格向上突破主要阈值");
    fireEvent.change(thresholdInputs[0], { target: { value: "" } });
    expect(thresholdInputs[0]).toHaveValue(null);
    expect(within(drawer).getByText(/有未保存修改/)).toBeInTheDocument();

    await user.click(within(drawer).getByRole("button", { name: "关闭监控计划" }));
    expect(confirm).toHaveBeenCalledWith("监控计划还有未保存修改，确认放弃并关闭？");
    expect(screen.getByRole("dialog", { name: "588870.SH 监控计划" })).toBeInTheDocument();

    await user.click(within(drawer).getByRole("button", { name: "保存并启用" }));
    expect(await within(drawer).findByRole("alert")).toHaveTextContent("价格向上突破主要阈值不能为空");
    expect(saveAndActivate).not.toHaveBeenCalled();
  });

  it("atomically submits the complete unsaved draft without a preliminary patch", async () => {
    const initialPlan = makePlan();
    const profile = makeProfile("pending_review", [makeVersion(1, "pending_review", initialPlan)]);
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({ profiles: [profile] });
    vi.mocked(api.getPortfolioMonitor).mockResolvedValue(profile);
    const updatePlan = vi.spyOn(api, "updatePortfolioMonitorPlan");
    const saveAndActivate = vi.spyOn(api, "saveAndActivatePortfolioMonitorPlan").mockResolvedValue({
      ...profile,
      status: "active",
      active_plan_version: 1,
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    renderPanel();

    const card = await screen.findByRole("article", { name: "588870.SH 588870 监控标的" });
    await user.click(within(card).getByRole("button", { name: "计划与审核" }));
    const drawer = await screen.findByRole("dialog", { name: "588870.SH 监控计划" });
    fireEvent.change(within(drawer).getByLabelText("接近目标距离（bps）"), { target: { value: "80" } });
    fireEvent.change(within(drawer).getByLabelText("量价缩量阈值"), { target: { value: "0.75" } });
    fireEvent.change(within(drawer).getAllByLabelText("价格向上突破主要阈值")[0], { target: { value: "2.25" } });
    await user.click(within(drawer).getByRole("button", { name: "保存并启用" }));

    const expectedPlan = structuredClone(initialPlan);
    expectedPlan.near_trigger_distance_bps = 80;
    if (expectedPlan.price_volume_policy) expectedPlan.price_volume_policy.contraction_ratio = 0.75;
    expectedPlan.market_rules[0].parameters.threshold = 2.25;
    expect(updatePlan).not.toHaveBeenCalled();
    expect(saveAndActivate).toHaveBeenCalledWith(
      profile.profile_id,
      1,
      expectedPlan,
      profile.profile_revision,
    );
  });

  it("shows running, pending, history versions and a structured pending diff", async () => {
    const activePlan = makePlan({ summary: "当前运行版。" });
    const changedRuleValidUntil = new Date(Date.now() + 120 * 24 * 60 * 60 * 1_000).toISOString();
    const pendingPlan = structuredClone(activePlan);
    Object.assign(pendingPlan, {
      summary: "待审核新版。",
      data_mode: "single_source" as const,
      quote_tier: "active",
      near_trigger_tier: "normal",
      near_trigger_distance_bps: 80,
    });
    if (pendingPlan.price_volume_policy) {
      Object.assign(pendingPlan.price_volume_policy, {
        baseline_sessions: 15,
        min_samples: 8,
        contraction_ratio: 0.7,
        expansion_ratio: 1.8,
        flat_return_bps: 15,
        acceleration_multiplier: 1.4,
      });
    }
    pendingPlan.market_rules[0] = {
      ...pendingPlan.market_rules[0],
      severity: "critical",
      target_intent: "stop_loss",
      target_level: 2,
      alert_cue: "ymca_v1",
      valid_until: changedRuleValidUntil,
      parameters: {
        ...pendingPlan.market_rules[0].parameters,
        clear_hysteresis_bps: 55,
      },
    };
    const activeVersion = makeVersion(1, "active", activePlan);
    const pendingVersion = makeVersion(2, "pending_review", pendingPlan);
    const historyVersion = makeVersion(0, "superseded", makePlan({ summary: "历史版。" }));
    const profile = makeProfile("active", [pendingVersion, activeVersion, historyVersion]);
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({ profiles: [profile] });
    vi.mocked(api.getPortfolioMonitor).mockResolvedValue(profile);
    const user = userEvent.setup();
    renderPanel();

    const card = await screen.findByRole("article", { name: "588870.SH 588870 监控标的" });
    await user.click(within(card).getByRole("button", { name: "计划与审核" }));
    const drawer = await screen.findByRole("dialog", { name: "588870.SH 监控计划" });
    expect(within(drawer).getByRole("button", { name: "运行版 v1" })).toHaveAttribute("aria-pressed", "true");
    expect(within(drawer).getByText("历史版本 1")).toBeInTheDocument();

    await user.click(within(drawer).getByRole("button", { name: "待审核版 v2" }));
    const diff = within(drawer).getByLabelText("运行版 v1 与待审核版 v2 差异");
    expect(diff).toHaveTextContent("常态检查频次");
    expect(diff).toHaveTextContent("每 5 分钟");
    expect(diff).toHaveTextContent("每 1 分钟");
    expect(diff).toHaveTextContent("接近目标距离");
    expect(diff).toHaveTextContent("数据模式");
    expect(diff).toHaveTextContent("单源模式");
    expect(diff).toHaveTextContent("接近目标频次");
    expect(diff).toHaveTextContent("量价分析 · 缩量阈值");
    const expandDiff = within(diff).getByRole("button", { name: /展开全部 \d+ 项差异/ });
    expect(expandDiff).toHaveAttribute("aria-expanded", "false");
    await user.click(expandDiff);
    expect(expandDiff).toHaveAttribute("aria-expanded", "true");
    expect(diff).toHaveTextContent("严重级别");
    expect(diff).toHaveTextContent("目标类型");
    expect(diff).toHaveTextContent("止损点");
    expect(diff).toHaveTextContent("目标层级");
    expect(diff).toHaveTextContent("提醒音效");
    expect(diff).toHaveTextContent("ymca_v1");
    expect(diff).toHaveTextContent("解除回差");
    expect(diff).toHaveTextContent("55");
    expect(diff).toHaveTextContent(changedRuleValidUntil);
  });

  it("reconciles a newly arrived pending version while preserving the selected running version", async () => {
    vi.useFakeTimers();
    const activeVersion = makeVersion(1, "active", makePlan({ summary: "运行版。" }));
    const activeProfile = makeProfile("active", [activeVersion]);
    const pendingVersion = makeVersion(2, "pending_review", makePlan({ summary: "轮询到达的待审核版。" }));
    const refreshedProfile = {
      ...makeProfile("active", [pendingVersion, activeVersion]),
      profile_revision: 9,
    };
    vi.mocked(api.listPortfolioMonitors)
      .mockReset()
      .mockResolvedValueOnce({ profiles: [activeProfile] })
      .mockResolvedValue({ profiles: [refreshedProfile] });
    vi.mocked(api.getPortfolioMonitor).mockResolvedValue(activeProfile);
    const saveAndActivate = vi.spyOn(api, "saveAndActivatePortfolioMonitorPlan").mockResolvedValue({
      ...refreshedProfile,
      status: "active",
      active_plan_version: 2,
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const view = renderPanel();
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      const card = screen.getByRole("article", { name: "588870.SH 588870 监控标的" });
      await act(async () => {
        fireEvent.click(within(card).getByRole("button", { name: "计划与审核" }));
        await vi.advanceTimersByTimeAsync(0);
      });
      const drawer = screen.getByRole("dialog", { name: "588870.SH 监控计划" });
      expect(within(drawer).getByRole("button", { name: "运行版 v1" })).toHaveAttribute("aria-pressed", "true");

      await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
      expect(within(drawer).getByRole("button", { name: "运行版 v1" })).toHaveAttribute("aria-pressed", "true");
      const pendingButton = within(drawer).getByRole("button", { name: "待审核版 v2" });
      fireEvent.click(pendingButton);
      fireEvent.click(within(drawer).getByRole("button", { name: "保存并启用" }));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      expect(saveAndActivate).toHaveBeenCalledWith(
        refreshedProfile.profile_id,
        2,
        pendingVersion.plan,
        9,
      );
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("keeps the detailed plan open when the five-second list poll returns summaries only", async () => {
    vi.useFakeTimers();
    const activeVersion = makeVersion(1, "active", makePlan({ summary: "详情接口返回的运行计划。" }));
    const detail = makeProfile("active", [activeVersion]);
    const summary: MonitorProfile = { ...detail, plans: undefined };
    vi.mocked(api.listPortfolioMonitors).mockReset().mockResolvedValue({ profiles: [summary] });
    vi.mocked(api.getPortfolioMonitor).mockResolvedValue(detail);
    const view = renderPanel();
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      const card = screen.getByRole("article", { name: "588870.SH 588870 监控标的" });
      expect(within(card).queryByRole("button", { name: "调整计划" })).not.toBeInTheDocument();
      await act(async () => {
        fireEvent.click(within(card).getByRole("button", { name: "计划与审核" }));
        await vi.advanceTimersByTimeAsync(0);
      });
      const drawer = screen.getByRole("dialog", { name: "588870.SH 监控计划" });
      expect(within(drawer).getByText("详情接口返回的运行计划。")).toBeInTheDocument();
      expect(within(drawer).getByRole("button", { name: "AI 重新分析" })).toBeInTheDocument();

      await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });

      expect(within(drawer).getByText("详情接口返回的运行计划。")).toBeInTheDocument();
      expect(within(drawer).queryByText("当前尚无可查看的监控计划。")).not.toBeInTheDocument();
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("explains the 09:35 first check during a trading-day preopen", async () => {
    const profile = makeProfile("active", [makeVersion(1, "active", makePlan())]);
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({ profiles: [profile] });
    vi.mocked(api.getPortfolioMonitoringStatus).mockResolvedValue(makeStatus({
      runtime: {
        enabled: true,
        running: true,
        leader: true,
        mode: "shadow",
        calendar: {
          mode: "cached_exchange_calendar",
          market_date: "2026-07-16",
          is_trading_day: true,
          session: "preopen",
          open: false,
        },
      },
    }));
    renderPanel();

    const heartbeat = await screen.findByLabelText("588870.SH 监控心跳");
    expect(heartbeat).toHaveTextContent("盘前准备 · 9:35 首轮检查");
    expect(heartbeat).toHaveTextContent("09:00 发送盘前提示");
  });

  it("requires a confirmation that names the active count before stopping globally", async () => {
    vi.mocked(api.getPortfolioMonitoringStatus).mockResolvedValue(makeStatus({ active_profiles: 3 }));
    const configure = vi.spyOn(api, "configurePortfolioMonitoringRuntime");
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderPanel();

    await userEvent.setup().click(await screen.findByRole("button", { name: "停止监控服务" }));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("当前有 3 个已启用标的"));
    expect(configure).not.toHaveBeenCalled();
  });

  it("sends the revision in both If-Match and the atomic request body", async () => {
    const plan = makePlan();
    const fetchRequest = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));

    await api.saveAndActivatePortfolioMonitorPlan("profile/with-slash", 3, plan, 7);

    expect(fetchRequest).toHaveBeenCalledWith(
      "/portfolio/monitors/profile%2Fwith-slash/plans/3/save-and-activate",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ "if-match": "7" }),
        body: JSON.stringify({ plan, expected_revision: 7 }),
      }),
    );
  });

  it("declares a disconnect after two failed polls, freezes effects, and reports recovery", async () => {
    vi.useFakeTimers();
    const activeVersion = makeVersion(1, "active", makePlan());
    const profile = makeProfile("active", [activeVersion]);
    const listProfiles = vi.mocked(api.listPortfolioMonitors);
    const getStatus = vi.mocked(api.getPortfolioMonitoringStatus);
    listProfiles
      .mockReset()
      .mockResolvedValueOnce({ profiles: [profile] })
      .mockRejectedValueOnce(new Error("offline"))
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue({ profiles: [profile] });
    getStatus
      .mockReset()
      .mockResolvedValueOnce(makeStatus())
      .mockRejectedValueOnce(new Error("offline"))
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue(makeStatus());
    const view = renderPanel();
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      const card = screen.getByRole("article", { name: "588870.SH 588870 监控标的" });

      await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
      expect(screen.getByRole("alert")).toHaveTextContent("页面与监控服务断联");
      expect(within(card).getByLabelText("588870.SH 监控心跳")).toHaveTextContent("页面与监控服务断联");
      expect(within(card).getByLabelText("588870.SH 价格监控概览")).toHaveAttribute("data-boost-direction", "none");
      expect(within(card).getByLabelText("588870.SH 下次检查倒计时")).toHaveTextContent("--");

      await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
      expect(screen.getByText(/已重新连接监控服务/)).toBeInTheDocument();
      expect(screen.queryByText(/页面与监控服务断联，已停止倒计时/)).not.toBeInTheDocument();
      expect(within(card).getByLabelText("588870.SH 下次检查倒计时")).not.toHaveTextContent("--");
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("ignores an older successful refresh after two newer sync failures", async () => {
    vi.useFakeTimers();
    const activeVersion = makeVersion(1, "active", makePlan());
    const profile = makeProfile("active", [activeVersion]);
    const staleManualProfiles = deferred<{ profiles: MonitorProfile[] }>();
    vi.mocked(api.listPortfolioMonitors)
      .mockReset()
      .mockResolvedValueOnce({ profiles: [profile] })
      .mockImplementationOnce(() => staleManualProfiles.promise)
      .mockRejectedValue(new Error("offline"));
    vi.mocked(api.getPortfolioMonitoringStatus)
      .mockReset()
      .mockResolvedValueOnce(makeStatus())
      .mockResolvedValueOnce(makeStatus())
      .mockRejectedValue(new Error("offline"));
    const view = renderPanel();
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      fireEvent.click(screen.getByRole("button", { name: "刷新" }));

      await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
      expect(screen.getByRole("alert")).toHaveTextContent("页面与监控服务断联");

      await act(async () => {
        staleManualProfiles.resolve({ profiles: [profile] });
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByRole("alert")).toHaveTextContent("页面与监控服务断联");
      expect(screen.queryByText(/已重新连接监控服务/)).not.toBeInTheDocument();
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("does not reopen a plan drawer when an obsolete detail request resolves", async () => {
    const profile = makeProfile();
    const detailRequest = deferred<MonitorProfile>();
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({ profiles: [profile] });
    vi.mocked(api.getPortfolioMonitor).mockReturnValue(detailRequest.promise);
    const user = userEvent.setup();
    renderPanel();

    const card = await screen.findByRole("article", { name: "588870.SH 588870 监控标的" });
    await user.click(within(card).getByRole("button", { name: "计划与审核" }));
    const drawer = screen.getByRole("dialog", { name: "588870.SH 监控计划" });
    await user.click(within(drawer).getByRole("button", { name: "关闭监控计划" }));
    expect(screen.queryByRole("dialog", { name: "588870.SH 监控计划" })).not.toBeInTheDocument();

    await act(async () => {
      detailRequest.resolve(profile);
      await detailRequest.promise;
    });
    expect(screen.queryByRole("dialog", { name: "588870.SH 监控计划" })).not.toBeInTheDocument();
  });
});
