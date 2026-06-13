"use client";

/**
 * Right-rail property inspector — kind-aware editor for the selected node.
 *
 * Renders different field sets per ``WorkflowNode.kind`` and patches the
 * editor store on every change. JSON-shaped fields (``params``,
 * ``json_schema``, ``payload``, ``filter``) get a Monaco-free textarea
 * with on-the-fly JSON validation so a bad keystroke doesn't corrupt the
 * definition — invalid JSON is kept locally while the field is in error
 * and only committed when it parses cleanly.
 */

import { CheckCircle2, Loader2, PlugZap, Sparkles, Trash2 } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import toast from "react-hot-toast";

import { api } from "@/lib/api";
import { startInlineOAuth } from "@/lib/oauth-popup";
import { cn } from "@/lib/utils";
import { useAuthStore } from "@/stores/authStore";
import type {
  ActionNode,
  AgentNode,
  ConditionNode,
  DataStoreNode,
  ForEachNode,
  IfNode,
  MemoryNode,
  NativeConnectionResponse,
  NativeProviderCatalogEntry,
  NativeProviderCatalogResponse,
  NodeSummaryResponse,
  OutputParserNode,
  TriggerNode,
  WaitForWebhookNode,
  WorkflowDefinition,
  WorkflowNode,
} from "@/types/api";

import type { EditorState } from "./useWorkflowEditor";
import { allNodes } from "./workflow-mutations";
import { summarizeNode } from "./workflow-summary";

interface PropertyInspectorProps {
  state: Pick<EditorState, "definition" | "selectedId">;
  onPatchNode: (id: string, patch: Partial<WorkflowNode>) => void;
  onRenameId: (oldId: string, newId: string) => void;
  onRemoveNode: (id: string) => void;
  onClearSelection: () => void;
  /** Saved-workflow id, enabling the LLM "detailed summary" call. ``null``
   *  for an unsaved draft — the instant template still shows. */
  workflowId?: string | null;
}

export function PropertyInspector({
  state,
  onPatchNode,
  onRenameId,
  onRemoveNode,
  onClearSelection,
  workflowId,
}: PropertyInspectorProps) {
  const selected = useMemo(() => {
    if (!state.selectedId) return null;
    return allNodes(state.definition).find((n) => n.id === state.selectedId) ?? null;
  }, [state.definition, state.selectedId]);

  if (!selected) {
    return (
      <aside
        aria-label="Property inspector"
        className="flex h-full w-80 shrink-0 flex-col rounded-2xl border border-dashed border-slate-200 bg-white/60 p-6 text-center text-xs text-slate-500 shadow-sm dark:border-slate-800 dark:bg-slate-950/60 dark:text-slate-400"
      >
        <p className="m-auto max-w-[14rem] leading-snug">
          Select a node on the canvas to edit its properties, or drag a node
          from the left palette to add a new step.
        </p>
      </aside>
    );
  }

  return (
    <aside
      aria-label="Property inspector"
      className="flex h-full w-80 shrink-0 flex-col gap-3 overflow-y-auto rounded-2xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-950"
    >
      <header className="flex items-center justify-between">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
            {selected.kind.replace(/_/g, " ")}
          </p>
          <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
            {selected.name || selected.id}
          </h2>
        </div>
        <button
          type="button"
          onClick={() => {
            onRemoveNode(selected.id);
            onClearSelection();
          }}
          className="rounded-md p-1.5 text-rose-600 hover:bg-rose-50 dark:hover:bg-rose-950/40"
          aria-label="Delete node"
          title="Delete node"
        >
          <Trash2 className="h-4 w-4" />
        </button>
      </header>

      <NodeSummary
        node={selected}
        definition={state.definition}
        workflowId={workflowId}
      />

      <CommonFields
        node={selected}
        onPatch={(p) => onPatchNode(selected.id, p)}
        onRename={(newId) => onRenameId(selected.id, newId)}
      />

      <KindSpecificFields
        node={selected}
        definition={state.definition}
        onPatch={(p) => onPatchNode(selected.id, p)}
      />
    </aside>
  );
}

// ---------------------------------------------------------------------------
// Node summary — instant deterministic template + optional LLM detail
// ---------------------------------------------------------------------------

/** Detailed (LLM) summaries cached for the session, keyed by workflow + node
 *  content so an unchanged node is never re-fetched. */
const detailedSummaryCache = new Map<string, string>();

