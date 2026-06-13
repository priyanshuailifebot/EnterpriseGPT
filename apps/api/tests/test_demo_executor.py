"""DemoExecutor + sample_input endpoint tests.

The demo executor must:
  * Emit the same event shape the real Dynamiq executor produces, so the
    frontend SSE consumer doesn't have to special-case demo runs.
  * Walk every node kind without crashing and without external calls.
  * Be deterministic — same definition, same event sequence.
  * Skip MemoryNode / OutputParserNode (non-executable kinds) cleanly.
  * Return ``__dry_run__: true`` stubs for action / data_store nodes.
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from httpx import AsyncClient

from models.user import UserRole
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
from services.demo_executor import run_demo, sample_input_for


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _register(client: AsyncClient, suffix: str) -> tuple[str, UUID]:
    body = {
        "email": f"demo-{suffix}@test.io",
        "password": "supersecret123",
        "full_name": "Demo",
        "role": UserRole.BUILDER.value,
    }
    r = await client.post("/api/v1/auth/register", json=body)
    assert r.status_code == 201, r.text
    d = r.json()
    return d["access_token"], UUID(d["user"]["workspaces"][0]["workspace_id"])


async def _create_workflow(
    client: AsyncClient, token: str, ws: UUID, defn: WorkflowDefinition
) -> UUID:
    r = await client.post(
        "/api/v1/workflows/",
        json={
            "workspace_id": str(ws),
            "definition": json.loads(defn.model_dump_json()),
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    return UUID(r.json()["id"])


def _trigger_agent_defn() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="Demo",
        nodes=[
            TriggerNode(id="trig", name="Trigger", trigger_type="manual"),
            AgentNode(
                id="agent",
                name="Demo Agent",
                depends_on=["trig"],
                role="Helpful demo agent",
                instructions="Demo only — return a friendly response.",
            ),
        ],
    )


def _full_zoo_defn() -> WorkflowDefinition:
    """Exercise every executable node kind in a single graph."""
    return WorkflowDefinition(
        name="Zoo",
        nodes=[
            TriggerNode(
                id="trig",
                name="Form",
                trigger_type="form",
                slug="zoo",
                form_fields=[
                    {"key": "email", "label": "Email", "type": "text"},
                    {"key": "tier", "label": "Tier", "type": "choice", "options": ["gold", "silver"]},
                ],
            ),
            AgentNode(
                id="agent",
                name="Triage",
                depends_on=["trig"],
                role="triage agent",
                instructions="Classify and route.",
            ),
            ActionNode(
                id="notify",
                name="Notify",
                depends_on=["agent"],
                provider="slack",
                action_slug="slack_send_message",
                params={"channel": "#demo"},
            ),
            DataStoreNode(
                id="store",
                name="Store",
                depends_on=["notify"],
                op="write",
                table="demo_table",
                key="demo-1",
                payload={"hello": "world"},
            ),
            ConditionNode(
                id="route",
                name="Route",
                depends_on=["store"],
                expression="is this important?",
                branches=["important", "trivial"],
            ),
            IfNode(
                id="check",
                name="Check",
                depends_on=["route"],
                expression="$.store.wrote.hello == 'world'",
            ),
            ForEachNode(
                id="loop",
                name="Loop",
                depends_on=["check"],
                items_from="store",
                body=["check"],
            ),
            MergeNode(id="merge", name="Merge", depends_on=["loop", "check"]),
            WaitForWebhookNode(
                id="wait",
                name="Wait",
                depends_on=["merge"],
                description="Wait for human",
            ),
            # Non-executable kinds — DemoExecutor must skip these.
            MemoryNode(id="mem", name="Mem", parent_agent_id="agent"),
            OutputParserNode(
                id="parser",
                name="Parser",
                parent_agent_id="agent",
                json_schema={"type": "object"},
            ),
        ],
    )


# ---------------------------------------------------------------------------
# run_demo — emits correct events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_emits_workflow_start_and_complete() -> None:
    defn = _trigger_agent_defn()
    events = []
    async for ev in run_demo(definition=defn, step_delay_ms=0):
        events.append(ev)
    assert events[0]["type"] == "workflow_start"
    assert events[0]["data"]["demo"] is True
    assert events[-1]["type"] == "workflow_complete"
    assert events[-1]["success"] is True


@pytest.mark.asyncio
async def test_demo_emits_trigger_then_agent_lifecycle() -> None:
    defn = _trigger_agent_defn()
    events = []
    async for ev in run_demo(
        definition=defn,
        input_data={"foo": "bar"},
        step_delay_ms=0,
    ):
        events.append(ev)
    types = [e["type"] for e in events]
    assert "trigger_fired" in types
    assert types.count("agent_start") == 1
    assert types.count("agent_thinking") == 1
    assert types.count("agent_complete") == 1
    # Trigger event includes the user-provided input.
    trig = next(e for e in events if e["type"] == "trigger_fired")
    assert trig["data"]["input"] == {"foo": "bar"}


@pytest.mark.asyncio
async def test_demo_visits_every_executable_kind() -> None:
    defn = _full_zoo_defn()
    events = []
    async for ev in run_demo(definition=defn, step_delay_ms=0):
        events.append(ev)
    types = [e["type"] for e in events]
    # One representative event per kind.
    for required in (
        "trigger_fired",
        "agent_start",
        "agent_complete",
        "action_invoked",
        "data_store_op",
        "condition_decided",
        "if_decided",
        "for_each_started",
        "for_each_complete",
        "wait_for_webhook",
        "webhook_resumed",
    ):
        assert required in types, f"missing {required!r} in {types}"


@pytest.mark.asyncio
async def test_demo_emits_node_complete_per_executed_node() -> None:
    """Every executed node kind gets exactly one node_complete with snapshots.

    This is the per-node inspection contract the test-run drawer + persisted
    step rows rely on. Demo and real runs must emit the same shape.
    """
    defn = _full_zoo_defn()
    events = []
    async for ev in run_demo(definition=defn, step_delay_ms=0):
        events.append(ev)

    nc = [e for e in events if e["type"] == "node_complete"]
    seen = {e["node_id"] for e in nc}
    # All executable kinds (memory / output_parser are skipped).
    assert seen == {
        "trig",
        "agent",
        "notify",
        "store",
        "route",
        "check",
        "loop",
        "merge",
        "wait",
    }, seen
    # One event per node — no duplicates.
    assert len(nc) == len(seen)

    for e in nc:
        # snapshot() always returns a dict so the keys survive _event's
        # None-dropping; both must be present on every event.
        assert isinstance(e["input_snapshot"], dict)
        assert isinstance(e["output_snapshot"], dict)
        assert e["status"] == "completed"
        assert isinstance(e["duration_ms"], int)
        assert e["node_kind"]

    # The unconnected action node is flagged dry-run.
    notify = next(e for e in nc if e["node_id"] == "notify")
    assert notify["dry_run"] is True


@pytest.mark.asyncio
async def test_demo_node_complete_does_not_shift_terminal_events() -> None:
    """Adding node_complete must not displace workflow_start / _complete."""
    defn = _full_zoo_defn()
    events = []
    async for ev in run_demo(definition=defn, step_delay_ms=0):
        events.append(ev)
    assert events[0]["type"] == "workflow_start"
    assert events[-1]["type"] == "workflow_complete"


@pytest.mark.asyncio
async def test_demo_action_emits_dry_run_stub() -> None:
    defn = _full_zoo_defn()
    events = []
    async for ev in run_demo(definition=defn, step_delay_ms=0):
        events.append(ev)
    dry_run = next(e for e in events if e["type"] == "action_dry_run")
    assert dry_run["data"]["result"]["__dry_run__"] is True
    assert dry_run["data"]["result"]["__demo__"] is True


@pytest.mark.asyncio
async def test_demo_is_deterministic() -> None:
    defn = _trigger_agent_defn()
    runs: list[list[str]] = []
    for _ in range(2):
        events = []
        async for ev in run_demo(definition=defn, step_delay_ms=0):
            events.append(ev)
        runs.append([e["type"] for e in events])
    assert runs[0] == runs[1]


@pytest.mark.asyncio
async def test_demo_agent_satellites_emit_tool_call_pairs() -> None:
    """Agents with satellite actions/data_stores fire one tool_call +
    tool_result pair per satellite."""
    defn = WorkflowDefinition(
        name="With Satellites",
        nodes=[
            TriggerNode(id="trig", name="Trigger", trigger_type="manual"),
            AgentNode(id="agent", name="Agent", depends_on=["trig"], role="r"),
            ActionNode(
                id="lookup",
                name="Lookup",
                parent_agent_id="agent",
                provider="postgres",
                action_slug="postgres_query",
                tool_description="lookup",
            ),
            DataStoreNode(
                id="counters",
                name="Counters",
                parent_agent_id="agent",
                op="read",
                table="counters",
            ),
        ],
    )
    events = []
    async for ev in run_demo(definition=defn, step_delay_ms=0):
        events.append(ev)
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_calls) == 2
    assert len(tool_results) == 2
    # Each tool_call is matched by a tool_result for the same satellite.
    call_ids = {e["data"]["node_id"] for e in tool_calls}
    result_ids = {e["data"]["node_id"] for e in tool_results}
    assert call_ids == {"lookup", "counters"}
    assert result_ids == {"lookup", "counters"}


# ---------------------------------------------------------------------------
# sample_input_for — pure helper
# ---------------------------------------------------------------------------


def test_sample_input_for_chat() -> None:
    defn = WorkflowDefinition(
        name="Chat",
        nodes=[
            TriggerNode(id="t", name="t", trigger_type="chat"),
            AgentNode(id="a", name="a", depends_on=["t"]),
        ],
    )
    assert sample_input_for(defn) == {"message": "Hello from the demo run."}


def test_sample_input_for_form() -> None:
    defn = WorkflowDefinition(
        name="Form",
        nodes=[
            TriggerNode(
                id="t",
                name="t",
                trigger_type="form",
                slug="f",
                form_fields=[
                    {"key": "email", "type": "text", "placeholder": "you@example.com"},
                    {"key": "tier", "type": "choice", "options": ["gold", "silver"]},
                    {"key": "tags", "type": "multi_choice", "options": ["a", "b"]},
                ],
            ),
            AgentNode(id="a", name="a", depends_on=["t"]),
        ],
    )
    out = sample_input_for(defn)
    assert out["email"] == "you@example.com"
    assert out["tier"] == "gold"
    assert out["tags"] == ["a"]


def test_sample_input_for_webhook() -> None:
    defn = WorkflowDefinition(
        name="Webhook",
        nodes=[
            TriggerNode(id="t", name="t", trigger_type="webhook", slug="hook"),
            AgentNode(id="a", name="a", depends_on=["t"]),
        ],
    )
    out = sample_input_for(defn)
    assert "event" in out
    assert isinstance(out["payload"], dict)


def test_sample_input_no_trigger_returns_empty() -> None:
    defn = WorkflowDefinition(
        name="No Trig",
        agents=[
            {
                "id": "a",
                "name": "a",
                "role": "",
                "instructions": "",
                "tools": [],
                "depends_on": [],
                "is_parallel": False,
            }
        ],  # type: ignore[arg-type]
    )
    assert sample_input_for(defn) == {}


# ---------------------------------------------------------------------------
# End-to-end: SSE execute endpoint with demo=true
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_endpoint_demo_streams_without_llm_creds(
    client: AsyncClient,
) -> None:
    """Demo mode runs without ``AZURE_OPENAI_ENDPOINT`` being set.

    This is the whole point — users can preview a workflow before they
    have any credentials configured. The endpoint streams SSE; we
    collect the body and assert the canonical event sequence is present.
    """
    token, ws = await _register(client, "exec")
    defn = _trigger_agent_defn()
    wf_id = await _create_workflow(client, token, ws, defn)

    # We use ``client.stream`` to read the SSE response without waiting
    # on the full event loop.
    async with client.stream(
        "POST",
        f"/api/v1/workflows/{wf_id}/execute",
        json={"input_data": {"foo": "bar"}, "variables": {}, "demo": True},
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        assert response.status_code == 200, await response.aread()
        chunks: list[str] = []
        async for line in response.aiter_lines():
            chunks.append(line)
    body = "\n".join(chunks)
    assert "workflow_start" in body
    assert "agent_start" in body
    assert "workflow_complete" in body
    # Demo flag round-tripped onto the workflow_start event.
    assert '"demo": true' in body or '"demo":true' in body


# ---------------------------------------------------------------------------
# Real-LLM-on-demo path — patched Azure client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_uses_real_llm_when_settings_provided(monkeypatch) -> None:
    """When ``settings`` has Azure creds, the demo agent calls real Azure
    and the returned text appears in the ``agent_complete`` event."""
    from services import demo_executor

    class _FakeSettings:
        AZURE_OPENAI_ENDPOINT = "https://demo.example.com"
        AZURE_OPENAI_API_KEY = "fake-key"
        AZURE_OPENAI_DEPLOYMENT = "gpt-4o-mini"
        AZURE_OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
        AZURE_OPENAI_API_VERSION = "2024-02-15-preview"

    async def _fake_call(node, *, settings, input_data, prior_outputs, kb_context=""):
        # The helper signature mirrors the real one — assert we got the
        # right context shape so production refactors don't silently
        # change the call signature.
        assert node.role == "Helpful demo agent"
        assert settings.AZURE_OPENAI_ENDPOINT == "https://demo.example.com"
        return "[real azure] Hi Alice, here's your sample response."

    monkeypatch.setattr(demo_executor, "_call_real_azure_for_agent", _fake_call)

    events = []
    async for ev in demo_executor.run_demo(
        definition=_trigger_agent_defn(),
        input_data={"name": "Alice"},
        step_delay_ms=0,
        settings=_FakeSettings(),
    ):
        events.append(ev)
    complete = next(e for e in events if e["type"] == "agent_complete")
    assert "[real azure]" in complete["content"]
    assert complete["data"]["real_llm"] is True


@pytest.mark.asyncio
async def test_demo_falls_back_to_stub_when_llm_call_returns_none(monkeypatch) -> None:
    """A failed/empty real-LLM call must not break the demo run — the
    synthesized stub kicks in and the run completes cleanly."""
    from services import demo_executor

    class _FakeSettings:
        AZURE_OPENAI_ENDPOINT = "https://demo.example.com"
        AZURE_OPENAI_API_KEY = "fake-key"
        AZURE_OPENAI_DEPLOYMENT = "gpt-4o-mini"
        AZURE_OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
        AZURE_OPENAI_API_VERSION = "2024-02-15-preview"

    async def _fake_call(node, *, settings, input_data, prior_outputs, kb_context=""):
        return None  # simulates timeout / auth error / empty response

    monkeypatch.setattr(demo_executor, "_call_real_azure_for_agent", _fake_call)

    events = []
    async for ev in demo_executor.run_demo(
        definition=_trigger_agent_defn(),
        step_delay_ms=0,
        settings=_FakeSettings(),
    ):
        events.append(ev)
    complete = next(e for e in events if e["type"] == "agent_complete")
    assert "[demo] Mock response" in complete["content"]
    assert complete["data"]["real_llm"] is False
    assert events[-1]["type"] == "workflow_complete"


@pytest.mark.asyncio
async def test_demo_ignores_settings_without_credentials() -> None:
    """``settings`` with blank Azure values must NOT trigger the real
    path — the synth stub is used instead."""
    from services import demo_executor

    class _BlankSettings:
        AZURE_OPENAI_ENDPOINT = ""
        AZURE_OPENAI_API_KEY = ""
        AZURE_OPENAI_DEPLOYMENT = ""
        AZURE_OPENAI_DEFAULT_MODEL = ""
        AZURE_OPENAI_API_VERSION = ""

    events = []
    async for ev in demo_executor.run_demo(
        definition=_trigger_agent_defn(),
        step_delay_ms=0,
        settings=_BlankSettings(),
    ):
        events.append(ev)
    complete = next(e for e in events if e["type"] == "agent_complete")
    assert complete["data"]["real_llm"] is False


@pytest.mark.asyncio
async def test_has_azure_creds_helper() -> None:
    from services.demo_executor import _has_azure_creds

    class _S:
        AZURE_OPENAI_ENDPOINT = "https://x.com"
        AZURE_OPENAI_API_KEY = "k"

    class _Blank:
        AZURE_OPENAI_ENDPOINT = ""
        AZURE_OPENAI_API_KEY = ""

    class _Partial:
        AZURE_OPENAI_ENDPOINT = "https://x.com"
        AZURE_OPENAI_API_KEY = "  "

    assert _has_azure_creds(_S()) is True
    assert _has_azure_creds(_Blank()) is False
    assert _has_azure_creds(_Partial()) is False


@pytest.mark.asyncio
async def test_execute_endpoint_demo_with_real_llm_flag(
    client: AsyncClient,
    monkeypatch,
) -> None:
    """Endpoint round-trip: client posts ``demo + use_real_llm``, server
    invokes the real-LLM path inside ``run_demo``."""
    from services import demo_executor

    captured_calls: list[dict[str, Any]] = []

    async def _fake_call(node, *, settings, input_data, prior_outputs, kb_context=""):
        captured_calls.append({"node": node.id, "input": input_data})
        return "[mocked-azure-from-endpoint]"

    monkeypatch.setattr(demo_executor, "_call_real_azure_for_agent", _fake_call)
    # Pretend Azure creds are configured for this run.
    monkeypatch.setattr(demo_executor, "_has_azure_creds", lambda _settings: True)

    token, ws = await _register(client, "endpoint-real")
    defn = _trigger_agent_defn()
    wf_id = await _create_workflow(client, token, ws, defn)

    async with client.stream(
        "POST",
        f"/api/v1/workflows/{wf_id}/execute",
        json={
            "input_data": {"name": "Bob"},
            "variables": {},
            "demo": True,
            "use_real_llm": True,
        },
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        assert response.status_code == 200, await response.aread()
        body_chunks = []
        async for line in response.aiter_lines():
            body_chunks.append(line)
    body = "\n".join(body_chunks)
    assert "[mocked-azure-from-endpoint]" in body
    assert len(captured_calls) >= 1
    assert captured_calls[0]["node"] == "agent"


@pytest.mark.asyncio
async def test_execute_endpoint_demo_without_real_llm_flag_skips_azure(
    client: AsyncClient,
    monkeypatch,
) -> None:
    """When ``use_real_llm=false`` (default), the Azure helper must NOT
    be invoked even when creds are configured."""
    from services import demo_executor

    call_count = 0

    async def _fake_call(node, *, settings, input_data, prior_outputs, kb_context=""):
        nonlocal call_count
        call_count += 1
        return "[should-not-appear]"

    monkeypatch.setattr(demo_executor, "_call_real_azure_for_agent", _fake_call)
    monkeypatch.setattr(demo_executor, "_has_azure_creds", lambda _settings: True)

    token, ws = await _register(client, "endpoint-noreal")
    defn = _trigger_agent_defn()
    wf_id = await _create_workflow(client, token, ws, defn)

    async with client.stream(
        "POST",
        f"/api/v1/workflows/{wf_id}/execute",
        json={
            "input_data": {},
            "variables": {},
            "demo": True,
            "use_real_llm": False,
        },
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        async for _line in response.aiter_lines():
            pass
    assert call_count == 0


@pytest.mark.asyncio
async def test_sample_input_endpoint_returns_trigger_aware_payload(
    client: AsyncClient,
) -> None:
    token, ws = await _register(client, "sample")
    defn = WorkflowDefinition(
        name="Chat Demo",
        nodes=[
            TriggerNode(id="t", name="t", trigger_type="chat", slug="chat-demo"),
            AgentNode(id="a", name="a", depends_on=["t"]),
        ],
    )
    wf_id = await _create_workflow(client, token, ws, defn)
    r = await client.get(
        f"/api/v1/workflows/{wf_id}/sample_input",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["input_data"]["message"]
