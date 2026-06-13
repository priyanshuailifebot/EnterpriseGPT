"""MCP-backed tool registry — talks directly to Composio's hosted MCP endpoint.

This is the v1 replacement for the legacy ``ToolRegistry`` in
``tool_registry.py``, which depends on the broken ``ComposioToolSet`` SDK
shim. Instead of the Python SDK, we speak the MCP wire protocol via the
``mcp`` client library — the same protocol Dynamiq uses natively.

Two operations are exposed:

* :meth:`list_tools` — enumerates the tools the Composio MCP server exposes
  for the configured consumer key. Cached in Redis per consumer key with a
  short TTL so the interpreter doesn't pay the round-trip on every preview.
* :meth:`call_tool` — invokes a tool by name with a dict of arguments.
  Returns the structured payload Composio returns, plus latency, and logs
  the run to ``ToolExecutionLog``.

Per-end-user routing: Composio identifies a consumer by the
``X-CONSUMER-API-KEY`` header. The default registry uses the workspace-wide
key from settings; future multi-tenant routing can be added by overriding
``_headers_for_user`` to swap keys per user. The plumbing is here so when
you wire that up later it's a one-method change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from egpt_mcp.tool_run_buffer import ToolRunBuffer
from models.tool_execution_log import ToolExecutionLog
from services.pii_service import PIIService

log = logging.getLogger(__name__)

MCP_TOOL_CACHE_PREFIX = "mcp_tools:"
MCP_TOOL_CACHE_TTL_SECONDS = 300


class MCPToolError(RuntimeError):
    """Raised when the MCP endpoint cannot list or execute a tool."""


def _redact_mapping(payload: dict[str, Any], pii: PIIService) -> dict[str, Any]:
    """Best-effort synchronous redaction for nested dict/list structures."""

    def walk(val: Any) -> Any:
        if isinstance(val, str):
            redacted, _ = pii.redact(val)
            return redacted
        if isinstance(val, list):
            return [walk(v) for v in val]
        if isinstance(val, dict):
            return {k: walk(v) for k, v in val.items()}
        return val

    return walk(dict(payload))


class MCPServerConfig(BaseModel):
    """Resolved config for one MCP server. Either built from settings (legacy
    env-config) or from an :class:`models.mcp_server.MCPServer` row."""

    url: str
    transport: str = "streamable-http"
    headers: dict[str, str] = Field(default_factory=dict)
    cache_suffix: str = "default"


class MCPToolRegistry:
    """Workspace-scoped tools from one or more hosted MCP endpoints.

    Two modes:

    * **Settings mode** (legacy): instantiate with ``(settings, redis)`` and
      it talks to the single endpoint configured via ``COMPOSIO_MCP_*``
      env vars. This is what the action runner uses today.
    * **Per-server mode**: instantiate with ``(settings, redis,
      server_config=...)`` to override the endpoint at call time. Used by
      the multi-server CRUD path so each :class:`MCPServer` row can be
      tested / queried independently.
    """

    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        *,
        server_config: "MCPServerConfig | None" = None,
    ) -> None:
        self._settings = settings
        self._redis = redis
        self._pii = PIIService()
        self._override = server_config

    # ------------------------------------------------------------------
    # Configuration helpers — override in subclasses for per-user routing.
    # ------------------------------------------------------------------

    def _resolved_config(self) -> "MCPServerConfig | None":
        if self._override is not None:
            return self._override
        # Fallback to env config (legacy single-endpoint mode).
        url = (self._settings.COMPOSIO_MCP_URL or "").strip()
        key = (self._settings.COMPOSIO_MCP_API_KEY or "").strip()
        if not url or not key:
            return None
        return MCPServerConfig(
            url=url,
            transport=(self._settings.COMPOSIO_MCP_TRANSPORT or "streamable-http").strip(),
            headers={"x-api-key": key},
            cache_suffix=f"env:{key[-12:]}",
        )

    def _is_enabled(self) -> bool:
        return self._resolved_config() is not None

    def _cache_key(self) -> str:
        cfg = self._resolved_config()
        suffix = cfg.cache_suffix if cfg else "default"
        return f"{MCP_TOOL_CACHE_PREFIX}{suffix}"

    # ------------------------------------------------------------------
    # MCP client lifecycle
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _open_session(self, user_id: UUID | None = None):  # noqa: ARG002 — reserved
        """Open an MCP ``ClientSession`` against the resolved endpoint."""
        cfg = self._resolved_config()
        if cfg is None:
            raise MCPToolError("MCP server is not configured")

        from mcp import ClientSession

        transport = (cfg.transport or "streamable-http").strip().lower()
        url = cfg.url
        headers = dict(cfg.headers or {})

        if transport in {"streamable-http", "streamable_http", "http"}:
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(url=url, headers=headers) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        else:
            from mcp.client.sse import sse_client

            async with sse_client(url=url, headers=headers) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    async def invalidate_cache(self) -> None:
        await self._redis.delete(self._cache_key())

    async def list_tools(self, user_id: UUID | None = None) -> list[dict[str, Any]]:
        """Return tools exposed by the MCP server, cached in Redis."""
        cache_key = self._cache_key()
        raw = await self._redis.get(cache_key)
        if raw:
            try:
                parsed = json.loads(raw if isinstance(raw, str) else raw.decode())
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        try:
            async with self._open_session(user_id) as session:
                result = await session.list_tools()
                tools = result.tools or []
                out: list[dict[str, Any]] = []
                for t in tools:
                    schema_obj: dict[str, Any] = {}
                    schema_attr = getattr(t, "inputSchema", None)
                    if isinstance(schema_attr, dict):
                        schema_obj = schema_attr
                    elif schema_attr is not None and hasattr(schema_attr, "model_dump"):
                        schema_obj = schema_attr.model_dump(mode="json")
                    out.append(
                        {
                            "name": t.name,
                            "description": (getattr(t, "description", "") or "")[:4096],
                            "provider": _infer_provider(t.name),
                            "parameters": schema_obj,
                            "source": "composio_mcp",
                        }
                    )
        except MCPToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("mcp.list_tools.failed", extra={"error": str(exc)})
            raise MCPToolError(f"failed to list MCP tools: {exc}") from exc

        await self._redis.set(cache_key, json.dumps(out), ex=MCP_TOOL_CACHE_TTL_SECONDS)
        return out

    async def get_tool_names_for_prompt(self) -> list[str]:
        try:
            tools = await self.list_tools()
        except MCPToolError:
            return []
        return sorted({str(t["name"]) for t in tools if t.get("name")})

    async def call_tool(
        self,
        db: AsyncSession | None,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        execution_id: UUID | None,
        tool_run_buffer: ToolRunBuffer | None = None,
        user_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Invoke a single MCP tool. Logs result to ``ToolExecutionLog``."""
        redacted = _redact_mapping(dict(arguments or {}), self._pii)
        buf = tool_run_buffer if tool_run_buffer is not None else ToolRunBuffer(execution_id)

        started = time.perf_counter()
        success = False
        err_msg: str | None = None
        output: dict[str, Any] = {}
        try:
            async with self._open_session(user_id) as session:
                result = await session.call_tool(name=tool_name, arguments=dict(arguments or {}))
            output = _serialise_call_result(result)
            success = not bool(getattr(result, "isError", False))
            if not success:
                err_msg = _extract_error_message(output) or "mcp_tool_failed"
        except MCPToolError as exc:
            err_msg = str(exc)
            output = {"successful": False, "error": err_msg, "data": {}}
        except Exception as exc:  # noqa: BLE001
            err_msg = str(exc)
            output = {"successful": False, "error": err_msg, "data": {}}
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            buf.append(
                {
                    "execution_id": execution_id,
                    "tool_name": tool_name,
                    "input_params": redacted,
                    "output_data": output,
                    "duration_ms": elapsed_ms,
                    "success": success,
                    "error_message": err_msg,
                }
            )

        # Persist the run log only when we have both a DB session and no
        # caller-owned buffer. The action_runner path runs without a DB
        # session (the higher-level executor emits its own audit event).
        if tool_run_buffer is None and db is not None:
            await self.persist_tool_run_buffer(db, buf)
            await db.commit()

        if err_msg:
            raise MCPToolError(err_msg)
        return output

    @staticmethod
    async def persist_tool_run_buffer(db: AsyncSession, buffer: ToolRunBuffer | None) -> None:
        if buffer is None or not buffer.entries:
            return
        for entry in buffer.entries:
            db.add(
                ToolExecutionLog(
                    execution_id=entry.get("execution_id"),
                    tool_name=entry["tool_name"],
                    input_params=entry.get("input_params") or {},
                    output_data=entry.get("output_data"),
                    duration_ms=entry.get("duration_ms"),
                    success=bool(entry.get("success")),
                    error_message=entry.get("error_message"),
                )
            )
        buffer.entries.clear()
        await db.flush()


