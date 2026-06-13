import { describe, expect, it } from "vitest";

import { hasPermission } from "@/lib/auth-permissions";

describe("ROLE_PERMISSIONS (mirrors backend)", () => {
  it("allows builder to create workflows", () => {
    expect(hasPermission("builder", "workflow:create")).toBe(true);
  });

  it("denies viewer run permission", () => {
    expect(hasPermission("viewer", "workflow:run")).toBe(false);
  });

  it("allows admin analytics read", () => {
    expect(hasPermission("admin", "analytics:read")).toBe(true);
  });
});
