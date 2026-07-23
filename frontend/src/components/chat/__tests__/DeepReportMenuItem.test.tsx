import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DeepReportMenuItem } from "../DeepReportMenuItem";

describe("DeepReportMenuItem", () => {
  it("explains the penetrative report on hover without replacing original research", async () => {
    const user = userEvent.setup();
    render(<DeepReportMenuItem onSelect={vi.fn()} />);

    const entry = screen.getByRole("button", { name: "股票 / ETF 穿透式深度研究" });
    const hint = screen.getByRole("tooltip");
    expect(entry).toHaveAttribute("aria-describedby", hint.id);
    expect(hint).toHaveClass("invisible", "group-hover:visible", "group-focus-within:visible");
    expect(hint).toHaveClass("bg-background", "backdrop-blur-xl", "shadow-2xl");

    await user.hover(entry);
    expect(screen.getByText("什么是穿透式深度研究？")).toBeInTheDocument();
    expect(screen.getByText(/没有替代原有深度研究/)).toBeInTheDocument();
    expect(screen.getByText(/“新建研究目标”仍用于开放式、多轮研究/)).toBeInTheDocument();
  });

  it("keeps selection behavior intact", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<DeepReportMenuItem onSelect={onSelect} />);

    await user.click(screen.getByRole("button", { name: "股票 / ETF 穿透式深度研究" }));

    expect(onSelect).toHaveBeenCalledTimes(1);
  });
});
