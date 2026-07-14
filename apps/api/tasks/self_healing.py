"""Autonomous self-heal monitor — the headless ARQ path.

Every window it scans recent workflow runs, flags unhealthy workflows by simple
heuristics, and heals the ones that opted in (per-workflow ``self_heal`` policy,
capped by the ``AGENT_SELF_HEAL_AUTO_APPLY`` env ceiling), rate-limited by a
per-workflow cooldown and a per-pass cap. It never talks to a human.

Nothing here is imported by the request path — it runs inside the ARQ worker
process (see ``tasks.worker``).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings, get_settings
from core.database import get_session_factory
from models.workflow import Workflow
from models.workflow_execution import WorkflowExecution, WorkflowExecutionStatus
from models.workspace import Workspace
from services.healing_service import HealingService

log = structlog.get_logger(__name__)

# Below this average call duration a run almost certainly failed instantly
# rather than doing real work.
_INSTANT_FAILURE_MS = 2000
_STUCK_AFTER = timedelta(minutes=30)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _health_reason(runs: list[WorkflowExecution]) -> str | None:
    """Return a human reason string if this workflow's runs look unhealthy."""
    total = len(runs)
    now = _utcnow()
    stuck = sum(
        1
        for r in runs
        if r.status in (WorkflowExecutionStatus.RUNNING, WorkflowExecutionStatus.PENDING)
        and r.started_at is not None
        and (now - r.started_at) > _STUCK_AFTER
    )
    terminal_incomplete = sum(
        1
        for r in runs
        if r.status in (WorkflowExecutionStatus.FAILED, WorkflowExecutionStatus.CANCELLED)
    )
    incomplete = terminal_incomplete + stuck

    reasons: list[str] = []
    if total >= 2 and incomplete / total >= 0.4:
        reasons.append(f"{incomplete}/{total} runs did not complete")
    # Only completed runs — a fast-but-healthy workflow shouldn't be flagged,
    # and in-flight/failed rows have meaningless or absent durations.
    durations = [
        r.duration_ms
        for r in runs
        if r.duration_ms is not None and r.status == WorkflowExecutionStatus.COMPLETED
    ]
    if len(durations) >= 3:
        avg = sum(durations) / len(durations)
        if avg < _INSTANT_FAILURE_MS:
            reasons.append(f"average duration {avg:.0f}ms (instant failures)")
    if stuck:
        reasons.append(f"{stuck} run(s) stuck >30min")
    return "; ".join(reasons) if reasons else None


async def _scan_unhealthy(
    db: AsyncSession, settings: Settings
) -> list[tuple[Workflow, str]]:
    window_start = _utcnow() - timedelta(minutes=settings.AGENT_SELF_HEAL_WINDOW_MINUTES)
    rows = list(
        (
            await db.execute(
                select(WorkflowExecution).where(
                    WorkflowExecution.demo.is_(False),
                    WorkflowExecution.started_at >= window_start,
                )
            )
        )
        .scalars()
        .all()
    )
    by_wf: dict[UUID, list[WorkflowExecution]] = defaultdict(list)
    for r in rows:
        by_wf[r.workflow_id].append(r)

    unhealthy: list[tuple[Workflow, str]] = []
    for wf_id, runs in by_wf.items():
        reason = _health_reason(runs)
        if reason is None:
            continue
        wf = await db.get(Workflow, wf_id)
        if wf is None or wf.deleted_at is not None:
            continue
        unhealthy.append((wf, reason))
    return unhealthy


async def _heal_one(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    target: tuple[UUID, str, str, int],
) -> dict[str, Any] | None:
    wf_id, reason, policy, cooldown = target
    svc = HealingService(settings)
    try:
        # Each heal gets its own session — a single AsyncSession is not safe to
        # share across the concurrent heals below.
        async with factory() as db:
            report = await svc.heal_headless(
                db,
                wf_id,
                complaint=f"Automatic monitor flagged: {reason}",
                policy=policy,
                triggered_by="monitor",
            )
        await svc.set_cooldown(wf_id, cooldown)
        return {
            "workflow_id": str(wf_id),
            "health": report.health,
            "published": report.published,
            "new_version": report.new_version_created,
            "note": report.simulation_verdict,
        }
    except Exception as exc:  # noqa: BLE001 — isolate: one failure must not stop the pass
        log.error("self_heal.heal_one_failed", workflow_id=str(wf_id), error=str(exc))
        return None


async def monitor_and_heal(ctx: dict[str, Any]) -> dict[str, Any]:
    """ARQ cron entrypoint. Scan → filter (opt-in, cooldown, tenant switch,
    policy) → heal up to the per-pass cap, in parallel and isolated."""
    settings = get_settings()
    if not settings.AGENT_SELF_HEAL_MONITOR:
        return {"skipped": "monitor disabled"}

    factory = get_session_factory()
    svc = HealingService(settings)
    targets: list[tuple[UUID, str, str, int]] = []

    async with factory() as db:
        for wf, reason in await _scan_unhealthy(db, settings):
            if await svc.in_cooldown(wf.id):
                continue
            # Tenant-level kill-switch (audit finding J).
            ws = await db.get(Workspace, wf.workspace_id)
            if ws is not None and bool((ws.settings or {}).get("self_heal_disabled")):
                continue
            policy = svc.effective_policy(wf, settings.AGENT_SELF_HEAL_AUTO_APPLY)
            if policy == "off":
                continue
            cooldown = int(
                (wf.self_heal or {}).get("cooldown_seconds")
                or settings.AGENT_SELF_HEAL_COOLDOWN_SECONDS
            )
            targets.append((wf.id, reason, policy, cooldown))
            if len(targets) >= settings.AGENT_SELF_HEAL_MAX_PER_PASS:
                break

    results = await asyncio.gather(*(_heal_one(factory, settings, t) for t in targets))
    healed = [r for r in results if r is not None]
    log.info("self_heal.pass_complete", flagged=len(targets), healed=len(healed))
    return {"count": len(healed), "healed": healed}
