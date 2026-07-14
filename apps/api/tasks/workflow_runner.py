"""Headless workflow execution — run a workflow to completion in the worker.

Shared primitive for the schedule dispatcher (cron-fired workflows) and any
other programmatic run. Runs the workflow **as its creator** so tenant scoping +
RBAC hold for downstream actions/RAG/connections — mirroring the webhook trigger
route (`routers/workflows.py:webhook_trigger_route`) and the self-heal run-as
pattern.

Under the event-boundary architecture (see docs/RECRUITMENT_WORKFLOW_PLAN.md
§0.6) every execution is short — no in-execution human waits — so draining the
`execute_workflow` async generator to completion here is safe. As a guard, if an
execution unexpectedly parks on a ``wait_for_webhook`` we stop draining and
return ``status="waiting"`` rather than blocking the worker on the in-process
poll.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from core.config import get_settings
from core.database import get_session_factory
from models.user import User
from models.workflow import Workflow
from services.workflow_service import WorkflowService

log = structlog.get_logger(__name__)


async def run_workflow_execution(
    ctx: dict[str, Any],
    *,
    workflow_id: str,
    input_data: dict[str, Any] | None = None,
    variables: dict[str, Any] | None = None,
    triggered_by: str = "system",
) -> dict[str, Any]:
    """Run one workflow execution to completion, as its owner. Never raises."""
    wid = UUID(str(workflow_id))
    factory = get_session_factory()
    service = WorkflowService(get_settings())

    execution_id: str | None = None
    status_label = "unknown"
    error: str | None = None
    try:
        async with factory() as db:
            workflow = await db.get(Workflow, wid)
            if workflow is None or workflow.deleted_at is not None:
                return {"ok": False, "workflow_id": str(wid), "error": "workflow not found"}
            owner = (
                await db.execute(select(User).where(User.id == workflow.created_by))
            ).scalar_one_or_none()
            if owner is None:
                return {"ok": False, "workflow_id": str(wid), "error": "workflow owner missing"}

            async for ev in service.execute_workflow(
                db,
                user=owner,
                workflow_id=wid,
                request_input=input_data or {},
                variables=variables or {},
                demo=False,
            ):
                etype = ev.get("type")
                if ev.get("execution_id"):
                    execution_id = str(ev.get("execution_id"))
                if etype == "workflow_complete":
                    status_label = "completed"
                elif etype == "error":
                    status_label = "failed"
                    error = ev.get("message") or error
                elif etype == "wait_for_webhook":
                    # Guard: a headless run must not block on the in-process
                    # park (event-boundary design means this shouldn't happen).
                    status_label = "waiting"
                    log.warning(
                        "workflow_runner.unexpected_park",
                        workflow_id=str(wid),
                        node_id=ev.get("node_id"),
                    )
                    break
    except Exception as exc:  # noqa: BLE001 — a worker job must not crash the worker
        log.error("workflow_runner.failed", workflow_id=str(wid), error=str(exc))
        return {"ok": False, "workflow_id": str(wid), "error": str(exc)}

    log.info(
        "workflow_runner.done",
        workflow_id=str(wid),
        execution_id=execution_id,
        status=status_label,
        triggered_by=triggered_by,
    )
    return {
        "ok": error is None and status_label != "failed",
        "workflow_id": str(wid),
        "execution_id": execution_id,
        "status": status_label,
        "error": error,
    }
