import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { WelcomeScreen } from "../WelcomeScreen";
import i18n from "@/i18n";

describe("WelcomeScreen", () => {
  const onExample = vi.fn();
  const onDraft = vi.fn();
  const onModeSelect = vi.fn();

  beforeEach(() => {
    onExample.mockClear();
    onDraft.mockClear();
    onModeSelect.mockClear();
  });

  it("renders the title", () => {
    render(<WelcomeScreen onExample={onExample} />);
    expect(screen.getByText("Vibe-Trading")).toBeInTheDocument();
  });

  it("renders capability chips", () => {
    render(<WelcomeScreen onExample={onExample} />);
    expect(screen.getByText("Finance Skills Library")).toBeInTheDocument();
    expect(screen.getByText("Swarm Agent Teams")).toBeInTheDocument();
    expect(screen.getByText("Shadow Account Backtest")).toBeInTheDocument();
  });

  it("renders example categories", () => {
    const view = render(<WelcomeScreen onExample={onExample} />);
    expect(screen.getByText("Multi-Market Backtest")).toBeInTheDocument();
    expect(screen.getByText("Research & Analysis")).toBeInTheDocument();
    expect(screen.getByText("Swarm Teams")).toBeInTheDocument();
    expect(view.container.querySelectorAll("[data-example-action]")).toHaveLength(17);
  });

  it("adds detailed hover and focus hints to the example actions", () => {
    render(<WelcomeScreen onExample={onExample} />);

    expect(screen.getByRole("button", { name: /Cross-Market Portfolio/ })).toHaveAttribute("aria-describedby");
    expect(screen.getAllByText("Lock securities, date range, frequency, and rebalancing rules to keep the setup consistent")).toHaveLength(3);
    expect(screen.getAllByText("Backtests study historical scenarios; they do not guarantee future returns or connect to live order placement.")).toHaveLength(3);
  });

  it("renders portfolio-focused quick actions with detailed hints", () => {
    render(<WelcomeScreen onExample={onExample} onDraft={onDraft} onModeSelect={onModeSelect} />);

    expect(screen.getByRole("button", { name: /Portfolio health check/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Quick stock analysis/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Equity deep research/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Formal weekly review/ })).toBeInTheDocument();
    expect(screen.getByText("Use the portfolio ledger as truth and refresh decision-critical quotes first")).toBeInTheDocument();
  });

  it("starts a portfolio analysis from its quick action", async () => {
    render(<WelcomeScreen onExample={onExample} onDraft={onDraft} onModeSelect={onModeSelect} />);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /Portfolio health check/ }));

    expect(onExample).toHaveBeenCalledWith(expect.stringContaining("portfolio_state"));
  });

  it("puts the stock analysis template into the composer", async () => {
    render(<WelcomeScreen onExample={onExample} onDraft={onDraft} onModeSelect={onModeSelect} />);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /Quick stock analysis/ }));

    expect(onDraft).toHaveBeenCalledWith(expect.stringContaining("[company name or ticker]"));
    expect(onExample).not.toHaveBeenCalled();
  });

  it("opens the existing deep-report mode from the quick action", async () => {
    render(<WelcomeScreen onExample={onExample} onDraft={onDraft} onModeSelect={onModeSelect} />);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /Equity deep research/ }));

    expect(onModeSelect).toHaveBeenCalledWith("deepReport");
  });

  it("puts a formal user-weekly request into the composer", async () => {
    render(<WelcomeScreen onExample={onExample} onDraft={onDraft} onModeSelect={onModeSelect} />);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /Formal weekly review/ }));

    expect(onDraft).toHaveBeenCalledWith(expect.stringContaining("weekly_report"));
    expect(onDraft).toHaveBeenCalledWith(expect.stringContaining("formal user-facing weekly report"));
    expect(onExample).not.toHaveBeenCalled();
  });

  it("calls onExample with prompt when an example button is clicked", async () => {
    render(<WelcomeScreen onExample={onExample} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /Cross-Market Portfolio/ }));
    expect(onExample).toHaveBeenCalledTimes(1);
    expect(onExample).toHaveBeenCalledWith(
      expect.stringContaining("risk-parity portfolio"),
    );
  });

  it("renders the helper text", () => {
    render(<WelcomeScreen onExample={onExample} />);
    expect(screen.getByText("Describe a trading strategy to get started.")).toBeInTheDocument();
    expect(screen.getByText("Try an example:")).toBeInTheDocument();
  });

  it("renders translated welcome content in Chinese", async () => {
    await i18n.changeLanguage("zh-CN");
    render(<WelcomeScreen onExample={onExample} />);

    expect(screen.getByText("金融技能库")).toBeInTheDocument();
    expect(screen.getByText("多市场回测")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /投资委员会评审/ })).toBeInTheDocument();
    expect(screen.getAllByText("按任务选择研究、风险、量化和投资经理等专业角色")).toHaveLength(2);
    expect(screen.getByText("描述一个交易策略即可开始。")).toBeInTheDocument();

    await i18n.changeLanguage("en");
  });
});
