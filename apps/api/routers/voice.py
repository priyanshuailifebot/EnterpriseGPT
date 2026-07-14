"""Retell call-ended webhook (P9).

When a Retell voice interview finishes, Retell POSTs here. We look up the
``call_id → {workspace, target trigger slug, candidate ctx}`` mapping that the
interview-start workflow registered (via ``internal.register_voice_route``) and
fire the scoring workflow, correlated by ``candidate_id``. This is the async
boundary that lets the interview run outside any execution (event-boundary
architecture — see docs/RECRUITMENT_WORKFLOW_PLAN.md §0.6).

Point your Retell agent's webhook at ``POST /api/v1/voice/retell/callback`` and
set ``RETELL_WEBHOOK_SECRET`` (sent as the ``X-Retell-Secret`` header).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.database import get_db
from core.redis import get_redis
from models.user import User
from models.workflow import Workflow, WorkflowStatus
from models.workflow_version import WorkflowVersion
from schemas.workflow import TriggerNode, WorkflowDefinition
from services.workflow_service import WorkflowService

router = APIRouter(prefix="/voice", tags=["voice"])

# Must match agents.action_runner._VOICE_ROUTE_KEY.
_ACCEPTED_EVENTS = {"call_ended", "call_analyzed"}


def _route_key(call_id: str) -> str:
    return f"egpt:voice:route:{call_id}"


async def _resolve_by_trigger_slug(
    db: AsyncSession, workspace_id: UUID, slug: str
) -> Workflow | None:
    """Find the published workflow in ``workspace_id`` whose live definition has
    a webhook trigger with ``slug`` (workspace-scoped sibling reference)."""
    rows = list(
        (
            await db.execute(
                select(Workflow).where(
                    Workflow.workspace_id == workspace_id,
                    Workflow.status == WorkflowStatus.PUBLISHED,
                    Workflow.deleted_at.is_(None),
                    Workflow.published_version_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for wf in rows:
        version = await db.get(WorkflowVersion, wf.published_version_id)
        if version is None:
            continue
        try:
            wd = WorkflowDefinition.model_validate(version.definition)
        except Exception:  # noqa: BLE001
            continue
        for node in wd.iter_nodes():
            if (
                isinstance(node, TriggerNode)
                and node.trigger_type == "webhook"
                and node.slug == slug
            ):
                return wf
    return None


@router.post("/retell/callback", include_in_schema=True)
async def retell_callback(
    payload: dict[str, Any] = Body(default_factory=dict),
    x_retell_secret: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    settings = get_settings()
    if not settings.RETELL_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="voice callback disabled",
        )
    if x_retell_secret != settings.RETELL_WEBHOOK_SECRET:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad secret")

    event = str(payload.get("event") or payload.get("type") or "")
    if event and event not in _ACCEPTED_EVENTS:
        return JSONResponse({"status": "ignored", "event": event})
    call = payload.get("call") if isinstance(payload.get("call"), dict) else payload
    call_id = str(call.get("call_id") or payload.get("call_id") or "").strip()
    if not call_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="missing call_id")

    raw = await get_redis().get(_route_key(call_id))
    if not raw:
        return JSONResponse({"status": "no_route", "call_id": call_id})
    record = json.loads(raw if isinstance(raw, str) else raw.decode())
    workspace_id = UUID(str(record["workspace_id"]))
    target_slug = str(record["target_slug"])
    ctx = record.get("ctx") if isinstance(record.get("ctx"), dict) else {}

    wf = await _resolve_by_trigger_slug(db, workspace_id, target_slug)
    if wf is None:
        return JSONResponse(
            {"status": "workflow_not_found", "target_slug": target_slug},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    owner = (
        await db.execute(select(User).where(User.id == wf.created_by))
    ).scalar_one_or_none()
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="workflow owner no longer exists",
        )

    error: str | None = None
    async for evt in WorkflowService(settings).execute_workflow(
        db,
        user=owner,
        workflow_id=wf.id,
        request_input={"call_id": call_id, **ctx},
        variables={},
        demo=False,
    ):
        if evt.get("type") == "error":
            error = str(evt.get("message") or "workflow failed")

    await get_redis().delete(_route_key(call_id))  # one-shot
    return JSONResponse(
        {
            "status": "error" if error else "ok",
            "workflow_id": str(wf.id),
            "call_id": call_id,
            "error": error,
        },
        status_code=(
            status.HTTP_500_INTERNAL_SERVER_ERROR if error else status.HTTP_200_OK
        ),
    )
