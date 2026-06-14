"""Workflow CRUD, NL interpret (clarification + preview), SSE execution, HITL."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any, Union
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agents.langgraph.service import LangGraphService
from core.config import get_settings
from core.database import get_db
from core.deps import get_tool_registry
from core.permissions import Permission, require_permission
from core.security import get_current_active_user
from egpt_mcp.tool_registry import ToolRegistry
from models.user import User
from models.workflow import Workflow as WFRow
from models.workflow_version import WorkflowVersion
from schemas.workflow import TriggerNode, WorkflowDefinition
from schemas.workflow import (
    AugmentRequest,
    AugmentResponse,
    ExecutionRequest,
    HITLApprovalBody,
    InterpretRequest,
    NeedsClarificationResponse,
    NodeSummaryRequest,
    NodeSummaryResponse,
    ReadyResponse,
    WorkflowCreateBody,
    WorkflowDetailOut,
    WorkflowListOut,
    WorkflowRenameBody,
    WorkflowRequirementsRequest,
    WorkflowRequirementsResponse,
    WorkflowSummaryOut,
    WorkflowUpdateBody,
    WorkflowVersionOut,
)
from services.clarification_service import ClarificationService
from services.workflow_service import WorkflowService

router = APIRouter(prefix="/workflows", tags=["workflows"])


def get_langgraph_service() -> LangGraphService:
    return LangGraphService(get_settings())


def get_clarification_service(
    lg: LangGraphService = Depends(get_langgraph_service),
) -> ClarificationService:
    return ClarificationService(get_settings(), langgraph_service=lg)


def get_workflow_service(
    clarification: ClarificationService = Depends(get_clarification_service),
    registry: ToolRegistry = Depends(get_tool_registry),
) -> WorkflowService:
    return WorkflowService(get_settings(), clarification_service=clarification, tool_registry=registry)


def _sse_pack(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, default=str)}\n\n"


async def _merge_heartbeat_stream(
    producer: AsyncIterator[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    sentinel = object()
    queue: asyncio.Queue = asyncio.Queue(maxsize=512)

    async def pump() -> None:
        try:
            async for item in producer:
                await queue.put(item)
        finally:
            await queue.put(sentinel)

    async def pulse() -> None:
        try:
            while True:
                await asyncio.sleep(15)
                await queue.put({"type": "heartbeat"})
        except asyncio.CancelledError:
            return

    task_p = asyncio.create_task(pump())
    task_h = asyncio.create_task(pulse())
    try:
        while True:
            evt = await queue.get()
            if evt is sentinel:
                break
            yield evt
    finally:
        task_h.cancel()
        await asyncio.gather(task_h, return_exceptions=True)
        await task_p


@router.post(
    "/interpret",
    response_model=Union[NeedsClarificationResponse, ReadyResponse],
    status_code=status.HTTP_200_OK,
    dependencies=[require_permission(Permission.WORKFLOW_CREATE)],
)
async def interpret_workflow_route(
    body: InterpretRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    service: WorkflowService = Depends(get_workflow_service),
) -> NeedsClarificationResponse | ReadyResponse:
    """NL → preview graph with optional LangGraph checkpoint-backed clarification rounds."""
    return await service.interpret_and_preview(db, user=user, request=body)


@router.get(
    "/checkpoint-state/{thread_id}",
    dependencies=[require_permission(Permission.WORKFLOW_READ)],
)
async def workflow_checkpoint_state_route(
    thread_id: str,
    user: User = Depends(get_current_active_user),
    lg: LangGraphService = Depends(get_langgraph_service),
) -> dict[str, Any]:
    """Return LangGraph-serialized execution state for ``thread_id`` (usually execution UUID)."""
    _ = user
    snapshot = await lg.get_checkpoint_state(thread_id)
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="checkpoint not found")
    return snapshot


@router.get(
    "/pending-hitl",
    dependencies=[require_permission(Permission.WORKFLOW_READ)],
)
async def pending_hitl_route(
    workspace_id: Annotated[UUID, Query()],
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    lg: LangGraphService = Depends(get_langgraph_service),
) -> dict[str, Any]:
    from services.workflow_service import ensure_workspace_membership

    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    items = await lg.list_pending_hitl(db, workspace_id=workspace_id)
    return {"items": items, "workspace_id": str(workspace_id)}


@router.post(
    "/",
    dependencies=[require_permission(Permission.WORKFLOW_CREATE)],
    status_code=status.HTTP_201_CREATED,
    response_model=WorkflowSummaryOut,
)
async def create_workflow_route(
    body: WorkflowCreateBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowSummaryOut:
    row = await service.create_workflow(db, user=user, body=body)
    return WorkflowSummaryOut.model_validate(row)


@router.get(
    "/",
    dependencies=[require_permission(Permission.WORKFLOW_READ)],
    response_model=WorkflowListOut,
)
async def list_workflows_route(
    workspace_id: Annotated[UUID | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowListOut:
    filt = [workspace_id] if workspace_id else None
    rows, total = await service.list_workflows(
        db,
        user=user,
        workspace_ids=filt,
        page=page,
        page_size=page_size,
    )
    return WorkflowListOut(
        items=[WorkflowSummaryOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{workflow_id}",
    dependencies=[require_permission(Permission.WORKFLOW_READ)],
    response_model=WorkflowDetailOut,
)
async def get_workflow_detail(
    workflow_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowDetailOut:
    detail = await service.get_detail(db, user=user, workflow_id=workflow_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    row, versions = detail
    return WorkflowDetailOut(
        workflow=WorkflowSummaryOut.model_validate(row),
        versions=[WorkflowVersionOut.model_validate(v) for v in versions],
    )


@router.put(
    "/{workflow_id}",
    dependencies=[require_permission(Permission.WORKFLOW_CREATE)],
    response_model=WorkflowSummaryOut,
)
async def update_workflow_route(
    workflow_id: UUID,
    body: WorkflowUpdateBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowSummaryOut:
    row = await service.update_workflow(db, user=user, workflow_id=workflow_id, body=body)
    return WorkflowSummaryOut.model_validate(row)


@router.patch(
    "/{workflow_id}",
    dependencies=[require_permission(Permission.WORKFLOW_CREATE)],
    response_model=WorkflowSummaryOut,
)
async def rename_workflow_route(
    workflow_id: UUID,
    body: WorkflowRenameBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowSummaryOut:
    """Rename only — no new version, no publish-state change."""
    row = await service.rename_workflow(
        db, user=user, workflow_id=workflow_id, name=body.name
    )
    return WorkflowSummaryOut.model_validate(row)


@router.post(
    "/{workflow_id}/publish",
    dependencies=[require_permission(Permission.WORKFLOW_CREATE)],
    response_model=WorkflowSummaryOut,
)
async def publish_workflow_route(
    workflow_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowSummaryOut:
    """Promote the current version to live. Requires a passing test run; 409 otherwise."""
    row = await service.publish_workflow(db, user=user, workflow_id=workflow_id)
    return WorkflowSummaryOut.model_validate(row)


@router.post(
    "/{workflow_id}/unpublish",
    dependencies=[require_permission(Permission.WORKFLOW_CREATE)],
    response_model=WorkflowSummaryOut,
)
async def unpublish_workflow_route(
    workflow_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowSummaryOut:
    """Take the workflow back to draft — live runs revert to previews."""
    row = await service.unpublish_workflow(db, user=user, workflow_id=workflow_id)
    return WorkflowSummaryOut.model_validate(row)


@router.get(
    "/{workflow_id}/sample_input",
    dependencies=[require_permission(Permission.WORKFLOW_RUN)],
)
async def workflow_sample_input_route(
    workflow_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    service: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    """Trigger-aware stub payload used to pre-fill the Test panel.

    Mirrors n8n's "Generate sample input" affordance — the shape returned
    here is suitable for ``ExecutionRequest.input_data`` and reflects the
    workflow's trigger (chat → ``{message}``, webhook → event envelope,
    form → values keyed by ``form_fields``, schedule → timestamp stub).
    """
    payload = await service.sample_input_for_workflow(
        db, user=user, workflow_id=workflow_id
    )
    return {"input_data": payload}


@router.post(
    "/{workflow_id}/augment",
    dependencies=[require_permission(Permission.WORKFLOW_CREATE)],
    response_model=AugmentResponse,
    status_code=status.HTTP_200_OK,
)
async def augment_workflow_route(
    workflow_id: UUID,
    body: AugmentRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    service: WorkflowService = Depends(get_workflow_service),
) -> AugmentResponse:
    """Propose an NL-driven modification of ``current_definition``.

    Does NOT persist — callers preview the result in the visual editor
    and then POST a PUT to save. This separation lets users iterate
    several augment steps without polluting the version history.
    """
    proposed, changes = await service.augment_definition(
        db,
        user=user,
        workflow_id=workflow_id,
        message=body.message,
        current_definition=body.current_definition,
        focus_node_id=body.focus_node_id,
    )
    return AugmentResponse(proposed_definition=proposed, changes=changes)


@router.post(
    "/{workflow_id}/nodes/{node_id}/summary",
    dependencies=[require_permission(Permission.WORKFLOW_READ)],
    response_model=NodeSummaryResponse,
    status_code=status.HTTP_200_OK,
)
async def summarize_node_route(
    workflow_id: UUID,
    node_id: str,
    body: NodeSummaryRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    service: WorkflowService = Depends(get_workflow_service),
) -> NodeSummaryResponse:
    """Return an LLM-generated plain-English explanation of one node.

    Operates on the definition supplied in the body (which may contain
    unsaved canvas edits) so the summary matches what the user sees.
    Results are cached per node-version, so re-requesting an unchanged node
    is free.
    """
    summary, cached = await service.summarize_node(
        db,
        user=user,
        workflow_id=workflow_id,
        node_id=node_id,
        definition=body.definition,
    )
    return NodeSummaryResponse(summary=summary, cached=cached)


@router.post(
    "/{workflow_id}/requirements",
    dependencies=[require_permission(Permission.WORKFLOW_READ)],
    response_model=WorkflowRequirementsResponse,
    status_code=status.HTTP_200_OK,
)
async def workflow_requirements_route(
    workflow_id: UUID,
    body: WorkflowRequirementsRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowRequirementsResponse:
    """List the external integrations this workflow needs + their live status.

    Evaluates the definition in the body (so the editor can include unsaved
    edits). The same evaluation backs the server-side publish gate.
    """
    requirements, missing = await service.workflow_requirements(
        db,
        user=user,
        workflow_id=workflow_id,
        definition=body.definition,
    )
    return WorkflowRequirementsResponse(
        requirements=requirements,
        missing_required=missing,
        publishable=len(missing) == 0,
    )


@router.delete(
    "/{workflow_id}",
    dependencies=[require_permission(Permission.WORKFLOW_DELETE)],
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_workflow_route(
    workflow_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    service: WorkflowService = Depends(get_workflow_service),
) -> Response:
    await service.soft_delete(db, user=user, workflow_id=workflow_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{workflow_id}/executions",
    dependencies=[require_permission(Permission.WORKFLOW_READ)],
)
async def list_executions_route(
    workflow_id: UUID,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    service: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    rows, total = await service.list_executions(
        db, user=user, workflow_id=workflow_id, page=page, page_size=page_size
    )
    payload = []
    for ex in rows:
        payload.append(
            {
                "id": str(ex.id),
                "status": ex.status.value if hasattr(ex.status, "value") else str(ex.status),
                "started_at": ex.started_at.isoformat() if ex.started_at else None,
                "completed_at": ex.completed_at.isoformat() if ex.completed_at else None,
                "duration_ms": ex.duration_ms,
                "error_message": ex.error_message,
            }
        )
    return {"items": payload, "total": total, "page": page, "page_size": page_size}


@router.get(
    "/{workflow_id}/executions/{execution_id}/steps",
    dependencies=[require_permission(Permission.WORKFLOW_READ)],
)
async def list_execution_steps_route(
    workflow_id: UUID,
    execution_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    service: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    """Per-node step records for one run — powers the test-run inspector."""
    steps = await service.get_execution_steps(
        db, user=user, workflow_id=workflow_id, execution_id=execution_id
    )
    items = [
        {
            "id": str(s.id),
            "step_index": s.step_index,
            "node_id": s.node_id,
            "node_name": s.node_name,
            "node_kind": s.node_kind,
            "status": s.status.value if hasattr(s.status, "value") else str(s.status),
            "dry_run": s.dry_run,
            "demo": s.demo,
            "input_snapshot": s.input_snapshot,
            "output_snapshot": s.output_snapshot,
            "error_message": s.error_message,
            "duration_ms": s.duration_ms,
            "completed_at": s.completed_at.isoformat() if s.completed_at else None,
        }
        for s in steps
    ]
    return {"items": items}


@router.post(
    "/{workflow_id}/executions/{execution_id}/approve",
    dependencies=[require_permission(Permission.WORKFLOW_RUN)],
    status_code=status.HTTP_202_ACCEPTED,
)
async def approve_hitl_route(
    workflow_id: UUID,
    execution_id: UUID,
    body: HITLApprovalBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    service: WorkflowService = Depends(get_workflow_service),
) -> dict[str, str]:
    await service.approve_hitl(
        db,
        user=user,
        workflow_id=workflow_id,
        execution_id=execution_id,
        approved=body.approved,
        feedback=body.feedback,
    )
    return {"detail": "approval recorded"}


@router.get("/templates", status_code=status.HTTP_200_OK)
async def list_templates_route(
    user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Curated workflow templates the user can one-click into their workspace.

    Public to any authenticated user — templates are read-only and contain no
    secrets. Each entry includes the original NL ``prompt`` (so users who
    want a deeper customise step can pipe it through ``/interpret``) and a
    fully-baked v2 ``definition`` they can save as-is via ``POST /workflows``.
    """
    from services.workflow_templates import public_catalog

    _ = user
    return {"templates": public_catalog()}


