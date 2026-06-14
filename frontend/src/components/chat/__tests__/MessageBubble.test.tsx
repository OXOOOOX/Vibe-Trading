import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MessageBubble } from "../MessageBubble";
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
      const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

      render(<MessageBubble msg={makeMsg({ type: "answer", content: "Report content" })} />);
      await userEvent.setup().click(screen.getByTitle("生成 PDF"));

      expect(api.generatePdf).toHaveBeenCalledWith("Vibe-Trading Research Report", "Report content");
      expect(createObjectURL).toHaveBeenCalledWith(pdf);
      expect(click).toHaveBeenCalled();
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:report");
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
      await userEvent.setup().click(screen.getByTitle("生成 PDF"));

      expect(api.generatePdf).toHaveBeenCalledWith(
        "Vibe-Trading Research Report",
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
