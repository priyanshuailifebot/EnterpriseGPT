"use client";

/**
 * List + paginate chat sessions for a workspace (and optionally a
 * specific workflow). Backed by ``GET /api/v1/chat/sessions``.
 *
 * Separate from ``useChatSession`` (singular) which drives ONE session's
 * streaming lifecycle. This hook only reads metadata.
 */

import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";

export interface SessionSummary {
  id: string;
  workspace_id: string;
  workflow_id: string;
  trigger_slug: string;
  agent_node_id: string;
  status: string;
  total_messages: number;
  total_cost_cents: number;
  last_activity_at: string | null;
  created_at: string;
}

interface ListResponse {
  items: SessionSummary[];
  total: number;
}

export function useChatSessions(opts: {
  workspaceId: string | null;
  workflowId?: string | null;
  pageSize?: number;
  enabled?: boolean;
}) {
  const { workspaceId, workflowId, pageSize = 20 } = opts;
  const enabled = opts.enabled ?? true;

  const [items, setItems] = useState<SessionSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!enabled || !workspaceId) return;
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({
        workspace_id: workspaceId,
        page: "1",
        page_size: String(pageSize),
      });
      if (workflowId) params.set("workflow_id", workflowId);
      const { data } = await api.get<ListResponse>(
        `/api/v1/chat/sessions?${params.toString()}`,
      );
      setItems(data.items);
      setTotal(data.total);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "failed to load sessions");
    } finally {
      setLoading(false);
    }
  }, [enabled, workspaceId, workflowId, pageSize]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { items, total, loading, error, refresh };
}