function NodeSummary({
  node,
  definition,
  workflowId,
}: {
  node: WorkflowNode;
  definition: WorkflowDefinition;
  workflowId?: string | null;
}) {
  const template = useMemo(
    () => summarizeNode(node, definition),
    [node, definition],
  );
  // Cache key folds in the node's content so editing it invalidates the
  // detailed summary; re-selecting an unchanged node restores it for free.
  const cacheKey = useMemo(
    () => `${workflowId ?? "_"}:${node.id}:${JSON.stringify(node)}`,
    [workflowId, node],
  );

  const [detailed, setDetailed] = useState<string | null>(
    () => detailedSummaryCache.get(cacheKey) ?? null,
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDetailed(detailedSummaryCache.get(cacheKey) ?? null);
    setError(null);
  }, [cacheKey]);

  const generate = useCallback(async () => {
    if (!workflowId) return;
    setLoading(true);
    setError(null);
    try {
      const { data } = await api.post<NodeSummaryResponse>(
        `/api/v1/workflows/${workflowId}/nodes/${encodeURIComponent(node.id)}/summary`,
        { definition },
      );
      detailedSummaryCache.set(cacheKey, data.summary);
      setDetailed(data.summary);
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Could not generate a summary.";
      setError(detail);
    } finally {
      setLoading(false);
    }
  }, [workflowId, node.id, definition, cacheKey]);

  return (
    <section className="flex flex-col gap-2 rounded-xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/60">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
          {detailed ? "Detailed summary" : "Summary"}
        </span>
        <button
          type="button"
          onClick={() => void generate()}
          disabled={loading || !workflowId}
          title={
            workflowId
              ? "Generate a detailed AI explanation of this node"
              : "Save the workflow to enable AI summaries"
          }
          className="inline-flex items-center gap-1 rounded-md border border-brand-200 px-2 py-1 text-[10px] font-semibold text-brand-700 hover:bg-brand-50 disabled:opacity-50 dark:border-brand-900 dark:text-brand-300 dark:hover:bg-brand-950"
        >
          {loading ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <Sparkles className="h-3 w-3" />
          )}
          {loading ? "Generating…" : detailed ? "Regenerate" : "Detailed summary"}
        </button>
      </div>
      <p className="text-[11px] leading-relaxed text-slate-600 dark:text-slate-300">
        {detailed ?? template}
      </p>
      {error ? (
        <p className="text-[10px] text-rose-600 dark:text-rose-400">{error}</p>
      ) : null}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Common fields — id / name / depends_on summary
// ---------------------------------------------------------------------------

