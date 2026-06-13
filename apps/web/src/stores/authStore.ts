import { create } from "zustand";

import { fetchMe, loginRequest, logoutRequest, registerRequest } from "@/lib/api";
import { hasPermission } from "@/lib/auth-permissions";
import { getAccessToken, setAccessToken } from "@/lib/access-token";
import type { LoginRequest, Permission, RegisterRequest, UserResponse } from "@/types/api";

const WS_KEY = "egpt_workspace_id";

type AuthState = {
  user: UserResponse | null;
  isBootstrapping: boolean;
  workspaceId: string | null;
  setWorkspaceId: (id: string | null) => void;
  ensureDefaultWorkspace: (u: UserResponse) => void;
  login: (body: LoginRequest) => Promise<void>;
  register: (body: RegisterRequest) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
  hydrateUser: () => Promise<void>;
  hasPermission: (p: Permission) => boolean;
};

function readStoredWorkspace(): string | null {
  if (typeof window === "undefined") return null;
  return sessionStorage.getItem(WS_KEY);
}

function persistWorkspace(id: string | null) {
  if (typeof window === "undefined") return;
  if (id) sessionStorage.setItem(WS_KEY, id);
  else sessionStorage.removeItem(WS_KEY);
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  isBootstrapping: false,
  workspaceId: null,

  setWorkspaceId(id) {
    persistWorkspace(id);
    set({ workspaceId: id });
  },

  ensureDefaultWorkspace(u) {
    const stored = readStoredWorkspace();
    const memberships = u.workspaces ?? [];
    const valid =
      stored && memberships.some((m) => m.workspace_id === stored) ?
        stored
      : memberships[0]?.workspace_id ?? null;
    persistWorkspace(valid);
    set({ workspaceId: valid });
  },

  async login(body) {
    const res = await loginRequest(body);
    set({ user: res.user });
    get().ensureDefaultWorkspace(res.user);
  },

  async register(body) {
    const res = await registerRequest(body);
    set({ user: res.user });
    get().ensureDefaultWorkspace(res.user);
  },

  async logout() {
    await logoutRequest();
    set({ user: null, workspaceId: null });
    persistWorkspace(null);
  },

  async refresh() {
    const { refreshAccessToken, fetchMe: fetchMeAgain } = await import("@/lib/api");
    await refreshAccessToken();
    const user = await fetchMeAgain();
    set({ user });
    get().ensureDefaultWorkspace(user);
  },

  async hydrateUser() {
    if (get().user) return;
    if (!getAccessToken()) return;
    set({ isBootstrapping: true });
    try {
      const user = await fetchMe();
      set({ user });
      get().ensureDefaultWorkspace(user);
    } catch {
      setAccessToken(null);
      set({ user: null, workspaceId: null });
    } finally {
      set({ isBootstrapping: false });
    }
  },

  hasPermission(p) {
    const u = get().user;
    if (!u) return false;
    return hasPermission(u.role, p);
  },
}));
