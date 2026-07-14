"""Recruitment template chain — validity + cross-workflow slug wiring.

The six workflows address each other by webhook-trigger slug + signed-link
paths. If those get out of sync the pipeline silently breaks, so pin them.
"""

from __future__ import annotations

from typing import Any

from croniter import croniter

from schemas.workflow import ActionNode, TriggerNode
from services.recruitment_templates import (
    HR_CHASER,
    HR_DECISION,
    HR_INTERVIEW,
    HR_RANKING,
    HR_SCORING,
    HR_SOURCING,
)
from services.workflow_templates import list_templates


def _node(defn: Any, node_id: str) -> Any:
    return next(n for n in defn.nodes if n.id == node_id)


def _trigger_slug(defn: Any) -> str:
    return next(n.slug for n in defn.nodes if isinstance(n, TriggerNode))


def test_seven_templates_registered() -> None:
    slugs = {t.slug for t in list_templates()}
    assert {"hr-sourcing", "hr-interview-start", "hr-interview-scoring", "hr-decision",
            "hr-chaser", "hr-ranking"} <= slugs


def test_sourcing_links_to_slot_form_then_trigger() -> None:
    # The invite links to the web slot-form page; that page POSTs to the
    # 'hr-slot' webhook trigger (W2).
    sign = _node(HR_SOURCING, "sign_slot_link")
    assert sign.params["base"] == "web"
    assert sign.params["path"] == "/hr/slot"
    assert _trigger_slug(HR_INTERVIEW) == "hr-slot"


def test_interview_routes_calls_to_scoring() -> None:
    reg = _node(HR_INTERVIEW, "register_route")
    assert reg.params["target_slug"] == "hr-scoring"
    assert _trigger_slug(HR_SCORING) == "hr-scoring"


def test_scoring_links_to_decision_trigger() -> None:
    for nid in ("sign_approve", "sign_reject"):
        assert _node(HR_SCORING, nid).params["path"].endswith("/hr-decision")
    assert _trigger_slug(HR_DECISION) == "hr-decision"


def test_decision_gate_is_human() -> None:
    # Rejection only via the explicit recruiter decision branch.
    mark_rejected = _node(HR_DECISION, "mark_rejected")
    assert mark_rejected.activate_on == {"is_approved": "false"}


def test_scheduled_templates_have_valid_cron() -> None:
    for defn in (HR_CHASER, HR_RANKING):
        trig = next(n for n in defn.nodes if isinstance(n, TriggerNode))
        assert trig.trigger_type == "schedule"
        assert croniter.is_valid(trig.schedule_cron)


def test_failure_routing_wired_on_risky_actions() -> None:
    # fetch (ATS) and start_call (Retell) route failures to a notify branch.
    assert _node(HR_SOURCING, "fetch").on_error == "route"
    assert _node(HR_INTERVIEW, "start_call").on_error == "route"
    assert isinstance(_node(HR_SOURCING, "notify_fetch_failed"), ActionNode)
