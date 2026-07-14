"""Pydantic models for Phase 2 — NL-defined workflows persisted as JSON graphs.

Schema v2 introduces *polymorphic nodes*: in addition to ``AgentDefinition``
(rendered as a Dynamiq Agent at runtime) the graph may contain control-flow
nodes — ``condition`` (branch), ``for_each`` (fan-out over a list), ``merge``
(join point), and ``wait_for_webhook`` (parks the execution until an external
HTTP POST resumes it). Back-compat is preserved: definitions that only set
``agents`` continue to validate exactly as before, and the executor synthesises
a node list from ``agents`` when ``nodes`` is empty.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Node ID regex shared across every node kind. Anchored, ASCII-only,
# tolerates hyphens & underscores. Mirrors the original ``AgentDefinition.id``
# constraint so existing graphs keep validating.
# ---------------------------------------------------------------------------

_NODE_ID_PATTERN = r"^[a-zA-Z0-9_-]+$"


class AgentDefinition(BaseModel):
    """Single agent blueprint inside a WorkflowDefinition DAG.

    Kept as a top-level type for back-compat: ``WorkflowDefinition.agents``
    accepts a list of these. At runtime the executor wraps each one in an
    ``AgentNode`` (kind="agent") inside the unified node list.
    """

    id: str = Field(min_length=1, max_length=128, pattern=_NODE_ID_PATTERN)
    name: str = Field(min_length=1, max_length=255)
    role: str = Field(default="", description="Primary persona instructions")
    instructions: str = Field(
        default="",
        description="Operational instructions appended to the agent prompt.",
    )
    tools: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    is_parallel: bool = Field(
        default=False,
        description="Hint for parallel tiers (best-effort; execution may still serialize).",
    )
    # New in v2: gate this agent on a specific branch of a condition node, or
    # bind it as the body of a ``for_each`` loop.
    activate_on: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional gate. Mapping of upstream node id → required value. "
            "For a condition node, the value is the branch label the agent "
            "should activate for. For a for_each node, leave the value as "
            "'*' (or omit entirely — depends_on alone is enough)."
        ),
    )


# ---------------------------------------------------------------------------
# v2 polymorphic node kinds — used when a definition opts into the new
# control-flow primitives by emitting a ``nodes`` array.
# ---------------------------------------------------------------------------


class _BaseNode(BaseModel):
    """Common fields for every v2 node kind."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=_NODE_ID_PATTERN)
    name: str = Field(min_length=1, max_length=255)
    depends_on: list[str] = Field(default_factory=list)
    activate_on: dict[str, str] | None = Field(default=None)
    # Failure policy (currently honored for action nodes; see extended_executor):
    #   "fail"     — node error aborts the execution (default; unchanged).
    #   "continue" — node error is non-fatal; the node is marked skipped so its
    #                dependents prune, and the run proceeds.
    #   "route"    — node error is non-fatal and sets an "ok"/"failed" decision
    #                so downstream nodes can gate via ``activate_on`` (an error
    #                branch, e.g. notify-recruiter), mirroring IfNode branching.
    on_error: Literal["fail", "continue", "route"] = "fail"


class AgentNode(_BaseNode):
    """v2 node wrapping an agent. Same payload as ``AgentDefinition``.

    For the n8n "Tools Agent" composite pattern the agent additionally
    references three optional satellites by id:

    * ``memory_ref`` → a ``MemoryNode`` providing conversation state
    * ``output_parser_ref`` → an ``OutputParserNode`` enforcing a JSON schema
    * ``chat_model`` → an optional explicit LLM choice (provider+model);
      when unset the platform's default LLM is used.

    All three are RENDERED as satellites hanging below the agent in the
    visual editor; runtime-wise the executor resolves them by id at
    invocation time.
    """

    kind: Literal["agent"] = "agent"
    role: str = Field(default="")
    instructions: str = Field(default="")
    tools: list[str] = Field(default_factory=list)
    is_parallel: bool = Field(default=False)
    # ---- Tools-Agent composite slots ----
    memory_ref: str = Field(default="", max_length=128)
    output_parser_ref: str = Field(default="", max_length=128)
    chat_model: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional explicit chat-model choice. Shape: "
            "``{'provider': 'openai|anthropic|azure', 'model': 'gpt-5', "
            "'temperature': 0.0}``."
        ),
    )


class ConditionNode(_BaseNode):
    """LLM-evaluated router. Reads upstream outputs and emits one branch label.

    The executor calls a tiny zero-temperature LLM with the configured
    ``expression`` and the outputs of every upstream dependency, then routes
    by setting a state value ``<condition_id> = <branch label>``. Downstream
    agents gate on this via their ``activate_on`` map.
    """

    kind: Literal["condition"] = "condition"
    expression: str = Field(
        min_length=1,
        max_length=4000,
        description=(
            "Natural-language predicate or short rubric. Example: 'Is the "
            "customer an existing record? Return existing or new.'"
        ),
    )
    branches: list[str] = Field(
        default_factory=lambda: ["true", "false"],
        min_length=2,
        max_length=8,
    )


