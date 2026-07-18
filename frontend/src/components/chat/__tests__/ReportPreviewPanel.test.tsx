import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ApiError, api } from "@/lib/api";
import { ReportPreviewPanel } from "../ReportPreviewPanel";

vi.mock("react-markdown", () => ({
  default: ({ children }: { children: string }) => <div data-testid="preview-markdown">{children}</div>,
}));
vi.mock("remark-gfm", () => ({ default: () => {} }));
vi.mock("rehype-highlight", () => ({ default: () => {} }));

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ReportPreviewPanel", () => {
  it("loads the complete run Markdown before the compiled report", async () => {
    vi.spyOn(api, "getRunReportPreview").mockResolvedValue({
      run_id: "run-1",
      title: "泰晶科技（603738.SH）深度研究报告",
      filename: "泰晶科技_603738_深度研究报告.md",
      relative_path: "agent/runs/run-1/泰晶科技_603738_深度研究报告.md",
      content: "# 完整报告\n\n## 核心结论\n正文",
      updated_at: "2026-07-17T00:47:43+08:00",
      source: "run_artifact",
    });
    const fallback = vi.spyOn(api, "getDeepReport");

    render(
      <ReportPreviewPanel
        target={{ runId: "run-1", reportId: "report-1" }}
        onClose={vi.fn()}
      />,
    );

    expect(await screen.findByText("泰晶科技（603738.SH）深度研究报告")).toBeInTheDocument();
    expect(screen.getByTestId("preview-markdown")).toHaveTextContent("核心结论");
    expect(screen.getByText("完整运行产物")).toBeInTheDocument();
    expect(screen.getByTitle("agent/runs/run-1/泰晶科技_603738_深度研究报告.md")).toBeInTheDocument();
    expect(fallback).not.toHaveBeenCalled();
  });

  it("falls back to the persisted Deep Report when the run has no Markdown", async () => {
    vi.spyOn(api, "getRunReportPreview").mockRejectedValue(new ApiError("not found", 404));
    vi.spyOn(api, "getDeepReport").mockResolvedValue({
      report_id: "report-1",
      session_id: "session-1",
      attempt_id: "attempt-1",
      profile: "equity_deep_research",
      symbol: "301308.SZ",
      security_name: "江波龙",
      report_date: "2026-07-17",
      data_as_of: "2026-07-16",
      quality_status: "passed_with_gaps",
      status: "completed",
      analysis_modules: {},
      artifacts: [{
        artifact_id: "markdown",
        artifact_type: "text/markdown",
        filename: "江波龙穿透式深度研究.md",
        path: "hidden-local-path",
        available: true,
      }],
      validation_issues: [],
      created_at: "2026-07-17T00:00:00+08:00",
      updated_at: "2026-07-17T01:00:00+08:00",
      revision: 1,
      content: "# 江波龙报告\n\n正式产物",
    });

    render(
      <ReportPreviewPanel
        target={{ runId: "run-1", reportId: "report-1" }}
        onClose={vi.fn()}
      />,
    );

    expect(await screen.findByText("江波龙（301308.SZ）穿透式深度研究")).toBeInTheDocument();
    expect(screen.getByTestId("preview-markdown")).toHaveTextContent("正式产物");
    expect(api.getDeepReport).toHaveBeenCalledWith("report-1", true);
  });

  it("closes from the panel header", async () => {
    const onClose = vi.fn();
    vi.spyOn(api, "getRunReportPreview").mockResolvedValue({
      run_id: "run-1",
      title: "报告",
      filename: "report.md",
      relative_path: "agent/runs/run-1/report.md",
      content: "正文",
      updated_at: "2026-07-17T00:00:00+08:00",
      source: "run_artifact",
    });
    render(<ReportPreviewPanel target={{ runId: "run-1" }} onClose={onClose} />);

    await userEvent.setup().click(screen.getByTitle("关闭预览"));

    expect(onClose).toHaveBeenCalledOnce();
  });

  it("switches between the current Markdown and the immutable revision diff", async () => {
    vi.spyOn(api, "getDeepReport").mockResolvedValue({
      report_id: "report-2",
      session_id: "session-1",
      attempt_id: "attempt-2",
      profile: "equity_deep_research",
      symbol: "603738.SH",
      security_name: "泰晶科技",
      report_date: "2026-07-17",
      data_as_of: "2026-07-16",
      quality_status: "passed_with_gaps",
      status: "completed",
      analysis_modules: {},
      artifacts: [
        { artifact_id: "markdown", artifact_type: "text/markdown", filename: "report.md", path: "hidden", available: true },
        { artifact_id: "diff", artifact_type: "text/markdown", filename: "diff.md", path: "hidden", available: true },
      ],
      validation_issues: [],
      created_at: "2026-07-17T00:00:00+08:00",
      updated_at: "2026-07-17T01:00:00+08:00",
      revision: 2,
      content: "# 当前报告\n正式正文",
      delivery_kind: "report",
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("# 版本差异\n变更内容", { status: 200 })));

    render(
      <ReportPreviewPanel
        target={{ reportId: "report-2", artifactId: "markdown" }}
        onClose={vi.fn()}
      />,
    );

    expect(await screen.findByText("当前报告")).toHaveAttribute("aria-pressed", "true");
    await userEvent.setup().click(screen.getByText("与上一版差异"));
    expect(await screen.findByText("版本差异", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByTestId("preview-markdown")).toHaveTextContent("版本差异");
  });
});
