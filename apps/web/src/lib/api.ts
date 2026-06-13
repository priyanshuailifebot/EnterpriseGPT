import axios, { AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from "axios";

import { getAccessToken, setAccessToken } from "@/lib/access-token";
import type {
  CostStats,
  LoginRequest,
  LoginResponse,
  OverviewStats,
  RagAnalytics,
  RefreshResponse,
  RegisterRequest,
  ToolUsageStat,
  UserResponse,
} from "@/types/api";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export const api: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  withCredentials: true,
  timeout: 120_000,
  headers: {
    "Content-Type": "application/json",
    Accept: "application/json",
  },
});

const SKIP_REFRESH_PREFIXES = [
  "/api/v1/auth/login",
  "/api/v1/auth/register",
  "/api/v1/auth/refresh",
  // ``/logout`` returns 401 when the caller is already unauthenticated.
  // If the interceptor tried to "fix" that by calling refresh, refresh
  // would 401, which would call logout again — an infinite loop that
  // burned through the anonymous-IP rate-limit budget and locked legit
  // logins out with 429s.
  "/api/v1/auth/logout",
];

function urlSkipsRefresh(url?: string): boolean {
  if (!url) return false;
  return SKIP_REFRESH_PREFIXES.some((p) => url.startsWith(p));
}

declare module "axios" {
  interface InternalAxiosRequestConfig {
    _retry?: boolean;
  }
}

api.interceptors.request.use((config) => {
  const token = getAccessToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const status = error.response?.status;
    const originalRequest = error.config as InternalAxiosRequestConfig | undefined;

    if (
      status === 401 &&
      originalRequest &&
      !originalRequest._retry &&
      !urlSkipsRefresh(originalRequest.url)
    ) {
      originalRequest._retry = true;
      try {
        const { data } = await api.post<RefreshResponse>("/api/v1/auth/refresh", {});
        setAccessToken(data.access_token);
        originalRequest.headers.Authorization = `Bearer ${data.access_token}`;
        return api(originalRequest);
      } catch {
        // Refresh failed → caller is fully unauthenticated. Drop the
        // local token and bounce to /login. Do NOT POST /logout here:
        // the server will reject it with 401, which would re-enter
        // this interceptor and re-trigger refresh, looping forever.
        // The refresh cookie expires on its own; explicit logout is
        // only useful when the user is currently authenticated.
        setAccessToken(null);
        const path = typeof window !== "undefined" ? window.location.pathname : "";
        if (
          typeof window !== "undefined" &&
          !path.startsWith("/login") &&
          !path.startsWith("/signup")
        ) {
          window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
        }
        return Promise.reject(error);
      }
    }

    if (status === 401 && typeof window !== "undefined") {
      setAccessToken(null);
    }
    return Promise.reject(error);
  },
);

export async function refreshAccessToken(): Promise<string> {
  const { data } = await api.post<RefreshResponse>("/api/v1/auth/refresh", {});
  setAccessToken(data.access_token);
  return data.access_token;
}

export async function fetchMe(): Promise<UserResponse> {
  const { data } = await api.get<UserResponse>("/api/v1/auth/me");
  return data;
}

export async function loginRequest(body: LoginRequest): Promise<LoginResponse> {
  const { data } = await api.post<LoginResponse>("/api/v1/auth/login", body);
  setAccessToken(data.access_token);
  return data;
}

export async function registerRequest(body: RegisterRequest): Promise<LoginResponse> {
  const { data } = await api.post<LoginResponse>("/api/v1/auth/register", body);
  setAccessToken(data.access_token);
  return data;
}

export async function logoutRequest(): Promise<void> {
  await api.post("/api/v1/auth/logout", {}).catch(() => undefined);
  setAccessToken(null);
}

export async function logoutOnlyClient(): Promise<void> {
  setAccessToken(null);
}

const isoDate = (d: Date) => d.toISOString().slice(0, 10);

export async function fetchAnalyticsOverview(
  workspaceId: string,
  start: Date,
  end: Date,
): Promise<OverviewStats> {
  const { data } = await api.get<OverviewStats>("/api/v1/analytics/overview", {
    params: { workspace_id: workspaceId, start: isoDate(start), end: isoDate(end) },
  });
  return data;
}

export async function fetchAnalyticsRag(
  workspaceId: string,
  start: Date,
  end: Date,
): Promise<RagAnalytics> {
  const { data } = await api.get<RagAnalytics>("/api/v1/analytics/rag", {
    params: { workspace_id: workspaceId, start: isoDate(start), end: isoDate(end) },
  });
  return data;
}

export async function fetchAnalyticsTools(workspaceId: string): Promise<ToolUsageStat[]> {
  const { data } = await api.get<ToolUsageStat[]>("/api/v1/analytics/tools", {
    params: { workspace_id: workspaceId },
  });
  return data;
}

export async function fetchAnalyticsCosts(
  workspaceId: string,
  start: Date,
  end: Date,
): Promise<CostStats> {
  const { data } = await api.get<CostStats>("/api/v1/analytics/costs", {
    params: { workspace_id: workspaceId, start: isoDate(start), end: isoDate(end) },
  });
  return data;
}

export async function ssePostStream(opts: {
  path: string;
  body?: unknown;
  signal?: AbortSignal;
  onEvent: (parsed: Record<string, unknown>) => void;
}): Promise<void> {
  let retried401 = false;

  const url =
    opts.path.startsWith("http") ? opts.path : `${API_BASE_URL}${opts.path}`;

  for (;;) {
    const token = getAccessToken();
    const res = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(opts.body ?? {}),
      signal: opts.signal,
    });

    if (res.status === 401 && !retried401) {
      try {
        await refreshAccessToken();
        retried401 = true;
        continue;
      } catch {
        throw new Error("401 Unauthorized (refresh failed)");
      }
    }

    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`${res.status} ${text}`);
    }
    if (!res.body) throw new Error("No response stream");
    await pumpSseReadableStream(res.body, opts.onEvent);
    return;
  }
}

async function pumpSseReadableStream(
  stream: ReadableStream<Uint8Array>,
  onEvent: (parsed: Record<string, unknown>) => void,
): Promise<void> {
  const { createParser } = await import("eventsource-parser");

  const reader = stream.getReader();
  const decoder = new TextDecoder();

  const parser = createParser({
    onEvent: (evt) => {
      if (!evt.data) return;
      try {
        const parsed = JSON.parse(evt.data) as Record<string, unknown>;
        if (parsed && typeof parsed === "object") onEvent(parsed);
      } catch {
        /* ignore malformed */
      }
    },
  });

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    parser.feed(decoder.decode(value, { stream: true }));
  }
}

export type ApiError = AxiosError<{ detail?: string; message?: string }>;

export function getErrorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const data = error.response?.data as
      | { detail?: string | { msg?: string }[]; message?: string }
      | undefined;
    if (typeof data?.detail === "string") return data.detail;
    if (Array.isArray(data?.detail) && data.detail[0]?.msg) return data.detail[0].msg;
    return data?.message ?? error.message;
  }
  if (error instanceof Error) return error.message;
  return "Unknown error";
}
