// @vitest-environment node

import { describe, expect, it } from "vitest";

import viteConfig from "../../vite.config";

describe("Vite API proxy", () => {
  it("forwards report-library requests in dev and preview", () => {
    expect(typeof viteConfig).toBe("function");

    const resolved = viteConfig({
      command: "serve",
      mode: "test",
      isSsrBuild: false,
      isPreview: false,
    });

    expect(resolved.server?.proxy).toHaveProperty("/report-library");
    expect(resolved.preview?.proxy).toHaveProperty("/report-library");
  });

  it("forwards Codex CLI settings requests instead of returning the SPA shell", () => {
    expect(typeof viteConfig).toBe("function");

    const resolved = viteConfig({
      command: "serve",
      mode: "test",
      isSsrBuild: false,
      isPreview: false,
    });

    expect(resolved.server?.proxy).toHaveProperty("/settings/codex-cli");
    expect(resolved.preview?.proxy).toHaveProperty("/settings/codex-cli");
  });

  it("forwards formal weekly-report requests instead of returning the SPA shell", () => {
    expect(typeof viteConfig).toBe("function");

    const resolved = viteConfig({
      command: "serve",
      mode: "test",
      isSsrBuild: false,
      isPreview: false,
    });

    expect(resolved.server?.proxy).toHaveProperty("/portfolio/weekly-runs");
    expect(resolved.preview?.proxy).toHaveProperty("/portfolio/weekly-runs");
  });
});
