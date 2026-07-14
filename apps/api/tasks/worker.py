"""ARQ worker entrypoint.

Run with::

    arq tasks.worker.WorkerSettings

Hosts the self-heal cron. The worker shares the app's Redis and DB engine
(initialized on startup here, since this process doesn't go through the FastAPI
lifespan).
"""

from __future__ import annotations

from typing import Any

import structlog
from arq import cron
from arq.connections import RedisSettings

from core.config import get_settings
from core.database import dispose_engine, init_engine
from core.redis import dispose_redis, init_redis
from tasks.schedule_dispatcher import dispatch_due_schedules
from tasks.self_healing import monitor_and_heal
from tasks.workflow_runner import run_workflow_execution

log = structlog.get_logger(__name__)


def _cron_minutes(interval: int) -> set[int]:
    """Minutes-past-the-hour at which to fire. ARQ cron takes a minute set, not
    an interval, so translate an every-N-minutes cadence into one (falling back
    to hourly when N doesn't evenly divide the hour)."""
    if interval <= 0 or interval >= 60 or 60 % interval != 0:
        return {0}
    return set(range(0, 60, interval))


async def _on_startup(ctx: dict[str, Any]) -> None:
    init_engine()
    init_redis()
    log.info("arq.worker.startup")


async def _on_shutdown(ctx: dict[str, Any]) -> None:
    await dispose_redis()
    await dispose_engine()
    log.info("arq.worker.shutdown")


class WorkerSettings:
    functions = [monitor_and_heal, run_workflow_execution, dispatch_due_schedules]
    cron_jobs = [
        cron(
            monitor_and_heal,
            minute=_cron_minutes(get_settings().AGENT_SELF_HEAL_INTERVAL_MINUTES),
            run_at_startup=False,
        ),
        # Every minute; the dispatcher itself decides which workflows are due.
        cron(dispatch_due_schedules, minute=set(range(60)), run_at_startup=False),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    # A heal pass can create a version, run a demo, and publish — give it room.
    job_timeout = 600
    max_jobs = 4
