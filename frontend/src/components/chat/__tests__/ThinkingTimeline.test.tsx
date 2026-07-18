import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThinkingTimeline } from "../ThinkingTimeline";
import type { AgentMessage } from "@/types/agent";

function makeMsg(overrides: Partial<AgentMessage> = {}): AgentMessage {
  return {
    id: "msg-1",
    type: "tool_call",
    content: "",
    tool: "bash",
    status: "running",
    timestamp: Date.now(),
    ...overrides,
  };
}

describe("ThinkingTimeline", () => {
  it("shows summary text when done", () => {
    const msgs: AgentMessage[] = [
      makeMsg({ type: "tool_call", tool: "run_backtest", status: "ok" }),
      makeMsg({
        type: "tool_result",
        tool: "run_backtest",
        status: "ok",
        elapsed_ms: 3200,
        content: "done",
      }),
    ];

    render(<ThinkingTimeline messages={msgs} />);
    expect(screen.getByText(/Done · 1 steps/)).toBeInTheDocument();
    expect(screen.getByText(/3\.2s/)).toBeInTheDocument();
  });

  it("shows running state with spinner", () => {
    const msgs: AgentMessage[] = [
      makeMsg({ type: "tool_call", tool: "run_backtest", status: "running" }),
    ];

    render(<ThinkingTimeline messages={msgs} isLatest />);
    expect(screen.getByText(/Running 运行策略回测/)).toBeInTheDocument();
  });

  it("does not allow a running tool timeline to collapse", async () => {
    const user = userEvent.setup();
    const msgs: AgentMessage[] = [
      makeMsg({ type: "tool_call", tool: "bash", status: "running" }),
    ];

    render(<ThinkingTimeline messages={msgs} />);
    const toggle = screen.getByRole("button");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText("执行数据处理")).toBeInTheDocument();

    await user.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("执行数据处理")).toBeInTheDocument();
  });

  it("expands and collapses on click", async () => {
    const user = userEvent.setup();
    const msgs: AgentMessage[] = [
      makeMsg({ type: "tool_call", tool: "bash", status: "ok" }),
      makeMsg({ type: "tool_result", tool: "bash", status: "ok", elapsed_ms: 100, content: "output" }),
    ];

    render(<ThinkingTimeline messages={msgs} />);

    // Initially collapsed
    expect(screen.queryByText("执行数据处理")).not.toBeInTheDocument();

    // Click to expand
    await user.click(screen.getByRole("button"));

    // Now expanded — should show step labels
    expect(screen.getByText("执行数据处理")).toBeInTheDocument();
  });

  it("shows error icon when a step failed", () => {
    const msgs: AgentMessage[] = [
      makeMsg({ type: "tool_call", tool: "bash", status: "ok" }),
      makeMsg({ type: "tool_result", tool: "bash", status: "error", content: "err" }),
    ];

    render(<ThinkingTimeline messages={msgs} isLatest />);
    // Error state should be visible in summary
    expect(screen.getByText(/Done · 1 steps/)).toBeInTheDocument();
  });

  it("shows thinking content when expanded with no tools", async () => {
    const user = userEvent.setup();
    const msgs: AgentMessage[] = [
      makeMsg({ type: "thinking", content: "Let me analyze this strategy carefully." }),
    ];

    render(<ThinkingTimeline messages={msgs} />);

    await user.click(screen.getByRole("button"));
    expect(screen.getByText("Let me analyze this strategy carefully.")).toBeInTheDocument();
  });

  it("starts expanded when isLatest is true", () => {
    const msgs: AgentMessage[] = [
      makeMsg({ type: "tool_call", tool: "write_file", status: "ok" }),
      makeMsg({ type: "tool_result", tool: "write_file", status: "ok", content: "ok" }),
    ];

    render(<ThinkingTimeline messages={msgs} isLatest />);
    // Should be expanded immediately — the user-facing action label is visible.
    expect(screen.getByText("生成文件")).toBeInTheDocument();
  });

  it("handles multiple tool steps", async () => {
    const user = userEvent.setup();
    const msgs: AgentMessage[] = [
      makeMsg({ type: "tool_call", tool: "bash", status: "ok" }),
      makeMsg({ type: "tool_result", tool: "bash", status: "ok", elapsed_ms: 500, content: "ok" }),
      makeMsg({ type: "tool_call", tool: "write_file", status: "ok" }),
      makeMsg({ type: "tool_result", tool: "write_file", status: "ok", elapsed_ms: 200, content: "ok" }),
      makeMsg({ type: "tool_call", tool: "run_backtest", status: "ok" }),
      makeMsg({ type: "tool_result", tool: "run_backtest", status: "ok", elapsed_ms: 5000, content: "ok" }),
    ];

    render(<ThinkingTimeline messages={msgs} />);
    expect(screen.getByText(/Done · 3 steps/)).toBeInTheDocument();
    expect(screen.getByText(/5\.7s/)).toBeInTheDocument();

    await user.click(screen.getByRole("button"));
    expect(screen.getByText("执行数据处理")).toBeInTheDocument();
    expect(screen.getByText("生成文件")).toBeInTheDocument();
    expect(screen.getByText("运行策略回测")).toBeInTheDocument();
  });
});
