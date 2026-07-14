"""Production HR recruitment workflow chain (event-boundary decomposition).

Rebuilds the old single-workflow ``_HR_RECRUITMENT`` demo into six short,
correlated workflows (see docs/RECRUITMENT_WORKFLOW_PLAN.md §0.6). Correlation
key throughout is ``candidate_id``; sibling workflows are referenced by their
webhook-trigger *slug* (resolved at runtime — ids don't exist at template time),
and email links are signed via ``internal.sign_link`` (which bakes in the
workspace so the id-free ``/workflows/slug/{trigger_slug}`` route can resolve).

Connectors are placeholders a tenant wires to real accounts:
  * ``ats``      — applicant/résumé source (the API you provide). Contract:
                   returns ``data: [{candidate_id, name, email, phone, ...}]``.
  * ``gmail``    — Google Workspace send.
  * ``mcp``      — the voice-MCP server (Retell): start_interview /
                   get_interview_transcript / score_interview.
  * ``pipedream``— Google Calendar free/busy + Calendly event creation.
  * ``internal`` — platform actions: sign_link, register_voice_route.
"""

from __future__ import annotations

from schemas.workflow import (
    ActionNode,
    AgentNode,
    DataStoreNode,
    ForEachNode,
    IfNode,
    TriggerNode,
    WorkflowDefinition,
)

_RUBRIC = [
    "local_market_knowledge",
    "relevant_industry_experience",
    "communication_skills",
    "past_experience",
    "customer_engagement_approach",
    "consultative_approach",
    "objection_handling",
    "customer_advisor_skills",
]

# Sibling trigger slugs — how the workflows address each other.
_SLOT_SLUG = "hr-slot"
_SCORING_SLUG = "hr-scoring"
_DECISION_SLUG = "hr-decision"


# ---------------------------------------------------------------------------
# W1 — Sourcing (manual, recruiter-initiated per role)
# ---------------------------------------------------------------------------
HR_SOURCING = WorkflowDefinition(
    name="HR Sourcing",
    description=(
        "Recruiter starts a sourcing run for a role: fetch matching candidates "
        "from the ATS, store the shortlist, and email each candidate a signed "
        "slot-selection link. Batch (for_each) over all candidates."
    ),
    trigger="manual",
    output_format="json",
    nodes=[
        TriggerNode(
            id="start",
            name="Start Sourcing",
            trigger_type="manual",
            slug="hr-sourcing-start",
            form_fields=[
                {"key": "jd_text", "label": "Job description", "type": "text", "required": True},
                {"key": "role_title", "label": "Role title", "type": "text", "required": True},
            ],
        ),
        ActionNode(
            id="fetch",
            name="Fetch Candidates (ATS)",
            depends_on=["start"],
            provider="ats",
            action_slug="ats_search_candidates",
            on_error="route",  # fetch failure → notify, don't crash silently
            params={
                "jd": "{{ start.jd_text }}",
                "role": "{{ start.role_title }}",
                "limit": 25,
            },
        ),
        ActionNode(
            id="notify_fetch_failed",
            name="Notify Recruiter: Sourcing Failed",
            depends_on=["fetch"],
            activate_on={"fetch": "failed"},
            provider="gmail",
            action_slug="gmail_send",
            params={
                "to": "recruiting@company.com",
                "subject": "Sourcing failed — {{ start.role_title }}",
                "html_body": "<p>The ATS candidate search failed for {{ start.role_title }}.</p>",
            },
        ),
        DataStoreNode(
            id="store_candidates",
            name="Store Shortlist",
            depends_on=["fetch"],
            activate_on={"fetch": "ok"},
            op="write",
            table="candidates",
            key="{{ start.role_title }}",
            payload={
                "role": "{{ start.role_title }}",
                "candidates": "{{ fetch.data }}",
                "status": "sourced",
            },
        ),
        ForEachNode(
            id="per_candidate",
            name="For Each Candidate",
            depends_on=["store_candidates"],
            items_from="fetch",
            items_path="$.data",
            item_var="candidate",
            body=["sign_slot_link", "send_invite"],
            max_concurrency=5,
        ),
        ActionNode(
            id="sign_slot_link",
            name="Build Signed Slot Link",
            depends_on=["per_candidate"],
            provider="internal",
            action_slug="sign_link",
            params={
                # Points at the web form page; the page POSTs slot+language to
                # /api/v1/workflows/slug/hr-slot with this same ctx token.
                "base": "web",
                "path": "/hr/slot",
                "context": {
                    "candidate_id": "{{ candidate.candidate_id }}",
                    "name": "{{ candidate.name }}",
                    "email": "{{ candidate.email }}",
                    "phone": "{{ candidate.phone }}",
                    "role_title": "{{ start.role_title }}",
                    "purpose": "slot",
                },
            },
        ),
        ActionNode(
            id="send_invite",
            name="Send Interview Invitation",
            depends_on=["per_candidate"],
            provider="gmail",
            action_slug="gmail_send",
            on_error="continue",  # one candidate's email failure must not abort the batch
            params={
                "to": "{{ candidate.email }}",
                "subject": "Interview invitation — {{ start.role_title }}",
                "html_body": (
                    "<p>Hi {{ candidate.name }},</p>"
                    "<p>Please pick your interview slot and preferred language here: "
                    "<a href=\"{{ sign_slot_link.data.url }}\">choose a slot</a></p>"
                ),
            },
        ),
    ],
)


