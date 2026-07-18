import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Reports } from "../Reports";

const apiMock = vi.hoisted(() => ({
  listRuns: vi.fn(),
  listDeepReports: vi.fn(),
  deepReportArtifactUrl: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: apiMock,
}));

describe("Reports page", () => {
  beforeEach(() => {
    apiMock.listRuns.mockReset();
    apiMock.listDeepReports.mockReset();
    apiMock.listDeepReports.mockResolvedValue([]);
    apiMock.deepReportArtifactUrl.mockImplementation(
      (reportId: string, artifactId: string) => `/reports/${reportId}/artifacts/${artifactId}`,
    );
  });

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

    expect(await screen.findByText("Backtest Report Library")).toBeInTheDocument();
    expect(apiMock.listRuns).toHaveBeenCalledWith(100);
    expect(screen.queryByText("chat-only")).not.toBeInTheDocument();
    const reportRunLinks = screen.getAllByRole("link", { name: /-report$/ });
    expect(reportRunLinks[0]).toHaveAttribute("href", "/runs/new-report");
    expect(reportRunLinks[1]).toHaveAttribute("href", "/runs/old-report");
    const fullReportLinks = screen.getAllByRole("link", { name: "Full Report" });
    expect(fullReportLinks[0]).toHaveAttribute("href", "/runs/new-report");
    expect(fullReportLinks[1]).toHaveAttribute("href", "/runs/old-report");
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
    await screen.findByText("aapl-report");

    fireEvent.change(screen.getByPlaceholderText("Search run id, prompt, symbol, status..."), {
      target: { value: "MSFT" },
    });

    expect(screen.queryByText("aapl-report")).not.toBeInTheDocument();
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

    expect(await screen.findByText("穿透式单股深度研究")).toBeInTheDocument();
    expect(screen.getByText("江波龙（301308.SZ）穿透式深度研究")).toBeInTheDocument();
    expect(screen.getByText("AI 自主监控生成")).toBeInTheDocument();
    expect(screen.getByText("自动触发原因：原报告过期、证据不足")).toBeInTheDocument();
    expect(screen.getByText(/1 项研究内容仍需补充/)).toBeInTheDocument();
    expect(screen.getByText("已完成，部分结论保留")).toBeInTheDocument();
    expect(screen.queryByText("passed_with_gaps")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /阅读完整报告/ })).toHaveAttribute(
      "href", "/reports/report_0123456789abcdef/artifacts/markdown",
    );
    expect(screen.getByRole("link", { name: /PDF/ })).toHaveAttribute(
      "href", "/reports/report_0123456789abcdef/artifacts/pdf",
    );
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
        { artifact_id: "markdown", artifact_type: "text/markdown", filename: "diagnostic.md", path: "diagnostic.md", available: true },
        { artifact_id: "pdf", artifact_type: "application/pdf", filename: "report.pdf", path: "report.pdf", available: false },
      ],
      validation_issues: ["missing_required_section:核心结论"],
      created_at: "2026-07-17T00:00:00Z",
      updated_at: "2026-07-17T00:10:00Z",
      revision: 1,
    }]);

    render(<Reports />, { wrapper: MemoryRouter });

    expect(await screen.findByText("泰晶科技（603738.SH）穿透式深度研究")).toBeInTheDocument();
    expect(screen.getByText("尚未形成正式报告")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("没有通过发布前校验");
    expect(screen.getByText("核心结论 · 校验失败")).toBeInTheDocument();
    expect(screen.getByText("三张报表与财务质量 · 校验失败")).toBeInTheDocument();
    expect(screen.getByText("长期经营情景 · 未执行")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看未发布原因" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "PDF" })).not.toBeInTheDocument();
    expect(apiMock.deepReportArtifactUrl).not.toHaveBeenCalledWith("report_failed", "pdf");
  });
});