function CommonFields({
  node,
  onPatch,
  onRename,
}: {
  node: WorkflowNode;
  onPatch: (patch: Partial<WorkflowNode>) => void;
  onRename: (newId: string) => void;
}) {
  const [idDraft, setIdDraft] = useState(node.id);
  useEffect(() => setIdDraft(node.id), [node.id]);
  const idValid = /^[a-zA-Z0-9_-]+$/.test(idDraft);

  return (
    <section className="flex flex-col gap-2">
      <Field label="Name">
        <input
          value={node.name}
          onChange={(e) => onPatch({ name: e.target.value })}
          className={inputClasses}
        />
      </Field>
      <Field label="ID" hint="snake_case, letters/digits/underscores/hyphens only.">
        <input
          value={idDraft}
          onChange={(e) => setIdDraft(e.target.value)}
          onBlur={() => {
            if (idDraft && idDraft !== node.id && idValid) onRename(idDraft);
            else setIdDraft(node.id);
          }}
          className={cn(inputClasses, !idValid && "border-rose-500 focus:border-rose-500")}
        />
      </Field>
      {node.depends_on.length > 0 ? (
        <Field label="Depends on">
          <p className="text-[10px] text-slate-500 dark:text-slate-400">
            {node.depends_on.join(", ")}{" "}
            <span className="opacity-60">(drag edges on the canvas to change)</span>
          </p>
        </Field>
      ) : null}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Kind-specific editors
// ---------------------------------------------------------------------------

function KindSpecificFields({
  node,
  definition,
  onPatch,
}: {
  node: WorkflowNode;
  definition: PropertyInspectorProps["state"]["definition"];
  onPatch: (patch: Partial<WorkflowNode>) => void;
}) {
  switch (node.kind) {
    case "agent":
      return <AgentFields node={node} definition={definition} onPatch={onPatch as (p: Partial<AgentNode>) => void} />;
    case "trigger":
      return <TriggerFields node={node} onPatch={onPatch as (p: Partial<TriggerNode>) => void} />;
    case "action":
      return <ActionFields node={node} onPatch={onPatch as (p: Partial<ActionNode>) => void} />;
    case "condition":
      return <ConditionFields node={node} onPatch={onPatch as (p: Partial<ConditionNode>) => void} />;
    case "if":
      return <IfFields node={node} onPatch={onPatch as (p: Partial<IfNode>) => void} />;
    case "for_each":
      return <ForEachFields node={node} definition={definition} onPatch={onPatch as (p: Partial<ForEachNode>) => void} />;
    case "wait_for_webhook":
      return <WaitFields node={node} onPatch={onPatch as (p: Partial<WaitForWebhookNode>) => void} />;
    case "data_store":
      return <DataStoreFields node={node} onPatch={onPatch as (p: Partial<DataStoreNode>) => void} />;
    case "memory":
      return <MemoryFields node={node} onPatch={onPatch as (p: Partial<MemoryNode>) => void} />;
    case "output_parser":
      return <OutputParserFields node={node} onPatch={onPatch as (p: Partial<OutputParserNode>) => void} />;
    case "merge":
      return null;
    default:
      return null;
  }
}

function AgentFields({
  node,
  definition,
  onPatch,
}: {
  node: AgentNode;
  definition: PropertyInspectorProps["state"]["definition"];
  onPatch: (p: Partial<AgentNode>) => void;
}) {
  const memoryOptions = allNodes(definition).filter((n) => n.kind === "memory");
  const parserOptions = allNodes(definition).filter((n) => n.kind === "output_parser");
  return (
    <section className="flex flex-col gap-2">
      <Field label="Role" hint="Persona / what the agent IS (shown in the system prompt).">
        <textarea
          value={node.role}
          onChange={(e) => onPatch({ role: e.target.value })}
          className={textareaClasses}
          rows={2}
        />
      </Field>
      <Field label="Instructions" hint="How the agent should BEHAVE. ≤250 words.">
        <textarea
          value={node.instructions}
          onChange={(e) => onPatch({ instructions: e.target.value })}
          className={textareaClasses}
          rows={5}
        />
      </Field>
      <Field label="Memory" hint="A Memory node to thread conversation state.">
        <select
          value={node.memory_ref ?? ""}
          onChange={(e) => onPatch({ memory_ref: e.target.value })}
          className={inputClasses}
        >
          <option value="">— none —</option>
          {memoryOptions.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name} ({m.id})
            </option>
          ))}
        </select>
      </Field>
      <Field label="Output parser" hint="Validate final output against a JSON schema.">
        <select
          value={node.output_parser_ref ?? ""}
          onChange={(e) => onPatch({ output_parser_ref: e.target.value })}
          className={inputClasses}
        >
          <option value="">— none —</option>
          {parserOptions.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name} ({p.id})
            </option>
          ))}
        </select>
      </Field>
      <Field label="Model provider">
        <input
          value={node.chat_model?.provider ?? ""}
          onChange={(e) =>
            onPatch({
              chat_model: {
                provider: e.target.value,
                model: node.chat_model?.model ?? "",
                temperature: node.chat_model?.temperature ?? 0,
              },
            })
          }
          placeholder="azure | openai | anthropic"
          className={inputClasses}
        />
      </Field>
      <Field label="Model name">
        <input
          value={node.chat_model?.model ?? ""}
          onChange={(e) =>
            onPatch({
              chat_model: {
                provider: node.chat_model?.provider ?? "azure",
                model: e.target.value,
                temperature: node.chat_model?.temperature ?? 0,
              },
            })
          }
          placeholder="gpt-4o-mini"
          className={inputClasses}
        />
      </Field>
      <Checkbox
        label="Run in parallel"
        checked={node.is_parallel}
        onChange={(v) => onPatch({ is_parallel: v })}
      />
    </section>
  );
}

