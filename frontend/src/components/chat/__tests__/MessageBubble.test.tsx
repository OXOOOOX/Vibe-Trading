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
