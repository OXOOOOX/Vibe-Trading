import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import {
  api,
  type MarketCacheRun,
  type PortfolioAnalysisSession,
  type PortfolioMandate,
  type MonitorPlanVersion,
  type PortfolioReview,
} from "@/lib/api";
import { getMarketAnalysisLabel, getMarketAnalysisPhase, Portfolio } from "../Portfolio";
import { PortfolioMonitorEffectsProvider } from "@/components/portfolio/PortfolioMonitorEffectsProvider";

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

function monitorDisplayPlan(
  dataAsOf: string,
): MonitorPlanVersion {
  return {
    profile_id: "profile-price-band",
    version: 1,
    status: "active",
    schema_version: 1,
    plan: {
      schema_version: 1,
      symbol: "588870.SH",
      summary: "价格区间可视化测试。",
      quote_tier: "normal",
      near_trigger_tier: "active",
      near_trigger_distance_bps: 100,
      market_rules: [
        {
          client_rule_id: "price-target-lower",
          kind: "price_cross_below",
          severity: "warning",
          enabled: true,
          target_intent: "add_position",
          target_level: 1,
          parameters: {
            threshold: 2,
            interval: "5m",
            adjustment: "raw",
            confirmation_count: 2,
            cooldown_minutes: 120,
            clear_hysteresis_bps: 30,
          },
          calculation_basis: {
            method: "current_to_stop_midpoint",
            method_label: "现价与止损防线中点",
            formula: "(最新价 + L2 止损点) ÷ 2",
            summary: "取现价 2.100 与 2026-06-03 低点 1.900 的中点，得到 2.000。",
            recommended_value: 2,
            references: [
              { label: "现价", value: 2.1 },
              { label: "2026-06-03 低点", value: 1.9, date: "2026-06-03" },
            ],
          },
        },
        {
          client_rule_id: "price-target-stop-loss",
          kind: "price_cross_below",
          severity: "critical",
          enabled: true,
          target_intent: "stop_loss",
          target_level: 2,
          parameters: {
            threshold: 1.9,
            interval: "5m",
            adjustment: "raw",
            confirmation_count: 2,
            cooldown_minutes: 120,
            clear_hysteresis_bps: 30,
          },
        },
        {
          client_rule_id: "price-target-upper",
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
          calculation_basis: {
            method: "range_upper_with_noise_buffer",
            method_label: "近20日震荡区间上沿 + 波动缓冲",
            formula: "max(近20日最高价, 最新价 × (1 + 日波动缓冲))",
            summary: "2026-06-18 高点 2.200 是近20日震荡区间上沿，高于波动缓冲价，因此取 2.200。",
            recommended_value: 2.2,
            references: [{ label: "震荡区间上沿", value: 2.2, date: "2026-06-18" }],
          },
        },
        {
          client_rule_id: "price-target-upper-2",
          kind: "price_cross_above",
          severity: "warning",
          enabled: true,
          target_intent: "take_profit",
          target_level: 2,
          parameters: {
            threshold: 2.3,
            interval: "5m",
            adjustment: "raw",
            confirmation_count: 2,
            cooldown_minutes: 120,
            clear_hysteresis_bps: 30,
          },
        },
      ],
      news_topics: [],
      fundamental_monitor: { enabled: false },
      hard_valid_until: "2026-10-14T01:00:00Z",
    },
    evidence_manifest: {},
    model_id: "evidence-policy-v1",
    data_as_of: dataAsOf,
    created_at: dataAsOf,
  };
}

function monitorPriceVolumeDisplayPlan(dataAsOf: string): MonitorPlanVersion {
  const version = monitorDisplayPlan(dataAsOf);
  return {
    ...version,
    schema_version: 2,
    plan: {
      ...version.plan,
      schema_version: 2,
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
    },
  };
}

