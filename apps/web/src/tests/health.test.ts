import { describe, expect, it } from "vitest";

import { cn } from "@/lib/utils";

describe("scaffold sanity", () => {
  it("merges class names with cn()", () => {
    expect(cn("text-sm", "text-base")).toBe("text-base");
  });

  it("filters out falsy values", () => {
    expect(cn("p-2", false && "p-4", null, undefined, "rounded")).toBe(
      "p-2 rounded",
    );
  });
});
