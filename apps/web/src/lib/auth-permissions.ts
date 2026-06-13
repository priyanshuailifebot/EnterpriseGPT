import type { Permission, UserRole } from "@/types/api";

/** Mirrors apps/api/core/permissions.py ROLE_PERMISSIONS. */

const SUPER: Permission[] = [
  "workflow:create",
  "workflow:run",
  "workflow:read",
  "workflow:delete",
  "document:upload",
  "document:read",
  "user:manage",
  "workspace:manage",
  "analytics:read",
  "mcp:manage",
];

const ADMIN: Permission[] = [
  "workflow:create",
  "workflow:run",
  "workflow:read",
  "workflow:delete",
  "document:upload",
  "document:read",
  "user:manage",
  "workspace:manage",
  "analytics:read",
  "mcp:manage",
];

const BUILDER: Permission[] = [
  "workflow:create",
  "workflow:run",
  "workflow:read",
  "document:upload",
  "document:read",
];

const OPERATOR: Permission[] = [
  "workflow:run",
  "workflow:read",
  "document:read",
];

const VIEWER: Permission[] = ["workflow:read", "document:read"];

const BY_ROLE: Record<UserRole, Set<Permission>> = {
  super_admin: new Set(SUPER),
  admin: new Set(ADMIN),
  builder: new Set(BUILDER),
  operator: new Set(OPERATOR),
  viewer: new Set(VIEWER),
};

export function hasPermission(role: UserRole, permission: Permission): boolean {
  return BY_ROLE[role]?.has(permission) ?? false;
}
