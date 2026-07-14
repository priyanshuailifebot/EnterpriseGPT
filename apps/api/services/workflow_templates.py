"""Prebaked v2 ``WorkflowDefinition`` templates for the demo gallery.

Each template ships with:

* a stable ``slug`` (URL-safe; used in the public list endpoint),
* a one-line ``title`` and a longer ``description`` (rendered in the UI),
* the NL ``prompt`` that originally produced the workflow — useful for users
  who want to re-derive the graph via the clarification flow,
* a fully-baked ``WorkflowDefinition`` so they can also one-click *Use as-is*.

We hand-author the definitions rather than re-running the LLM at template
time: stability beats novelty here. Re-derivation remains available through
the regular ``POST /workflows/interpret`` route.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from schemas.workflow import (
    ActionNode,
    AgentNode,
    DataStoreNode,
    MemoryNode,
    OutputParserNode,
    TriggerNode,
    WorkflowDefinition,
)
from services.recruitment_templates import (
    HR_CHASER,
    HR_DECISION,
    HR_INTERVIEW,
    HR_RANKING,
    HR_SCORING,
    HR_SOURCING,
)


@dataclass(frozen=True)
class WorkflowTemplate:
    slug: str
    title: str
    summary: str
    category: str
    prompt: str
    definition: WorkflowDefinition
    required_integrations: tuple[str, ...]


# ---------------------------------------------------------------------------
# 1. Customer Service Triage — exercises condition + wait_for_webhook
# ---------------------------------------------------------------------------


_CUSTOMER_SERVICE = WorkflowDefinition(
    name="Customer Service Triage",
    description=(
        "Live chat-style customer service agent. The agent owns the "
        "conversation; routing (existing vs new, has-ticket vs not) is "
        "decided by the LLM via tool-calls — not as visible graph edges. "
        "Memory is shared between the chat trigger and the agent so the "
        "conversation survives across turns."
    ),
    trigger="chat",
    output_format="json",
    nodes=[
        # Shared conversation memory: referenced by both the chat trigger
        # and the agent so the customer's history survives across turns.
        MemoryNode(
            id="conversation_memory",
            name="Conversation Memory",
            scope="session",
            store="redis",
            ttl_seconds=3600,
            max_turns=48,
        ),
        # Chat trigger — n8n's leftmost rounded "Customer Service Chat" box.
        TriggerNode(
            id="cs_chat",
            name="Customer Service Chat",
            trigger_type="chat",
            slug="customer-service",
            chat_welcome_message=(
                "Hi! I'm here to help. Could you share the email you've "
                "registered with us and a short description of the issue?"
            ),
            chat_memory_ref="conversation_memory",
        ),
        # The single AI agent — depends only on the chat trigger and owns
        # all tools as satellites. Memory + Output Parser hang off it too.
        AgentNode(
            id="cs_agent",
            name="Customer Service Agent",
            depends_on=["cs_chat"],
            role="Empathic, decisive customer service agent.",
            instructions=(
                "You are the first point of contact for a customer. "
                "Workflow:\n"
                "1. If the customer hasn't shared an email yet, ask for it.\n"
                "2. Call ``check_customer_exists`` with the email. If the "
                "customer is NOT found, call ``register_new_customer`` "
                "with the details they've shared.\n"
                "3. Call ``check_existing_complaints`` with the customer "
                "id. If there is an open ticket, call ``escalate_complaint``"
                " with the ticket id + a one-line summary of the new "
                "concern.\n"
                "4. Otherwise: gather enough detail to summarise the "
                "problem, then call ``create_new_ticket`` with "
                "{customer_id, category, summary, severity}.\n"
                "5. Always reply to the customer in the same language they "
                "wrote in. Be warm and concise."
            ),
            # Tools are listed both here (so the slugs are visible in the
            # agent's tools list) AND as satellite ActionNodes below (so
            # the editor renders them as separately-configurable boxes).
            tools=[
                "check_customer_exists",
                "register_new_customer",
                "check_existing_complaints",
                "create_new_ticket",
                "escalate_complaint",
            ],
            memory_ref="conversation_memory",
            output_parser_ref="cs_output_parser",
            chat_model={
                "provider": "openai",
                "model": "gpt-4o",
                "temperature": 0.0,
            },
        ),
        # ---- Tool satellites — each is a real ActionNode/DataStoreNode ----
        DataStoreNode(
            id="tool_check_customer",
            name="Check Customer Exists",
            parent_agent_id="cs_agent",
            op="read",
            table="customers",
            key="{{ input.email }}",
            tool_description=(
                "Returns the customer record keyed by email, or "
                '{"found": false} if absent. Call this BEFORE creating a '
                "new customer."
            ),
        ),
        DataStoreNode(
            id="tool_register_customer",
            name="Register New Customer",
            parent_agent_id="cs_agent",
            op="write",
            table="customers",
            key="{{ input.email }}",
            payload={
                "email": "{{ input.email }}",
                "name": "{{ input.name }}",
                "created_via": "chat",
            },
            tool_description=(
                "Creates a new customer row keyed by email. Call this "
                "ONLY when check_customer_exists returned not-found."
            ),
        ),
        DataStoreNode(
            id="tool_check_complaints",
            name="Check Existing Complaints",
            parent_agent_id="cs_agent",
            op="query",
            table="tickets",
            filter={
                "customer_id": "{{ input.customer_id }}",
                "status": "open",
            },
            tool_description=(
                "Lists open tickets for the customer. Returns "
                '{"rows": [...], "count": N}.'
            ),
        ),
        DataStoreNode(
            id="tool_create_ticket",
            name="Create New Ticket",
            parent_agent_id="cs_agent",
            op="write",
            table="tickets",
            key="{{ input.ticket_id }}",
            payload={
                "customer_id": "{{ input.customer_id }}",
                "category": "{{ input.category }}",
                "summary": "{{ input.summary }}",
                "severity": "{{ input.severity }}",
                "status": "open",
            },
            tool_description=(
                "Opens a new ticket for the customer. Call ONLY when "
                "there are no open complaints already."
            ),
        ),
        ActionNode(
            id="tool_escalate",
            name="Escalate Complaint",
            parent_agent_id="cs_agent",
            provider="slack",
            action_slug="slack_post_message",
            params={
                "channel": "#cx-escalations",
                "text": (
                    "Existing ticket {{ input.ticket_id }} — customer "
                    "{{ input.customer_id }} has a new concern: "
                    "{{ input.summary }}"
                ),
            },
            tool_description=(
                "Posts a Slack escalation about an EXISTING open ticket. "
                "Call ONLY when check_existing_complaints returned at "
                "least one row."
            ),
        ),
        # ---- Output parser satellite ----
        OutputParserNode(
            id="cs_output_parser",
            name="Structured Output",
            parent_agent_id="cs_agent",
            json_schema={
                "type": "object",
                "required": ["reply_to_customer", "action_taken"],
                "properties": {
                    "reply_to_customer": {"type": "string"},
                    "action_taken": {
                        "type": "string",
                        "enum": [
                            "registered_new_customer",
                            "escalated_existing",
                            "opened_new_ticket",
                            "asked_for_info",
                            "resolved_inline",
                        ],
                    },
                    "ticket_id": {"type": ["string", "null"]},
                    "customer_id": {"type": ["string", "null"]},
                },
            },
            max_retries=2,
        ),
    ],
)


_TEMPLATES: tuple[WorkflowTemplate, ...] = (
    WorkflowTemplate(
        slug="customer-service-triage",
        title="Customer Service Triage",
        summary=(
            "Inbound inquiry → existing/new branch → existing-complaint "
            "branch → escalate or open a new ticket."
        ),
        category="customer-service",
        prompt=(
            "I am a customer service agent. I receive inbound inquiries from "
            "customers. First, validate the customer (existing vs new). If "
            "existing, check if there is an existing complaint in the system. "
            "If yes, escalate internally. If no, create a new ticket, "
            "understand the problem, attempt resolution, and raise the "
            "ticket. If the customer is new, first register them, then "
            "understand the problem, attempt resolution, and raise the ticket."
        ),
        definition=_CUSTOMER_SERVICE,
        required_integrations=(
            "pipedream",
            "slack",
            "http_bearer",
        ),
    ),
    # ---- HR recruitment chain (event-boundary). Correlated by candidate_id; ----
    # ---- sibling workflows addressed by webhook-trigger slug. See           ----
    # ---- services/recruitment_templates.py + docs/RECRUITMENT_WORKFLOW_PLAN. ----
    WorkflowTemplate(
        slug="hr-sourcing",
        title="HR Sourcing (batch)",
        summary=(
            "Recruiter starts a run for a role → fetch matching candidates from "
            "the ATS → email every candidate a signed slot-selection link."
        ),
        category="hr",
        prompt=(
            "Source candidates for a role: pull matching résumés from the ATS "
            "and email each candidate a link to pick an interview slot and "
            "language. Kicks off the recruitment pipeline for the whole batch."
        ),
        definition=HR_SOURCING,
        required_integrations=("ats", "gmail"),
    ),
    WorkflowTemplate(
        slug="hr-interview-start",
        title="HR Interview — Start Call",
        summary=(
            "Candidate submits their slot → place the outbound voice interview "
            "call (Retell) and register it for scoring. Triggered by the signed "
            "slot link."
        ),
        category="hr",
        prompt=(
            "When a candidate picks their slot and language, place the "
            "telephonic AI interview call and register it so scoring runs when "
            "the call ends."
        ),
        definition=HR_INTERVIEW,
        required_integrations=("mcp", "gmail"),
    ),
    WorkflowTemplate(
        slug="hr-interview-scoring",
        title="HR Interview — Score & Review",
        summary=(
            "Call ends → fetch transcript → score against the rubric → store "
            "results → email the candidate a summary and send the recruiter "
            "signed approve/reject links (human review gate)."
        ),
        category="hr",
        prompt=(
            "After the interview call ends, transcribe and score the candidate "
            "across local-market knowledge, industry experience, communication, "
            "past experience, customer engagement, consultative approach, "
            "objection handling and advisor skills; summarise to the candidate "
            "and ask a recruiter to approve or reject."
        ),
        definition=HR_SCORING,
        required_integrations=("mcp", "gmail"),
    ),
    WorkflowTemplate(
        slug="hr-decision",
        title="HR Interview — Decision",
        summary=(
            "Recruiter approves → check HR availability and book the HR round; "
            "rejects → record the outcome. Triggered by the approve/reject link."
        ),
        category="hr",
        prompt=(
            "On a recruiter's approve/reject decision, either schedule the HR "
            "round based on HR-team availability, or record that the candidate "
            "did not advance."
        ),
        definition=HR_DECISION,
        required_integrations=("pipedream", "gmail"),
    ),
    WorkflowTemplate(
        slug="hr-chaser",
        title="HR Chaser — Slot Reminders",
        summary="Daily scheduled sweep: remind sourced candidates who haven't picked a slot.",
        category="hr",
        prompt="Every day, remind candidates who were sourced but haven't yet chosen an interview slot.",
        definition=HR_CHASER,
        required_integrations=("gmail",),
    ),
    WorkflowTemplate(
        slug="hr-ranking",
        title="HR Ranking — Stack Rank",
        summary="Weekly scheduled sweep: stack-rank interviewed candidates by overall score.",
        category="hr",
        prompt="Every week, stack-rank the interviewed candidates by their overall interview score.",
        definition=HR_RANKING,
        required_integrations=(),
    ),
)


def list_templates() -> tuple[WorkflowTemplate, ...]:
    return _TEMPLATES


def get_template(slug: str) -> WorkflowTemplate | None:
    for t in _TEMPLATES:
        if t.slug == slug:
            return t
    return None


def public_catalog() -> list[dict[str, Any]]:
    return [
        {
            "slug": t.slug,
            "title": t.title,
            "summary": t.summary,
            "category": t.category,
            "prompt": t.prompt,
            "required_integrations": list(t.required_integrations),
            "definition": t.definition.model_dump(mode="json"),
        }
        for t in _TEMPLATES
    ]


__all__ = [
    "WorkflowTemplate",
    "list_templates",
    "get_template",
    "public_catalog",
]