class WebhookResumeBody(BaseModel):
    """Free-form payload posted to ``resume/{token}``.

    Public endpoint by design — workflow authors mint these URLs and embed
    them in emails or candidate-facing pages. Security is via the random
    token (24 url-safe bytes, ~190 bits). Bodies are stored verbatim and
    surfaced as the parked node's output.
    """

    model_config = ConfigDict(extra="allow")


@router.post(
    "/executions/{execution_id}/resume/{token}",
    status_code=status.HTTP_202_ACCEPTED,
)
async def resume_webhook_route(
    execution_id: UUID,
    token: str,
    payload: WebhookResumeBody = Body(default_factory=WebhookResumeBody),
) -> dict[str, Any]:
    """Resume a parked ``wait_for_webhook`` node with an arbitrary JSON body.

    Unauthenticated by design — the token (192-bit, generated at park time)
    is the capability. Tokens expire with the node's configured timeout.
    """
    from agents.extended_executor import _resume_token_key, _resume_payload_key
    from core.redis import get_redis

    redis = get_redis()
    raw = await redis.get(_resume_token_key(token))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="resume token invalid or expired",
        )
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="corrupted park record",
        ) from None
    if str(meta.get("execution_id")) != str(execution_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="token does not belong to this execution",
        )
    node_id = str(meta.get("node_id") or "")
    if not node_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="park record missing node_id",
        )
    body_dump = payload.model_dump()
    await redis.set(
        _resume_payload_key(execution_id, node_id),
        json.dumps(body_dump),
        ex=3600,
    )
    await redis.delete(_resume_token_key(token))
    return {
        "detail": "resumed",
        "execution_id": str(execution_id),
        "node_id": node_id,
    }


