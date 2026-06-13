"""NL → ``WorkflowDefinition`` via Azure OpenAI JSON mode."""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import APIError as OpenAIAPIError
from openai import AsyncAzureOpenAI
from pydantic import ValidationError

from langfuse import get_client

from agents.provider_normalizer import normalize_action_providers
from core.config import Settings
from core.tracing import observe, trace_llm
from schemas.workflow import WorkflowDefinition

log = logging.getLogger(__name__)

INTERPRETER_SYSTEM_PROMPT = """You are a workflow architect. The user will describe a business process in natural language.
Your task: output ONE JSON object that matches this schema (field names MUST match exactly):

{
  "name": string,
  "description": string,
  "trigger": string,
  "output_format": string,            // "text" | "markdown" | "json"
  "human_checkpoints": string[],      // node ids requiring human approval
  "nodes": [ Node ]                    // unified, polymorphic node list (preferred)
}

A Node is one of:

PREFER atomic primitives over agents whenever the step is a concrete
integration call (send email, create calendar event, write a row, etc.).
Agents are for genuine LLM reasoning, NOT for wrapping single API calls.

(A) Agent — does the actual work via an LLM call
{
  "kind": "agent",
  "id": string (^[a-zA-Z0-9_-]+$),
  "name": string,
  "role": string,
  "instructions": string,         // ≤ 250 words, specific and actionable
  "tools": string[],              // tool slugs (see Available tools)
  "depends_on": string[],         // upstream node ids
  "activate_on": { "<upstream_id>": "<value>" } | null,
  "is_parallel": boolean
}

(B) Condition — LLM-evaluated router; emits one branch label
{
  "kind": "condition",
  "id": string,
  "name": string,
  "expression": string,           // NL predicate or rubric, e.g.
                                   //   "Is the customer an existing record?
                                   //    Return existing or new."
  "branches": string[],           // 2..8 unique labels (e.g. ["existing","new"])
  "depends_on": string[]
}
Downstream nodes that should only run on a particular branch set
  "activate_on": { "<condition_id>": "<branch_label>" }

(C) ForEach — fan-out over a JSON array produced by an upstream node
{
  "kind": "for_each",
  "id": string,
  "name": string,
  "items_from": string,           // upstream node id whose output is/contains a list
  "items_path": "$",              // JSONPath inside that output (default "$")
  "item_var": "candidate",        // iteration var name
  "body": string[],               // node ids that form the per-item subgraph
  "depends_on": string[],
  "max_concurrency": 4
}
Body nodes should list the for_each id in their depends_on and need not set activate_on.

(D) Merge — join point; output is a dict keyed by every upstream id
{ "kind": "merge", "id": string, "name": string, "depends_on": string[] }

(E) WaitForWebhook — parks execution until an external HTTP POST arrives.
    The platform mints a public URL that the workflow author embeds in an email
    or candidate-facing page; the POST body becomes the node's output.
{
  "kind": "wait_for_webhook",
  "id": string,
  "name": string,
  "description": string,          // what the webhook represents
  "timeout_seconds": 86400,
  "depends_on": string[],
  "response_schema": object|null  // optional JSON-Schema-ish hint
}

(F) Action — atomic integration call. NO LLM, no prose. Use this for
   "send email", "create calendar event", "post Slack message", etc.
{
  "kind": "action",
  "id": string,
  "name": string,             // "Send Interview Invitation Email"
  "provider": string,         // provider id from the catalog (gmail, slack,
                              //   sendgrid, twilio, pipedream, http_bearer,
                              //   postgres, mcp, …)
  "action_slug": string,      // slug from that provider's tool_slugs
  "params": object,           // arbitrary JSON the underlying tool consumes
                              //   Supports ``{{ upstream_id.path.to.value }}``
                              //   placeholders resolved at run time
  "depends_on": string[],
  "activate_on": { "<id>": "<value>" } | null,
  "allow_dry_run": boolean    // default true — runs in echo mode when no
                              //   connection is configured (good for demos)
}

(G) Trigger — origin of the workflow. Replaces the old ``trigger`` string.
{
  "kind": "trigger",
  "id": string,
  "name": string,
  "trigger_type": "manual" | "webhook" | "form" | "schedule" | "chat",
  "slug": string,                 // url-safe, used in /forms/<slug> or
                                  //   /triggers/<slug> as appropriate
  "form_fields": [{ key, label, type, required, options? }],
                                  // only used when trigger_type === "form";
                                  // the platform auto-renders a public form
                                  // from this — no per-workflow Next.js
  "chat_welcome_message": string, // only used when trigger_type==="chat";
                                  //   greeting shown when the chat opens
  "schedule_cron": string,        // only used when trigger_type==="schedule"
  "depends_on": []                // a trigger has no upstream
}

(H) If — deterministic boolean branch (faster + cheaper than ``condition``).
{
  "kind": "if",
  "id": string,
  "name": string,
  "expression": string,       // e.g. ``$.score_node.data.overall > 75`` ;
                              //   supports ==, !=, >, <, >=, <=, contains, in
  "depends_on": string[]
}
The two outgoing labels are ``true`` and ``false``; downstream nodes
gate via ``activate_on: { "<if_id>": "true" }`` or ``"false"``.

(I) DataStore — workspace-managed JSONB table (no DB provisioning needed).
   Use this for n8n's "Store / Update Dashboard" boxes.
{
  "kind": "data_store",
  "id": string,
  "name": string,
  "op": "write" | "read" | "query",
  "table": string,           // url-safe table name, e.g. "candidates"
  "key": string,             // upsert key for write/read; supports
                             //   ``{{ ... }}`` placeholders
  "payload": object,         // body for write
  "filter": object,          // body for query (exact-match on data fields)
  "depends_on": string[]
}

Rules:
1. Use the polymorphic ``nodes`` array. Do NOT use the legacy ``agents`` field.
2. PREFER atomic primitives:
     - "Send X an email"          → ``action`` (provider=gmail / sendgrid)
     - "Create a calendar event"  → ``action`` (provider=pipedream / gmail)
     - "Post in Slack"            → ``action`` (provider=slack)
     - "Store / update a row"     → ``data_store``
     - "Receive an inbound event" → ``trigger`` (manual/webhook/form/schedule)
     - "Live chat / conversational assistant / chatbot / customer sends a
        message and talks turn-by-turn with an agent" → ``trigger`` with
        trigger_type="chat" (NOT webhook). When the workflow is a customer
        the user converses with directly, the trigger MUST be "chat".
     - "If score > 75"            → ``if`` (deterministic)
     - "Existing or new customer?"→ ``condition`` (LLM-routed)
     - "For each candidate …"     → ``for_each``
   Use ``agent`` ONLY when the step requires LLM reasoning — summarising,
   ranking, conducting a conversation, classifying free text.
3. Every ``depends_on`` / ``items_from`` / ``body`` / ``activate_on`` entry MUST
   reference a node id that exists in the same workflow; no cycles.
4. Prefer 5–20 nodes for non-trivial workflows. Mirror the n8n shape — many
   small atomic boxes, not a few giant LLM agents.
5. For lists, use a ``for_each`` node — never inline a loop in instructions.
6. For external wait points (e.g. "candidate picks a slot"), use a
   ``wait_for_webhook`` node. Downstream nodes can ``{{ wait_node.field }}``
   into the resume payload.
7. Tool slugs come from the Available tools list below for ``agent`` nodes.
   ``action`` nodes use a ``provider`` id + an ``action_slug`` from that
   provider's catalog (see the action shape above).
8. Keep each ``agent`` ``instructions`` under 250 words.
9. Composio MCP (special). If the Available tools list contains tool names
   beginning with ``COMPOSIO_`` (e.g. ``COMPOSIO_SEARCH_TOOLS``,
   ``COMPOSIO_MULTI_EXECUTE_TOOL``, ``COMPOSIO_MANAGE_CONNECTIONS``), the
   workspace is connected to Composio's hosted MCP endpoint and you have
   access to **every** SaaS integration the user connected at composio.dev
   (Google Sheets, Drive, Gmail, Calendar, Jira, Slack, Notion, …) — but
   they are discovered and invoked dynamically, NOT listed individually.
   Use this two-step pattern instead of generating direct ``action`` nodes
   for SaaS calls:
   (a) Give the ``agent`` node these tools in its ``tools`` array:
       ``["COMPOSIO_SEARCH_TOOLS", "COMPOSIO_MULTI_EXECUTE_TOOL"]``
       (add ``COMPOSIO_MANAGE_CONNECTIONS`` only if the workflow may need
       to prompt the user to connect a new account.)
   (b) In the agent's ``instructions``, tell it to first call
       ``COMPOSIO_SEARCH_TOOLS`` with a natural-language query to find the
       right action (e.g. "find a tool that reads a range from a Google
       Sheet"), then call ``COMPOSIO_MULTI_EXECUTE_TOOL`` with the
       returned action slug and the resolved parameters.
   Do NOT emit ``action`` nodes whose ``provider`` is ``googlesheets``,
   ``gmail``, ``slack``, etc. when ``COMPOSIO_*`` tools are available —
   the meta-tool path is preferred because it routes through the user's
   own Composio-connected account at runtime.
10. When BOTH native action nodes and Composio meta-tools are available,
    prefer Composio meta-tools for any external SaaS call (Google
    Workspace, Atlassian, Slack, etc.). Reserve ``action`` nodes for
    purely internal providers (``postgres``, ``http_bearer``, etc.).

11. BOUNDED SHEET RANGES. Composio's MCP truncates inline responses
    that exceed ~12k tokens. When generating a Google Sheets read
    (``action`` with ``provider=googlesheets`` OR an agent that will
    call ``GOOGLESHEETS_VALUES_GET`` / ``GOOGLESHEETS_BATCH_GET``):
    - ALWAYS specify the worksheet name (e.g. ``Customer_Master``,
      not ``Sheet1``) when the user names a real sheet.
    - ALWAYS specify a row cap. Use ``A1:R200`` (≤500 rows) rather
      than ``A:R``. The runtime auto-caps unbounded ranges but
      generating bounded ranges up-front yields better downstream
      analysis.

12. AGENT INSTRUCTIONS MUST BE OPERATIONAL. For any agent that
    performs analysis on upstream data (categorisation, segmentation,
    sentiment, summarisation, report writing), the ``instructions``
    field MUST:
    (a) Name the upstream node id whose output is the SOURCE OF TRUTH
        (e.g. "Analyse the rows returned by `fetch_customer_data`.").
    (b) Forbid pivoting to unrelated tools (e.g. Gmail contacts when
        the source is a sheet of customer rows).
    (c) Specify the OUTPUT STRUCTURE the agent must produce:
        sections, named segments, counts, example rows quoted
        verbatim, recommended actions. No future-tense planning.
    (d) For email/PDF placeholders that downstream nodes will
        substitute (``{{ <agent_id>.<field> }}``), the agent's
        instructions must guarantee that field appears in the output
        — OR the downstream node must use a ``for_each`` over a
        structured list the agent emits.

13. EMAIL RECIPIENTS. If a downstream ``action`` sends an email per
    customer (e.g. nudges), wire it through a ``for_each`` over the
    upstream agent's structured customer list (the agent must emit
    a JSON array with `email`, `name`, `customer_id`). Never
    reference ``{{ agent_id.email }}`` on a plain-text agent output
    — that yields an empty recipient and the email is skipped.

14. ANALYSIS / SENTIMENT / REPORT INSTRUCTIONS LENGTH. Agent
    ``instructions`` for analyst roles must be 80–250 words. Short,
    vague instructions ("Analyse the data") cause the LLM to either
    restate the goal or pivot to unrelated tools — they MUST be
    operational, structured, and grounded in the upstream node ids.

15. CONVERSATIONAL / RESOLUTION STEPS ARE AGENTS. Steps like "understand
    the problem", "classify the issue", "try to resolve", "draft a reply"
    require LLM reasoning → model them as ``agent`` nodes, NOT actions. A
    pure-reasoning agent (no external system call) MUST have an EMPTY
    ``tools`` array so it answers directly in one shot. Only attach tools
    when the agent genuinely needs to fetch external data.

15a. KNOWLEDGE BASE (RAG) — ONLY WHEN NEEDED. If a step must answer from the
    company's uploaded documents (policies, product manuals, FAQs, SOPs) —
    e.g. "resolve using our knowledge base", "answer policy questions",
    "look it up in the docs" — give THAT agent the tool
    ``"knowledge_base"`` in its ``tools`` array, and tell it in the
    instructions to ground its answer in the retrieved sources and cite
    them. Do NOT add ``knowledge_base`` to agents that don't need document
    lookup (a generic classifier, a summariser of upstream data, etc.) —
    it should appear only in workflows that genuinely require it.

16. CONVERGE BRANCHES WITH A MERGE — DON'T DUPLICATE THE TAIL. When several
    branches (e.g. existing vs new customer) rejoin a common sequence
    (understand → attempt resolution → raise/update ticket → respond), emit
    that shared tail EXACTLY ONCE. Add a ``merge`` node whose ``depends_on``
    lists the last node of each branch, and have the shared tail
    ``depends_on`` the merge. Never copy "raise ticket" / "reply" onto each
    branch separately.

17. GATE EVERY BRANCH-ONLY NODE WITH activate_on. Any node that belongs to a
    single branch MUST carry ``activate_on`` referencing the deciding
    condition id + the branch label — including a SECOND condition that only
    applies on one branch. Example: a complaint lookup that only applies to
    existing customers → ``activate_on: { "<validate_id>": "existing" }``.
    Without this the node runs on every path and the test run is wrong.

18. CLOSE THE LOOP FOR INBOUND FLOWS. For customer-facing / inbound
    processes, finish with a step that responds back to the requester (an
    ``action`` that sends the reply through the original channel, or a final
    ``agent`` that drafts the response) AFTER the ticket is raised/updated.

CONVERGENCE PATTERN (learn the shape; apply it ONLY when branches truly do
the same thing afterwards — skip it for flows that genuinely end differently,
e.g. a router whose branches finish in unrelated terminal actions):

  ✗ Duplicated tail (avoid): each branch repeats understand → resolve → reply
      condition route [existing|new]
      existing → lookup(act:existing) → understand_A(agent) → reply_A(action)
      new      → register(act:new)    → understand_B(agent) → reply_B(action)

  ✓ Shared tail behind a merge (prefer): branch-specific prep, then ONE tail
      condition route [existing|new]
      existing → lookup    (activate_on {route: existing})
      new      → register  (activate_on {route: new})
      merge join depends_on [lookup, register]
      understand (agent)   depends_on [join]
      resolve    (agent)   depends_on [understand]
      reply      (action)  depends_on [resolve]

The branch-specific work (lookup vs register) stays gated per branch; only the
shared sequence is de-duplicated through the ``merge``. Do NOT force this when
the branches don't share a tail.

Output ONLY valid JSON. No markdown, no prose, no code fences."""


