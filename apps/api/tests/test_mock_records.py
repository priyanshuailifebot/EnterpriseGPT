"""Realistic demo mock records — internal actions return believable ids."""

from __future__ import annotations

import pytest

from schemas.workflow import ActionNode, TriggerNode, WorkflowDefinition
from services.demo_executor import run_demo
from services.mock_responses import mock_for_action


def test_business_action_mocks_have_visible_ids() -> None:
    t = mock_for_action("http_bearer", "create_ticket", {})
    assert t and t["ticket_id"].startswith("TICKET-")
    assert t["status"] == "open"

    c = mock_for_action("http_bearer", "register_customer", {})
    assert c and c["customer_id"].startswith("CUST-")

    e = mock_for_action("http_bearer", "escalate_complaint", {})
    assert e and e["escalation_id"].startswith("ESC-")
    assert e["assigned_team"]

    s = mock_for_action("internal", "schedule_hr_interview", {})
    assert s and s["event_id"].startswith("EVT-")


def test_mock_ids_are_deterministic() -> None:
    a = mock_for_action("http_bearer", "create_ticket", {"x": 1})
    b = mock_for_action("http_bearer", "create_ticket", {"x": 1})
    assert a["ticket_id"] == b["ticket_id"]


def test_known_provider_still_wins() -> None:
    # gmail.send_email keeps its provider-specific shape (message_id), not the
    # generic business fallback.
    g = mock_for_action("gmail", "send_email", {"to": "a@b.com"})
    assert g and "message_id" in g


@pytest.mark.asyncio
async def test_demo_action_output_carries_ticket_id() -> None:
    defn = WorkflowDefinition(
        name="T",
        nodes=[
            TriggerNode(id="t", name="T", trigger_type="manual"),
            ActionNode(
                id="ticket", name="Create ticket", depends_on=["t"],
                provider="http_bearer", action_slug="create_ticket",
            ),
        ],
    )
    out = None
    async for ev in run_demo(definition=defn, step_delay_ms=0):
        if ev.get("type") == "node_complete" and ev.get("node_id") == "ticket":
            out = ev.get("output_snapshot")
    assert out and str(out.get("ticket_id", "")).startswith("TICKET-")
