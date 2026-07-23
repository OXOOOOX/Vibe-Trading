import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ReportMarkdownContent } from "@/components/reports/ReportMarkdownContent";


describe("ReportMarkdownContent", () => {
  it("renders academic footnotes as unobtrusive superscript links", () => {
    render(
      <ReportMarkdownContent
        content={"正文结论[^1]\n\n[^1]: [公开资料](https://example.test/source)。"}
      />,
    );

    const citation = screen.getByRole("link", { name: "1" });
    expect(citation.closest("sup")).not.toBeNull();
    expect(screen.getByRole("link", { name: "公开资料" })).toHaveAttribute(
      "href",
      "https://example.test/source",
    );
  });
});