function TriggerFields({
  node,
  onPatch,
}: {
  node: TriggerNode;
  onPatch: (p: Partial<TriggerNode>) => void;
}) {
  return (
    <section className="flex flex-col gap-2">
      <Field label="Trigger type">
        <select
          value={node.trigger_type}
          onChange={(e) => onPatch({ trigger_type: e.target.value as TriggerNode["trigger_type"] })}
          className={inputClasses}
        >
          <option value="manual">Manual</option>
          <option value="webhook">Webhook</option>
          <option value="form">Form</option>
          <option value="schedule">Schedule</option>
          <option value="chat">Chat</option>
        </select>
      </Field>
      <Field label="Slug" hint="URL slug used by webhook/form/chat routes.">
        <input
          value={node.slug}
          onChange={(e) => onPatch({ slug: e.target.value })}
          className={inputClasses}
        />
      </Field>
      {node.trigger_type === "schedule" ? (
        <Field label="Cron expression">
          <input
            value={node.schedule_cron}
            onChange={(e) => onPatch({ schedule_cron: e.target.value })}
            placeholder="0 9 * * *"
            className={inputClasses}
          />
        </Field>
      ) : null}
      {node.trigger_type === "chat" ? (
        <>
          <Field label="Welcome message" hint="First message users see in the chat panel.">
            <textarea
              value={node.chat_welcome_message}
              onChange={(e) => onPatch({ chat_welcome_message: e.target.value })}
              rows={2}
              className={textareaClasses}
            />
          </Field>
          <Field label="Chat memory node id">
            <input
              value={node.chat_memory_ref}
              onChange={(e) => onPatch({ chat_memory_ref: e.target.value })}
              placeholder="memory_node_id"
              className={inputClasses}
            />
          </Field>
        </>
      ) : null}
      <Checkbox
        label="Require shared secret"
        checked={node.secret_required}
        onChange={(v) => onPatch({ secret_required: v })}
      />
    </section>
  );
}