class ForEachNode(_BaseNode):
    """Fan-out over a JSON array produced by an upstream node.

    The executor expects ``items_from`` to refer to an upstream node whose
    output JSON-decodes to a list (or, when ``items_path`` is set, contains
    that list at a JSONPath inside the output). The nodes in ``body`` are
    executed once per item with the iteration value exposed as ``item_var``
    in the per-iteration input map.
    """

    kind: Literal["for_each"] = "for_each"
    items_from: str = Field(min_length=1, max_length=128)
    items_path: str = Field(default="$", max_length=512)
    item_var: str = Field(default="item", min_length=1, max_length=64)
    body: list[str] = Field(
        min_length=1,
        description="Node ids that form the per-item subgraph.",
    )
    max_concurrency: int = Field(default=4, ge=1, le=32)


class MergeNode(_BaseNode):
    """Join point. Output is a dict keyed by each upstream id."""

    kind: Literal["merge"] = "merge"


class WaitForWebhookNode(_BaseNode):
    """Parks the execution until ``POST /executions/{id}/resume/{token}``.

    The executor surfaces a ``wait_for_webhook`` event containing a signed
    token + a public resume URL the workflow author can embed in an email
    or candidate-facing page. The execution row's status becomes
    ``HITL_WAITING`` (re-used) and resumes when the payload arrives.
    """

    kind: Literal["wait_for_webhook"] = "wait_for_webhook"
    description: str = Field(default="", max_length=2000)
    timeout_seconds: int = Field(default=86400, ge=30, le=30 * 86400)
    response_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional JSON-Schema-ish hint describing the expected resume "
            "payload. Used by UI to render a parked-execution card."
        ),
    )


class TriggerNode(_BaseNode):
    """Origin node — yields the inbound payload as the workflow's input.

    Five flavours mirror what every workflow tool ships:

    * ``manual`` — user clicks "Run" and passes a JSON payload by hand.
    * ``webhook`` — platform mints ``POST /triggers/{slug}`` and resumes
      here when an external system calls it. Auth is via the slug + an
      optional shared secret.
    * ``form`` — platform auto-renders a public form at ``/forms/{slug}``
      from ``form_fields``; submit values become the workflow input.
    * ``schedule`` — cron-style firing (executor reads ``schedule_cron``).
    * ``chat`` — long-running conversational session. The platform mounts
      ``/chat/{slug}`` and streams turns back via SSE/websocket. A chat
      trigger paired with a ``memory`` node-kind keeps state across user
      turns; the downstream agent is invoked once per inbound message.
    """

    kind: Literal["trigger"] = "trigger"
    trigger_type: Literal["manual", "webhook", "form", "schedule", "chat"] = "manual"
    slug: str = Field(default="", max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$|^$")
    form_fields: list[dict[str, Any]] = Field(default_factory=list)
    schedule_cron: str = Field(default="", max_length=128)
    secret_required: bool = Field(default=False)
    # Chat-specific. Empty when ``trigger_type != "chat"``.
    chat_welcome_message: str = Field(default="", max_length=2000)
    chat_memory_ref: str = Field(
        default="",
        max_length=128,
        description=(
            "Node id of the MemoryNode this chat session writes/reads. The "
            "same memory node is typically referenced by the downstream "
            "AgentNode so trigger + agent share conversation state."
        ),
    )
    # Production-hardening: per-session ceilings for chat triggers. ``None``
    # means "unlimited" (suitable for internal demos; set explicit values
    # for any customer-facing trigger). The runtime enforces these via the
    # ``RateLimiter`` service before every LLM call.
    rate_limits: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional per-session ceilings. Shape: "
            "``{messages_per_minute: int, max_total_tokens: int, "
            "max_total_cost_cents: int}``. Any subset may be omitted; "
            "missing keys disable that particular ceiling."
        ),
    )


