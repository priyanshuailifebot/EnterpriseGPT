"""CRUD + encryption + credential-probe for native Dynamiq connections (Phase A)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.native_providers import NativeProvider, get_provider, list_providers
from core.crypto import decrypt_secret, encrypt_secret
from models.native_connection import (
    NativeConnection,
    NativeConnectionAuthType,
    NativeConnectionStatus,
)


class NativeConnectionError(ValueError):
    """Raised for user-correctable connection problems (bad fields, dupes)."""


def serialize_config(plain: dict[str, Any]) -> str:
    return encrypt_secret(json.dumps(plain, separators=(",", ":")))


# Alias kept for callers that imported the private name during refactor.
_serialize_config = serialize_config


def decode_config(row: NativeConnection) -> dict[str, Any]:
    raw = decrypt_secret(row.config_encrypted)
    return json.loads(raw)


def validate_payload(provider: NativeProvider, raw: dict[str, Any]) -> dict[str, Any]:
    """Strip to declared fields, enforce required ones, return the normalized dict."""
    clean: dict[str, Any] = {}
    for field in provider.fields:
        val = raw.get(field.key)
        if field.required and (val is None or (isinstance(val, str) and not val.strip())):
            raise NativeConnectionError(f"missing required field: {field.key}")
        if val is None:
            continue
        clean[field.key] = val.strip() if isinstance(val, str) else val
    return clean


async def create_connection(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    user_id: UUID,
    provider_id: str,
    name: str,
    config: dict[str, Any],
) -> NativeConnection:
    provider = get_provider(provider_id)
    if not provider:
        raise NativeConnectionError(f"unknown provider: {provider_id}")
    clean = validate_payload(provider, config)

    existing = await db.execute(
        select(NativeConnection).where(
            NativeConnection.workspace_id == workspace_id,
            NativeConnection.provider == provider.id,
            NativeConnection.name == name,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise NativeConnectionError("a connection with this name already exists for this provider")

    row = NativeConnection(
        workspace_id=workspace_id,
        created_by_id=user_id,
        provider=provider.id,
        name=name.strip() or provider.name,
        auth_type=NativeConnectionAuthType(provider.auth_type),
        status=NativeConnectionStatus.ACTIVE,
        config_encrypted=_serialize_config(clean),
    )
    db.add(row)
    await db.flush()
    return row


async def update_connection(
    db: AsyncSession,
    *,
    row: NativeConnection,
    config_patch: dict[str, Any] | None = None,
    name: str | None = None,
) -> NativeConnection:
    provider = get_provider(row.provider)
    if not provider:
        raise NativeConnectionError(f"unknown provider: {row.provider}")

    if config_patch is not None:
        current = decode_config(row)
        merged = {**current, **config_patch}
        validated = validate_payload(provider, merged)
        row.config_encrypted = _serialize_config(validated)
        row.status = NativeConnectionStatus.ACTIVE
        row.last_test_error = None
    if name is not None and name.strip():
        row.name = name.strip()
    await db.flush()
    return row


async def list_connections(
    db: AsyncSession, *, workspace_id: UUID
) -> list[NativeConnection]:
    res = await db.execute(
        select(NativeConnection)
        .where(NativeConnection.workspace_id == workspace_id)
        .order_by(NativeConnection.provider.asc(), NativeConnection.name.asc())
    )
    return list(res.scalars().all())


async def delete_connection(
    db: AsyncSession, *, row: NativeConnection
) -> None:
    await db.delete(row)
    await db.flush()


def test_connection(row: NativeConnection) -> tuple[bool, str]:
    """Synchronously probe the upstream provider with the stored credentials.

    Returns ``(success, message)``. Updates on the row itself are caller's
    responsibility — this is pure to make it easy to call from a router or a
    queue.
    """
    provider = get_provider(row.provider)
    if not provider or not provider.build_connection:
        return False, "provider not yet supported for credential probing"
    cfg = decode_config(row)
    try:
        conn = provider.build_connection(cfg)
    except Exception as exc:  # noqa: BLE001
        return False, f"could not instantiate connection: {exc}"
    if not provider.probe:
        return True, "credentials stored (no probe configured for this provider)"
    try:
        msg = provider.probe(conn)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return True, msg


async def record_test_result(
    db: AsyncSession,
    *,
    row: NativeConnection,
    success: bool,
    message: str,
) -> None:
    row.last_test_at = datetime.now(timezone.utc)
    if success:
        row.status = NativeConnectionStatus.ACTIVE
        row.last_test_error = None
    else:
        row.status = NativeConnectionStatus.ERROR
        row.last_test_error = message[:1024]
    await db.flush()


def public_provider_catalog() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in list_providers():
        out.append(
            {
                "id": p.id,
                "name": p.name,
                "category": p.category,
                "description": p.description,
                "auth_type": p.auth_type,
                "icon": p.icon,
                "docs_url": p.docs_url,
                "tool_slugs": list(p.tool_slugs),
                "fields": [
                    {
                        "key": f.key,
                        "label": f.label,
                        "type": f.type,
                        "required": f.required,
                        "placeholder": f.placeholder,
                        "help_text": f.help_text,
                    }
                    for f in p.fields
                ],
            }
        )
    return out


__all__ = [
    "NativeConnectionError",
    "create_connection",
    "update_connection",
    "list_connections",
    "delete_connection",
    "test_connection",
    "record_test_result",
    "decode_config",
    "serialize_config",
    "public_provider_catalog",
]
