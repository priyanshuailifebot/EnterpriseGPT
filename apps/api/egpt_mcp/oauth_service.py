"""OAuth connection orchestration with Composio connected accounts."""

from __future__ import annotations

import json
import secrets
from uuid import UUID

from egpt_mcp._composio_compat import ComposioToolSet
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from egpt_mcp.provider_apps import resolve_composio_app, supported_providers
from egpt_mcp.tool_registry import ToolRegistry
from models.integration import Integration, IntegrationStatus
from models.user import User


class OAuthStateError(ValueError):
    """Invalid or expired OAuth state token."""


OAUTH_STATE_PREFIX = "egpt:oauth_state:"
OAUTH_STATE_TTL_SECONDS = 600


class OAuthService:
    """Initiate / finalize Composio OAuth flows scoped per workspace + user."""

    def __init__(self, settings: Settings, redis: Redis, tool_registry: ToolRegistry) -> None:
        self._settings = settings
        self._redis = redis
        self._registry = tool_registry

    def _toolset(self, *, entity_id: str) -> ComposioToolSet:
        key = self._settings.COMPOSIO_API_KEY.strip()
        if not key:
            raise RuntimeError("COMPOSIO_API_KEY is not configured")
        return ComposioToolSet(api_key=key, entity_id=entity_id)

    def _callback_base_url(self) -> str:
        return self._settings.APP_PUBLIC_URL.rstrip("/")

    async def initiate_connection(
        self,
        db: AsyncSession,
        *,
        workspace_id: UUID,
        user: User,
        provider: str,
    ) -> tuple[str, str]:
        prov = provider.strip().lower()
        if prov not in supported_providers():
            raise ValueError(f"unsupported provider `{provider}`")

        app = resolve_composio_app(prov)
        if app is None:
            raise ValueError(f"unable to resolve Composio app for `{provider}`")

        entity_id = f"egpt-{workspace_id}-{user.id}"
        state_token = secrets.token_urlsafe(32)
        callback_url = (
            f"{self._callback_base_url()}/api/v1/integrations/callback"
            f"?state={state_token}"
        )

        toolset = self._toolset(entity_id=entity_id)
        conn = toolset.initiate_connection(app=app, redirect_url=callback_url)

        redirect = conn.redirectUrl or ""
        if not redirect:
            raise RuntimeError("Composio did not return a redirect URL")

        pending = Integration(
            workspace_id=workspace_id,
            user_id=user.id,
            provider=prov,
            composio_entity_id=entity_id,
            composio_connection_id=conn.connectedAccountId,
            status=IntegrationStatus.PENDING,
            scopes=[],
        )
        db.add(pending)
        await db.flush()

        payload = {
            "workspace_id": str(workspace_id),
            "user_id": str(user.id),
            "provider": prov,
            "integration_id": str(pending.id),
            "entity_id": entity_id,
        }
        await self._redis.set(
            f"{OAUTH_STATE_PREFIX}{state_token}",
            json.dumps(payload),
            ex=OAUTH_STATE_TTL_SECONDS,
        )

        await db.commit()
        await db.refresh(pending)
        return redirect, state_token

    async def handle_oauth_callback(
        self,
        db: AsyncSession,
        *,
        status: str | None,
        connected_account_id: str | None,
        state: str | None,
    ) -> Integration:
        if not state:
            raise OAuthStateError("missing state")
        raw = await self._redis.get(f"{OAUTH_STATE_PREFIX}{state}")
        if not raw:
            raise OAuthStateError("invalid or expired state")
        await self._redis.delete(f"{OAUTH_STATE_PREFIX}{state}")

        try:
            meta = json.loads(raw if isinstance(raw, str) else raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OAuthStateError("corrupt state payload") from exc

        integration_id = UUID(meta["integration_id"])
        workspace_id = UUID(meta["workspace_id"])

        stmt = select(Integration).where(Integration.id == integration_id)
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise OAuthStateError("integration not found")

        if status and status.lower() != "success":
            row.status = IntegrationStatus.ERROR
            await db.commit()
            await db.refresh(row)
            return row

        ca_id = connected_account_id or row.composio_connection_id
        toolset = self._toolset(entity_id=row.composio_entity_id)
        account = toolset.client.connected_accounts.get(connection_id=ca_id)

        from datetime import UTC, datetime

        row.status = IntegrationStatus.CONNECTED
        row.composio_connection_id = account.id
        row.connected_at = datetime.now(UTC)
        row.scopes = []

        await db.commit()
        await db.refresh(row)
        await self._registry.invalidate_workspace_tool_cache(workspace_id)
        return row

    async def revoke_integration(
        self,
        db: AsyncSession,
        *,
        integration_id: UUID,
        workspace_id: UUID,
    ) -> None:
        stmt = select(Integration).where(
            Integration.id == integration_id,
            Integration.workspace_id == workspace_id,
        )
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ValueError("integration not found")

        row.status = IntegrationStatus.REVOKED
        await db.commit()
        await self._registry.invalidate_workspace_tool_cache(workspace_id)


__all__ = ["OAuthService", "OAuthStateError", "OAUTH_STATE_PREFIX"]
