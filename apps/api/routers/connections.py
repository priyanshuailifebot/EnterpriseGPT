"""Native Dynamiq connections API (Phase A + B + C)."""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.native_providers import get_provider
from core.database import get_db
from core.permissions import Permission, require_permission
from core.redis import get_redis
from core.security import get_current_active_user
from models.native_connection import (
    NativeConnection,
    NativeConnectionAuthType,
    NativeConnectionStatus,
)
from models.user import User
from schemas.native_connections import (
    ConnectionCreateRequest,
    ConnectionPatchRequest,
    ConnectionResponse,
    ConnectionTestResponse,
    ProviderCatalogResponse,
)
from services.native_connection_service import (
    NativeConnectionError,
    _serialize_config,  # type: ignore[reportPrivateUsage]
    create_connection,
    decode_config,
    delete_connection,
    list_connections,
    public_provider_catalog,
    record_test_result,
    test_connection,
    update_connection,
)
from services.oauth2_service import (
    OAuthError,
    OAuthNotConfigured,
    build_authorize_url,
    consume_state,
    exchange_code,
    get_oauth_provider,
    stash_state,
)
from services.workflow_service import ensure_workspace_membership

router = APIRouter(prefix="/connections", tags=["connections"])


def _to_response(row: NativeConnection) -> ConnectionResponse:
    provider = get_provider(row.provider)
    return ConnectionResponse(
        id=row.id,
        workspace_id=row.workspace_id,
        provider=row.provider,
        name=row.name,
        auth_type=row.auth_type.value,
        status=row.status.value,
        tool_slugs=list(provider.tool_slugs) if provider else [],
        last_test_at=row.last_test_at,
        last_test_error=row.last_test_error,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get(
    "/providers",
    response_model=ProviderCatalogResponse,
    dependencies=[require_permission(Permission.WORKFLOW_READ)],
)
async def list_providers_route() -> ProviderCatalogResponse:
    return ProviderCatalogResponse(providers=public_provider_catalog())


@router.get(
    "",
    response_model=list[ConnectionResponse],
    dependencies=[require_permission(Permission.WORKFLOW_READ)],
)
async def list_route(
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> list[ConnectionResponse]:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    rows = await list_connections(db, workspace_id=workspace_id)
    return [_to_response(r) for r in rows]


@router.post(
    "",
    response_model=ConnectionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_permission(Permission.WORKSPACE_MANAGE)],
)
async def create_route(
    body: ConnectionCreateRequest,
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> ConnectionResponse:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    try:
        row = await create_connection(
            db,
            workspace_id=workspace_id,
            user_id=user.id,
            provider_id=body.provider,
            name=body.name,
            config=body.config,
        )
    except NativeConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(row)
    return _to_response(row)


async def _load_row(
    db: AsyncSession, *, connection_id: UUID, workspace_id: UUID
) -> NativeConnection:
    res = await db.execute(
        select(NativeConnection).where(
            NativeConnection.id == connection_id,
            NativeConnection.workspace_id == workspace_id,
        )
    )
    row = res.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="connection not found")
    return row


@router.patch(
    "/{connection_id}",
    response_model=ConnectionResponse,
    dependencies=[require_permission(Permission.WORKSPACE_MANAGE)],
)
async def patch_route(
    connection_id: UUID,
    body: ConnectionPatchRequest,
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> ConnectionResponse:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    row = await _load_row(db, connection_id=connection_id, workspace_id=workspace_id)
    try:
        row = await update_connection(
            db, row=row, config_patch=body.config, name=body.name
        )
    except NativeConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(row)
    return _to_response(row)


@router.delete(
    "/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[require_permission(Permission.WORKSPACE_MANAGE)],
)
async def delete_route(
    connection_id: UUID,
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> Response:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    row = await _load_row(db, connection_id=connection_id, workspace_id=workspace_id)
    await delete_connection(db, row=row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{connection_id}/test",
    response_model=ConnectionTestResponse,
    dependencies=[require_permission(Permission.WORKFLOW_RUN)],
)
async def test_route(
    connection_id: UUID,
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> ConnectionTestResponse:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    row = await _load_row(db, connection_id=connection_id, workspace_id=workspace_id)
    # Probe is sync httpx — run off the event loop so we don't stall the server.
    success, message = await asyncio.to_thread(test_connection, row)
    await record_test_result(db, row=row, success=success, message=message)
    await db.commit()
    return ConnectionTestResponse(success=success, message=message)


# ---------------------------------------------------------------------------
# OAuth2 flows (Phase B) — Gmail / Slack / Jira
# ---------------------------------------------------------------------------


class OAuthAuthorizeResponse(BaseModel):
    redirect_url: str
    state: str


class OAuthCallbackBody(BaseModel):
    state: str
    code: str


class OAuthCallbackResponse(BaseModel):
    connection_id: UUID
    provider: str
    status: str


@router.post(
    "/oauth/{provider_id}/authorize",
    response_model=OAuthAuthorizeResponse,
    dependencies=[require_permission(Permission.WORKSPACE_MANAGE)],
)
async def oauth_authorize_route(
    provider_id: str,
    workspace_id: UUID = Query(...),
    connection_name: str = Query(..., min_length=1, max_length=128),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> OAuthAuthorizeResponse:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    provider = get_oauth_provider(provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="unknown OAuth provider")
    try:
        redis = get_redis()
        state = await stash_state(
            redis,
            workspace_id=workspace_id,
            user_id=user.id,
            provider=provider.id,
            connection_name=connection_name,
        )
        url = build_authorize_url(provider, state)
    except OAuthNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OAuthAuthorizeResponse(redirect_url=url, state=state)


@router.post(
    "/oauth/callback",
    response_model=OAuthCallbackResponse,
    dependencies=[require_permission(Permission.WORKSPACE_MANAGE)],
)
async def oauth_callback_route(
    body: OAuthCallbackBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> OAuthCallbackResponse:
    redis = get_redis()
    try:
        state = await consume_state(redis, body.state)
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if state["user_id"] != str(user.id):
        # Bind the callback to the user who initiated it.
        raise HTTPException(status_code=403, detail="state does not match user")

    provider = get_oauth_provider(state["provider"])
    if not provider:
        raise HTTPException(status_code=400, detail="unknown OAuth provider")

    try:
        creds = await exchange_code(provider, body.code)
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Atlassian needs a follow-up call to discover the cloud_id once.
    if provider.id == "jira" and creds.get("access_token"):
        creds["cloud_id"] = await _discover_jira_cloud_id(creds["access_token"])

    workspace_id = UUID(state["workspace_id"])
    connection_name = state["connection_name"]

    # Upsert: if a connection with this (workspace, provider, name) exists,
    # replace its credentials; otherwise create a new row.
    existing_q = await db.execute(
        select(NativeConnection).where(
            NativeConnection.workspace_id == workspace_id,
            NativeConnection.provider == provider.id,
            NativeConnection.name == connection_name,
        )
    )
    row = existing_q.scalar_one_or_none()
    if row is None:
        row = NativeConnection(
            workspace_id=workspace_id,
            created_by_id=user.id,
            provider=provider.id,
            name=connection_name,
            auth_type=NativeConnectionAuthType.OAUTH2,
            status=NativeConnectionStatus.ACTIVE,
            config_encrypted=_serialize_config(creds),
        )
        db.add(row)
    else:
        row.config_encrypted = _serialize_config(creds)
        row.status = NativeConnectionStatus.ACTIVE
        row.last_test_error = None
    await db.commit()
    await db.refresh(row)
    return OAuthCallbackResponse(
        connection_id=row.id, provider=row.provider, status=row.status.value
    )


async def _discover_jira_cloud_id(access_token: str) -> str | None:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.atlassian.com/oauth/token/accessible-resources",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code >= 400:
                return None
            items = resp.json()
            if isinstance(items, list) and items:
                return str(items[0].get("id") or "") or None
    except Exception:  # noqa: BLE001
        return None
    return None
