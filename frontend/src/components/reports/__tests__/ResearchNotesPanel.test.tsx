import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ResearchNotesPanel } from "@/components/reports/ResearchNotesPanel";

const apiMock = vi.hoisted(() => ({
  getReportLibraryResearchNotes: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api: apiMock }));

describe("ResearchNotesPanel", () => {
  beforeEach(() => {
    apiMock.getReportLibraryResearchNotes.mockReset();
    apiMock.getReportLibraryResearchNotes.mockResolvedValue({
      subject_key: "588870.SH",
      notes: [{
        note_claim_id: "note-1",
        subject_key: "588870.SH",
        session_id: "session-1",
        message_id: "message-1",
        role: "assistant",
        text: "等待指数估值回落后，再复核仓位建议。",
        claim_status: "active",
        created_at: "2026-07-19T10:00:00+08:00",
        derived_status: "unverified",
        resolutions: [],
      }],
      counts: { unverified: 1, confirmed: 5, contradicted: 0, superseded: 0 },
      total_count: 6,
      next_cursor: null,
    });
  });

  it("stays collapsed without a request, then loads and filters independently", async () => {
    render(<ResearchNotesPanel subjectKey="588870.SH" totalHint={6} confirmedHint={5} />);

    const toggle = screen.getByRole("button", { name: /研究笔记 · 6 条 · 5 条已被正式报告确认/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(apiMock.getReportLibraryResearchNotes).not.toHaveBeenCalled();
    expect(screen.queryByText("研究会话")).not.toBeInTheDocument();

    fireEvent.click(toggle);
    expect(await screen.findByText("等待指数估值回落后，再复核仓位建议。")).toBeInTheDocument();
    expect(apiMock.getReportLibraryResearchNotes).toHaveBeenCalledWith("588870.SH", {
      status: undefined,
      limit: 10,
      cursor: undefined,
    });

    fireEvent.click(screen.getByRole("button", { name: /已确认 5/ }));
    await waitFor(() => {
      expect(apiMock.getReportLibraryResearchNotes).toHaveBeenLastCalledWith("588870.SH", {
        status: "confirmed",
        limit: 10,
        cursor: undefined,
      });
    });
  });
});
