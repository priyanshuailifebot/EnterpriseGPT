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

# Résumé-screening shortlist threshold (0-100). Candidates the LLM scores at or
# above this bar are invited to book a slot; the rest are filtered out before
# any invite is sent.
_SCREEN_THRESHOLD = 70

# Explicit, weighted criteria the screening LLM applies to each résumé against
# the job description. Kept as prose in the agent instructions so it is
# auditable and easy for a recruiter to tune per role.
_SCREEN_CRITERIA = (
    "Screening criteria (weight each, then compute an overall fit_score 0-100):\n"
    "1. Relevant sales experience (~30%): direct field / B2B / B2C / channel "
    "sales; more relevant years and clear selling roles score higher. Purely "
    "non-sales backgrounds score low.\n"
    "2. Quota / target attainment (~25%): concrete evidence of meeting or "
    "beating sales targets, revenue, or growth numbers.\n"
    "3. Local market & language fit (~15%): knowledge of the territory named in "
    "the JD and ability to work in the relevant language(s).\n"
    "4. Industry / domain relevance (~15%): experience in the same or an "
    "adjacent industry to the role.\n"
    "5. Customer-facing & communication signals (~10%): consultative selling, "
    "objection handling, relationship management.\n"
    "6. Stability & progression (~5%): sensible tenure and career growth; "
    "excessive unexplained job-hopping is a mild negative."
)

# Sibling trigger slugs — how the workflows address each other.
_SLOT_SLUG = "hr-slot"
_SCORING_SLUG = "hr-scoring"
_DECISION_SLUG = "hr-decision"
_FEEDBACK_SLUG = "hr-scoring"  # human-round feedback re-enters the SAME scoring gate