class ActionNode(_BaseNode):
    """Atomic integration call — provider + action slug + parameter map.

    This is the n8n "Gmail: Send Message" / "Google Calendar: Create
    Event" shape — no LLM, no prose instructions, just a deterministic
    API call. The executor's ``ActionRunner`` resolves ``provider`` to a
    stored workspace connection, instantiates the matching Dynamiq tool,
    and invokes it with ``params``. When no connection exists, the runner
    returns a dry-run stub so demo templates work without credentials.

    Values inside ``params`` may use ``{{ <upstream_id>.<json_path> }}``
    placeholders resolved against prior node outputs at execution time.

    When ``parent_agent_id`` is set, the action is a **satellite tool of
    that agent** — the top-level executor skips it; the parent agent
    invokes it through its tool-calling loop. Satellites must not declare
    ``depends_on`` (they live outside the graph's execution order).
    """

    kind: Literal["action"] = "action"
    provider: str = Field(min_length=1, max_length=64)
    action_slug: str = Field(min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)
    allow_dry_run: bool = Field(default=True)
    connection_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Bind this action to a specific workspace connection (for "
            "providers with multiple named accounts, e.g. 'Personal Gmail' vs "
            "'Company Gmail'). When unset, the executor uses the first active "
            "connection for the provider."
        ),
    )
    parent_agent_id: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "When set, this action is a satellite tool of the named agent. "
            "It does NOT appear in the top-level execution order; the agent "
            "invokes it via its tool-calling loop."
        ),
    )
    # Optional human label distinct from ``name`` — n8n shows the tool
    # description here ("Look up customer by email"); the LLM uses this
    # text to decide when to call the tool.
    tool_description: str = Field(default="", max_length=2000)
    # Production-hardening: per-invocation timeout + retry. A stuck tool
    # call would otherwise hold an LLM session open indefinitely; agents
    # would also bounce off transient upstream errors that a single retry
    # often clears (DNS, 502, rate-limit-with-Retry-After).
    timeout_ms: int = Field(default=30000, ge=100, le=300000)
    max_retries: int = Field(default=1, ge=0, le=5)
    retry_initial_delay_ms: int = Field(default=200, ge=0, le=10000)


class IfNode(_BaseNode):
    """Deterministic boolean branch.

    Unlike ``ConditionNode`` (LLM-evaluated router), ``IfNode`` evaluates
    a small expression over a JSON value pulled from an upstream node.
    Supported forms:

    * ``$.lookup.score > 75`` — compare JSONPath value to a constant
    * ``$.lookup.is_existing == true``
    * ``$.lookup.tickets.length > 0``

    Output is the literal string ``"true"`` or ``"false"``; downstream
    nodes gate on ``activate_on``.
    """

    kind: Literal["if"] = "if"
    expression: str = Field(min_length=1, max_length=2000)


class DataStoreNode(_BaseNode):
    """Workspace-managed key/value table operation.

    The platform owns a generic ``workflow_data`` JSONB table per
    workspace — the n8n "Store Candidates in Dashboard" /
    "Update Ranking in Dashboard" boxes all map to this primitive. No
    DB provisioning required.
    """

    kind: Literal["data_store"] = "data_store"
    op: Literal["write", "read", "query"] = "write"
    table: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    key: str = Field(default="", max_length=256)
    payload: dict[str, Any] = Field(default_factory=dict)
    filter: dict[str, Any] = Field(default_factory=dict)
    parent_agent_id: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "When set, this data_store op is a satellite tool of the named "
            "agent. Same semantics as ActionNode.parent_agent_id."
        ),
    )
    tool_description: str = Field(default="", max_length=2000)
    # Mirrors ActionNode — data_store ops can hit Postgres latency too.
    timeout_ms: int = Field(default=30000, ge=100, le=300000)
    max_retries: int = Field(default=1, ge=0, le=5)
    retry_initial_delay_ms: int = Field(default=200, ge=0, le=10000)


class MemoryNode(_BaseNode):
    """Persistent conversation memory for the Tools-Agent pattern.

    Memory is keyed by session id at runtime; the platform owns the
    store. Three scopes mirror what every chat runtime needs:

    * ``session`` — per-conversation; cleared when the chat trigger
      session ends (or its TTL elapses).
    * ``user``    — per-authenticated-user; survives across sessions.
    * ``workflow``— global to the workflow; useful for slow-changing
      reference data the agent should not have to re-fetch.

    A MemoryNode is referenced (by id) from BOTH the chat trigger
    (``chat_memory_ref``) and the AgentNode (``memory_ref``) — that's
    what makes the n8n "shared Memory between trigger and agent"
    pattern explicit instead of magic.

    Phase 1 ships the schema + visual representation; the runtime
    read/write contract is finalised in Phase 2. MemoryNodes therefore
    have no ``depends_on`` and never appear in the execution order.
    """

    kind: Literal["memory"] = "memory"
    scope: Literal["session", "user", "workflow"] = "session"
    store: Literal["redis", "postgres"] = "redis"
    ttl_seconds: int = Field(default=3600, ge=60, le=30 * 86400)
    # Hard ceiling on how many turns of history the memory exposes to
    # the agent. Prevents prompt-context bloat for long conversations.
    max_turns: int = Field(default=24, ge=1, le=512)
    parent_agent_id: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "When set, this memory is rendered as a satellite of the named "
            "agent. Optional — memory may also be referenced standalone "
            "(e.g. shared with a chat trigger)."
        ),
    )


