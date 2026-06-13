"""Composio integration management — OAuth, tool listing, dry-run execution."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.database import get_db
from core.deps import get_tool_registry
from core.permissions import Permission, require_permission
from core.redis import get_redis
from core.security import get_current_active_user
from egpt_mcp.oauth_service import OAuthService, OAuthStateError
from egpt_mcp.provider_apps import supported_providers
from egpt_mcp.tool_registry import ToolExecutionError, ToolRegistry
from models.integration import Integration
from models.user import User
from schemas.integrations import (
    ConnectIntegrationResponse,
    IntegrationResponse,
    OAuthCallbackResponse,
    ToolDefinition,
    ToolTestRequest,
    ToolTestResponse,
    ToolsListResponse,
)
from services.workflow_service import ensure_workspace_membership

router = APIRouter(prefix="/integrations", tags=["integrations"])


def get_oauth_service(
    registry: ToolRegistry = Depends(get_tool_registry),
) -> OAuthService:
    from core.config import get_settings
    from core.redis import get_redis

    return OAuthService(get_settings(), get_redis(), registry)


@router.get(
    "",
    response_model=list[IntegrationResponse],
    dependencies=[require_permission(Permission.WORKFLOW_READ)],
)
async def list_integrations(
    workspace_id: UUID = Query(..., description="Workspace scope"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    registry: ToolRegistry = Depends(get_tool_registry),
) -> list[IntegrationResponse]:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    stmt = select(Integration).where(Integration.workspace_id == workspace_id)
    rows = list((await db.execute(stmt)).scalars().all())

    tools = await registry.get_workspace_tools(db, workspace_id)
    by_provider: dict[str, list[str]] = {}
    for t in tools:
        prov = str(t.get("provider") or "").lower()
        by_provider.setdefault(prov, []).append(str(t["name"]))

    out: list[IntegrationResponse] = []
    for row in rows:
        prov = row.provider.lower()
        out.append(
            IntegrationResponse(
                id=row.id,
                provider=row.provider,
                status=row.status.value,
                scopes=list(row.scopes or []),
                connected_at=row.connected_at,
                last_used=row.last_used,
                available_tools=sorted(by_provider.get(prov, [])),
            )
        )
    return out


@router.post(
    "/{provider}/connect",
    response_model=ConnectIntegrationResponse,
    dependencies=[require_permission(Permission.WORKSPACE_MANAGE)],
)
async def connect_integration(
    provider: str,
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    oauth: OAuthService = Depends(get_oauth_service),
) -> ConnectIntegrationResponse:
    if provider.strip().lower() not in supported_providers():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported provider")

    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    try:
        redirect_url, state_token = await oauth.initiate_connection(
            db,
            workspace_id=workspace_id,
            user=user,
            provider=provider,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return ConnectIntegrationResponse(redirect_url=redirect_url, state_token=state_token)


@router.get("/callback", response_model=OAuthCallbackResponse)
async def oauth_callback_route(
    state: str,
    status: str | None = Query(None),
    connected_account_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    oauth: OAuthService = Depends(get_oauth_service),
) -> OAuthCallbackResponse:
    try:
        row = await oauth.handle_oauth_callback(
            db,
            status=status,
            connected_account_id=connected_account_id,
            state=state,
        )
    except OAuthStateError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return OAuthCallbackResponse(
        status=row.status.value,
        integration_id=row.id,
    )


@router.delete(
    "/{integration_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[require_permission(Permission.WORKSPACE_MANAGE)],
)
async def delete_integration_route(
    integration_id: UUID,
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    oauth: OAuthService = Depends(get_oauth_service),
) -> Response:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    try:
        await oauth.revoke_integration(
            db,
            integration_id=integration_id,
            workspace_id=workspace_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/tools",
    response_model=ToolsListResponse,
    dependencies=[require_permission(Permission.WORKFLOW_READ)],
)
async def list_workspace_tools_route(
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    registry: ToolRegistry = Depends(get_tool_registry),
) -> ToolsListResponse:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)

    seen: set[str] = set()
    tools: list[ToolDefinition] = []

    # Preferred: MCP catalogs (talks the wire protocol, no SDK shim).
    # Pulls tools from every workspace-registered MCP server, plus the
    # env-configured fallback. What the interpreter sees here is exactly
    # what action_runner will invoke at runtime.
    from egpt_mcp.mcp_tool_registry import MCPToolError, MCPToolRegistry
    from services.mcp_server_service import list_servers, to_server_config

    mcp_registries: list[MCPToolRegistry] = []
    try:
        ws_servers = await list_servers(db, workspace_id)
        for row in ws_servers:
            try:
                mcp_registries.append(
                    MCPToolRegistry(
                        get_settings(), get_redis(),
                        server_config=to_server_config(row),
                    )
                )
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass

    env_registry = MCPToolRegistry(get_settings(), get_redis())
    if env_registry._is_enabled():  # type: ignore[attr-defined]
        mcp_registries.append(env_registry)

    for reg in mcp_registries:
        try:
            mcp_tools = await reg.list_tools()
        except MCPToolError:
            continue
        except Exception:  # noqa: BLE001 — never block tool listing on MCP outage
            log_module = __import__("logging").getLogger(__name__)
            log_module.warning("mcp.list_tools.endpoint_failed", exc_info=True)
            continue
        for t in mcp_tools:
            name = str(t.get("name") or "")
            if not name or name in seen:
                continue
            seen.add(name)
            tools.append(
                ToolDefinition(
                    name=name,
                    description=t.get("description") or "",
                    provider=t.get("provider") or "",
                    parameters=t.get("parameters") or {},
                )
            )

    # Legacy Composio catalog (kept as a fallback while the MCP migration
    # lands — will be removed once everyone is on MCP).
    raw = await registry.get_workspace_tools(db, workspace_id)
    for t in raw:
        name = str(t["name"])
        if name in seen:
            continue
        seen.add(name)
        tools.append(
            ToolDefinition(
                name=name,
                description=t.get("description") or "",
                provider=t.get("provider") or "",
                parameters=t.get("parameters") or {},
            )
        )

    # Native Dynamiq tools that the workspace has connections for. The interpreter
    # uses this list to know which tool slugs an agent may emit.
    from agents.native_tool_factory import load_workspace_connections
    from agents.native_providers import get_provider as _get_native_provider

    native_conns = await load_workspace_connections(db, workspace_id=workspace_id)
    for row in native_conns:
        provider = _get_native_provider(row.provider)
        if not provider:
            continue
        for slug in provider.tool_slugs:
            if slug in seen:
                continue
            seen.add(slug)
            tools.append(
                ToolDefinition(
                    name=slug,
                    description=provider.description,
                    provider=f"native:{provider.id}",
                    parameters={},
                )
            )

    return ToolsListResponse(tools=tools)


@router.post(
    "/tools/test",
    response_model=ToolTestResponse,
    dependencies=[require_permission(Permission.WORKFLOW_RUN)],
)
async def test_tool_route(
    body: ToolTestRequest,
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    registry: ToolRegistry = Depends(get_tool_registry),
) -> ToolTestResponse:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    try:
        result = await registry.execute_tool(
            db,
            tool_name=body.tool_name,
            params=body.params,
            workspace_id=workspace_id,
            execution_id=None,
            tool_run_buffer=None,
        )
    except ToolExecutionError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return ToolTestResponse(result=result)