# ---------------------------------------------------------------------------
# Interview-ladder "brains" — two LLM steps that make the pipeline adapt to the
# role instead of hardcoding rounds/questions:
#   * _LADDER_*   designs the sequence of rounds from the JD.
#   * _QUESTION_* generates one round's questions + rubric from JD + round type.
# Both were validated live against Software-Engineer and Field-Sales JDs.
# ---------------------------------------------------------------------------
_LADDER_ROLE = "You are an experienced technical recruiter and interview designer."
_LADDER_INSTR = (
    "Given a job description, design the interview ROUND LADDER for this role. "
    "Choose the RIGHT sequence of rounds for THIS specific role — do not use a "
    "fixed template. General principles: role-specific / technical screens come "
    "before behavioral / HR rounds; a final hiring-manager or offer round comes "
    "last. Use 2-4 rounds. `type` MUST be exactly one of: technical, "
    "role_specific, behavioral, hr, managerial, offer. `mode` is the judging "
    "mode and MUST be exactly 'ai' or 'human' — default EVERY round to 'ai' "
    "unless it inherently needs a person (a final hiring-manager or offer round "
    "→ 'human'). Return STRICT JSON and nothing else: "
    '{"rounds":[{"name": str, "type": str, "mode": str, "focus": str}]} '
    "where focus is one sentence on what the round assesses."
)
_QUESTION_ROLE = "You are an expert interviewer preparing for a specific interview round."
_QUESTION_INSTR = (
    "Generate the QUESTION PLAN for ONE interview round. The input gives the job "
    "description and the round (name, type, focus). Produce 5-7 questions tailored "
    "to THIS job and THIS round type: for technical/role_specific rounds ask "
    "concrete job-specific questions drawn from the JD's actual skills and "
    "responsibilities; for behavioral/hr ask behavioral, motivation and teamwork "
    "questions; for managerial/offer ask seniority-calibration and expectations "
    "questions. Also give a concise scoring rubric of 3-5 criteria for this round. "
    'Return STRICT JSON and nothing else: {"questions": [str,...], "rubric": [str,...]}.'
)
_SELECT_ROLE = "You select one interview round from a ladder by index."
_SELECT_INSTR = (
    "The input has a JSON `ladder` (array of round objects) and an integer "
    "`round_index` (0-based). Return STRICT JSON and nothing else: the single "
    "round object at that index, with keys name, type, mode, focus. If the index "
    "is out of range, return {}."
)
_ADVANCE_ROLE = "You advance a candidate through an interview ladder."
_ADVANCE_INSTR = (
    "The input has a JSON `ladder` (array of rounds) and an integer "
    "`round_index` for the round the candidate JUST completed. Compute the next "
    "step. Return STRICT JSON and nothing else: "
    '{"next_index": int, "has_next": bool, "next_name": str} where '
    "next_index = round_index + 1, has_next = (next_index < number_of_rounds), "
    "and next_name is the name of the round at next_index (or empty string if "
    "none)."
)


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
                "to": "recruiting@example.com",
                "subject": "Sourcing failed — {{ start.role_title }}",
                "html_body": "<p>The ATS candidate search failed for {{ start.role_title }}.</p>",
            },
        ),
        # Résumé screening: an LLM reads every fetched candidate's résumé,
        # scores it against the JD on explicit criteria, and returns ONLY the
        # candidates who clear the bar — so invites go to the shortlist, not the
        # whole ATS dump. Tool-less agent → returns its completion (the JSON
        # array); the for_each loose-JSON parser consumes it directly.
        AgentNode(
            id="screen",
            name="Screen Résumés → Shortlist",
            depends_on=["fetch", "start"],
            activate_on={"fetch": "ok"},
            role="You are an expert technical recruiter screening résumés for a role.",
            instructions=(
                "You are given a job description and a JSON array of candidates "
                "(each with fields such as candidate_id, name, email, phone, and "
                "a résumé / experience summary). Evaluate EACH candidate's résumé "
                "against the job description using the criteria below.\n\n"
                + _SCREEN_CRITERIA
                + "\n\nShortlist a candidate only if their overall fit_score is "
                + str(_SCREEN_THRESHOLD)
                + " or higher. Return a JSON array containing ONLY the "
                "shortlisted candidates. For each, COPY every original field "
                "verbatim (candidate_id, name, email, phone, role) and ADD two "
                "fields: `fit_score` (integer 0-100) and `fit_reason` (one short "
                "sentence). Do not alter emails, ids, or names in any way. Output "
                "ONLY the JSON array — no prose, no code fences, no commentary. "
                "If nobody qualifies, output []."
            ),
        ),
        # Design the interview ROUND LADDER for this role from the JD (LLM).
        # Generated once per sourcing run and stored per-role; every candidate
        # of this role advances through the same ladder (progress tracked per
        # candidate via round_index in the signed ctx).
        AgentNode(
            id="plan",
            name="Design Interview Ladder",
            depends_on=["fetch", "start"],
            activate_on={"fetch": "ok"},
            role=_LADDER_ROLE,
            instructions=_LADDER_INSTR,
        ),
        DataStoreNode(
            id="store_plan",
            name="Store Interview Ladder",
            depends_on=["plan"],
            op="write",
            table="interview_plans",
            key="{{ start.role_title }}",
            payload={
                "role_title": "{{ start.role_title }}",
                "jd_text": "{{ start.jd_text }}",
                "ladder": "{{ plan }}",
            },
        ),
        DataStoreNode(
            id="store_candidates",
            name="Store Shortlist",
            depends_on=["screen"],
            op="write",
            table="candidates",
            key="{{ start.role_title }}",
            payload={
                "role": "{{ start.role_title }}",
                "sourced": "{{ fetch.data }}",
                "shortlist": "{{ screen }}",
                "status": "screened",
            },
        ),
        ForEachNode(
            id="per_candidate",
            name="For Each Shortlisted Candidate",
            depends_on=["store_candidates", "store_plan"],
            items_from="screen",
            items_path="$",
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
                    "round_index": 0,
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
    name="HR Interview — Start Round",
    description=(
        "Fired when a candidate submits their slot for the CURRENT round. Reads "
        "the role's interview ladder, generates that round's questions + rubric "
        "from the JD, then either places the AI voice interview (mode=ai) or "
        "books a human interviewer with a question brief + feedback link "
        "(mode=human). Round-aware: driven by round_index carried in the ctx."
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
                "round_index": "{{ start.round_index }}",
                "status": "slot_selected",
            },
        ),
        # Load the role's ladder (+ JD) so this round knows its type and can
        # tailor questions. Written by HR Sourcing's store_plan.
        DataStoreNode(
            id="read_plan",
            name="Load Interview Ladder",
            depends_on=["start"],
            op="read",
            table="interview_plans",
            key="{{ start.role_title }}",
        ),
        # Deterministically select ladder[round_index] (pure function, no LLM)
        # so the round's mode/name are always reliable structured values.
        ActionNode(
            id="pick_round",
            name="Select Current Round",
            depends_on=["read_plan", "start"],
            provider="internal",
            action_slug="hr_pick_round",
            params={
                "ladder": "{{ read_plan.row.ladder }}",
                "round_index": "{{ start.round_index }}",
            },
        ),
        # LLM generates this round's questions from the JD + round type/focus.
        # Output is prose (interviewer-facing) — consumed as free text, so it
        # never depends on strict JSON.
        AgentNode(
            id="gen_q",
            name="Generate Round Questions",
            depends_on=["pick_round", "read_plan"],
            role=_QUESTION_ROLE,
            instructions=(
                "The input contains the job description (`jd_text`) and the "
                "current interview round (name, type, focus). "
                + _QUESTION_INSTR
                + " Present the questions as a clean numbered list followed by a "
                "short 'Scoring rubric:' list — readable prose an interviewer can "
                "use directly."
            ),
        ),
        # mode gate: AI voice round vs human interviewer round.
        IfNode(
            id="is_ai",
            name="AI round?",
            depends_on=["pick_round"],
            expression="$.pick_round.data.mode == 'ai'",
        ),
        # ---- AI branch: place the voice interview with THIS round's questions ----
        ActionNode(
            id="start_call",
            name="Place Interview Call (Retell)",
            depends_on=["is_ai", "gen_q"],
            activate_on={"is_ai": "true"},
            provider="mcp",
            action_slug="start_interview",
            on_error="route",
            params={
                "phone": "{{ start.phone }}",
                "language": "{{ start.language }}",
                "jd_summary": "{{ read_plan.row.jd_text }}",
                "round": "{{ pick_round.data.name }}",
                "questions": "{{ gen_q }}",
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
                "to": "recruiting@example.com",
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
                    "phone": "{{ start.phone }}",
                    "role_title": "{{ start.role_title }}",
                    "round_index": "{{ start.round_index }}",
                    "round_name": "{{ pick_round.data.name }}",
                    "round_focus": "{{ pick_round.data.focus }}",
                    "mode": "ai",
                },
            },
        ),
        # ---- Human branch: book the interviewer + send a brief + feedback link ----
        ActionNode(
            id="book_human",
            name="Book Human Interviewer",
            depends_on=["is_ai"],
            activate_on={"is_ai": "false"},
            provider="pipedream",
            action_slug="pipedream_calendly_create_event",
            on_error="continue",
            params={
                "calendar": "hiring-team@example.com",
                "attendees": ["{{ start.email }}"],
                "start": "{{ start.slot_iso }}",
                "duration_minutes": 45,
                "summary": "{{ pick_round.data.name }} — {{ start.role_title }} ({{ start.name }})",
            },
        ),
        ActionNode(
            id="sign_feedback",
            name="Build Feedback-Form Link",
            depends_on=["is_ai"],
            activate_on={"is_ai": "false"},
            provider="internal",
            action_slug="sign_link",
            params={
                "base": "web",
                "path": "/hr/feedback",
                "context": {
                    "candidate_id": "{{ start.candidate_id }}",
                    "name": "{{ start.name }}",
                    "email": "{{ start.email }}",
                    "phone": "{{ start.phone }}",
                    "role_title": "{{ start.role_title }}",
                    "round_index": "{{ start.round_index }}",
                    "round_name": "{{ pick_round.data.name }}",
                    "round_focus": "{{ pick_round.data.focus }}",
                    "purpose": "feedback",
                },
            },
        ),
        ActionNode(
            id="email_brief",
            name="Email Interviewer Brief",
            # Gated to the human branch via sign_feedback (is_ai=false); NOT
            # dependent on book_human succeeding — a missing calendar connection
            # must not suppress the interviewer's brief + feedback link.
            depends_on=["sign_feedback", "gen_q"],
            activate_on={"is_ai": "false"},
            provider="gmail",
            action_slug="gmail_send",
            params={
                "to": "hiring-team@example.com",
                "subject": "Interview brief: {{ start.name }} — {{ pick_round.data.name }}",
                "html_body": (
                    '<div style="font-family:Arial,sans-serif;font-size:14px;'
                    'color:#1a2233;line-height:1.6"><p>You have an upcoming '
                    "<b>{{ pick_round.data.name }}</b> interview with "
                    "<b>{{ start.name }}</b> for <b>{{ start.role_title }}</b>.</p>"
                    "<p><b>Focus:</b> {{ pick_round.data.focus }}</p>"
                    "<p><b>Suggested questions:</b></p>"
                    "<p>{{ gen_q }}</p>"
                    "<p style=\"margin-top:16px\">After the interview, submit your "
                    'assessment here: <a href="{{ sign_feedback.data.url }}" '
                    'style="background:#2563EB;color:#fff;padding:9px 16px;'
                    'border-radius:6px;text-decoration:none">Submit feedback</a></p>'
                    "</div>"
                ),
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
        # An LLM agent scores the interview transcript (which arrives on the
        # trigger ctx from the Retell call-ended callback) and drafts a
        # professional recruiter assessment in prose. Replaces the old voice-MCP
        # scorer so it works with any transcript source and yields a real,
        # presentable assessment instead of an unrendered score field.
        AgentNode(
            id="score",
            name="Score & Draft",
            depends_on=["start"],
            role="You are an expert hiring assessor.",
            instructions=(
                "Your input is a JSON object describing ONE completed interview "
                "round. It contains either an AI-interview `transcript` OR a human "
                "interviewer's `feedback`/`notes` (and possibly a numeric "
                "`rating`), plus candidate details (name, role_title) and the round "
                "(round_name, round_focus). Write a concise, professional hiring "
                "assessment of 4-6 sentences for the recruiter, judged against "
                "THIS round's focus: the candidate's performance in this round, "
                "key strengths, any gaps, an explicit overall rating out of 100, "
                "and a clear recommendation (advance / hold / decline). Base "
                "everything strictly on the transcript or feedback provided — do "
                "not invent facts. Write clean prose; you may use simple HTML tags "
                "like <p> and <b>."
            ),
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
                "round_index": "{{ start.round_index }}",
                "round_name": "{{ start.round_name }}",
                "assessment": "{{ score }}",
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
                "subject": "Thank you for interviewing — {{ start.role_title }}",
                "html_body": (
                    '<div style="font-family:Arial,sans-serif;font-size:14px;'
                    'color:#1a2233;line-height:1.6"><p>Hi {{ start.name }},</p>'
                    "<p>Thank you for taking the time to interview for the "
                    "<b>{{ start.role_title }}</b> role. We really enjoyed the "
                    "conversation, and our team is reviewing it now — we'll follow "
                    "up with next steps shortly.</p><p>Warm regards,<br>"
                    "The Talent Team</p></div>"
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
                    "phone": "{{ start.phone }}",
                    "role_title": "{{ start.role_title }}",
                    "round_index": "{{ start.round_index }}",
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
                    "name": "{{ start.name }}",
                    "email": "{{ start.email }}",
                    "role_title": "{{ start.role_title }}",
                    "round_index": "{{ start.round_index }}",
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
                "to": "recruiting@example.com",
                "subject": "Review: {{ start.name }} — {{ start.round_name }} ({{ start.role_title }})",
                "html_body": (
                    '<div style="font-family:Arial,sans-serif;font-size:14px;'
                    'color:#1a2233;line-height:1.6"><p><b>Candidate:</b> '
                    "{{ start.name }} &nbsp;·&nbsp; <b>Role:</b> {{ start.role_title }}"
                    " &nbsp;·&nbsp; <b>Round:</b> {{ start.round_name }}</p>"
                    '<div style="background:#f5f7f3;border-left:3px solid #157f57;'
                    'border-radius:6px;padding:12px 14px">{{ score }}</div>'
                    '<p style="margin-top:18px"><a href="{{ sign_approve.data.url }}" '
                    'style="background:#157f57;color:#fff;padding:9px 16px;'
                    'border-radius:6px;text-decoration:none">Approve → next round / '
                    'offer</a> &nbsp; <a href="{{ sign_reject.data.url }}" '
                    'style="color:#b45309">Reject</a></p></div>'
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
        "Fired when the recruiter clicks approve or reject. On approve, advances "
        "the candidate through the interview LADDER: if another round remains it "
        "invites them to book the next round (looping back to Start Round); if "
        "the ladder is exhausted it extends an offer. On reject, records the "
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
        # --- Approve: figure out the next rung of the ladder ---
        DataStoreNode(
            id="read_plan",
            name="Load Interview Ladder",
            depends_on=["is_approved"],
            activate_on={"is_approved": "true"},
            op="read",
            table="interview_plans",
            key="{{ start.role_title }}",
        ),
        ActionNode(
            id="advance",
            name="Advance Ladder",
            depends_on=["read_plan", "start"],
            activate_on={"is_approved": "true"},
            provider="internal",
            action_slug="hr_advance",
            params={
                "ladder": "{{ read_plan.row.ladder }}",
                "round_index": "{{ start.round_index }}",
            },
        ),
        IfNode(
            id="has_next",
            name="Another round?",
            depends_on=["advance"],
            expression="$.advance.data.has_next == true",
        ),
        # --- Next round exists: invite the candidate to book it (loop) ---
        ActionNode(
            id="sign_next",
            name="Build Next-Round Slot Link",
            depends_on=["has_next"],
            activate_on={"has_next": "true"},
            provider="internal",
            action_slug="sign_link",
            params={
                "base": "web",
                "path": "/hr/slot",
                "context": {
                    "candidate_id": "{{ start.candidate_id }}",
                    "name": "{{ start.name }}",
                    "email": "{{ start.email }}",
                    "phone": "{{ start.phone }}",
                    "role_title": "{{ start.role_title }}",
                    "round_index": "{{ advance.data.next_index }}",
                    "purpose": "slot",
                },
            },
        ),
        ActionNode(
            id="invite_next",
            name="Invite to Next Round",
            depends_on=["sign_next"],
            provider="gmail",
            action_slug="gmail_send",
            params={
                "to": "{{ start.email }}",
                "subject": "You're moving forward — {{ advance.data.next_name }} ({{ start.role_title }})",
                "html_body": (
                    '<div style="font-family:Arial,sans-serif;font-size:14px;'
                    'color:#1a2233;line-height:1.6"><p>Hi {{ start.name }},</p>'
                    "<p>Great news — you've advanced to the next round: "
                    "<b>{{ advance.data.next_name }}</b>. Please pick a time here: "
                    '<a href="{{ sign_next.data.url }}">choose a slot</a></p>'
                    "<p>Warm regards,<br>The Talent Team</p></div>"
                ),
            },
        ),
        DataStoreNode(
            id="mark_advanced",
            name="Mark Advanced",
            depends_on=["invite_next"],
            op="write",
            table="interview_results",
            key="{{ start.candidate_id }}",
            payload={
                "status": "advanced",
                "current_round_index": "{{ advance.data.next_index }}",
                "current_round_name": "{{ advance.data.next_name }}",
            },
        ),
        # --- Ladder exhausted: extend an offer ---
        ActionNode(
            id="offer_email",
            name="Email Offer",
            depends_on=["has_next"],
            activate_on={"has_next": "false"},
            provider="gmail",
            action_slug="gmail_send",
            params={
                "to": "{{ start.email }}",
                "subject": "Great news about your application — {{ start.role_title }}",
                "html_body": (
                    '<div style="font-family:Arial,sans-serif;font-size:14px;'
                    'color:#1a2233;line-height:1.6"><p>Hi {{ start.name }},</p>'
                    "<p>Congratulations — you've successfully completed all "
                    "interview rounds for the <b>{{ start.role_title }}</b> role, "
                    "and we'd love to move forward with an offer. Our team will be "
                    "in touch shortly with the details.</p>"
                    "<p>Warm regards,<br>The Talent Team</p></div>"
                ),
            },
        ),
        DataStoreNode(
            id="mark_offer",
            name="Mark Offer Extended",
            depends_on=["offer_email"],
            op="write",
            table="interview_results",
            key="{{ start.candidate_id }}",
            payload={"status": "offer_extended"},
        ),
        # --- Reject ---
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