class HumanHandoffNode(_BaseNode):
    """Tool satellite that pauses the conversation for a human agent.

    The agent's LLM calls this tool when it decides escalation is
    warranted (the tool's ``description`` text — "Escalate to a human
    when you can't resolve the customer's issue" — is what the LLM
    reads). The runtime intercepts the invocation, writes a queue row,
    flips the session status, and terminates the streaming response with
    a ``handoff_requested`` event.
    """

    kind: Literal["human_handoff"] = "human_handoff"
    parent_agent_id: str | None = Field(
        default=None,
        max_length=128,
        description="Parent agent. Always set in practice.",
    )
    # Description the LLM sees in its tool list. Picked carefully — the
    # default works for most CX agents.
    tool_description: str = Field(
        default=(
            "Escalate this conversation to a human agent. Call this only "
            "when the customer is frustrated, asking for a human, or when "
            "the issue is outside your competence. Pass a one-line reason."
        ),
        max_length=2000,
    )
    priority_default: Literal["low", "normal", "high"] = "normal"


class OutputParserNode(_BaseNode):
    """JSON-schema enforcer attached to an agent.

    Validates the agent's final response against ``json_schema`` and
    re-prompts on mismatch (up to ``max_retries`` times). This is the
    n8n "Structured Output" sub-node — it prevents downstream automation
    from breaking when the LLM produces free text instead of JSON.

    Like MemoryNode, OutputParserNodes are pure configuration: they
    never appear in the top-level execution order. The agent that
    references them (``output_parser_ref``) consults the schema at the
    end of its run.
    """

    kind: Literal["output_parser"] = "output_parser"
    json_schema: dict[str, Any] = Field(default_factory=dict)
    max_retries: int = Field(default=2, ge=0, le=5)
    parent_agent_id: str | None = Field(
        default=None,
        max_length=128,
        description="When set, rendered as a satellite of the named agent.",
    )


