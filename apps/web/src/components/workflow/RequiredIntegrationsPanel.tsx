"use client";

/**
 * Required-integrations panel — n8n-style "what does this workflow need to go
 * live" checklist, with inline connect.
 *
 * Posts the current (possibly unsaved) definition to
 * ``/workflows/{id}/requirements`` and renders each required/optional
 * integration with a live connected/missing badge. OAuth providers connect via
 * the popup; API-key providers get a compact inline form. The same evaluation
 * backs the server-side publish gate, so a green panel means publishable.
 */

import {
  CheckCircle2,
  CircleAlert,
  Loader2,
  PlugZap,
  RefreshCw,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import toast from "react-hot-toast";

import { api } from "@/lib/api";
import { startInlineOAuth } from "@/lib/oauth-popup";
import { cn } from "@/lib/utils";
import type {
  NativeConnectionResponse,
  NativeProviderCatalogEntry,
  NativeProviderCatalogResponse,
  WorkflowDefinition,
  WorkflowRequirement,
  WorkflowRequirementsResponse,
} from "@/types/api";

interface RequiredIntegrationsPanelProps {
  open: boolean;
  onClose: () => void;
  workflowId: string | null;
  workspaceId: string | null;
  definition: WorkflowDefinition;
}

export function RequiredIntegrationsPanel({
  open,
  onClose,
  workflowId,
  workspaceId,
  definition,
}: RequiredIntegrationsPanelProps) {
  const [reqs, setReqs] = useState<WorkflowRequirement[]>([]);
  const [publishable, setPublishable] = useState(true);
  const [loading, setLoading] = useState(false);
  const [catalog, setCatalog] = useState<NativeProviderCatalogEntry[]>([]);

  const catalogById = useMemo(() => {
    const m = new Map<string, NativeProviderCatalogEntry>();
    for (const p of catalog) m.set(p.id, p);
    return m;
  }, [catalog]);

  const refresh = useCallback(async () => {
    if (!workflowId) return;
    setLoading(true);
    try {
      const [reqRes, catRes] = await Promise.all([
        api.post<WorkflowRequirementsResponse>(
          `/api/v1/workflows/${workflowId}/requirements`,
          { definition },
        ),
        api
          .get<NativeProviderCatalogResponse>("/api/v1/connections/providers")
          .catch(() => null),
      ]);
      setReqs(reqRes.data.requirements);
      setPublishable(reqRes.data.publishable);
      if (catRes) setCatalog(catRes.data.providers);
    } catch {
      toast.error("Could not load integration requirements.");
    } finally {
      setLoading(false);
    }
  }, [workflowId, definition]);

  useEffect(() => {
    if (open) void refresh();
    // Re-evaluate whenever the panel opens; ``refresh`` already depends on the
    // current definition so reopening after an edit shows fresh status.
  }, [open, refresh]);

  if (!open) return null;

  const missing = reqs.filter((r) => r.required && !r.connected);

  return (
    <div className="fixed right-0 top-0 z-30 flex h-full w-[400px] max-w-[94vw] flex-col border-l border-slate-200 bg-white shadow-2xl dark:border-slate-800 dark:bg-slate-950">
      <header className="flex items-center justify-between gap-2 border-b border-slate-200 px-4 py-3 dark:border-slate-800">
        <div>
          <p className="text-sm font-semibold text-slate-900 dark:text-slate-100">
            Integrations
          </p>
          <p className="text-[10px] text-slate-500 dark:text-slate-400">
            What this workflow needs to publish & run for real.
          </p>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="rounded-md p-1 text-slate-500 hover:bg-slate-100 disabled:opacity-50 dark:hover:bg-slate-800"
            aria-label="Refresh"
            title="Re-check status"
          >
            <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
          </button>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </header>

      <div
        className={cn(
          "border-b px-4 py-2 text-[11px] font-medium",
          publishable
            ? "border-emerald-100 bg-emerald-50 text-emerald-800 dark:border-emerald-950 dark:bg-emerald-950/40 dark:text-emerald-200"
            : "border-amber-100 bg-amber-50 text-amber-800 dark:border-amber-950 dark:bg-amber-950/40 dark:text-amber-200",
        )}
      >
        {publishable
          ? "All required integrations are connected — ready to publish."
          : `${missing.length} required integration${missing.length === 1 ? "" : "s"} still need connecting before publish.`}
      </div>

      <div className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
        {loading && reqs.length === 0 ? (
          <p className="flex items-center gap-2 text-[12px] text-slate-500 dark:text-slate-400">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Checking…
          </p>
        ) : reqs.length === 0 ? (
          <p className="rounded-xl border border-dashed border-slate-200 p-4 text-center text-[11px] text-slate-500 dark:border-slate-800 dark:text-slate-400">
            This workflow doesn&apos;t need any external integrations — it uses
            only the platform&apos;s built-in model and tools.
          </p>
        ) : (
          reqs.map((r) => (
            <RequirementRow
              key={r.provider}
              req={r}
              workspaceId={workspaceId}
              catalogEntry={catalogById.get(r.provider) ?? null}
              onConnected={() => void refresh()}
            />
          ))
        )}
      </div>
    </div>
  );
}

function RequirementRow({
  req,
  workspaceId,
  catalogEntry,
  onConnected,
}: {
  req: WorkflowRequirement;
  workspaceId: string | null;
  catalogEntry: NativeProviderCatalogEntry | null;
  onConnected: () => void;
}) {
  const [connecting, setConnecting] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const isOAuth = (req.auth_type ?? "").toLowerCase().includes("oauth");

  const connectOAuth = useCallback(async () => {
    if (!workspaceId) return;
    setConnecting(true);
    try {
      const res = await startInlineOAuth({
        provider: req.provider,
        workspaceId,
        connectionName: req.name,
      });
      if (res.ok) {
        toast.success(`Connected to ${req.name}.`);
        onConnected();
      } else {
        toast.error(res.message || "OAuth was not completed.");
      }
    } finally {
      setConnecting(false);
    }
  }, [workspaceId, req.provider, req.name, onConnected]);

  const submitApiKey = useCallback(async () => {
    if (!workspaceId || !catalogEntry) return;
    setConnecting(true);
    try {
      const res = await api.post<NativeConnectionResponse>(
        `/api/v1/connections?workspace_id=${workspaceId}`,
        { provider: catalogEntry.id, name: catalogEntry.name, config: values },
      );
      try {
        await api.post(
          `/api/v1/connections/${res.data.id}/test?workspace_id=${workspaceId}`,
        );
      } catch {
        /* surfaced on refresh via last_test_error */
      }
      toast.success(`Connected to ${req.name}.`);
      setFormOpen(false);
      setValues({});
      onConnected();
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Could not save the connection.";
      toast.error(detail);
    } finally {
      setConnecting(false);
    }
  }, [workspaceId, catalogEntry, values, req.name, onConnected]);

  return (
    <div
      className={cn(
        "rounded-xl border p-3",
        req.connected
          ? "border-emerald-200 bg-emerald-50/50 dark:border-emerald-900 dark:bg-emerald-950/20"
          : req.required
            ? "border-amber-200 bg-amber-50/50 dark:border-amber-900 dark:bg-amber-950/20"
            : "border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            {req.connected ? (
              <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-600" />
            ) : (
              <CircleAlert
                className={cn(
                  "h-3.5 w-3.5 shrink-0",
                  req.required ? "text-amber-600" : "text-slate-400",
                )}
              />
            )}
            <span className="truncate text-[13px] font-semibold text-slate-900 dark:text-slate-100">
              {req.name}
            </span>
            <span className="rounded-full bg-slate-100 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide text-slate-500 dark:bg-slate-800 dark:text-slate-400">
              {req.kind}
            </span>
            {!req.required ? (
              <span className="text-[9px] text-slate-400">optional</span>
            ) : null}
          </div>
          <p className="mt-1 text-[11px] leading-snug text-slate-600 dark:text-slate-400">
            {req.reason}
          </p>
          {req.used_by.length > 0 ? (
            <p className="mt-0.5 text-[10px] text-slate-400">
              Used by: {req.used_by.join(", ")}
            </p>
          ) : null}
        </div>

        {!req.connected ? (
          <ConnectButton
            req={req}
            isOAuth={isOAuth}
            connectable={req.connectable && !!catalogEntry}
            connecting={connecting}
            onOAuth={() => void connectOAuth()}
            onApiKey={() => setFormOpen((v) => !v)}
          />
        ) : (
          <span className="shrink-0 text-[10px] font-semibold text-emerald-700 dark:text-emerald-300">
            Connected
          </span>
        )}
      </div>

      {formOpen && catalogEntry && !isOAuth ? (
        <div className="mt-3 flex flex-col gap-2 border-t border-slate-200 pt-3 dark:border-slate-700">
          {catalogEntry.fields.map((f) => (
            <label key={f.key} className="flex flex-col gap-1">
              <span className="text-[10px] font-medium text-slate-500 dark:text-slate-400">
                {f.label}
                {f.required ? " *" : ""}
              </span>
              <input
                type={f.type === "secret" ? "password" : "text"}
                value={values[f.key] ?? ""}
                placeholder={f.placeholder ?? ""}
                onChange={(e) =>
                  setValues((v) => ({ ...v, [f.key]: e.target.value }))
                }
                className="w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-[12px] text-slate-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
              />
            </label>
          ))}
          <div className="flex justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={() => setFormOpen(false)}
              className="text-[11px] text-slate-500 hover:text-slate-700 dark:text-slate-400"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void submitApiKey()}
              disabled={connecting}
              className="inline-flex items-center gap-1 rounded-lg bg-brand-600 px-3 py-1 text-[11px] font-semibold text-white disabled:opacity-50"
            >
              {connecting ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <PlugZap className="h-3 w-3" />
              )}
              {connecting ? "Connecting…" : "Connect & test"}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function ConnectButton({
  req,
  isOAuth,
  connectable,
  connecting,
  onOAuth,
  onApiKey,
}: {
  req: WorkflowRequirement;
  isOAuth: boolean;
  connectable: boolean;
  connecting: boolean;
  onOAuth: () => void;
  onApiKey: () => void;
}) {
  if (!connectable) {
    return (
      <span
        className="shrink-0 text-[10px] text-slate-400"
        title="Connect this in the Integrations page"
      >
        {req.kind === "saas" ? "via Integrations" : "not connectable here"}
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={isOAuth ? onOAuth : onApiKey}
      disabled={connecting}
      className="inline-flex shrink-0 items-center gap-1 rounded-lg bg-brand-600 px-2.5 py-1 text-[11px] font-semibold text-white hover:bg-brand-700 disabled:opacity-50"
    >
      {connecting ? (
        <Loader2 className="h-3 w-3 animate-spin" />
      ) : (
        <PlugZap className="h-3 w-3" />
      )}
      Connect
    </button>
  );
}