describe("Portfolio market cache", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
    vi.spyOn(api, "getPortfolioReview").mockResolvedValue(review);
    vi.spyOn(api, "getPortfolioMandate").mockResolvedValue(mandate);
    vi.spyOn(api, "listPortfolioDailyRuns").mockResolvedValue({ runs: [] });
    vi.spyOn(api, "listPortfolioMonitors").mockResolvedValue({ profiles: [] });
    vi.spyOn(api, "listPortfolioMonitorEvents").mockResolvedValue({ events: [] });
    vi.spyOn(api, "listPortfolioMonitorDeliveryTargets").mockResolvedValue({ targets: [] });
    vi.spyOn(api, "listPortfolioMonitorReportCandidates").mockImplementation(async (symbol) => ({
      symbol,
      candidates: [],
    }));
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
    vi.spyOn(api, "listPortfolioMonitorRecommendations").mockResolvedValue({ recommendations: [] });
    vi.spyOn(api, "getPortfolioMonitoringStatus").mockResolvedValue({
      enabled_by_config: false,
      effective_mode: "off",
      runtime: { enabled: false, running: false, leader: false, mode: "off", last_tick: null },
      capabilities: { market_rules: "available", automatic_trading: "forbidden" },
      profiles: 0,
      active_profiles: 0,
      events: 0,
      pending_deliveries: 0,
      uncertain_deliveries: 0,
      shadow_suppressed_deliveries: 0,
      blocked_profiles: 0,
      database_size_bytes: 0,
      database_max_bytes: 536870912,
      database_utilization: 0,
      delivery_status_counts: {},
      observation_status_counts: {},
      runtime_health: {
        window_hours: 24,
        tick_count: 0,
        error_tick_count: 0,
        duration_ms: {},
        schedule_lag_ms: {},
        bar_lag_ms: {},
        database_growth_bytes: 0,
        counters: {},
      },
      profile_health: [],
      maintenance: null,
    });
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

    await userEvent.setup().click(await screen.findByRole(
      "button",
      { name: "一键生成今日组合晨会" },
      { timeout: 3000 },
    ));

    expect(api.startPortfolioDailyRun).toHaveBeenCalledWith({ refresh_policy: "ensure_fresh" });
    expect(await screen.findByText("2026-07-13 · queued")).toBeInTheDocument();
  });

  it("selects a holding, creates a monitor draft, and atomically saves and activates it", async () => {
    const target = {
      target_id: "target-1",
      channel: "feishu" as const,
      chat_id: "ou_user",
      chat_type: "p2p" as const,
      session_key: "feishu:ou_user",
      status: "active" as const,
      created_at: "2026-07-14T01:00:00Z",
    };
    vi.mocked(api.listPortfolioMonitorDeliveryTargets).mockResolvedValue({ targets: [target] });
    const plan = {
      schema_version: 3,
      symbol: "588870.SH",
      summary: "基于已校核行情生成的待审核观察草案。",
      quote_tier: "normal" as const,
      near_trigger_tier: "active" as const,
      near_trigger_distance_bps: 100,
      price_volume_policy: {
        enabled: true,
        interval: "5m" as const,
        baseline_method: "same_time_bucket_median" as const,
        baseline_sessions: 10,
        min_samples: 5,
        contraction_ratio: 0.8,
        expansion_ratio: 1.5,
        flat_return_bps: 10,
        acceleration_multiplier: 1.2,
      },
      market_rules: [{
        client_rule_id: "breakout",
        kind: "price_cross_above" as const,
        severity: "warning" as const,
        enabled: true,
        alert_cue: "none" as const,
        parameters: {
          threshold: 2.2,
          interval: "5m" as const,
          adjustment: "raw" as const,
          confirmation_count: 2,
          cooldown_minutes: 120,
          clear_hysteresis_bps: 30,
        },
        valid_until: "2026-10-14T01:00:00Z",
        rationale: "价格突破近期区间后提醒复核。",
        calculation_basis: {
          method: "range_upper_with_noise_buffer",
          method_label: "近20日震荡区间上沿 + 波动缓冲",
          formula: "max(近20日最高价, 最新价 × (1 + 日波动缓冲))",
          summary: "2026-06-18 高点 2.200 是近20日震荡区间上沿，因此推荐 2.200。",
          recommended_value: 2.2,
          references: [{ label: "震荡区间上沿", value: 2.2, date: "2026-06-18" }],
        },
      }, {
        client_rule_id: "breakout-two",
        kind: "price_cross_above" as const,
        severity: "warning" as const,
        enabled: true,
        alert_cue: "none" as const,
        parameters: {
          threshold: 2.4,
          interval: "5m" as const,
          adjustment: "raw" as const,
          confirmation_count: 2,
          cooldown_minutes: 120,
          clear_hysteresis_bps: 30,
        },
        valid_until: "2026-10-14T01:00:00Z",
      }, {
        client_rule_id: "breakdown",
        kind: "price_cross_below" as const,
        severity: "warning" as const,
        enabled: true,
        alert_cue: "none" as const,
        parameters: {
          threshold: 1.9,
          interval: "5m" as const,
          adjustment: "raw" as const,
          confirmation_count: 2,
          cooldown_minutes: 120,
          clear_hysteresis_bps: 30,
        },
        valid_until: "2026-10-14T01:00:00Z",
      }, {
        client_rule_id: "breakdown-two",
        kind: "price_cross_below" as const,
        severity: "critical" as const,
        enabled: true,
        alert_cue: "none" as const,
        parameters: {
          threshold: 1.7,
          interval: "5m" as const,
          adjustment: "raw" as const,
          confirmation_count: 2,
          cooldown_minutes: 120,
          clear_hysteresis_bps: 30,
        },
        valid_until: "2026-10-14T01:00:00Z",
      }],
      news_topics: [],
      fundamental_monitor: { enabled: false },
      hard_valid_until: "2026-10-14T01:00:00Z",
    };
    const profile = {
      profile_id: "profile-1",
      symbol: "588870.SH",
      market: "SH",
      instrument_type: "etf" as const,
      status: "pending_review" as const,
      active_plan_version: null,
      profile_revision: 1,
      delivery_target_id: target.target_id,
      input_outdated: false,
      blocked_reasons: [],
      updated_at: "2026-07-14T01:00:00Z",
      plans: [{
        profile_id: "profile-1",
        version: 1,
        status: "pending_review",
        schema_version: 3,
        plan,
        evidence_manifest: {},
        model_id: "evidence-policy-v1",
        data_as_of: "2026-07-14T01:00:00Z",
        created_at: "2026-07-14T01:00:00Z",
      }],
    };
    const create = vi.spyOn(api, "createPortfolioMonitorPlannerJob").mockResolvedValue({
      job_id: "planner-job-1",
      status: "ready",
      requested_symbols: ["588870.SH"],
      report_refs: {},
      research_policy: "if_needed",
      delivery_target_id: target.target_id,
      force_fresh: true,
      cancel_requested: false,
      created_at: "2026-07-14T01:00:00Z",
      started_at: "2026-07-14T01:00:01Z",
      completed_at: "2026-07-14T01:00:02Z",
      items: [{
        symbol: "588870.SH",
        status: "ready",
        report_ref: null,
        report_snapshot_id: null,
        research_snapshot_id: null,
        profile_id: profile.profile_id,
        plan_version: 1,
        blocked_reasons: [],
        validation_errors: [],
        progress: {},
        error: null,
        attempt: 1,
      }],
    });
    let profileState = profile;
    vi.spyOn(api, "getPortfolioMonitor").mockImplementation(async () => profileState);
    const updatePlan = vi.spyOn(api, "updatePortfolioMonitorPlan").mockImplementation(async (
      _profileId,
      _version,
      nextPlan,
    ) => {
      const nextVersion = { ...profileState.plans[0], plan: nextPlan };
      profileState = {
        ...profileState,
        profile_revision: profileState.profile_revision + 1,
        plans: [nextVersion],
      };
      return nextVersion;
    });
    const saveAndActivate = vi.spyOn(api, "saveAndActivatePortfolioMonitorPlan").mockResolvedValue({
      ...profile,
      status: "active",
      active_plan_version: 1,
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<Portfolio />, { wrapper: MemoryRouter });

    const radar = await screen.findByRole("button", { name: "选择 588870.SH 用于 AI 监控" });
    expect(radar).toHaveAttribute("aria-pressed", "false");
    expect(radar).toHaveAttribute("data-monitor-selected", "false");
    await user.click(radar);
    expect(screen.getByRole("button", { name: "取消选择 588870.SH 用于 AI 监控" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "取消选择 588870.SH 用于 AI 监控" })).toHaveAttribute("data-monitor-selected", "true");
    await waitFor(() => expect(JSON.parse(
      localStorage.getItem("vibe-trading:portfolio-monitor-selection:v1") || "{}",
    )).toEqual({ version: 1, symbols: ["588870.SH"] }));
    await user.click(screen.getByRole("button", { name: "生成监控草案" }));
    expect(create).toHaveBeenCalledWith({
      symbols: ["588870.SH"],
      report_refs: {},
      research_policy: "if_needed",
      delivery_target_id: target.target_id,
      force_fresh: true,
    });
    expect(await screen.findByRole("dialog", { name: "588870.SH 监控计划" })).toBeInTheDocument();
    const frequency = screen.getByLabelText("服务器检查频次");
    expect(frequency).toHaveValue("normal");
    const priceVolumeToggle = screen.getByLabelText("启用量价分析");
    expect(priceVolumeToggle).toBeChecked();
    await user.click(priceVolumeToggle);
    expect(priceVolumeToggle).not.toBeChecked();
    await user.click(priceVolumeToggle);
    await user.selectOptions(frequency, "active");
    await user.selectOptions(screen.getAllByLabelText("价格向上突破K线粒度")[0], "1m");
    const upYmcaRule = screen.getByLabelText("上涨 YMCA 规则");
    const downYmcaRule = screen.getByLabelText("下跌 YMCA 规则");
    await user.selectOptions(upYmcaRule, "breakout");
    expect(upYmcaRule).toHaveValue("breakout");
    await user.selectOptions(downYmcaRule, "breakdown");
    expect(downYmcaRule).toHaveValue("breakdown");
    expect(upYmcaRule).toHaveValue("breakout");
    await user.selectOptions(upYmcaRule, "breakout-two");
    await user.selectOptions(downYmcaRule, "breakdown-two");
    expect(upYmcaRule).toHaveValue("breakout-two");
    expect(downYmcaRule).toHaveValue("breakdown-two");
    fireEvent.change(screen.getByLabelText("接近目标距离（bps）"), { target: { value: "80" } });
    fireEvent.change(screen.getByLabelText("量价缩量阈值"), { target: { value: "0.75" } });
    fireEvent.change(screen.getByLabelText("量价放量阈值"), { target: { value: "1.6" } });
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(updatePlan).toHaveBeenCalledWith(
      "profile-1",
      1,
      expect.objectContaining({
        quote_tier: "active",
        near_trigger_distance_bps: 80,
        price_volume_policy: expect.objectContaining({
          enabled: true,
          contraction_ratio: 0.75,
          expansion_ratio: 1.6,
        }),
        market_rules: expect.arrayContaining([
          expect.objectContaining({
            client_rule_id: "breakout",
            alert_cue: "none",
            parameters: expect.objectContaining({ interval: "1m" }),
          }),
          expect.objectContaining({ client_rule_id: "breakout-two", alert_cue: "ymca_v1" }),
          expect.objectContaining({ client_rule_id: "breakdown", alert_cue: "none" }),
          expect.objectContaining({ client_rule_id: "breakdown-two", alert_cue: "ymca_v1" }),
        ]),
      }),
      1,
    ));
    expect(screen.getByLabelText("服务器检查频次")).toHaveValue("active");
    expect(screen.getByRole("button", { name: "取消选择 588870.SH 用于 AI 监控" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "保存并启用" }));
    expect(saveAndActivate).toHaveBeenCalledWith(
      "profile-1",
      1,
      expect.objectContaining({
        quote_tier: "active",
        near_trigger_distance_bps: 80,
      }),
      2,
    );
  }, 10_000);

  it("keeps each monitor radar on or off across page remounts", async () => {
    const user = userEvent.setup();
    const firstRender = render(<Portfolio />, { wrapper: MemoryRouter });

    await user.click(await screen.findByRole("button", { name: "选择 588870.SH 用于 AI 监控" }));
    await waitFor(() => expect(JSON.parse(
      localStorage.getItem("vibe-trading:portfolio-monitor-selection:v1") || "{}",
    )).toEqual({ version: 1, symbols: ["588870.SH"] }));
    firstRender.unmount();
    expect(JSON.parse(
      localStorage.getItem("vibe-trading:portfolio-monitor-selection:v1") || "{}",
    )).toEqual({ version: 1, symbols: ["588870.SH"] });

    const secondRender = render(<Portfolio />, { wrapper: MemoryRouter });
    expect(JSON.parse(
      localStorage.getItem("vibe-trading:portfolio-monitor-selection:v1") || "{}",
    )).toEqual({ version: 1, symbols: ["588870.SH"] });
    const persistedRadar = await screen.findByRole("button", { name: "取消选择 588870.SH 用于 AI 监控" });
    expect(persistedRadar).toHaveAttribute("aria-pressed", "true");
    await user.click(persistedRadar);
    await waitFor(() => expect(JSON.parse(
      localStorage.getItem("vibe-trading:portfolio-monitor-selection:v1") || "{}",
    )).toEqual({ version: 1, symbols: [] }));
    secondRender.unmount();

    render(<Portfolio />, { wrapper: MemoryRouter });
    expect(await screen.findByRole("button", { name: "选择 588870.SH 用于 AI 监控" }))
      .toHaveAttribute("aria-pressed", "false");
  });

  it("hydrates a missing local monitor selection from the server without clearing autopilot", async () => {
    const serverAutopilot = {
      ...await api.getPortfolioMonitoringAutopilot(),
      enabled: true,
      selected_symbols: ["588870.SH"],
    };
    vi.mocked(api.getPortfolioMonitoringAutopilot).mockResolvedValue(serverAutopilot);
    const configure = vi.spyOn(api, "configurePortfolioMonitoringAutopilot").mockResolvedValue(serverAutopilot);

    render(<Portfolio />, { wrapper: MemoryRouter });

    expect(await screen.findByRole("button", { name: "取消选择 588870.SH 用于 AI 监控" }))
      .toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(JSON.parse(
      localStorage.getItem("vibe-trading:portfolio-monitor-selection:v1") || "{}",
    )).toEqual({ version: 1, symbols: ["588870.SH"] }));
    expect(configure).not.toHaveBeenCalled();
  });

  it("keeps an enabled server scope when this page only has a stale empty snapshot", async () => {
    localStorage.setItem("vibe-trading:portfolio-monitor-selection:v1", JSON.stringify({
      version: 1,
      symbols: [],
    }));
    const serverAutopilot = {
      ...await api.getPortfolioMonitoringAutopilot(),
      enabled: true,
      selected_symbols: ["588870.SH"],
    };
    vi.mocked(api.getPortfolioMonitoringAutopilot).mockResolvedValue(serverAutopilot);
    const configure = vi.spyOn(api, "configurePortfolioMonitoringAutopilot");

    render(<Portfolio />, { wrapper: MemoryRouter });

    expect(await screen.findByRole("switch", { name: "关闭自主监控" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await act(async () => { await Promise.resolve(); });
    expect(configure).not.toHaveBeenCalled();
    expect(await screen.findByRole("button", { name: "选择 588870.SH 用于 AI 监控" }))
      .toHaveAttribute("aria-pressed", "false");
    expect(JSON.parse(
      localStorage.getItem("vibe-trading:portfolio-monitor-selection:v1") || "{}",
    )).toEqual({ version: 1, symbols: [] });
  });

  it("closes autopilot when the user explicitly clears the active matrix selection", async () => {
    localStorage.setItem("vibe-trading:portfolio-monitor-selection:v1", JSON.stringify({
      version: 1,
      symbols: ["588870.SH"],
    }));
    const serverAutopilot = {
      ...await api.getPortfolioMonitoringAutopilot(),
      enabled: true,
      selected_symbols: ["588870.SH"],
    };
    const disabledAutopilot = {
      ...serverAutopilot,
      enabled: false,
      selected_symbols: [],
      revision: 2,
    };
    vi.mocked(api.getPortfolioMonitoringAutopilot).mockResolvedValue(serverAutopilot);
    const configure = vi.spyOn(api, "configurePortfolioMonitoringAutopilot").mockResolvedValue(disabledAutopilot);
    const user = userEvent.setup();

    render(<Portfolio />, { wrapper: MemoryRouter });
    await user.click(await screen.findByRole("button", { name: "取消选择 588870.SH 用于 AI 监控" }));

    await waitFor(() => expect(configure).toHaveBeenCalledWith(expect.objectContaining({
      enabled: false,
      selected_symbols: [],
      change_source: "holding_selection",
    })));
    expect(await screen.findByRole("switch", { name: "开启自主监控" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });

  it("removes persisted monitor symbols that are no longer in the portfolio", async () => {
    localStorage.setItem("vibe-trading:portfolio-monitor-selection:v1", JSON.stringify({
      version: 1,
      symbols: ["588870.SH", "REMOVED.SH"],
    }));

    render(<Portfolio />, { wrapper: MemoryRouter });

    expect(await screen.findByRole("button", { name: "取消选择 588870.SH 用于 AI 监控" }))
      .toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(JSON.parse(
      localStorage.getItem("vibe-trading:portfolio-monitor-selection:v1") || "{}",
    )).toEqual({ version: 1, symbols: ["588870.SH"] }));
  });

  it("shows the holding name together with the six-digit code in monitor targets", async () => {
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({
      profiles: [{
        profile_id: "profile-visible-label",
        symbol: "588870.SH",
        market: "SH",
        instrument_type: "etf",
        status: "active",
        active_plan_version: 1,
        profile_revision: 1,
        delivery_target_id: "target-1",
        input_outdated: false,
        blocked_reasons: [],
        updated_at: "2026-07-14T01:00:00Z",
        last_quote_check_at: "2026-07-14T01:01:00Z",
      }],
    });

    render(<Portfolio />, { wrapper: MemoryRouter });

    const card = await screen.findByRole("article", { name: "科创50指 588870 监控标的" });
    expect(within(card).getByText("科创50指")).toBeInTheDocument();
    expect(within(card).getByText("588870")).toBeInTheDocument();
    expect(within(card).getByText("SH")).toBeInTheDocument();
  });

  it("closes a monitor card while keeping its historical event available", async () => {
    let closed = false;
    const profile = {
      profile_id: "profile-close",
      symbol: "588870.SH",
      market: "SH",
      instrument_type: "etf" as const,
      status: "active" as const,
      active_plan_version: 1,
      profile_revision: 1,
      delivery_target_id: "target-1",
      input_outdated: false,
      blocked_reasons: [],
      updated_at: "2026-07-14T01:00:00Z",
    };
    const event = {
      event_id: "event-preserved-after-close",
      profile_id: profile.profile_id,
      symbol: profile.symbol,
      plan_version: 1,
      kind: "market_rule_trigger",
      status: "resolved",
      severity: "warning",
      title: "588870.SH 历史监控事件",
      summary: "这条记录在关闭监控后仍应保留。",
      facts: {},
      first_seen_at: "2026-07-14T02:00:00Z",
      deliveries: [],
    };
    vi.mocked(api.listPortfolioMonitors).mockImplementation(async () => ({
      profiles: [{
        ...profile,
        status: closed ? "closed" as const : "active" as const,
        closed_at: closed ? "2026-07-14T03:00:00Z" : null,
        profile_revision: closed ? 2 : 1,
      }],
    }));
    vi.mocked(api.listPortfolioMonitorEvents).mockResolvedValue({ events: [event] });
    const close = vi.spyOn(api, "closePortfolioMonitor").mockImplementation(async () => {
      closed = true;
      return {
        ...profile,
        status: "closed",
        closed_at: "2026-07-14T03:00:00Z",
        profile_revision: 2,
      };
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<Portfolio />, { wrapper: MemoryRouter });

    const card = await screen.findByRole("article", { name: "科创50指 588870 监控标的" });
    expect(within(card).getByRole("button", { name: "关闭监控" }).parentElement)
      .toHaveClass("items-center", "self-end");
    expect(screen.getByText(event.summary)).toBeInTheDocument();
    await userEvent.setup().click(within(card).getByRole("button", { name: "关闭监控" }));

    await waitFor(() => expect(close).toHaveBeenCalledWith(profile.profile_id));
    await waitFor(() => expect(screen.queryByRole("article", { name: "科创50指 588870 监控标的" })).not.toBeInTheDocument());
    expect(screen.getByText("当前没有正在监控的标的。已关闭卡片已收起，历史事件仍在下方保留。")).toBeInTheDocument();
    expect(screen.getByText(event.summary)).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "查看已关闭 1" }));
    expect(await screen.findByRole("article", { name: "科创50指 588870 监控标的" })).toBeInTheDocument();
    expect(screen.getByText("已关闭")).toBeInTheDocument();
  });

  it("shows the active plan immediately even when a newer pending draft also exists", async () => {
    const activePlan = {
      schema_version: 1,
      symbol: "588870.SH",
      summary: "当前正在运行的监控计划。",
      quote_tier: "normal" as const,
      near_trigger_tier: "active" as const,
      near_trigger_distance_bps: 100,
      market_rules: [{
        client_rule_id: "breakout",
        kind: "price_cross_above" as const,
        severity: "warning" as const,
        enabled: true,
        parameters: {
          threshold: 2.2,
          interval: "5m" as const,
          adjustment: "raw" as const,
          confirmation_count: 2,
          cooldown_minutes: 120,
          clear_hysteresis_bps: 30,
        },
        rationale: "价格突破近期区间后提醒复核。",
        calculation_basis: {
          method: "range_upper_with_noise_buffer",
          method_label: "近20日震荡区间上沿 + 波动缓冲",
          formula: "max(近20日最高价, 最新价 × (1 + 日波动缓冲))",
          summary: "2026-06-18 高点 2.200 是近20日震荡区间上沿，因此推荐 2.200。",
          recommended_value: 2.2,
          references: [{ label: "震荡区间上沿", value: 2.2, date: "2026-06-18" }],
        },
      }],
      news_topics: [],
      fundamental_monitor: { enabled: false },
      hard_valid_until: "2026-10-14T01:00:00Z",
    };
    const profile = {
      profile_id: "profile-active-plan",
      symbol: "588870.SH",
      market: "SH",
      instrument_type: "etf" as const,
      status: "active" as const,
      active_plan_version: 1,
      profile_revision: 3,
      delivery_target_id: "target-1",
      input_outdated: false,
      blocked_reasons: [],
      updated_at: "2026-07-14T01:00:00Z",
      last_quote_check_at: null,
      next_quote_run_at: "2026-07-14T00:55:00Z",
      display_plan: {
        profile_id: "profile-active-plan",
        version: 1,
        status: "active",
        schema_version: 1,
        plan: activePlan,
        evidence_manifest: {},
        model_id: "evidence-policy-v1",
        data_as_of: "2026-07-14T01:00:00Z",
        created_at: "2026-07-14T01:00:00Z",
      },
    };
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({ profiles: [profile] });
    vi.spyOn(api, "getPortfolioMonitor").mockImplementation(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30));
      return {
        ...profile,
        plans: [{
          profile_id: profile.profile_id,
          version: 2,
          status: "pending_review",
          schema_version: 1,
          plan: { ...activePlan, summary: "尚未启用的新草案。" },
          evidence_manifest: {},
          model_id: "evidence-policy-v1",
          created_at: "2026-07-14T01:02:00Z",
        }, {
          profile_id: profile.profile_id,
          version: 1,
          status: "active",
          schema_version: 1,
          plan: activePlan,
          evidence_manifest: {},
          model_id: "evidence-policy-v1",
          created_at: "2026-07-14T01:00:00Z",
        }],
      };
    });

    render(<Portfolio />, { wrapper: MemoryRouter });
    const card = await screen.findByRole("article", { name: "科创50指 588870 监控标的" });
    expect(within(card).getByText("计划已启用")).toBeInTheDocument();
    const heartbeat = within(card).getByLabelText("588870.SH 监控心跳");
    expect(within(heartbeat).getByText("当前未实际监控 · 服务未启动")).toBeInTheDocument();
    expect(within(heartbeat).getByText(/下次检查：等待监控服务启动/)).toBeInTheDocument();
    expect(within(heartbeat).queryByText(/下次预计|分钟[前]/)).not.toBeInTheDocument();
    const summary = within(card).getByLabelText("监控计划 v1 摘要");
    expect(within(summary).getByText("每 5 分钟")).toBeInTheDocument();
    expect(within(summary).getByText("5m K线")).toBeInTheDocument();
    expect(within(summary).getByText("突破点 L1 · 价格向上突破 2.2")).toBeInTheDocument();
    expect(within(summary).getByText("飞书目标待确认")).toBeInTheDocument();
    await userEvent.setup().click(within(card).getByRole("button", { name: "计划与审核" }));

    const planDrawer = screen.getByRole("dialog", { name: "588870.SH 监控计划" });
    expect(planDrawer).toBeInTheDocument();
    expect(within(planDrawer).getByRole("heading", { name: "科创50指" })).toBeInTheDocument();
    expect(within(planDrawer).getByText("588870.SH")).toBeInTheDocument();
    expect(await screen.findByText("当前正在运行的监控计划。")).toBeInTheDocument();
    const pointBasis = within(planDrawer).getByLabelText("价格向上突破 策略推荐点位依据");
    expect(pointBasis).toHaveTextContent("近20日震荡区间上沿 + 波动缓冲");
    expect(pointBasis).toHaveTextContent("2026-06-18 高点 2.200");
    expect(pointBasis).toHaveTextContent("公式：max(近20日最高价, 最新价 × (1 + 日波动缓冲))");
    expect(screen.queryByText("尚未启用的新草案。")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存并启用" })).not.toBeInTheDocument();
  });

  it("does not let a legacy pending draft save a YMCA cue before schema v3", async () => {
    const legacyVersion = {
      ...monitorDisplayPlan("2026-07-14T01:00:00Z"),
      status: "pending_review",
    };
    const profile = {
      profile_id: legacyVersion.profile_id,
      symbol: "588870.SH",
      market: "SH",
      instrument_type: "etf" as const,
      status: "pending_review" as const,
      active_plan_version: null,
      profile_revision: 1,
      delivery_target_id: "target-1",
      input_outdated: false,
      blocked_reasons: [],
      updated_at: "2026-07-14T01:00:00Z",
      display_plan: legacyVersion,
      plans: [legacyVersion],
    };
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({ profiles: [profile] });
    vi.spyOn(api, "getPortfolioMonitor").mockResolvedValue(profile);

    render(<Portfolio />, { wrapper: MemoryRouter });
    const card = await screen.findByRole("article", { name: "科创50指 588870 监控标的" });
    await userEvent.setup().click(within(card).getByRole("button", { name: "计划与审核" }));

    const drawer = await screen.findByRole("dialog", { name: "588870.SH 监控计划" });
    expect(within(drawer).getByLabelText("上涨 YMCA 规则")).toBeDisabled();
    expect(within(drawer).getByLabelText("下跌 YMCA 规则")).toBeDisabled();
    expect(within(drawer).getByText(/当前计划协议早于 schema v3/)).toBeInTheDocument();
  });

  it("refreshes the durable event list when the global stream resets its cursor", async () => {
    type Listener = (event: Event) => void;
    class ResetEventSource {
      static current: ResetEventSource;
      listeners = new Map<string, Set<Listener>>();
      onopen: (() => void) | null = null;
      onerror: (() => void) | null = null;

      constructor(_url: string) {
        ResetEventSource.current = this;
      }

      addEventListener(type: string, listener: Listener) {
        const listeners = this.listeners.get(type) ?? new Set<Listener>();
        listeners.add(listener);
        this.listeners.set(type, listeners);
      }

      removeEventListener(type: string, listener: Listener) {
        this.listeners.get(type)?.delete(listener);
      }

      close() {}

      reset() {
        const event = new MessageEvent("portfolio.monitor.reset", {
          data: JSON.stringify({ reason: "cursor_not_found", cursor: null }),
        });
        this.listeners.get("portfolio.monitor.reset")?.forEach((listener) => listener(event));
      }
    }
    const listEvents = vi.mocked(api.listPortfolioMonitorEvents);
    vi.stubGlobal("EventSource", ResetEventSource);
    vi.stubGlobal("BroadcastChannel", undefined);
    try {
      render(
        <MemoryRouter>
          <PortfolioMonitorEffectsProvider><Portfolio /></PortfolioMonitorEffectsProvider>
        </MemoryRouter>,
      );
      await screen.findByRole("button", { name: "一键生成今日组合晨会" });
      await waitFor(() => expect(listEvents).toHaveBeenCalledTimes(1));

      act(() => ResetEventSource.current.reset());

      await waitFor(() => expect(listEvents).toHaveBeenCalledTimes(2));
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("explains stale data and lets a drafting monitor fetch fresh evidence again", async () => {
    const profile = {
      profile_id: "profile-stale",
      symbol: "588870.SH",
      market: "SH",
      instrument_type: "etf" as const,
      status: "drafting" as const,
      active_plan_version: null,
      profile_revision: 3,
      delivery_target_id: "target-1",
      input_outdated: false,
      blocked_reasons: ["quote_not_actionable:stale"],
      updated_at: "2026-07-14T01:00:00Z",
      last_quote_check_at: null,
    };
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({ profiles: [profile] });
    const retry = vi.spyOn(api, "reanalyzePortfolioMonitor").mockResolvedValue({
      batch_id: "batch-stale-retry",
      status: "completed_with_blocks",
      requested_symbols: [profile.symbol],
      delivery_target_id: profile.delivery_target_id,
      created_at: "2026-07-14T01:01:00Z",
      items: [{
        symbol: profile.symbol,
        status: "blocked",
        profile_id: profile.profile_id,
        blocked_reasons: ["quote_not_actionable:stale"],
      }],
    });

    render(<Portfolio />, { wrapper: MemoryRouter });

    const card = await screen.findByRole("article", { name: "科创50指 588870 监控标的" });
    const monitorRow = screen.getByRole("button", { name: /科创50指 588870.*监控数据异常/ });
    expect(within(monitorRow).getByRole("img", { name: "588870.SH 监控数据异常" }))
      .toHaveAttribute("data-monitor-lamp", "error");
    expect(within(monitorRow).queryByLabelText(/下次检查倒计时/)).not.toBeInTheDocument();
    expect(within(card).getByText("行情缓存已过期，尚未取得足够新的双源数据", { exact: false })).toBeInTheDocument();
    await userEvent.setup().click(within(card).getByRole("button", { name: "重新获取数据" }));
    expect(retry).toHaveBeenCalledWith(profile.profile_id);
  });

  it("offers explicit single-source consent and keeps the warning visible on the draft", async () => {
    const profile = {
      profile_id: "profile-single-source",
      symbol: "588870.SH",
      market: "SH",
      instrument_type: "etf" as const,
      status: "drafting" as const,
      active_plan_version: null,
      profile_revision: 2,
      delivery_target_id: "target-1",
      input_outdated: false,
      blocked_reasons: ["quote_not_actionable:single_source"],
      updated_at: "2026-07-14T01:00:00Z",
    };
    const singleSourcePlan = {
      schema_version: 1,
      symbol: "588870.SH",
      data_mode: "single_source" as const,
      summary: "用户已同意使用单源数据。",
      quote_tier: "normal" as const,
      near_trigger_tier: "active" as const,
      near_trigger_distance_bps: 100,
      market_rules: [{
        client_rule_id: "breakout",
        kind: "price_cross_above" as const,
        severity: "warning" as const,
        enabled: true,
        parameters: {
          threshold: 2.2,
          interval: "5m" as const,
          adjustment: "raw" as const,
          confirmation_count: 2,
          cooldown_minutes: 120,
          clear_hysteresis_bps: 30,
        },
        valid_until: "2026-10-14T01:00:00Z",
      }],
      news_topics: [],
      fundamental_monitor: { enabled: false },
      hard_valid_until: "2026-10-14T01:00:00Z",
    };
    const detail = {
      ...profile,
      status: "pending_review" as const,
      blocked_reasons: [],
      profile_revision: 3,
      plans: [{
        profile_id: profile.profile_id,
        version: 1,
        status: "pending_review",
        schema_version: 1,
        plan: singleSourcePlan,
        evidence_manifest: { data_mode: "single_source" },
        model_id: "evidence-policy-v1",
        data_as_of: "2026-07-14T01:00:00Z",
        created_at: "2026-07-14T01:00:00Z",
      }],
    };
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({ profiles: [profile] });
    const reanalyze = vi.spyOn(api, "reanalyzePortfolioMonitor").mockResolvedValue({
      batch_id: "batch-single-source",
      status: "completed",
      requested_symbols: [profile.symbol],
      delivery_target_id: profile.delivery_target_id,
      created_at: "2026-07-14T01:00:00Z",
      items: [{
        symbol: profile.symbol,
        status: "ready",
        profile_id: profile.profile_id,
        plan_version: 1,
        blocked_reasons: [],
      }],
    });
    vi.spyOn(api, "getPortfolioMonitor").mockResolvedValue(detail);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<Portfolio />, { wrapper: MemoryRouter });

    const card = await screen.findByRole("article", { name: "科创50指 588870 监控标的" });
    expect(within(card).getByText("当前仍只有单一来源", { exact: false })).toBeInTheDocument();
    await userEvent.setup().click(within(card).getByRole("button", { name: "同意使用单源模式" }));

    expect(reanalyze).toHaveBeenCalledWith(profile.profile_id, true);
    expect(await screen.findByRole("dialog", { name: "588870.SH 监控计划" })).toBeInTheDocument();
    expect(screen.getByLabelText("单源数据警告")).toHaveTextContent("此数据为单源，可能不准确");
  });

  it("opens a plan immediately and rechecks a closed monitor inside the drawer before activation", async () => {
    const target = {
      target_id: "target-reopen",
      channel: "feishu" as const,
      chat_id: "ou_user",
      chat_type: "p2p" as const,
      session_key: "feishu:ou_user",
      status: "active" as const,
      created_at: "2026-07-14T01:00:00Z",
    };
    const plan = {
      schema_version: 1,
      symbol: "588870.SH",
      summary: "上一次启用的监控计划。",
      quote_tier: "normal" as const,
      near_trigger_tier: "active" as const,
      near_trigger_distance_bps: 100,
      market_rules: [{
        client_rule_id: "breakout",
        kind: "price_cross_above" as const,
        severity: "warning" as const,
        enabled: true,
        parameters: {
          threshold: 2.2,
          interval: "5m" as const,
          adjustment: "raw" as const,
          confirmation_count: 2,
          cooldown_minutes: 120,
          clear_hysteresis_bps: 30,
        },
        valid_until: "2026-10-14T01:00:00Z",
      }],
      news_topics: [],
      fundamental_monitor: { enabled: false },
      hard_valid_until: "2026-10-14T01:00:00Z",
    };
    const closedProfile = {
      profile_id: "profile-reopen",
      symbol: "588870.SH",
      market: "SH",
      instrument_type: "etf" as const,
      status: "closed" as const,
      active_plan_version: 1,
      profile_revision: 2,
      delivery_target_id: target.target_id,
      input_outdated: false,
      blocked_reasons: ["quote_not_actionable:single_source"],
      updated_at: "2026-07-14T01:00:00Z",
    };
    const closedDetail = {
      ...closedProfile,
      plans: [{
        profile_id: closedProfile.profile_id,
        version: 1,
        status: "active",
        schema_version: 1,
        plan,
        evidence_manifest: {},
        model_id: "evidence-policy-v1",
        created_at: "2026-07-14T01:00:00Z",
      }],
    };
    const refreshedPlan = {
      ...plan,
      summary: "重新校验后生成的待审核草案。",
    };
    const reviewProfile = {
      ...closedProfile,
      status: "pending_review" as const,
      profile_revision: 4,
      active_plan_version: null,
      delivery_target_id: target.target_id,
      blocked_reasons: [],
      plans: [{
        profile_id: closedProfile.profile_id,
        version: 2,
        status: "pending_review",
        schema_version: 1,
        plan: refreshedPlan,
        evidence_manifest: {},
        model_id: "evidence-policy-v1",
        created_at: "2026-07-14T01:01:00Z",
      }, closedDetail.plans[0]],
    };
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({ profiles: [closedProfile] });
    vi.mocked(api.listPortfolioMonitorDeliveryTargets).mockResolvedValue({ targets: [target] });
    const reopen = vi.spyOn(api, "reopenPortfolioMonitor").mockResolvedValue({
      batch_id: "batch-reopen",
      status: "completed",
      requested_symbols: [closedProfile.symbol],
      delivery_target_id: target.target_id,
      created_at: "2026-07-14T01:01:00Z",
      items: [{
        symbol: closedProfile.symbol,
        status: "ready",
        profile_id: closedProfile.profile_id,
        plan_version: 2,
        blocked_reasons: [],
      }],
    });
    let profileReadCount = 0;
    vi.spyOn(api, "getPortfolioMonitor").mockImplementation(async () => {
      profileReadCount += 1;
      if (profileReadCount === 1) {
        await new Promise((resolve) => setTimeout(resolve, 30));
        return closedDetail;
      }
      return reviewProfile;
    });
    const saveAndActivate = vi.spyOn(api, "saveAndActivatePortfolioMonitorPlan").mockResolvedValue({
      ...reviewProfile,
      status: "active",
      active_plan_version: 2,
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<Portfolio />, { wrapper: MemoryRouter });

    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "查看已关闭 1" }));
    const card = await screen.findByRole("article", { name: "科创50指 588870 监控标的" });
    await user.click(within(card).getByRole("button", { name: "计划与审核" }));
    expect(screen.getByRole("dialog", { name: "588870.SH 监控计划" })).toBeInTheDocument();
    expect(await screen.findByText("上一次启用的监控计划。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存并启用" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "重新检测数据源" }));
    expect(reopen).toHaveBeenCalledWith(closedProfile.profile_id, target.target_id);
    expect((await screen.findAllByText("重新校验后生成的待审核草案。")).length).toBeGreaterThan(0);
    expect(saveAndActivate).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "保存并启用" }));
    expect(saveAndActivate).toHaveBeenCalledWith(
      closedProfile.profile_id,
      2,
      expect.objectContaining({ summary: "重新校验后生成的待审核草案。" }),
      reviewProfile.profile_revision,
    );
  });

  it("binds a Feishu private or group target with a one-time verification code", async () => {
    const target = {
      target_id: "target-bound",
      channel: "feishu" as const,
      chat_id: "oc_monitor_group",
      chat_type: "group" as const,
      session_key: "feishu:oc_monitor_group",
      status: "active" as const,
      created_at: "2026-07-14T01:00:10Z",
    };
    let claimed = false;
    vi.mocked(api.listPortfolioMonitorDeliveryTargets).mockImplementation(async () => ({
      targets: claimed ? [target] : [],
    }));
    vi.spyOn(api, "createPortfolioMonitorDeliveryBindingCode").mockResolvedValue({
      binding_id: "binding-1",
      code: "ABCD-EFGH",
      command: "绑定监控 ABCD-EFGH",
      status: "pending",
      created_at: "2026-07-14T01:00:00Z",
      expires_at: "2026-07-14T01:10:00Z",
    });
    vi.spyOn(api, "getPortfolioMonitorDeliveryBindingCode").mockImplementation(async () => {
      claimed = true;
      return {
        binding_id: "binding-1",
        status: "claimed",
        created_at: "2026-07-14T01:00:00Z",
        expires_at: "2026-07-14T01:10:00Z",
        claimed_at: "2026-07-14T01:00:10Z",
        target_id: target.target_id,
        target,
      };
    });
    const user = userEvent.setup();
    render(<Portfolio />, { wrapper: MemoryRouter });

    await user.click(await screen.findByRole("button", { name: "生成飞书绑定验证码" }));
    expect(await screen.findByText("ABCD-EFGH")).toBeInTheDocument();
    expect(screen.getByText("绑定监控 ABCD-EFGH")).toBeInTheDocument();
    expect(screen.getByText("@机器人 绑定监控 ABCD-EFGH")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "我已发送，立即检查" }));
    await waitFor(() => expect(api.getPortfolioMonitorDeliveryBindingCode).toHaveBeenCalledWith("binding-1"));
    expect(await screen.findByText("绑定成功，已自动选中该群聊。")).toBeInTheDocument();
    expect(screen.getByLabelText("飞书提醒目标")).toHaveValue("target-bound");
  });

  it("shows shadow mode health and marks would-deliver events as not sent", async () => {
    const checkedAt = new Date().toISOString();
    const nextCheckAt = new Date(Date.now() + 60_000).toISOString();
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({
      profiles: [{
        profile_id: "profile-heartbeat",
        symbol: "588870.SH",
        market: "SH",
        instrument_type: "etf",
        status: "active",
        active_plan_version: 1,
        profile_revision: 1,
        delivery_target_id: "target-1",
        input_outdated: false,
        blocked_reasons: [],
        updated_at: checkedAt,
        last_quote_check_at: checkedAt,
        last_success_at: checkedAt,
        next_quote_run_at: nextCheckAt,
      }],
    });
    vi.mocked(api.getPortfolioMonitoringStatus).mockResolvedValue({
      enabled_by_config: true,
      effective_mode: "shadow",
      runtime: {
        enabled: true,
        running: true,
        leader: true,
        mode: "shadow",
        mode_valid: true,
        current_tick_started_at: checkedAt,
        last_tick: { decision: "evaluated" },
        calendar: {
          mode: "cached_exchange_calendar",
          market_date: "2026-07-14",
          is_trading_day: true,
          session: "morning",
          open: true,
        },
      },
      capabilities: { market_rules: "available", automatic_trading: "forbidden" },
      profiles: 1,
      active_profiles: 1,
      events: 1,
      pending_deliveries: 0,
      uncertain_deliveries: 0,
      shadow_suppressed_deliveries: 1,
      blocked_profiles: 0,
      database_size_bytes: 1024,
      database_max_bytes: 536870912,
      database_utilization: 0.000002,
      delivery_status_counts: { shadow_suppressed: 1 },
      observation_status_counts: { verified: 3 },
      runtime_health: {
        window_hours: 24,
        tick_count: 3,
        error_tick_count: 0,
        duration_ms: { p95: 80 },
        schedule_lag_ms: { p95: 1200 },
        bar_lag_ms: { p95: 60000 },
        database_growth_bytes: 1024,
        counters: {},
      },
      profile_health: [],
      maintenance: null,
    });
    vi.mocked(api.listPortfolioMonitorEvents).mockResolvedValue({
      events: [{
        event_id: "event-shadow",
        profile_id: "profile-1",
        symbol: "588870.SH",
        plan_version: 1,
        kind: "market_rule_trigger",
        status: "confirmed",
        severity: "warning",
        title: "588870.SH 监控条件已满足",
        summary: "闭合行情连续 2 次满足条件。",
        facts: {
          last_price: 2.2,
          target_assessment: {
            client_rule_id: "price-target-lower",
            target_intent: "add_position",
            target_level: 1,
            phase: "approaching",
            decision: "opposes_add",
            distance_bps: 42,
            message: "放量加速下跌，不宜补仓",
            reason_codes: ["accelerated_decline"],
          },
        },
        first_seen_at: "2026-07-14T02:00:00Z",
        deliveries: [{
          delivery_id: "delivery-shadow",
          status: "shadow_suppressed",
          delivery_mode: "shadow",
          would_deliver: true,
          suppression_reason: "shadow_mode",
        }],
      }],
    });

    render(<Portfolio />, { wrapper: MemoryRouter });

    expect(await screen.findByText("影子运行")).toBeInTheDocument();
    expect(screen.getByText(/当前为影子模式/)).toBeInTheDocument();
    expect(screen.getByText("影子命中 · 未发送")).toBeInTheDocument();
    const assessment = screen.getByLabelText("科创50指（588870） 目标位量价确认");
    expect(assessment).toHaveAttribute("data-target-assessment", "opposes_add");
    expect(within(assessment).getByText("L1 · 加仓点")).toBeInTheDocument();
    expect(within(assessment).getByText("接近目标 · 42 bps")).toBeInTheDocument();
    expect(within(assessment).getByText("不宜补仓")).toBeInTheDocument();
    expect(within(assessment).getByText("放量加速下跌，不宜补仓")).toBeInTheDocument();
    expect(screen.getByText("上午交易")).toBeInTheDocument();
    expect(screen.getByLabelText("监控检查进行中")).toBeInTheDocument();
    const heartbeat = screen.getByLabelText("588870.SH 监控心跳");
    expect(heartbeat).toHaveAttribute("data-check-pulse", "true");
    expect(within(heartbeat).getByText("刚刚完成检查 · 数据正常")).toBeInTheDocument();
    expect(within(heartbeat).getByText(/下次检查：.*后/)).toBeInTheDocument();
    expect(within(heartbeat).queryByText(/下次预计|分钟[前]/)).not.toBeInTheDocument();
  });

  it("starts persistent shadow monitoring from the page without editing config", async () => {
    const initialStatus = await vi.mocked(api.getPortfolioMonitoringStatus)();
    const configure = vi.spyOn(api, "configurePortfolioMonitoringRuntime").mockResolvedValue({
      ...initialStatus,
      enabled_by_config: true,
      effective_mode: "shadow",
      runtime: {
        ...initialStatus.runtime,
        enabled: true,
        running: true,
        leader: true,
        mode: "shadow",
      },
    });
    const user = userEvent.setup();
    render(<Portfolio />, { wrapper: MemoryRouter });

    await user.click(await screen.findByRole("button", { name: "启动影子监控" }));

    await waitFor(() => expect(configure).toHaveBeenCalledWith(true, "shadow"));
    expect(screen.getByRole("button", { name: "停止监控服务" })).toBeInTheDocument();
    expect(screen.getByText("影子运行")).toBeInTheDocument();
  });

  it("keeps US plans enabled but does not claim the mainland scheduler is monitoring them", async () => {
    const now = new Date().toISOString();
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({
      profiles: [{
        profile_id: "profile-aapl",
        symbol: "AAPL.US",
        market: "US",
        instrument_type: "stock",
        status: "active",
        active_plan_version: 1,
        profile_revision: 1,
        delivery_target_id: "target-1",
        input_outdated: false,
        blocked_reasons: [],
        updated_at: now,
        last_quote_check_at: null,
        last_success_at: null,
        next_quote_run_at: now,
      }],
    });
    const initialStatus = await vi.mocked(api.getPortfolioMonitoringStatus)();
    vi.mocked(api.getPortfolioMonitoringStatus).mockResolvedValue({
      ...initialStatus,
      enabled_by_config: true,
      effective_mode: "shadow",
      runtime: {
        ...initialStatus.runtime,
        enabled: true,
        running: true,
        leader: true,
        mode: "shadow",
        calendar: {
          mode: "cached_exchange_calendar",
          market_date: "2026-07-15",
          is_trading_day: true,
          session: "morning",
          open: true,
        },
      },
    });
    render(<Portfolio />, { wrapper: MemoryRouter });

    expect(await screen.findByText("计划已启用")).toBeInTheDocument();
    const heartbeat = screen.getByLabelText("AAPL.US 监控心跳");
    expect(within(heartbeat).getByText("当前未实际监控 · 该市场调度待接入")).toBeInTheDocument();
    expect(within(heartbeat).getByText(/等待该市场交易日历接入/)).toBeInTheDocument();
  });

  it.each([
    ["上涨", "text-red-600", "up"],
    ["下跌", "text-emerald-600", "down"],
  ] as const)("shows the last monitored price, countdown ring, and %s target band", async (
    _label,
    colorClass,
    direction,
  ) => {
    const checkedAt = new Date().toISOString();
    const nextRunAt = new Date(Date.now() + 65_000).toISOString();
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({
      profiles: [{
        profile_id: "profile-price-band",
        symbol: "588870.SH",
        market: "SH",
        instrument_type: "etf",
        status: "active",
        active_plan_version: 1,
        profile_revision: 1,
        delivery_target_id: "target-1",
        input_outdated: false,
        blocked_reasons: [],
        updated_at: checkedAt,
        last_quote_check_at: checkedAt,
        last_success_at: checkedAt,
        next_quote_run_at: nextRunAt,
        last_quote: {
          price: 2.1,
          observed_at: checkedAt,
          data_as_of: checkedAt,
          status: "verified",
          interval: "5m",
          sources: ["tencent", "mootdx"],
          session_open: 2.05,
          session_date: "2026-07-15",
          previous_price: direction === "up" ? 2.08 : 2.12,
          previous_data_as_of: new Date(Date.now() - 300_000).toISOString(),
          price_change: direction === "up" ? 0.02 : -0.02,
          price_change_pct: direction === "up" ? 0.9615 : -0.9434,
          trend: direction,
        },
        display_plan: monitorDisplayPlan(checkedAt),
      }],
    });
    const initialStatus = await vi.mocked(api.getPortfolioMonitoringStatus)();
    vi.mocked(api.getPortfolioMonitoringStatus).mockResolvedValue({
      ...initialStatus,
      enabled_by_config: true,
      effective_mode: "shadow",
      runtime: {
        ...initialStatus.runtime,
        enabled: true,
        running: true,
        leader: true,
        mode: "shadow",
        calendar: {
          mode: "cached_exchange_calendar",
          market_date: "2026-07-15",
          is_trading_day: true,
          session: "morning",
          open: true,
        },
      },
    });
    const user = userEvent.setup();
    render(<Portfolio />, { wrapper: MemoryRouter });

    const card = await screen.findByRole("article", { name: "科创50指 588870 监控标的" });
    const monitorRow = screen.getByRole("button", {
      name: /科创50指 588870，现价 2\.100，较开盘 \+2\.44%，监控运行正常/,
    });
    const nearestTargetGlyph = within(monitorRow).getByLabelText(/L1 加仓，目标价 2\.000，距离 4\.76%/);
    expect(within(nearestTargetGlyph).getByText("L1 加仓")).toHaveClass("text-blue-700");
    expect(within(monitorRow).getByRole("img", {
      name: /588870.SH 监控运行正常，下次检查倒计时 1:0[0-5]/,
    })).toHaveAttribute("data-monitor-lamp", "healthy");
    expect(within(monitorRow).queryByText("检查倒计时")).not.toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: /监控运行正常/ })).toHaveLength(2);
    const snapshot = within(card).getByLabelText("588870.SH 价格监控概览");
    expect(within(snapshot).getAllByText("2.100").length).toBeGreaterThan(0);
    const countdown = within(snapshot).getByLabelText("588870.SH 下次检查倒计时");
    expect(within(countdown).getByText(/1:0[0-5]/)).toBeInTheDocument();
    const targetBand = within(snapshot).getByLabelText("588870.SH 价格目标区间");
    expect(targetBand).toHaveAttribute("data-current-range-state", "inside");
    expect(targetBand).toHaveAttribute("data-current-position", "50.00");
    expect(targetBand).toHaveAttribute("data-open-position", "25.00");
    expect(within(targetBand).getByText("左侧目标")).toBeInTheDocument();
    expect(within(targetBand).getByText("右侧目标")).toBeInTheDocument();
    expect(within(targetBand).getAllByText("L1 · 加仓点").length).toBeGreaterThan(0);
    expect(within(targetBand).getAllByText("L1 · 止盈点").length).toBeGreaterThan(0);
    const addPositionTarget = within(targetBand).getByRole("button", { name: "L1 · 加仓点，查看点位依据" });
    await user.hover(addPositionTarget);
    const addPositionTooltip = await screen.findByRole("tooltip");
    expect(addPositionTooltip).toHaveTextContent("2026-06-03 低点 1.900");
    expect(addPositionTooltip).toHaveTextContent("公式：(最新价 + L2 止损点) ÷ 2");
    expect(addPositionTooltip).toHaveClass("bg-popover/85", "backdrop-blur-md");
    await user.unhover(addPositionTarget);
    await waitFor(() => expect(screen.queryByRole("tooltip")).not.toBeInTheDocument());
    const takeProfitTarget = within(targetBand).getByRole("button", { name: "L1 · 止盈点，查看点位依据" });
    await user.hover(takeProfitTarget);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("2026-06-18 高点 2.200");
    await user.unhover(takeProfitTarget);
    expect(within(targetBand).getByText("今日开盘基准")).toBeInTheDocument();
    expect(within(targetBand).getByText("2.000")).toBeInTheDocument();
    expect(within(targetBand).getByText("2.050")).toBeInTheDocument();
    expect(within(targetBand).getByText("2.200")).toBeInTheDocument();
    expect(within(targetBand).getByText("较开盘 -2.44%")).toHaveClass("text-emerald-600");
    expect(within(targetBand).getByText("较开盘 +7.32%")).toHaveClass("text-red-600");
    expect(within(targetBand).getByText("较开盘 +2.44%")).toHaveClass("text-red-600");
    const trendText = "较开盘 +2.44%";
    expect(within(snapshot).getAllByText(trendText)[0]?.parentElement).toHaveClass("text-red-600");
    const trendAria = "趋势上涨";
    expect(within(targetBand).getByLabelText(`最新监测价 2.100，较开盘 +2.44%，${trendAria}`)).toHaveTextContent("现价 2.100");
    expect(snapshot).toHaveAttribute("data-boost-direction", "none");
    expect(card.querySelector("[data-boost-particles='true']")).toBeNull();
  });

  it("shows the deterministic price-volume snapshot and accelerated-decline warning", async () => {
    const checkedAt = new Date().toISOString();
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({
      profiles: [{
        profile_id: "profile-price-volume",
        symbol: "588870.SH",
        market: "SH",
        instrument_type: "etf",
        status: "active",
        active_plan_version: 1,
        profile_revision: 1,
        delivery_target_id: "target-1",
        input_outdated: false,
        blocked_reasons: [],
        updated_at: checkedAt,
        last_quote_check_at: checkedAt,
        last_success_at: checkedAt,
        next_quote_run_at: new Date(Date.now() + 60_000).toISOString(),
        last_quote: {
          price: 2.01,
          observed_at: checkedAt,
          data_as_of: checkedAt,
          status: "verified",
          interval: "5m",
          sources: ["tencent", "mootdx"],
          session_open: 2.1,
          previous_price: 2.03,
          trend: "down",
          price_volume: {
            status: "ready",
            regime: "bearish_expansion",
            volume_state: "expanded",
            volume_ratio: 1.86,
            baseline_samples: 8,
            three_bar_return_bps: -61,
            latest_return_bps: -31,
            close_location: 0.2,
            accelerated_decline: true,
            reason_codes: [],
          },
        },
        display_plan: monitorPriceVolumeDisplayPlan(checkedAt),
      }],
    });

    render(<Portfolio />, { wrapper: MemoryRouter });

    const card = await screen.findByRole("article", { name: "科创50指 588870 监控标的" });
    const priceVolume = within(card).getByLabelText("588870.SH 量价分析");
    const snapshot = within(card).getByLabelText("588870.SH 价格监控概览");
    const targetBand = within(snapshot).getByLabelText("588870.SH 价格目标区间");
    expect(priceVolume.compareDocumentPosition(targetBand) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(card).toHaveClass("max-h-[56rem]");
    expect(card.parentElement).toHaveClass("lg:h-[56rem]");
    expect(priceVolume).toHaveAttribute("data-price-volume-status", "ready");
    expect(priceVolume).toHaveAttribute("data-accelerated-decline", "true");
    expect(within(priceVolume).getAllByText("放量加速下跌").length).toBeGreaterThan(0);
    expect(within(priceVolume).getByText("1.86× · 放量")).toBeInTheDocument();
    expect(within(priceVolume).getByText("-61 bps")).toBeInTheDocument();
    expect(within(priceVolume).getByText("可用 · 8 个同期样本")).toBeInTheDocument();
    expect(within(priceVolume).getByText("放量加速下跌，不宜补仓")).toBeInTheDocument();
  });

  it("keeps price monitoring visible while marking price-volume evidence as insufficient", async () => {
    const checkedAt = new Date().toISOString();
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({
      profiles: [{
        profile_id: "profile-price-volume-insufficient",
        symbol: "588870.SH",
        market: "SH",
        instrument_type: "etf",
        status: "active",
        active_plan_version: 1,
        profile_revision: 1,
        delivery_target_id: "target-1",
        input_outdated: false,
        blocked_reasons: [],
        updated_at: checkedAt,
        last_quote_check_at: checkedAt,
        last_success_at: checkedAt,
        last_quote: {
          price: 2.1,
          observed_at: checkedAt,
          data_as_of: checkedAt,
          status: "verified",
          interval: "5m",
          sources: ["tencent", "mootdx"],
          session_open: 2.08,
          price_volume: {
            status: "insufficient_data",
            regime: null,
            volume_state: null,
            volume_ratio: null,
            baseline_samples: 2,
            three_bar_return_bps: null,
            latest_return_bps: null,
            close_location: null,
            accelerated_decline: false,
            reason_codes: ["insufficient_same_time_baseline"],
          },
        },
        display_plan: monitorPriceVolumeDisplayPlan(checkedAt),
      }],
    });

    render(<Portfolio />, { wrapper: MemoryRouter });

    const card = await screen.findByRole("article", { name: "科创50指 588870 监控标的" });
    expect(within(card).getByLabelText("588870.SH 价格监控概览")).toHaveTextContent("2.100");
    const priceVolume = within(card).getByLabelText("588870.SH 量价分析");
    expect(priceVolume).toHaveAttribute("data-price-volume-status", "insufficient_data");
    expect(priceVolume).toHaveTextContent("数据质量：量价证据不足");
    expect(priceVolume).toHaveTextContent("当前 2 个同期样本");
    expect(priceVolume).toHaveTextContent("历史同时间桶样本不足");
  });

  it("keeps intraday high and low visible after price returns inside the target window", async () => {
    const checkedAt = new Date().toISOString();
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({
      profiles: [{
        profile_id: "profile-price-band",
        symbol: "588870.SH",
        market: "SH",
        instrument_type: "etf",
        status: "active",
        active_plan_version: 1,
        profile_revision: 1,
        delivery_target_id: "target-1",
        input_outdated: false,
        blocked_reasons: [],
        updated_at: checkedAt,
        last_quote_check_at: checkedAt,
        last_success_at: checkedAt,
        next_quote_run_at: new Date(Date.now() + 65_000).toISOString(),
        last_quote: {
          price: 2.1,
          observed_at: checkedAt,
          data_as_of: checkedAt,
          status: "verified",
          interval: "5m",
          sources: ["tencent", "mootdx"],
          session_open: 2.05,
          session_high: 2.35,
          session_low: 1.85,
          session_date: "2026-07-15",
          previous_price: 2.08,
          previous_data_as_of: new Date(Date.now() - 300_000).toISOString(),
          price_change: 0.02,
          price_change_pct: 0.9615,
          trend: "up",
        },
        display_plan: monitorDisplayPlan(checkedAt),
      }],
    });

    render(<Portfolio />, { wrapper: MemoryRouter });

    const card = await screen.findByRole("article", { name: "科创50指 588870 监控标的" });
    const snapshot = within(card).getByLabelText("588870.SH 价格监控概览");
    const targetBand = within(snapshot).getByLabelText("588870.SH 价格目标区间");
    expect(targetBand).toHaveAttribute("data-current-position", "50.00");
    expect(targetBand).toHaveAttribute("data-open-position", "40.00");
    expect(targetBand).toHaveAttribute("data-left-target-position", "30.00");
    expect(targetBand).toHaveAttribute("data-right-target-position", "70.00");
    expect(targetBand).toHaveAttribute("data-session-low-position", "0.00");
    expect(targetBand).toHaveAttribute("data-session-high-position", "100.00");
    expect(within(targetBand).getByLabelText("当日低点 1.850")).toHaveClass("bg-zinc-950");
    expect(within(targetBand).getByLabelText("当日高点 2.350")).toHaveClass("bg-zinc-950");
    expect(within(targetBand).getByText("当日低 1.850")).toBeInTheDocument();
    expect(within(targetBand).getByText("当日高 2.350")).toBeInTheDocument();
    expect(snapshot).toHaveAttribute("data-boost-direction", "none");
  });

  it.each([
    {
      label: "上破第一止盈点后显示第二止盈点",
      price: 2.25,
      previous: 2.23,
      trend: "up" as const,
      direction: "up",
      rangeState: "inside",
      position: "50.00",
      left: "L1 · 止盈点",
      right: "L2 · 止盈点",
      crossed: "1",
      message: /突破 Boost · 已突破 L1 · 止盈点；正在冲击 L2 · 止盈点/,
    },
    {
      label: "下破加仓观察点后显示下一止损点",
      price: 1.95,
      previous: 1.97,
      trend: "down" as const,
      direction: "down",
      rangeState: "inside",
      position: "50.00",
      left: "L2 · 止损点",
      right: "L1 · 加仓点",
      crossed: "1",
      message: /下行 Boost · 已跌破 L1 · 加仓点；正在接近 L2 · 止损点/,
    },
    {
      label: "没有更高目标时使用现价作为右侧边界",
      price: 2.35,
      previous: 2.33,
      trend: "up" as const,
      direction: "up",
      rangeState: "above-last-target",
      position: "100.00",
      left: "L2 · 止盈点",
      right: "现价边界",
      crossed: "2",
      message: /暂无更高目标，现价作为右侧边界/,
    },
  ])("uses the dynamic target ladder when $label", async ({
    price,
    previous,
    trend,
    direction,
    rangeState,
    position,
    left,
    right,
    crossed,
    message,
  }) => {
    const checkedAt = new Date().toISOString();
    vi.mocked(api.listPortfolioMonitors).mockResolvedValue({
      profiles: [{
        profile_id: "profile-price-band",
        symbol: "588870.SH",
        market: "SH",
        instrument_type: "etf",
        status: "active",
        active_plan_version: 1,
        profile_revision: 1,
        delivery_target_id: "target-1",
        input_outdated: false,
        blocked_reasons: [],
        updated_at: checkedAt,
        last_quote_check_at: checkedAt,
        last_success_at: checkedAt,
        next_quote_run_at: new Date(Date.now() + 65_000).toISOString(),
        last_quote: {
          price,
          observed_at: checkedAt,
          data_as_of: checkedAt,
          status: "verified",
          interval: "5m",
          sources: ["tencent", "mootdx"],
          session_open: 2.05,
          session_date: "2026-07-15",
          previous_price: previous,
          previous_data_as_of: new Date(Date.now() - 300_000).toISOString(),
          price_change: price - previous,
          price_change_pct: ((price - previous) / previous) * 100,
          trend,
        },
        display_plan: monitorDisplayPlan(checkedAt),
      }],
    });

    render(<Portfolio />, { wrapper: MemoryRouter });

    const card = await screen.findByRole("article", { name: "科创50指 588870 监控标的" });
    expect(card).toHaveAttribute("data-boost-direction", direction);
    const particles = card.querySelector(`[data-boost-particles='true'][data-direction='${direction}']`);
    expect(particles).not.toBeNull();
    expect(particles?.querySelectorAll(".monitor-boost-particle")).toHaveLength(12);
    const snapshot = within(card).getByLabelText("588870.SH 价格监控概览");
    expect(snapshot).toHaveAttribute("data-boost-direction", direction);
    const boostBadge = within(snapshot).getByRole("status");
    expect(within(boostBadge).getByText(message)).toBeInTheDocument();
    expect(boostBadge).toHaveClass("max-w-[13rem]", "text-[10px]", "py-1");
    expect(boostBadge.parentElement).toHaveClass("justify-self-end");
    const targetBand = within(snapshot).getByLabelText("588870.SH 价格目标区间");
    expect(targetBand).toHaveAttribute("data-current-range-state", rangeState);
    expect(targetBand).toHaveAttribute("data-current-position", position);
    expect(targetBand).toHaveAttribute("data-crossed-target-count", crossed);
    expect(within(targetBand).getAllByText(left).length).toBeGreaterThan(0);
    expect(within(targetBand).getAllByText(right).length).toBeGreaterThan(0);
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

    expect(await screen.findByText("科创50指（588870.SH）· 2026-07-14 PDF")).toBeInTheDocument();
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
    expect(within(offensiveZone).getByTestId("holding-zone-total")).toHaveClass(
      "text-base",
      "font-bold",
      "text-red-500",
    );
    expect(within(defensiveZone).getByTestId("holding-zone-total")).toHaveClass(
      "text-base",
      "font-bold",
      "text-blue-500",
    );
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
    expect(document.querySelectorAll('[data-cache-group="510300.SH"]')).toHaveLength(0);
    await userEvent.setup().click(screen.getAllByTitle("展开/收起详情")[0]);
    expect(screen.getByText("实际来源")).toBeInTheDocument();
    expect(screen.getAllByText("eastmoney").length).toBeGreaterThan(0);
    expect(screen.getByText("10,000 share")).toBeInTheDocument();
  });

  it("hides non-holding caches and collapses fully verified holdings by default", async () => {
    vi.mocked(api.getPortfolioReview).mockResolvedValue({
      ...review,
      verified_market_cache: [review.verified_market_cache[0], review.verified_market_cache[2]],
    });
    const user = userEvent.setup();
    render(<Portfolio />, { wrapper: MemoryRouter });

    const expand = await screen.findByRole("button", {
      name: "科创50指 588870.SH 展开校核行情",
    });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText(/全部校核通过，已折叠/)).toBeInTheDocument();
    expect(document.querySelectorAll('[data-cache-group="510300.SH"]')).toHaveLength(0);
    expect(screen.queryByText("raw")).not.toBeInTheDocument();

    await user.click(expand);

    expect(screen.getByRole("button", { name: "科创50指 588870.SH 收起校核行情" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByText("raw")).toBeInTheDocument();
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

    await user.type(await screen.findByLabelText("证券代码或美股 ticker"), "588870");
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
    const tradesTable = within(screen.getByRole("table", { name: "最近交易记录" }));
    expect(tradesTable.getByText("科创50ETF汇添富（588870）")).toBeInTheDocument();
    expect(tradesTable.getByText("买入")).toBeInTheDocument();
  });

  it("resolves and records an AAPL trade with the qualified U.S. symbol", async () => {
    vi.mocked(api.lookupPortfolioSecurity).mockResolvedValue({
      code: "AAPL",
      symbol: "AAPL.US",
      name: "Apple Inc.",
      market: "us",
      source: "yahoo",
    });
    vi.spyOn(api, "recordPortfolioTrade").mockResolvedValue({
      ...review,
      portfolio_state: {
        ...review.portfolio_state,
        holdings: [{
          code: "AAPL",
          symbol: "AAPL.US",
          name: "Apple Inc.",
          quantity: 2,
          cost_price: 210.5,
        }],
      },
    });
    const user = userEvent.setup();
    render(<Portfolio />, { wrapper: MemoryRouter });

    await user.type(await screen.findByLabelText("证券代码或美股 ticker"), "aapl");
    await waitFor(() => expect(screen.getByLabelText("自动补全的证券标识")).toHaveValue("AAPL.US"));
    expect(screen.getByLabelText("自动补全的证券名称")).toHaveValue("Apple Inc.");
    await user.type(screen.getByPlaceholderText("数量"), "2");
    await user.type(screen.getByPlaceholderText("价格"), "210.5");
    await user.click(screen.getByRole("button", { name: "保存交易" }));

    expect(api.lookupPortfolioSecurity).toHaveBeenCalledWith("AAPL", expect.any(AbortSignal));
    expect(api.recordPortfolioTrade).toHaveBeenCalledWith(expect.objectContaining({
      code: "AAPL",
      symbol: "AAPL.US",
      name: "Apple Inc.",
      quantity: 2,
      price: 210.5,
    }));
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

    await user.click(await screen.findByRole("button", { name: "删除交易 科创50ETF汇添富（588870） 2026-07-12" }));

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
    expect(await screen.findByRole("dialog", { name: "科创50指（588870） K线检阅" })).toBeInTheDocument();
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

  it("opens named market details beside the cached K-line action", async () => {
    render(
      <MemoryRouter initialEntries={["/portfolio"]}>
        <LocationProbe />
        <Portfolio />
      </MemoryRouter>,
    );

    await userEvent.setup().click(await screen.findByRole("button", { name: "科创50指 588870.SH 行情详情" }));

    expect(screen.getByTestId("location")).toHaveTextContent(
      "/data-center?symbol=588870.SH&name=%E7%A7%91%E5%88%9B50%E6%8C%87",
    );
  });

  it("refreshes one holding from its cache group action", async () => {
    const running = {
      ...makeRun("running"),
      profile: "symbol_detail",
      symbols: ["588870.SH"],
    };
    const completed = {
      ...makeRun("completed"),
      profile: "symbol_detail",
      symbols: ["588870.SH"],
    };
    vi.spyOn(api, "startMarketCacheRefresh").mockResolvedValue({
      status: "accepted",
      run_id: running.run_id,
      deduplicated: false,
      run: running,
    });
    vi.spyOn(api, "getMarketCacheRun").mockResolvedValue(completed);
    render(<Portfolio />, { wrapper: MemoryRouter });

    await userEvent.setup().click(await screen.findByRole("button", {
      name: "刷新 科创50指 588870.SH 行情",
    }));

    expect(api.startMarketCacheRefresh).toHaveBeenCalledWith({
      symbols: ["588870.SH"],
      profile: "symbol_detail",
    });
    expect(screen.getByRole("button", { name: "科创50指 588870.SH 行情刷新中" })).toBeDisabled();
    await waitFor(() => expect(api.getMarketCacheRun).toHaveBeenCalledWith(running.run_id), { timeout: 2000 });
    await waitFor(() => expect(api.getPortfolioReview).toHaveBeenCalledTimes(2), { timeout: 2000 });
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
    const refreshSection = (await screen.findByText("行情缓存刷新")).closest("section");
    expect(refreshSection).not.toBeNull();
    expect(within(refreshSection as HTMLElement).getByText("科创50指（588870）")).toBeInTheDocument();
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
