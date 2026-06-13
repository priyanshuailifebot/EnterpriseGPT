"""Public chat API — opens sessions, posts messages, inspects state.

These routes are intentionally NOT bound to the standard auth dependency:
chat triggers are public by design (the slug is the discovery mechanism;
an optional shared secret on the TriggerNode is the auth).

Three resource shapes:

* ``POST /api/v1/chat/{trigger_slug}/sessions``
        Open a new session bound to a (workspace_id, workflow_id).
        Returns ``session_id`` and the agent's welcome message.

* ``POST /api/v1/chat/sessions/{session_id}/messages``
        Submit one user message. Returns the assistant's final response
        plus the validated structured output (when an OutputParserNode is
        attached) and per-turn telemetry.

* ``GET  /api/v1/chat/sessions/{session_id}/messages``
        Paginated read of the durable message history (audit trail).

* ``DELETE /api/v1/chat/sessions/{session_id}``
        Close the session + clear its bound memory.

* ``GET  /api/v1/chat/sessions/{session_id}/memory``
        Operator-facing memory inspection (count, ttl, scope).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.chat_runtime import ChatRuntime, RateLimitExceeded
from agents.native_tool_factory import load_workspace_connections
from core.config import get_settings
from core.database import get_db
from core.redis import get_redis
from models.chat_attachment import ChatAttachment
from models.chat_session import (
    ChatMessage,
    ChatSession,
    ChatSessionStatus,
)
from models.human_handoff import HandoffStatus, HumanHandoffQueueItem
from models.workflow import Workflow as WFRow
from models.workflow_version import WorkflowVersion
from schemas.chat import (
    ChatMessageListResponse,
    ChatMessageOut,
    MemoryInspectResponse,
    OpenSessionRequest,
    OpenSessionResponse,
    SendMessageRequest,
    SendMessageResponse,
    SessionListItem,
    SessionListResponse,
    SessionUsageResponse,
)
from services.llm_pricing import microcents_to_cents
from services.pii_service import PIIService
from schemas.workflow import (
    AgentNode,
    MemoryNode,
    TriggerNode,
    WorkflowDefinition,
)
from services.memory_store import MemoryStore

router = APIRouter(prefix="/chat", tags=["chat"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_latest_definition(
    db: AsyncSession, workflow_id: UUID
) -> tuple[WFRow, WorkflowDefinition]:
    wf = (
        await db.execute(
            select(WFRow).where(WFRow.id == workflow_id, WFRow.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if wf is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
    ver = (
        await db.execute(
            select(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == wf.id)
            .order_by(desc(WorkflowVersion.version))
            .limit(1)
        )
    ).scalar_one_or_none()
    if ver is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workflow has no versions",
        )
    try:
        definition = WorkflowDefinition.model_validate(ver.definition)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"workflow definition no longer validates: {exc}",
        ) from exc
    return wf, definition


def _find_chat_trigger(
    definition: WorkflowDefinition, slug: str
) -> TriggerNode:
    for n in definition.iter_nodes():
        if (
            isinstance(n, TriggerNode)
            and n.trigger_type == "chat"
            and n.slug == slug
        ):
            return n
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"workflow has no chat trigger with slug {slug!r}",
    )


def _agent_for_trigger(
    definition: WorkflowDefinition, trigger: TriggerNode
) -> AgentNode:
    """Find the AgentNode the chat trigger feeds into.

    Picks the first downstream agent whose ``depends_on`` references the
    trigger. If multiple agents do — uncommon but legal — picks the first
    in declaration order so behaviour is stable.
    """
    for n in definition.iter_nodes():
        if isinstance(n, AgentNode) and trigger.id in n.depends_on:
            return n
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            f"chat trigger {trigger.slug!r} has no downstream AgentNode — "
            "the workflow is misconfigured"
        ),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/{trigger_slug}/sessions",
    response_model=OpenSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def open_session_route(
    trigger_slug: str,
    body: OpenSessionRequest,
    db: AsyncSession = Depends(get_db),
) -> OpenSessionResponse:
    wf, definition = await _load_latest_definition(db, body.workflow_id)
    if wf.workspace_id != body.workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workflow does not belong to this workspace",
        )
    trigger = _find_chat_trigger(definition, trigger_slug)
    if trigger.secret_required and not body.secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="this chat trigger requires a secret in the request body",
        )
    agent = _agent_for_trigger(definition, trigger)

    sess = ChatSession(
        workspace_id=body.workspace_id,
        workflow_id=body.workflow_id,
        trigger_slug=trigger.slug,
        agent_node_id=agent.id,
        metadata_json=body.metadata or {},
    )
    db.add(sess)
    await db.commit()
    await db.refresh(sess)
    return OpenSessionResponse(
        session_id=sess.id,
        workspace_id=sess.workspace_id,
        workflow_id=sess.workflow_id,
        trigger_slug=sess.trigger_slug,
        agent_node_id=sess.agent_node_id,
        welcome_message=trigger.chat_welcome_message or None,
        created_at=sess.created_at,
    )


@router.post(
    "/sessions/{session_id}/messages",
    response_model=SendMessageResponse,
)
async def send_message_route(
    session_id: UUID,
    body: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
) -> SendMessageResponse:
    sess = (
        await db.execute(select(ChatSession).where(ChatSession.id == session_id))
    ).scalar_one_or_none()
    if sess is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="session not found"
        )
    if sess.status != ChatSessionStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"session is {sess.status.value}",
        )
    _, definition = await _load_latest_definition(db, sess.workflow_id)
    connections = await load_workspace_connections(
        db, workspace_id=sess.workspace_id
    )

    runtime = ChatRuntime(
        settings=get_settings(),
        db=db,
        session=sess,
        workflow_definition=definition,
        workspace_connections=connections,
    )
    try:
        result = await runtime.handle_user_message(body.content)
    except RateLimitExceeded as exc:
        # Map to 429 with the standard ``Retry-After`` header. Snapshot
        # goes in the body so the client can render an informative
        # toast ("you've used 95% of your token budget").
        headers: dict[str, str] = {}
        if exc.decision.retry_after_seconds:
            headers["Retry-After"] = str(exc.decision.retry_after_seconds)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "detail": "rate_limited",
                "reason": exc.decision.reason,
                "retry_after_seconds": exc.decision.retry_after_seconds,
                "snapshot": exc.decision.snapshot,
            },
            headers=headers,
        ) from exc
    return SendMessageResponse(
        session_id=sess.id,
        assistant_text=result.assistant_text,
        structured=result.structured,
        parser_status=(
            "ok" if (result.parser and result.parser.ok) else
            "failed" if result.parser else None
        ),
        parser_error=result.parser.error if result.parser else None,
        tool_call_count=result.tool_call_count,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )


# ---------------------------------------------------------------------------
# Streaming variant — SSE
#
# Wire format follows the standard ``text/event-stream`` spec: one event per
# ``data: <json>\n\n`` block. The frontend uses ``fetch`` + a ReadableStream
# reader to parse this (EventSource doesn't support POST bodies).
#
# Cancellation: when the client disconnects mid-stream the request's
# ``is_disconnected()`` flips and we stop pulling from the runtime
# generator — that in turn lets it close the LLM stream and free the
# connection slot. We also emit a periodic heartbeat so intermediaries
# (nginx, CloudFront) don't kill an idle connection.
# ---------------------------------------------------------------------------


_HEARTBEAT_SECONDS = 15


def _sse_pack(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, default=str)}\n\n"


async def _merge_heartbeat(
    producer: AsyncIterator[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    """Yield producer events, injecting a ``heartbeat`` every N seconds.

    The runtime can pause for several seconds during slow LLM responses
    or long-running tool calls; some HTTP proxies idle-close such
    connections. The heartbeat keeps the socket warm.
    """
    sentinel: object = object()
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=256)

    async def pump() -> None:
        try:
            async for item in producer:
                await queue.put(item)
        except Exception as exc:  # noqa: BLE001 — propagate as terminal event
            await queue.put({"type": "error", "message": str(exc)})
        finally:
            await queue.put(sentinel)

    async def beat() -> None:
        try:
            while True:
                await asyncio.sleep(_HEARTBEAT_SECONDS)
                await queue.put({"type": "heartbeat"})
        except asyncio.CancelledError:
            return

    pump_task = asyncio.create_task(pump())
    beat_task = asyncio.create_task(beat())
    try:
        while True:
            item = await queue.get()
            if item is sentinel:
                break
            yield item
    finally:
        beat_task.cancel()
        try:
            await beat_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await pump_task


@router.post("/sessions/{session_id}/messages/stream")
async def send_message_stream_route(
    session_id: UUID,
    body: SendMessageRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Server-Sent-Events variant of ``POST /messages``.

    Yields:
      * ``ready``               — runtime initialised, agent metadata.
      * ``assistant_delta``     — incremental assistant text.
      * ``tool_call`` / ``tool_result`` — per tool invocation.
      * ``parser_validating`` / ``parser_retry``
      * ``turn_complete``       — final telemetry + structured output.
      * ``error``               — terminal failure (stream ends).
      * ``heartbeat``           — every ~15 s to keep proxies awake.
    """
    sess = (
        await db.execute(select(ChatSession).where(ChatSession.id == session_id))
    ).scalar_one_or_none()
    if sess is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="session not found"
        )
    if sess.status != ChatSessionStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"session is {sess.status.value}",
        )

    _, definition = await _load_latest_definition(db, sess.workflow_id)
    connections = await load_workspace_connections(
        db, workspace_id=sess.workspace_id
    )
    runtime = ChatRuntime(
        settings=get_settings(),
        db=db,
        session=sess,
        workflow_definition=definition,
        workspace_connections=connections,
    )

    async def event_stream() -> AsyncIterator[bytes]:
        try:
            producer = runtime.handle_user_message_stream(body.content)
            async for ev in _merge_heartbeat(producer):
                if await request.is_disconnected():
                    # Client went away — stop pulling. The runtime gets
                    # garbage collected; its open LLM HTTP connection
                    # tears down through the httpx client's cleanup.
                    break
                yield _sse_pack(ev).encode("utf-8")
        except asyncio.CancelledError:
            # FastAPI cancels the response generator on client disconnect
            # in some uvicorn versions. Treat as a clean stop.
            return
        except Exception as exc:  # noqa: BLE001
            yield _sse_pack({"type": "error", "message": str(exc)}).encode("utf-8")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/sessions/{session_id}/messages",
    response_model=ChatMessageListResponse,
)
async def list_messages_route(
    session_id: UUID,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    db: AsyncSession = Depends(get_db),
) -> ChatMessageListResponse:
    sess = (
        await db.execute(select(ChatSession).where(ChatSession.id == session_id))
    ).scalar_one_or_none()
    if sess is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    total = int(
        await db.scalar(
            select(func.count())
            .select_from(ChatMessage)
            .where(ChatMessage.session_id == session_id)
        )
        or 0
    )
    rows = list(
        (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.created_at.asc())
                .offset(max(page - 1, 0) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
    )
    # Restore PII tokens before sending to the client. The rows on disk
    # store redacted tokens; the UI shows real values to the end user
    # (typically the same person who supplied them).
    pii = PIIService()
    token_map = await pii.load_token_map(str(session_id))
    return ChatMessageListResponse(
        items=[
            ChatMessageOut(
                id=r.id,
                role=r.role.value if hasattr(r.role, "value") else str(r.role),
                content=pii.restore(r.content or "", token_map),
                tool_calls=r.tool_calls,
                tool_call_id=r.tool_call_id,
                tool_name=r.tool_name,
                created_at=r.created_at,
                parser_status=r.parser_status,
            )
            for r in rows
        ],
        total=total,
    )


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def close_session_route(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    sess = (
        await db.execute(select(ChatSession).where(ChatSession.id == session_id))
    ).scalar_one_or_none()
    if sess is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    sess.status = ChatSessionStatus.CLOSED
    sess.closed_at = datetime.now(timezone.utc)

    # Best-effort memory clear so a closed session doesn't leak state.
    _, definition = await _load_latest_definition(db, sess.workflow_id)
    for n in definition.iter_nodes():
        if isinstance(n, AgentNode) and n.id == sess.agent_node_id and n.memory_ref:
            for m in definition.iter_nodes():
                if isinstance(m, MemoryNode) and m.id == n.memory_ref:
                    await MemoryStore(get_redis()).clear(
                        m,
                        session_id=sess.id,
                        user_id=sess.started_by_id,
                        workflow_id=sess.workflow_id,
                    )
    await db.commit()


@router.get(
    "/sessions/{session_id}/memory",
    response_model=MemoryInspectResponse,
)
async def inspect_memory_route(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> MemoryInspectResponse:
    sess = (
        await db.execute(select(ChatSession).where(ChatSession.id == session_id))
    ).scalar_one_or_none()
    if sess is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    _, definition = await _load_latest_definition(db, sess.workflow_id)
    agent = next(
        (n for n in definition.iter_nodes()
         if isinstance(n, AgentNode) and n.id == sess.agent_node_id),
        None,
    )
    if agent is None or not agent.memory_ref:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent has no memory node bound",
        )
    mem = next(
        (n for n in definition.iter_nodes()
         if isinstance(n, MemoryNode) and n.id == agent.memory_ref),
        None,
    )
    if mem is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="memory node missing from definition",
        )
    info = await MemoryStore(get_redis()).inspect(
        mem,
        session_id=sess.id,
        user_id=sess.started_by_id,
        workflow_id=sess.workflow_id,
    )
    return MemoryInspectResponse(**info)


@router.get(
    "/sessions",
    response_model=SessionListResponse,
)
async def list_sessions_route(
    workspace_id: Annotated[UUID, Query(...)],
    workflow_id: Annotated[UUID | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    db: AsyncSession = Depends(get_db),
) -> SessionListResponse:
    """Recent chat sessions for a workspace (optionally filtered by workflow).

    Used by the frontend session picker to let an operator resume a
    conversation that was started in a previous browser tab.
    """
    where = [ChatSession.workspace_id == workspace_id]
    if workflow_id is not None:
        where.append(ChatSession.workflow_id == workflow_id)
    total = int(
        await db.scalar(
            select(func.count()).select_from(ChatSession).where(*where)
        )
        or 0
    )
    rows = list(
        (
            await db.execute(
                select(ChatSession)
                .where(*where)
                .order_by(desc(ChatSession.last_activity_at))
                .offset(max(page - 1, 0) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
    )
    items = [
        SessionListItem(
            id=r.id,
            workspace_id=r.workspace_id,
            workflow_id=r.workflow_id,
            trigger_slug=r.trigger_slug,
            agent_node_id=r.agent_node_id,
            status=r.status.value if hasattr(r.status, "value") else str(r.status),
            total_messages=r.total_messages,
            total_cost_cents=microcents_to_cents(r.total_cost_microcents),
            last_activity_at=r.last_activity_at,
            created_at=r.created_at,
        )
        for r in rows
    ]
    return SessionListResponse(items=items, total=total)


@router.get(
    "/sessions/{session_id}/usage",
    response_model=SessionUsageResponse,
)
async def session_usage_route(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> SessionUsageResponse:
    sess = (
        await db.execute(select(ChatSession).where(ChatSession.id == session_id))
    ).scalar_one_or_none()
    if sess is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    _, definition = await _load_latest_definition(db, sess.workflow_id)
    trig = next(
        (
            n for n in definition.iter_nodes()
            if getattr(n, "trigger_type", None) == "chat"
            and getattr(n, "slug", None) == sess.trigger_slug
        ),
        None,
    )
    return SessionUsageResponse(
        session_id=sess.id,
        total_prompt_tokens=sess.total_prompt_tokens,
        total_completion_tokens=sess.total_completion_tokens,
        total_messages=sess.total_messages,
        total_cost_cents=microcents_to_cents(sess.total_cost_microcents),
        rate_limits=getattr(trig, "rate_limits", None) if trig else None,
    )


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


@router.post(
    "/sessions/{session_id}/attachments",
    status_code=status.HTTP_201_CREATED,
)
async def upload_attachment_route(
    session_id: UUID,
    file: UploadFile = File(...),
    label: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Upload a file as an attachment to a chat session.

    Stored in the workspace's MinIO bucket; metadata in ``chat_attachments``.
    The next ``POST /messages`` call binds any unbound attachments
    uploaded under the same session to the resulting user-message row.
    Public route (the session id is the capability) — matches the rest
    of the chat API surface.
    """
    sess = (
        await db.execute(select(ChatSession).where(ChatSession.id == session_id))
    ).scalar_one_or_none()
    if sess is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if sess.status != ChatSessionStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"session is {sess.status.value}",
        )

    from core.storage import StorageService

    body = await file.read()
    if not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="empty file",
        )
    storage = StorageService(get_settings())
    bucket = await storage.ensure_bucket(sess.workspace_id)
    # Upload via the existing helper — it owns key naming + content-type.
    # We bind to ``sess.id`` as a pseudo "user id" so the storage layer
    # gives us a key under the session prefix.
    info = await storage.upload_document(
        body,
        filename=file.filename or "attachment",
        workspace_id=sess.workspace_id,
        user_id=sess.id,
    )
    row = ChatAttachment(
        session_id=sess.id,
        message_id=None,
        bucket=info["bucket"],
        object_key=info["key"],
        content_type=file.content_type or "application/octet-stream",
        byte_size=len(body),
        filename=file.filename or "attachment",
        uploaded_by_id=sess.started_by_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {
        "id": str(row.id),
        "session_id": str(row.session_id),
        "filename": row.filename,
        "content_type": row.content_type,
        "byte_size": row.byte_size,
        "label": label,
        "url": info["url"],
    }


@router.get("/attachments/{attachment_id}")
async def get_attachment_route(
    attachment_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return a public URL (or short-lived presigned URL) for the blob.

    Phase 2d returns the raw MinIO URL — Phase 3 will swap in presigned
    URLs once we move to S3 + bucket policies that require auth.
    """
    row = (
        await db.execute(
            select(ChatAttachment).where(ChatAttachment.id == attachment_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    s = get_settings()
    base = (
        f"{'https' if s.MINIO_USE_SSL else 'http'}://{s.MINIO_ENDPOINT}"
    )
    return {
        "id": str(row.id),
        "session_id": str(row.session_id),
        "filename": row.filename,
        "content_type": row.content_type,
        "byte_size": row.byte_size,
        "url": f"{base}/{row.bucket}/{row.object_key}",
    }


# ---------------------------------------------------------------------------
# Human handoff queue
# ---------------------------------------------------------------------------


@router.get("/handoff/queue")
async def list_handoff_queue_route(
    workspace_id: Annotated[UUID, Query(...)],
    status_filter: Annotated[str, Query(alias="status")] = "pending",
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Human-agent–facing queue listing. Default filter: ``pending``."""
    where = [HumanHandoffQueueItem.workspace_id == workspace_id]
    if status_filter:
        try:
            where.append(HumanHandoffQueueItem.status == HandoffStatus(status_filter))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown status: {status_filter}",
            ) from exc
    rows = list(
        (
            await db.execute(
                select(HumanHandoffQueueItem)
                .where(*where)
                .order_by(HumanHandoffQueueItem.created_at.asc())
                .limit(200)
            )
        ).scalars().all()
    )
    return {
        "items": [
            {
                "id": str(r.id),
                "session_id": str(r.session_id),
                "workspace_id": str(r.workspace_id),
                "reason": r.reason,
                "customer_summary": r.customer_summary,
                "status": r.status.value,
                "priority": r.priority,
                "claimed_by_id": (
                    str(r.claimed_by_id) if r.claimed_by_id else None
                ),
                "claimed_at": r.claimed_at,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    }


@router.post("/handoff/queue/{queue_id}/claim", status_code=status.HTTP_200_OK)
async def claim_handoff_route(
    queue_id: UUID,
    operator_id: Annotated[UUID, Query(...)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """A human agent claims a pending queue item. Idempotent on first claim."""
    row = (
        await db.execute(
            select(HumanHandoffQueueItem).where(HumanHandoffQueueItem.id == queue_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if row.status != HandoffStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"queue item is {row.status.value}",
        )
    row.status = HandoffStatus.CLAIMED
    row.claimed_by_id = operator_id
    row.claimed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"id": str(row.id), "status": row.status.value}


@router.post("/handoff/queue/{queue_id}/resolve", status_code=status.HTTP_200_OK)
async def resolve_handoff_route(
    queue_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Mark a handoff resolved + return the underlying session to ACTIVE."""
    row = (
        await db.execute(
            select(HumanHandoffQueueItem).where(HumanHandoffQueueItem.id == queue_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if row.status == HandoffStatus.RESOLVED:
        return {"id": str(row.id), "status": row.status.value}
    row.status = HandoffStatus.RESOLVED
    row.resolved_at = datetime.now(timezone.utc)
    # Re-activate the session so the customer's next message goes back
    # to the agent (the human is done).
    sess = (
        await db.execute(
            select(ChatSession).where(ChatSession.id == row.session_id)
        )
    ).scalar_one_or_none()
    if sess is not None:
        sess.status = ChatSessionStatus.ACTIVE
    await db.commit()
    return {"id": str(row.id), "status": row.status.value}


__all__ = ["router"]