function ActionFields({
  node,
  onPatch,
}: {
  node: ActionNode;
  onPatch: (p: Partial<ActionNode>) => void;
}) {
  const workspaceId = useAuthStore((s) => s.workspaceId);
  const [catalog, setCatalog] = useState<NativeProviderCatalogEntry[]>([]);
  const [connections, setConnections] = useState<NativeConnectionResponse[]>([]);
  const [loadingConn, setLoadingConn] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [apiFormOpen, setApiFormOpen] = useState(false);
  const [apiFormValues, setApiFormValues] = useState<Record<string, string>>({});
  const [savingApi, setSavingApi] = useState(false);

  const loadConnections = useCallback(async () => {
    if (!workspaceId) return;
    setLoadingConn(true);
    try {
      const [catRes, connRes] = await Promise.all([
        api.get<NativeProviderCatalogResponse>("/api/v1/connections/providers"),
        api.get<NativeConnectionResponse[]>(
          `/api/v1/connections?workspace_id=${workspaceId}`,
        ),
      ]);
      setCatalog(catRes.data.providers);
      setConnections(connRes.data);
    } catch {
      // Silent — the property inspector should still be usable without
      // connection data. The user sees a free-text fallback below.
    } finally {
      setLoadingConn(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    void loadConnections();
  }, [loadConnections]);

  const providerEntry = useMemo(
    () => catalog.find((p) => p.id === node.provider) ?? null,
    [catalog, node.provider],
  );
  const providerConnections = useMemo(
    () => connections.filter((c) => c.provider === node.provider && c.status === "active"),
    [connections, node.provider],
  );
  const activeConnection = useMemo(
    () =>
      providerConnections.find((c) => c.id === node.connection_id) ??
      providerConnections[0] ??
      null,
    [providerConnections, node.connection_id],
  );

  const isOAuth = (providerEntry?.auth_type ?? "").toLowerCase().includes("oauth");

  const handleConnect = useCallback(async () => {
    if (!workspaceId || !providerEntry) return;
    setConnecting(true);
    try {
      const result = await startInlineOAuth({
        provider: providerEntry.id,
        workspaceId,
        connectionName: providerEntry.name,
      });
      if (result.ok) {
        toast.success(`Connected to ${providerEntry.name}.`);
        await loadConnections();
      } else {
        toast.error(result.message || "OAuth was not completed.");
      }
    } finally {
      setConnecting(false);
    }
  }, [loadConnections, providerEntry, workspaceId]);

  // Inline API-key connect — create the connection right here on the node,
  // then test it, without leaving the canvas (n8n-style credentials on node).
  const submitApiKey = useCallback(async () => {
    if (!workspaceId || !providerEntry) return;
    setSavingApi(true);
    try {
      const res = await api.post<NativeConnectionResponse>(
        `/api/v1/connections?workspace_id=${workspaceId}`,
        { provider: providerEntry.id, name: providerEntry.name, config: apiFormValues },
      );
      // Best-effort connectivity test; don't block on its result.
      try {
        await api.post(
          `/api/v1/connections/${res.data.id}/test?workspace_id=${workspaceId}`,
        );
      } catch {
        /* surfaced via the connection's last_test_error on reload */
      }
      toast.success(`Connected to ${providerEntry.name}.`);
      setApiFormOpen(false);
      setApiFormValues({});
      await loadConnections();
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Could not save the connection.";
      toast.error(detail);
    } finally {
      setSavingApi(false);
    }
  }, [workspaceId, providerEntry, apiFormValues, loadConnections]);

  return (
    <section className="flex flex-col gap-2">
      <Field label="Provider">
        {catalog.length > 0 ? (
          <select
            value={node.provider}
            onChange={(e) =>
              onPatch({ provider: e.target.value, action_slug: "" })
            }
            className={inputClasses}
          >
            <option value="">— select a provider —</option>
            {catalog.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
            {node.provider && !catalog.some((p) => p.id === node.provider) ? (
              <option value={node.provider}>{node.provider} (custom)</option>
            ) : null}
          </select>
        ) : (
          <input
            value={node.provider}
            onChange={(e) => onPatch({ provider: e.target.value })}
            placeholder="gmail / slack / http_bearer"
            className={inputClasses}
          />
        )}
      </Field>

      {node.provider ? (
        <ConnectionStatusRow
          loading={loadingConn}
          connection={activeConnection}
          providerEntry={providerEntry}
          isOAuth={isOAuth}
          connecting={connecting}
          onConnect={handleConnect}
          onConnectApiKey={() => setApiFormOpen((v) => !v)}
        />
      ) : null}

      {apiFormOpen && providerEntry && !isOAuth ? (
        <div className="flex flex-col gap-2 rounded-xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-700 dark:bg-slate-900">
          <p className="text-[11px] font-semibold text-slate-700 dark:text-slate-200">
            Connect {providerEntry.name}
          </p>
          {providerEntry.fields.map((f) => (
            <label key={f.key} className="flex flex-col gap-1">
              <span className="text-[10px] font-medium text-slate-500 dark:text-slate-400">
                {f.label}
                {f.required ? " *" : ""}
              </span>
              <input
                type={f.type === "secret" ? "password" : "text"}
                value={apiFormValues[f.key] ?? ""}
                placeholder={f.placeholder ?? ""}
                onChange={(e) =>
                  setApiFormValues((v) => ({ ...v, [f.key]: e.target.value }))
                }
                className={inputClasses}
              />
              {f.help_text ? (
                <span className="text-[10px] text-slate-400">{f.help_text}</span>
              ) : null}
            </label>
          ))}
          <div className="flex items-center justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={() => setApiFormOpen(false)}
              className="text-[11px] text-slate-500 hover:text-slate-700 dark:text-slate-400"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void submitApiKey()}
              disabled={savingApi}
              className="inline-flex items-center gap-1 rounded-lg bg-brand-600 px-3 py-1 text-[11px] font-semibold text-white disabled:opacity-50"
            >
              {savingApi ? <Loader2 className="h-3 w-3 animate-spin" /> : <PlugZap className="h-3 w-3" />}
              {savingApi ? "Connecting…" : "Connect & test"}
            </button>
          </div>
        </div>
      ) : null}

      {providerConnections.length > 1 ? (
        <Field label="Account" hint="This provider has multiple connections — pick which one this node uses.">
          <select
            value={node.connection_id ?? ""}
            onChange={(e) => onPatch({ connection_id: e.target.value || null })}
            className={inputClasses}
          >
            <option value="">Auto (first active: {providerConnections[0].name})</option>
            {providerConnections.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </Field>
      ) : null}

      <Field label="Action">
        {providerEntry && providerEntry.tool_slugs.length > 0 ? (
          <select
            value={node.action_slug}
            onChange={(e) => onPatch({ action_slug: e.target.value })}
            className={inputClasses}
            disabled={!activeConnection && !node.allow_dry_run}
          >
            <option value="">— select an action —</option>
            {providerEntry.tool_slugs.map((slug) => (
              <option key={slug} value={slug}>
                {slug}
              </option>
            ))}
            {node.action_slug &&
            !providerEntry.tool_slugs.includes(node.action_slug) ? (
              <option value={node.action_slug}>{node.action_slug} (custom)</option>
            ) : null}
          </select>
        ) : (
          <input
            value={node.action_slug}
            onChange={(e) => onPatch({ action_slug: e.target.value })}
            placeholder="slack_send_message"
            className={inputClasses}
          />
        )}
      </Field>
      <Field label="Tool description" hint="Shown to the parent agent's LLM when this is a satellite.">
        <textarea
          value={node.tool_description ?? ""}
          onChange={(e) => onPatch({ tool_description: e.target.value })}
          rows={2}
          className={textareaClasses}
        />
      </Field>
      <JsonField
        label="Params"
        value={node.params}
        onChange={(v) => onPatch({ params: v as Record<string, unknown> })}
      />
      <Field label="Parent agent" hint="Set when this action is a satellite tool of an agent.">
        <input
          value={node.parent_agent_id ?? ""}
          onChange={(e) => onPatch({ parent_agent_id: e.target.value || null })}
          placeholder="agent_id (optional)"
          className={inputClasses}
        />
      </Field>
      <Checkbox
        label="Allow dry-run if no connection"
        checked={node.allow_dry_run}
        onChange={(v) => onPatch({ allow_dry_run: v })}
      />
    </section>
  );
}

function ConnectionStatusRow({
  loading,
  connection,
  providerEntry,
  isOAuth,
  connecting,
  onConnect,
  onConnectApiKey,
}: {
  loading: boolean;
  connection: NativeConnectionResponse | null;
  providerEntry: NativeProviderCatalogEntry | null;
  isOAuth: boolean;
  connecting: boolean;
  onConnect: () => void;
  onConnectApiKey: () => void;
}) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-slate-200 px-3 py-2 text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Checking connection…
      </div>
    );
  }
  if (connection) {
    return (
      <div className="flex items-center justify-between gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs dark:border-emerald-900 dark:bg-emerald-950">
        <span className="inline-flex items-center gap-1.5 text-emerald-900 dark:text-emerald-100">
          <CheckCircle2 className="h-3.5 w-3.5" />
          Connected as <strong>{connection.name}</strong>
        </span>
        {isOAuth ? (
          <button
            type="button"
            onClick={onConnect}
            disabled={connecting}
            className="text-[11px] font-medium text-emerald-900 underline disabled:opacity-50 dark:text-emerald-100"
          >
            {connecting ? "Opening…" : "Reconnect"}
          </button>
        ) : (
          <button
            type="button"
            onClick={onConnectApiKey}
            className="text-[11px] font-medium text-emerald-900 underline dark:text-emerald-100"
          >
            Update
          </button>
        )}
      </div>
    );
  }
  // Not connected.
  if (!providerEntry) {
    return (
      <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100">
        Unknown provider — check spelling, or add a connection in{" "}
        <Link href="/integrations" className="underline">
          Integrations
        </Link>
        .
      </div>
    );
  }
  return (
    <div className="flex items-center justify-between gap-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs dark:border-amber-900 dark:bg-amber-950">
      <span className="text-amber-900 dark:text-amber-100">
        Not connected to <strong>{providerEntry.name}</strong> yet.
      </span>
      {isOAuth ? (
        <button
          type="button"
          onClick={onConnect}
          disabled={connecting}
          className="inline-flex items-center gap-1 rounded-lg bg-brand-600 px-3 py-1 text-[11px] font-semibold text-white disabled:opacity-50"
        >
          {connecting ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <PlugZap className="h-3 w-3" />
          )}
          {connecting ? "Opening…" : `Connect ${providerEntry.name}`}
        </button>
      ) : (
        <button
          type="button"
          onClick={onConnectApiKey}
          className="inline-flex items-center gap-1 rounded-lg bg-brand-600 px-3 py-1 text-[11px] font-semibold text-white"
        >
          <PlugZap className="h-3 w-3" />
          Connect {providerEntry.name}
        </button>
      )}
    </div>
  );
}