NodeDefinition = Annotated[
    Union[
        AgentNode,
        ActionNode,
        ConditionNode,
        IfNode,
        ForEachNode,
        MergeNode,
        WaitForWebhookNode,
        TriggerNode,
        DataStoreNode,
        MemoryNode,
        OutputParserNode,
        HumanHandoffNode,
    ],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Satellite helpers — node-kinds that may declare ``parent_agent_id``.
# Centralised so the validator and the executor agree on the rules.
# ---------------------------------------------------------------------------


_SATELLITE_KINDS: frozenset[str] = frozenset(
    {"action", "data_store", "memory", "output_parser", "human_handoff"}
)

# Kinds that hold configuration only — never invoked as workflow steps,
# regardless of whether they're satellites of an agent or stand-alone
# (a memory may be shared between a chat trigger and an agent, in which
# case it isn't a satellite of either but still must not execute as a
# top-level step). Cycle detection still considers them; only the
# execution order excludes them.
_NON_EXECUTABLE_KINDS: frozenset[str] = frozenset(
    {"memory", "output_parser", "human_handoff"}
)


def _is_satellite(node: Any) -> bool:
    """A node is a "satellite" iff it declares a non-empty ``parent_agent_id``.

    Satellites live outside the top-level execution order. The visual
    editor renders them hanging beneath their parent; the executor's
    top-level walker skips them; the parent agent invokes them via its
    tool-calling loop at run time.
    """
    return bool(getattr(node, "parent_agent_id", None))


def _is_executable(node: Any) -> bool:
    """``True`` when the executor's top-level walker should step into this node."""
    if _is_satellite(node):
        return False
    return getattr(node, "kind", None) not in _NON_EXECUTABLE_KINDS


class WorkflowDefinition(BaseModel):
    """JSON graph produced by the NL interpreter.

    Two shapes are accepted:

    * **Legacy** — set ``agents`` only; ``nodes`` is auto-derived. Validates
      identically to v1.
    * **v2** — set ``nodes`` to a list of polymorphic node kinds. ``agents``
      may be omitted entirely, or it may co-exist and the validator merges.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=4000)
    trigger: str = Field(default="", max_length=2048)
    agents: list[AgentDefinition] = Field(default_factory=list)
    nodes: list[NodeDefinition] = Field(default_factory=list)
    human_checkpoints: list[str] = Field(default_factory=list)
    output_format: str = Field(default="text", max_length=256)

    @model_validator(mode="before")
    @classmethod
    def _coerce_trigger_object(cls, data: Any) -> Any:
        """The NL interpreter sometimes emits the trigger *node object* in the
        top-level ``trigger`` field, which is meant to be a string trigger-type
        hint. Normalise: move the object into ``nodes`` (unless a trigger node
        is already present) and reduce ``trigger`` to its ``trigger_type``
        string. Without this, a chat/webhook trigger object raises a
        ``string_type`` validation error and the interpret call 503s."""
        if isinstance(data, dict):
            trig = data.get("trigger")
            if isinstance(trig, dict) and trig.get("kind") == "trigger":
                nodes = data.get("nodes")
                nodes = list(nodes) if isinstance(nodes, list) else []
                has_trigger_node = any(
                    isinstance(n, dict) and n.get("kind") == "trigger" for n in nodes
                )
                if not has_trigger_node:
                    data["nodes"] = [trig, *nodes]
                data["trigger"] = (
                    trig.get("trigger_type") or trig.get("slug") or "chat"
                )
        return data

    @staticmethod
    def _edges_from_nodes(nodes: list[_BaseNode]) -> Iterator[tuple[str, str]]:
        node_ids = {n.id for n in nodes}
        for n in nodes:
            for d in n.depends_on:
                if d not in node_ids:
                    raise ValueError(f"depends_on references unknown node id: {d!r}")
                yield d, n.id

    def iter_nodes(self) -> list[NodeDefinition]:
        """Unified node view — promotes legacy ``agents`` to ``AgentNode`` so
        downstream executors only deal with one shape."""
        if self.nodes:
            # ``nodes`` is authoritative when set; legacy ``agents`` is dropped
            # from the view (callers wanting both must put their agents under
            # ``nodes`` directly).
            return list(self.nodes)
        return [
            AgentNode(
                id=a.id,
                name=a.name,
                depends_on=list(a.depends_on),
                activate_on=dict(a.activate_on) if a.activate_on else None,
                role=a.role,
                instructions=a.instructions,
                tools=list(a.tools),
                is_parallel=a.is_parallel,
                memory_ref="",
                output_parser_ref="",
                chat_model=None,
            )
            for a in self.agents
        ]

    def agent_tool_bindings(self) -> dict[str, list[str]]:
        """Map agent node id → tool slug list for runtime tool wiring.

        v2 workflows declare agents under ``nodes``; legacy workflows use
        ``agents``. Tool factories must consult both so Composio/native
        bridge tools are attached regardless of which shape was persisted.
        """
        bindings: dict[str, list[str]] = {}
        for ag in self.agents:
            bindings[ag.id] = list(ag.tools)
        for node in self.iter_nodes():
            if isinstance(node, AgentNode):
                bindings.setdefault(node.id, list(node.tools))
        return bindings

    @model_validator(mode="after")
    def _validate_graph(self) -> WorkflowDefinition:
        # 1) Must declare at least one node (via either ``agents`` or ``nodes``).
        if not self.agents and not self.nodes:
            raise ValueError("workflow must declare at least one agent or node")

        # 2) For the legacy back-compat path, keep the original agent-id rules.
        if self.agents:
            agent_ids = [a.id for a in self.agents]
            if len(agent_ids) != len(set(agent_ids)):
                raise ValueError("agent ids must be unique")
            checkpoints = set(self.human_checkpoints)
            unknown_cp = checkpoints - set(agent_ids) - {n.id for n in self.nodes}
            if unknown_cp:
                raise ValueError(
                    "human_checkpoints references unknown agent ids: "
                    + ",".join(sorted(unknown_cp))
                )

        # 3) Unified node validation — runs over the promoted list so the
        #    same rules apply whether the caller used ``agents`` or ``nodes``.
        nodes_view = self.iter_nodes()
        ids = [n.id for n in nodes_view]
        if len(ids) != len(set(ids)):
            raise ValueError("node ids must be unique")

        id_set = set(ids)

        # 3a) ``activate_on`` references must be known node ids.
        for n in nodes_view:
            if not n.activate_on:
                continue
            for k in n.activate_on:
                if k not in id_set:
                    raise ValueError(
                        f"activate_on references unknown node id: {k!r} on node {n.id!r}"
                    )

        # 3b) Type-specific references.
        for n in nodes_view:
            if isinstance(n, ForEachNode):
                if n.items_from not in id_set:
                    raise ValueError(
                        f"for_each {n.id!r} items_from refers to unknown node {n.items_from!r}"
                    )
                for child_id in n.body:
                    if child_id not in id_set:
                        raise ValueError(
                            f"for_each {n.id!r} body refers to unknown node {child_id!r}"
                        )
            if isinstance(n, ConditionNode):
                if len(set(n.branches)) != len(n.branches):
                    raise ValueError(f"condition {n.id!r} has duplicate branches")

        by_id: dict[str, Any] = {n.id: n for n in nodes_view}

        # 3c) Satellite (parent_agent_id) integrity. These rules guard the
        #     tools-agent composite pattern from ill-formed graphs:
        #
        #     * Only the kinds in ``_SATELLITE_KINDS`` may declare it.
        #     * The referenced parent must exist and must be an AgentNode.
        #     * Satellites cannot themselves declare ``depends_on`` — they
        #       are not part of the top-level execution order.
        #     * Cross-agent shared memory is allowed (one MemoryNode may be
        #       referenced by multiple agents); we only reject pointing at
        #       a non-agent.
        for n in nodes_view:
            if not _is_satellite(n):
                continue
            if n.kind not in _SATELLITE_KINDS:
                raise ValueError(
                    f"node kind {n.kind!r} cannot declare parent_agent_id (node {n.id!r})"
                )
            parent_id = getattr(n, "parent_agent_id", None)
            parent = by_id.get(parent_id) if parent_id else None
            if parent is None:
                raise ValueError(
                    f"satellite {n.id!r} references unknown parent_agent_id {parent_id!r}"
                )
            if parent.kind != "agent":
                raise ValueError(
                    f"satellite {n.id!r} parent {parent_id!r} must be an "
                    f"AgentNode (got {parent.kind!r})"
                )
            if n.depends_on:
                raise ValueError(
                    f"satellite {n.id!r} cannot declare depends_on — "
                    "satellites are invoked by their parent agent, not by "
                    "the top-level executor"
                )

        # 3d) Agent ref integrity — memory_ref / output_parser_ref must
        #     point at the right kind of node when set.
        for n in nodes_view:
            if not isinstance(n, AgentNode):
                continue
            if n.memory_ref:
                tgt = by_id.get(n.memory_ref)
                if tgt is None or tgt.kind != "memory":
                    raise ValueError(
                        f"agent {n.id!r} memory_ref {n.memory_ref!r} must "
                        "reference a MemoryNode"
                    )
            if n.output_parser_ref:
                tgt = by_id.get(n.output_parser_ref)
                if tgt is None or tgt.kind != "output_parser":
                    raise ValueError(
                        f"agent {n.id!r} output_parser_ref {n.output_parser_ref!r} "
                        "must reference an OutputParserNode"
                    )

        # 3e) Trigger refs — a chat trigger's chat_memory_ref must point
        #     at a MemoryNode when set.
        for n in nodes_view:
            if isinstance(n, TriggerNode) and n.chat_memory_ref:
                tgt = by_id.get(n.chat_memory_ref)
                if tgt is None or tgt.kind != "memory":
                    raise ValueError(
                        f"trigger {n.id!r} chat_memory_ref {n.chat_memory_ref!r} "
                        "must reference a MemoryNode"
                    )

        # 3f) Every ``depends_on`` must reference an existing node id
        #     (and nothing may depend on a satellite).
        satellite_ids = {n.id for n in nodes_view if _is_satellite(n)}
        for n in nodes_view:
            for d in n.depends_on:
                if d not in id_set:
                    raise ValueError(
                        f"depends_on references unknown node id: {d!r} on node {n.id!r}"
                    )
                if d in satellite_ids:
                    raise ValueError(
                        f"node {n.id!r} cannot depend on satellite {d!r}"
                    )

        # 3g) Cycle detection over the executable graph only (satellites and
        #     pure-configuration kinds like memory / output_parser are not
        #     part of the execution order, so they can't form cycles).
        non_exec_ids = {n.id for n in nodes_view if not _is_executable(n)}
        top_level_ids = id_set - non_exec_ids
        incoming: defaultdict[str, set[str]] = defaultdict(set)
        outgoing: defaultdict[str, set[str]] = defaultdict(set)
        for n in nodes_view:
            if n.id in non_exec_ids:
                continue
            for d in n.depends_on:
                # Already rejected above; defensive against future
                # validator reordering.
                if d in non_exec_ids:
                    continue
                outgoing[d].add(n.id)
                incoming[n.id].add(d)
        indeg = {nid: len(incoming[nid]) for nid in top_level_ids}
        queue = [nid for nid in top_level_ids if indeg[nid] == 0]
        visited = 0
        while queue:
            cur = queue.pop()
            visited += 1
            for m in outgoing.get(cur, ()):
                indeg[m] -= 1
                if indeg[m] == 0:
                    queue.append(m)
        if visited != len(top_level_ids):
            raise ValueError("depends_on edges contain a cycle")

        return self


def workflow_execution_order(definition: WorkflowDefinition) -> list[str]:
    """Return top-level node ids in a valid execution order.

    Satellites (nodes with ``parent_agent_id`` set) are explicitly excluded
    — they're invoked by their parent agent's tool-calling loop, not by
    the top-level walker. Including them here would force them to run as
    independent steps too, which is almost always wrong.
    """
    nodes = [n for n in definition.iter_nodes() if _is_executable(n)]
    ids = [n.id for n in nodes]
    all_ids = set(ids)
    indeg = {i: 0 for i in all_ids}
    outgoing: defaultdict[str, list[str]] = defaultdict(list)
    for n in nodes:
        for dep in n.depends_on:
            if dep not in all_ids:
                # Satellite references — already rejected by the validator,
                # but guard so the topo sort doesn't underflow indeg.
                continue
            outgoing[dep].append(n.id)
            indeg[n.id] += 1
    queue = sorted(nid for nid in all_ids if indeg[nid] == 0)
    result: list[str] = []
    while queue:
        cur = queue.pop(0)
        result.append(cur)
        for dest in sorted(outgoing.get(cur, ())):
            indeg[dest] -= 1
            if indeg[dest] == 0:
                queue.append(dest)
        queue.sort()
    if len(result) != len(all_ids):
        raise ValueError("internal: cycle detected unexpectedly")
    return result


def satellites_by_agent(
    definition: WorkflowDefinition,
) -> dict[str, list[NodeDefinition]]:
    """Group satellite nodes by their parent agent id.

    Returned mapping is keyed by agent id; the values list satellites in
    declaration order (stable for visual layout). Agents with no
    satellites do not appear in the mapping.
    """
    out: dict[str, list[NodeDefinition]] = {}
    for n in definition.iter_nodes():
        if not _is_satellite(n):
            continue
        parent = getattr(n, "parent_agent_id", None)
        if not parent:
            continue
        out.setdefault(parent, []).append(n)
    return out


class ClarificationQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=4000)
    type: Literal["text", "choice", "multi_choice"]
    options: list[str] | None = None
    why_asked: str = Field(default="", max_length=2000)
    required: bool = True


class ClarificationAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1, max_length=128)
    answer: str | list[str]


class InterpretRequest(BaseModel):
    """Body for NL → WorkflowDefinition preview with optional clarification rounds."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(
        None,
        max_length=32000,
        description="Natural language workflow description; required for new sessions.",
    )
    session_id: str | None = Field(None, max_length=128)
    answers: list[ClarificationAnswer] = Field(default_factory=list)
    force_proceed: bool = False
    skip_clarification: bool = False
    workspace_id: uuid.UUID | None = Field(
        None,
        description="Workspace scope for new prompts; optional when continuing via session_id.",
    )

    @model_validator(mode="after")
    def _text_or_session(self) -> InterpretRequest:
        if self.session_id is None:
            if not self.text or not self.text.strip():
                raise ValueError("text is required when session_id is omitted")
        return self


class NeedsClarificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["needs_clarification"] = "needs_clarification"
    session_id: str
    questions: Annotated[list[ClarificationQuestion], Field(min_length=1, max_length=4)]
    round_number: int = Field(ge=1, le=3)
    original_prompt: str


class ReadyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready"] = "ready"
    definition: WorkflowDefinition
    augmented_prompt: str
    rounds_used: int = Field(ge=0)


InterpretResponse = Annotated[
    NeedsClarificationResponse | ReadyResponse,
    Field(discriminator="status"),
]


class AugmentRequest(BaseModel):
    """Body for incremental refinement of an existing graph via NL.

    The caller hands us the current definition plus a short instruction
    like "add a Slack notification after the Hiring Manager review". The
    interpreter returns a new ``WorkflowDefinition`` reflecting the
    requested change, preserving stable ids wherever possible so the
    visual canvas can diff cleanly.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(
        min_length=1,
        max_length=8000,
        description="NL instruction describing what to change.",
    )
    current_definition: WorkflowDefinition
    focus_node_id: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "When set, the instruction is scoped to this node — the model is "
            "told to focus its change there. Other nodes are still preserved."
        ),
    )


class AugmentResponse(BaseModel):
    """Result of an augment call — never persisted automatically.

    Callers preview ``proposed_definition`` in the visual canvas and then
    explicitly ``PUT /workflows/{id}`` to save. The ``changes`` array
    summarises what the interpreter did so the UI can render a diff list.
    """

    model_config = ConfigDict(extra="forbid")

    proposed_definition: WorkflowDefinition
    changes: list[str] = Field(
        default_factory=list,
        description="Human-readable summary of node-level changes.",
    )


class NodeSummaryRequest(BaseModel):
    """Body for an on-demand, LLM-generated explanation of one node.

    The caller hands us the *current working* definition (which may include
    unsaved canvas edits) so the summary reflects exactly what the user sees
    rather than the last persisted version. The node to explain is identified
    by the ``node_id`` path parameter.
    """

    model_config = ConfigDict(extra="forbid")

    definition: WorkflowDefinition


class NodeSummaryResponse(BaseModel):
    """Plain-English explanation of a single node, with a cache-hit flag."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    cached: bool = Field(
        default=False,
        description="True when served from the per-node-version cache.",
    )


