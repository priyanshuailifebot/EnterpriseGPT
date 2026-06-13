"use client";

import {
  CheckCircle2,
  Loader2,
  PlugZap,
  Trash2,
  XCircle,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import toast from "react-hot-toast";
import axios from "axios";

import { api, getErrorMessage } from "@/lib/api";
import type {
  MCPServerCreateRequest,
  MCPServerResponse,
  MCPServerTestResponse,
  MCPTransport,
} from "@/types/api";

type Props = {
  workspaceId: string;
  hasManage: boolean;
};

const DEFAULT_FORM: MCPServerCreateRequest = {
  name: "Composio",
  url: "https://connect.composio.dev/mcp",
  transport: "streamable-http",
  auth_header_name: "X-CONSUMER-API-KEY",
  auth_header_value: "",
  extra_headers: {},
};

export function MCPServersPanel({ workspaceId, hasManage }: Props) {
  const [rows, setRows] = useState<MCPServerResponse[]>([]);
  const [loading, setLoading] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<MCPServerCreateRequest>(DEFAULT_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [testingId, setTestingId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await api.get<MCPServerResponse[]>(
        `/api/v1/mcp-servers?workspace_id=${workspaceId}`,
      );
      setRows(data);
    } catch (e) {
      toast.error(
        axios.isAxiosError(e) ? getErrorMessage(e) : "Failed to load MCP servers.",
      );
    } finally {
      setLoading(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const create = useCallback(async () => {
    if (!form.name.trim() || !form.url.trim() || !form.auth_header_value.trim()) {
      toast.error("Name, URL, and auth header value are required.");
      return;
    }
    setSubmitting(true);
    try {
      await api.post<MCPServerResponse>(
        `/api/v1/mcp-servers?workspace_id=${workspaceId}`,
        form,
      );
      toast.success("MCP server saved.");
      setShowForm(false);
      setForm(DEFAULT_FORM);
      await refresh();
    } catch (e) {
      toast.error(axios.isAxiosError(e) ? getErrorMessage(e) : "Could not save.");
    } finally {
      setSubmitting(false);
    }
  }, [form, refresh, workspaceId]);

  const test = useCallback(
    async (row: MCPServerResponse) => {
      setTestingId(row.id);
      try {
        const { data } = await api.post<MCPServerTestResponse>(
          `/api/v1/mcp-servers/${row.id}/test?workspace_id=${workspaceId}`,
        );
        if (data.success) {
          toast.success(
            `${row.name}: ${data.message}${
              data.sample_tool_names.length > 0
                ? ` (e.g. ${data.sample_tool_names.slice(0, 3).join(", ")})`
                : ""
            }`,
          );
        } else {
          toast.error(`${row.name}: ${data.message}`);
        }
        await refresh();
      } catch (e) {
        toast.error(axios.isAxiosError(e) ? getErrorMessage(e) : "Test failed.");
      } finally {
        setTestingId(null);
      }
    },
    [refresh, workspaceId],
  );

  const remove = useCallback(
    async (row: MCPServerResponse) => {
      if (!confirm(`Delete MCP server "${row.name}"?`)) return;
      try {
        await api.delete(
          `/api/v1/mcp-servers/${row.id}?workspace_id=${workspaceId}`,
        );
        toast.success("Deleted.");
        await refresh();
      } catch (e) {
        toast.error(axios.isAxiosError(e) ? getErrorMessage(e) : "Delete failed.");
      }
    },
    [refresh, workspaceId],
  );

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-slate-900 dark:text-slate-50">
            MCP servers
          </h2>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            Connect a Model Context Protocol endpoint (Composio, self-hosted, or
            anything else that speaks MCP). Tools advertised by these servers
            appear in the agent toolbelt automatically.
          </p>
        </div>
        {hasManage ? (
          <button
            type="button"
            onClick={() => setShowForm((v) => !v)}
            className="inline-flex items-center gap-1.5 rounded-xl bg-brand-600 px-3 py-1.5 text-xs font-semibold text-white"
          >
            <PlugZap className="h-3.5 w-3.5" />
            {showForm ? "Cancel" : "Add MCP server"}
          </button>
        ) : null}
      </div>

      {showForm && hasManage ? (
        <div className="space-y-3 rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
          <FormField label="Name">
            <input
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
              className={inputCls}
              placeholder="Composio"
            />
          </FormField>
          <FormField label="URL">
            <input
              value={form.url}
              onChange={(e) => setForm((f) => ({ ...f, url: e.target.value }))}
              className={inputCls}
              placeholder="https://connect.composio.dev/mcp"
            />
          </FormField>
          <FormField label="Transport">
            <select
              value={form.transport}
              onChange={(e) =>
                setForm((f) => ({
                  ...f,
                  transport: e.target.value as MCPTransport,
                }))
              }
              className={inputCls}
            >
              <option value="streamable-http">streamable-http (newer)</option>
              <option value="sse">sse (legacy)</option>
            </select>
          </FormField>
          <div className="grid grid-cols-2 gap-3">
            <FormField label="Auth header name">
              <input
                value={form.auth_header_name}
                onChange={(e) =>
                  setForm((f) => ({ ...f, auth_header_name: e.target.value }))
                }
                className={inputCls}
                placeholder="X-CONSUMER-API-KEY"
              />
            </FormField>
            <FormField label="Auth header value">
              <input
                type="password"
                value={form.auth_header_value}
                onChange={(e) =>
                  setForm((f) => ({ ...f, auth_header_value: e.target.value }))
                }
                className={inputCls}
                placeholder="ck_..."
              />
            </FormField>
          </div>
          <div className="flex justify-end">
            <button
              type="button"
              onClick={() => void create()}
              disabled={submitting}
              className="inline-flex items-center gap-1.5 rounded-xl bg-brand-600 px-4 py-2 text-xs font-semibold text-white disabled:opacity-50"
            >
              {submitting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
              Save & test
            </button>
          </div>
        </div>
      ) : null}

      {loading ? (
        <p className="text-xs text-slate-500">Loading MCP servers…</p>
      ) : rows.length === 0 ? (
        <p className="rounded-2xl border border-dashed border-slate-300 p-4 text-xs text-slate-500 dark:border-slate-700">
          No MCP servers registered. Add one to expose external tools to your
          agents.
        </p>
      ) : (
        <ul className="space-y-2">
          {rows.map((row) => (
            <li
              key={row.id}
              className="flex flex-wrap items-center gap-3 rounded-2xl border border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-900"
            >
              <StatusPill status={row.status} />
              <div className="flex-1 min-w-[200px]">
                <p className="text-sm font-medium text-slate-900 dark:text-slate-50">
                  {row.name}
                </p>
                <p className="truncate text-[11px] text-slate-500 dark:text-slate-400">
                  {row.url} · {row.transport}
                  {row.last_tool_count !== null
                    ? ` · ${row.last_tool_count} tool(s)`
                    : ""}
                </p>
                {row.last_test_error ? (
                  <p className="mt-0.5 truncate text-[11px] text-red-600 dark:text-red-400">
                    {row.last_test_error}
                  </p>
                ) : null}
              </div>
              {hasManage ? (
                <>
                  <button
                    type="button"
                    onClick={() => void test(row)}
                    disabled={testingId === row.id}
                    className="rounded-lg border border-slate-200 px-3 py-1 text-xs dark:border-slate-700"
                  >
                    {testingId === row.id ? "Testing…" : "Test"}
                  </button>
                  <button
                    type="button"
                    onClick={() => void remove(row)}
                    className="rounded-lg p-1.5 text-red-600 hover:bg-red-50 dark:hover:bg-red-950"
                    aria-label="Delete server"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function StatusPill({ status }: { status: string }) {
  if (status === "active") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] font-semibold text-emerald-800 dark:bg-emerald-950 dark:text-emerald-100">
        <CheckCircle2 className="h-3 w-3" />
        active
      </span>
    );
  }
  if (status === "error") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-red-50 px-2 py-0.5 text-[10px] font-semibold text-red-800 dark:bg-red-950 dark:text-red-100">
        <XCircle className="h-3 w-3" />
        error
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-700 dark:bg-slate-800 dark:text-slate-200">
      {status}
    </span>
  );
}

function FormField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block text-xs">
      <span className="font-medium text-slate-700 dark:text-slate-200">{label}</span>
      <div className="mt-1">{children}</div>
    </label>
  );
}

const inputCls =
  "w-full rounded-xl border border-slate-200 bg-transparent px-3 py-2 text-xs dark:border-slate-700";