function ConditionFields({
  node,
  onPatch,
}: {
  node: ConditionNode;
  onPatch: (p: Partial<ConditionNode>) => void;
}) {
  return (
    <section className="flex flex-col gap-2">
      <Field label="Expression" hint="Natural-language predicate or rubric.">
        <textarea
          value={node.expression}
          onChange={(e) => onPatch({ expression: e.target.value })}
          rows={3}
          className={textareaClasses}
        />
      </Field>
      <Field label="Branches" hint="Comma-separated labels (2-8).">
        <input
          value={node.branches.join(", ")}
          onChange={(e) =>
            onPatch({
              branches: e.target.value
                .split(",")
                .map((s) => s.trim())
                .filter((s) => s.length > 0),
            })
          }
          className={inputClasses}
        />
      </Field>
    </section>
  );
}

function IfFields({
  node,
  onPatch,
}: {
  node: IfNode;
  onPatch: (p: Partial<IfNode>) => void;
}) {
  return (
    <Field label="Expression" hint="e.g. $.lookup.score > 75">
      <textarea
        value={node.expression}
        onChange={(e) => onPatch({ expression: e.target.value })}
        rows={2}
        className={textareaClasses}
      />
    </Field>
  );
}

function ForEachFields({
  node,
  definition,
  onPatch,
}: {
  node: ForEachNode;
  definition: PropertyInspectorProps["state"]["definition"];
  onPatch: (p: Partial<ForEachNode>) => void;
}) {
  const upstreamOptions = allNodes(definition).filter((n) => n.id !== node.id);
  return (
    <section className="flex flex-col gap-2">
      <Field label="Items from" hint="Upstream node producing a list.">
        <select
          value={node.items_from}
          onChange={(e) => onPatch({ items_from: e.target.value })}
          className={inputClasses}
        >
          <option value="">— pick a node —</option>
          {upstreamOptions.map((n) => (
            <option key={n.id} value={n.id}>
              {n.name} ({n.id})
            </option>
          ))}
        </select>
      </Field>
      <Field label="Items JSONPath">
        <input
          value={node.items_path}
          onChange={(e) => onPatch({ items_path: e.target.value })}
          placeholder="$"
          className={inputClasses}
        />
      </Field>
      <Field label="Iterator variable">
        <input
          value={node.item_var}
          onChange={(e) => onPatch({ item_var: e.target.value })}
          className={inputClasses}
        />
      </Field>
      <Field label="Max concurrency">
        <input
          type="number"
          min={1}
          max={32}
          value={node.max_concurrency}
          onChange={(e) =>
            onPatch({ max_concurrency: Math.max(1, Math.min(32, Number(e.target.value) || 1)) })
          }
          className={inputClasses}
        />
      </Field>
      <Field label="Body node ids" hint="Comma-separated; nodes that run per item.">
        <input
          value={node.body.join(", ")}
          onChange={(e) =>
            onPatch({
              body: e.target.value
                .split(",")
                .map((s) => s.trim())
                .filter((s) => s.length > 0),
            })
          }
          className={inputClasses}
        />
      </Field>
    </section>
  );
}