# ---------------------------------------------------------------------------
# W2 — Interview start (candidate submits slot → webhook 'hr-slot')
# ---------------------------------------------------------------------------
HR_INTERVIEW = WorkflowDefinition(
    name="HR Interview — Start Call",
    description=(
        "Fired when a candidate submits their slot + language. Records the "
        "selection, places the outbound Retell interview call, and registers "
        "the call so its completion routes to scoring. Ends immediately — the "
        "call runs asynchronously."
    ),
    trigger="webhook",
    output_format="json",
    nodes=[
        TriggerNode(
            id="start",
            name="Slot Submitted",
            trigger_type="webhook",
            slug=_SLOT_SLUG,
        ),
        DataStoreNode(
            id="store_slot",
            name="Store Slot Selection",
            depends_on=["start"],
            op="write",
            table="slot_submissions",
            key="{{ start.candidate_id }}",
            payload={
                "candidate_id": "{{ start.candidate_id }}",
                "slot_iso": "{{ start.slot_iso }}",
                "language": "{{ start.language }}",
                "status": "slot_selected",
            },
        ),
        ActionNode(
            id="start_call",
            name="Place Interview Call (Retell)",
            depends_on=["store_slot"],
            provider="mcp",
            action_slug="start_interview",
            on_error="route",
            params={
                "phone": "{{ start.phone }}",
                "language": "{{ start.language }}",
                "jd_summary": "{{ start.role_title }}",
                "rubric": _RUBRIC,
            },
        ),
        ActionNode(
            id="notify_call_failed",
            name="Notify Recruiter: Call Failed",
            depends_on=["start_call"],
            activate_on={"start_call": "failed"},
            provider="gmail",
            action_slug="gmail_send",
            params={
                "to": "recruiting@company.com",
                "subject": "Interview call failed to place",
                "html_body": "<p>Could not start the interview for {{ start.candidate_id }}.</p>",
            },
        ),
        ActionNode(
            id="register_route",
            name="Register Call → Scoring Route",
            depends_on=["start_call"],
            activate_on={"start_call": "ok"},
            provider="internal",
            action_slug="register_voice_route",
            params={
                "call_id": "{{ start_call.data.call_id }}",
                "target_slug": _SCORING_SLUG,
                "context": {
                    "candidate_id": "{{ start.candidate_id }}",
                    "name": "{{ start.name }}",
                    "email": "{{ start.email }}",
                    "role_title": "{{ start.role_title }}",
                },
            },
        ),
    ],
)


