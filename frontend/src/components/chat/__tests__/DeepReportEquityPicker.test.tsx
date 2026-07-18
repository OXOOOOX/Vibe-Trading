import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DeepReportEquityPicker } from "../DeepReportEquityPicker";

const candidates = [
  { symbol: "603738.SH", security_name: "泰晶科技", market: "cn", source: "tencent" },
  { symbol: "300738.SZ", security_name: "奥飞数据", market: "cn", source: "tencent" },
];

describe("DeepReportEquityPicker", () => {
  it("renders fuzzy matches and confirms the selected symbol", async () => {
    const onConfirm = vi.fn();
    const user = userEvent.setup();
    render(<DeepReportEquityPicker candidates={candidates} onConfirm={onConfirm} />);

    expect(screen.getByText("泰晶科技")).toBeInTheDocument();
    expect(screen.getByText("603738.SH")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "确认并研究 泰晶科技（603738.SH）" }));

    expect(onConfirm).toHaveBeenCalledWith(candidates[0]);
  });

  it("prevents confirmation while another request is streaming", () => {
    render(<DeepReportEquityPicker candidates={candidates} disabled onConfirm={vi.fn()} />);

    expect(screen.getByRole("button", { name: "确认并研究 泰晶科技（603738.SH）" })).toBeDisabled();
  });
});