function WaitFields({
  node,
  onPatch,
}: {
  node: WaitForWebhookNode;
  onPatch: (p: Partial<WaitForWebhookNode>) => void;
}) {
  return (
    <section className="flex flex-col gap-2">
      <Field label="Description">
        <textarea
          value={node.description}
          onChange={(e) => onPatch({ description: e.target.value })}
          rows={2}
          className={textareaClasses}
        />
      </Field>
      <Field label="Timeout (seconds)">
        <input
          type="number"
          min={30}
          value={node.timeout_seconds}
          onChange={(e) => onPatch({ timeout_seconds: Math.max(30, Number(e.target.value) || 30) })}
          className={inputClasses}
        />
      </Field>
    </section>
  );
}

function DataStoreFields({
  node,
  onPatch,
}: {
  node: DataStoreNode;
  onPatch: (p: Partial<DataStoreNode>) => void;
}) {
  return (
    <section className="flex flex-col gap-2">
      <Field label="Operation">
        <select
          value={node.op}
          onChange={(e) => onPatch({ op: e.target.value as DataStoreNode["op"] })}
          className={inputClasses}
        >
          <option value="write">Write</option>
          <option value="read">Read</option>
          <option value="query">Query</option>
        </select>
      </Field>
      <Field label="Table">
        <input
          value={node.table}
          onChange={(e) => onPatch({ table: e.target.value })}
          className={inputClasses}
        />
      </Field>
      <Field label="Key">
        <input
          value={node.key}
          onChange={(e) => onPatch({ key: e.target.value })}
          placeholder='{{ upstream.email }}'
          className={inputClasses}
        />
      </Field>
      {node.op === "write" ? (
        <JsonField
          label="Payload"
          value={node.payload}
          onChange={(v) => onPatch({ payload: v as Record<string, unknown> })}
        />
      ) : null}
      {node.op === "query" ? (
        <JsonField
          label="Filter"
          value={node.filter}
          onChange={(v) => onPatch({ filter: v as Record<string, unknown> })}
        />
      ) : null}
    </section>
  );
}