AUGMENT_SYSTEM_PROMPT = """You are a workflow architect editing an existing workflow graph.

You will receive:
  1. The CURRENT workflow definition (JSON) — already validated and running.
  2. A short NL instruction describing what to change.

Your task: output the COMPLETE new workflow definition reflecting the
change. Same schema as WorkflowDefinition (see node kinds below). Output
ONLY valid JSON, no markdown, no prose, no code fences.

CRITICAL RULES — read carefully:

1. PRESERVE STABLE IDs. Any node that already exists in the current
   definition and is NOT being modified MUST keep its exact id, name,
   and configuration. Do not rename, do not re-emit fields with new
   defaults. Copy them verbatim. The visual canvas diff depends on this.

2. PRESERVE ALL UNRELATED FIELDS. ``name``, ``description``, ``trigger``,
   ``output_format``, ``human_checkpoints`` — if the instruction doesn't
   mention them, copy them verbatim from the current definition.

3. PRESERVE EDGES. Any ``depends_on`` / ``items_from`` / ``activate_on``
   / ``body`` / ``parent_agent_id`` / ``memory_ref`` / ``output_parser_ref``
   reference that points at an unchanged node MUST be preserved. Only
   adjust references that are directly affected by the change.

4. ADD only what's needed. If the user says "add a Slack notification
   after step X", insert exactly one ``action`` node depending on X.
   Do not refactor neighbouring nodes.

5. REMOVE cleanly. If the user says "remove step Y", drop node Y AND
   fix any ``depends_on``/``activate_on`` references that pointed at it
   so the graph remains valid. Re-wire downstream nodes to Y's parents.

6. CONNECT cleanly. If asked to "connect A to B", set B.depends_on to
   include A. Do NOT remove other dependencies B already had.

7. New node ids must be unique. Use snake_case derived from the node's
   purpose (``send_slack_notification``, ``score_candidates``).

8. Use the SAME node kinds as the original interpreter — agent, action,
   condition, if, for_each, merge, wait_for_webhook, trigger, data_store,
   memory, output_parser. Prefer atomic primitives over agents.

9. The graph MUST be acyclic and every reference MUST resolve. The
   server validates the result against the Pydantic schema and will
   reject malformed output.

10. CONVERGE ON REQUEST. If the user asks to "merge the branches",
    "converge the paths", or "share the tail", de-duplicate the repeated
    tail: keep the branch-specific prep nodes (with their ``activate_on``),
    add a ``merge`` node depending on the last node of each branch, and
    point a single shared tail (understand → resolve → reply, etc.) at the
    merge. Delete the now-duplicate per-branch tail nodes and re-wire.

Available node-kind shapes are identical to those documented in the
interpreter prompt. Pay particular attention to the satellite pattern:
memory / output_parser / human_handoff / and action+data_store with
``parent_agent_id`` are tools of their parent agent — they don't appear
in the executor's top-level walk, so don't put them in any
``depends_on`` chain.

Output ONLY the complete new WorkflowDefinition JSON."""