# ---------------------------------------------------------------------------
# W3 — Scoring (Retell call-ended callback → webhook 'hr-scoring')
# ---------------------------------------------------------------------------
HR_SCORING = WorkflowDefinition(
    name="HR Interview — Score & Review",
    description=(
        "Fired by the Retell call-ended callback. Fetches the transcript, "
        "scores it against the rubric, stores results, emails the candidate a "
        "summary, and sends the recruiter signed approve/reject links (the "
        "human review gate before any rejection)."
    ),
    trigger="webhook",
    output_format="json",
    nodes=[
        TriggerNode(
            id="start",
            name="Call Ended",
            trigger_type="webhook",
            slug=_SCORING_SLUG,
        ),
        ActionNode(
            id="get_transcript",
            name="Get Interview Transcript",
            depends_on=["start"],
            provider="mcp",
            action_slug="get_interview_transcript",
            params={"call_id": "{{ start.call_id }}"},
        ),
        ActionNode(
            id="score",
            name="Score Interview",
            depends_on=["get_transcript"],
            provider="mcp",
            action_slug="score_interview",
            params={"call_id": "{{ start.call_id }}", "rubric": _RUBRIC},
        ),
        DataStoreNode(
            id="store_results",
            name="Store Interview Results",
            depends_on=["score"],
            op="write",
            table="interview_results",
            key="{{ start.candidate_id }}",
            payload={
                "candidate_id": "{{ start.candidate_id }}",
                "role_title": "{{ start.role_title }}",
                "overall": "{{ score.data.overall }}",
                "scores": "{{ score.data.scores }}",
                "rationale": "{{ score.data.rationale }}",
                "status": "scored",
            },
        ),
        ActionNode(
            id="summary_email",
            name="Email Candidate Summary",
            depends_on=["store_results"],
            provider="gmail",
            action_slug="gmail_send",
            params={
                "to": "{{ start.email }}",
                "subject": "Your interview summary",
                "html_body": (
                    "<p>Thank you for interviewing, {{ start.name }}.</p>"
                    "<p>Overall: {{ score.data.overall }}%</p>"
                    "<p>{{ score.data.rationale }}</p>"
                ),
            },
        ),
        ActionNode(
            id="sign_approve",
            name="Build Approve Link",
            depends_on=["store_results"],
            provider="internal",
            action_slug="sign_link",
            params={
                "path": "/api/v1/workflows/slug/" + _DECISION_SLUG,
                "context": {
                    "candidate_id": "{{ start.candidate_id }}",
                    "name": "{{ start.name }}",
                    "email": "{{ start.email }}",
                    "role_title": "{{ start.role_title }}",
                    "decision": "approve",
                },
            },
        ),
        ActionNode(
            id="sign_reject",
            name="Build Reject Link",
            depends_on=["store_results"],
            provider="internal",
            action_slug="sign_link",
            params={
                "path": "/api/v1/workflows/slug/" + _DECISION_SLUG,
                "context": {
                    "candidate_id": "{{ start.candidate_id }}",
                    "role_title": "{{ start.role_title }}",
                    "decision": "reject",
                },
            },
        ),
        ActionNode(
            id="notify_recruiter",
            name="Recruiter Review (Approve/Reject)",
            depends_on=["sign_approve", "sign_reject"],
            provider="gmail",
            action_slug="gmail_send",
            params={
                "to": "recruiting@company.com",
                "subject": "Review candidate — {{ start.role_title }} ({{ score.data.overall }}%)",
                "html_body": (
                    "<p>{{ start.name }} scored {{ score.data.overall }}%.</p>"
                    "<p>{{ score.data.rationale }}</p>"
                    "<p><a href=\"{{ sign_approve.data.url }}\">Approve → schedule HR round</a> "
                    "&nbsp;|&nbsp; "
                    "<a href=\"{{ sign_reject.data.url }}\">Reject</a></p>"
                ),
            },
        ),
    ],
)


# ---------------------------------------------------------------------------
# W4 — Decision (recruiter clicks approve/reject → webhook 'hr-decision')
# ---------------------------------------------------------------------------
HR_DECISION = WorkflowDefinition(
    name="HR Interview — Decision",
    description=(
        "Fired when the recruiter clicks approve or reject. On approve, checks "
        "HR availability and books the HR round; on reject, records the "
        "outcome. Rejection only happens on this explicit human action."
    ),
    trigger="webhook",
    output_format="json",
    nodes=[
        TriggerNode(
            id="start",
            name="Recruiter Decision",
            trigger_type="webhook",
            slug=_DECISION_SLUG,
        ),
        IfNode(
            id="is_approved",
            name="Approved?",
            depends_on=["start"],
            expression="$.start.decision == 'approve'",
        ),
        ActionNode(
            id="check_hr",
            name="Check HR Availability",
            depends_on=["is_approved"],
            activate_on={"is_approved": "true"},
            provider="pipedream",
            action_slug="pipedream_run_action",
            params={
                "app": "google_calendar",
                "action": "freebusy_query",
                "calendars": ["hr-team@company.com"],
            },
        ),
        ActionNode(
            id="schedule_hr",
            name="Schedule HR Interview",
            depends_on=["check_hr"],
            provider="pipedream",
            action_slug="pipedream_calendly_create_event",
            params={
                "calendar": "hr-team@company.com",
                "attendees": ["{{ start.email }}"],
                "start": "{{ check_hr.data.next_slot }}",
                "duration_minutes": 30,
                "summary": "HR round — {{ start.role_title }}",
            },
        ),
        DataStoreNode(
            id="mark_booked",
            name="Mark HR Round Booked",
            depends_on=["schedule_hr"],
            op="write",
            table="interview_results",
            key="{{ start.candidate_id }}",
            payload={
                "hr_interview_scheduled_at": "{{ schedule_hr.data.start }}",
                "status": "hr_round_booked",
            },
        ),
        DataStoreNode(
            id="mark_rejected",
            name="Mark Not Advanced",
            depends_on=["is_approved"],
            activate_on={"is_approved": "false"},
            op="write",
            table="interview_results",
            key="{{ start.candidate_id }}",
            payload={"status": "not_advanced"},
        ),
    ],
)


