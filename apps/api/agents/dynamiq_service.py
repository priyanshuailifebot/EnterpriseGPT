"""Hydrate ``WorkflowDefinition`` graphs into Dynamiq ``Workflow`` + stream execution."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from dynamiq import Workflow
from dynamiq.callbacks.streaming import AsyncStreamingIteratorCallbackHandler
from dynamiq.connections.connections import Anthropic as AnthropicConn
from dynamiq.connections.connections import AzureAI as AzureAIConn
from dynamiq.flows import Flow
from dynamiq.nodes.agents import Agent
from dynamiq.nodes.types import Behavior
from dynamiq.nodes.llms import Anthropic, AzureAI as AzureAILLM
from dynamiq.nodes.node import InputTransformer, NodeDependency
from dynamiq.runnables import RunnableConfig, RunnableResult, RunnableStatus
from dynamiq.types.streaming import StreamingEventMessage
from dynamiq.types.streaming import StreamingMode
from dynamiq.types.streaming import StreamingConfig
from dynamiq.utils import serialize_files_in_value

from core.config import Settings
from core.tracing import observe
from schemas.workflow import AgentDefinition
from schemas.workflow import WorkflowDefinition
from schemas.workflow import workflow_execution_order

log = logging.getLogger(__name__)

_MCP_META_TOOL_DESCRIPTIONS: dict[str, str] = {
    "COMPOSIO_SEARCH_TOOLS": (
        "Search Composio for integration tools matching a use case. "
        "Arguments: {queries: [{use_case: string, known_fields?: object}]}. "
        "Returns primary_tool_slugs you can execute next."
    ),
    "COMPOSIO_MULTI_EXECUTE_TOOL": (
        "Execute one or more Composio integration tools. "
        "Arguments: {tools: [{tool_slug: string, arguments: object, account?: string}]}. "
        "Use after COMPOSIO_SEARCH_TOOLS to run the discovered slug."
    ),
    "COMPOSIO_MANAGE_CONNECTIONS": (
        "Manage Composio OAuth connections (list, create, remove). "
        "Use only when a tool call fails due to a missing connection."
    ),
}


def _primary_dep(agent_def, topo_index: dict[str, int]) -> str | None:
    if not agent_def.depends_on:
        return None
    return max(agent_def.depends_on, key=lambda d: topo_index.get(d, -1))


def _combined_role(
    agent_def: AgentDefinition,
    definition: WorkflowDefinition,
    *,
    prior_outputs: dict[str, str] | None = None,
) -> str:
    parts = [f"# Workflow: {definition.name}"]
    if definition.description.strip():
        parts.append(f"# Context:\n{definition.description.strip()}")
    if definition.trigger.strip():
        parts.append(f"# Trigger:\n{definition.trigger.strip()}")
    if agent_def.role.strip():
        parts.append(f"# Persona / role:\n{agent_def.role.strip()}")
    if agent_def.instructions.strip():
        parts.append(f"# Operational instructions:\n{agent_def.instructions.strip()}")
    if definition.output_format.strip():
        parts.append(f"# Output format:\nProduce output as: {definition.output_format.strip()}.")
    if agent_def.tools:
        parts.append("# Composio tools available to this agent:\n" + ", ".join(agent_def.tools))
        if any(t.upper().startswith("COMPOSIO_") for t in agent_def.tools):
            parts.append(
                "# Composio MCP usage:\n"
                "1. Call COMPOSIO_SEARCH_TOOLS with a clear use_case to discover tool slugs.\n"
                "2. Call COMPOSIO_MULTI_EXECUTE_TOOL with the returned slug and arguments.\n"
                "Always use these tools when you need live data from connected integrations."
            )
    if prior_outputs:
        rendered = "\n".join(f"- `{aid}`:\n{text}" for aid, text in sorted(prior_outputs.items()))
        parts.append(f"# Outputs from preceding agents:\n{rendered}")
    return "\n\n".join(parts)


def _map_stream_chunk(msg: StreamingEventMessage) -> dict[str, Any] | None:
    src = msg.source
    agent_id = src.id if src else None
    agent_name = src.name if src else None
    grp = getattr(src, "group", None) if src else None
    dtype = getattr(src, "type", None) if src else None
    payload: Any = msg.data

    txt: str | None = None
    if isinstance(payload, str):
        txt = payload
    elif isinstance(payload, dict):
        for key in ("content", "message", "text", "thought"):
            chunk = payload.get(key)
            if isinstance(chunk, str) and chunk:
                txt = chunk
                break
        else:
            try:
                txt = json.dumps(serialize_files_in_value(payload))
            except (TypeError, ValueError):
                txt = repr(payload)
    elif payload is not None:
        txt = str(payload)

    if grp == "tools" or dtype == "tool":
        name = getattr(src, "name", None)
        hint = getattr(msg, "event", "") or ""
        if "tool" in hint.lower() or grp == "tools":
            if "result" in hint.lower():
                event_type = "tool_result"
            else:
                event_type = "tool_call"
            return {
                "type": event_type,
                "agent_id": agent_id,
                "agent_name": agent_name,
                "tool_name": name or (payload.get("name") if isinstance(payload, dict) else None),
                "content": txt,
                "data": payload if isinstance(payload, dict) else None,
            }
    if grp == "agents" or dtype == "agent":
        lower = txt.lower() if txt else ""
        evt = (
            "agent_thinking"
            if txt and ("thought" in lower or lower.startswith("{") or "action" in lower)
            else "agent_thinking"
        )
        return {
            "type": evt,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "content": txt,
            "raw_event": msg.event,
            "group": grp,
        }
    if grp == "llms":
        return {
            "type": "agent_thinking",
            "agent_id": agent_id,
            "agent_name": agent_name or "LLM",
            "content": txt,
            "raw_event": msg.event,
        }

    # Drop workflow/flow-level wrapper events — they re-broadcast the same
    # output as a JSON dict and would surface as a duplicate "phantom" agent
    # card in the UI (different source id than the real Agent node).
    return None


class DynamiqService:
    """Builds Dynamiq Agents/Flows + streams normalized SSE payloads."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _resolve_llm(self) -> Anthropic | AzureAILLM:
        ep = self._settings.AZURE_OPENAI_ENDPOINT.strip().rstrip("/")
        key = self._settings.AZURE_OPENAI_API_KEY.strip()
        if ep and key:
            conn = AzureAIConn(
                api_key=key,
                url=ep,
                api_version=self._settings.AZURE_OPENAI_API_VERSION,
            )
            deployment = (
                self._settings.AZURE_OPENAI_DEPLOYMENT
                or self._settings.AZURE_OPENAI_DEFAULT_MODEL
            )
            return AzureAILLM(
                connection=conn,
                model=deployment,
                temperature=0.0,
            )
        anth = self._settings.ANTHROPIC_API_KEY.strip()
        if anth:
            return Anthropic(
                connection=AnthropicConn(api_key=anth),
                model="claude-3-5-sonnet-20241022",
                temperature=0.0,
            )
        raise RuntimeError(
            "No LLM configured — set AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY "
            "or ANTHROPIC_API_KEY."
        )

    def build_agent_composio_tools(
        self,
        definition: WorkflowDefinition,
        *,
        allowed_slugs: set[str],
        invoke: Callable[[str, dict[str, Any]], Any],
    ) -> dict[str, list[Any]]:
        """Map agent ids → Dynamiq tool nodes that call Composio actions."""
        from agents.composio_bridge_tool import ComposioBridgeTool

        mapping: dict[str, list[Any]] = {}
        for agent_id, tool_slugs in definition.agent_tool_bindings().items():
            nodes: list[Any] = []
            for tool_slug in tool_slugs:
                if tool_slug not in allowed_slugs:
                    continue

                def _invoke(params: dict[str, Any], *, slug: str = tool_slug) -> Any:
                    return invoke(slug, params)

                nodes.append(
                    ComposioBridgeTool(
                        action_slug=tool_slug,
                        description=f"Composio toolkit action `{tool_slug}`.",
                        invoke_fn=_invoke,
                    )
                )
            if nodes:
                mapping[agent_id] = nodes
        return mapping

    def build_agent_mcp_meta_tools(
        self,
        definition: WorkflowDefinition,
        *,
        registry: Any,
        execution_id: Any | None = None,
        tool_run_buffer: Any | None = None,
    ) -> dict[str, list[Any]]:
        """Map agent ids → Dynamiq tools for Composio MCP meta-tools (``COMPOSIO_*``).

        These slugs are advertised by the hosted Composio MCP server but are
        not present in the legacy ``ToolRegistry`` action catalog, so agents
        that declare them would otherwise run tool-less.
        """
        from agents.composio_bridge_tool import ComposioBridgeTool
        from egpt_mcp.mcp_tool_registry import run_async

        mapping: dict[str, list[Any]] = {}
        for agent_id, tool_slugs in definition.agent_tool_bindings().items():
            nodes: list[Any] = []
            for tool_slug in tool_slugs:
                slug = (tool_slug or "").strip().upper()
                if not slug.startswith("COMPOSIO_"):
                    continue

                def _invoke(params: dict[str, Any], *, name: str = slug) -> dict[str, Any]:
                    try:
                        result = run_async(
                            registry.call_tool(
                                db=None,
                                tool_name=name,
                                arguments=params or {},
                                execution_id=execution_id,
                                tool_run_buffer=tool_run_buffer,
                            )
                        )
                        return {"ok": True, "data": result}
                    except Exception as exc:  # noqa: BLE001 — surface to the LLM
                        return {"ok": False, "error": str(exc), "data": {}}

                desc = _MCP_META_TOOL_DESCRIPTIONS.get(
                    slug,
                    f"Composio MCP meta-tool `{slug}`.",
                )
                nodes.append(
                    ComposioBridgeTool(
                        action_slug=slug,
                        description=desc,
                        invoke_fn=_invoke,
                    )
                )
            if nodes:
                mapping[agent_id] = nodes
        return mapping

    def hydrate_workflow(
        self,
        definition: WorkflowDefinition,
        *,
        agent_tools_by_id: dict[str, list[Any]] | None = None,
    ) -> Workflow:
        llm = self._resolve_llm()
        by_id = {a.id: a for a in definition.agents}
        order = workflow_execution_order(definition)
        topo_rank = {aid: idx for idx, aid in enumerate(order)}
        built: dict[str, Agent] = {}

        for aid in order:
            ag_def = by_id[aid]
            dep_nodes = [built[d] for d in sorted(ag_def.depends_on)]
            deps_kw: dict[str, Any] = {}
            if dep_nodes:
                deps_kw["depends"] = [NodeDependency(n) for n in dep_nodes]
            prime = _primary_dep(ag_def, topo_rank)
            if not ag_def.depends_on:
                itrans = InputTransformer(selector={"input": "$.input"})
            elif prime:
                # JSONPath bracket notation tolerates any agent id (hyphens,
                # dots, etc.) — Dynamiq's InputTransformer routes this through
                # jsonpath-ng, which rejects the `${[id]}` template syntax we
                # had before (Lark raises "Unexpected character: {").
                safe_prime = prime.replace("'", "\\'")
                selector = {"input": f"$['{safe_prime}'].output.content"}
                itrans = InputTransformer(selector=selector)
            else:
                itrans = InputTransformer(selector={"input": "$.input"})

            bridge_tools = list((agent_tools_by_id or {}).get(aid, []))

            node = Agent(
                id=ag_def.id,
                name=ag_def.name or ag_def.id,
                llm=llm,
                role=_combined_role(ag_def, definition),
                tools=bridge_tools,
                # A tool-less agent has nothing to act on — running the ReAct
                # loop just makes it emit raw text/JSON the Action parser
                # rejects ("No valid Action and Action Input pairs"), looping to
                # the limit and returning empty. One pass returns the answer.
                max_loops=12 if bridge_tools else 2,
                input_transformer=itrans,
                streaming=StreamingConfig(enabled=True, mode=StreamingMode.ALL),
                **deps_kw,
            )
            built[aid] = node

        flow = Flow(nodes=list(built.values()))
        return Workflow(id=definition.name.lower().replace(" ", "_")[:64], flow=flow, name=definition.name)

    def hydrate_agent_stage(
        self,
        definition: WorkflowDefinition,
        *,
        focus_id: str,
        prior_outputs: dict[str, str],
        agent_tools_by_id: dict[str, list[Any]] | None = None,
    ) -> Workflow:
        """Single-agent Dynamiq workflow seeded with upstream outputs in the role block."""
        llm = self._resolve_llm()
        by_id = {a.id: a for a in definition.agents}
        if focus_id not in by_id:
            raise KeyError(f"unknown agent id {focus_id!r}")
        ag_def = by_id[focus_id]
        bridge_tools = list((agent_tools_by_id or {}).get(focus_id, []))
        node = Agent(
            id=ag_def.id,
            name=ag_def.name or ag_def.id,
            llm=llm,
            role=_combined_role(ag_def, definition, prior_outputs=prior_outputs or None),
            tools=bridge_tools,
            # Tool-less agents do a single completion instead of a ReAct loop —
            # the loop has no actions to take and otherwise burns its budget
            # rejecting the agent's own text, returning empty. See hydrate_workflow.
            max_loops=12 if bridge_tools else 2,
            # A tool-less agent can't loop fewer than 2 times (ge=2) and its
            # plain/JSON answer isn't a parseable ReAct action, so the default
            # RAISE behaviour loop-fails and returns empty. RETURN makes it hand
            # back the agent's actual completion — the single-shot answer we want.
            behaviour_on_max_loops=(Behavior.RAISE if bridge_tools else Behavior.RETURN),
            input_transformer=InputTransformer(selector={"input": "$.input"}),
            streaming=StreamingConfig(enabled=True, mode=StreamingMode.ALL),
        )
        flow = Flow(nodes=[node])
        return Workflow(id=f"{focus_id}_stage", flow=flow, name=ag_def.name)

    @observe()
    async def run_workflow_stream(
        self,
        workflow: Workflow,
        *,
        input_data: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Execute ``workflow`` with streaming callbacks normalized to SSE dict payloads."""
        loop = asyncio.get_running_loop()
        handler = AsyncStreamingIteratorCallbackHandler(loop=loop)
        config = RunnableConfig(callbacks=[handler])
        emitted_agents: set[str] = set()

        runner = asyncio.create_task(workflow.run_async(input_data, config))
        yield {"type": "workflow_start", "workflow_name": workflow.name}

        try:
            async for chunk in handler:
                mapped = _map_stream_chunk(chunk)
                if not mapped:
                    continue
                aid = mapped.get("agent_id")
                if mapped.get("type") == "agent_thinking" and aid and aid not in emitted_agents:
                    emitted_agents.add(aid)
                    yield {
                        "type": "agent_start",
                        "agent_id": aid,
                        "agent_name": mapped.get("agent_name"),
                    }
                yield mapped
        finally:
            result: RunnableResult | None = None
            exc: BaseException | None = None
            try:
                result = await runner
            except BaseException as err:  # noqa: BLE001
                exc = err
                log.exception("dynamiq.workflow.failed")

            if isinstance(exc, Exception):
                yield {"type": "error", "message": str(exc)}
            elif result:
                yield self._finalize_workflow_chunk(result)

    @staticmethod
    def _finalize_workflow_chunk(result: RunnableResult) -> dict[str, Any]:
        summary: Any = None
        ok = bool(result.status == RunnableStatus.SUCCESS)
        if getattr(result, "output", None) is not None:
            try:
                summary = serialize_files_in_value(result.output)
            except (TypeError, ValueError):  # pragma: no cover
                summary = str(result.output)
        err_txt: str | None = None
        if not ok:
            msg = getattr(getattr(result, "error", None), "message", None)
            err_txt = str(msg) if msg else "workflow_failure"
        payload = {"type": "workflow_complete", "success": ok, "result": summary}
        if err_txt:
            payload["message"] = err_txt
            payload.setdefault("success", False)
        return payload


__all__ = ["DynamiqService"]
