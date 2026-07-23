import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { buildPdfReportTitle, MessageBubble, sanitizePdfFilename } from "../MessageBubble";
import type { AgentMessage } from "@/types/agent";
import { api } from "@/lib/api";

// Mock react-markdown (heavy dependency, renders raw content in tests)
vi.mock("react-markdown", () => ({
  default: ({ children }: { children: string }) => <div data-testid="markdown">{children}</div>,
}));
vi.mock("remark-gfm", () => ({ default: () => {} }));
vi.mock("rehype-highlight", () => ({ default: () => {} }));

// Mock RunCompleteCard (complex component with ECharts)
vi.mock("../RunCompleteCard", () => ({
  RunCompleteCard: ({ msg }: { msg: AgentMessage }) => (
    <div data-testid="run-complete-card">Run: {msg.runId}</div>
  ),
}));

function makeMsg(overrides: Partial<AgentMessage> = {}): AgentMessage {
  return {
    id: "msg-1",
    type: "answer",
    content: "test",
    timestamp: new Date(2024, 0, 1, 14, 30).getTime(),
    ...overrides,
  };
}

describe("MessageBubble", () => {
  describe("user messages", () => {
    it("renders user content in a styled bubble", () => {
      render(<MessageBubble msg={makeMsg({ type: "user", content: "Hello agent!" })} />);
      expect(screen.getByText("Hello agent!")).toBeInTheDocument();
    });

    it("shows timestamp", () => {
      render(<MessageBubble msg={makeMsg({ type: "user" })} />);
      expect(screen.getByText("14:30")).toBeInTheDocument();
    });

    it("does not offer branching from a persisted user message", () => {
      const onFork = vi.fn();
      const msg = makeMsg({ type: "user", sourceMessageId: "server-msg-1" });
      render(<MessageBubble msg={msg} onFork={onFork} />);

      expect(screen.queryByTitle("Fork conversation from here")).not.toBeInTheDocument();
    });
  });

  describe("answer messages", () => {
    it("renders markdown content", () => {
      render(<MessageBubble msg={makeMsg({ type: "answer", content: "Here is the **analysis**" })} />);
      expect(screen.getByTestId("markdown")).toHaveTextContent("Here is the **analysis**");
    });

    it("keeps follow-up prompts after a disclaimer visible in chat", () => {
      const content = [
        "研究结论",
        "",
        "> ⚠ 免责声明：以上为研究分析，不构成投资建议。市场有风险，投资需谨慎。",
        "有什么需要我进一步深挖的吗？比如：技术面分析？",
      ].join("\n");

      render(<MessageBubble msg={makeMsg({ type: "answer", content })} />);

      expect(screen.getByTestId("markdown")).toHaveTextContent("免责声明");
      expect(screen.getByTestId("markdown")).toHaveTextContent("有什么需要");
      expect(screen.getByTestId("markdown")).toHaveTextContent("技术面分析");
    });

    it("keeps normal follow-up wording when no disclaimer is present", () => {
      render(<MessageBubble msg={makeMsg({ type: "answer", content: "有什么需要重点验证？" })} />);
      expect(screen.getByTestId("markdown")).toHaveTextContent("有什么需要重点验证？");
    });

    it("uses persisted artifacts and can continue the same deep-report session", async () => {
      vi.spyOn(api, "deepReportArtifactUrl").mockReturnValue(
        "/api/reports/report_0123456789abcdef/artifacts/pdf",
      );
      vi.spyOn(api, "followUpDeepReport").mockResolvedValue({
        message_id: "msg-next",
        attempt_id: "attempt-next",
        parent_report_id: "report_0123456789abcdef",
      });
      render(<MessageBubble msg={makeMsg({
        type: "answer",
        content: "# 江波龙（301308.SZ）穿透式深度研究",
        reportId: "report_0123456789abcdef",
        reportQualityStatus: "passed_with_gaps",
        reportSymbol: "301308.SZ",
        reportDataAsOf: "2026-07-16",
        reportMissingModules: ["terminal_scenarios"],
        reportPdfAvailable: true,
        reportGenerationSource: "portfolio_monitor_autopilot",
        reportGenerationReason: "原报告过期",
      })} />);

      expect(screen.getByText("穿透式深度研究")).toBeInTheDocument();
      expect(screen.getByText("已完成，部分结论保留")).toBeInTheDocument();
      expect(screen.getByText(/数据更新至/)).toBeInTheDocument();
      expect(screen.getByText("仍需补充的研究内容（1）")).toBeInTheDocument();
      expect(screen.queryByText("passed_with_gaps")).not.toBeInTheDocument();
      expect(screen.getByText("AI 自主监控生成")).toBeInTheDocument();
      expect(screen.getByText("自动触发原因：原报告过期")).toBeInTheDocument();
      expect(screen.getByTitle("下载已校验的深度研究 PDF")).toHaveAttribute(
        "href", "/api/reports/report_0123456789abcdef/artifacts/pdf",
      );
      expect(api.deepReportArtifactUrl).toHaveBeenCalledWith(
        "report_0123456789abcdef", "pdf",
      );
      await userEvent.setup().click(screen.getByRole("button", { name: "继续研究" }));
      expect(api.followUpDeepReport).toHaveBeenCalledWith(
        "report_0123456789abcdef",
        expect.stringContaining("证据缺口"),
      );
    });

    it("hands a new-data revision back to the Agent streaming UI", async () => {
      const onDeepReportTaskStarted = vi.fn();
      vi.spyOn(api, "refreshDeepReport").mockResolvedValue({
        message_id: "msg-refresh",
        attempt_id: "attempt-refresh",
        parent_report_id: "report_0123456789abcdef",
        revision_mode: "full_refresh",
      });
      render(<MessageBubble
        msg={makeMsg({
          type: "answer",
          reportId: "report_0123456789abcdef",
          reportQualityStatus: "failed_validation",
        })}
        onDeepReportTaskStarted={onDeepReportTaskStarted}
      />);

      await userEvent.setup().click(screen.getByRole("button", { name: "用新数据更新" }));

      expect(onDeepReportTaskStarted).toHaveBeenCalledWith({
        action: "refresh",
        reportId: "report_0123456789abcdef",
        parentReportId: "report_0123456789abcdef",
        attemptId: "attempt-refresh",
        messageId: "msg-refresh",
      });
    });

    it("requires explicit consent before starting higher-cost evidence enrichment", async () => {
      const onDeepReportTaskStarted = vi.fn();
      const confirm = vi.spyOn(window, "confirm")
        .mockReturnValueOnce(false)
        .mockReturnValueOnce(true);
      const enrich = vi.spyOn(api, "enrichDeepReport").mockResolvedValue({
        message_id: "msg-enrich",
        attempt_id: "attempt-enrich",
        parent_report_id: "report_with_gaps",
        revision_mode: "full_refresh",
        research_depth: "extended",
        token_notice_acknowledged: true,
      });
      render(<MessageBubble
        msg={makeMsg({
          type: "answer",
          reportId: "report_with_gaps",
          reportQualityStatus: "passed_with_gaps",
          reportMissingModules: ["holding_penetration"],
        })}
        onDeepReportTaskStarted={onDeepReportTaskStarted}
      />);

      const button = screen.getByRole("button", { name: "补齐资料并重新生成" });
      expect(button).toHaveAccessibleDescription(/耗时和 Token 消耗通常更高/);
      await userEvent.setup().click(button);
      expect(enrich).not.toHaveBeenCalled();

      await userEvent.setup().click(button);

      expect(confirm).toHaveBeenCalledTimes(2);
      expect(enrich).toHaveBeenCalledWith(
        "report_with_gaps",
        expect.stringContaining("缺少的往年数据"),
      );
      expect(onDeepReportTaskStarted).toHaveBeenCalledWith({
        action: "enrich",
        reportId: "report_with_gaps",
        parentReportId: "report_with_gaps",
        attemptId: "attempt-enrich",
        messageId: "msg-enrich",
      });
      confirm.mockRestore();
    });

    it("creates an immutable repair revision for a failed validation report", async () => {
      const onDeepReportTaskStarted = vi.fn();
      vi.spyOn(api, "repairDeepReport").mockResolvedValue({
        message_id: "msg-repair",
        attempt_id: "attempt-repair",
        parent_report_id: "report_failed",
        revision_mode: "repair",
      });
      render(<MessageBubble
        msg={makeMsg({
          type: "answer",
          reportId: "report_failed",
          reportQualityStatus: "failed_validation",
        })}
        onDeepReportTaskStarted={onDeepReportTaskStarted}
      />);

      await userEvent.setup().click(screen.getByRole("button", { name: "修复报告" }));

      expect(api.repairDeepReport).toHaveBeenCalledWith(
        "report_failed",
        expect.stringContaining("复用现有"),
      );
      expect(onDeepReportTaskStarted).toHaveBeenCalledWith({
        action: "repair",
        reportId: "report_failed",
        parentReportId: "report_failed",
        attemptId: "attempt-repair",
        messageId: "msg-repair",
      });
    });

    it("disables report actions while another Agent task is streaming", () => {
      render(<MessageBubble
        msg={makeMsg({ type: "answer", reportId: "report_busy" })}
        deepReportBusy
      />);

      expect(screen.getByRole("button", { name: "继续研究" })).toBeDisabled();
      expect(screen.getByRole("button", { name: "用新数据更新" })).toBeDisabled();
      expect(screen.getByRole("button", { name: "重写此章节" })).toBeDisabled();
    });

    it("turns the run report path into a right-side preview action", async () => {
      const onPreviewReport = vi.fn();
      const content = [
        "### 报告路径",
        "```",
        "agent/runs/20260717_003433_20_ea12c7/泰晶科技_603738_深度研究报告_20260717.md",
        "```",
      ].join("\n");
      render(<MessageBubble
        msg={makeMsg({
          content,
          runId: "20260717_003433_20_ea12c7",
          reportId: "report_e271467d36274f31",
          reportSecurityName: "泰晶科技",
        })}
        onPreviewReport={onPreviewReport}
      />);

      await userEvent.setup().click(screen.getByRole("button", {
        name: "在右侧预览报告 泰晶科技_603738_深度研究报告_20260717.md",
      }));

      expect(onPreviewReport).toHaveBeenCalledWith({
        runId: "20260717_003433_20_ea12c7",
        reportId: "report_e271467d36274f31",
        artifactId: "markdown",
        title: "泰晶科技穿透式深度研究",
      });
    });

    it("does not offer report preview for an ordinary run answer", () => {
      render(<MessageBubble
        msg={makeMsg({ content: "普通分析结果", runId: "plain_run" })}
        onPreviewReport={vi.fn()}
      />);

      expect(screen.queryByText("预览完整报告")).not.toBeInTheDocument();
    });

    it("explains deep-report actions and can archive the report to Obsidian", async () => {
      vi.spyOn(api, "archiveDeepReport").mockResolvedValue({
        status: "ok",
        path: "QQQ/Invest/report.md",
        bytes_written: 128,
      });
      render(<MessageBubble msg={makeMsg({
        type: "answer",
        content: "# 已校验的深度研究",
        reportId: "report_0123456789abcdef",
        reportQualityStatus: "passed",
        reportPdfAvailable: true,
      })} />);

      const sectionPicker = screen.getByLabelText("选择要重写的报告章节");
      expect(sectionPicker).toHaveValue("counter_thesis");
      expect(sectionPicker).toHaveAccessibleDescription(
        "选择“重写此章节”要作用的目标模块；当前选中“反方、风险与催化剂”。只选择，不会立即开始。",
      );
      expect(screen.getByRole("button", { name: "继续研究" })).toHaveAccessibleDescription(
        /不整份重跑/,
      );
      expect(screen.getByRole("button", { name: "用新数据更新" })).toHaveAccessibleDescription(
        /最新可验证数据/,
      );
      expect(screen.getByRole("button", { name: "重写此章节" })).toHaveAccessibleDescription(
        /仅重写左侧选中的章节/,
      );

      await userEvent.setup().click(screen.getByRole("button", { name: "保存到 Obsidian" }));
      expect(api.archiveDeepReport).toHaveBeenCalledWith("report_0123456789abcdef");
    });

    it("presents failed deep-report validation as diagnostics and hides PDF download", () => {
      const artifactUrl = vi.spyOn(api, "deepReportArtifactUrl");
      render(<MessageBubble msg={makeMsg({
        type: "answer",
        content: "报告校验诊断",
        reportId: "report_failed",
        reportQualityStatus: "failed_validation",
        reportSymbol: "603738.SH",
        reportDataAsOf: "2026-07-17",
        reportMissingModules: ["executive_summary", "financial_quality", "terminal_scenarios"],
        reportPdfAvailable: false,
      })} />);

      expect(screen.getByText("尚未形成正式报告")).toBeInTheDocument();
      expect(screen.getByRole("alert")).toHaveTextContent("单独修改章节无法解决");
      expect(screen.getByText("暂不提供 PDF")).toBeInTheDocument();
      const missingModules = screen.getByLabelText("仍需补充的研究内容");
      expect(missingModules).toHaveTextContent("核心结论");
      expect(missingModules).toHaveTextContent("三张报表与财务质量");
      expect(missingModules).toHaveTextContent("长期经营情景");
      expect(screen.queryByTitle("下载已校验的深度研究 PDF")).not.toBeInTheDocument();
      expect(screen.queryByTitle("Generate PDF")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "保存到 Obsidian" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "修复报告" })).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "用新数据更新" })).toBeInTheDocument();
      expect(artifactUrl).not.toHaveBeenCalledWith("report_failed", "pdf");
    });

    it("generates a PDF from the answer", async () => {
      const pdf = new Blob(["pdf"], { type: "application/pdf" });
      vi.spyOn(api, "generatePdf").mockResolvedValue(pdf);
      const createObjectURL = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:report");
      const revokeObjectURL = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
      let downloadedName = "";
      const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function () {
        downloadedName = this.download;
      });
      const content = [
        "数据已齐全，现在开始撰写完整分析。",
        "",
        "🔬 科创50ETF（588870.SH）单标持仓深度分析",
        "分析日期：2026-07-11（周六）",
        "数据截止：日线至 2026-07-11 收盘",
      ].join("\n");

      render(<MessageBubble msg={makeMsg({ type: "answer", content })} />);
      await userEvent.setup().click(screen.getByTitle("Generate PDF"));

      expect(api.generatePdf).toHaveBeenCalledWith(
        "2026-07-11_科创50ETF（588870.SH）单标持仓深度分析",
        content,
      );
      expect(downloadedName).toBe("2026-07-11_科创50ETF（588870.SH）单标持仓深度分析.pdf");
      expect(createObjectURL).toHaveBeenCalledWith(pdf);
      expect(click).toHaveBeenCalled();
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:report");
    });

    it("builds report titles from markdown headings and uses a safe filename", () => {
      expect(buildPdfReportTitle(
        "# 半导体：周度复盘\n\n> **报告日期**: 2026-07-10",
        new Date(2024, 0, 1),
      )).toBe("2026-07-10_半导体：周度复盘");
      expect(sanitizePdfFilename("2026-07-10_半导体：周度复盘/观察?"))
        .toBe("2026-07-10_半导体：周度复盘_观察_");
    });

    it("can fork from a persisted answer message", async () => {
      const onFork = vi.fn();
      const msg = makeMsg({ type: "answer", sourceMessageId: "server-msg-2" });
      render(<MessageBubble msg={msg} onFork={onFork} />);

      await userEvent.setup().click(screen.getByTitle("Fork conversation from here"));

      expect(onFork).toHaveBeenCalledWith(msg);
    });

    it("generates a PDF without the post-disclaimer follow-up", async () => {
      vi.spyOn(api, "generatePdf").mockResolvedValue(new Blob(["pdf"], { type: "application/pdf" }));
      vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:report");
      vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
      vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

      render(<MessageBubble msg={makeMsg({
        type: "answer",
        content: "结论\n免责声明：不构成投资建议。\n如有需要，我可以继续分析。",
      })} />);
      await userEvent.setup().click(screen.getByTitle("Generate PDF"));

      expect(api.generatePdf).toHaveBeenCalledWith(
        expect.stringMatching(/^\d{4}-\d{2}-\d{2}_Vibe-Trading Research Report$/),
        "结论\n免责声明：不构成投资建议。",
      );
    });
  });

  describe("error messages", () => {
    it("renders error content with danger styling", () => {
      render(<MessageBubble msg={makeMsg({ type: "error", content: "Execution failed" })} />);
      expect(screen.getByText("Execution failed")).toBeInTheDocument();
    });

    it("shows retry button when onRetry is provided", () => {
      const onRetry = vi.fn();
      render(<MessageBubble msg={makeMsg({ type: "error", content: "Something broke" })} onRetry={onRetry} />);
      expect(screen.getByRole("button")).toBeInTheDocument();
    });

    it("calls onRetry when retry button is clicked", async () => {
      const onRetry = vi.fn();
      const msg = makeMsg({ type: "error", content: "Something broke" });
      render(<MessageBubble msg={msg} onRetry={onRetry} />);

      const user = userEvent.setup();
      await user.click(screen.getByRole("button"));
      expect(onRetry).toHaveBeenCalledWith(msg);
    });

    it("shows timeout hint for timeout errors", () => {
      render(
        <MessageBubble
          msg={makeMsg({ type: "error", content: "Execution timed out after 600s" })}
          onRetry={vi.fn()}
        />,
      );
      expect(screen.getByText(/Try simplifying the strategy/)).toBeInTheDocument();
    });
  });

  describe("run_complete messages", () => {
    it("renders RunCompleteCard when runId is present", () => {
      render(<MessageBubble msg={makeMsg({ type: "run_complete", runId: "run-42" })} />);
      expect(screen.getByTestId("run-complete-card")).toBeInTheDocument();
      expect(screen.getByText("Run: run-42")).toBeInTheDocument();
    });
  });

  describe("fallback", () => {
    it("renders content for unknown message types", () => {
      render(<MessageBubble msg={makeMsg({ type: "thinking", content: "analyzing data..." })} />);
      expect(screen.getByText("analyzing data...")).toBeInTheDocument();
    });

    it("renders null for empty content on unknown types", () => {
      const { container } = render(<MessageBubble msg={makeMsg({ type: "thinking", content: "" })} />);
      expect(container.innerHTML).toBe("");
    });
  });
});