SUMMARIZE_NODE_SYSTEM_PROMPT = """You explain ONE node of an automated workflow to a non-technical business user.

You receive the full workflow definition (JSON) and the id of one node.
Write a SHORT, plain-English explanation of that single node covering:
  - what this node does,
  - when it runs (its trigger, upstream dependencies, or branch condition),
  - and what it produces or hands to the next step.

Rules:
  - 2 to 4 sentences. No headings, no bullet points, no markdown, no JSON.
  - Refer to other nodes by their human NAME, never their id.
  - Do not restate the whole workflow — focus on this one node.
  - Plain, concrete language a non-technical user understands. No jargon."""


class WorkflowInterpretationError(RuntimeError):
    """Raised when interpreter cannot produce a schema-valid definition."""


class WorkflowInterpreter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _azure_client(self) -> AsyncAzureOpenAI:
        ep = self._settings.AZURE_OPENAI_ENDPOINT.strip().rstrip("/")
        key = self._settings.AZURE_OPENAI_API_KEY.strip()
        if not ep or not key:
            raise WorkflowInterpretationError(
                "Azure OpenAI endpoint or API key missing (set AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY)."
            )
        return AsyncAzureOpenAI(
            azure_endpoint=ep,
            api_key=key,
            api_version=self._settings.AZURE_OPENAI_API_VERSION,
        )

    @trace_llm
    async def _call_llm(
        self,
        *,
        messages: list[dict[str, Any]],
    ) -> str:
        deployment = (
            self._settings.AZURE_OPENAI_DEPLOYMENT
            or self._settings.AZURE_OPENAI_DEFAULT_MODEL
        )
        client = self._azure_client()
        completion = await client.chat.completions.create(
            model=deployment,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=messages,
        )
        usage = completion.usage
        if usage is not None:
            ud = {
                k: int(v)
                for k, v in {
                    "input": usage.prompt_tokens,
                    "output": usage.completion_tokens,
                    "total": usage.total_tokens,
                }.items()
                if v is not None
            }
            if ud:
                try:
                    get_client().update_current_generation(
                        model=deployment,
                        usage_details=ud,
                    )
                except Exception:  # noqa: BLE001 — telemetry must not break interpreter
                    log.debug("langfuse.update_generation_failed", exc_info=True)
        choice = completion.choices[0]
        content = choice.message.content or ""
        if not content.strip():
            raise WorkflowInterpretationError("LLM returned empty content")
        return content

    @trace_llm
    async def _complete_text(
        self,
        *,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
    ) -> str:
        """Plain-text completion (no JSON-mode), used for prose like node
        summaries. Mirrors :meth:`_call_llm` but without ``response_format``."""
        deployment = (
            self._settings.AZURE_OPENAI_DEPLOYMENT
            or self._settings.AZURE_OPENAI_DEFAULT_MODEL
        )
        client = self._azure_client()
        completion = await client.chat.completions.create(
            model=deployment,
            temperature=temperature,
            messages=messages,
        )
        usage = completion.usage
        if usage is not None:
            ud = {
                k: int(v)
                for k, v in {
                    "input": usage.prompt_tokens,
                    "output": usage.completion_tokens,
                    "total": usage.total_tokens,
                }.items()
                if v is not None
            }
            if ud:
                try:
                    get_client().update_current_generation(
                        model=deployment,
                        usage_details=ud,
                    )
                except Exception:  # noqa: BLE001 — telemetry must not break the call
                    log.debug("langfuse.update_generation_failed", exc_info=True)
        content = completion.choices[0].message.content or ""
        if not content.strip():
            raise WorkflowInterpretationError("LLM returned empty content")
        return content.strip()

    @observe()
    async def summarize_node(
        self,
        *,
        definition: WorkflowDefinition,
        node_id: str,
    ) -> str:
        """Return a short plain-English explanation of a single node.

        The caller guarantees ``node_id`` exists in ``definition``. We pass the
        full graph for context so the model can reference upstream node names,
        and ask for prose (not JSON) via :meth:`_complete_text`.
        """
        definition_json = definition.model_dump_json()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SUMMARIZE_NODE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"WORKFLOW:\n{definition_json}\n\nEXPLAIN_NODE_ID: {node_id}"
                ),
            },
        ]
        try:
            return await self._complete_text(messages=messages)
        except OpenAIAPIError as exc:
            raise WorkflowInterpretationError(str(exc)) from exc

    @observe()
    async def interpret(
        self,
        *,
        user_input: str,
        available_tools: list[str],
    ) -> WorkflowDefinition:
        tools_prompt = ", ".join(available_tools) if available_tools else "(none)"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": INTERPRETER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Available tools: {tools_prompt}\n\n"
                    f"User request:\n{user_input.strip()}"
                ),
            },
        ]

        validation_error_detail: str | None = None
        for attempt in range(2):
            msgs = list(messages)
            if validation_error_detail is not None and attempt > 0:
                msgs.append(
                    {
                        "role": "user",
                        "content": (
                            "Your prior JSON failed validation:\n"
                            f"{validation_error_detail}\n"
                            "Respond again with ONLY corrected JSON conforming "
                            "to the schema."
                        ),
                    },
                )

            try:
                raw_json = await self._call_llm(messages=msgs)
            except WorkflowInterpretationError:
                raise
            except OpenAIAPIError as exc:
                raise WorkflowInterpretationError(str(exc)) from exc

            trimmed = raw_json.strip()
            if trimmed.startswith("```"):
                trimmed = trimmed.strip("`")
                idx = trimmed.find("{")
                if idx != -1:
                    trimmed = trimmed[idx:]
            try:
                data = json.loads(trimmed)
            except json.JSONDecodeError as exc:
                validation_error_detail = f"invalid JSON: {exc}"
                continue

            try:
                return normalize_action_providers(WorkflowDefinition.model_validate(data))
            except ValidationError as exc:
                validation_error_detail = exc.json()
                log.info(
                    "workflow.interpreter.validation_retry: %s",
                    str(exc.errors())[:2000],
                )

        raise WorkflowInterpretationError(
            validation_error_detail or "failed after retry"
        )

    @observe()
    async def augment(
        self,
        *,
        current_definition: WorkflowDefinition,
        user_message: str,
        available_tools: list[str],
        focus_node_id: str | None = None,
    ) -> WorkflowDefinition:
        """Return a modified ``WorkflowDefinition`` reflecting ``user_message``.

        Same retry-on-validation loop as :meth:`interpret`. Stable node ids
        from ``current_definition`` are expected to be preserved by the
        LLM — we don't enforce that here (the schema would reject an
        invalid graph anyway), but the system prompt makes it the rule.

        When ``focus_node_id`` is given, the instruction is scoped to that
        node: the model is told to change that node (and only re-wire what's
        strictly necessary), leaving every other node verbatim.
        """
        tools_prompt = ", ".join(available_tools) if available_tools else "(none)"
        current_json = current_definition.model_dump_json()
        focus_clause = ""
        if focus_node_id:
            focus_clause = (
                f"FOCUS_NODE_ID: {focus_node_id}\n"
                "The instruction below is about this node specifically. Apply the "
                "change to it (and only re-wire neighbours if strictly required); "
                "copy every other node verbatim.\n\n"
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": AUGMENT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Available tools: {tools_prompt}\n\n"
                    f"CURRENT_DEFINITION:\n{current_json}\n\n"
                    f"{focus_clause}"
                    f"INSTRUCTION:\n{user_message.strip()}"
                ),
            },
        ]

        validation_error_detail: str | None = None
        for attempt in range(2):
            msgs = list(messages)
            if validation_error_detail is not None and attempt > 0:
                msgs.append(
                    {
                        "role": "user",
                        "content": (
                            "Your prior JSON failed validation:\n"
                            f"{validation_error_detail}\n"
                            "Respond again with ONLY corrected JSON conforming "
                            "to the schema. Preserve unchanged node ids and "
                            "fields verbatim from CURRENT_DEFINITION."
                        ),
                    }
                )
            try:
                raw_json = await self._call_llm(messages=msgs)
            except WorkflowInterpretationError:
                raise
            except OpenAIAPIError as exc:
                raise WorkflowInterpretationError(str(exc)) from exc

            trimmed = raw_json.strip()
            if trimmed.startswith("```"):
                trimmed = trimmed.strip("`")
                idx = trimmed.find("{")
                if idx != -1:
                    trimmed = trimmed[idx:]
            try:
                data = json.loads(trimmed)
            except json.JSONDecodeError as exc:
                validation_error_detail = f"invalid JSON: {exc}"
                continue
            try:
                return normalize_action_providers(WorkflowDefinition.model_validate(data))
            except ValidationError as exc:
                validation_error_detail = exc.json()
                log.info(
                    "workflow.augment.validation_retry: %s",
                    str(exc.errors())[:2000],
                )

        raise WorkflowInterpretationError(
            validation_error_detail or "augment failed after retry"
        )


