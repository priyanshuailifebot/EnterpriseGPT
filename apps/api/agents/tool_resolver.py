"""Resolve an AgentNode's satellites into OpenAI-shaped tools + handlers.

The chat runtime hands the resulting list to the LLM in two parts:

1. ``specs`` — the function-calling tool descriptors (name + description +
   JSON-Schema params) the LLM sees in its prompt.
2. ``handlers`` — a parallel dict ``{tool_name → async invoker}`` the
   runtime calls when the LLM requests a tool.

Two satellite kinds are supported in Phase 2a:

* ``ActionNode``     — invoked via ``ActionRunner.invoke_action``
* ``DataStoreNode``  — invoked against the workspace's ``workflow_data``
                       table directly (mirrors what
                       ``extended_executor._data_store_op`` does, but
                       keyed off ``payload`` / ``filter`` supplied by the
                       LLM at tool-call time rather than from the static
                       node config — that's the whole point of letting an
                       agent decide what to write).

The resolver does NOT touch unused satellite kinds (memory, output_parser)
— those have their own runtime paths.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from agents.action_runner import invoke_action, render_placeholders
from models.native_connection import NativeConnection
from schemas.workflow import ActionNode, AgentNode, DataStoreNode, WorkflowDefinition

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Public shape
# ---------------------------------------------------------------------------


ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class ResolvedTool:
    name: str
    spec: dict[str, Any]            # OpenAI ``tools[]`` entry
    invoke: ToolHandler


@dataclass
class ResolvedToolset:
    tools: list[ResolvedTool]

    def specs(self) -> list[dict[str, Any]]:
        return [t.spec for t in self.tools]

    def handlers(self) -> dict[str, ToolHandler]:
        return {t.name: t.invoke for t in self.tools}


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _with_timeout_and_retry(
    invoker: ToolHandler,
    *,
    timeout_ms: int,
    max_retries: int,
    initial_delay_ms: int,
    label: str,
) -> ToolHandler:
    """Wrap a tool handler with per-call timeout + exponential-backoff retry.

    Failure modes the runtime needs to keep separate at the wire format
    level:
      * ``timeout``  — the tool didn't return within ``timeout_ms``. We
                       cancel the underlying task so it doesn't keep
                       hogging a connection.
      * ``failed``   — exception inside the tool (network error, JSON
                       parse, etc.). Retried up to ``max_retries`` times.
      * ``ok``       — pass-through.

    Each retry waits ``initial_delay_ms * 2^attempt`` milliseconds. We
    cap the backoff at 10 s so a high ``max_retries`` doesn't hang the
    agent's tool loop for tens of seconds.
    """

    timeout_seconds = max(0.1, timeout_ms / 1000.0)
    initial_delay = max(0.0, initial_delay_ms / 1000.0)

    async def wrapped(args: dict[str, Any]) -> dict[str, Any]:
        last_error: dict[str, Any] | None = None
        for attempt in range(max_retries + 1):
            try:
                result = await asyncio.wait_for(invoker(args), timeout=timeout_seconds)
                # Treat structured ``{ok: false}`` results as retryable on
                # all but the last attempt — the upstream often signals
                # a transient failure that way.
                is_explicit_failure = (
                    isinstance(result, dict)
                    and result.get("ok") is False
                    and not result.get("__dry_run__")
                )
                if is_explicit_failure and attempt < max_retries:
                    last_error = result
                    await asyncio.sleep(min(10.0, initial_delay * (2**attempt)))
                    continue
                return result if isinstance(result, dict) else {"ok": True, "value": result}
            except asyncio.TimeoutError:
                last_error = {
                    "ok": False,
                    "error": f"timeout after {timeout_ms} ms",
                    "code": "timeout",
                    "tool": label,
                    "attempt": attempt + 1,
                }
                if attempt < max_retries:
                    log.info(
                        "tool_resolver.timeout_retrying",
                        tool=label,
                        attempt=attempt + 1,
                    )
                    await asyncio.sleep(min(10.0, initial_delay * (2**attempt)))
                    continue
                return last_error
            except Exception as exc:  # noqa: BLE001 — propagate to the LLM
                last_error = {
                    "ok": False,
                    "error": str(exc),
                    "code": "exception",
                    "tool": label,
                    "attempt": attempt + 1,
                }
                if attempt < max_retries:
                    log.info(
                        "tool_resolver.exception_retrying",
                        tool=label,
                        attempt=attempt + 1,
                        error=str(exc),
                    )
                    await asyncio.sleep(min(10.0, initial_delay * (2**attempt)))
                    continue
                return last_error
        # Unreachable; loop above always returns. Defensive only.
        return last_error or {"ok": False, "error": "unknown_error", "tool": label}

    return wrapped


def _sanitise_tool_name(raw: str) -> str:
    """OpenAI requires ``^[a-zA-Z0-9_-]{1,64}$``. Our slugs already match
    that — keep this guard defensive so a malformed slug fails fast."""
    cleaned = "".join(c for c in raw if c.isalnum() or c in "_-")
    return (cleaned[:64] or "tool").lower()


def _data_store_param_schema(node: DataStoreNode) -> dict[str, Any]:
    """JSON-Schema parameters the LLM is allowed to supply at call time.

    Different ops accept different fields. We expose only what makes sense:
    a write/read takes a ``key`` + ``payload`` / no payload, query takes a
    ``filter``. The agent's instructions already document which to call.
    """
    if node.op == "write":
        return {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Row key (optional)"},
                "payload": {
                    "type": "object",
                    "description": (
                        "Row data to upsert. Merged with existing data when the key already exists."
                    ),
                },
            },
            "additionalProperties": False,
        }
    if node.op == "read":
        return {
            "type": "object",
            "required": ["key"],
            "properties": {
                "key": {"type": "string", "description": "Row key to read"},
            },
            "additionalProperties": False,
        }
    # query
    return {
        "type": "object",
        "properties": {
            "filter": {
                "type": "object",
                "description": "Exact-match filter on row data fields",
            },
        },
        "additionalProperties": False,
    }


def _action_param_schema(node: ActionNode) -> dict[str, Any]:
    """For ActionNodes the params shape is provider-specific and the agent
    is told what to send via ``tool_description``. We accept any object so
    the LLM can pass through whatever the upstream API expects."""
    return {
        "type": "object",
        "description": (
            "Parameters for the underlying integration call. Match the "
            "fields described in this tool's description."
        ),
        "additionalProperties": True,
    }


def build_toolset(
    *,
    workflow_definition: WorkflowDefinition,
    agent: AgentNode,
    workspace_connections: list[NativeConnection],
    workspace_id: UUID,
    workflow_id: UUID,
    execution_id: UUID | None = None,
    db: AsyncSession | None = None,
) -> ResolvedToolset:
    """Walk the satellites attached to ``agent`` and produce a callable toolset."""
    from schemas.workflow import satellites_by_agent

    sats = satellites_by_agent(workflow_definition).get(agent.id, [])
    tools: list[ResolvedTool] = []
    for sat in sats:
        if isinstance(sat, ActionNode):
            tools.append(
                _wrap_action(
                    sat,
                    workspace_connections=workspace_connections,
                    workspace_id=workspace_id,
                    db=db,
                )
            )
        elif isinstance(sat, DataStoreNode):
            tools.append(
                _wrap_data_store(
                    sat,
                    workspace_id=workspace_id,
                    workflow_id=workflow_id,
                    execution_id=execution_id,
                    db=db,
                )
            )
        # Memory + OutputParser satellites are NOT advertised as tools —
        # they have their own runtime paths (MemoryStore / OutputParser
        # service). Skip silently.

    return ResolvedToolset(tools=tools)


# ---------------------------------------------------------------------------
# Internals — wrappers
# ---------------------------------------------------------------------------


def _wrap_action(
    node: ActionNode,
    *,
    workspace_connections: list[NativeConnection],
    workspace_id: UUID | None = None,
    db: AsyncSession | None = None,
) -> ResolvedTool:
    name = _sanitise_tool_name(node.action_slug or node.id)
    description = (
        node.tool_description.strip()
        or f"{node.provider}: {node.action_slug}"
    )
    spec = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": _action_param_schema(node),
        },
    }

    async def invoke(args: dict[str, Any]) -> dict[str, Any]:
        # Merge the static params declared on the node (with placeholder
        # substitution against the args the LLM provided) with the args
        # themselves so the runner sees a single dict.
        rendered_static = render_placeholders(node.params, {"input": args, **args})
        merged = {**(rendered_static or {}), **args}
        try:
            return await invoke_action(
                provider_id=node.provider,
                action_slug=node.action_slug,
                params=merged,
                workspace_connections=workspace_connections,
                allow_dry_run=node.allow_dry_run,
                workspace_id=workspace_id,
                db=db,
            )
        except Exception as exc:  # noqa: BLE001 — surface the failure to the LLM
            log.warning(
                "tool_resolver.action_failed",
                node=node.id,
                action=node.action_slug,
                error=str(exc),
            )
            return {
                "ok": False,
                "error": str(exc),
                "__provider__": node.provider,
                "__action__": node.action_slug,
            }

    invoke_guarded = _with_timeout_and_retry(
        invoke,
        timeout_ms=node.timeout_ms,
        max_retries=node.max_retries,
        initial_delay_ms=node.retry_initial_delay_ms,
        label=f"{node.provider}:{node.action_slug}",
    )
    return ResolvedTool(name=name, spec=spec, invoke=invoke_guarded)


def _wrap_data_store(
    node: DataStoreNode,
    *,
    workspace_id: UUID,
    workflow_id: UUID,
    execution_id: UUID | None,
    db: AsyncSession | None,
) -> ResolvedTool:
    name = _sanitise_tool_name(node.id)
    description = (
        node.tool_description.strip()
        or f"data_store {node.op} on table '{node.table}'"
    )
    spec = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": _data_store_param_schema(node),
        },
    }

    async def invoke(args: dict[str, Any]) -> dict[str, Any]:
        if db is None:
            # Echo mode for tests / dry-run demos. Shape parallels the real
            # response so downstream code paths don't have to branch.
            return {
                "ok": True,
                "__dry_run__": True,
                "op": node.op,
                "table": node.table,
                "echo": args,
            }

        # Re-resolve placeholders inside the LLM-provided args against
        # themselves (so the LLM can still pass the same templating style
        # by accident; the agent doesn't have to).
        args_rendered = render_placeholders(args, {"input": args, **args})

        from sqlalchemy import select
        from models.workflow_data import WorkflowData

        if node.op == "write":
            # When the caller doesn't supply a key, mint a readable unique one
            # (e.g. ``TICKET-A1B2C3``). LLMs are unreliable at inventing unique
            # ids — they reuse obvious values like "12345" — so for create-style
            # writes the agent is told to omit ``key`` and let the system assign.
            key = str(
                args_rendered.get("key")
                or f"{node.table.rstrip('s').upper()}-{uuid4().hex[:6].upper()}"
            )
            payload = args_rendered.get("payload")
            if not isinstance(payload, dict):
                payload = {"value": payload}
            stmt = select(WorkflowData).where(
                WorkflowData.workspace_id == workspace_id,
                WorkflowData.table_name == node.table,
                WorkflowData.row_key == key,
            )
            existing = (await db.execute(stmt)).scalar_one_or_none()
            if existing is None:
                row = WorkflowData(
                    workspace_id=workspace_id,
                    table_name=node.table,
                    row_key=key,
                    data=payload,
                    last_workflow_id=workflow_id,
                    last_execution_id=execution_id,
                )
                db.add(row)
            else:
                existing.data = {**(existing.data or {}), **payload}
                existing.last_workflow_id = workflow_id
                existing.last_execution_id = execution_id
            await db.commit()
            return {"ok": True, "op": "write", "table": node.table, "key": key}

        if node.op == "read":
            key = args_rendered.get("key")
            if not key:
                return {"ok": False, "error": "key required for read"}
            stmt = select(WorkflowData).where(
                WorkflowData.workspace_id == workspace_id,
                WorkflowData.table_name == node.table,
                WorkflowData.row_key == str(key),
            )
            existing = (await db.execute(stmt)).scalar_one_or_none()
            return {
                "ok": True,
                "op": "read",
                "table": node.table,
                "key": key,
                "found": existing is not None,
                "row": existing.data if existing else None,
            }

        # query
        stmt = select(WorkflowData).where(
            WorkflowData.workspace_id == workspace_id,
            WorkflowData.table_name == node.table,
        )
        rows = list((await db.execute(stmt)).scalars().all())
        flt = args_rendered.get("filter") or {}
        if not isinstance(flt, dict):
            flt = {}
        out: list[dict[str, Any]] = []
        for r in rows:
            data = r.data or {}
            if all(data.get(k) == v for k, v in flt.items()):
                out.append({"key": r.row_key, "data": data})
        return {
            "ok": True,
            "op": "query",
            "table": node.table,
            "rows": out,
            "count": len(out),
        }

    invoke_guarded = _with_timeout_and_retry(
        invoke,
        timeout_ms=node.timeout_ms,
        max_retries=node.max_retries,
        initial_delay_ms=node.retry_initial_delay_ms,
        label=f"data_store:{node.op}:{node.table}",
    )
    return ResolvedTool(name=name, spec=spec, invoke=invoke_guarded)


__all__ = [
    "ResolvedTool",
    "ResolvedToolset",
    "ToolHandler",
    "build_toolset",
]