def _infer_provider(tool_name: str) -> str:
    """Composio tool names look like ``GOOGLESHEETS_BATCH_GET`` — first
    segment maps cleanly to the provider id used elsewhere in the app."""
    head = tool_name.split("_", 1)[0] if tool_name else ""
    return head.lower()


def _extract_error_message(output: dict[str, Any]) -> str | None:
    """Pull the most informative error out of a Composio MCP failure payload.

    Composio nests specific error messages multiple levels deep (e.g. inside
    ``data.results[].response.error``) while also emitting a generic wrapper
    error at the top level (``"1 out of 1 tools failed"``). We harvest every
    error/message string we can find and return the longest one — specific
    errors are almost always longer than generic wrappers.
    """
    candidates: list[str] = []

    def harvest(obj: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(obj, str):
            stripped = obj.strip()
            if stripped.startswith("{"):
                try:
                    parsed = json.loads(stripped)
                except (TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    harvest(parsed, depth + 1)
            return
        if isinstance(obj, dict):
            for key in ("error", "message", "errorMessage", "detail"):
                v = obj.get(key)
                if isinstance(v, str) and v.strip():
                    candidates.append(v.strip())
            for v in obj.values():
                harvest(v, depth + 1)
            return
        if isinstance(obj, list):
            for v in obj:
                harvest(v, depth + 1)

    harvest(output)
    if not candidates:
        return None
    return max(candidates, key=len)


def _serialise_call_result(result: Any) -> dict[str, Any]:
    """Best-effort conversion of an MCP ``CallToolResult`` to a plain dict.

    The MCP Python SDK returns a pydantic-shaped object whose ``content``
    field is a list of typed parts (text, image, etc). For workflow output
    we collapse text parts into one string and pass structured parts through
    as-is."""
    if result is None:
        return {}
    if isinstance(result, dict):
        return result

    out: dict[str, Any] = {}
    if hasattr(result, "model_dump"):
        try:
            out = result.model_dump(mode="json")
        except Exception:  # noqa: BLE001
            out = {}
    if not out:
        # Fallback for SDK versions without model_dump
        content = getattr(result, "content", None) or []
        out = {"content": [getattr(c, "model_dump", lambda **_: c)() for c in content]}
    if "successful" not in out:
        out["successful"] = not bool(getattr(result, "isError", False))
    return out


__all__ = [
    "MCP_TOOL_CACHE_PREFIX",
    "MCP_TOOL_CACHE_TTL_SECONDS",
    "MCPToolError",
    "MCPToolRegistry",
]


# Async helper so callers in sync contexts can still drive the registry.
def run_async(coro):  # pragma: no cover — trivial wrapper
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    return asyncio.run_coroutine_threadsafe(coro, loop).result()
