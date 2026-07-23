import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Reports } from "../Reports";

const apiMock = vi.hoisted(() => ({
  listRuns: vi.fn(),
  listDeepReports: vi.fn(),
  deepReportArtifactUrl: vi.fn(),
  getRunReportPreview: vi.fn(),
  runReportArtifactUrl: vi.fn(),
  sendReportArtifactToFeishu: vi.fn(),
  listReportLibrary: vi.fn(),
  listReportLibrarySubjects: vi.fn(),
  getReportLibrarySubject: vi.fn(),
  getReportLibrarySubjectReports: vi.fn(),
  getReportLibraryResearchNotes: vi.fn(),
  getReportLibrarySources: vi.fn(),
  refreshReportLibrarySources: vi.fn(),
  getReportLibraryAnnualReportCoverage: vi.fn(),
  backfillReportLibraryAnnualReports: vi.fn(),
  startReportLibraryAnnualReportBackfill: vi.fn(),
  getReportLibraryAnnualReportBackfillJob: vi.fn(),
  getLatestReportLibraryAnnualReportBackfillJob: vi.fn(),
  rebuildReportLibraryFinancialSnapshots: vi.fn(),
  refreshReportLibraryInstrumentProfile: vi.fn(),
  refreshReportLibraryHistoricalPercentile: vi.fn(),
  refreshReportLibraryETFProfile: vi.fn(),
  compareReportLibrary: vi.fn(),
  reconcileReportLibrary: vi.fn(),
  reportLibraryArtifactUrl: vi.fn(),
  getFeishuDeliverySettings: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: apiMock,
}));

