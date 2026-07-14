"""Workflow persistence, interpreter bridge, Dynamiq execution, PII lane, SSE helpers."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agents.dynamiq_service import DynamiqService
from agents.langgraph.service import LangGraphService
from core.config import Settings, get_settings
from core.redis import get_redis as _redis_global
from core.snapshot import snapshot
from egpt_mcp.mcp_tool_registry import MCPToolError, MCPToolRegistry
from egpt_mcp.tool_registry import ToolRegistry
from egpt_mcp.tool_run_buffer import ToolRunBuffer
from models.integration import Integration, IntegrationStatus
from models.native_connection import NativeConnection, NativeConnectionStatus
from models.user import User
from models.workflow import Workflow as WFRow
from models.workflow import WorkflowStatus
from models.workflow_execution import WorkflowExecution, WorkflowExecutionStatus
from models.workflow_execution_step import (
    WorkflowExecutionStep,
    WorkflowExecutionStepStatus,
)
from models.workflow_version import WorkflowVersion
from models.workspace_member import WorkspaceMember
from schemas.workflow import (
    InterpretRequest,
    NeedsClarificationResponse,
    ReadyResponse,
    WorkflowCreateBody,
    WorkflowDefinition,
    WorkflowRequirement,
    WorkflowUpdateBody,
    slugify_name,
)
from services.clarification_service import (
    ClarificationAccessDeniedError,
    ClarificationReady,
    ClarificationService,
    ClarificationSessionNotFoundError,
)
from services.pii_service import PIIService, PIIToken
from services.workflow_interpreter import (
    WorkflowInterpretationError,
    WorkflowInterpreter,
    diff_definitions,
)
from services.workflow_requirements import derive_requirements

log = structlog.get_logger("enterprisegpt.workflow")
EXECUTION_TTL_SECONDS = 86400
HITL_POLL_TIMEOUT_SECONDS = 24 * 3600


def _state_key(execution_id: UUID) -> str:
    return f"egpt:execution:{execution_id}:state"


def _approval_key(execution_id: UUID) -> str:
    return f"egpt:execution:{execution_id}:approval"


async def ensure_workspace_membership(
    db: AsyncSession, *, user_id: UUID, workspace_id: UUID
) -> WorkspaceMember:
    stmt = (
        select(WorkspaceMember)
        .where(
            WorkspaceMember.user_id == user_id,
            WorkspaceMember.workspace_id == workspace_id,
        )
        .limit(1)
    )
    membership = (await db.execute(stmt)).scalar_one_or_none()
    if membership is None:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Workspace access denied",
        )
    return membership


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def deep_redact(
    payload: dict[str, Any], pii: PIIService
) -> tuple[dict[str, Any], dict[str, PIIToken]]:
    merged: dict[str, PIIToken] = {}

    def walk(val: Any) -> Any:
        if isinstance(val, str):
            r, m = pii.redact(val)
            merged.update(m)
            return r
        if isinstance(val, list):
            return [walk(item) for item in val]
        if isinstance(val, dict):
            return {k: walk(v) for k, v in val.items()}
        return val

    return walk(payload), merged


async def deep_restore(payload: dict[str, Any], pii: PIIService, tm: dict[str, PIIToken]) -> dict[str, Any]:
    def walk(val: Any) -> Any:
        if isinstance(val, str):
            return pii.restore(val, tm)
        if isinstance(val, list):
            return [walk(v) for v in val]
        if isinstance(val, dict):
            return {k: walk(v) for k, v in val.items()}
        return val

    return walk(payload)


def build_dynamiq_input(payload: dict[str, Any]) -> dict[str, Any]:
    if "input" in payload and isinstance(payload["input"], str):
        return dict(payload)
    try:
        return {"input": json.dumps(payload)}
    except (TypeError, ValueError):  # pragma: no cover
        return {"input": repr(payload)}


def extract_agent_blob(output: dict[str, Any] | None, agent_id: str) -> str:
    if not isinstance(output, dict):
        return ""
    cell = output.get(agent_id)
    if not isinstance(cell, dict):
        return ""
    inner = cell.get("output")
    if isinstance(inner, dict):
        content = inner.get("content")
        if isinstance(content, str):
            return content
        return json.dumps(inner)[:16000]
    if isinstance(inner, str):
        return inner
    return ""


class WorkflowService:
    def __init__(
        self,
        settings: Settings,
        *,
        clarification_service: ClarificationService | None = None,
        pii_service: PIIService | None = None,
        dynamiq: DynamiqService | None = None,
        interpreter: WorkflowInterpreter | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.settings = settings
        self._pii = pii_service or PIIService()
        self._dynamiq = dynamiq or DynamiqService(settings)
        self._interp = interpreter or WorkflowInterpreter(settings)
        self._clarification = clarification_service
        self._tool_registry = tool_registry

    async def _gather_mcp_tool_names(
        self, db: AsyncSession, workspace_id: UUID,
    ) -> list[str]:
        """Collect tool names from every MCP server (workspace-registered +
        env-config fallback). Returns a sorted, deduped list. Errors are
        swallowed — MCP being down shouldn't block workflow generation."""
        names: set[str] = set()

        # Workspace-registered servers
        try:
            from services.mcp_server_service import list_servers, to_server_config

            rows = await list_servers(db, workspace_id)
            for row in rows:
                try:
                    reg = MCPToolRegistry(
                        get_settings(), _redis_global(),
                        server_config=to_server_config(row),
                    )
                    for t in await reg.list_tools():
                        n = str(t.get("name") or "")
                        if n:
                            names.add(n)
                except (MCPToolError, Exception):  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass

        # Env-config fallback
        try:
            env_reg = MCPToolRegistry(get_settings(), _redis_global())
            if env_reg._is_enabled():  # type: ignore[attr-defined]
                for t in await env_reg.list_tools():
                    n = str(t.get("name") or "")
                    if n:
                        names.add(n)
        except Exception:  # noqa: BLE001
            pass

        return sorted(names)

    # ---------------------------------------------------------------------
    async def interpret_and_preview(
        self,
        db: AsyncSession,
        *,
        user: User,
        request: InterpretRequest,
    ) -> NeedsClarificationResponse | ReadyResponse:
        from fastapi import HTTPException, status

        preview = self.settings.workflow_preview_tools
        clarification = self._clarification
        if clarification is None:
            clarification = ClarificationService(
                self.settings,
                langgraph_service=LangGraphService(self.settings),
            )

        rounds_used = 0
        augmented_prompt: str

        try:
            if request.session_id:
                ws_id = await clarification.resolve_workspace_for_session(
                    request.session_id, user.id
                )
                await ensure_workspace_membership(db, user_id=user.id, workspace_id=ws_id)
                registry_names: list[str] = []
                if self._tool_registry:
                    registry_names = await self._tool_registry.get_tool_names_for_prompt(
                        db, ws_id
                    )
                mcp_names = await self._gather_mcp_tool_names(db, ws_id)
                tools = sorted(set(registry_names) | set(mcp_names) | set(preview))
                sub = await clarification.submit_answers(
                    db,
                    request.session_id,
                    request.answers,
                    tools,
                    user_id=user.id,
                    force_proceed=request.force_proceed,
                )
                if isinstance(sub, NeedsClarificationResponse):
                    return sub
                assert isinstance(sub, ClarificationReady)
                augmented_prompt = sub.augmented_prompt
                rounds_used = sub.rounds_used
            else:
                if request.workspace_id is None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="workspace_id is required for new interpretation sessions",
                    )
                await ensure_workspace_membership(
                    db, user_id=user.id, workspace_id=request.workspace_id
                )
                registry_names = []
                if self._tool_registry:
                    registry_names = await self._tool_registry.get_tool_names_for_prompt(
                        db, request.workspace_id
                    )
                mcp_names = await self._gather_mcp_tool_names(db, request.workspace_id)
                tools = sorted(set(registry_names) | set(mcp_names) | set(preview))
                augmented_prompt = (request.text or "").strip()
                use_clarification = (
                    self.settings.CLARIFICATION_ENABLED and not request.skip_clarification
                )
                if use_clarification:
                    nc = await clarification.analyze_initial(
                        db,
                        augmented_prompt,
                        tools,
                        workspace_id=request.workspace_id,
                        user_id=user.id,
                    )
                    if nc is not None:
                        return nc
                    rounds_used = 0
                else:
                    rounds_used = 0

            try:
                definition = await self._interp.interpret(
                    user_input=augmented_prompt,
                    available_tools=tools,
                )
            except WorkflowInterpretationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
                ) from exc

            return ReadyResponse(
                definition=definition,
                augmented_prompt=augmented_prompt,
                rounds_used=rounds_used,
            )
        except ClarificationSessionNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="clarification session not found or expired",
            ) from None
        except ClarificationAccessDeniedError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="clarification session access denied",
            ) from None
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    async def interpret_nl(self, *, user_input: str) -> WorkflowDefinition:
        tools = self.settings.workflow_preview_tools
        try:
            return await self._interp.interpret(user_input=user_input, available_tools=tools)
        except WorkflowInterpretationError as exc:
            from fastapi import HTTPException, status

            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    async def sample_input_for_workflow(
        self,
        db: AsyncSession,
        *,
        user: User,
        workflow_id: UUID,
    ) -> dict[str, Any]:
        """Return a trigger-aware sample payload for the latest version.

        The frontend uses this to pre-fill the "Test workflow" input form
        so users don't have to craft the JSON by hand.
        """
        from fastapi import HTTPException, status

        from services.demo_executor import sample_input_for

        row = await self._fetch_workflow_for_user(
            db, user_id=user.id, workflow_id=workflow_id
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found"
            )
        latest_ver = (
            await db.execute(
                select(WorkflowVersion)
                .where(WorkflowVersion.workflow_id == row.id)
                .order_by(WorkflowVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest_ver is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="no workflow versions"
            )
        wd = WorkflowDefinition.model_validate(latest_ver.definition)
        return sample_input_for(wd)

    async def augment_definition(
        self,
        db: AsyncSession,
        *,
        user: User,
        workflow_id: UUID,
        message: str,
        current_definition: WorkflowDefinition,
        focus_node_id: str | None = None,
    ) -> tuple[WorkflowDefinition, list[str]]:
        """Return a proposed (NOT persisted) modification of ``current_definition``.

        The caller is expected to preview the result and explicitly commit
        via ``PUT /workflows/{id}``. We still enforce workspace membership
        + workflow existence here so the augment endpoint is a true peer
        of update_workflow from an RBAC perspective.
        """
        from fastapi import HTTPException, status

        row = await self._fetch_workflow_for_user(
            db, user_id=user.id, workflow_id=workflow_id
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found"
            )
        tools = self.settings.workflow_preview_tools
        try:
            proposed = await self._interp.augment(
                current_definition=current_definition,
                user_message=message,
                available_tools=tools,
                focus_node_id=focus_node_id,
            )
        except WorkflowInterpretationError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        changes = diff_definitions(before=current_definition, after=proposed)
        log.info(
            "workflow.augmented",
            workflow_id=str(workflow_id),
            change_count=len(changes),
        )
        return proposed, changes

    async def summarize_node(
        self,
        db: AsyncSession,
        *,
        user: User,
        workflow_id: UUID,
        node_id: str,
        definition: WorkflowDefinition,
    ) -> tuple[str, bool]:
        """Return ``(summary, cached)`` — a plain-English explanation of one node.

        Enforces the same workspace-membership / existence checks as the rest
        of the workflow API, then serves from a Redis cache keyed by the
        node's content hash (so a given node-version is only paid for once)
        and falls back to an LLM call on a miss.
        """
        from fastapi import HTTPException, status

        row = await self._fetch_workflow_for_user(
            db, user_id=user.id, workflow_id=workflow_id
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found"
            )

        node = next(
            (n for n in definition.iter_nodes() if n.id == node_id), None
        )
        if node is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"node {node_id!r} not found in definition",
            )

        # Cache by the node's own content so edits invalidate naturally and
        # re-selecting an unchanged node is free.
        node_hash = hashlib.sha256(node.model_dump_json().encode("utf-8")).hexdigest()
        cache_key = f"wf:node_summary:{workflow_id}:{node_hash}"
        redis = _redis_global()
        try:
            cached_summary = await redis.get(cache_key)
        except Exception:  # noqa: BLE001 — cache is best-effort
            cached_summary = None
        if cached_summary:
            return cached_summary, True

        try:
            summary = await self._interp.summarize_node(
                definition=definition, node_id=node_id
            )
        except WorkflowInterpretationError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc

        try:
            # 7-day TTL — node summaries are stable for a node-version.
            await redis.setex(cache_key, 7 * 24 * 3600, summary)
        except Exception:  # noqa: BLE001 — never fail the request on a cache write
            log.debug("workflow.node_summary.cache_write_failed", exc_info=True)
        return summary, False

    async def _evaluate_requirements(
        self,
        db: AsyncSession,
        *,
        workspace_id: UUID,
        definition: WorkflowDefinition,
    ) -> tuple[list[WorkflowRequirement], list[str]]:
        """Return ``(requirements, missing_required)`` for a definition.

        Shared by the requirements endpoint (panel) and the publish gate so
        there's exactly one notion of "what does this workflow need".
        """
        specs = derive_requirements(definition)

        active_rows = await db.execute(
            select(NativeConnection.provider).where(
                NativeConnection.workspace_id == workspace_id,
                NativeConnection.status == NativeConnectionStatus.ACTIVE,
            )
        )
        active = {p.strip().lower() for (p,) in active_rows.all()}

        # Composio is configured at the platform level (hosted MCP), so treat
        # its meta-tools as available when credentials are present.
        composio_ready = bool(
            self.settings.COMPOSIO_MCP_API_KEY or self.settings.COMPOSIO_API_KEY
        )

        requirements: list[WorkflowRequirement] = []
        missing_required: list[str] = []
        for s in specs:
            connected = composio_ready if s.provider == "composio" else s.provider in active
            if s.required and s.connectable and not connected:
                missing_required.append(s.provider)
            requirements.append(
                WorkflowRequirement(
                    provider=s.provider,
                    name=s.name,
                    kind=s.kind,
                    auth_type=s.auth_type,
                    connectable=s.connectable,
                    required=s.required,
                    connected=connected,
                    used_by=s.used_by,
                    reason=s.reason,
                )
            )
        return requirements, missing_required

    async def workflow_requirements(
        self,
        db: AsyncSession,
        *,
        user: User,
        workflow_id: UUID,
        definition: WorkflowDefinition,
    ) -> tuple[list[WorkflowRequirement], list[str]]:
        """RBAC-checked requirements for a (possibly unsaved) definition."""
        from fastapi import HTTPException, status

        row = await self._fetch_workflow_for_user(
            db, user_id=user.id, workflow_id=workflow_id
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found"
            )
        return await self._evaluate_requirements(
            db, workspace_id=row.workspace_id, definition=definition
        )

    async def create_workflow(
        self,
        db: AsyncSession,
        *,
        user: User,
        body: WorkflowCreateBody,
    ) -> WFRow:
        await ensure_workspace_membership(db, user_id=user.id, workspace_id=body.workspace_id)
        wf_def = body.definition
        slug = body.slug or await self._derive_unique_slug_async(db, body.workspace_id, wf_def.name)
        row = WFRow(
            workspace_id=body.workspace_id,
            name=wf_def.name,
            slug=slug,
            current_version=1,
            created_by=user.id,
        )
        db.add(row)
        await db.flush()

        ver = WorkflowVersion(
            workflow_id=row.id,
            version=1,
            definition=wf_def.model_dump(mode="json"),
            change_note=body.change_note,
            created_by=user.id,
        )
        db.add(ver)
        await db.commit()
        await db.refresh(row)
        log.info(
            "workflow.created",
            workflow_id=str(row.id),
            slug=slug,
            version=1,
        )
        return row

    async def _derive_unique_slug_async(
        self, db: AsyncSession, workspace_id: UUID, workflow_name: str
    ) -> str:
        import uuid as uuid_lib

        base = slugify_name(workflow_name)
        candidate = base
        for _ in range(25):
            cnt_stmt = select(func.count()).select_from(WFRow).where(
                WFRow.workspace_id == workspace_id,
                WFRow.slug == candidate,
                WFRow.deleted_at.is_(None),
            )
            exists_num = await db.scalar(cnt_stmt)
            if not exists_num:
                return candidate
            candidate = f"{base}-{uuid_lib.uuid4().hex[:8]}"
        raise RuntimeError("unable to derive unique slug")  # pragma: no cover

    async def update_workflow(
        self,
        db: AsyncSession,
        *,
        user: User,
        workflow_id: UUID,
        body: WorkflowUpdateBody,
    ) -> WFRow:
        row = await self._fetch_workflow_for_user(db, user_id=user.id, workflow_id=workflow_id)
        if row is None:
            from fastapi import HTTPException, status

            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
        row.current_version += 1
        ver = WorkflowVersion(
            workflow_id=row.id,
            version=row.current_version,
            definition=body.definition.model_dump(mode="json"),
            change_note=body.change_note,
            created_by=user.id,
        )
        row.name = body.definition.name
        # Editing changes behavior — a previously-published workflow reverts to
        # draft so the new version can't go live without being re-validated and
        # re-published. (No-op if already a draft.)
        row.status = WorkflowStatus.DRAFT
        row.published_at = None
        row.published_version_id = None
        db.add(ver)
        await db.commit()
        await db.refresh(row)
        return row

    async def rename_workflow(
        self,
        db: AsyncSession,
        *,
        user: User,
        workflow_id: UUID,
        name: str,
    ) -> WFRow:
        """Rename a workflow WITHOUT creating a version or touching publish state.

        Updates ``workflows.name`` and the latest version's ``definition.name``
        in place so the library, editor, and runtime all agree on the new name.
        """
        from fastapi import HTTPException, status

        clean = name.strip()
        if not clean:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="name cannot be empty",
            )
        row = await self._fetch_workflow_for_user(db, user_id=user.id, workflow_id=workflow_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")

        row.name = clean
        latest = (
            await db.execute(
                select(WorkflowVersion)
                .where(WorkflowVersion.workflow_id == row.id)
                .order_by(WorkflowVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest is not None:
            # Reassign (not in-place mutate) so SQLAlchemy flags the JSONB dirty.
            latest.definition = {**latest.definition, "name": clean}
            db.add(latest)
        await db.commit()
        await db.refresh(row)
        return row

    async def publish_workflow(
        self,
        db: AsyncSession,
        *,
        user: User,
        workflow_id: UUID,
    ) -> WFRow:
        """Promote the current version to ``published`` so live runs perform
        real side effects.

        Requires a COMPLETED test run on the current version (so you can't
        publish something that was never executed) AND that its readiness
        verdict had no blocking issues. Raises HTTP 409 otherwise.
        """
        from fastapi import HTTPException
        from fastapi import status as http_status

        row = await self._fetch_workflow_for_user(db, user_id=user.id, workflow_id=workflow_id)
        if row is None:
            raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND)

        latest_ver = (
            await db.execute(
                select(WorkflowVersion)
                .where(WorkflowVersion.workflow_id == row.id)
                .order_by(WorkflowVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest_ver is None:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="no workflow version to publish",
            )

        # Gate: a completed run must exist for the current version.
        validated = (
            await db.execute(
                select(WorkflowExecution.id)
                .where(
                    WorkflowExecution.workflow_id == row.id,
                    WorkflowExecution.version_id == latest_ver.id,
                    WorkflowExecution.status == WorkflowExecutionStatus.COMPLETED,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if validated is None:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail=(
                    "Run a successful test of the current version before "
                    "publishing."
                ),
            )

        # Gate: every required, connectable integration must be connected.
        try:
            definition = WorkflowDefinition.model_validate(latest_ver.definition)
        except Exception:  # noqa: BLE001 — malformed stored definition
            definition = None
        if definition is not None:
            _, missing = await self._evaluate_requirements(
                db, workspace_id=row.workspace_id, definition=definition
            )
            if missing:
                raise HTTPException(
                    status_code=http_status.HTTP_409_CONFLICT,
                    detail=(
                        "Connect required integrations before publishing: "
                        + ", ".join(missing)
                    ),
                )

        row.status = WorkflowStatus.PUBLISHED
        row.published_at = _utcnow()
        row.published_version_id = latest_ver.id
        await db.commit()
        await db.refresh(row)
        return row

    async def unpublish_workflow(
        self,
        db: AsyncSession,
        *,
        user: User,
        workflow_id: UUID,
    ) -> WFRow:
        """Take a workflow back to ``draft`` — live runs revert to previews."""
        from fastapi import HTTPException
        from fastapi import status as http_status

        row = await self._fetch_workflow_for_user(db, user_id=user.id, workflow_id=workflow_id)
        if row is None:
            raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND)
        row.status = WorkflowStatus.DRAFT
        row.published_at = None
        row.published_version_id = None
        await db.commit()
        await db.refresh(row)
        return row

    async def rollback_workflow(
        self,
        db: AsyncSession,
        *,
        user: User,
        workflow_id: UUID,
        target_version: int,
    ) -> WFRow:
        """Restore a prior version's definition as a new current version.

        Since versions are immutable and publish always targets the latest
        version, "rollback" re-appends the target version's definition as a new
        version (reverting the workflow to draft, per ``update_workflow``). The
        caller re-runs and re-publishes to take the restored definition live.
        """
        from fastapi import HTTPException
        from fastapi import status as http_status

        row = await self._fetch_workflow_for_user(db, user_id=user.id, workflow_id=workflow_id)
        if row is None:
            raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND)
        target = (
            await db.execute(
                select(WorkflowVersion).where(
                    WorkflowVersion.workflow_id == row.id,
                    WorkflowVersion.version == target_version,
                )
            )
        ).scalar_one_or_none()
        if target is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"version {target_version} not found",
            )
        return await self.update_workflow(
            db,
            user=user,
            workflow_id=workflow_id,
            body=WorkflowUpdateBody(
                definition=WorkflowDefinition.model_validate(target.definition),
                change_note=f"rollback to v{target_version}",
            ),
        )

    async def set_self_heal(
        self,
        db: AsyncSession,
        *,
        user: User,
        workflow_id: UUID,
        config: dict,
    ) -> WFRow:
        """Set the workflow's autonomous self-heal policy (operational config —
        does NOT create a new version)."""
        from fastapi import HTTPException
        from fastapi import status as http_status

        row = await self._fetch_workflow_for_user(db, user_id=user.id, workflow_id=workflow_id)
        if row is None:
            raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND)
        row.self_heal = config
        await db.commit()
        await db.refresh(row)
        return row

    async def soft_delete(
        self,
        db: AsyncSession,
        *,
        user: User,
        workflow_id: UUID,
    ) -> None:
        row = await self._fetch_workflow_for_user(db, user_id=user.id, workflow_id=workflow_id)
        if row is None:
            from fastapi import HTTPException, status

            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
        row.deleted_at = _utcnow()
        row.is_active = False
        await db.commit()

    async def list_workflows(
        self,
        db: AsyncSession,
        *,
        user: User,
        workspace_ids: list[UUID] | None,
        page: int,
        page_size: int,
    ) -> tuple[list[WFRow], int]:
        if workspace_ids:
            wid_filter = workspace_ids
        else:
            m_stmt = select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id)
            wid_filter = list((await db.execute(m_stmt)).scalars().all())

        if not wid_filter:
            return [], 0

        count_stmt = select(func.count()).select_from(WFRow).where(
            WFRow.workspace_id.in_(wid_filter),
            WFRow.deleted_at.is_(None),
        )
        total = int(await db.scalar(count_stmt) or 0)

        stmt = (
            select(WFRow)
            .where(
                WFRow.workspace_id.in_(wid_filter),
                WFRow.deleted_at.is_(None),
            )
            .order_by(WFRow.updated_at.desc())
            .offset(max(page - 1, 0) * page_size)
            .limit(page_size)
        )
        rows = (await db.execute(stmt)).scalars().all()
        return list(rows), total

    async def get_detail(
        self,
        db: AsyncSession,
        *,
        user: User,
        workflow_id: UUID,
    ) -> tuple[WFRow, list[WorkflowVersion]] | None:
        row = await self._fetch_workflow_for_user(db, user_id=user.id, workflow_id=workflow_id)
        if row is None:
            return None
        vstmt = (
            select(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow_id)
            .order_by(WorkflowVersion.version.desc())
        )
        versions = list((await db.execute(vstmt)).scalars().all())
        return row, versions

    async def _fetch_workflow_for_user(
        self, db: AsyncSession, *, user_id: UUID, workflow_id: UUID
    ) -> WFRow | None:
        stmt = (
            select(WFRow)
            .join(
                WorkspaceMember,
                WorkspaceMember.workspace_id == WFRow.workspace_id,
            )
            .where(
                WorkspaceMember.user_id == user_id,
                WFRow.id == workflow_id,
                WFRow.deleted_at.is_(None),
            )
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    async def list_executions(
        self,
        db: AsyncSession,
        *,
        user: User,
        workflow_id: UUID,
        page: int,
        page_size: int,
    ) -> tuple[list[WorkflowExecution], int]:
        row = await self._fetch_workflow_for_user(db, user_id=user.id, workflow_id=workflow_id)
        if row is None:
            from fastapi import HTTPException, status

            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        # Demo/test runs are persisted (for the inspector) but excluded here so
        # they never count as production runs. Filter BOTH the count and the
        # page query or pagination totals drift.
        stmt_count = (
            select(func.count())
            .select_from(WorkflowExecution)
            .where(
                WorkflowExecution.workflow_id == workflow_id,
                WorkflowExecution.demo.is_(False),
            )
        )
        total = int(await db.scalar(stmt_count) or 0)
        stmt = (
            select(WorkflowExecution)
            .where(
                WorkflowExecution.workflow_id == workflow_id,
                WorkflowExecution.demo.is_(False),
            )
            .order_by(WorkflowExecution.started_at.desc())
            .offset(max(page - 1, 0) * page_size)
            .limit(page_size)
        )
        rows = list((await db.execute(stmt)).scalars().all())
        return rows, total

    async def get_execution_steps(
        self,
        db: AsyncSession,
        *,
        user: User,
        workflow_id: UUID,
        execution_id: UUID,
    ) -> list[WorkflowExecutionStep]:
        """Per-node step records for one run, ordered by emission.

        Reuses the canonical membership gate (404 for non-members) and 404s if
        the execution doesn't belong to ``workflow_id`` so steps never leak
        across workflows. Loads steps with a direct ``select`` (never the lazy
        ``exec_row.steps`` relationship) to stay safe in the async session.
        """
        from fastapi import HTTPException, status

        wf = await self._fetch_workflow_for_user(
            db, user_id=user.id, workflow_id=workflow_id
        )
        if wf is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        exec_row = (
            await db.execute(
                select(WorkflowExecution.id).where(
                    WorkflowExecution.id == execution_id,
                    WorkflowExecution.workflow_id == workflow_id,
                )
            )
        ).scalar_one_or_none()
        if exec_row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        stmt = (
            select(WorkflowExecutionStep)
            .where(WorkflowExecutionStep.execution_id == execution_id)
            .order_by(WorkflowExecutionStep.step_index.asc())
        )
        return list((await db.execute(stmt)).scalars().all())

    async def approve_hitl(
        self,
        db: AsyncSession,
        *,
        user: User,
        workflow_id: UUID,
        execution_id: UUID,
        approved: bool,
        feedback: str | None,
    ) -> None:
        row = (
            await db.execute(select(WFRow).where(WFRow.id == workflow_id, WFRow.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if row is None:
            from fastapi import HTTPException, status

            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        await ensure_workspace_membership(db, user_id=user.id, workspace_id=row.workspace_id)

        exec_row = (
            await db.execute(
                select(WorkflowExecution).where(
                    WorkflowExecution.id == execution_id,
                    WorkflowExecution.workflow_id == workflow_id,
                )
            )
        ).scalar_one_or_none()
        if exec_row is None:
            from fastapi import HTTPException, status

            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        redis = _redis_global()
        payload = {
            "approved": approved,
            "feedback": feedback,
            "decided_at": _utcnow().isoformat(),
        }
        await redis.set(
            _approval_key(execution_id),
            json.dumps(payload),
            ex=EXECUTION_TTL_SECONDS,
        )

    # ---------------------------- execution ---------------------------------

    async def _poll_approval(self, redis, execution_id: UUID) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + HITL_POLL_TIMEOUT_SECONDS
        key = _approval_key(execution_id)
        while asyncio.get_running_loop().time() < deadline:
            raw = await redis.get(key)
            if raw:
                try:
                    return json.loads(raw if isinstance(raw, str) else raw.decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return {"approved": False, "feedback": None}
            await asyncio.sleep(0.4)
        raise TimeoutError("approval window expired")

    async def _finalize_success(
        self,
        *,
        exec_row: WorkflowExecution,
        output_data: dict[str, Any],
        loop_started: float,
    ) -> None:
        elapsed_ms = int((asyncio.get_running_loop().time() - loop_started) * 1000)
        exec_row.completed_at = _utcnow()
        exec_row.duration_ms = elapsed_ms
        exec_row.status = WorkflowExecutionStatus.COMPLETED
        exec_row.output_data = output_data
        token_map = await self._pii.load_token_map(str(exec_row.id))
        if token_map:
            exec_row.output_data = await deep_restore(exec_row.output_data or {}, self._pii, token_map)
            await self._pii.delete_token_map(str(exec_row.id))

    # ------------------------- per-node step records ------------------------

    @staticmethod
    def _step_status(ev: dict[str, Any]) -> WorkflowExecutionStepStatus:
        s = ev.get("status")
        if s == "failed":
            return WorkflowExecutionStepStatus.FAILED
        if s == "skipped":
            return WorkflowExecutionStepStatus.SKIPPED
        return WorkflowExecutionStepStatus.COMPLETED

    def _record_step(
        self,
        db: AsyncSession,
        exec_row: WorkflowExecution,
        ev: dict[str, Any],
        *,
        step_index: int,
        demo: bool,
    ) -> None:
        """Persist one ``node_complete`` event as a step row.

        Uses ``db.add(WorkflowExecutionStep(execution_id=...))`` — never
        ``exec_row.steps.append(...)``, which would trigger an async lazy-load
        of the relationship mid-stream and raise ``MissingGreenlet``. Rows are
        committed by the caller's existing ``db.commit()``.
        """
        db.add(
            WorkflowExecutionStep(
                execution_id=exec_row.id,
                step_index=step_index,
                node_id=str(ev.get("node_id") or ev.get("agent_id") or ""),
                node_name=ev.get("node_name") or ev.get("name") or ev.get("agent_name"),
                node_kind=str(ev.get("node_kind") or "agent"),
                status=self._step_status(ev),
                dry_run=bool(ev.get("dry_run")),
                demo=demo,
                input_snapshot=ev.get("input_snapshot"),
                output_snapshot=ev.get("output_snapshot"),
                duration_ms=ev.get("duration_ms"),
                completed_at=_utcnow(),
            )
        )

    def _agent_event_to_node_complete(
        self,
        ev: dict[str, Any],
        *,
        execution_id: UUID,
        workflow_input: Any,
    ) -> dict[str, Any]:
        """Synthesize a ``node_complete`` from a native/HITL ``agent_complete``.

        The native Dynamiq and LangGraph-HITL paths emit only agent-level
        events, so we translate them into the canonical per-node shape. Per-node
        upstream input isn't tracked on these paths, so ``input_snapshot``
        carries the workflow-level input as a best-effort (truthful) view.
        """
        node_id = str(ev.get("agent_id") or ev.get("node_id") or "")
        return {
            "type": "node_complete",
            "node_id": node_id,
            "agent_id": node_id,
            "node_name": ev.get("name") or ev.get("agent_name"),
            "node_kind": "agent",
            "input_snapshot": snapshot(workflow_input),
            "output_snapshot": snapshot(ev.get("content", "")),
            "status": "completed",
            "duration_ms": ev.get("duration_ms"),
            "dry_run": False,
            "execution_id": str(execution_id),
        }

    @staticmethod
    def _build_readiness(
        wd: WorkflowDefinition, dry_run_nodes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Consolidated "will this work in production?" verdict.

        Flags (a) action/data_store nodes that only dry-ran because no
        integration is connected, and (b) agent nodes with no model configured.
        """
        issues: list[dict[str, Any]] = []
        for n in dry_run_nodes:
            issues.append(
                {
                    "node_id": n.get("node_id"),
                    "node_name": n.get("node_name"),
                    "reason": "action_not_connected",
                }
            )
        for node in wd.iter_nodes():
            if getattr(node, "kind", None) == "agent" and not (
                getattr(node, "chat_model", None) or getattr(node, "model", None)
            ):
                issues.append(
                    {
                        "node_id": node.id,
                        "node_name": node.name,
                        "reason": "agent_missing_model",
                    }
                )
        return {"ready": not issues, "issues": issues}

    async def execute_workflow(
        self,
        db: AsyncSession,
        *,
        user: User,
        workflow_id: UUID,
        request_input: dict[str, Any],
        variables: dict[str, Any] | None = None,
        demo: bool = False,
        use_real_llm: bool = False,
        branch_overrides: dict[str, str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        row = (
            await db.execute(
                select(WFRow)
                .options(selectinload(WFRow.versions))
                .where(WFRow.id == workflow_id, WFRow.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if row is None:
            yield {"type": "error", "message": "workflow not found"}
            return

        await ensure_workspace_membership(db, user_id=user.id, workspace_id=row.workspace_id)

        latest_ver = (
            await db.execute(
                select(WorkflowVersion)
                .where(WorkflowVersion.workflow_id == row.id)
                .order_by(WorkflowVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest_ver is None:
            yield {"type": "error", "message": "no workflow versions"}
            return

        wd = WorkflowDefinition.model_validate(latest_ver.definition)

        # Demo / "Test workflow" path — fully mocked (no real LLM unless opted
        # in, no integration call). Surfaces the same SSE event shape as the
        # real executor so the frontend doesn't need to branch. Unlike before,
        # demo runs now persist a lightweight ``demo=True`` WorkflowExecution +
        # step rows so the test-run inspector can reopen them. Demo rows are
        # excluded from the default executions listing (see ``list_executions``)
        # so they never masquerade as production runs. Input is redacted before
        # both the run and persistence, so free-form test input never lands in
        # the DB in plaintext.
        if demo:
            from services.demo_executor import run_demo

            demo_merged: dict[str, Any] = dict(request_input)
            demo_merged.update(variables or {})
            demo_redacted, _demo_cmap = await deep_redact(demo_merged, self._pii)

            demo_row = WorkflowExecution(
                workflow_id=row.id,
                version_id=latest_ver.id,
                status=WorkflowExecutionStatus.RUNNING,
                input_data=demo_redacted,
                started_by=user.id,
                started_at=_utcnow(),
                demo=True,
            )
            db.add(demo_row)
            await db.flush()

            demo_started = asyncio.get_running_loop().time()
            demo_counter = 0
            demo_dry_run_nodes: list[dict[str, Any]] = []
            try:
                async for ev in run_demo(
                    definition=wd,
                    input_data=demo_redacted,
                    execution_id=demo_row.id,
                    settings=self.settings if use_real_llm else None,
                    branch_overrides=branch_overrides,
                    workspace_id=row.workspace_id,
                ):
                    # Stamp workflow_id so the frontend can correlate the
                    # demo run with the canvas it was launched from.
                    ev.setdefault("workflow_id", str(workflow_id))
                    if ev.get("type") == "node_complete":
                        self._record_step(
                            db, demo_row, ev, step_index=demo_counter, demo=True
                        )
                        demo_counter += 1
                        if ev.get("dry_run"):
                            demo_dry_run_nodes.append(ev)
                    if ev.get("type") == "workflow_complete":
                        demo_row.status = WorkflowExecutionStatus.COMPLETED
                        demo_row.completed_at = _utcnow()
                        demo_row.duration_ms = int(
                            (asyncio.get_running_loop().time() - demo_started) * 1000
                        )
                        demo_row.output_data = (ev.get("data") or {}).get("outputs")
                        ev["readiness"] = self._build_readiness(wd, demo_dry_run_nodes)
                        ev["live"] = False
                        ev["mode"] = "preview"
                    yield ev
                await db.commit()
            except Exception:  # noqa: BLE001 — record failure, then re-raise
                demo_row.status = WorkflowExecutionStatus.FAILED
                demo_row.completed_at = _utcnow()
                await db.commit()
                raise
            return

        merged: dict[str, Any] = dict(request_input)
        merged.update(variables or {})
        merged_redacted, cmap = await deep_redact(merged, self._pii)

        exec_row = WorkflowExecution(
            workflow_id=row.id,
            version_id=latest_ver.id,
            status=WorkflowExecutionStatus.RUNNING,
            input_data=merged_redacted,
            started_by=user.id,
            started_at=_utcnow(),
        )
        db.add(exec_row)
        await db.flush()
        if cmap:
            await self._pii.save_token_map(str(exec_row.id), cmap)
            log.info(
                "pii.redaction",
                execution_id=str(exec_row.id),
                token_count=len(cmap),
            )

        redis = _redis_global()
        loop_stamp = asyncio.get_running_loop().time()

        aggregated: dict[str, Any] = {"agent_outputs": {}}
        dynamiq_input = build_dynamiq_input(merged_redacted)

        tool_run_buffer = ToolRunBuffer(exec_row.id)
        agent_tools_by_id: dict[str, list[Any]] | None = None

        # 1) Native Dynamiq connections (Phase A): prefer these — no third-party
        #    hop, no Composio dependency. Builds a mapping for any agent tool
        #    slug whose provider is registered in the catalog AND has a stored
        #    connection in this workspace.
        from agents.native_tool_factory import (
            build_native_agent_tools,
            load_workspace_connections,
        )

        native_conns = await load_workspace_connections(
            db, workspace_id=row.workspace_id
        )
        native_mapping, native_resolved_slugs = build_native_agent_tools(
            wd, connections=native_conns
        )
        if any(native_mapping.values()):
            agent_tools_by_id = {k: list(v) for k, v in native_mapping.items()}

        # 2) Composio bridge (fallback): only fill the gaps for slugs the native
        #    layer didn't already cover.
        if self._tool_registry:
            tool_defs = await self._tool_registry.get_workspace_tools(db, row.workspace_id)
            allowed_slugs = {
                str(t["name"])
                for t in tool_defs
                if str(t["name"]).lower() not in native_resolved_slugs
            }
            stmt_i = select(Integration).where(
                Integration.workspace_id == row.workspace_id,
                Integration.status == IntegrationStatus.CONNECTED,
            )
            integrations = list((await db.execute(stmt_i)).scalars().all())

            if allowed_slugs and integrations:

                def invoke(slug: str, params: dict[str, Any]) -> dict[str, Any]:
                    assert self._tool_registry is not None
                    return self._tool_registry.sync_execute_action(
                        integrations=integrations,
                        tool_name=slug,
                        params=params,
                        execution_id=exec_row.id,
                        tool_run_buffer=tool_run_buffer,
                    )

                composio_mapping = self._dynamiq.build_agent_composio_tools(
                    wd,
                    allowed_slugs=allowed_slugs,
                    invoke=invoke,
                )
                if any(composio_mapping.values()):
                    merged: dict[str, list[Any]] = dict(agent_tools_by_id or {})
                    for aid, nodes in composio_mapping.items():
                        if not nodes:
                            continue
                        merged.setdefault(aid, []).extend(nodes)
                    agent_tools_by_id = merged

        # 3) Composio MCP meta-tools (COMPOSIO_*): wire directly to the hosted
        #    MCP endpoint. Agent nodes declare these slugs but they are absent
        #    from the legacy ToolRegistry catalog filtered above.
        from egpt_mcp.mcp_tool_registry import MCPToolRegistry

        mcp_registry = MCPToolRegistry(self.settings, redis)
        if mcp_registry._is_enabled():
            mcp_mapping = self._dynamiq.build_agent_mcp_meta_tools(
                wd,
                registry=mcp_registry,
                execution_id=exec_row.id,
                tool_run_buffer=tool_run_buffer,
            )
            if any(mcp_mapping.values()):
                merged = dict(agent_tools_by_id or {})
                for aid, nodes in mcp_mapping.items():
                    if not nodes:
                        continue
                    existing = {
                        getattr(n, "name", None) for n in merged.get(aid, [])
                    }
                    for node in nodes:
                        name = getattr(node, "name", None)
                        if name and name not in existing:
                            merged.setdefault(aid, []).append(node)
                            existing.add(name)
                agent_tools_by_id = merged

        exec_row.agent_states = {"checkpoint": [], "prior": {}, "engine": "dynamiq"}

        async def autosave(extra: dict[str, Any] | None = None) -> None:
            merged_state = dict(exec_row.agent_states or {})
            if extra:
                merged_state.update(extra)
            exec_row.agent_states = merged_state
            await redis.set(
                _state_key(exec_row.id),
                json.dumps(merged_state, default=str),
                ex=EXECUTION_TTL_SECONDS,
            )
            await db.commit()

        yield {"type": "workflow_start", "workflow_name": row.name, "workflow_id": str(row.id)}

        skip_terminal_workflow_complete = False

        # Per-node step persistence + readiness accounting, shared across all
        # three execution paths below. ``step_counter`` is a per-run local (not
        # globally unique); ``dry_run_nodes`` feeds the readiness verdict.
        step_counter = 0
        dry_run_nodes: list[dict[str, Any]] = []

        def record_node_step(ev: dict[str, Any]) -> None:
            nonlocal step_counter
            self._record_step(db, exec_row, ev, step_index=step_counter, demo=False)
            step_counter += 1
            if ev.get("dry_run"):
                dry_run_nodes.append(ev)

        # Publish-gate: real side effects only fire for a PUBLISHED workflow on a
        # production (non-demo) run. This is a non-demo branch, so ``live`` hinges
        # purely on publish status — a draft production run still previews.
        live = row.status == WorkflowStatus.PUBLISHED

        # Decide which executor to use. v2 workflows that declare a ``nodes``
        # list containing any non-agent kind require the unified-graph
        # executor; agent-only graphs (legacy or v2) keep going through the
        # native Dynamiq path so nothing changes for existing demos.
        use_extended = bool(wd.nodes) and any(
            getattr(n, "kind", "agent") != "agent" for n in wd.nodes
        )

        try:
            if use_extended:
                from agents.extended_executor import ExtendedWorkflowExecutor

                executor = ExtendedWorkflowExecutor(
                    self.settings,
                    dynamiq=self._dynamiq,
                    db=db,
                    workspace_id=row.workspace_id,
                    workflow_id=row.id,
                    workspace_connections=native_conns,
                    live=live,
                )
                graph_summary: Any = None
                graph_error: str | None = None
                async for ev in executor.stream(
                    definition=wd,
                    execution_id=exec_row.id,
                    input_data=dynamiq_input,
                    agent_tools_by_id=agent_tools_by_id,
                ):
                    if ev.get("type") == "workflow_start":
                        continue
                    if ev.get("type") == "wait_for_webhook":
                        exec_row.status = WorkflowExecutionStatus.HITL_WAITING
                        await autosave(
                            {
                                "awaiting_webhook": ev.get("node_id"),
                                "resume_token": ev.get("resume_token"),
                                "engine": "extended_executor",
                            }
                        )
                        yield ev
                        continue
                    if ev.get("type") == "webhook_resumed":
                        exec_row.status = WorkflowExecutionStatus.RUNNING
                        await db.commit()
                        yield ev
                        continue
                    if ev.get("type") == "workflow_complete":
                        graph_summary = ev.get("result")
                        continue
                    if ev.get("type") == "node_complete":
                        record_node_step(ev)
                        yield ev
                        continue
                    if ev.get("type") == "error":
                        graph_error = str(ev.get("message") or "workflow_failure")
                    yield ev
                aggregated["workflow"] = graph_summary
                if graph_error:
                    exec_row.status = WorkflowExecutionStatus.FAILED
                    exec_row.error_message = graph_error
                    exec_row.completed_at = _utcnow()
                    exec_row.duration_ms = int(
                        (asyncio.get_running_loop().time() - loop_stamp) * 1000
                    )
                    await db.commit()
                    yield {"type": "error", "message": graph_error}
                    return
                # Fall through to the shared success-finalization tail below.
            elif wd.human_checkpoints:
                lg = LangGraphService(self.settings)
                aggregated = {"agent_outputs": {}}

                async def consume_approval() -> dict[str, Any]:
                    verdict = await self._poll_approval(redis, exec_row.id)
                    await redis.delete(_approval_key(exec_row.id))
                    return verdict

                async for ev in lg.stream_hitl_with_dynamiq(
                    definition=wd,
                    dynamiq=self._dynamiq,
                    dynamiq_input=dynamiq_input,
                    workflow_id=row.id,
                    execution_id=exec_row.id,
                    workspace_id=row.workspace_id,
                    user_id=user.id,
                    max_iterations=10,
                    poll_approval=consume_approval,
                    agent_tools_by_id=agent_tools_by_id,
                ):
                    if ev.get("type") == "hitl_required":
                        exec_row.status = WorkflowExecutionStatus.HITL_WAITING
                        await autosave(
                            {
                                "awaiting_checkpoint": ev.get("checkpoint_id"),
                                "langgraph_thread_id": str(exec_row.id),
                                "engine": "langgraph_hitl",
                            }
                        )
                        yield ev
                        continue

                    exec_row.status = WorkflowExecutionStatus.RUNNING
                    if ev.get("type") == "workflow_complete" and ev.get("success"):
                        payload = ev.get("result") or {}
                        aos = payload.get("agent_outputs")
                        if isinstance(aos, dict):
                            aggregated["agent_outputs"].update(aos)
                        ev["readiness"] = self._build_readiness(wd, dry_run_nodes)
                        ev["live"] = live
                        ev["mode"] = "live" if live else "preview"
                        yield ev
                        skip_terminal_workflow_complete = True
                        break

                    if ev.get("type") == "agent_complete":
                        # The HITL/LangGraph path emits only agent-level events;
                        # synthesize a node_complete so step rows + the inspect
                        # drawer work here too. Kept strictly off the
                        # pause/resume path (hitl_required handled above).
                        nc = self._agent_event_to_node_complete(
                            ev, execution_id=exec_row.id, workflow_input=dynamiq_input
                        )
                        record_node_step(nc)
                        yield ev
                        yield nc
                        continue

                    if ev.get("type") == "error":
                        msg_txt = str(ev.get("message") or "error")
                        if "hitl_rejected" in msg_txt.lower() or msg_txt.endswith("hitl_rejected"):
                            exec_row.status = WorkflowExecutionStatus.CANCELLED
                        else:
                            exec_row.status = WorkflowExecutionStatus.FAILED
                        exec_row.error_message = msg_txt
                        exec_row.completed_at = _utcnow()
                        exec_row.duration_ms = int(
                            (asyncio.get_running_loop().time() - loop_stamp) * 1000
                        )
                        await db.commit()
                        yield ev
                        return

                    yield ev

                else:
                    # Completed without workflow_complete sentinel (shouldn't happen).
                    exec_row.status = WorkflowExecutionStatus.FAILED
                    exec_row.error_message = "langgraph_stream_ended_early"
                    exec_row.completed_at = _utcnow()
                    exec_row.duration_ms = int(
                        (asyncio.get_running_loop().time() - loop_stamp) * 1000
                    )
                    await db.commit()
                    yield {"type": "error", "message": exec_row.error_message}
                    return
            else:
                wf_all = self._dynamiq.hydrate_workflow(wd, agent_tools_by_id=agent_tools_by_id)
                graph_summary: Any = None
                graph_error: str | None = None
                async for ev in self._dynamiq.run_workflow_stream(wf_all, input_data=dynamiq_input):
                    if ev.get("type") == "workflow_start":
                        continue
                    if ev.get("type") == "workflow_complete":
                        graph_summary = ev.get("result")
                        if not ev.get("success", True):
                            graph_error = ev.get("message") or "workflow_failure"
                        continue
                    if ev.get("type") == "error":
                        graph_error = str(ev.get("message") or "workflow_failure")
                    if ev.get("type") == "agent_complete":
                        # Native Dynamiq emits only agent-level events; synthesize
                        # a node_complete so step rows + the inspect drawer work
                        # for pure-agent / legacy workflows too.
                        nc = self._agent_event_to_node_complete(
                            ev, execution_id=exec_row.id, workflow_input=dynamiq_input
                        )
                        record_node_step(nc)
                        yield ev
                        yield nc
                        continue
                    yield ev
                aggregated["workflow"] = graph_summary
                if graph_error:
                    exec_row.status = WorkflowExecutionStatus.FAILED
                    exec_row.error_message = graph_error
                    exec_row.completed_at = _utcnow()
                    exec_row.duration_ms = int(
                        (asyncio.get_running_loop().time() - loop_stamp) * 1000
                    )
                    await db.commit()
                    yield {"type": "error", "message": graph_error}
                    return

            await self._finalize_success(
                exec_row=exec_row,
                output_data=aggregated,
                loop_started=loop_stamp,
            )
            await db.commit()
            if not skip_terminal_workflow_complete:
                yield {
                    "type": "workflow_complete",
                    "success": True,
                    "execution_id": str(exec_row.id),
                    "result": aggregated,
                    "readiness": self._build_readiness(wd, dry_run_nodes),
                    "live": live,
                    "mode": "live" if live else "preview",
                }

        except TimeoutError as exc:
            exec_row.status = WorkflowExecutionStatus.FAILED
            exec_row.error_message = str(exc)
            exec_row.completed_at = _utcnow()
            exec_row.duration_ms = int((asyncio.get_running_loop().time() - loop_stamp) * 1000)
            await db.commit()
            yield {"type": "error", "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            log.exception("workflow.execute.failed", execution_id=str(exec_row.id))
            exec_row.status = WorkflowExecutionStatus.FAILED
            exec_row.error_message = str(exc)
            exec_row.completed_at = _utcnow()
            exec_row.duration_ms = int((asyncio.get_running_loop().time() - loop_stamp) * 1000)
            await db.commit()
            yield {"type": "error", "message": str(exc)}
        finally:
            if self._tool_registry:
                await ToolRegistry.persist_tool_run_buffer(db, tool_run_buffer)


__all__ = [
    "WorkflowService",
    "build_dynamiq_input",
    "deep_redact",
    "deep_restore",
    "ensure_workspace_membership",
    "extract_agent_blob",
]
