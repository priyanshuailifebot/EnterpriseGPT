"use client";

import {
  CheckCircle2,
  Cpu,
  Globe,
  KeyRound,
  Loader2,
  Pencil,
  Plug,
  Search,
  Trash2,
  XCircle,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import toast from "react-hot-toast";
import axios from "axios";

import { MCPServersPanel } from "@/components/integrations/MCPServersPanel";
import { api, getErrorMessage } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";
import type {
  NativeConnectionCreateRequest,
  NativeConnectionPatchRequest,
  NativeConnectionResponse,
  NativeConnectionTestResponse,
  NativeProviderCatalogEntry,
  NativeProviderCatalogResponse,
} from "@/types/api";

function ProviderIcon({ icon }: { icon: string }) {
  const className = "h-5 w-5 text-brand-500";
  if (icon === "cpu") return <Cpu className={className} />;
  if (icon === "globe") return <Globe className={className} />;
  return <Search className={className} />;
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; tone: string; Icon: typeof CheckCircle2 }> = {
    active: {
      label: "Active",
      tone: "bg-emerald-50 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-100",
      Icon: CheckCircle2,
    },
    error: {
      label: "Error",
      tone: "bg-red-50 text-red-800 dark:bg-red-950 dark:text-red-100",
      Icon: XCircle,
    },
    revoked: {
      label: "Revoked",
      tone: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200",
      Icon: XCircle,
    },
  };
  const cfg = map[status] ?? map.revoked;
  const { Icon } = cfg;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold ${cfg.tone}`}
    >
      <Icon className="h-3 w-3" />
      {cfg.label}
    </span>
  );
}

type ConnectModalProps = {
  provider: NativeProviderCatalogEntry;
  existing?: NativeConnectionResponse | null;
  workspaceId: string;
  onClose: () => void;
  onSaved: (row: NativeConnectionResponse) => void;
};

function ConnectModal({
  provider,
  existing,
  workspaceId,
  onClose,
  onSaved,
}: ConnectModalProps) {
  const isEdit = Boolean(existing);
  const [name, setName] = useState(existing?.name ?? provider.name);
  const [values, setValues] = useState<Record<string, string>>(() => {
    const seed: Record<string, string> = {};
    for (const f of provider.fields) seed[f.key] = "";
    return seed;
  });
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<NativeConnectionTestResponse | null>(null);

  const setVal = (key: string, v: string) =>
    setValues((prev) => ({ ...prev, [key]: v }));

  const save = useCallback(async (): Promise<NativeConnectionResponse | null> => {
    setSaving(true);
    try {
      const filledConfig: Record<string, unknown> = {};
      for (const f of provider.fields) {
        if (values[f.key]) filledConfig[f.key] = values[f.key];
      }
      let row: NativeConnectionResponse;
      if (isEdit && existing) {
        const body: NativeConnectionPatchRequest = {
          name: name !== existing.name ? name : undefined,
          config: Object.keys(filledConfig).length > 0 ? filledConfig : undefined,
        };
        const res = await api.patch<NativeConnectionResponse>(
          `/api/v1/connections/${existing.id}?workspace_id=${workspaceId}`,
          body,
        );
        row = res.data;
      } else {
        const body: NativeConnectionCreateRequest = {
          provider: provider.id,
          name,
          config: filledConfig,
        };
        const res = await api.post<NativeConnectionResponse>(
          `/api/v1/connections?workspace_id=${workspaceId}`,
          body,
        );
        row = res.data;
      }
      onSaved(row);
      return row;
    } catch (e) {
      toast.error(axios.isAxiosError(e) ? getErrorMessage(e) : "Could not save connection.");
      return null;
    } finally {
      setSaving(false);
    }
  }, [provider, values, name, isEdit, existing, workspaceId, onSaved]);

  const saveAndTest = useCallback(async () => {
    const row = await save();
    if (!row) return;
    setTesting(true);
    try {
      const res = await api.post<NativeConnectionTestResponse>(
        `/api/v1/connections/${row.id}/test?workspace_id=${workspaceId}`,
      );
      setTestResult(res.data);
      if (res.data.success) toast.success("Credentials verified.");
      else toast.error(`Test failed: ${res.data.message}`);
    } catch (e) {
      toast.error(axios.isAxiosError(e) ? getErrorMessage(e) : "Test request failed.");
    } finally {
      setTesting(false);
    }
  }, [save, workspaceId]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-md rounded-3xl border border-slate-200 bg-white p-6 shadow-2xl dark:border-slate-800 dark:bg-slate-950">
        <div className="mb-4 flex items-start justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="rounded-xl bg-brand-50 p-2 dark:bg-brand-950">
              <ProviderIcon icon={provider.icon} />
            </div>
            <div>
              <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-50">
                {isEdit ? "Edit" : "Connect"} {provider.name}
              </h2>
              <p className="text-xs text-slate-500 dark:text-slate-400">
                {provider.description}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-sm text-slate-400 hover:text-slate-700 dark:hover:text-slate-200"
          >
            ✕
          </button>
        </div>

        <div className="space-y-4">
          <label className="block">
            <span className="text-xs font-medium text-slate-600 dark:text-slate-300">
              Connection name
            </span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="mt-1 w-full rounded-xl border border-slate-200 bg-transparent px-3 py-2 text-sm dark:border-slate-700"
            />
          </label>

          {provider.fields.map((f) => (
            <label key={f.key} className="block">
              <span className="text-xs font-medium text-slate-600 dark:text-slate-300">
                {f.label}
                {f.required ? " *" : ""}
              </span>
              <input
                type={f.type === "secret" ? "password" : "text"}
                value={values[f.key] ?? ""}
                onChange={(e) => setVal(f.key, e.target.value)}
                placeholder={
                  f.placeholder ??
                  (isEdit ? "Leave blank to keep existing value" : undefined)
                }
                className="mt-1 w-full rounded-xl border border-slate-200 bg-transparent px-3 py-2 font-mono text-xs dark:border-slate-700"
              />
              {f.help_text ? (
                <span className="text-[11px] text-slate-500 dark:text-slate-400">
                  {f.help_text}
                </span>
              ) : null}
            </label>
          ))}

          {testResult ? (
            <div
              className={`rounded-xl px-3 py-2 text-xs ${
                testResult.success
                  ? "bg-emerald-50 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100"
                  : "bg-red-50 text-red-900 dark:bg-red-950 dark:text-red-100"
              }`}
            >
              {testResult.success ? "✓ " : "✗ "}
              {testResult.message}
            </div>
          ) : null}

          {provider.docs_url ? (
            <a
              href={provider.docs_url}
              target="_blank"
              rel="noreferrer"
              className="block text-xs text-brand-700 hover:underline dark:text-brand-300"
            >
              Get an API key →
            </a>
          ) : null}
        </div>

        <div className="mt-6 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-xl border border-slate-200 px-4 py-2 text-sm dark:border-slate-700"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={saving || testing}
            onClick={() => void saveAndTest()}
            className="inline-flex items-center gap-2 rounded-xl bg-brand-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
          >
            {(saving || testing) ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            {isEdit ? "Save & test" : "Connect & test"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function IntegrationsPage() {
  const workspaceId = useAuthStore((s) => s.workspaceId);
  const hasManage = useAuthStore((s) => s.hasPermission("workspace:manage"));

  const [catalog, setCatalog] = useState<NativeProviderCatalogEntry[]>([]);
  const [connections, setConnections] = useState<NativeConnectionResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [modal, setModal] = useState<{
    provider: NativeProviderCatalogEntry;
    existing: NativeConnectionResponse | null;
  } | null>(null);

  const refresh = useCallback(async () => {
    if (!workspaceId) return;
    setLoading(true);
    try {
      const [catRes, connRes] = await Promise.all([
        api.get<NativeProviderCatalogResponse>("/api/v1/connections/providers"),
        api.get<NativeConnectionResponse[]>(
          `/api/v1/connections?workspace_id=${workspaceId}`,
        ),
      ]);
      setCatalog(catRes.data.providers);
      setConnections(connRes.data);
    } catch (e) {
      toast.error(axios.isAxiosError(e) ? getErrorMessage(e) : "Failed to load integrations.");
    } finally {
      setLoading(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const connByProvider = useMemo(() => {
    const map: Record<string, NativeConnectionResponse[]> = {};
    for (const c of connections) {
      (map[c.provider] ??= []).push(c);
    }
    return map;
  }, [connections]);

  const grouped = useMemo(() => {
    const groups: Record<string, NativeProviderCatalogEntry[]> = {};
    for (const p of catalog) {
      (groups[p.category] ??= []).push(p);
    }
    return groups;
  }, [catalog]);

  const testConnection = useCallback(
    async (row: NativeConnectionResponse) => {
      if (!workspaceId) return;
      try {
        const res = await api.post<NativeConnectionTestResponse>(
          `/api/v1/connections/${row.id}/test?workspace_id=${workspaceId}`,
        );
        if (res.data.success) toast.success(`${row.name}: ${res.data.message}`);
        else toast.error(`${row.name}: ${res.data.message}`);
        await refresh();
      } catch (e) {
        toast.error(axios.isAxiosError(e) ? getErrorMessage(e) : "Test failed.");
      }
    },
    [workspaceId, refresh],
  );

  const startOAuth = useCallback(
    async (provider: NativeProviderCatalogEntry) => {
      if (!workspaceId) return;
      const name = window.prompt(
        `Name this ${provider.name} connection`,
        provider.name,
      );
      if (!name) return;
      try {
        const { data } = await api.post<{ redirect_url: string; state: string }>(
          `/api/v1/connections/oauth/${provider.id}/authorize?workspace_id=${workspaceId}&connection_name=${encodeURIComponent(name)}`,
        );
        window.sessionStorage.setItem(
          `oauth_pending_${data.state}`,
          JSON.stringify({ workspaceId, provider: provider.id, name }),
        );
        window.location.href = data.redirect_url;
      } catch (e) {
        toast.error(axios.isAxiosError(e) ? getErrorMessage(e) : "Could not start OAuth.");
      }
    },
    [workspaceId],
  );

  const disconnect = useCallback(
    async (row: NativeConnectionResponse) => {
      if (!workspaceId) return;
      if (!confirm(`Disconnect ${row.name}?`)) return;
      try {
        await api.delete(`/api/v1/connections/${row.id}?workspace_id=${workspaceId}`);
        toast.success("Disconnected.");
        await refresh();
      } catch (e) {
        toast.error(axios.isAxiosError(e) ? getErrorMessage(e) : "Could not disconnect.");
      }
    },
    [workspaceId, refresh],
  );

  if (!workspaceId) {
    return (
      <p className="text-sm text-slate-600 dark:text-slate-400">
        Select a workspace to manage integrations.
      </p>
    );
  }

  const categoryOrder = ["search", "scraping", "llm", "oauth", "mcp"];
  const categoryLabel: Record<string, string> = {
    search: "Web search",
    scraping: "Web scraping",
    llm: "LLM providers",
    oauth: "OAuth apps",
    mcp: "MCP servers",
  };

  return (
    <div className="mx-auto max-w-6xl space-y-10">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-50">
          Integrations
        </h1>
        <p className="text-sm text-slate-600 dark:text-slate-400">
          Connect providers directly via native Dynamiq connectors — no third-party
          proxy. Tools resolve in-process for lower latency and zero external
          dependency. Composio remains available as a fallback for OAuth apps
          not yet built natively.
        </p>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-slate-500">
          <Loader2 className="h-4 w-4 animate-spin" />
          Loading catalog…
        </div>
      ) : null}

      <MCPServersPanel workspaceId={workspaceId} hasManage={hasManage} />

      {categoryOrder.map((cat) => {
        const providers = grouped[cat];
        if (!providers || providers.length === 0) return null;
        return (
          <section key={cat} className="space-y-4">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-500">
              {categoryLabel[cat] ?? cat}
            </h2>
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {providers.map((p) => {
                const rows = connByProvider[p.id] ?? [];
                const primary = rows[0];
                return (
                  <div
                    key={p.id}
                    className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-950"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex items-center gap-3">
                        <div className="rounded-xl bg-brand-50 p-2 dark:bg-brand-950">
                          <ProviderIcon icon={p.icon} />
                        </div>
                        <div>
                          <p className="text-base font-semibold text-slate-900 dark:text-slate-50">
                            {p.name}
                          </p>
                          {primary ? <StatusBadge status={primary.status} /> : null}
                        </div>
                      </div>
                    </div>
                    <p className="mt-3 line-clamp-2 text-xs text-slate-600 dark:text-slate-400">
                      {p.description}
                    </p>

                    {rows.length > 0 ? (
                      <ul className="mt-3 space-y-1.5">
                        {rows.map((r) => (
                          <li
                            key={r.id}
                            className="flex items-center justify-between gap-2 rounded-xl border border-slate-100 px-3 py-2 text-xs dark:border-slate-800"
                          >
                            <div className="min-w-0">
                              <p className="truncate font-medium text-slate-900 dark:text-slate-100">
                                {r.name}
                              </p>
                              {r.last_test_error ? (
                                <p className="truncate text-[11px] text-red-700 dark:text-red-300">
                                  {r.last_test_error}
                                </p>
                              ) : null}
                            </div>
                            <div className="flex items-center gap-1">
                              <button
                                type="button"
                                title="Test"
                                onClick={() => void testConnection(r)}
                                className="rounded p-1 text-slate-500 hover:bg-slate-100 hover:text-slate-900 dark:hover:bg-slate-800 dark:hover:text-slate-100"
                              >
                                <KeyRound className="h-3.5 w-3.5" />
                              </button>
                              {hasManage ? (
                                <>
                                  <button
                                    type="button"
                                    title="Edit"
                                    onClick={() => setModal({ provider: p, existing: r })}
                                    className="rounded p-1 text-slate-500 hover:bg-slate-100 hover:text-slate-900 dark:hover:bg-slate-800 dark:hover:text-slate-100"
                                  >
                                    <Pencil className="h-3.5 w-3.5" />
                                  </button>
                                  <button
                                    type="button"
                                    title="Disconnect"
                                    onClick={() => void disconnect(r)}
                                    className="rounded p-1 text-red-500 hover:bg-red-50 dark:hover:bg-red-950"
                                  >
                                    <Trash2 className="h-3.5 w-3.5" />
                                  </button>
                                </>
                              ) : null}
                            </div>
                          </li>
                        ))}
                      </ul>
                    ) : null}

                    {hasManage ? (
                      <button
                        type="button"
                        onClick={() =>
                          p.auth_type === "oauth2"
                            ? void startOAuth(p)
                            : setModal({ provider: p, existing: null })
                        }
                        className="mt-4 inline-flex w-full items-center justify-center gap-2 rounded-xl bg-brand-600 px-3 py-2 text-sm font-semibold text-white"
                      >
                        <Plug className="h-4 w-4" />
                        {rows.length > 0 ? "Add another" : "Connect"}
                      </button>
                    ) : (
                      <p className="mt-4 text-[11px] text-slate-500">
                        Ask an admin to connect this provider.
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          </section>
        );
      })}

      {modal ? (
        <ConnectModal
          provider={modal.provider}
          existing={modal.existing}
          workspaceId={workspaceId}
          onClose={() => setModal(null)}
          onSaved={async () => {
            setModal(null);
            await refresh();
          }}
        />
      ) : null}
    </div>
  );
}
