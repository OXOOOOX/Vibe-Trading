import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18n from "@/i18n";
import { ResearchGoalMenuItem, SwarmTeamMenuItem } from "../ResearchModeMenuItem";

describe("research mode menu explanations", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("zh-CN");
  });

  afterEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("explains the persistent, evidence-ledger behavior of a research goal", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<ResearchGoalMenuItem onSelect={onSelect} />);

    const entry = screen.getByRole("button", { name: "新建研究目标" });
    const hint = screen.getByRole("tooltip");
    expect(entry).toHaveAttribute("aria-describedby", hint.id);
    expect(hint).toHaveClass("invisible", "group-hover:visible", "group-focus-within:visible");

    await user.hover(entry);
    expect(screen.getByText("什么是研究目标？")).toBeInTheDocument();
    expect(screen.getByText(/创建达成标准并立即启动首轮研究/)).toBeInTheDocument();
    expect(screen.getByText(/完成前必须根据证据台账做审计/)).toBeInTheDocument();

    await user.click(entry);
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("explains preset selection, dependency-aware roles, and the swarm cost tradeoff", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<SwarmTeamMenuItem onSelect={onSelect} />);

    const entry = screen.getByRole("button", { name: "运行群体团队" });
    await user.hover(entry);
    expect(screen.getByText("什么是群体团队？")).toBeInTheDocument();
    expect(screen.getByText(/30 套预设团队/)).toBeInTheDocument();
    expect(screen.getByText(/耗时与模型调用通常高于普通对话/)).toBeInTheDocument();

    await user.click(entry);
    expect(onSelect).toHaveBeenCalledTimes(1);
  });
});
