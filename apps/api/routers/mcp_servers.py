"""CRUD endpoints for per-workspace MCP server registrations."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.permissions import Permission, require_permission
from core.security import get_current_active_user
from models.user import User
from schemas.mcp_servers import (
    MCPServerCreateRequest,
    MCPServerResponse,
    MCPServerTestResponse,
)
from services.mcp_server_service import (
    MCPServerError,
    create_server,
    delete_server,
    get_server,
    list_servers,
    response_payload,
    test_server,
)
from services.workflow_service import ensure_workspace_membership

router = APIRouter(prefix="/mcp-servers", tags=["mcp-servers"])


@router.get(
    "",
    response_model=list[MCPServerResponse],
    dependencies=[require_permission(Permission.WORKFLOW_READ)],
)
async def list_mcp_servers(
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> list[MCPServerResponse]:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    rows = await list_servers(db, workspace_id)
    return [MCPServerResponse.model_validate(response_payload(r)) for r in rows]


@router.post(
    "",
    response_model=MCPServerResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_permission(Permission.WORKSPACE_MANAGE)],
)
async def create_mcp_server(
    body: MCPServerCreateRequest,
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> MCPServerResponse:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    try:
        row = await create_server(
            db,
            workspace_id=workspace_id,
            created_by_id=user.id,
            name=body.name,
            url=str(body.url),
            transport=body.transport,
            auth_header_name=body.auth_header_name,
            auth_header_value=body.auth_header_value,
            extra_headers=body.extra_headers,
        )
    except MCPServerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MCPServerResponse.model_validate(response_payload(row))


@router.post(
    "/{server_id}/test",
    response_model=MCPServerTestResponse,
    dependencies=[require_permission(Permission.WORKSPACE_MANAGE)],
)
async def test_mcp_server(
    server_id: UUID,
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> MCPServerTestResponse:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    row = await get_server(db, workspace_id, server_id)
    if row is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    ok, msg, count, sample = await test_server(db, row=row)
    return MCPServerTestResponse(
        success=ok, message=msg, tool_count=count, sample_tool_names=sample,
    )


@router.delete(
    "/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=[require_permission(Permission.WORKSPACE_MANAGE)],
)
async def delete_mcp_server(
    server_id: UUID,
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> None:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    removed = await delete_server(db, workspace_id=workspace_id, server_id=server_id)
    if not removed:
        raise HTTPException(status_code=404, detail="MCP server not found")