# ---------------------------------------------------------------------------
# W-Chaser — remind candidates who never picked a slot (scheduled)
# ---------------------------------------------------------------------------
HR_CHASER = WorkflowDefinition(
    name="HR Chaser — Slot Reminders",
    description=(
        "Daily: find sourced candidates who haven't picked an interview slot "
        "and email them a reminder."
    ),
    trigger="schedule",
    output_format="json",
    nodes=[
        TriggerNode(
            id="start",
            name="Daily Sweep",
            trigger_type="schedule",
            schedule_cron="0 10 * * *",
        ),
        DataStoreNode(
            id="all_candidates",
            name="All Sourced Candidates",
            depends_on=["start"],
            op="query",
            table="candidates",
            filter={},
        ),
        DataStoreNode(
            id="submissions",
            name="Slot Submissions",
            depends_on=["start"],
            op="query",
            table="slot_submissions",
            filter={},
        ),
        AgentNode(
            id="find_pending",
            name="Find Non-Responders",
            role="Recruiting operations analyst.",
            instructions=(
                "From the candidates query and the slot_submissions query, "
                "return the candidates who have NOT submitted a slot. Emit a "
                "JSON array: [{candidate_id, name, email}]."
            ),
            depends_on=["all_candidates", "submissions"],
        ),
        ForEachNode(
            id="per_pending",
            name="For Each Non-Responder",
            depends_on=["find_pending"],
            items_from="find_pending",
            items_path="$",
            item_var="pending",
            body=["send_reminder"],
            max_concurrency=5,
        ),
        ActionNode(
            id="send_reminder",
            name="Send Reminder Email",
            depends_on=["per_pending"],
            provider="gmail",
            action_slug="gmail_send",
            params={
                "to": "{{ pending.email }}",
                "subject": "Reminder: pick your interview slot",
                "html_body": "<p>Hi {{ pending.name }}, a quick reminder to choose your interview slot.</p>",
            },
        ),
    ],
)


# ---------------------------------------------------------------------------
# W-Ranking — stack-rank interviewed candidates (scheduled)
# ---------------------------------------------------------------------------
HR_RANKING = WorkflowDefinition(
    name="HR Ranking — Stack Rank",
    description="Weekly: stack-rank interviewed candidates by overall score and store the leaderboard.",
    trigger="schedule",
    output_format="json",
    nodes=[
        TriggerNode(
            id="start",
            name="Weekly Ranking",
            trigger_type="schedule",
            schedule_cron="0 18 * * 5",
        ),
        DataStoreNode(
            id="results",
            name="All Interview Results",
            depends_on=["start"],
            op="query",
            table="interview_results",
            filter={},
        ),
        AgentNode(
            id="rank",
            name="Stack Rank",
            role="Recruiting analyst.",
            instructions=(
                "Take the interview_results rows. Sort by overall score "
                "descending. Emit JSON: [{candidate_id, overall, rank}]."
            ),
            depends_on=["results"],
        ),
        DataStoreNode(
            id="store_ranking",
            name="Store Ranking",
            depends_on=["rank"],
            op="write",
            table="candidate_ranking",
            key="latest",
            payload={"ranking": "{{ rank }}"},
        ),
    ],
)