def diff_definitions(
    *,
    before: WorkflowDefinition,
    after: WorkflowDefinition,
) -> list[str]:
    """Return a short human-readable list of node-level changes.

    Compares two workflow definitions by node id and produces strings the
    UI can render as a diff list. Edge changes are surfaced as part of a
    node's ``modified`` entry. This is intentionally simple: callers want
    a 3-5 line "what did this do" summary, not a structural diff tool.
    """
    before_nodes = {n.id: n for n in before.iter_nodes()}
    after_nodes = {n.id: n for n in after.iter_nodes()}

    added = sorted(after_nodes.keys() - before_nodes.keys())
    removed = sorted(before_nodes.keys() - after_nodes.keys())
    common = before_nodes.keys() & after_nodes.keys()

    changes: list[str] = []
    for nid in added:
        n = after_nodes[nid]
        changes.append(f"added {n.kind!r} node {nid!r} ({n.name})")
    for nid in removed:
        n = before_nodes[nid]
        changes.append(f"removed {n.kind!r} node {nid!r} ({n.name})")
    for nid in sorted(common):
        b = before_nodes[nid]
        a = after_nodes[nid]
        # Compare model_dump output. Stable for Pydantic v2.
        if b.model_dump() != a.model_dump():
            changes.append(f"modified {nid!r} ({a.name})")
    return changes


__all__ = ["WorkflowInterpretationError", "WorkflowInterpreter", "diff_definitions"]
