import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { api, type PortfolioReconciliation } from "@/lib/api";
import PortfolioReconciliationPanel from "../PortfolioReconciliationPanel";


const preview: PortfolioReconciliation = {
  reconciliation_id: "recon-1",
  status: "preview",
  base_revision: 7,
  request: { raw_text: "科创50ETF汇添富 588870 13700 2.100" },
  created_at: "2026-07-22T00:00:00Z",
  preview: {
    base_revision: 7,
    holding_diffs: [{
      symbol: "588870.SH",
      status: "changed",
      changes: {
        quantity: { current: 10500, broker: 13700 },
        cost_price: { current: 1.938, broker: 2.1 },
      },
    }],
    missing_ledger_event_ids: [],
    extra_ledger_event_ids: [],
    suspicious_events: [{ event_id: "553a08a-cancelled" }],
    broker_reported_pnl: 271.64,
    computed_realized_pnl: null,
    unexplained_pnl: null,
    pnl_status: "broker_reported",
    requires_explicit_commit: true,
    target_state: { holdings: [], recent_trades: [] },
  },
};


describe("PortfolioReconciliationPanel", () => {
  afterEach(() => vi.restoreAllMocks());

  it("previews first and requires explicit confirmation before commit", async () => {
    const user = userEvent.setup();
    const previewSpy = vi.spyOn(api, "previewPortfolioReconciliation").mockResolvedValue(preview);
    const commitSpy = vi.spyOn(api, "commitPortfolioReconciliation").mockResolvedValue({
      ...preview,
      status: "committed",
      committed_at: "2026-07-22T00:01:00Z",
      state: { holdings: [], recent_trades: [], revision: 8 },
      deduplicated: false,
    });
    const onCommitted = vi.fn();
    render(<PortfolioReconciliationPanel currentRevision={7} onCommitted={onCommitted} />);

    await user.type(
      screen.getByLabelText("券商持仓表"),
      "科创50ETF汇添富 588870 13700 2.100",
    );
    await user.click(screen.getByRole("button", { name: "生成差异预览" }));

    await screen.findByText("588870.SH");
    expect(previewSpy).toHaveBeenCalledWith(expect.objectContaining({
      raw_text: "科创50ETF汇添富 588870 13700 2.100",
    }));
    expect(screen.getByText(/取消尝试候选/)).toBeInTheDocument();
    const commitButton = screen.getByRole("button", { name: "确认并提交对账" });
    expect(commitButton).toBeDisabled();

    await user.click(screen.getByRole("checkbox"));
    await user.click(commitButton);
    await waitFor(() => expect(commitSpy).toHaveBeenCalledWith("recon-1", 7));
    expect(onCommitted).toHaveBeenCalledTimes(1);
  });
});
