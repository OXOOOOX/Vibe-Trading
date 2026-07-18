import { render, screen } from "@testing-library/react";
import { ToolProgressIndicator } from "../ToolProgressIndicator";
import type { ToolCallEntry } from "@/types/agent";

function makeTc(overrides: Partial<ToolCallEntry> = {}): ToolCallEntry {
  return {
    id: "tc-1",
    tool: "run_backtest",
    arguments: {},
    status: "running",
    timestamp: Date.now(),
    ...overrides,
  };
}

describe("ToolProgressIndicator", () => {
  it("keeps completed calls visible until the active turn ends", () => {
    const tcs = [makeTc({ status: "ok" }), makeTc({ id: "tc-2", status: "error" })];
    render(<ToolProgressIndicator toolCalls={tcs} />);
    expect(screen.getByText("2 tool calls")).toBeInTheDocument();
    expect(screen.getByText("Tool 1")).toBeInTheDocument();
    expect(screen.getByText("Tool 2")).toBeInTheDocument();
  });

  it("renders nothing for empty array", () => {
    const { container } = render(<ToolProgressIndicator toolCalls={[]} />);
    expect(container.innerHTML).toBe("");
  });

  it("renders single running tool", () => {
    const tcs = [makeTc({ elapsed_s: 5 })];
    render(<ToolProgressIndicator toolCalls={tcs} />);
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.getByText("Tool 1")).toBeInTheDocument();
    expect(screen.getByText(/运行策略回测/)).toBeInTheDocument();
    expect(screen.getByText("5s")).toBeInTheDocument();
  });

  it("renders multiple running tools with header", () => {
    const tcs = [
      makeTc({ id: "tc-1", tool: "bash" }),
      makeTc({ id: "tc-2", tool: "write_file" }),
    ];
    render(<ToolProgressIndicator toolCalls={tcs} />);
    expect(screen.getByText("2 tools running")).toBeInTheDocument();
    expect(screen.getByText(/执行数据处理/)).toBeInTheDocument();
    expect(screen.getByText(/生成文件/)).toBeInTheDocument();
  });

  it("shows every running tool without an overflow fold", () => {
    const tcs = [
      makeTc({ id: "tc-1", tool: "bash" }),
      makeTc({ id: "tc-2", tool: "write_file" }),
      makeTc({ id: "tc-3", tool: "run_backtest" }),
      makeTc({ id: "tc-4", tool: "read_file" }),
    ];
    render(<ToolProgressIndicator toolCalls={tcs} />);
    expect(screen.getByText("Tool 1")).toBeInTheDocument();
    expect(screen.getByText("Tool 2")).toBeInTheDocument();
    expect(screen.getByText("Tool 3")).toBeInTheDocument();
    expect(screen.getByText("Tool 4")).toBeInTheDocument();
    expect(screen.queryByText(/more/)).not.toBeInTheDocument();
  });

  it("shows determinate progress bar when progress data exists", () => {
    const tcs = [
      makeTc({
        progress: { current: 5, total: 10, stage: "financial_data", message: "Reading balance sheet" },
      }),
    ];
    render(<ToolProgressIndicator toolCalls={tcs} />);
    expect(screen.getByText("Phase: 处理中")).toBeInTheDocument();
    expect(screen.getByText("5/10")).toBeInTheDocument();
    expect(screen.getByText("正在处理当前研究步骤")).toHaveClass("line-clamp-2");
    // Should have a progressbar element
    expect(screen.getByRole("progressbar")).toBeInTheDocument();
  });

  it("treats a new phase as a fresh progress sequence", () => {
    const { rerender } = render(
      <ToolProgressIndicator
        toolCalls={[makeTc({ elapsed_s: 10, progress: { current: 5, total: 10, stage: "download" } })]}
      />,
    );
    rerender(
      <ToolProgressIndicator
        toolCalls={[makeTc({ elapsed_s: 10, progress: { current: 6, total: 10, stage: "download" } })]}
      />,
    );
    expect(screen.getByText("~7s left")).toBeInTheDocument();

    rerender(
      <ToolProgressIndicator
        toolCalls={[makeTc({ elapsed_s: 12, progress: { current: 1, total: 4, stage: "parse" } })]}
      />,
    );
    expect(screen.queryByText(/left$/)).not.toBeInTheDocument();

    rerender(
      <ToolProgressIndicator
        toolCalls={[makeTc({ elapsed_s: 12, progress: { current: 3, total: 4, stage: "parse" } })]}
      />,
    );
    expect(screen.getByText("~4s left")).toBeInTheDocument();
  });
});
