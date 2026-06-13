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
    ConditionNode,
    DataStoreNode,
    ForEachNode,
    IfNode,
    MemoryNode,
    MergeNode,
    OutputParserNode,
    TriggerNode,
    WaitForWebhookNode,
    WorkflowDefinition,
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


# ---------------------------------------------------------------------------
# 2. HR Recruitment — exercises for_each + wait_for_webhook + voice MCP
# ---------------------------------------------------------------------------


_HR_RECRUITMENT = WorkflowDefinition(
    name="HR Recruitment — Field Sales Advisor",
    description=(
        "JD → résumé match from Darwin Box → email shortlist → candidate "
        "picks slot + language → outbound voice interview → score → schedule "
        "qualified candidates with HR. Mirrors the n8n recruitment shape."
    ),
    trigger="manual",
    output_format="json",
    nodes=[
        # ---------- Row 1 of the n8n screenshot ----------
        TriggerNode(
            id="start_recruitment",
            name="Start Recruitment Process",
            trigger_type="manual",
            slug="hr-recruitment-start",
            form_fields=[
                {
                    "key": "jd_text",
                    "label": "Job description",
                    "type": "text",
                    "required": True,
                },
                {
                    "key": "role_title",
                    "label": "Role title",
                    "type": "text",
                    "required": True,
                },
            ],
        ),
        ActionNode(
            id="fetch_cvs",
            name="Fetch CVs from DarwinBox",
            depends_on=["start_recruitment"],
            provider="http_bearer",
            action_slug="darwinbox_resume_search",
            params={
                "method": "GET",
                "url_suffix": "/api/recruitment/candidates/search",
                "query": {
                    "jd": "{{ start_recruitment.jd_text }}",
                    "role": "{{ start_recruitment.role_title }}",
                    "limit": 10,
                },
            },
        ),
        DataStoreNode(
            id="store_candidates",
            name="Store Candidates in Dashboard",
            depends_on=["fetch_cvs"],
            op="write",
            table="candidates",
            key="{{ start_recruitment.role_title }}",
            payload={
                "role": "{{ start_recruitment.role_title }}",
                "candidates": "{{ fetch_cvs.data }}",
                "status": "shortlisted",
            },
        ),
        ActionNode(
            id="send_invite_email",
            name="Send Interview Invitation Email",
            depends_on=["store_candidates"],
            provider="gmail",
            action_slug="gmail_send",
            params={
                "to": "{{ fetch_cvs.data.0.email }}",
                "subject": "Interview invitation — {{ start_recruitment.role_title }}",
                "html_body": (
                    "<p>Hi {{ fetch_cvs.data.0.name }},</p>"
                    "<p>Please pick an interview slot here: "
                    "{{ slot_form.public_url }}</p>"
                ),
            },
        ),
        DataStoreNode(
            id="status_email_sent",
            name="Update Status: Email Sent",
            depends_on=["send_invite_email"],
            op="write",
            table="candidates",
            key="{{ start_recruitment.role_title }}",
            payload={"status": "email_sent"},
        ),

        # ---------- Row 2 of the n8n screenshot ----------
        TriggerNode(
            id="slot_form",
            name="Interview Slot Selection Form",
            trigger_type="form",
            slug="interview-slot",
            form_fields=[
                {
                    "key": "slot_iso",
                    "label": "Preferred slot",
                    "type": "text",
                    "required": True,
                },
                {
                    "key": "language",
                    "label": "Preferred language",
                    "type": "choice",
                    "options": ["en-IN", "hi-IN", "ta-IN", "te-IN", "mr-IN"],
                    "required": True,
                },
            ],
        ),
        WaitForWebhookNode(
            id="wait_slot",
            name="Wait for slot submission",
            depends_on=["slot_form"],
            description=(
                "Candidate fills the slot selection form; submit POSTs to "
                "the workflow's resume URL."
            ),
            timeout_seconds=604800,
        ),
        DataStoreNode(
            id="store_schedule",
            name="Store Interview Schedule",
            depends_on=["wait_slot"],
            op="write",
            table="interview_schedule",
            key="{{ wait_slot.candidate_id }}",
            payload={
                "slot_iso": "{{ wait_slot.slot_iso }}",
                "language": "{{ wait_slot.language }}",
                "status": "scheduled",
            },
        ),

        # ---------- The composite AI agent ----------
        AgentNode(
            id="interview_conductor",
            name="AI Interview Conductor",
            role="Conversational interview agent.",
            instructions=(
                "Conduct the interview by calling start_interview with the "
                "candidate phone, JD summary, and chosen language. Poll "
                "get_interview_status until the call ends, then call "
                "get_interview_transcript. Emit {call_id, transcript, "
                "language}."
            ),
            tools=[
                "start_interview",
                "get_interview_status",
                "get_interview_transcript",
            ],
            depends_on=["store_schedule"],
        ),
        ActionNode(
            id="parse_assessment",
            name="Interview Assessment Parser",
            depends_on=["interview_conductor"],
            provider="mcp",
            action_slug="score_interview",
            params={
                "call_id": "{{ interview_conductor.call_id }}",
                "rubric": [
                    "local_market_knowledge",
                    "relevant_industry_experience",
                    "communication_skills",
                    "past_experience",
                    "customer_engagement_approach",
                    "consultative_approach",
                    "objection_handling",
                    "customer_advisor_skills",
                ],
            },
        ),
        DataStoreNode(
            id="store_results",
            name="Store Interview Results",
            depends_on=["parse_assessment"],
            op="write",
            table="interview_results",
            key="{{ wait_slot.candidate_id }}",
            payload={
                "scores": "{{ parse_assessment.data.scores }}",
                "overall": "{{ parse_assessment.data.overall }}",
                "rationale": "{{ parse_assessment.data.rationale }}",
            },
        ),
        ActionNode(
            id="send_summary",
            name="Send Interview Summary to Candidate",
            depends_on=["store_results"],
            provider="gmail",
            action_slug="gmail_send",
            params={
                "to": "{{ fetch_cvs.data.0.email }}",
                "subject": "Your interview summary",
                "html_body": (
                    "<p>Thank you for interviewing.</p>"
                    "<p>Overall: {{ parse_assessment.data.overall }}%</p>"
                    "<p>{{ parse_assessment.data.rationale }}</p>"
                ),
            },
        ),
        IfNode(
            id="check_score",
            name="Check Score > 75%",
            depends_on=["send_summary"],
            expression="$.parse_assessment.data.overall > 75",
        ),
        ActionNode(
            id="check_availability",
            name="Check HR Team Availability",
            depends_on=["check_score"],
            activate_on={"check_score": "true"},
            provider="pipedream",
            action_slug="pipedream_run_action",
            params={
                "app": "google_calendar",
                "action": "freebusy_query",
                "calendars": ["hr-team@company.com"],
                "time_min": "{{ wait_slot.slot_iso }}",
                "time_max": "{{ wait_slot.slot_iso }}",
            },
        ),
        ActionNode(
            id="schedule_hr",
            name="Schedule HR Interview",
            depends_on=["check_availability"],
            provider="pipedream",
            action_slug="pipedream_calendly_create_event",
            params={
                "calendar": "hr-team@company.com",
                "attendees": ["{{ fetch_cvs.data.0.email }}"],
                "start": "{{ check_availability.data.next_slot }}",
                "duration_minutes": 30,
                "summary": "HR round — {{ start_recruitment.role_title }}",
            },
        ),
        DataStoreNode(
            id="update_scheduled",
            name="Update: HR Interview Scheduled",
            depends_on=["schedule_hr"],
            op="write",
            table="interview_results",
            key="{{ wait_slot.candidate_id }}",
            payload={
                "hr_interview_scheduled_at": "{{ schedule_hr.data.start }}",
                "status": "hr_round_booked",
            },
        ),
        DataStoreNode(
            id="update_below",
            name="Update: Below Threshold",
            depends_on=["check_score"],
            activate_on={"check_score": "false"},
            op="write",
            table="interview_results",
            key="{{ wait_slot.candidate_id }}",
            payload={"status": "below_threshold"},
        ),

        # ---------- Row 3 of the n8n screenshot — ranking sweep ----------
        DataStoreNode(
            id="get_all_for_ranking",
            name="Get All Candidates for Ranking",
            depends_on=["update_scheduled", "update_below"],
            op="query",
            table="interview_results",
            filter={},
        ),
        AgentNode(
            id="add_stack_ranking",
            name="Add Stack Ranking",
            role="Recruiting analyst.",
            instructions=(
                "Take the rows array from the upstream data_store query. "
                "Sort by overall score descending. Emit JSON: "
                "[{candidate_id, overall, rank}]."
            ),
            depends_on=["get_all_for_ranking"],
        ),
        DataStoreNode(
            id="update_ranking",
            name="Update Ranking in Dashboard",
            depends_on=["add_stack_ranking"],
            op="write",
            table="candidate_ranking",
            key="{{ start_recruitment.role_title }}",
            payload={
                "ranking": "{{ add_stack_ranking }}",
            },
        ),

        MergeNode(
            id="finalise",
            name="Finalise",
            depends_on=["update_ranking"],
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
    WorkflowTemplate(
        slug="hr-recruitment-field-sales-advisor",
        title="HR Recruitment — Field Sales Advisor",
        summary=(
            "JD → résumé match from Darwin Box → email invites → candidate "
            "picks slot + language → voice interview → score → schedule "
            "qualified candidates."
        ),
        category="hr",
        prompt=(
            "I'm an HR Recruitment agent for a life insurance company. Based "
            "on the JD for Field Sales Advisor, fetch the best matching CVs "
            "by resume parsing from Darwin Box HRMS, then email interview "
            "links to all candidates. Candidates receive the email and "
            "choose the slot for telephonic interview and their preferred "
            "language. Based on the selection, candidates receive the "
            "telephone call for interview and answer the questions in a "
            "humanised experience covering local market knowledge, industry "
            "experience, communication skills, past experience, customer "
            "engagement approach, consultative approach, objection handling "
            "and customer advisor skills. Post-interview the candidates get "
            "summarisation and stack ranking; if a candidate scores >75%, "
            "schedule the interview with HR team based on HR availability. "
            "All candidate interview status should be available on a "
            "dashboard for HR management."
        ),
        definition=_HR_RECRUITMENT,
        required_integrations=(
            "http_bearer",        # Darwin Box
            "sendgrid",
            "mcp",                # Voice-MCP server (Retell)
            "pipedream",
            "postgres",
        ),
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
