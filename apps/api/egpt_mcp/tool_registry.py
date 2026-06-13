"""Composio MCP tool registry — schemas, cache, execute, logging."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from egpt_mcp._composio_compat import ComposioToolSet
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from egpt_mcp.provider_apps import resolve_composio_app
from egpt_mcp.tool_run_buffer import ToolRunBuffer
from models.integration import Integration, IntegrationStatus
from models.tool_execution_log import ToolExecutionLog
from services.pii_service import PIIService

log = logging.getLogger(__name__)

TOOL_CACHE_PREFIX = "tools:"
TOOL_CACHE_TTL_SECONDS = 300


class ToolExecutionError(RuntimeError):
    """Raised when Composio cannot execute an action."""


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


def _action_prefix_provider(action_slug: str) -> str:
    return action_slug.split("_", 1)[0].lower()


class ToolRegistry:
    """Workspace-scoped Composio tools with Redis caching and execution logging."""

    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        *,
        composio_toolset_factory: Any | None = None,
    ) -> None:
        self._settings = settings
        self._redis = redis
        self._pii = PIIService()
        self._toolset_factory = composio_toolset_factory or self._default_toolset

    def _default_toolset(self, *, entity_id: str) -> ComposioToolSet:
        key = self._settings.COMPOSIO_API_KEY.strip()
        if not key:
            raise ToolExecutionError("COMPOSIO_API_KEY is not configured")
        return ComposioToolSet(api_key=key, entity_id=entity_id)

    def _cache_key(self, workspace_id: UUID) -> str:
        return f"{TOOL_CACHE_PREFIX}{workspace_id}"

    async def invalidate_workspace_tool_cache(self, workspace_id: UUID) -> None:
        await self._redis.delete(self._cache_key(workspace_id))

    async def get_workspace_tools(self, db: AsyncSession, workspace_id: UUID) -> list[dict[str, Any]]:
        cache_key = self._cache_key(workspace_id)
        raw = await self._redis.get(cache_key)
        if raw:
            try:
                parsed = json.loads(raw if isinstance(raw, str) else raw.decode())
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        tools = await self._fetch_tools_fresh(db, workspace_id)
        await self._redis.set(cache_key, json.dumps(tools), ex=TOOL_CACHE_TTL_SECONDS)
        return tools

    async def _fetch_tools_fresh(self, db: AsyncSession, workspace_id: UUID) -> list[dict[str, Any]]:
        stmt = select(Integration).where(
            Integration.workspace_id == workspace_id,
            Integration.status == IntegrationStatus.CONNECTED,
        )
        rows = list((await db.execute(stmt)).scalars().all())
        if not rows:
            return []

        apps: list[Any] = []
        for row in rows:
            app = resolve_composio_app(row.provider)
            if app is not None:
                apps.append(app)

        if not apps:
            return []

        sample_entity = rows[0].composio_entity_id
        try:
            toolset = self._toolset_factory(entity_id=sample_entity)
            client = toolset._init_client()  # noqa: SLF001 — SDK helper
            actions = client.actions.get(apps=apps, allow_all=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("composio.actions.fetch_failed", error=str(exc))
            raise ToolExecutionError(f"failed to load tools from Composio: {exc}") from exc

        out: list[dict[str, Any]] = []
        for action in actions:
            params_schema: dict[str, Any]
            try:
                params_schema = action.parameters.model_dump(mode="json")
            except (TypeError, ValueError):  # pragma: no cover
                params_schema = {}
            app_slug = action.appName.lower() if action.appName else ""
            out.append(
                {
                    "name": action.name,
                    "description": (action.description or "")[:4096],
                    "provider": app_slug,
                    "parameters": params_schema,
                }
            )
        return out

    async def get_tool_names_for_prompt(self, db: AsyncSession, workspace_id: UUID) -> list[str]:
        tools = await self.get_workspace_tools(db, workspace_id)
        return sorted({str(t["name"]) for t in tools if t.get("name")})

    def sync_execute_action(
        self,
        *,
        integrations: Sequence[Integration],
        tool_name: str,
        params: dict[str, Any],
        execution_id: UUID | None,
        tool_run_buffer: ToolRunBuffer | None,
    ) -> dict[str, Any]:
        prefix = _action_prefix_provider(tool_name)
        integration = next(
            (
                i
                for i in integrations
                if i.provider.lower() == prefix and i.status == IntegrationStatus.CONNECTED
            ),
            None,
        )
        if integration is None:
            raise ToolExecutionError(
                f"No connected integration for action `{tool_name}` (provider `{prefix}`)"
            )

        redacted_params = _redact_mapping(dict(params or {}), self._pii)

        started = time.perf_counter()
        success = False
        err_msg: str | None = None
        output: dict[str, Any] = {}
        try:
            toolset = self._toolset_factory(entity_id=integration.composio_entity_id)
            output = toolset.execute_action(
                action=tool_name,
                params=dict(params or {}),
                entity_id=integration.composio_entity_id,
                connected_account_id=integration.composio_connection_id,
            )
            success = bool(output.get("successfull", output.get("successful", True)))
            if not success:
                err_msg = str(output.get("error") or output.get("message") or "tool_failed")
        except Exception as exc:  # noqa: BLE001
            err_msg = str(exc)
            output = {"successfull": False, "error": err_msg, "data": {}}
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            row = {
                "execution_id": execution_id,
                "tool_name": tool_name,
                "input_params": redacted_params,
                "output_data": output,
                "duration_ms": elapsed_ms,
                "success": success,
                "error_message": err_msg,
            }
            if tool_run_buffer is not None:
                tool_run_buffer.append(row)

        if err_msg:
            raise ToolExecutionError(err_msg)
        return output

    async def execute_tool(
        self,
        db: AsyncSession,
        *,
        tool_name: str,
        params: dict[str, Any],
        workspace_id: UUID,
        execution_id: UUID | None,
        tool_run_buffer: ToolRunBuffer | None = None,
    ) -> dict[str, Any]:
        stmt = select(Integration).where(
            Integration.workspace_id == workspace_id,
            Integration.status == IntegrationStatus.CONNECTED,
        )
        integrations = list((await db.execute(stmt)).scalars().all())

        buf = tool_run_buffer if tool_run_buffer is not None else ToolRunBuffer(execution_id)
        import asyncio

        result = await asyncio.to_thread(
            self.sync_execute_action,
            integrations=integrations,
            tool_name=tool_name,
            params=params,
            execution_id=execution_id,
            tool_run_buffer=buf,
        )
        if tool_run_buffer is None:
            await self.persist_tool_run_buffer(db, buf)
            await db.commit()
        return result

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


__all__ = [
    "TOOL_CACHE_PREFIX",
    "TOOL_CACHE_TTL_SECONDS",
    "ToolExecutionError",
    "ToolRegistry",
]
