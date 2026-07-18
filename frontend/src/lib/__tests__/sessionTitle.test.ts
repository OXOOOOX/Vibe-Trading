import { describe, expect, it } from "vitest";

import { initialSessionTitle } from "../sessionTitle";

describe("initialSessionTitle", () => {
  it("uses the confirmed equity identity for a new deep-research conversation", () => {
    expect(initialSessionTitle("研究泰晶科技", {
      securityName: "泰晶科技",
      symbol: "603738.SH",
    })).toBe("泰晶科技（603738.SH）穿透式深度研究");
  });

  it("keeps the existing prompt-based title for ordinary conversations", () => {
    const prompt = "a".repeat(60);

    expect(initialSessionTitle(prompt)).toBe("a".repeat(50));
  });
});
