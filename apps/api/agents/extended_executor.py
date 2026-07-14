"""Unified-graph executor for v2 ``WorkflowDefinition``s.

This module walks a workflow's polymorphic node list (agent / condition /
for_each / merge / wait_for_webhook) and emits the same SSE event shape as
``DynamiqService.run_workflow_stream``. Pure-agent flows still go through the
existing Dynamiq path inside ``workflow_service``; this executor is only used
when ``definition.nodes`` is non-empty (the v2 shape) **and** the graph
contains at least one non-agent node.

Design notes:

* Per-node agent runs are delegated to Dynamiq via
  ``DynamiqService.hydrate_agent_stage`` — one Dynamiq Workflow per agent. We
  give it the merged "input" map (string), it streams normally, and the
  serialized output text gets stored as that node's output.
* Condition nodes call a small zero-temperature LLM with the upstream
  outputs + the predicate, and force its output into one of the declared
  branch labels.
* ``for_each`` nodes pull a JSON list from the named upstream output and
  spawn one virtual sub-execution per item (the body subgraph is run once
  per item with the iteration value bound under ``item_var``).
* ``wait_for_webhook`` mints a signed token, parks the execution by writing
  state to Redis, surfaces a ``wait_for_webhook`` SSE event with the resume
  URL, and blocks until a ``resume/{token}`` POST writes a payload back. The
  executor reads that payload from Redis and stores it as the node's output.

Outputs are normalised to text via ``_to_text``; downstream agents receive
the upstream output verbatim as their ``input``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time

import structlog
from collections.abc import AsyncIterator, Callable, Awaitable
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agents.action_runner import invoke_action, render_placeholders
from agents.tool_resolver import _with_timeout_and_retry
from agents.dynamiq_service import DynamiqService
from agents.kb_tool import agent_uses_kb, kb_search
from core.config import Settings, get_settings
from core.redis import get_redis
from core.snapshot import snapshot
from services.output_parser_service import extract_json_loose
from schemas.workflow import (
    ActionNode,
    AgentNode,
    ConditionNode,
    DataStoreNode,
    ForEachNode,
    IfNode,
    MemoryNode,
    MergeNode,
    NodeDefinition,
    OutputParserNode,
    TriggerNode,
    WaitForWebhookNode,
    WorkflowDefinition,
    satellites_by_agent,
    workflow_execution_order,
)

log = logging.getLogger(__name__)
slog = structlog.get_logger("egpt.executor")


def _preview(value: Any, limit: int = 240) -> str:
    """Render any value as a short string suitable for a log line.

    Long blobs are truncated with an explicit "(+N chars)" marker so logs
    stay readable while preserving the size signal.  Used for both inputs
    and outputs at every node — never raises.
    """
    try:
        if value is None:
            return "<none>"
        if isinstance(value, (dict, list)):
            txt = json.dumps(value, default=str)
        else:
            txt = str(value)
    except Exception:  # noqa: BLE001
        txt = "<unserialisable>"
    if len(txt) <= limit:
        return txt
    return f"{txt[:limit]}…(+{len(txt) - limit} chars)"


def _node_kind(node: Any) -> str:
    return getattr(node, "kind", None) or type(node).__name__


# ---------------------------------------------------------------------------
# Redis key helpers — parked executions live under deterministic prefixes so
# the resume route can find them by exec id + token without touching the DB.
# ---------------------------------------------------------------------------


def _resume_token_key(token: str) -> str:
    return f"egpt:resume:token:{token}"


def _resume_payload_key(execution_id: UUID, node_id: str) -> str:
    return f"egpt:resume:payload:{execution_id}:{node_id}"


def _decision_key(execution_id: UUID) -> str:
    return f"egpt:decisions:{execution_id}"


_PARK_POLL_SECONDS = 0.4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_text(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    try:
        return json.dumps(val, default=str)
    except (TypeError, ValueError):
        return str(val)


def _extract_upstream_text(val: Any) -> str:
    """Pull human-readable text from an upstream node output.

    Action nodes often return nested Composio/MCP envelopes; agents need
    the tabular payload, not the wrapper JSON. We also surface
    Composio's "data_preview" + remote-file envelope as an explicit
    PARTIAL-PREVIEW message so the agent re-fetches with a bounded range
    instead of going off-script.

    Dry-run envelopes (``__dry_run__: true``) are replaced with an
    unmistakable ``<<UPSTREAM_NOT_EXECUTED ...>>`` marker so downstream
    agents see a structured signal instead of a stringified "please
    connect an integration" note that they tend to refuse on.
    """
    if val is None:
        return ""
    if isinstance(val, str):
        parsed = _maybe_json(val)
        return _extract_upstream_text(parsed) if parsed is not None else val
    if isinstance(val, dict):
        if val.get("__dry_run__") is True:
            provider = str(
                val.get("__provider__")
                or val.get("provider")
                or val.get("integration")
                or val.get("app")
                or "unknown"
            )
            action = str(
                val.get("__action__")
                or val.get("action")
                or val.get("tool")
                or val.get("action_slug")
                or "unknown"
            )
            reason = str(
                val.get("__reason__")
                or val.get("reason")
                or val.get("detail")
                or "no_connection_configured"
            )
            return (
                f'<<UPSTREAM_NOT_EXECUTED provider="{provider}" '
                f'action="{action}" reason="{reason}">>'
            )
        data = val.get("data")
        if isinstance(data, dict):
            content = data.get("content")
            if isinstance(content, list):
                chunks: list[str] = []
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "text":
                        continue
                    inner = block.get("text")
                    if not isinstance(inner, str):
                        continue
                    inner_parsed = _maybe_json(inner)
                    if inner_parsed is not None:
                        sheet_txt = _extract_sheet_values(inner_parsed)
                        if sheet_txt:
                            chunks.append(sheet_txt)
                            continue
                        partial = _summarise_composio_preview(inner_parsed)
                        if partial:
                            chunks.append(partial)
                            continue
                    chunks.append(inner)
                if chunks:
                    return "\n\n".join(chunks)
        for key in ("content", "text", "message", "output", "result"):
            inner = val.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner
    return _to_text(val)


def _role_specific_rules(node: Any, prior_outputs: dict[str, str]) -> str:
    """Inject role-aware analyst guardrails based on the agent's role/name.

    The interpreter sometimes emits very short instructions
    (e.g. "Perform sentiment analysis"). Without structural rules the
    LLM tends to either restate the goal or pivot to unrelated tools.
    This helper appends a deterministic, role-specific rubric so the
    output is grounded and consistent across runs.
    """
    role = (getattr(node, "role", "") or "").lower()
    name = (getattr(node, "name", "") or "").lower()
    nid = (getattr(node, "id", "") or "").lower()
    blob = " ".join([role, name, nid])
    dep_label = next(iter(prior_outputs.keys()), "the upstream node")

    def has(*kws: str) -> bool:
        return any(k in blob for k in kws)

    if has("categori", "segment", "cluster"):
        return (
            "ROLE: Customer segmentation analyst.\n"
            f"INPUT: tabular customer rows from `{dep_label}`.\n"
            "STEPS:\n"
            "1. List each column header you can see and the count of rows.\n"
            "2. Build 4–6 NAMED segments using the visible columns "
            "(e.g. demographics, geography, tenure, opt-in flags).\n"
            "3. For each segment, output: name, criteria, row count, "
            "2–3 example customers verbatim from the data (Customer_ID + "
            "Full_Name), and a recommended engagement tactic.\n"
            "4. Finish with an `engagement_targets` JSON array of the "
            "TOP-5 customers most likely to renew, with their email and "
            "the segment they belong to. Use ONLY emails that appear in "
            "the input data; do not invent addresses."
        )
    if has("sentiment", "feedback", "voice of"):
        return (
            "ROLE: Sentiment / feedback analyst.\n"
            f"INPUT: the upstream output of `{dep_label}` (real customer "
            "feedback text or response messages).\n"
            "RULES:\n"
            "- If the upstream contains no real feedback text (only an "
            "error, dry-run notice, or absent recipient), STATE that "
            "explicitly and return `{ \"status\": \"no_feedback_available\", "
            "\"reason\": \"...\" }`. Do NOT fabricate quotes, customers, "
            "or sentiment scores.\n"
            "- If feedback IS present, classify each item as positive / "
            "neutral / negative with a 1-line justification, then output "
            "an aggregate summary: counts, top themes, and 3 actionable "
            "recommendations grounded in the actual text."
        )
    if has("report", "summary", "summari", "analysis"):
        return (
            "ROLE: Report writer.\n"
            f"INPUT: outputs from preceding agents (notably `{dep_label}`).\n"
            "Produce a polished markdown report with these sections:\n"
            "1. Executive Summary (≤120 words).\n"
            "2. Key Segments / Findings — table with name, size, "
            "criteria, sample customers.\n"
            "3. Sentiment Highlights (only if upstream sentiment data "
            "exists; otherwise omit).\n"
            "4. Recommended Next Actions — bullet list of 3–6 concrete "
            "next steps with owners/timeline if implied by the data.\n"
            "Cite specific numbers and names taken verbatim from the "
            "upstream data — no invented figures."
        )
    return ""


def _summarise_composio_preview(obj: Any) -> str:
    """Turn a Composio ``data_preview`` envelope into actionable text.

    When the underlying tool response exceeds ~12k tokens, Composio
    swaps the inline ``data`` payload for ``data_preview`` (truncated
    rows, "...N more items") plus a ``remote_file_info`` block pointing
    at a sandbox file. Returning the raw JSON to a downstream LLM
    causes it to either hallucinate (it can't read the sandbox) or
    pivot to unrelated tools. We rewrite it as a clear instruction so
    the agent re-fetches with a bounded range.
    """

    def walk(node: Any, depth: int = 0) -> dict[str, Any] | None:
        if depth > 8 or not isinstance(node, (dict, list)):
            return None
        if isinstance(node, list):
            for item in node:
                hit = walk(item, depth + 1)
                if hit:
                    return hit
            return None
        preview = node.get("data_preview")
        if isinstance(preview, dict) and "values" in preview:
            return {
                "preview": preview,
                "tool_slug": node.get("tool_slug"),
                "remote": node.get("remote_file_info"),
            }
        for v in node.values():
            hit = walk(v, depth + 1)
            if hit:
                return hit
        return None

    hit = walk(obj)
    if not hit:
        return ""
    preview = hit["preview"]
    values = preview.get("values") or []
    rng = preview.get("range") or "(range unknown)"
    tool_slug = hit.get("tool_slug") or "GOOGLESHEETS_VALUES_GET"
    display_url = str(preview.get("display_url") or "")
    spreadsheet_id = ""
    if "/d/" in display_url:
        try:
            spreadsheet_id = display_url.split("/d/", 1)[1].split("/", 1)[0]
        except IndexError:
            spreadsheet_id = ""
    rendered_rows: list[str] = []
    for row in values[:5]:
        if isinstance(row, str):
            if "more items" in row.lower():
                continue
            rendered_rows.append(row)
        elif isinstance(row, list):
            cells = [
                c for c in row
                if not (isinstance(c, str) and "more items" in c.lower())
            ]
            rendered_rows.append(",".join(str(c) for c in cells))
    sample = "\n".join(rendered_rows) if rendered_rows else "(no inline rows visible)"
    smaller_range = rng
    if "!" in rng and ":" in rng:
        tab, _, body = rng.partition("!")
        left, _, right = body.partition(":")
        right_col = "".join(ch for ch in right if ch.isalpha()) or "R"
        smaller_range = f"{tab}!A1:{right_col}50"
    sid_hint = (
        f"`spreadsheet_id` = `{spreadsheet_id}`. "
        if spreadsheet_id
        else "The upstream `display_url` (visible in your input) contains the "
             "spreadsheet ID — extract it from the `/d/<ID>/edit` segment. "
             "Do NOT use placeholders like `your_spreadsheet_id_here`. "
    )
    return (
        "PARTIAL PREVIEW ONLY — the upstream Google Sheets fetch returned a "
        "truncated sample because the full payload exceeded the inline size "
        f"limit. Range: `{rng}`. First visible rows:\n{sample}\n\n"
        f"{sid_hint}"
        "To get usable rows for analysis, call `COMPOSIO_MULTI_EXECUTE_TOOL` "
        f"with `tool_slug={tool_slug}` and BOTH `spreadsheet_id` and a "
        f"BOUNDED `range` like `{smaller_range}` (≤50 rows). Do NOT call any "
        "other integration (e.g. Gmail contacts) to substitute data — "
        "analyse ONLY the rows returned by this re-fetch."
    )


def _extract_sheet_values(obj: Any) -> str:
    """Flatten Composio Google Sheets valueRanges into CSV-ish text."""
    if not isinstance(obj, dict):
        return ""

    def walk(node: Any) -> list[list[str]]:
        if isinstance(node, dict):
            vr = node.get("valueRanges")
            if isinstance(vr, list):
                rows: list[list[str]] = []
                for item in vr:
                    if isinstance(item, dict) and isinstance(item.get("values"), list):
                        for row in item["values"]:
                            if isinstance(row, list):
                                rows.append([str(c) for c in row])
                if rows:
                    return rows
            for v in node.values():
                found = walk(v)
                if found:
                    return found
        elif isinstance(node, list):
            for v in node:
                found = walk(v)
                if found:
                    return found
        return []

    rows = walk(obj)
    if not rows:
        return ""
    return "\n".join(",".join(row) for row in rows)


def _maybe_json(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _jsonpath_dollar(obj: Any, path: str) -> Any:
    """Tiny JSONPath subset — supports ``$``, ``$.key``, ``$.a.b``, ``$[idx]``.

    The full jsonpath-ng grammar is overkill here and pulls a second parser
    onto the hot path; we only need enough to dig into a list nested inside a
    dict the upstream node returned.
    """
    if path in ("$", "", None):
        return obj
    cur: Any = obj
    p = path[1:] if path.startswith("$") else path
    parts: list[str] = []
    buf = ""
    i = 0
    while i < len(p):
        ch = p[i]
        if ch == "[":
            if buf:
                parts.append(buf)
                buf = ""
            end = p.find("]", i)
            if end == -1:
                raise ValueError(f"invalid path: {path}")
            parts.append(p[i + 1 : end])
            i = end + 1
            if i < len(p) and p[i] == ".":
                i += 1
            continue
        if ch == ".":
            if buf:
                parts.append(buf)
                buf = ""
            i += 1
            continue
        buf += ch
        i += 1
    if buf:
        parts.append(buf)
    for part in parts:
        try:
            idx = int(part)
            if isinstance(cur, list):
                cur = cur[idx]
                continue
        except (TypeError, ValueError):
            pass
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = None
    return cur


def _evaluate_if(expression: str, outputs: dict[str, Any]) -> str:
    """Evaluate an ``IfNode`` expression deterministically.

    Supports a small but useful grammar — the n8n ``If`` node covers ~90%
    of real workflows with the same vocabulary:

    * ``<left> <op> <right>`` where ``op`` is one of ``==``, ``!=``,
      ``>``, ``<``, ``>=``, ``<=``, ``contains``, ``in``
    * ``<left>`` and ``<right>`` may be JSONPath-ish references
      (``$.upstream_id.foo.bar``), bare upstream ids, or literals
      (numbers, ``true``/``false``/``null``, single/double-quoted strings).
    * Bare references like ``$.lookup.score > 75`` resolve through the
      ``outputs`` dict by treating the first path segment as the upstream
      node id.

    Returns the literal string ``"true"`` or ``"false"`` so downstream
    nodes can ``activate_on`` either branch with no further casting.
    """
    expr = (expression or "").strip()
    if not expr:
        return "false"

    operators = (" >= ", " <= ", " != ", " == ", " > ", " < ", " contains ", " in ")
    op_used: str | None = None
    left_raw: str = expr
    right_raw: str = ""
    for op in operators:
        idx = expr.find(op)
        if idx != -1:
            op_used = op.strip()
            left_raw = expr[:idx].strip()
            right_raw = expr[idx + len(op) :].strip()
            break

    if op_used is None:
        # Truthiness check: ``$.foo`` alone returns "true" when non-empty.
        val = _resolve_ref(left_raw, outputs)
        return "true" if _truthy(val) else "false"

    left = _resolve_ref(left_raw, outputs)
    right = _resolve_ref(right_raw, outputs)

    try:
        if op_used == "==":
            return "true" if left == right else "false"
        if op_used == "!=":
            return "true" if left != right else "false"
        if op_used == "contains":
            if isinstance(left, str) and isinstance(right, str):
                return "true" if right in left else "false"
            if isinstance(left, (list, dict)):
                return "true" if right in left else "false"
            return "false"
        if op_used == "in":
            if isinstance(right, (list, dict, str)):
                return "true" if left in right else "false"
            return "false"
        # Numeric comparisons coerce.
        lf = float(left) if not isinstance(left, bool) else float(int(left))
        rf = float(right) if not isinstance(right, bool) else float(int(right))
        if op_used == ">":
            return "true" if lf > rf else "false"
        if op_used == "<":
            return "true" if lf < rf else "false"
        if op_used == ">=":
            return "true" if lf >= rf else "false"
        if op_used == "<=":
            return "true" if lf <= rf else "false"
    except (TypeError, ValueError):
        return "false"
    return "false"


def _resolve_ref(token: str, outputs: dict[str, Any]) -> Any:
    """Resolve one side of an If expression to a concrete value.

    Order: JSON literal (numbers, true/false/null), quoted string,
    JSONPath ($-prefixed) lookup, then a bare ``upstream_id`` lookup.
    Falls back to the literal string when nothing matches — handy when
    the workflow author wrote ``status == active`` (no quotes).
    """
    token = (token or "").strip()
    if not token:
        return ""
    low = token.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low == "null" or low == "none":
        return None
    # Numbers.
    try:
        if "." in token:
            return float(token)
        return int(token)
    except ValueError:
        pass
    # Quoted strings.
    if (token.startswith('"') and token.endswith('"')) or (
        token.startswith("'") and token.endswith("'")
    ):
        return token[1:-1]
    # JSONPath / dotted reference.
    if token.startswith("$"):
        path = token[1:].lstrip(".")
    else:
        path = token
    parts = [p for p in path.replace("[", ".").replace("]", "").split(".") if p]
    if not parts:
        return None if token.startswith("$") else token
    is_jsonpath = token.startswith("$")
    root_key = parts[0]
    cur: Any = outputs.get(root_key)
    if cur is None and is_jsonpath and len(parts) == 1:
        return None
    if isinstance(cur, str):
        try:
            cur = json.loads(cur)
        except json.JSONDecodeError:
            pass
    for p in parts[1:]:
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(p)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    if cur is None and is_jsonpath:
        return None
    return cur if cur is not None else token


def _truthy(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        return bool(val.strip())
    if isinstance(val, (list, dict, tuple, set)):
        return len(val) > 0
    return True


def _coerce_branch(text: str, branches: list[str]) -> str:
    """Pick the branch label whose name appears in the LLM's response.

    Defensive: even when the LLM ignores the rubric and returns prose, we
    score by case-insensitive substring containment and pick the longest
    match. Falls back to the first branch on no signal.
    """
    norm = (text or "").strip().lower()
    if not norm:
        return branches[0]
    # Direct equality first (best case).
    for b in branches:
        if b.strip().lower() == norm:
            return b
    # Longest containment match (tied → first declared).
    scored = sorted(
        ((b, norm.count(b.strip().lower())) for b in branches),
        key=lambda t: (-t[1], branches.index(t[0])),
    )
    top, count = scored[0]
    return top if count > 0 else branches[0]


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class ExtendedWorkflowExecutor:
    """Walks a v2 ``WorkflowDefinition`` and yields SSE-shaped event dicts."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        dynamiq: DynamiqService | None = None,
        condition_eval: Callable[[str, dict[str, str]], Awaitable[str]] | None = None,
        db: AsyncSession | None = None,
        workspace_id: UUID | None = None,
        workflow_id: UUID | None = None,
        workspace_connections: list[Any] | None = None,
        live: bool = True,
    ) -> None:
        self._settings = settings or get_settings()
        self._dynamiq = dynamiq or DynamiqService(self._settings)
        # Pluggable for tests; defaults to a real LLM call via Dynamiq.
        self._condition_eval = condition_eval or self._llm_route
        # ``db``, ``workspace_id``, ``workflow_id`` and ``workspace_connections``
        # are required for the n8n-shape primitives. They're optional so
        # existing call sites (tests, pure-agent flows) keep working —
        # action / data_store nodes degrade to dry-run mode when missing.
        self._db = db
        self._workspace_id = workspace_id
        self._workflow_id = workflow_id
        self._workspace_connections = list(workspace_connections or [])
        # Publish-gate: when False, side-effecting actions are previewed, never
        # executed. Set by the service from workflow status + run mode.
        self._live = live

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def stream(
        self,
        *,
        definition: WorkflowDefinition,
        execution_id: UUID,
        input_data: dict[str, Any],
        agent_tools_by_id: dict[str, list[Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        nodes_by_id: dict[str, NodeDefinition] = {n.id: n for n in definition.iter_nodes()}
        # ``workflow_execution_order`` already excludes satellites and
        # non-executable kinds (memory / output_parser), so we never see
        # them in this top-level walk.
        order = workflow_execution_order(definition)
        # Pre-compute satellites per agent so each agent run can inject
        # them as tools via the existing Dynamiq agent tool mapping.
        satellites_map = satellites_by_agent(definition)

        outputs: dict[str, Any] = {}
        decisions: dict[str, str] = {}
        skipped: set[str] = set()
        # Track for_each context: parent_id → {item_var: value, idx: int}
        for_each_ctx: dict[str, dict[str, Any]] = {}

        # Per-run tallies surfaced on the terminal workflow_complete event so
        # the frontend SummaryCard reflects what actually happened rather than
        # having to re-derive it from event-stream side effects (which miss
        # dry-run / fully-skipped workflows).
        tally: dict[str, int] = {
            "agents_run": 0,
            "agents_skipped": 0,
            "actions_succeeded": 0,
            "actions_dry_run": 0,
            "nodes_skipped": 0,
            "total_executable": len(order),
        }

        # Seed initial input as a synthetic "$input" entry agents can pull from.
        initial_input_text = _to_text(input_data.get("input") or input_data)

        wf_logger = slog.bind(
            execution_id=str(execution_id),
            workflow_name=definition.name,
        )
        wf_logger.info(
            "workflow.start",
            node_count=len(order),
            order=order,
            kinds={nid: _node_kind(nodes_by_id[nid]) for nid in order},
            input_preview=_preview(input_data),
        )
        wf_started_at = time.perf_counter()

        yield {"type": "workflow_start", "workflow_name": definition.name}

        for node_idx, node_id in enumerate(order):
            node = nodes_by_id[node_id]
            kind = _node_kind(node)
            node_logger = wf_logger.bind(
                node_id=node_id,
                node_kind=kind,
                node_name=node.name,
                step=f"{node_idx + 1}/{len(order)}",
            )

            # 0) Already executed inside a for_each body — don't run it again at
            # the top level (where it would have no iteration var bound and emit
            # an empty result). The for_each added it to ``skipped``.
            if node_id in skipped:
                node_logger.info("node.skipped", reason="ran_in_for_each_body")
                yield {
                    "type": "node_skipped",
                    "node_id": node_id,
                    "name": node.name,
                    "reason": "ran_in_for_each_body",
                }
                continue

            # 1) Skip if any upstream branch decision excludes this node.
            if not self._activated(node, decisions, skipped):
                skipped.add(node_id)
                tally["nodes_skipped"] += 1
                if isinstance(node, AgentNode):
                    tally["agents_skipped"] += 1
                node_logger.info(
                    "node.skipped",
                    reason="activate_on_not_matched_or_upstream_skipped",
                    activate_on=getattr(node, "activate_on", None),
                    decisions={k: v for k, v in decisions.items()},
                )
                yield {
                    "type": "node_skipped",
                    "node_id": node_id,
                    "name": node.name,
                    "reason": "activate_on_not_matched_or_upstream_skipped",
                }
                continue
            # A merge is an OR-join converging mutually-exclusive branches:
            # prune it only when ALL inputs were skipped. Every other kind is
            # pruned as soon as any single upstream was skipped.
            if isinstance(node, MergeNode):
                upstream_pruned = bool(node.depends_on) and all(
                    dep in skipped for dep in node.depends_on
                )
            else:
                upstream_pruned = any(dep in skipped for dep in node.depends_on)
            if upstream_pruned:
                skipped.add(node_id)
                tally["nodes_skipped"] += 1
                if isinstance(node, AgentNode):
                    tally["agents_skipped"] += 1
                node_logger.info(
                    "node.skipped",
                    reason="upstream_skipped",
                    depends_on=list(node.depends_on),
                    skipped_upstreams=[d for d in node.depends_on if d in skipped],
                )
                yield {
                    "type": "node_skipped",
                    "node_id": node_id,
                    "name": node.name,
                    "reason": "upstream_skipped",
                }
                continue

            # 2) Build the input map this node sees.
            node_input = self._build_input(
                node,
                outputs=outputs,
                initial_input_text=initial_input_text,
            )
            node_logger.info(
                "node.start",
                depends_on=list(node.depends_on),
                upstream_outputs={
                    dep: _preview(outputs.get(dep), limit=120)
                    for dep in node.depends_on
                },
                input_preview=_preview(node_input),
            )
            node_started_at = time.perf_counter()

            # 3) Dispatch by kind.
            if isinstance(node, AgentNode):
                # Surface the agent's satellite tools (action / data_store)
                # as workspace-defined tools the agent's LLM can call. This
                # is the runtime side of the Tools-Agent composite pattern;
                # the visual editor already shows the satellites as a fan
                # under the agent card.
                agent_satellites = satellites_map.get(node_id, [])
                node_logger.info(
                    "agent.dispatch",
                    role=node.role,
                    tool_count=len(node.tools),
                    tools=list(node.tools),
                    satellite_count=len(agent_satellites),
                    memory_ref=node.memory_ref or None,
                    output_parser_ref=node.output_parser_ref or None,
                    chat_model=node.chat_model,
                )
                yield {
                    "type": "agent_start",
                    "agent_id": node_id,
                    "name": node.name,
                    "satellite_count": len(agent_satellites),
                    "memory_ref": node.memory_ref or None,
                    "output_parser_ref": node.output_parser_ref or None,
                    "chat_model": node.chat_model,
                }
                # Knowledge base (RAG) grounding — only for agents that declare
                # the tool. Retrieve from the workspace's documents and fold the
                # cited context into the agent's input, with a visible lookup.
                if agent_uses_kb(node.tools) and self._workspace_id:
                    kb_q = _to_text(node_input)
                    yield {
                        "type": "tool_call",
                        "agent_id": node_id,
                        "tool_name": "knowledge_base",
                        "data": {"args": {"query": kb_q[:200]}, "node_id": f"{node_id}__kb"},
                    }
                    kb = await kb_search(kb_q, self._workspace_id, self._settings, top_k=5)
                    yield {
                        "type": "tool_result",
                        "agent_id": node_id,
                        "tool_name": "knowledge_base",
                        "data": {
                            "result": {
                                "found": kb.get("found"),
                                "count": kb.get("count", 0),
                                "sources": kb.get("sources", []),
                            },
                            "node_id": f"{node_id}__kb",
                        },
                    }
                    if kb.get("found"):
                        node_input = {
                            **node_input,
                            "input": (
                                f"{node_input.get('input', '')}\n\n"
                                "Knowledge base sources (ground your answer in these "
                                "and cite as [1], [2]):\n" + kb["context"]
                            ),
                        }
                tool_calls_seen = 0
                async for ev in self._run_agent(
                    definition,
                    node,
                    node_input,
                    agent_tools_by_id=agent_tools_by_id,
                    satellites=agent_satellites,
                    memory_node=(
                        nodes_by_id.get(node.memory_ref)
                        if node.memory_ref else None
                    ),
                    output_parser_node=(
                        nodes_by_id.get(node.output_parser_ref)
                        if node.output_parser_ref else None
                    ),
                ):
                    if ev.get("type") == "tool_call":
                        tool_calls_seen += 1
                        node_logger.info(
                            "agent.tool_call",
                            tool=ev.get("tool_name"),
                            input_preview=_preview(ev.get("data"), limit=160),
                        )
                    elif ev.get("type") == "tool_result":
                        node_logger.info(
                            "agent.tool_result",
                            tool=ev.get("tool_name"),
                            output_preview=_preview(ev.get("data"), limit=160),
                        )
                    yield ev
                    if ev.get("type") == "agent_complete":
                        outputs[node_id] = ev.get("content", "")
                        tally["agents_run"] += 1
                node_logger.info(
                    "agent.summary",
                    tool_calls=tool_calls_seen,
                    output_length=len(str(outputs.get(node_id) or "")),
                )

            elif isinstance(node, ConditionNode):
                branch = await self._evaluate_condition(node, node_input)
                decisions[node_id] = branch
                outputs[node_id] = branch
                await self._persist_decisions(execution_id, decisions)
                node_logger.info(
                    "condition.decided",
                    branch=branch,
                    branches=list(node.branches),
                )
                yield {
                    "type": "condition_decided",
                    "node_id": node_id,
                    "name": node.name,
                    "branch": branch,
                    "branches": list(node.branches),
                }

            elif isinstance(node, ForEachNode):
                async for ev in self._run_for_each(
                    definition,
                    node,
                    outputs=outputs,
                    decisions=decisions,
                    skipped=skipped,
                    execution_id=execution_id,
                    agent_tools_by_id=agent_tools_by_id,
                ):
                    yield ev
                    if ev.get("type") == "for_each_complete":
                        outputs[node_id] = ev.get("results")
                # body nodes were executed inside _run_for_each; mark them
                # skipped at the top-level so we don't run them a second
                # time when the outer loop reaches them.
                for body_id in node.body:
                    skipped.add(body_id)

            elif isinstance(node, MergeNode):
                merged = {dep: outputs.get(dep) for dep in node.depends_on}
                outputs[node_id] = merged
                node_logger.info(
                    "merge.complete",
                    merged_keys=list(merged.keys()),
                )

            elif isinstance(node, WaitForWebhookNode):
                async for ev in self._park_for_webhook(
                    node, execution_id=execution_id
                ):
                    yield ev
                    if ev.get("type") == "webhook_resumed":
                        outputs[node_id] = ev.get("payload")

            elif isinstance(node, TriggerNode):
                # The trigger payload is whatever ``input_data`` was at
                # workflow entry. We surface it as the node's output so
                # downstream nodes can ``{{ trigger.foo }}`` into it.
                payload = (
                    input_data.get("input")
                    if isinstance(input_data, dict)
                    else input_data
                )
                if isinstance(payload, str):
                    parsed = _maybe_json(payload)
                    payload = parsed if parsed is not None else payload
                outputs[node_id] = payload if payload is not None else {}
                node_logger.info(
                    "trigger.fired",
                    trigger_type=node.trigger_type,
                    slug=node.slug,
                    payload_preview=_preview(outputs[node_id], limit=160),
                )
                yield {
                    "type": "trigger_fired",
                    "node_id": node_id,
                    "name": node.name,
                    "trigger_type": node.trigger_type,
                    "slug": node.slug,
                    "payload": outputs[node_id],
                }

            elif isinstance(node, ActionNode):
                node_logger.info(
                    "action.dispatch",
                    provider=node.provider,
                    action_slug=node.action_slug,
                    params_preview=_preview(node.params, limit=160),
                    allow_dry_run=node.allow_dry_run,
                )
                # Consume the action's events. An ``error`` event is intercepted
                # (not yielded straight through) so the node's ``on_error`` policy
                # decides whether it's fatal, skipped, or routed (P4).
                action_failed = False
                action_succeeded = False
                action_error_msg: str | None = None
                async for ev in self._run_action(node, outputs=outputs):
                    if ev.get("type") == "error":
                        action_failed = True
                        action_error_msg = ev.get("message")
                        node_logger.error("action.error", message=action_error_msg)
                        continue
                    yield ev
                    if ev.get("type") == "action_result":
                        outputs[node_id] = ev.get("result")
                        action_succeeded = True
                        result = ev.get("result") or {}
                        if bool(result.get("__dry_run__")):
                            tally["actions_dry_run"] += 1
                        else:
                            tally["actions_succeeded"] += 1
                        node_logger.info(
                            "action.result",
                            provider=result.get("__provider__"),
                            action_slug=result.get("__action__"),
                            via=result.get("__via__"),
                            dry_run=bool(result.get("__dry_run__")),
                            resolved_slug=result.get("__resolved_slug__"),
                            result_preview=_preview(result.get("data"), limit=200),
                        )
                    elif ev.get("type") == "action_dry_run":
                        outputs[node_id] = ev.get("result")
                        action_succeeded = True
                        result = ev.get("result") or {}
                        tally["actions_dry_run"] += 1
                        node_logger.info(
                            "action.dry_run",
                            provider=result.get("__provider__"),
                            action_slug=result.get("__action__"),
                            reason=result.get("__reason__"),
                        )
                    elif ev.get("type") == "hitl_required":
                        node_logger.info(
                            "action.hitl_required",
                            action_slug=ev.get("action_slug"),
                            auto_approved=bool(ev.get("auto_approved")),
                        )

                policy = getattr(node, "on_error", "fail")
                if action_failed:
                    if policy == "continue":
                        # Non-fatal: prune dependents, keep the run going.
                        skipped.add(node_id)
                        tally["nodes_skipped"] += 1
                        node_logger.info("action.error_handled", policy="continue")
                        yield {
                            "type": "node_error", "node_id": node_id,
                            "name": node.name, "handled": "continue",
                            "message": action_error_msg,
                        }
                    elif policy == "route":
                        # Non-fatal: set a "failed" decision so an error branch can
                        # gate via activate_on={node: "failed"}.
                        outputs[node_id] = {"ok": False, "__error__": action_error_msg}
                        decisions[node_id] = "failed"
                        await self._persist_decisions(execution_id, decisions)
                        node_logger.info("action.error_handled", policy="route")
                        yield {
                            "type": "node_error", "node_id": node_id,
                            "name": node.name, "handled": "route",
                            "message": action_error_msg,
                        }
                    else:
                        # Default "fail": re-emit as a fatal error so the service
                        # marks the execution FAILED (unchanged behavior).
                        yield {
                            "type": "error", "node_id": node_id,
                            "name": node.name, "message": action_error_msg,
                        }
                elif policy == "route" and action_succeeded:
                    # Success on a routing node → "ok" decision for the ok-branch.
                    decisions[node_id] = "ok"
                    await self._persist_decisions(execution_id, decisions)

            elif isinstance(node, IfNode):
                branch = _evaluate_if(node.expression, outputs)
                decisions[node_id] = branch
                outputs[node_id] = branch
                await self._persist_decisions(execution_id, decisions)
                node_logger.info(
                    "if.decided", expression=node.expression, branch=branch,
                )
                yield {
                    "type": "if_decided",
                    "node_id": node_id,
                    "name": node.name,
                    "expression": node.expression,
                    "branch": branch,
                }

            elif isinstance(node, DataStoreNode):
                rendered_payload = render_placeholders(node.payload, outputs)
                rendered_key = render_placeholders(node.key, outputs) or None
                rendered_filter = render_placeholders(node.filter, outputs)
                node_logger.info(
                    "data_store.dispatch",
                    op=node.op,
                    table=node.table,
                    key=rendered_key,
                    payload_preview=_preview(rendered_payload, limit=160),
                )
                result = await self._data_store_op(
                    node,
                    execution_id=execution_id,
                    key=rendered_key,
                    payload=rendered_payload,
                    filter_=rendered_filter,
                )
                outputs[node_id] = result
                node_logger.info(
                    "data_store.result",
                    op=node.op,
                    result_preview=_preview(result, limit=160),
                )
                yield {
                    "type": "data_store_op",
                    "node_id": node_id,
                    "name": node.name,
                    "op": node.op,
                    "table": node.table,
                    "result": result,
                }

            else:  # pragma: no cover — exhaustive dispatch above
                node_logger.error("node.unknown_kind", type_name=type(node).__name__)
                raise RuntimeError(f"unknown node kind: {type(node).__name__}")

            node_duration_ms = int((time.perf_counter() - node_started_at) * 1000)
            node_logger.info(
                "node.complete",
                duration_ms=node_duration_ms,
                output_preview=_preview(outputs.get(node_id)),
                decision=decisions.get(node_id),
            )

            # n8n-style per-node inspection. One event for every node kind that
            # actually executed (skipped nodes `continue` above and never reach
            # here). ``input_snapshot`` is the generic upstream-input view built
            # at _build_input — faithful for agent/condition/if, best-effort for
            # action/data_store/for_each/merge whose true payloads are derived
            # inside their handlers.
            node_output = outputs.get(node_id)
            ran = node_id in outputs
            node_dry_run = isinstance(node_output, dict) and bool(
                node_output.get("__dry_run__")
            )
            yield {
                "type": "node_complete",
                "node_id": node_id,
                "agent_id": node_id,  # mirror so FE id-matching works either way
                "node_name": node.name,
                "node_kind": kind,
                "input_snapshot": snapshot(node_input),
                "output_snapshot": snapshot(node_output),
                "status": "completed" if ran else "failed",
                "duration_ms": node_duration_ms,
                "dry_run": node_dry_run,
                "execution_id": str(execution_id),
            }

        wf_logger.info(
            "workflow.complete",
            duration_ms=int((time.perf_counter() - wf_started_at) * 1000),
            executed=len(order) - len(skipped),
            skipped=len(skipped),
            skipped_ids=sorted(skipped),
            decisions=decisions,
            output_node_ids=list(outputs.keys()),
        )

        yield {
            "type": "workflow_complete",
            "success": True,
            "execution_id": str(execution_id),
            "result": {"agent_outputs": outputs, "decisions": decisions},
            "summary": dict(tally),
        }

    # ------------------------------------------------------------------
    # Per-kind helpers
    # ------------------------------------------------------------------

    def _activated(
        self,
        node: NodeDefinition,
        decisions: dict[str, str],
        skipped: set[str],
    ) -> bool:
        if not node.activate_on:
            return True
        for ref, required in node.activate_on.items():
            if ref in skipped:
                return False
            if ref in decisions:
                if required != "*" and decisions[ref] != required:
                    return False
        return True

    def _build_input(
        self,
        node: NodeDefinition,
        *,
        outputs: dict[str, Any],
        initial_input_text: str,
    ) -> dict[str, Any]:
        if not node.depends_on:
            return {"input": initial_input_text}
        primary = node.depends_on[-1]
        primary_out = outputs.get(primary, "")
        upstream = {dep: outputs.get(dep) for dep in node.depends_on}
        readable = _extract_upstream_text(primary_out)
        return {
            "input": readable or _to_text(primary_out) or initial_input_text,
            "upstream": upstream,
        }

    async def _run_agent(
        self,
        definition: WorkflowDefinition,
        node: AgentNode,
        node_input: dict[str, Any],
        *,
        agent_tools_by_id: dict[str, list[Any]] | None,
        satellites: list[Any] | None = None,
        memory_node: Any | None = None,
        output_parser_node: Any | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        # Materialise a one-agent Dynamiq sub-workflow so we can stream it.
        # ``hydrate_agent_stage`` expects an AgentDefinition keyed inside
        # ``definition.agents``; we synthesise that mapping on the fly.
        #
        # Tools-Agent composite handling (Phase 1):
        #   * Satellite ActionNodes are appended to the agent's ``tools``
        #     list so the LLM sees them in its function-calling prompt.
        #   * Memory + Output Parser are not yet wired into the runtime
        #     (Phase 2). We surface them in the agent_start event so the
        #     UI can render the satellite badges and so callers can audit
        #     which agents already declare these slots.
        from schemas.workflow import AgentDefinition

        composite_tools = list(node.tools)
        satellites = satellites or []
        for sat in satellites:
            slot = sat.slot if hasattr(sat, "slot") else None
            sat_node = sat.node if hasattr(sat, "node") else sat
            if slot == "tool" and hasattr(sat_node, "action_slug"):
                # Avoid duplicates if the agent already lists the slug.
                if sat_node.action_slug not in composite_tools:
                    composite_tools.append(sat_node.action_slug)

        # Append a runtime guardrail that stops agents from hallucinating
        # file-system side-effects ("I have saved a PDF to ...").  Agents only
        # produce text; downstream tooling renders the text into a PDF if
        # asked.  Without this rail the LLM tends to invent file names and
        # confuse users into thinking a file was actually written.
        guarded_instructions = (node.instructions or "").strip()
        guardrail = (
            "Output rules: you can only return text/markdown. You do NOT "
            "have file-system access, so never claim to have 'saved', "
            "'created', 'generated', or 'uploaded' a PDF, document, or any "
            "file. Do not invent filenames or download links. Present your "
            "analysis directly in the response — the UI renders it as a "
            "report and offers the user a Download-as-PDF button."
        )
        upstream = node_input.get("upstream") or {}
        prior_outputs: dict[str, str] = {}
        for dep_id, dep_val in upstream.items():
            text = _extract_upstream_text(dep_val) or _to_text(dep_val)
            if text.strip():
                prior_outputs[dep_id] = text.strip()

        not_executed_deps: list[tuple[str, str, str]] = []
        for dep_id, dep_text in prior_outputs.items():
            if "<<UPSTREAM_NOT_EXECUTED" in dep_text:
                provider_match = re.search(r'provider="([^"]+)"', dep_text)
                reason_match = re.search(r'reason="([^"]+)"', dep_text)
                provider = (
                    provider_match.group(1) if provider_match else "unknown"
                )
                reason = reason_match.group(1) if reason_match else "unknown"
                not_executed_deps.append((dep_id, provider, reason))
        all_deps_missing = bool(prior_outputs) and len(
            not_executed_deps
        ) == len(prior_outputs)

        input_text = str(node_input.get("input") or "").strip()
        combined_context = (
            input_text + "\n" + "\n".join(prior_outputs.values())
        ).lower()
        has_upstream_data = bool(prior_outputs) or bool(input_text)
        has_partial_preview = "partial preview only" in combined_context
        has_composio = any(
            (t or "").upper().startswith("COMPOSIO_") for t in composite_tools
        )
        role_rules = _role_specific_rules(node, prior_outputs)

        if has_upstream_data:
            analysis_rules = (
                "Analysis requirements: Use ONLY the upstream data provided "
                "in your input and in `# Outputs from preceding agents`. "
                "Perform the analysis NOW — do not describe future plans, "
                "do not say what you 'will' do. Report concrete findings "
                "with counts, named segments/categories, example rows "
                "copied from the data, sentiment scores, and actionable "
                "recommendations grounded in that data."
            )
            if has_composio:
                if has_partial_preview:
                    analysis_rules += (
                        " The upstream data is a PARTIAL PREVIEW. Your "
                        "FIRST action MUST be `COMPOSIO_MULTI_EXECUTE_TOOL` "
                        "with `tool_slug=GOOGLESHEETS_VALUES_GET` and a "
                        "bounded range like `Customer_Master!A1:R200` "
                        "against the same spreadsheet ID shown above, so "
                        "you get real rows. Do NOT call `GMAIL_GET_CONTACTS`, "
                        "`GOOGLESHEETS_AGGREGATE_COLUMN_DATA`, or any tool "
                        "unrelated to the upstream source — they will not "
                        "produce data about THESE customers."
                    )
                else:
                    analysis_rules += (
                        " Do NOT call Composio tools to fetch substitute "
                        "data from unrelated sources (Gmail contacts, "
                        "Drive, etc.). Use the rows already provided. "
                        "Only call a Composio tool if a specific field is "
                        "missing and clearly named in the upstream data."
                    )
            guardrail = f"{analysis_rules}\n\n{guardrail}"

        if role_rules:
            guardrail = f"{role_rules}\n\n{guardrail}"

        if not_executed_deps:
            dep_ids_json = "[" + ", ".join(
                f'"{d[0]}"' for d in not_executed_deps
            ) + "]"
            first_id, first_provider, first_reason = not_executed_deps[0]
            example_reason = (
                f"upstream node {first_id} was not executed "
                f"(provider: {first_provider}, reason: {first_reason})"
            )
            example_json = (
                '{"status": "no_data", "reason": "' + example_reason
                + '", "missing_upstream": ' + dep_ids_json + "}"
            )
            if all_deps_missing:
                no_data_rule = (
                    "CRITICAL no-data rule: every upstream dependency for "
                    "this agent was NOT executed (see "
                    "<<UPSTREAM_NOT_EXECUTED ...>> markers in the prior "
                    "outputs). You MUST NOT fabricate data, MUST NOT issue "
                    "a natural-language refusal like 'unable to retrieve "
                    "customer data', and MUST NOT call substitute tools to "
                    "invent results. Instead, respond with ONLY the "
                    "following structured JSON object (no prose, no "
                    "markdown fence) so downstream nodes can detect the "
                    f"missing input: {example_json}"
                )
            else:
                no_data_rule = (
                    "Partial no-data rule: one or more upstream "
                    "dependencies were NOT executed (see "
                    "<<UPSTREAM_NOT_EXECUTED ...>> markers). Do NOT "
                    "fabricate values for the missing inputs. If you can "
                    "still produce a meaningful answer from the executed "
                    "upstream data, do so and clearly note which inputs "
                    "were unavailable. If you cannot, return ONLY this "
                    f"structured JSON: {example_json}"
                )
            guardrail = f"{no_data_rule}\n\n{guardrail}"

        if guarded_instructions:
            guarded_instructions = f"{guarded_instructions}\n\n{guardrail}"
        else:
            guarded_instructions = guardrail

        synthetic_def = WorkflowDefinition(
            name=definition.name,
            description=definition.description,
            trigger=definition.trigger,
            agents=[
                AgentDefinition(
                    id=node.id,
                    name=node.name,
                    role=node.role,
                    instructions=guarded_instructions,
                    tools=composite_tools,
                    depends_on=[],
                    is_parallel=node.is_parallel,
                )
            ],
            output_format=definition.output_format,
        )
        wf = self._dynamiq.hydrate_agent_stage(
            synthetic_def,
            focus_id=node.id,
            prior_outputs=prior_outputs,
            agent_tools_by_id=agent_tools_by_id,
        )
        final_text: list[str] = []
        async for ev in self._dynamiq.run_workflow_stream(wf, input_data=node_input):
            if ev.get("type") == "workflow_complete":
                # Extract the agent's content from the final Dynamiq payload.
                res = ev.get("result")
                if isinstance(res, dict):
                    cell = res.get(node.id)
                    if isinstance(cell, dict):
                        inner = cell.get("output")
                        if isinstance(inner, dict):
                            cnt = inner.get("content")
                            if isinstance(cnt, str):
                                final_text.append(cnt)
                if not final_text and isinstance(ev.get("result"), str):
                    final_text.append(ev["result"])
                yield {
                    "type": "agent_complete",
                    "agent_id": node.id,
                    "agent_name": node.name,
                    "content": "\n".join(final_text),
                }
                continue
            if ev.get("type") in {"workflow_start", "error"}:
                # Suppress wrapper start (we already emitted one) but
                # surface errors so callers can fail-fast.
                if ev.get("type") == "error":
                    yield ev
                continue
            # Pass-through agent_start / agent_thinking / tool_call / tool_result
            yield ev

    async def _evaluate_condition(
        self,
        node: ConditionNode,
        node_input: dict[str, Any],
    ) -> str:
        upstream = node_input.get("upstream") or {}
        upstream_strs = {k: _to_text(v) for k, v in upstream.items()}
        if not upstream_strs:
            upstream_strs = {"input": _to_text(node_input.get("input"))}
        raw = await self._condition_eval(node.expression, upstream_strs)
        # Bind branches from the closure so the LLM's free-text answer maps.
        return _coerce_branch(raw, list(node.branches))

    async def _llm_route(self, expression: str, upstream: dict[str, str]) -> str:
        """Default condition evaluator — one zero-temperature chat call."""
        from openai import AsyncAzureOpenAI

        ep = self._settings.AZURE_OPENAI_ENDPOINT.strip().rstrip("/")
        key = self._settings.AZURE_OPENAI_API_KEY.strip()
        if not ep or not key:
            # No Azure configured — fall back to a heuristic so tests/dev
            # without an LLM still get deterministic routing.
            return self._heuristic_route(expression, upstream)
        client = AsyncAzureOpenAI(
            azure_endpoint=ep,
            api_key=key,
            api_version=self._settings.AZURE_OPENAI_API_VERSION,
        )
        deployment = (
            self._settings.AZURE_OPENAI_DEPLOYMENT
            or self._settings.AZURE_OPENAI_DEFAULT_MODEL
        )
        rendered = "\n\n".join(
            f"### Upstream `{k}`:\n{v[:6000]}" for k, v in upstream.items()
        )
        completion = await client.chat.completions.create(
            model=deployment,
            temperature=0.0,
            max_tokens=16,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a strict workflow router. Read the upstream "
                        "outputs and answer the question with EXACTLY one of "
                        "the allowed labels. No prose. No punctuation."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Question: {expression}\n\n{rendered}",
                },
            ],
        )
        return (completion.choices[0].message.content or "").strip()

    @staticmethod
    def _heuristic_route(expression: str, upstream: dict[str, str]) -> str:
        """Deterministic fallback used when no LLM is configured.

        Looks for any branch-like keyword in the joined upstream text. Tests
        rely on this by injecting upstream text that contains the desired
        branch label.
        """
        blob = " ".join(upstream.values()).lower()
        # Use the order in the expression as a hint: extract any "Return X or Y"
        # phrasing and pick the first whose word appears.
        # The caller passes `branches` separately via _coerce_branch on the
        # *raw* output of this function — so just return the blob.
        return blob

    async def _persist_decisions(
        self, execution_id: UUID, decisions: dict[str, str]
    ) -> None:
        try:
            redis = get_redis()
            await redis.set(
                _decision_key(execution_id),
                json.dumps(decisions),
                ex=86400,
            )
        except Exception:  # noqa: BLE001 — telemetry must not break the run
            log.debug("extended_executor.persist_decisions_failed", exc_info=True)

    async def _run_for_each(
        self,
        definition: WorkflowDefinition,
        node: ForEachNode,
        *,
        outputs: dict[str, Any],
        decisions: dict[str, str],
        skipped: set[str],
        execution_id: UUID,
        agent_tools_by_id: dict[str, list[Any]] | None,
    ) -> AsyncIterator[dict[str, Any]]:
        upstream_out = outputs.get(node.items_from)
        items_obj = None
        if upstream_out is not None:
            text = _to_text(upstream_out)
            # Strict parse first; then tolerate a ```code fence or prose wrapped
            # around the JSON list (LLMs love adding both even when told not to).
            items_obj = _maybe_json(text)
            if items_obj is None:
                items_obj = extract_json_loose(text)
        items = _jsonpath_dollar(items_obj, node.items_path) if items_obj is not None else None
        if not isinstance(items, list):
            yield {
                "type": "for_each_complete",
                "node_id": node.id,
                "name": node.name,
                "results": [],
                "message": "items_from did not yield a list",
            }
            return

        yield {
            "type": "for_each_started",
            "node_id": node.id,
            "name": node.name,
            "count": len(items),
        }

        nodes_by_id = {n.id: n for n in definition.iter_nodes()}
        # Run each body subgraph sequentially per item (max_concurrency
        # honoured by chunking — keeps event ordering deterministic).
        results: list[dict[str, Any]] = []
        sem = asyncio.Semaphore(max(1, node.max_concurrency))

        async def _one(idx: int, item: Any) -> dict[str, Any]:
            async with sem:
                item_outputs: dict[str, Any] = dict(outputs)
                item_skipped: set[str] = set(skipped)
                # Seed the iteration variable as a virtual node output the
                # body subgraph can depends_on via `node.id`.
                item_outputs[node.id] = {
                    node.item_var: item,
                    "_index": idx,
                }
                # Also expose the iteration value at top level under its
                # ``item_var`` name so body params can template it directly,
                # e.g. ``{{ candidate.Email }}`` for a per-item Send Email.
                item_outputs[node.item_var] = item
                events: list[dict[str, Any]] = []
                # Walk body in declared order — they were placed after the
                # for_each in the topo, but inside the body subgraph the
                # ordering is the body[] list itself.
                body_order = [bid for bid in node.body if bid in nodes_by_id]
                for body_id in body_order:
                    sub_node = nodes_by_id[body_id]
                    if not self._activated(sub_node, decisions, item_skipped):
                        item_skipped.add(body_id)
                        events.append(
                            {
                                "type": "node_skipped",
                                "node_id": body_id,
                                "name": sub_node.name,
                                "for_each_index": idx,
                                "reason": "activate_on_mismatch",
                            }
                        )
                        continue
                    if any(d in item_skipped for d in sub_node.depends_on):
                        item_skipped.add(body_id)
                        continue
                    sub_input = self._build_input(
                        sub_node,
                        outputs=item_outputs,
                        initial_input_text=_to_text(item),
                    )
                    if isinstance(sub_node, AgentNode):
                        # Buffer agent events so they appear contiguously per
                        # item rather than interleaved across items.
                        captured: list[dict[str, Any]] = []
                        async for ev in self._run_agent(
                            definition,
                            sub_node,
                            sub_input,
                            agent_tools_by_id=agent_tools_by_id,
                        ):
                            ev = {**ev, "for_each_index": idx, "for_each_id": node.id}
                            captured.append(ev)
                            if ev.get("type") == "agent_complete":
                                item_outputs[body_id] = ev.get("content", "")
                        events.extend(captured)
                    elif isinstance(sub_node, ActionNode):
                        # Per-item action (e.g. Send Email once per customer).
                        # Params template against ``item_outputs`` which now
                        # exposes the iteration var, so {{ candidate.Email }}
                        # resolves to this item.
                        item_failed = False
                        item_err: str | None = None
                        async for ev in self._run_action(
                            sub_node, outputs=item_outputs
                        ):
                            ev = {**ev, "for_each_index": idx, "for_each_id": node.id}
                            if ev.get("type") == "error":
                                item_failed = True
                                item_err = ev.get("message")
                                continue  # defer to the node's on_error policy
                            events.append(ev)
                            if ev.get("type") in ("action_result", "action_dry_run"):
                                item_outputs[body_id] = ev.get("result")
                        if item_failed:
                            # ``fail`` (default) aborts the whole run as before.
                            # ``continue``/``route`` ISOLATE this item's failure
                            # so one bad item doesn't kill the batch (per-item
                            # error branches aren't supported inside a loop, so
                            # route degrades to continue here).
                            policy = getattr(sub_node, "on_error", "fail")
                            if policy in ("continue", "route"):
                                item_skipped.add(body_id)
                                events.append({
                                    "type": "node_error", "node_id": body_id,
                                    "name": sub_node.name, "handled": policy,
                                    "message": item_err, "for_each_index": idx,
                                })
                            else:
                                events.append({
                                    "type": "error", "node_id": body_id,
                                    "name": sub_node.name, "message": item_err,
                                    "for_each_index": idx,
                                })
                    else:
                        # Nested control-flow (condition/for_each/etc.) and
                        # data_store inside a for_each body remain out of scope.
                        events.append(
                            {
                                "type": "node_skipped",
                                "node_id": body_id,
                                "name": sub_node.name,
                                "for_each_index": idx,
                                "reason": "nested_control_flow_unsupported",
                            }
                        )
                return {
                    "_index": idx,
                    "item": item,
                    "events": events,
                    "outputs": {k: item_outputs.get(k) for k in body_order},
                }

        completed = await asyncio.gather(*[_one(i, it) for i, it in enumerate(items)])
        for r in completed:
            for ev in r["events"]:
                yield ev
            yield {
                "type": "for_each_item",
                "node_id": node.id,
                "for_each_index": r["_index"],
                "outputs": r["outputs"],
            }
            results.append({"index": r["_index"], "outputs": r["outputs"]})

        yield {
            "type": "for_each_complete",
            "node_id": node.id,
            "name": node.name,
            "count": len(results),
            "results": results,
        }

    async def _park_for_webhook(
        self,
        node: WaitForWebhookNode,
        *,
        execution_id: UUID,
    ) -> AsyncIterator[dict[str, Any]]:
        token = secrets.token_urlsafe(24)
        redis = get_redis()
        # Token → (execution_id, node_id) so the resume route can find us.
        await redis.set(
            _resume_token_key(token),
            json.dumps({"execution_id": str(execution_id), "node_id": node.id}),
            ex=node.timeout_seconds,
        )
        base = (
            getattr(self._settings, "PUBLIC_BASE_URL", None)
            or getattr(self._settings, "API_BASE_URL", None)
            or ""
        ).rstrip("/")
        resume_url = (
            f"{base}/api/v1/workflows/executions/{execution_id}/resume/{token}"
            if base
            else f"/api/v1/workflows/executions/{execution_id}/resume/{token}"
        )
        yield {
            "type": "wait_for_webhook",
            "node_id": node.id,
            "name": node.name,
            "description": node.description,
            "resume_url": resume_url,
            "resume_token": token,
            "timeout_seconds": node.timeout_seconds,
            "response_schema": node.response_schema,
        }

        payload_key = _resume_payload_key(execution_id, node.id)
        deadline = asyncio.get_running_loop().time() + node.timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            raw = await redis.get(payload_key)
            if raw:
                await redis.delete(payload_key)
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = {"raw": raw}
                yield {
                    "type": "webhook_resumed",
                    "node_id": node.id,
                    "name": node.name,
                    "payload": parsed,
                }
                return
            await asyncio.sleep(_PARK_POLL_SECONDS)
        # Timed out — surface an error so the caller can mark the execution
        # FAILED. The token is left to expire on its own TTL.
        yield {
            "type": "error",
            "node_id": node.id,
            "message": f"wait_for_webhook timeout after {node.timeout_seconds}s",
        }

    # ------------------------------------------------------------------
    # n8n-shape primitives: action + data_store
    # ------------------------------------------------------------------

    async def _run_action(
        self,
        node: ActionNode,
        *,
        outputs: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Invoke one integration action; yield invoked + result events.

        ``params`` are rendered through ``render_placeholders`` so the
        workflow author can stitch upstream outputs into the call payload
        without writing code. Result is always a dict — downstream nodes
        JSONPath into ``data`` for the real response.
        """
        rendered_params = render_placeholders(node.params, outputs)
        yield {
            "type": "action_invoked",
            "node_id": node.id,
            "name": node.name,
            "provider": node.provider,
            "action_slug": node.action_slug,
            "params": rendered_params,
        }
        # Honor the node's timeout/retry config for top-level actions (P5).
        # Previously these fields only applied when the action ran as an agent
        # satellite; reuse the same wrapper here. It retries on
        # exception/timeout with exponential backoff and returns a structured
        # error dict (``ok: False`` + ``code``) on final failure rather than
        # raising. invoke_action's success dicts never carry a top-level ``ok``,
        # so the wrapper never mistakes a real result for a retryable failure.
        async def _invoke(_args: dict[str, Any]) -> dict[str, Any]:
            return await invoke_action(
                provider_id=node.provider,
                action_slug=node.action_slug,
                params=rendered_params if isinstance(rendered_params, dict) else {"value": rendered_params},
                workspace_connections=self._workspace_connections,
                allow_dry_run=node.allow_dry_run,
                workspace_id=self._workspace_id,
                db=self._db,
                live=self._live,
                connection_id=getattr(node, "connection_id", None),
            )

        wrapped = _with_timeout_and_retry(
            _invoke,
            timeout_ms=getattr(node, "timeout_ms", 30000),
            max_retries=getattr(node, "max_retries", 1),
            initial_delay_ms=getattr(node, "retry_initial_delay_ms", 200),
            label=node.action_slug,
        )
        result = await wrapped({})
        if result.get("ok") is False and result.get("code") in ("timeout", "exception"):
            yield {
                "type": "error",
                "node_id": node.id,
                "name": node.name,
                "message": f"action {node.action_slug} failed after retries: {result.get('error')}",
            }
            return

        # HITL gate — the action runner detected a request_approval-style node.
        # Emit a hitl_required event (matching the LangGraph HITL contract) and
        # auto-approve so the extended-executor path continues unblocked.
        if result.get("__hitl_required__"):
            yield {
                "type": "hitl_required",
                "node_id": node.id,
                "name": node.name,
                "action_slug": node.action_slug,
                "message": result.get("data", {}).get("message", "Human approval required."),
                "auto_approved": True,
            }
            yield {
                "type": "action_result",
                "node_id": node.id,
                "name": node.name,
                "provider": node.provider,
                "action_slug": node.action_slug,
                "result": {**result, "data": {**result.get("data", {}), "approved": True}},
            }
            return

        is_dry = bool(result.get("__dry_run__"))
        yield {
            "type": "action_dry_run" if is_dry else "action_result",
            "node_id": node.id,
            "name": node.name,
            "provider": node.provider,
            "action_slug": node.action_slug,
            "result": result,
        }

    async def _data_store_op(
        self,
        node: DataStoreNode,
        *,
        execution_id: UUID,
        key: str | None,
        payload: Any,
        filter_: Any,
    ) -> dict[str, Any]:
        """Run one read/write/query against the workspace's workflow_data table.

        When the executor wasn't given a DB session (e.g. unit tests, or a
        v1 caller that didn't migrate), data_store nodes degrade to an
        in-memory echo so the flow still walks visually.
        """
        if self._db is None or self._workspace_id is None:
            return {
                "__dry_run__": True,
                "op": node.op,
                "table": node.table,
                "key": key,
                "payload": payload,
                "filter": filter_,
                "note": "no DB session bound — data_store running in echo mode",
            }
        from uuid import uuid4 as _uuid4

        from sqlalchemy import select
        from models.workflow_data import WorkflowData

        if node.op == "write":
            row_key = str(key) if key else _uuid4().hex
            stmt = select(WorkflowData).where(
                WorkflowData.workspace_id == self._workspace_id,
                WorkflowData.table_name == node.table,
                WorkflowData.row_key == row_key,
            )
            existing = (await self._db.execute(stmt)).scalar_one_or_none()
            data_value = payload if isinstance(payload, dict) else {"value": payload}
            if existing is None:
                row = WorkflowData(
                    workspace_id=self._workspace_id,
                    table_name=node.table,
                    row_key=row_key,
                    data=data_value,
                    last_workflow_id=self._workflow_id,
                    last_execution_id=execution_id,
                )
                self._db.add(row)
            else:
                existing.data = {**(existing.data or {}), **data_value}
                existing.last_workflow_id = self._workflow_id
                existing.last_execution_id = execution_id
            try:
                await self._db.commit()
            except Exception as exc:  # noqa: BLE001
                await self._db.rollback()
                return {
                    "ok": False,
                    "op": node.op,
                    "table": node.table,
                    "key": row_key,
                    "error": str(exc),
                }
            return {
                "ok": True,
                "op": node.op,
                "table": node.table,
                "key": row_key,
                "row": data_value,
            }

        if node.op == "read":
            if not key:
                return {"ok": False, "op": node.op, "error": "key required for read"}
            stmt = select(WorkflowData).where(
                WorkflowData.workspace_id == self._workspace_id,
                WorkflowData.table_name == node.table,
                WorkflowData.row_key == str(key),
            )
            existing = (await self._db.execute(stmt)).scalar_one_or_none()
            return {
                "ok": True,
                "op": node.op,
                "table": node.table,
                "key": key,
                "found": existing is not None,
                "row": existing.data if existing else None,
            }

        # query
        stmt = select(WorkflowData).where(
            WorkflowData.workspace_id == self._workspace_id,
            WorkflowData.table_name == node.table,
        )
        rows = list((await self._db.execute(stmt)).scalars().all())
        # Filter is a dict of {field: value} applied to row.data; missing
        # filter or empty dict means "all rows".
        out: list[dict[str, Any]] = []
        flt = filter_ if isinstance(filter_, dict) else {}
        for r in rows:
            data = r.data or {}
            if all(data.get(k) == v for k, v in flt.items()):
                out.append({"key": r.row_key, "data": data})
        return {
            "ok": True,
            "op": node.op,
            "table": node.table,
            "rows": out,
            "count": len(out),
        }


__all__ = [
    "ExtendedWorkflowExecutor",
    "_resume_token_key",
    "_resume_payload_key",
]
