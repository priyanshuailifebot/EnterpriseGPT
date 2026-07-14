"""P2 — schedule-trigger dispatcher: due-slot logic + guards."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from schemas.workflow import ActionNode, TriggerNode, WorkflowDefinition
from tasks.schedule_dispatcher import _due_slot, _schedule_cron, dispatch_due_schedules


def _sched_def(cron: str) -> WorkflowDefinition:
    return WorkflowDefinition(
        name="sched",
        nodes=[
            TriggerNode(id="t", name="T", trigger_type="schedule", schedule_cron=cron),
            ActionNode(id="a", name="A", provider="http_bearer", action_slug="x", depends_on=["t"]),
        ],
    )


def test_schedule_cron_extracted() -> None:
    assert _schedule_cron(_sched_def("*/5 * * * *")) == "*/5 * * * *"


def test_schedule_cron_none_for_manual() -> None:
    wd = WorkflowDefinition(
        name="m",
        nodes=[TriggerNode(id="t", name="T", trigger_type="manual")],
    )
    assert _schedule_cron(wd) is None


def test_due_slot_fires_for_just_passed_minute() -> None:
    now = datetime(2026, 7, 6, 15, 0, 30, tzinfo=UTC)  # 30s into the minute
    slot = _due_slot("* * * * *", now)  # every minute → prev = 15:00:00, 30s ago
    assert slot == int(datetime(2026, 7, 6, 15, 0, 0, tzinfo=UTC).timestamp())


def test_due_slot_no_catch_up_for_stale_slot() -> None:
    now = datetime(2026, 7, 6, 15, 0, 0, tzinfo=UTC)
    # 3:30am daily — the last slot was ~11.5h ago, far outside the tick window.
    assert _due_slot("30 3 * * *", now) is None


def test_due_slot_invalid_cron() -> None:
    assert _due_slot("not a cron", datetime(2026, 7, 6, 15, 0, 0, tzinfo=UTC)) is None


@pytest.mark.asyncio
async def test_dispatch_disabled_by_default() -> None:
    # WORKFLOW_SCHEDULER_ENABLED defaults False → no DB/redis access.
    out: dict[str, Any] = await dispatch_due_schedules({})
    assert out.get("skipped")