class WorkflowRequirementsRequest(BaseModel):
    """Body for evaluating which integrations a (possibly unsaved) graph needs."""

    model_config = ConfigDict(extra="forbid")

    definition: WorkflowDefinition


class WorkflowRequirement(BaseModel):
    """One integration the workflow depends on, with live connection status."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    name: str
    kind: str = Field(description='"action" | "tool" | "llm" | "saas".')
    auth_type: str | None = None
    connectable: bool = Field(
        description="True when the platform can connect this provider inline."
    )
    required: bool = Field(description="True when it must be connected to publish.")
    connected: bool
    used_by: list[str] = Field(default_factory=list)
    reason: str = ""


class WorkflowRequirementsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirements: list[WorkflowRequirement] = Field(default_factory=list)
    missing_required: list[str] = Field(
        default_factory=list,
        description="Provider ids that are required, connectable, and not yet connected.",
    )
    publishable: bool = Field(
        description="True when no required integration is missing."
    )


class WorkflowCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: uuid.UUID
    definition: WorkflowDefinition
    slug: str | None = Field(
        None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
        description="URL-safe slug; auto-generated when omitted.",
    )
    change_note: str | None = Field(None, max_length=2000)


class WorkflowUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: WorkflowDefinition
    change_note: str | None = Field(None, max_length=2000)


class WorkflowRenameBody(BaseModel):
    """Name-only rename — does NOT create a version or change publish state."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)