@router.post(
    "/{workflow_id}/execute",
    dependencies=[require_permission(Permission.WORKFLOW_RUN)],
)
async def execute_workflow_route(
    workflow_id: UUID,
    body: ExecutionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    service: WorkflowService = Depends(get_workflow_service),
) -> StreamingResponse:
    base_gen = service.execute_workflow(
        db,
        user=user,
        workflow_id=workflow_id,
        request_input=body.input_data,
        variables=body.variables,
        demo=body.demo,
        use_real_llm=body.use_real_llm,
        branch_overrides=body.branch_overrides,
    )

    async def event_stream() -> Any:
        merged = _merge_heartbeat_stream(base_gen)
        async for evt in merged:
            yield _sse_pack(evt)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Public webhook trigger — fires a workflow from an external HTTP POST.
# Unauthenticated by design (n8n-style). When ``secret_required`` is set on the
# trigger node, callers must supply ``X-Webhook-Secret`` matching the workflow's
# stored secret. Runs as the workflow's creator so RBAC + tenant scoping stay
# intact downstream.
# ---------------------------------------------------------------------------


def _find_trigger_by_slug(
    wd: WorkflowDefinition, slug: str
) -> TriggerNode | None:
    target = slug.strip().lower()
    for node in wd.nodes or []:
        if not isinstance(node, TriggerNode):
            continue
        candidates = {
            (node.slug or "").lower(),
            node.id.lower(),
        }
        if target in candidates:
            return node
    return None