describe("Reports page", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  beforeEach(() => {
    window.localStorage.clear();
    window.history.replaceState({}, "", "/reports");
    apiMock.listRuns.mockReset();
    apiMock.listDeepReports.mockReset();
    apiMock.getRunReportPreview.mockReset();
    apiMock.runReportArtifactUrl.mockReset();
    apiMock.sendReportArtifactToFeishu.mockReset();
    apiMock.listReportLibrary.mockReset();
    apiMock.listReportLibrarySubjects.mockReset();
    apiMock.getReportLibrarySubject.mockReset();
    apiMock.getReportLibrarySubjectReports.mockReset();
    apiMock.getReportLibraryResearchNotes.mockReset();
    apiMock.getReportLibrarySources.mockReset();
    apiMock.refreshReportLibrarySources.mockReset();
    apiMock.getReportLibraryAnnualReportCoverage.mockReset();
    apiMock.backfillReportLibraryAnnualReports.mockReset();
    apiMock.startReportLibraryAnnualReportBackfill.mockReset();
    apiMock.getReportLibraryAnnualReportBackfillJob.mockReset();
    apiMock.getLatestReportLibraryAnnualReportBackfillJob.mockReset();
    apiMock.rebuildReportLibraryFinancialSnapshots.mockReset();
    apiMock.refreshReportLibraryInstrumentProfile.mockReset();
    apiMock.refreshReportLibraryHistoricalPercentile.mockReset();
    apiMock.refreshReportLibraryETFProfile.mockReset();
    apiMock.compareReportLibrary.mockReset();
    apiMock.reconcileReportLibrary.mockReset();
    apiMock.reportLibraryArtifactUrl.mockReset();
    apiMock.getFeishuDeliverySettings.mockReset();
    apiMock.listDeepReports.mockResolvedValue([]);
    apiMock.listReportLibrary.mockResolvedValue({ reports: [], next_cursor: null });
    apiMock.listReportLibrarySubjects.mockImplementation(async (params = {}) => {
      const result = await apiMock.listReportLibrary(params);
      const grouped = new Map<string, Array<Record<string, any>>>();
      for (const report of result.reports || []) {
        grouped.set(report.subject_key, [...(grouped.get(report.subject_key) || []), report]);
      }
      const subjects = Array.from(grouped.entries()).map(([subjectKey, reports]) => {
        const sorted = reports.slice().sort((left, right) => String(right.generated_at).localeCompare(String(left.generated_at)));
        const latest = sorted[0];
        return {
          subject_key: subjectKey,
          symbol: latest.symbol,
          security_name: latest.security_name || latest.symbol || subjectKey,
          report_count: reports.length,
          new_report_count: reports.length,
          latest_generated_at: latest.generated_at,
          latest_data_as_of: latest.data_as_of,
          research_note_count: 0,
          confirmed_note_count: 0,
          broker_research_count: 0,
          report_kinds: Array.from(new Set(reports.map((report) => report.report_kind))),
          current_viewpoint_summary: latest.viewpoint?.daily?.summary || latest.viewpoint?.structural?.summary,
          quality_summary: {
            passed: reports.filter((report) => report.report_quality_status !== "failed_validation").length,
            complete: reports.filter((report) => report.coverage_status === "complete").length,
          },
          latest_report: latest,
        };
      }).sort((left, right) => String(right.latest_generated_at).localeCompare(String(left.latest_generated_at)));
      return { subjects, next_cursor: null, total_count: subjects.length };
    });
    apiMock.getReportLibrarySubjectReports.mockImplementation(async (subjectKey: string) => {
      const result = await apiMock.listReportLibrary();
      const reports = (result.reports || [])
        .filter((report: Record<string, any>) => report.subject_key === subjectKey)
        .sort((left: Record<string, any>, right: Record<string, any>) => String(right.generated_at).localeCompare(String(left.generated_at)));
      return { reports, next_cursor: null, total_count: reports.length };
    });
    apiMock.getReportLibraryResearchNotes.mockResolvedValue({
      subject_key: "",
      notes: [],
      counts: { unverified: 0, confirmed: 0, contradicted: 0, superseded: 0 },
      total_count: 0,
      next_cursor: null,
    });
    apiMock.getReportLibrarySources.mockResolvedValue({ report_id: "", sources: [] });
    apiMock.refreshReportLibrarySources.mockResolvedValue({ status: "completed" });
    apiMock.getReportLibraryAnnualReportCoverage.mockResolvedValue({
      symbol: "600519.SH",
      requested_years: [2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018],
      covered_years: [2025, 2024],
      missing_years: [2023, 2022, 2021, 2020, 2019, 2018],
      coverage_ratio: 0.25,
      documents_by_year: {},
    });
    apiMock.backfillReportLibraryAnnualReports.mockResolvedValue({
      status: "completed",
      coverage: {
        symbol: "600519.SH",
        requested_years: [2025, 2024],
        covered_years: [2025, 2024],
        missing_years: [],
        coverage_ratio: 1,
        documents_by_year: {},
      },
    });
    apiMock.getLatestReportLibraryAnnualReportBackfillJob.mockResolvedValue({ job: null });
    apiMock.rebuildReportLibraryFinancialSnapshots.mockResolvedValue({
      subject_key: "588870.SH",
      validated: 1,
    });
    apiMock.refreshReportLibraryETFProfile.mockResolvedValue({ status: "completed", errors: [] });
    apiMock.refreshReportLibraryHistoricalPercentile.mockResolvedValue({ status: "available" });
    apiMock.reconcileReportLibrary.mockResolvedValue({ status: "ok" });
    apiMock.deepReportArtifactUrl.mockImplementation(
      (reportId: string, artifactId: string, mode = "preview") => `/reports/${reportId}/artifacts/${artifactId}?download=${mode === "download" ? 1 : 0}`,
    );
    apiMock.runReportArtifactUrl.mockImplementation(
      (runId: string, mode = "preview") => `/runs/${runId}/report-artifact?download=${mode === "download" ? 1 : 0}`,
    );
    apiMock.sendReportArtifactToFeishu.mockResolvedValue({
      status: "delivered",
      filename: "report.md",
      target_id: "target-1",
      target_name: "飞书 · 群聊 · …123456",
    });
    apiMock.getFeishuDeliverySettings.mockResolvedValue({
      targets: [{
        target_id: "target-1",
        channel: "feishu",
        chat_id: "oc_test123456",
        chat_type: "group",
        session_key: "feishu:group:oc_test123456",
        status: "active",
        created_at: "2026-07-19T10:00:00Z",
      }],
      default_target_id: "target-1",
      effective_target_id: "target-1",
      requires_selection: false,
    });
    apiMock.reportLibraryArtifactUrl.mockImplementation(
      (artifact: { url?: string | null }, mode = "preview") => artifact.url
        ? `${artifact.url}?download=${mode === "download" ? 1 : 0}`
        : "#",
    );
  });

  async function openLegacyReports() {
    fireEvent.click(screen.getByRole("button", { name: "旧版报告" }));
    await screen.findByText("Backtest Report Library");
  }

  async function expandReportGroup(subject: string) {
    fireEvent.click(await screen.findByRole("button", { name: new RegExp(`^展开${subject}.*份报告$`) }));
  }

  async function expandFirstLibraryReportDetails() {
    const buttons = await screen.findAllByRole("button", { name: "展开报告详情" });
    fireEvent.click(buttons[0]);
  }

  it("lists backtest reports newest first with Full Report links and skips non-report runs", async () => {
    apiMock.listRuns.mockResolvedValue([
      {
        run_id: "old-report",
        status: "success",
        created_at: "2026-06-01T00:00:00Z",
        prompt: "Old report",
        codes: ["MSFT"],
        total_return: 0.05,
        sharpe: 1.1,
      },
      {
        run_id: "chat-only",
        status: "success",
        created_at: "2026-06-03T00:00:00Z",
        prompt: "No metrics",
        codes: [],
      },
      {
        run_id: "previous-report",
        status: "success",
        created_at: "2026-06-02T00:00:00Z",
        prompt: "Previous Apple report",
        codes: ["AAPL"],
        total_return: 0.07,
        sharpe: 1.2,
      },
      {
        run_id: "new-report",
        status: "success",
        created_at: "2026-06-04T00:00:00Z",
        prompt: "New report",
        codes: ["AAPL"],
        total_return: 0.12,
        sharpe: 1.8,
      },
    ]);

    render(<Reports />, { wrapper: MemoryRouter });

    await openLegacyReports();
    expect(apiMock.listRuns).toHaveBeenCalledWith(100);
    const groups = screen.getAllByRole("button", { name: /^展开.*份报告$/ });
    expect(groups[0]).toHaveAccessibleName(/AAPL.*2 份报告/);
    expect(groups[1]).toHaveAccessibleName(/MSFT/);
    expect(screen.queryByText("new-report")).not.toBeInTheDocument();
    await expandReportGroup("AAPL");
    await expandReportGroup("MSFT");
    await screen.findByText("new-report");
    expect(screen.queryByText("chat-only")).not.toBeInTheDocument();
    const reportRunLinks = screen.getAllByRole("link", { name: /-report$/ });
    expect(reportRunLinks[0]).toHaveAttribute("href", "/runs/new-report");
    expect(reportRunLinks[1]).toHaveAttribute("href", "/runs/previous-report");
    expect(reportRunLinks[2]).toHaveAttribute("href", "/runs/old-report");
    const fullReportLinks = screen.getAllByRole("link", { name: "Full Report" });
    expect(fullReportLinks[0]).toHaveAttribute("href", "/runs/new-report");
    expect(fullReportLinks[1]).toHaveAttribute("href", "/runs/previous-report");
    expect(fullReportLinks[2]).toHaveAttribute("href", "/runs/old-report");
  });

  it("filters reports by search text", async () => {
    apiMock.listRuns.mockResolvedValue([
      {
        run_id: "aapl-report",
        status: "success",
        created_at: "2026-06-04T00:00:00Z",
        prompt: "Apple strategy",
        codes: ["AAPL"],
        total_return: 0.12,
      },
      {
        run_id: "msft-report",
        status: "success",
        created_at: "2026-06-03T00:00:00Z",
        prompt: "Microsoft strategy",
        codes: ["MSFT"],
        total_return: 0.08,
      },
    ]);

    render(<Reports />, { wrapper: MemoryRouter });
    await openLegacyReports();
    await screen.findByRole("button", { name: /展开AAPL/ });

    fireEvent.change(screen.getByPlaceholderText("Search run id, prompt, symbol, status..."), {
      target: { value: "MSFT" },
    });

    expect(screen.queryByText("aapl-report")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /展开AAPL/ })).not.toBeInTheDocument();
    await expandReportGroup("MSFT");
    expect(screen.getByText("msft-report")).toBeInTheDocument();
  });

  it("shows deep research separately with quality gaps and persisted artifacts", async () => {
    apiMock.listRuns.mockResolvedValue([]);
    apiMock.listDeepReports.mockResolvedValue([{
      report_id: "report_0123456789abcdef",
      session_id: "session-1",
      attempt_id: "attempt-1",
      profile: "equity_deep_research",
      symbol: "301308.SZ",
      security_name: "江波龙",
      report_date: "2026-07-16",
      data_as_of: "2026-07-16T10:00:00+08:00",
      quality_status: "passed_with_gaps",
      status: "completed",
      analysis_modules: {
        financial_quality: { status: "passed" },
        terminal_scenarios: { status: "insufficient_evidence" },
      },
      artifacts: [
        { artifact_id: "markdown", artifact_type: "text/markdown", filename: "report.md", path: "report.md", available: true },
        { artifact_id: "pdf", artifact_type: "application/pdf", filename: "report.pdf", path: "report.pdf", available: true },
      ],
      validation_issues: [],
      created_at: "2026-07-16T10:00:00Z",
      updated_at: "2026-07-16T10:10:00Z",
      revision: 1,
      parent_report_id: null,
      generation_source: "portfolio_monitor_autopilot",
      generation_reason: "原报告过期、证据不足",
    }]);

    render(<Reports />, { wrapper: MemoryRouter });

    await openLegacyReports();
    expect(await screen.findByText("结构与穿透式深度研究")).toBeInTheDocument();
    await expandReportGroup("江波龙");
    expect(screen.getByText("江波龙（301308.SZ）穿透式深度研究")).toBeInTheDocument();
    expect(screen.getByText("AI 自主监控生成")).toBeInTheDocument();
    expect(screen.getByText("自动触发原因：原报告过期、证据不足")).toBeInTheDocument();
    expect(screen.getByText(/1 项研究内容仍需补充/)).toBeInTheDocument();
    expect(screen.getByText("已完成，部分结论保留")).toBeInTheDocument();
    expect(screen.queryByText("passed_with_gaps")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /阅读完整报告/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /预览 PDF/ })).toBeInTheDocument();
  });

  it("shows failed validation as diagnostics, exposes module failures, and hides unavailable PDF", async () => {
    apiMock.listRuns.mockResolvedValue([]);
    apiMock.listDeepReports.mockResolvedValue([{
      report_id: "report_failed",
      session_id: "session-1",
      attempt_id: "attempt-1",
      profile: "equity_deep_research",
      symbol: "603738.SH",
      security_name: "泰晶科技",
      report_date: "2026-07-17",
      data_as_of: "2026-07-17",
      quality_status: "failed_validation",
      status: "completed",
      analysis_modules: {
        executive_summary: { status: "failed_validation", reason: "missing section" },
        financial_quality: { status: "failed_validation", reason: "missing section" },
        terminal_scenarios: { status: "not_requested", reason: "missing TAM" },
      },
      artifacts: [
        { artifact_id: "diagnostic", artifact_type: "text/markdown", filename: "diagnostic.md", path: "diagnostic.md", available: true },
        { artifact_id: "pdf", artifact_type: "application/pdf", filename: "report.pdf", path: "report.pdf", available: false },
      ],
      validation_issues: ["missing_required_section:核心结论"],
      created_at: "2026-07-17T00:00:00Z",
      updated_at: "2026-07-17T00:10:00Z",
      revision: 1,
    }]);

    render(<Reports />, { wrapper: MemoryRouter });

    await openLegacyReports();
    await expandReportGroup("泰晶科技");
    expect(await screen.findByText("泰晶科技（603738.SH）穿透式深度研究")).toBeInTheDocument();
    expect(screen.getByText("尚未形成正式报告")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("没有通过发布前校验");
    expect(screen.getByText("核心结论 · 校验失败")).toBeInTheDocument();
    expect(screen.getByText("三张报表与财务质量 · 校验失败")).toBeInTheDocument();
    expect(screen.getByText("长期经营情景 · 未执行")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看未发布原因" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "预览 PDF" })).not.toBeInTheDocument();
    expect(apiMock.deepReportArtifactUrl).not.toHaveBeenCalledWith("report_failed", "pdf");
  });

  it("organizes new reports by subject and compares same-horizon viewpoints", async () => {
    const older = reportFixture({
      report_id: "report_week_old",
      source_id: "old",
      generated_at: "2026-07-17T18:00:00+08:00",
      data_as_of: "2026-07-17T15:00:00+08:00",
      coverage_status: "complete",
      report_quality_status: "passed",
      stance: "bullish",
      action: "add",
    });
    const latest = reportFixture({
      report_id: "report_week_new",
      source_id: "new",
      generated_at: "2026-07-18T09:30:00+08:00",
      data_as_of: "2026-07-18T09:20:00+08:00",
      coverage_status: "partial",
      report_quality_status: "passed_with_gaps",
      stance: "neutral",
      action: "observe",
    });
    const etfUniverse = {
      snapshot_id: "etfsnap_test",
      etf_symbol: "588870.SH",
      etf_name: "科创50ETF",
      tracked_index_code: "000688.SH",
      tracked_index_name: "科创50",
      data_as_of: "2026-06-30T00:00:00+00:00",
      retrieved_at: "2026-07-18T00:00:00+00:00",
      freshness_expires_at: "2026-08-14T00:00:00+00:00",
      quality_status: "passed",
      quality: "complete",
      provider_id: "csi_official_close_weight",
      source_type: "official_index_weight",
      source_ids: ["csi:000688:20260630:closeweight"],
      source_urls: ["https://example.test/000688closeweight.xls"],
      weight_scale: "fraction",
      weight_semantics: "tracked_index_weight",
      expected_component_count: 50,
      observed_component_count: 50,
      observed_weight_coverage: 0.99999,
      required_field_coverage: 1,
      universe_complete: true,
      partial_components_are_top_ranked: false,
      warnings: [],
      components: Array.from({ length: 12 }, (_, index) => ({
        symbol: index === 0 ? "688256.SH" : `688${String(index).padStart(3, "0")}.SH`,
        name: index === 0 ? "寒武纪" : `成分股${index + 1}`,
        weight: index === 0 ? 0.09204 : 0.08 - index * 0.004,
        metadata: {},
      })),
    };
    const instrumentProfile = {
      schema_version: 1,
      snapshot_id: "instrumentsnap_etf_test",
      symbol: "588870.SH",
      instrument_type: "etf",
      data_as_of: "2026-07-17T08:11:47+00:00",
      retrieved_at: "2026-07-19T05:00:00+00:00",
      quality_status: "complete",
      history_count: 2,
      identity: {
        symbol: "588870.SH",
        name: "科创50ETF汇添富",
        instrument_type: "etf",
        exchange: "SH",
        currency: "CNY",
        industry: null,
        region: null,
        concepts: [],
        listing_date: "2025-01-27",
      },
      metrics: [
        {
          key: "current_price", label: "最新价", value: 1.741, unit: "CNY", category: "market",
          status: "available", source_id: "quote", data_as_of: "2026-07-17T08:11:47+00:00",
          raw_field: "f43", semantics: "latest_exchange_quote",
        },
        {
          key: "total_market_cap", label: "ETF 市值（价格×份额）", value: 735_663_032, unit: "CNY", category: "scale",
          status: "available", source_id: "quote", data_as_of: "2026-07-17T08:11:47+00:00",
          raw_field: "f116", semantics: "market_price_times_total_units",
        },
        {
          key: "total_shares", label: "基金份额", value: 422_552_000, unit: "fund_units", category: "scale",
          status: "available", source_id: "quote", data_as_of: "2026-07-17T08:11:47+00:00",
          raw_field: "f84", semantics: "listed_fund_units",
        },
      ],
      sources: [{
        source_id: "quote",
        provider_id: "eastmoney_push2_quote",
        label: "东方财富实时行情",
        data_as_of: "2026-07-17T08:11:47+00:00",
        retrieved_at: "2026-07-19T05:00:00+00:00",
        url: "https://push2.eastmoney.com/api/qt/stock/get",
      }],
      warnings: ["ETF 自身不适用公司 PE/PB；跟踪指数估值应使用独立指数口径。"],
    };
    const valuationPercentile = {
      schema_version: 1,
      snapshot_id: "etfvaluation_ui_fixture",
      symbol: "588870.SH",
      tracked_index_code: "000688",
      tracked_index_name: "科创50",
      status: "available",
      lookback_years: 10,
      data_as_of: "2026-07-17T00:00:00+08:00",
      retrieved_at: "2026-07-19T05:00:00+00:00",
      mapping_method: "tracked_index_code_exact",
      metrics: [{
        key: "pe", label: "PE · 市盈率", value: 209.9431,
        percentile: 98.5517, temperature: "极热",
      }, {
        key: "pb", label: "PB · 市净率", value: 8.3463,
        percentile: 97.7931, temperature: "极热",
      }, {
        key: "ps", label: "PS · 市销率", value: 10.857,
        percentile: 90.3448, temperature: "极热",
      }],
      source: {
        source_id: "baifenwei-test",
        provider_id: "baifenwei_index_valuation",
        label: "百分位 · 指数估值",
        publisher: "百分位 baifenwei.com",
        verification_status: "public_secondary",
        url: "https://baifenwei.com/index/kc50/",
        methodology_url: "https://baifenwei.com/methodology/",
        retrieved_at: "2026-07-19T05:00:00+00:00",
      },
      unavailable_reason: null,
      warnings: ["百分位仅描述历史相对位置，不代表未来涨跌或买卖信号。"],
      history_count: 2,
    };
    const productField = (value: string | number | null, unit: string | null = null, dataAsOf = "2026-07-17") => ({
      value,
      status: value === null ? "missing" : "available",
      unit,
      data_as_of: dataAsOf,
      source_ids: ["official-product"],
      semantics: "fixture",
      note: value === null ? "官方页面未发布数值" : null,
    });
    const etfProduct = {
      schema_version: 1,
      profile_snapshot_id: "etfprofile_ui_fixture",
      symbol: "588870.SH",
      data_as_of: "2026-07-17",
      retrieved_at: "2026-07-19T05:00:00+00:00",
      snapshot_ids: {
        identity: "identity", index_methodology: "methodology", product_metrics: "metrics",
      },
      identity: {
        fund_full_name: productField("汇添富上证科创板50成份交易型开放式指数证券投资基金"),
        manager: productField("汇添富基金管理股份有限公司"),
        custodian: productField("中信证券股份有限公司"),
        exchange: productField("上海证券交易所"),
        contract_effective_date: productField("2025-01-20"),
        listing_date: productField("2025-01-27"),
        tracked_index_code: productField("000688.SH"),
        tracked_index_name: productField("上证科创板50成份指数"),
      },
      index_methodology: {
        version: productField("V1.1", null, "2020-12"),
        target_component_count: productField(50, "count", "2020-12"),
        single_constituent_weight_cap: productField(0.1, "ratio", "2020-12"),
        top_five_weight_cap: productField(0.4, "ratio", "2020-12"),
        review_frequency: productField("quarterly", null, "2020-12"),
      },
      product_metrics: {
        management_fee_rate: productField(0.0015, "ratio"),
        custody_fee_rate: productField(0.0005, "ratio"),
        unit_nav: productField(1.739, "CNY_per_fund_unit"),
        fund_units: productField(464_552_000, "fund_units"),
        published_net_assets: productField(710_743_392.87, "CNY", "2025-12-31"),
        exchange_market_value: productField(808_000_000, "CNY"),
        iopv: productField(null, "CNY_per_fund_unit"),
        premium_discount_rate: productField(null, "ratio"),
      },
      share_history: {
        current_units: 464_552_000,
        delta_1d: 42_000_000,
        delta_5d: 132_000_000,
        delta_20d: 147_000_000,
        estimated_net_flow_1d: 73_122_000,
        estimated_net_flow_semantics: "share_delta_times_current_market_price_proxy",
        observations: [],
      },
      peer_group: {
        tracked_index_code: "000688.SH",
        tracked_index_name: "上证科创板50成份指数",
        data_as_of: "2026-07-17",
        member_count: 2,
        official_index_mapping_count: 2,
        name_mapped_count: 0,
        estimated_net_flow_1d: 100_000_000,
        inflow_member_ratio_1d: 1,
        flow_coverage_ratio: 1,
        unit_change_coverage_ratio: 1,
        warnings: [],
        members: [{
          symbol: "588870.SH", name: "科创50ETF汇添富", manager: "汇添富基金",
          mapping_status: "official_index_code", data_as_of: "2026-07-17",
          current_units: 464_552_000, delta_1d: 42_000_000, delta_5d: 132_000_000,
          delta_20d: 147_000_000, current_price: 1.741,
          estimation_price: 1.741, estimation_price_type: "exchange_market_price",
          estimated_net_flow_1d: 73_122_000, source_ids: ["official-share"],
        }, {
          symbol: "588000.SH", name: "科创50ETF华夏", manager: "华夏基金",
          mapping_status: "official_index_code", data_as_of: "2026-07-17",
          current_units: 5_000_000_000, delta_1d: 15_000_000, delta_5d: 20_000_000,
          delta_20d: 30_000_000, current_price: 1.2,
          estimation_price: 1.2, estimation_price_type: "exchange_market_price",
          estimated_net_flow_1d: 18_000_000, source_ids: ["official-share"],
        }],
      },
      sources: [{
        source_id: "official-product", kind: "fund_product", title: "产品资料与费率",
        publisher: "汇添富基金管理股份有限公司", url: "https://example.test/product",
        content_hash: "abc", retrieved_at: "2026-07-19T05:00:00+00:00",
        verification_status: "official_primary",
      }],
      hard_gate_status: "passed",
      quality_status: "passed_with_gaps",
      missing_hard_fields: [],
      missing_optional_fields: ["iopv", "premium_discount_rate"],
      conflicts: [],
      refresh_errors: [],
      refresh_status: "completed",
      source_policy: {
        registry_version: "etf-source-rules-v1",
        rules: [{
          rule_id: "99fund.product_detail.v1",
          label: "汇添富产品资料与费率",
          phase: "product_profile",
          slot: "product",
          source_kind: "fund_product",
          publisher: "汇添富基金管理股份有限公司",
          verification_status: "official_primary",
          priority: 95,
          parser_id: "manager_fee_page_v1",
          response_type: "html",
          provides: ["management_fee_rate", "custody_fee_rate"],
          required_for_publish: false,
          freshness_days: 30,
          refresh_trigger: "when_stale_or_explicit_refresh",
          failure_policy: "warn_and_use_cache",
          status: "completed",
          source_id: "official-product",
          url: "https://example.test/product",
        }, {
          rule_id: "sse.etf_share_scale_history.v1",
          label: "上交所 ETF 日终份额",
          phase: "share_flow",
          slot: "sse_scale",
          source_kind: "fund_share_scale",
          publisher: "上海证券交易所",
          verification_status: "official_primary",
          priority: 100,
          parser_id: "sse_etf_scale_v1",
          response_type: "json",
          provides: ["fund_units", "fund_units_change"],
          required_for_publish: false,
          freshness_days: 1,
          refresh_trigger: "explicit_refresh_and_report_generation",
          failure_policy: "warn_and_use_cache",
          status: "completed",
          source_id: "official-share",
          url: "https://example.test/shares",
        }],
      },
    };
    apiMock.listReportLibrary.mockResolvedValue({ reports: [latest, older], next_cursor: null });
    apiMock.refreshReportLibraryInstrumentProfile.mockResolvedValue(instrumentProfile);
    apiMock.getReportLibrarySubject.mockResolvedValue({
      subject_type: "symbol",
      subject_key: "588870.SH",
      symbol: "588870.SH",
      security_name: "科创板100ETF华夏",
      instrument_profile: instrumentProfile,
      etf_universe: etfUniverse,
      etf_product: etfProduct,
      etf_valuation_percentile: valuationPercentile,
      profile: {
        etf: {
          instrument: instrumentProfile,
          universe: etfUniverse,
          product: etfProduct,
          valuation_percentile: valuationPercentile,
        },
      },
      source_bundle: {
        symbol: "588870.SH",
        generated_at: "2026-07-18T09:20:00+08:00",
        traceable_count: 5,
        excluded_count: 1,
        verification_counts: {
          official_primary: 1,
          live_retrieved: 1,
          source_recorded: 3,
          historical_context: 0,
        },
        verification_contract: {
          official_primary: "官方原文",
          live_retrieved: "实时取证",
          source_recorded: "来源已记录",
          historical_context: "历史背景",
        },
        domains: [{
          kind: "fund_product",
          label: "ETF 产品资料（上方已展示）",
          description: "与上方 ETF 产品概览重复",
          document_count: 1,
          documents: [{
            document_id: "research-cache:product-duplicate",
            kind: "fund_product",
            title: "ETF 产品资料重复项",
            summary: null,
            publisher: "基金管理人",
            provider: "official",
            published_at: "2026-07-18",
            retrieved_at: "2026-07-18T09:20:00+08:00",
            source_url: "https://example.test/product-duplicate",
            verification_status: "official_primary",
            metrics: [],
          }],
        }, {
          kind: "market_data",
          label: "ETF 行情（上方已展示）",
          description: "与上方标的关键资料重复",
          document_count: 1,
          documents: [{
            document_id: "research-cache:market-duplicate",
            kind: "market_data",
            title: "ETF 行情快照重复项",
            summary: null,
            publisher: "行情数据源",
            provider: "market",
            published_at: "2026-07-18",
            retrieved_at: "2026-07-18T09:20:00+08:00",
            source_url: null,
            verification_status: "source_recorded",
            metrics: [],
          }],
        }, {
          kind: "fundamental",
          label: "年度财务数据",
          description: "结构化年度指标，不等同于交易所年报原文",
          document_count: 1,
          documents: [{
            document_id: "research-cache:1",
            kind: "fundamental",
            title: "2025年报结构化财务数据",
            summary: "当前记录不是交易所年报 PDF 原文。",
            publisher: "eastmoney",
            provider: "eastmoney",
            published_at: "2026-03-13T00:00:00+08:00",
            retrieved_at: "2026-07-18T09:20:00+08:00",
            source_url: null,
            verification_status: "source_recorded",
            structured_status: "needs_review",
            structured_metrics_count: 13,
            structured_extractor_version: "v7",
            structured_failed_checks: ["balance_sheet_reconciles"],
            structured_auto_repair_available: true,
            metrics: [{ label: "ROE", value: 26.96, unit: "%" }],
          }],
        }, {
          kind: "news",
          label: "相关新闻",
          description: "保留发布方、原文链接和抓取时间",
          document_count: 1,
          documents: [{
            document_id: "research-cache:2",
            kind: "news",
            title: "科创板新闻更新",
            summary: null,
            publisher: "经济参考报",
            provider: "eastmoney",
            published_at: "2026-07-18T03:02:21+08:00",
            retrieved_at: "2026-07-18T09:20:00+08:00",
            source_url: "https://finance.eastmoney.com/example.html",
            verification_status: "live_retrieved",
            metrics: [],
          }],
        }, {
          kind: "report",
          label: "券商研报",
          description: "保留机构、分析师和发布日期",
          document_count: 1,
          documents: [{
            document_id: "research-cache:3",
            kind: "report",
            title: "AI 产业链跟踪",
            summary: "评级 买入 · 分析师 黄晨",
            publisher: "第一上海证券",
            provider: "eastmoney",
            published_at: "2026-04-28",
            retrieved_at: "2026-07-18T09:20:00+08:00",
            source_url: null,
            verification_status: "source_recorded",
            metrics: [],
          }],
        }],
      },
      current: {
        intraday: { latest: null, latest_complete: null },
        daily: {
          latest: currentCandidate(latest),
          latest_complete: currentCandidate(older),
        },
        weekly: { latest: null, latest_complete: null },
        structural: { latest: null, latest_complete: null },
      },
      timeline: [latest, older],
    });
    apiMock.compareReportLibrary.mockResolvedValue({
      selected: [],
      deltas: [{
        base_report_id: older.report_id,
        current_report_id: latest.report_id,
        relation: "diverged",
        changes: { stance: { before: "bullish", after: "neutral" } },
        research_delta: {},
      }],
      ai_summary: { status: "not_requested" },
    });

    render(<Reports />, { wrapper: MemoryRouter });

    expect(await screen.findByText("统一报告中心")).toBeInTheDocument();
    expect(screen.getByText("588870.SH")).toBeInTheDocument();
    expect(screen.getByText("2 份")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /科创板100ETF华夏/ }));

    const historyToggle = await screen.findByRole("button", { name: /历次报告/ });
    expect(historyToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("报告时间线")).not.toBeInTheDocument();
    const productSection = screen.getByRole("region", { name: "ETF 产品与指数" });
    expect(productSection).toHaveTextContent("汇添富基金管理股份有限公司");
    expect(productSection).toHaveTextContent("中信证券股份有限公司");
    expect(productSection).toHaveTextContent("V1.1");
    expect(productSection).toHaveTextContent("0.15%");
    expect(productSection).toHaveTextContent("单日份额变化");
    expect(productSection).toHaveTextContent("+4200.00 万份");
    expect(productSection).toHaveTextContent("同指数 ETF 流量分析");
    expect(productSection).toHaveTextContent("上证科创板50成份指数 · 000688.SH");
    expect(productSection).toHaveTextContent("市场价×份额变化");
    expect(productSection).toHaveTextContent("科创50ETF华夏");
    expect(productSection).toHaveTextContent("来源与获取规则");
    expect(productSection).toHaveTextContent("etf-source-rules-v1 · 2/2 成功");
    expect(productSection).toHaveTextContent("汇添富产品资料与费率");
    expect(productSection).toHaveTextContent("上交所 ETF 日终份额");
    expect(within(productSection).getByRole("link", { name: /产品资料与费率/ })).toHaveAttribute(
      "href", "https://example.test/product",
    );
    const profileSection = screen.getByRole("region", { name: "标的关键资料" });
    expect(profileSection).toHaveTextContent("ETF 市值（价格×份额）");
    expect(profileSection).toHaveTextContent("7.36 亿元");
    expect(profileSection).toHaveTextContent("4.23 亿份");
    expect(profileSection).toHaveTextContent("ETF 自身不适用公司 PE/PB");
    expect(within(profileSection).queryByText("市盈率 TTM")).not.toBeInTheDocument();
    expect(profileSection).toHaveTextContent("跟踪指数估值百分位");
    expect(profileSection).toHaveTextContent("科创50 · 000688");
    expect(profileSection).toHaveTextContent("209.94");
    expect(profileSection).toHaveTextContent("近 10 年百分位");
    expect(profileSection).toHaveTextContent("98.6%");
    expect(profileSection).toHaveTextContent("PB · 市净率");
    expect(profileSection).toHaveTextContent("PS · 市销率");
    expect(within(profileSection).getByRole("progressbar", { name: "PE · 市盈率近 10 年百分位" })).toHaveAttribute(
      "aria-valuenow", "98.5517",
    );
    expect(within(profileSection).getByRole("link", { name: /百分位 · 指数估值/ })).toHaveAttribute(
      "href", "https://baifenwei.com/index/kc50/",
    );
    fireEvent.click(within(profileSection).getByRole("button", { name: "更新标的资料" }));
    expect(apiMock.refreshReportLibraryETFProfile).toHaveBeenCalledWith("588870.SH");
    expect(screen.getByRole("button", { name: /详细资料与证据档案/ })).toHaveAttribute("aria-expanded", "true");
    const sourceSection = await screen.findByRole("region", { name: "标的资料与证据" });
    expect(sourceSection).toHaveTextContent("新闻、券商报告、年报/定期报告及其他补充证据");
    expect(sourceSection).toHaveTextContent("2025年报结构化财务数据");
    expect(sourceSection).toHaveTextContent("科创板新闻更新");
    expect(sourceSection).toHaveTextContent("AI 产业链跟踪");
    expect(sourceSection).toHaveTextContent("可追溯资料 3 条");
    expect(sourceSection).not.toHaveTextContent("ETF 产品资料（上方已展示）");
    expect(sourceSection).not.toHaveTextContent("ETF 行情（上方已展示）");
    expect(within(sourceSection).queryByRole("button", { name: "补齐历史年报" })).not.toBeInTheDocument();
    fireEvent.click(within(sourceSection).getByRole("button", { name: "自动修复结构化（1 份）" }));
    await vi.waitFor(() => {
      expect(apiMock.rebuildReportLibraryFinancialSnapshots).toHaveBeenCalledWith("588870.SH", true);
    });
    expect(within(sourceSection).getByRole("link", { name: "打开来源：科创板新闻更新" })).toHaveAttribute(
      "href",
      "https://finance.eastmoney.com/example.html",
    );
    const constituentSection = screen.getByRole("region", { name: "ETF成分股权重" });
    expect(constituentSection).toHaveTextContent("跟踪指数口径");
    expect(constituentSection).toHaveTextContent("科创50 · 000688.SH");
    expect(constituentSection).toHaveTextContent("9.204%");
    expect(within(constituentSection).queryByText("成分股12")).not.toBeInTheDocument();
    fireEvent.click(within(constituentSection).getByRole("button", { name: "查看全部 12 只成分股" }));
    expect(within(constituentSection).getByText("成分股12")).toBeInTheDocument();
    expect(screen.getByText(/最新观点存在缺口/)).toBeInTheDocument();
    expect(screen.getByText(/最近完整观点截至/)).toBeInTheDocument();
    expect(apiMock.getReportLibrarySubjectReports).not.toHaveBeenCalled();
    fireEvent.click(historyToggle);
    expect(await screen.findAllByText(/生成于/)).toHaveLength(2);
    expect(screen.getAllByText(/数据截至/).length).toBeGreaterThanOrEqual(3);

    const reportDetailButtons = screen.getAllByRole("button", { name: "展开报告详情" });
    reportDetailButtons.forEach((button) => fireEvent.click(button));
    const dailyViewpoints = screen.getAllByRole("button", { name: /当日 ·/ });
    fireEvent.click(dailyViewpoints[0]);
    fireEvent.click(dailyViewpoints[1]);
    fireEvent.click(screen.getByRole("button", { name: /结构化对照/ }));

    expect(await screen.findByText("同周期分化")).toBeInTheDocument();
    expect(apiMock.compareReportLibrary).toHaveBeenCalledWith([
      { report_id: latest.report_id, horizon: "daily" },
      { report_id: older.report_id, horizon: "daily" },
    ], false);
  });

  it("treats the instrument profile as authoritative and hides ETF-only cards for a company equity", async () => {
    const report = {
      ...reportFixture({
        report_id: "report_equity_sources",
        source_id: "equity-sources",
        generated_at: "2026-07-19T09:30:00+08:00",
        data_as_of: "2026-07-18T15:00:00+08:00",
        coverage_status: "complete",
        report_quality_status: "passed",
        stance: "neutral",
        action: "observe",
      }),
      family_id: "family_688256",
      subject_key: "688256.SH",
      symbol: "688256.SH",
      security_name: "中科寒武纪科技股份有限公司",
    };
    const equityProfile = {
      schema_version: 1,
      snapshot_id: "instrument_equity_test",
      symbol: "688256.SH",
      instrument_type: "company_equity" as const,
      data_as_of: "2026-07-18T15:00:00+08:00",
      retrieved_at: "2026-07-19T09:30:00+08:00",
      quality_status: "complete",
      history_count: 1,
      identity: {
        symbol: "688256.SH",
        name: "中科寒武纪科技股份有限公司",
        instrument_type: "company_equity" as const,
        exchange: "SH",
        currency: "CNY",
        concepts: [],
      },
      metrics: [],
      sources: [],
      warnings: [],
    };
    const sourceDocument = (kind: "fund_product" | "index_methodology" | "index_constituents" | "fund_share_scale" | "official_filing" | "news") => ({
      document_id: `equity-${kind}`,
      kind,
      title: `${kind} fixture`,
      publisher: kind === "news" ? "腾讯新闻" : "测试官方来源",
      retrieved_at: "2026-07-19T09:30:00+08:00",
      source_url: null,
      verification_status: kind === "news" ? "live_retrieved" as const : "official_primary" as const,
      metrics: [],
    });
    const domainLabels = {
      fund_product: "ETF 产品资料",
      index_methodology: "指数编制方案",
      index_constituents: "成分与权重",
      fund_share_scale: "ETF 份额",
      official_filing: "官方披露",
      news: "新闻",
    } as const;

    apiMock.listReportLibrary.mockResolvedValue({ reports: [report], next_cursor: null });
    apiMock.refreshReportLibraryInstrumentProfile.mockResolvedValue(equityProfile);
    apiMock.getReportLibrarySubject.mockResolvedValue({
      subject_type: "symbol",
      subject_key: report.subject_key,
      symbol: report.symbol,
      security_name: report.security_name,
      instrument_profile: equityProfile,
      profile: {
        etf: { instrument: equityProfile },
      },
      source_bundle: {
        symbol: report.symbol,
        generated_at: "2026-07-19T09:30:00+08:00",
        traceable_count: 6,
        excluded_count: 0,
        verification_counts: {
          official_primary: 5,
          live_retrieved: 1,
          source_recorded: 0,
          historical_context: 0,
        },
        verification_contract: {
          official_primary: "官方原文",
          live_retrieved: "实时抓取",
          source_recorded: "来源记录",
          historical_context: "历史缓存",
        },
        domains: (Object.keys(domainLabels) as Array<keyof typeof domainLabels>).map((kind) => ({
          kind,
          label: domainLabels[kind],
          description: `${domainLabels[kind]}说明`,
          document_count: 1,
          documents: [sourceDocument(kind)],
        })),
      },
      current: {
        intraday: { latest: null, latest_complete: null },
        daily: { latest: currentCandidate(report), latest_complete: currentCandidate(report) },
        weekly: { latest: null, latest_complete: null },
        structural: { latest: null, latest_complete: null },
      },
      timeline: [report],
    });
    const annualYears = [2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018];
    const annualJob = {
      schema_version: 1,
      job_id: "annual_backfill_equity",
      symbol: "688256.SH",
      years: annualYears,
      force: false,
      status: "running",
      stage: "discovering",
      message: "正在查找 2025 年官方年报",
      progress_pct: 3,
      year_progress: annualYears.map((year) => ({
        year,
        status: "pending",
        current_stage: "pending",
        message: "等待处理",
        phases: {
          discovery: "pending",
          download: "pending",
          parsing: "pending",
          validation: "pending",
        },
      })),
      result: null,
      error: null,
      created_at: "2026-07-19T10:00:00Z",
      updated_at: "2026-07-19T10:00:00Z",
    };
    apiMock.startReportLibraryAnnualReportBackfill.mockResolvedValue({
      status: "accepted",
      job_id: annualJob.job_id,
      deduplicated: false,
      job: annualJob,
    });
    apiMock.getReportLibraryAnnualReportBackfillJob.mockResolvedValue({
      ...annualJob,
      status: "completed",
      stage: "completed",
      message: "历史年报补齐完成",
      progress_pct: 100,
      year_progress: annualYears.map((year) => ({
        year,
        status: "completed",
        current_stage: "completed",
        message: `${year} 年报已归档并通过校验`,
        phases: {
          discovery: "completed",
          download: "completed",
          parsing: "completed",
          validation: "completed",
        },
      })),
      result: {
        status: "completed",
        coverage: {
          symbol: "688256.SH",
          requested_years: annualYears,
          covered_years: annualYears,
          archived_years: annualYears,
          analysis_ready_years: annualYears,
          needs_review_years: [],
          unusable_years: [],
          missing_years: [],
          coverage_ratio: 1,
          analysis_ready_ratio: 1,
          documents_by_year: {},
        },
      },
    });

    render(<Reports />, { wrapper: MemoryRouter });
    fireEvent.click(await screen.findByRole("button", { name: /中科寒武纪科技股份有限公司/ }));

    expect(await screen.findByRole("button", { name: /详细资料与证据档案/ })).toHaveAttribute("aria-expanded", "true");
    const sourceSection = await screen.findByRole("region", { name: "标的资料与证据" });
    expect(sourceSection).toHaveTextContent("新闻");
    expect(sourceSection).toHaveTextContent("官方披露");
    expect(sourceSection).toHaveTextContent("可追溯资料 2 条");
    expect(sourceSection).toHaveTextContent("1官方原文");
    expect(sourceSection).not.toHaveTextContent("ETF 产品资料");
    expect(sourceSection).not.toHaveTextContent("指数编制方案");
    expect(sourceSection).not.toHaveTextContent("成分与权重");
    expect(sourceSection).not.toHaveTextContent("ETF 份额");
    fireEvent.click(within(sourceSection).getByRole("button", { name: "补齐历史年报" }));
    await vi.waitFor(() => {
      expect(apiMock.startReportLibraryAnnualReportBackfill).toHaveBeenCalledWith(
        "688256.SH",
        annualYears,
        false,
      );
    });
    expect(await screen.findByRole("region", { name: "历史年报补齐任务" })).toBeInTheDocument();
    await vi.waitFor(() => {
      expect(apiMock.getReportLibraryAnnualReportBackfillJob).toHaveBeenCalledWith(
        "688256.SH",
        annualJob.job_id,
      );
      expect(screen.queryByRole("region", { name: "历史年报补齐任务" })).not.toBeInTheDocument();
    }, { timeout: 2500 });
    const officialSection = within(sourceSection).getByText("官方披露").closest("article");
    expect(officialSection).not.toBeNull();
    if (officialSection) {
      expect(within(officialSection).getByText("年报覆盖 8/8 年")).toBeInTheDocument();
    }
    expect(screen.queryByRole("region", { name: "ETF 产品与指数" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "ETF成分股权重" })).not.toBeInTheDocument();

    const profileSection = screen.getByRole("region", { name: "标的关键资料" });
    fireEvent.click(within(profileSection).getByRole("button", { name: "更新标的资料" }));
    await vi.waitFor(() => {
      expect(apiMock.refreshReportLibraryInstrumentProfile).toHaveBeenCalledWith("688256.SH");
      expect(apiMock.refreshReportLibraryHistoricalPercentile).toHaveBeenCalledWith("688256.SH");
    });
    expect(apiMock.refreshReportLibraryETFProfile).not.toHaveBeenCalled();
  });

  it("previews Markdown and PDF on the right, downloads explicitly, and sends the selected file to Feishu", async () => {
    const report = {
      ...reportFixture({
        report_id: "report_with_files",
        source_id: "daily-files",
        generated_at: "2026-07-18T09:30:00+08:00",
        data_as_of: "2026-07-18T09:20:00+08:00",
        coverage_status: "complete",
        report_quality_status: "passed",
        stance: "bullish",
        action: "add",
      }),
      artifacts: [
        {
          artifact_id: "holding-md",
          artifact_role: "holding_daily_markdown",
          filename: "2026-07-18_588870.md",
          media_type: "text/markdown",
          source_locator: "daily-run:daily-files:holding-md",
          available: true,
          revision: 1,
          url: "/portfolio/daily-runs/daily-files/artifacts/holding-md",
        },
        {
          artifact_id: "holding-pdf",
          artifact_role: "holding_daily_pdf",
          filename: "2026-07-18_588870.pdf",
          media_type: "application/pdf",
          source_locator: "daily-run:daily-files:holding-pdf",
          available: true,
          revision: 1,
          url: "/portfolio/daily-runs/daily-files/artifacts/holding-pdf",
        },
      ],
    };
    apiMock.listReportLibrary.mockResolvedValue({ reports: [report], next_cursor: null });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      text: async () => "# Markdown 预览正文\n\n确定性观点。",
    }));

    render(<Reports />, { wrapper: MemoryRouter });
    fireEvent.click(screen.getByRole("button", { name: "全部新报告" }));

    await expandReportGroup("科创板100ETF华夏");
    await expandFirstLibraryReportDetails();
    fireEvent.click(await screen.findByRole("button", { name: "预览 Markdown" }));
    expect(await screen.findByText("Markdown 预览正文")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "报告附件预览" })).toBeInTheDocument();
    const split = screen.getByTestId("report-preview-split");
    vi.spyOn(split, "getBoundingClientRect").mockReturnValue({
      x: 0,
      y: 0,
      top: 0,
      right: 1200,
      bottom: 800,
      left: 0,
      width: 1200,
      height: 800,
      toJSON: () => ({}),
    });
    const separator = screen.getByRole("separator", { name: "调整报告列表与预览宽度" });
    expect(separator).toHaveAttribute("aria-valuetext", "报告列表 54%，预览 46%");
    expect(screen.getByTestId("report-preview-region")).toHaveStyle({ width: "46%" });
    fireEvent.pointerDown(separator, { pointerId: 1, clientX: 720 });
    fireEvent.pointerMove(separator, { pointerId: 1, clientX: 720 });
    fireEvent.pointerUp(separator, { pointerId: 1, clientX: 720 });
    expect(separator).toHaveAttribute("aria-valuetext", "报告列表 60%，预览 40%");
    expect(screen.getByTestId("report-preview-region")).toHaveStyle({ width: "40%" });
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(separator).toHaveAttribute("aria-valuetext", "报告列表 58%，预览 42%");
    expect(screen.getByRole("link", { name: "下载 2026-07-18_588870.md" })).toHaveAttribute(
      "href",
      "/portfolio/daily-runs/daily-files/artifacts/holding-md?download=1",
    );

    const formats = screen.getByRole("navigation", { name: "报告文件格式" });
    fireEvent.click(within(formats).getByRole("button", { name: "PDF" }));
    expect(screen.getByTitle("科创板100ETF华夏 PDF 预览")).toHaveAttribute(
      "src",
      "/portfolio/daily-runs/daily-files/artifacts/holding-pdf?download=0",
    );
    expect(screen.getByRole("link", { name: "下载 2026-07-18_588870.pdf" })).toHaveAttribute(
      "href",
      "/portfolio/daily-runs/daily-files/artifacts/holding-pdf?download=1",
    );

    const sendButton = screen.getByRole("button", { name: "一键发送到飞书" });
    await vi.waitFor(() => expect(sendButton).toBeEnabled());
    fireEvent.click(sendButton);
    expect(await screen.findByText("已发送到飞书 · 群聊 · …123456：report.md")).toBeInTheDocument();
    expect(apiMock.sendReportArtifactToFeishu).toHaveBeenCalledWith({
      source: "report_library",
      report_id: "report_with_files",
      artifact_id: "holding-pdf",
    });
  });

  it("folds all new reports by subject and orders subjects by their latest generated report", async () => {
    const targetOlder = reportFixture({
      report_id: "target_old",
      source_id: "target-old",
      generated_at: "2026-07-17T18:00:00+08:00",
      data_as_of: "2026-07-17T15:00:00+08:00",
      coverage_status: "complete",
      report_quality_status: "passed",
      stance: "bullish",
      action: "add",
    });
    const targetLatest = reportFixture({
      report_id: "target_latest",
      source_id: "target-latest",
      generated_at: "2026-07-18T09:30:00+08:00",
      data_as_of: "2026-07-18T09:20:00+08:00",
      coverage_status: "partial",
      report_quality_status: "passed_with_gaps",
      stance: "neutral",
      action: "observe",
    });
    const newestSubject = {
      ...reportFixture({
        report_id: "newest_subject",
        source_id: "newest-subject",
        generated_at: "2026-07-18T10:00:00+08:00",
        data_as_of: "2026-07-18T09:50:00+08:00",
        coverage_status: "complete",
        report_quality_status: "passed",
        stance: "bullish",
        action: "add",
      }),
      family_id: "family_000651",
      subject_key: "000651.SZ",
      symbol: "000651.SZ",
      security_name: "格力电器",
    };
    apiMock.listReportLibrary.mockResolvedValue({
      reports: [targetOlder, newestSubject, targetLatest],
      next_cursor: null,
    });

    render(<Reports />, { wrapper: MemoryRouter });
    fireEvent.click(screen.getByRole("button", { name: "全部新报告" }));

    const groups = await screen.findAllByRole("button", { name: /^展开.*份报告$/ });
    expect(groups).toHaveLength(2);
    expect(groups[0]).toHaveAccessibleName(/格力电器.*1 份报告/);
    expect(groups[1]).toHaveAccessibleName(/科创板100ETF华夏.*2 份报告/);
    expect(screen.queryByText(/^生成于/)).not.toBeInTheDocument();

    fireEvent.click(groups[1]);
    const generatedTimes = await screen.findAllByText(/^生成于/);
    expect(generatedTimes[0]).toHaveTextContent("09:30");
    expect(generatedTimes[1]).toHaveTextContent("07/17");

    fireEvent.click(screen.getByRole("button", { name: /^收起科创板100ETF华夏.*2 份报告$/ }));
    expect(screen.queryByText(/^生成于/)).not.toBeInTheDocument();
  });

  it("shows structured monitoring candidates, warnings, and watch-only status without implying execution", async () => {
    const report = {
      ...reportFixture({
        report_id: "daily_structured",
        source_id: "daily-structured",
        generated_at: "2026-07-18T09:00:00+08:00",
        data_as_of: "2026-07-18T08:55:00+08:00",
        coverage_status: "partial",
        report_quality_status: "passed_with_gaps",
        stance: "neutral",
        action: "observe",
      }),
      monitoring_bundle: {
        schema_version: 1 as const,
        symbol: "588870.SH",
        instrument_type: "etf" as const,
        horizon: "daily" as const,
        generated_at: "2026-07-18T09:00:00+08:00",
        data_as_of: "2026-07-18T08:55:00+08:00",
        valid_from: "2026-07-18T09:00:00+08:00",
        valid_until: "2026-07-25T09:00:00+08:00",
        review_due_at: "2026-07-19T09:00:00+08:00",
        price_basis: { adjustment: "raw" as const, currency: "CNY" as const, tick_size: 0.001 },
        monitoring_status: "available" as const,
        price_volume_context: {
          policy: {
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
          data_mode: "single_source" as const,
          source_count: 1,
          sources: ["tencent"],
          single_source_authorized: false,
          warnings: ["当前仅有单一数据源且未获明确授权。"],
          refresh_attempted: true,
          refresh_succeeded: true,
        },
        candidates: [{
          scenario_id: "candidate-1",
          candidate_id: "candidate-1",
          scenario_family_id: "scenario-1",
          client_rule_id: "rule-1",
          label: "突破阻力后的观察",
          intent: "breakout" as const,
          evidence_refs: ["claim-1"],
          original_level: { kind: "price" as const, value: 1.85, unit: "CNY", adjustment: "raw" as const },
          trigger: { kind: "price_cross_above" as const, threshold: 1.85, interval: "5m" as const, confirmation_count: 2 },
          approach_policy: { distance_bps: 100, source: "report" as const, check_interval: "1m" as const },
          volume_confirmation: {
            metric: "same_bucket_5m_volume_ratio" as const,
            comparator: "gte" as const,
            threshold: 1.5,
            min_samples: 5,
            mode: "classify_only" as const,
            unit: "ratio",
          },
          resolution_policy: { rejection_hysteresis_bps: 30, max_observation_bars: 6, close_action: "unresolved" as const },
          invalidation: { kind: "price_cross_below" as const, level: 1.80 },
          rationale: "等待人工复核",
          source_conditions: [{
            condition_id: "daily-volume",
            source_text: "日线放量确认",
            role: "required" as const,
            coverage_status: "unsupported" as const,
            reason: "当前实时引擎不支持日线量价条件",
            evidence_refs: ["claim-1"],
          }],
          automation_status: "watch_only" as const,
          change_type: "new" as const,
        }],
        scenario_changes: [],
        validation_errors: [],
        source: "structured_daily_report" as const,
        activation_policy: "manual_confirmation_required" as const,
        trade_execution: "forbidden" as const,
      },
    };
    apiMock.listReportLibrary.mockResolvedValue({ reports: [report], next_cursor: null });

    render(<Reports />, { wrapper: MemoryRouter });
    fireEvent.click(screen.getByRole("button", { name: "全部新报告" }));
    await expandReportGroup("科创板100ETF华夏");
    await expandFirstLibraryReportDetails();

    expect(await screen.findByRole("region", { name: "结构化监控候选" })).toBeInTheDocument();
    expect(screen.getByText("当前仅有单一数据源且未获明确授权。")).toBeInTheDocument();
    expect(screen.getByText("仅观察")).toBeInTheDocument();
    expect(screen.queryByText("人工复核候选")).not.toBeInTheDocument();
    expect(screen.getByText(/当前实时引擎不支持日线量价条件/)).toBeInTheDocument();
    expect(screen.getByText(/交易执行 禁止/)).toBeInTheDocument();
  });

  it("shows weekly conclusions, prior validation, source expiry, and weekly-only filtering", async () => {
    const report = {
      ...reportFixture({
        report_id: "weekly_588870_20260717",
        source_id: "weekly-run-1:588870.SH",
        generated_at: "2026-07-18T09:00:00+08:00",
        data_as_of: "2026-07-17T15:00:00+08:00",
        coverage_status: "partial",
        report_quality_status: "passed_with_gaps",
        stance: "bullish",
        action: "observe",
      }),
      report_kind: "weekly_review" as const,
      weekly_review: {
        week_start: "2026-07-13",
        week_end: "2026-07-17",
        generated_at: "2026-07-18T09:00:00+08:00",
        data_as_of: "2026-07-17T15:00:00+08:00",
        valid_from: "2026-07-18T09:00:00+08:00",
        valid_until: "2026-07-24T15:30:00+08:00",
        review_due_at: "2026-07-24T15:30:00+08:00",
        source_valid_until: "2026-07-24T15:30:00+08:00",
        quality_status: "passed_with_gaps" as const,
        coverage_status: "partial" as const,
        weekly_view: {
          trend_stage: "上升",
          trend_direction: "向上",
          trend_strength: "中",
          week_return_pct: 2.35,
          summary: "本周价格抬升，但量价确认仍需日线数据。",
        },
        previous_week_validation: [{
          scenario_family_id: "scenario-breakout",
          outcome: "approached",
          summary: "本周接近阻力，但日线量能未确认。",
        }],
        key_levels: [],
        scenario_changes: [{
          scenario_family_id: "scenario-breakout",
          candidate_id: "candidate-weekly",
          previous_candidate_id: "candidate-prior",
          change_type: "raised" as const,
          change_details: { summary: "阻力位上移" },
        }],
        data_gaps: ["ETF跟踪误差数据暂缺"],
      },
      monitoring_bundle: {
        schema_version: 1 as const,
        symbol: "588870.SH",
        instrument_type: "etf" as const,
        horizon: "weekly" as const,
        generated_at: "2026-07-18T09:00:00+08:00",
        data_as_of: "2026-07-17T15:00:00+08:00",
        valid_from: "2026-07-18T09:00:00+08:00",
        valid_until: "2026-07-24T15:30:00+08:00",
        review_due_at: "2026-07-24T15:30:00+08:00",
        source_valid_until: "2026-07-24T15:30:00+08:00",
        price_basis: { adjustment: "raw" as const, currency: "CNY" as const, tick_size: 0.001 },
        monitoring_status: "available" as const,
        price_volume_context: {
          policy: {
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
          data_mode: "verified" as const,
          source_count: 2,
          sources: ["tencent", "mootdx"],
          single_source_authorized: false,
          warnings: [],
          refresh_attempted: true,
          refresh_succeeded: true,
        },
        candidates: [{
          scenario_id: "candidate-weekly",
          candidate_id: "candidate-weekly",
          scenario_family_id: "scenario-breakout",
          client_rule_id: "weekly-rule",
          label: "周度阻力突破",
          intent: "breakout" as const,
          evidence_refs: ["claim-1"],
          original_level: { kind: "price" as const, value: 1.85, unit: "CNY", adjustment: "raw" as const },
          trigger: { kind: "price_cross_above" as const, threshold: 1.85, interval: "1m" as const, confirmation_count: 1 },
          approach_policy: { distance_bps: 100, source: "report" as const, check_interval: "1m" as const },
          volume_confirmation: { metric: "same_bucket_5m_volume_ratio" as const, comparator: "gte" as const, threshold: 1.5, min_samples: 5, mode: "classify_only" as const, unit: "ratio" },
          resolution_policy: { rejection_hysteresis_bps: 30, max_observation_bars: 6, close_action: "unresolved" as const },
          rationale: "等待日线确认",
          source_conditions: [{
            condition_id: "daily-confirmation",
            source_text: "日线收盘突破并放量",
            role: "required" as const,
            coverage_status: "awaiting_data" as const,
            reason: "分钟价格不能替代日线收盘与五日均量",
            evidence_refs: ["claim-1"],
          }],
          automation_status: "watch_only" as const,
          change_type: "raised" as const,
        }],
        scenario_changes: [],
        validation_errors: [],
        source: "structured_weekly_report" as const,
        source_report_id: "weekly_588870_20260717",
        source_period: { week_start: "2026-07-13", week_end: "2026-07-17", label: "2026-07-13 至 2026-07-17" },
        activation_policy: "manual_confirmation_required" as const,
        trade_execution: "forbidden" as const,
      },
    };
    apiMock.listReportLibrary.mockResolvedValue({ reports: [report], next_cursor: null });

    render(<Reports />, { wrapper: MemoryRouter });
    fireEvent.click(screen.getByRole("button", { name: "全部新报告" }));
    await expandReportGroup("科创板100ETF华夏");
    await expandFirstLibraryReportDetails();

    expect(await screen.findByRole("region", { name: "周度复盘摘要" })).toHaveTextContent("2026-07-13 至 2026-07-17");
    expect(screen.getByText(/本周 \+2.35%/)).toBeInTheDocument();
    expect(screen.getByText("已接近")).toBeInTheDocument();
    expect(screen.getByText(/ETF跟踪误差数据暂缺/)).toBeInTheDocument();
    expect(screen.getByText(/周报结构化 JSON/)).toBeInTheDocument();
    expect(screen.getByText(/可复核 0 · 仅观察 1/)).toBeInTheDocument();
    expect(screen.getByText(/分钟价格不能替代日线收盘与五日均量/)).toBeInTheDocument();

    fireEvent.change(screen.getByRole("combobox", { name: "报告类型筛选" }), { target: { value: "weekly_review" } });
    expect(apiMock.listReportLibrary).toHaveBeenLastCalledWith(expect.objectContaining({ reportKind: "weekly_review" }));
  });
});

