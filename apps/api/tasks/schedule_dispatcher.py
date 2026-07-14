"""Schedule-trigger runtime (P2).

Wires the `schedule` TriggerNode's `schedule_cron` — previously a
defined-but-dead field — into real execution. An ARQ cron runs this every
minute; for each *published* workflow whose live definition has a schedule
trigger that just came due, it enqueues one `run_workflow_execution` job.

Design notes:
- **No catch-up.** Only a slot whose scheduled time is within the last tick
  window fires; a workflow published mid-day won't retroactively fire earlier
  slots.
- **Multi-replica safe / idempotent per slot.** A ``SET NX`` marker keyed by
  ``(workflow_id, slot_epoch)`` ensures each slot fires exactly once even if
  several worker replicas tick simultaneously.
- Runs the workflow **as its owner** (via `run_workflow_execution`), so tenant
  scoping holds.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from croniter import croniter
from sqlalchemy import select

from core.config import get_settings
from core.database import get_session_factory
from models.workflow import Workflow, WorkflowStatus
from models.workflow_version import WorkflowVersion
from schemas.workflow import TriggerNode, WorkflowDefinition

log = structlog.get_logger(__name__)

# A slot fires only if its scheduled time is within this many seconds of now
# (the cron ticks every 60s; the slack absorbs tick jitter without catch-up).
_DUE_WINDOW_SECONDS = 90
_FIRED_KEY = "egpt:sched:fired:{workflow_id}:{slot}"


def _schedule_cron(definition: WorkflowDefinition) -> str | None:
    for node in definition.iter_nodes():
        if isinstance(node, TriggerNode) and node.trigger_type == "schedule":
            return node.schedule_cron or None
    return None


def _due_slot(cron_expr: str, now: datetime) -> int | None:
    """Return the epoch of the just-passed cron slot if it's due now (within the
    tick window), else None. No catch-up: a slot older than the window is
    ignored so a workflow published mid-day doesn't retroactively fire."""
    if not cron_expr or not croniter.is_valid(cron_expr):
        return None
    prev = croniter(cron_expr, now).get_prev(datetime)
    if (now - prev).total_seconds() > _DUE_WINDOW_SECONDS:
        return None
    return int(prev.timestamp())


async def dispatch_due_schedules(ctx: dict[str, Any]) -> dict[str, Any]:
    """ARQ cron entrypoint: enqueue runs for schedule-triggered workflows due now."""
    settings = get_settings()
    if not settings.WORKFLOW_SCHEDULER_ENABLED:
        return {"skipped": "scheduler disabled"}

    now = datetime.now(UTC)
    redis = ctx["redis"]  # ArqRedis — supports set()/enqueue_job()
    factory = get_session_factory()
    fired: list[str] = []

    async with factory() as db:
        rows = list(
            (
                await db.execute(
                    select(Workflow).where(
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
                definition = WorkflowDefinition.model_validate(version.definition)
            except Exception:  # noqa: BLE001 — a malformed stored def must not stall the loop
                continue
            cron_expr = _schedule_cron(definition)
            if not cron_expr:
                continue
            slot = _due_slot(cron_expr, now)
            if slot is None:
                continue  # not valid, or not just-now (no catch-up)

            slot_key = _FIRED_KEY.format(workflow_id=wf.id, slot=slot)
            # NX marker: whoever sets it first owns this slot.
            if not await redis.set(slot_key, "1", nx=True, ex=3600):
                continue

            await redis.enqueue_job(
                "run_workflow_execution",
                workflow_id=str(wf.id),
                triggered_by="schedule",
            )
            fired.append(str(wf.id))

    if fired:
        log.info("schedule_dispatcher.fired", count=len(fired), workflow_ids=fired)
    return {"fired": len(fired), "workflow_ids": fired}