async def _load_workflow_for_webhook(
    db: AsyncSession, workflow_id: UUID
) -> tuple[WFRow, WorkflowDefinition]:
    row = (
        await db.execute(
            select(WFRow)
            .options(selectinload(WFRow.versions))
            .where(WFRow.id == workflow_id, WFRow.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found"
        )
    latest = (
        await db.execute(
            select(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == row.id)
            .order_by(WorkflowVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no workflow versions"
        )
    wd = WorkflowDefinition.model_validate(latest.definition)
    return row, wd


@router.get("/{workflow_id}/webhook/{slug}", include_in_schema=False)
async def webhook_trigger_info_route(
    workflow_id: UUID,
    slug: str,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Friendly landing page when a user pastes the webhook URL in a browser."""
    try:
        row, wd = await _load_workflow_for_webhook(db, workflow_id)
    except HTTPException as exc:
        return HTMLResponse(
            f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>",
            status_code=exc.status_code,
        )
    trigger = _find_trigger_by_slug(wd, slug)
    if trigger is None:
        return HTMLResponse(
            "<h1>404</h1><p>No webhook trigger matches this slug.</p>",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>{row.name} — webhook trigger</title>
    <style>
      body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
        background: #0f172a; color: #e2e8f0; padding: 48px;
        line-height: 1.55;
      }}
      code {{ background: #1e293b; padding: 2px 6px; border-radius: 4px; }}
      pre {{ background: #1e293b; padding: 16px; border-radius: 8px; overflow:auto; }}
      h1 {{ margin-top: 0; }}
      a {{ color: #60a5fa; }}
    </style>
  </head>
  <body>
    <h1>Webhook trigger ready</h1>
    <p>
      Workflow <strong>{row.name}</strong> · trigger <code>{slug}</code>
    </p>
    <p>
      This endpoint expects an HTTP <code>POST</code> with a JSON body. The body becomes
      the workflow's <code>input_data</code>.
    </p>
    <pre>curl -X POST '{slug}' \\
  -H 'Content-Type: application/json' \\
  -d '{{"message": "Hi, I have a problem with my order"}}'</pre>
    <p style="opacity:.6">
      You're seeing this page because you opened the URL in a browser (which sends a GET).
      Open the workflow's Run page in the EnterpriseGPT UI to fire it manually.
    </p>
  </body>
</html>""",
        status_code=status.HTTP_200_OK,
    )


@router.post(
    "/{workflow_id}/webhook/{slug}",
    status_code=status.HTTP_200_OK,
    include_in_schema=True,
)
async def webhook_trigger_route(
    workflow_id: UUID,
    slug: str,
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
    service: WorkflowService = Depends(get_workflow_service),
) -> JSONResponse:
    """Fire a workflow from a webhook POST (no auth — anyone with the URL).

    Runs as the workflow's creator so tenant scoping + RBAC stay intact for
    downstream actions / RAG / connections. Returns the aggregated outputs
    synchronously — fine for demos and most external integrations; long-running
    flows can switch to ``/execute`` via SSE.
    """
    row, wd = await _load_workflow_for_webhook(db, workflow_id)

    trigger = _find_trigger_by_slug(wd, slug)
    if trigger is None or trigger.trigger_type != "webhook":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no webhook trigger matches this slug",
        )

    if trigger.secret_required:
        provided = request.headers.get("X-Webhook-Secret", "")
        if not provided:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing X-Webhook-Secret header",
            )

    owner = (
        await db.execute(select(User).where(User.id == row.created_by))
    ).scalar_one_or_none()
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="workflow owner no longer exists",
        )

    execution_id: str | None = None
    status_label: str = "running"
    output: Any = None
    error: str | None = None
    events: list[dict[str, Any]] = []

    async for evt in service.execute_workflow(
        db,
        user=owner,
        workflow_id=workflow_id,
        request_input=dict(payload or {}),
        variables={},
        demo=False,
    ):
        et = evt.get("type")
        ev_exec = evt.get("execution_id")
        if ev_exec and not execution_id:
            execution_id = str(ev_exec)
        if et == "workflow_complete":
            status_label = str(evt.get("status") or "complete")
            output = (evt.get("data") or {}).get("outputs", evt.get("data"))
        elif et == "error":
            status_label = "error"
            error = str(evt.get("message") or "workflow failed")
        if et in {
            "workflow_start",
            "workflow_complete",
            "node_complete",
            "node_skipped",
            "action_invoked",
            "error",
        }:
            events.append(evt)

    code = (
        status.HTTP_500_INTERNAL_SERVER_ERROR
        if error
        else status.HTTP_200_OK
    )
    return JSONResponse(
        status_code=code,
        content={
            "workflow_id": str(workflow_id),
            "execution_id": execution_id,
            "status": status_label,
            "output": output,
            "error": error,
            "trigger_slug": slug,
            "events": events,
        },
    )