function reportFixture({
  report_id,
  source_id,
  generated_at,
  data_as_of,
  coverage_status,
  report_quality_status,
  stance,
  action,
}: {
  report_id: string;
  source_id: string;
  generated_at: string;
  data_as_of: string;
  coverage_status: "complete" | "partial";
  report_quality_status: "passed" | "passed_with_gaps";
  stance: "bullish" | "neutral";
  action: "add" | "observe";
}) {
  return {
    report_id,
    family_id: "family_588870",
    report_kind: "daily_holding" as const,
    subject_type: "symbol" as const,
    subject_key: "588870.SH",
    symbol: "588870.SH",
    security_name: "科创板100ETF华夏",
    status: "published" as const,
    report_quality_status,
    coverage_status,
    generated_at,
    data_as_of,
    source_type: "daily_run",
    source_id,
    source_revision: 1,
    knowledge_link: { claim_ids: [] },
    viewpoints: [{
      viewpoint_id: `${report_id}_daily`,
      report_id,
      horizon: "daily" as const,
      stance,
      action,
      confidence: coverage_status === "complete" ? "high" as const : "medium" as const,
      reason_claim_ids: [],
      risk_claim_ids: [],
      condition_claim_ids: [],
      invalidation_claim_ids: [],
      valid_from: data_as_of,
      valid_until: null,
    }],
    artifacts: [],
    relations: [],
    created_at: generated_at,
    updated_at: generated_at,
  };
}

function currentCandidate(report: ReturnType<typeof reportFixture>) {
  return {
    report_id: report.report_id,
    report_kind: report.report_kind,
    symbol: report.symbol,
    security_name: report.security_name,
    data_as_of: report.data_as_of,
    generated_at: report.generated_at,
    report_quality_status: report.report_quality_status,
    coverage_status: report.coverage_status,
    viewpoint: report.viewpoints[0],
  };
}
