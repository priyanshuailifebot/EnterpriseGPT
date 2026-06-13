"""CRUD + encryption for per-workspace MCP server registrations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.crypto import decrypt_secret, encrypt_secret
from core.redis import get_redis
from egpt_mcp.mcp_tool_registry import (
    MCPServerConfig,
    MCPToolError,
    MCPToolRegistry,
)
from models.mcp_server import MCPServer, MCPServerStatus, MCPServerTransport


class MCPServerError(ValueError):
    """User-visible MCP server validation / persistence error."""


def serialize_config(payload: dict[str, Any]) -> str:
    return encrypt_secret(json.dumps(payload, separators=(",", ":")))


def decode_config(row: MCPServer) -> dict[str, Any]:
    raw = decrypt_secret(row.config_encrypted)
    return json.loads(raw)


def to_server_config(row: MCPServer) -> MCPServerConfig:
    """Build the runtime config the registry uses to talk to this server."""
    cfg = decode_config(row)
    headers: dict[str, str] = dict(cfg.get("extra_headers") or {})
    name = cfg.get("auth_header_name") or "X-CONSUMER-API-KEY"
    value = cfg.get("auth_header_value") or ""
    if value:
        headers[name] = value
    return MCPServerConfig(
        url=row.url,
        transport=row.transport.value if hasattr(row.transport, "value") else str(row.transport),
        headers=headers,
        cache_suffix=f"row:{row.id}",
    )


async def list_servers(db: AsyncSession, workspace_id: UUID) -> list[MCPServer]:
    stmt = select(MCPServer).where(MCPServer.workspace_id == workspace_id).order_by(
        MCPServer.created_at.desc(),
    )
    return list((await db.execute(stmt)).scalars().all())


async def get_server(db: AsyncSession, workspace_id: UUID, server_id: UUID) -> MCPServer | None:
    stmt = select(MCPServer).where(
        MCPServer.id == server_id,
        MCPServer.workspace_id == workspace_id,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def create_server(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    created_by_id: UUID,
    name: str,
    url: str,
    transport: str,
    auth_header_name: str,
    auth_header_value: str,
    extra_headers: dict[str, str] | None = None,
) -> MCPServer:
    cfg_payload = {
        "auth_header_name": auth_header_name,
        "auth_header_value": auth_header_value,
        "extra_headers": extra_headers or {},
    }
    try:
        transport_enum = MCPServerTransport(transport)
    except ValueError as exc:
        raise MCPServerError(f"unknown transport: {transport!r}") from exc

    row = MCPServer(
        workspace_id=workspace_id,
        created_by_id=created_by_id,
        name=name.strip(),
        url=url.strip(),
        transport=transport_enum,
        status=MCPServerStatus.ACTIVE,
        config_encrypted=serialize_config(cfg_payload),
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise MCPServerError(f"a server named {name!r} already exists") from exc
    await db.commit()
    await db.refresh(row)
    return row


async def delete_server(db: AsyncSession, *, workspace_id: UUID, server_id: UUID) -> bool:
    row = await get_server(db, workspace_id, server_id)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True


async def test_server(
    db: AsyncSession,
    *,
    row: MCPServer,
) -> tuple[bool, str, int, list[str]]:
    """Probe a server by listing its tools. Updates the row's test fields."""
    registry = MCPToolRegistry(
        get_settings(),
        get_redis(),
        server_config=to_server_config(row),
    )
    try:
        tools = await registry.list_tools()
    except MCPToolError as exc:
        row.status = MCPServerStatus.ERROR
        row.last_test_at = datetime.now(timezone.utc)
        row.last_test_error = str(exc)
        row.last_tool_count = 0
        await db.commit()
        return False, str(exc), 0, []
    except Exception as exc:  # noqa: BLE001 — surface any other failure cleanly
        row.status = MCPServerStatus.ERROR
        row.last_test_at = datetime.now(timezone.utc)
        row.last_test_error = str(exc)
        row.last_tool_count = 0
        await db.commit()
        return False, str(exc), 0, []

    sample = [str(t.get("name") or "") for t in tools[:5] if t.get("name")]
    row.status = MCPServerStatus.ACTIVE
    row.last_test_at = datetime.now(timezone.utc)
    row.last_test_error = None
    row.last_tool_count = len(tools)
    await db.commit()
    return True, f"connected — {len(tools)} tool(s) advertised", len(tools), sample


def response_payload(row: MCPServer) -> dict[str, Any]:
    """Project the row into the shape the API returns (auth value redacted)."""
    cfg = decode_config(row)
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "name": row.name,
        "url": row.url,
        "transport": row.transport.value if hasattr(row.transport, "value") else str(row.transport),
        "status": row.status.value if hasattr(row.status, "value") else str(row.status),
        "auth_header_name": cfg.get("auth_header_name") or "X-CONSUMER-API-KEY",
        "last_test_at": row.last_test_at,
        "last_test_error": row.last_test_error,
        "last_tool_count": row.last_tool_count,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


__all__ = [
    "MCPServerError",
    "create_server",
    "decode_config",
    "delete_server",
    "get_server",
    "list_servers",
    "response_payload",
    "serialize_config",
    "test_server",
    "to_server_config",
]