ExecutionEventType = Literal[
    "workflow_start",
    "agent_start",
    "agent_thinking",
    "tool_call",
    "tool_result",
    "agent_complete",
    "hitl_required",
    "workflow_complete",
    "error",
    "heartbeat",
    # v2 control-flow events
    "condition_decided",
    "for_each_started",
    "for_each_item",
    "for_each_complete",
    "wait_for_webhook",
    "webhook_resumed",
    "node_skipped",
    # n8n-shape primitives
    "trigger_fired",
    "action_invoked",
    "action_result",
    "action_dry_run",
    "if_decided",
    "data_store_op",
]


class ExecutionEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: ExecutionEventType
    agent_id: str | None = None
    agent_name: str | None = None
    content: str | None = None
    tool_name: str | None = None
    data: dict[str, Any] | None = None
    checkpoint_id: str | None = None
    message: str | None = None


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_data: dict[str, Any] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)
    demo: bool = Field(
        default=False,
        description=(
            "When true, run a fully-mocked walk of the graph. No LLM calls, "
            "no external integrations — every node emits a deterministic stub "
            "output. Lets users preview the topology end-to-end without "
            "configuring credentials. Mirrors n8n's 'Test workflow' button."
        ),
    )
    use_real_llm: bool = Field(
        default=False,
        description=(
            "Only consulted when ``demo=true``. When true AND Azure OpenAI "
            "credentials are configured, the demo executor invokes the real "
            "LLM for agent nodes (using the workspace's default deployment) "
            "instead of returning a synthetic stub. External integrations "
            "remain dry-run regardless — this flag scopes specifically to "
            "the agent reasoning layer."
        ),
    )
    branch_overrides: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Demo only. Force specific condition/if nodes down a chosen branch "
            "(``{node_id: branch_label}``) so a test run can exercise each path "
            "deterministically — e.g. force the 'new customer' route. When "
            "omitted, conditions are decided by the LLM (if use_real_llm) or "
            "default to the first branch."
        ),
    )


class HITLApprovalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    feedback: str | None = Field(None, max_length=8000)


class DialogTurnBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=8000)
    workspace_id: uuid.UUID


class WorkflowVersionOut(BaseModel):
    id: uuid.UUID
    version: int
    change_note: str | None
    created_by: uuid.UUID
    created_at: datetime
    definition: WorkflowDefinition

    model_config = ConfigDict(from_attributes=True)

    @field_validator("definition", mode="before")
    @classmethod
    def _coerce_definition(cls, v: object) -> WorkflowDefinition:
        if isinstance(v, WorkflowDefinition):
            return v
        if isinstance(v, dict):
            return WorkflowDefinition.model_validate(v)
        raise TypeError("definition must be dict or WorkflowDefinition")


class WorkflowSummaryOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    slug: str
    current_version: int
    is_active: bool
    # Publish lifecycle: "draft" | "published" | "archived".
    status: str = "draft"
    published_at: datetime | None = None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    # Per-workflow autonomous self-heal policy (null = never auto-healed).
    self_heal: dict | None = None

    model_config = ConfigDict(from_attributes=True)


class WorkflowDetailOut(BaseModel):
    workflow: WorkflowSummaryOut
    versions: list[WorkflowVersionOut]


class WorkflowListOut(BaseModel):
    items: list[WorkflowSummaryOut]
    total: int
    page: int
    page_size: int


def slugify_name(name: str) -> str:
    """Derive a workspace-unique slug base from a human title."""
    base = name.lower().strip()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    if not base:
        base = "workflow"
    return base[:80]


__all__ = [
    "ActionNode",
    "AgentDefinition",
    "AgentNode",
    "AugmentRequest",
    "AugmentResponse",
    "ClarificationAnswer",
    "ClarificationQuestion",
    "ConditionNode",
    "DataStoreNode",
    "DialogTurnBody",
    "ExecutionEvent",
    "ExecutionEventType",
    "ExecutionRequest",
    "ForEachNode",
    "HITLApprovalBody",
    "HumanHandoffNode",
    "IfNode",
    "InterpretRequest",
    "InterpretResponse",
    "MemoryNode",
    "MergeNode",
    "NeedsClarificationResponse",
    "NodeDefinition",
    "NodeSummaryRequest",
    "NodeSummaryResponse",
    "OutputParserNode",
    "ReadyResponse",
    "TriggerNode",
    "WaitForWebhookNode",
    "WorkflowCreateBody",
    "WorkflowDefinition",
    "WorkflowDetailOut",
    "WorkflowListOut",
    "WorkflowRenameBody",
    "WorkflowRequirement",
    "WorkflowRequirementsRequest",
    "WorkflowRequirementsResponse",
    "WorkflowSummaryOut",
    "WorkflowUpdateBody",
    "WorkflowVersionOut",
    "satellites_by_agent",
    "slugify_name",
    "workflow_execution_order",
]
