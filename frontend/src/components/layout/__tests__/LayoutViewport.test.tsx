import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { Layout } from "../Layout";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: {
      resolvedLanguage: "zh-CN",
      language: "zh-CN",
      changeLanguage: vi.fn(),
    },
  }),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/lib/api", () => ({
  api: {
    listSessions: vi.fn().mockResolvedValue([]),
    deleteSession: vi.fn(),
    renameSession: vi.fn(),
    continueSessionOnFeishu: vi.fn(),
  },
}));

vi.mock("@/hooks/useDarkMode", () => ({
  useDarkMode: () => ({ dark: true, toggle: vi.fn() }),
}));

vi.mock("@/stores/agent", () => ({
  useAgentStore: (selector: (state: Record<string, unknown>) => unknown) => selector({
    sseStatus: "connected",
    sseRetryAttempt: 0,
    streamingSessionId: null,
  }),
}));

vi.mock("@/components/layout/ConnectionBanner", () => ({
  ConnectionBanner: () => <div data-testid="connection-banner" />,
}));

vi.mock("@/components/portfolio/PortfolioMonitorEffectsProvider", () => ({
  PortfolioMonitorEffectsProvider: ({ children }: { children: ReactNode }) => children,
}));

describe("Layout viewport containment", () => {
  it("keeps the document fixed to the viewport and delegates scrolling to main", () => {
    const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
    expect(html).toMatch(/<html[^>]+class="h-full overflow-hidden"/);
    expect(html).toMatch(/<body class="h-full overflow-hidden/);
    expect(html).toMatch(/<div id="root" class="h-full overflow-hidden"><\/div>/);

    const { container } = render(
      <MemoryRouter initialEntries={["/portfolio"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="portfolio" element={<div>portfolio content</div>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    const sidebar = container.querySelector("aside");
    const shell = sidebar?.parentElement;
    const main = container.querySelector("main");

    expect(shell).toHaveClass("h-full", "min-h-0", "overflow-hidden");
    expect(main?.parentElement).toHaveClass("min-h-0", "overflow-hidden");
    expect(main).toHaveClass("min-h-0", "overflow-auto", "overscroll-contain");
  });
});
