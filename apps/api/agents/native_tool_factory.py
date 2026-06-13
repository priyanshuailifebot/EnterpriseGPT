"""Resolve workflow agents' tool slugs to native Dynamiq tool nodes.

Given a workspace's stored :class:`models.native_connection.NativeConnection`
rows and a workflow definition, produce ``agent_id -> [DynamiqToolNode]`` —
mirroring the shape of ``DynamiqService.build_agent_composio_tools``. Callers
in ``workflow_service.execute_workflow`` prefer this mapping and only fall
back to the Composio bridge for slugs we don't yet support natively.
"""

from __future__ import annotations

import structlog
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.native_providers import (
    NativeProvider,
    list_providers,
    resolve_provider_for_slug,
)
from models.native_connection import (
    NativeConnection,
    NativeConnectionAuthType,
    NativeConnectionStatus,
)
from schemas.workflow import WorkflowDefinition
from services.native_connection_service import decode_config, serialize_config
from services.oauth2_service import (
    OAuthError,
    get_oauth_provider,
    refresh_token_if_needed,
)

log = structlog.get_logger(__name__)


async def load_workspace_connections(
    db: AsyncSession, *, workspace_id: UUID
) -> list[NativeConnection]:
    res = await db.execute(
        select(NativeConnection).where(
            NativeConnection.workspace_id == workspace_id,
            NativeConnection.status == NativeConnectionStatus.ACTIVE,
        )
    )
    return list(res.scalars().all())


def _maybe_refresh_oauth(row: NativeConnection, cfg: dict[str, Any]) -> dict[str, Any]:
    """Refresh OAuth credentials in-place if expired; returns the (possibly new) dict."""
    if row.auth_type != NativeConnectionAuthType.OAUTH2:
        return cfg
    oauth_provider = get_oauth_provider(row.provider)
    if not oauth_provider:
        return cfg
    try:
        # ``refresh_token_if_needed`` is async; this helper is sync, so use
        # ``asyncio.run`` inside a thread (callers run us under ``asyncio.to_thread``
        # from the executor, so a nested loop is fine).
        import asyncio

        refreshed = asyncio.run(refresh_token_if_needed(oauth_provider, cfg))
    except OAuthError as exc:
        log.warning(
            "native_tool_factory.refresh_failed", provider=row.provider, error=str(exc)
        )
        return cfg
    if refreshed is None:
        return cfg
    # Persistence of the refreshed token is deferred — the caller below
    # writes it back through SQLAlchemy.
    cfg.update(refreshed)
    return cfg


def _instantiate_provider_tool(
    provider: NativeProvider, row: NativeConnection, slug: str
) -> tuple[Any | None, dict[str, Any] | None]:
    """Returns ``(tool_node, refreshed_cfg_or_none)``.

    ``refreshed_cfg_or_none`` is set when the OAuth token was rotated and the
    caller should persist the new ciphertext on the row.
    """
    if provider.build_connection is None or provider.build_tool is None:
        return None, None
    try:
        cfg = decode_config(row)
        original = dict(cfg)
        cfg = _maybe_refresh_oauth(row, cfg)
        conn = provider.build_connection(cfg)
        tool = provider.build_tool(conn, slug)
        rotated = cfg if cfg != original else None
        return tool, rotated
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "native_tool_factory.instantiate_failed",
            provider=provider.id,
            slug=slug,
            error=str(exc),
        )
        return None, None


def build_native_agent_tools(
    definition: WorkflowDefinition,
    *,
    connections: list[NativeConnection],
) -> tuple[dict[str, list[Any]], set[str]]:
    """Return ``(agent_id -> tools, resolved_slugs)``.

    ``resolved_slugs`` is the lowercase set of tool slugs we natively built —
    callers can subtract this from the workflow's allowed slugs so the
    Composio bridge only fills the gap.
    """
    by_provider: dict[str, NativeConnection] = {}
    for row in connections:
        # First (most-recently-updated by ordering at the DB) wins; for the
        # simple per-workspace-per-provider case that's the one to use.
        by_provider.setdefault(row.provider, row)

    mapping: dict[str, list[Any]] = {}
    resolved: set[str] = set()

    refreshed_rows: dict[str, dict[str, Any]] = {}
    for agent_id, tool_slugs in definition.agent_tool_bindings().items():
        nodes: list[Any] = []
        for slug in tool_slugs:
            provider = resolve_provider_for_slug(slug)
            if provider is None:
                continue
            row = by_provider.get(provider.id)
            if row is None:
                continue
            tool, rotated_cfg = _instantiate_provider_tool(provider, row, slug)
            if tool is None:
                continue
            nodes.append(tool)
            resolved.add(slug.lower())
            if rotated_cfg is not None:
                refreshed_rows[str(row.id)] = rotated_cfg
        if nodes:
            mapping[agent_id] = nodes

    # Persist any rotated OAuth credentials. We do this here rather than in the
    # helper because SQLAlchemy sessions are owned by the caller.
    if refreshed_rows:
        for row in connections:
            new_cfg = refreshed_rows.get(str(row.id))
            if new_cfg is not None:
                row.config_encrypted = serialize_config(new_cfg)

    return mapping, resolved


def available_native_tool_slugs() -> list[dict[str, str]]:
    """Flat catalog of every slug that has a native binding.

    Used by the workflow interpreter to advertise non-Composio tools.
    """
    out: list[dict[str, str]] = []
    for p in list_providers():
        for slug in p.tool_slugs:
            out.append(
                {
                    "name": slug,
                    "provider": p.id,
                    "description": p.description,
                    "category": p.category,
                }
            )
    return out


__all__ = [
    "build_native_agent_tools",
    "load_workspace_connections",
    "available_native_tool_slugs",
]