function MemoryFields({
  node,
  onPatch,
}: {
  node: MemoryNode;
  onPatch: (p: Partial<MemoryNode>) => void;
}) {
  return (
    <section className="flex flex-col gap-2">
      <Field label="Scope">
        <select
          value={node.scope}
          onChange={(e) => onPatch({ scope: e.target.value as MemoryNode["scope"] })}
          className={inputClasses}
        >
          <option value="session">Session</option>
          <option value="user">User</option>
          <option value="workflow">Workflow</option>
        </select>
      </Field>
      <Field label="Store">
        <select
          value={node.store}
          onChange={(e) => onPatch({ store: e.target.value as MemoryNode["store"] })}
          className={inputClasses}
        >
          <option value="redis">Redis</option>
          <option value="postgres">Postgres</option>
        </select>
      </Field>
      <Field label="TTL (seconds)">
        <input
          type="number"
          min={60}
          value={node.ttl_seconds}
          onChange={(e) => onPatch({ ttl_seconds: Math.max(60, Number(e.target.value) || 60) })}
          className={inputClasses}
        />
      </Field>
      <Field label="Max turns">
        <input
          type="number"
          min={1}
          max={512}
          value={node.max_turns}
          onChange={(e) => onPatch({ max_turns: Math.max(1, Math.min(512, Number(e.target.value) || 1)) })}
          className={inputClasses}
        />
      </Field>
    </section>
  );
}

function OutputParserFields({
  node,
  onPatch,
}: {
  node: OutputParserNode;
  onPatch: (p: Partial<OutputParserNode>) => void;
}) {
  return (
    <section className="flex flex-col gap-2">
      <JsonField
        label="JSON schema"
        value={node.json_schema}
        onChange={(v) => onPatch({ json_schema: v as Record<string, unknown> })}
      />
      <Field label="Max retries">
        <input
          type="number"
          min={0}
          max={5}
          value={node.max_retries}
          onChange={(e) => onPatch({ max_retries: Math.max(0, Math.min(5, Number(e.target.value) || 0)) })}
          className={inputClasses}
        />
      </Field>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Reusable form primitives
// ---------------------------------------------------------------------------

const inputClasses =
  "w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-[12px] text-slate-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100";

const textareaClasses =
  "w-full resize-y rounded-md border border-slate-300 bg-white px-2 py-1.5 text-[12px] leading-snug text-slate-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100";

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
        {label}
      </span>
      {children}
      {hint ? (
        <span className="text-[10px] leading-tight text-slate-500 dark:text-slate-500">
          {hint}
        </span>
      ) : null}
    </label>
  );
}

function Checkbox({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-2 text-[12px] text-slate-700 dark:text-slate-300">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="h-3.5 w-3.5 rounded border-slate-300 dark:border-slate-700"
      />
      {label}
    </label>
  );
}

function JsonField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const [draft, setDraft] = useState(() => formatJson(value));
  const [error, setError] = useState<string | null>(null);

  // Re-sync when the underlying value changes (e.g. selection change).
  useEffect(() => {
    setDraft(formatJson(value));
    setError(null);
  }, [value]);

  return (
    <Field label={label} hint='Valid JSON. Supports {{ upstream.path }} placeholders inside string values.'>
      <textarea
        value={draft}
        onChange={(e) => {
          const next = e.target.value;
          setDraft(next);
          if (next.trim() === "") {
            setError(null);
            onChange({});
            return;
          }
          try {
            const parsed = JSON.parse(next);
            setError(null);
            onChange(parsed);
          } catch (err) {
            setError(err instanceof Error ? err.message : "Invalid JSON");
          }
        }}
        rows={5}
        spellCheck={false}
        className={cn(
          textareaClasses,
          "font-mono",
          error && "border-rose-500 focus:border-rose-500 focus:ring-rose-500",
        )}
      />
      {error ? (
        <span className="text-[10px] text-rose-600 dark:text-rose-400">{error}</span>
      ) : null}
    </Field>
  );
}

function formatJson(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "string") return v;
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return "";
  }
}
