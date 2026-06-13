"""LangGraph conversational dialog API (Phase 3)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from agents.langgraph.service import LangGraphService
from core.config import get_settings
from core.database import get_db
from core.permissions import Permission, require_permission
from core.security import get_current_active_user
from models.user import User
from schemas.workflow import DialogTurnBody
from services.workflow_service import ensure_workspace_membership

router = APIRouter(prefix="/dialog", tags=["dialog"])


def get_langgraph_service() -> LangGraphService:
    return LangGraphService(get_settings())


@router.post(
    "/sessions/{session_id}/turn",
    status_code=status.HTTP_200_OK,
    dependencies=[require_permission(Permission.WORKFLOW_READ)],
)
async def dialog_turn_route(
    session_id: str,
    body: DialogTurnBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    lg: LangGraphService = Depends(get_langgraph_service),
) -> dict[str, Any]:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=body.workspace_id)
    _ = user
    last: dict[str, Any] | None = None
    async for evt in lg.run_dialog_turn(
        db=db,
        session_id=session_id,
        user_message=body.message,
        workspace_id=body.workspace_id,
    ):
        last = evt
    if last is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="dialog turn produced no events",
        )
    return last
