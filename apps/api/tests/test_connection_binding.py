"""invoke_action honors a node-bound connection_id (multi-account)."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from agents import action_runner
from models.native_connection import NativeConnectionStatus
from schemas.workflow import ActionNode


def _conn(cid, provider="gmail"):
    return SimpleNamespace(id=cid, provider=provider, status=NativeConnectionStatus.ACTIVE)


def test_action_node_accepts_connection_id() -> None:
    n = ActionNode(
        id="a", name="A", provider="gmail", action_slug="gmail_send",
        connection_id="11111111-1111-1111-1111-111111111111",
    )
    assert n.connection_id == "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_invoke_action_prefers_bound_connection(monkeypatch) -> None:
    c1, c2 = uuid4(), uuid4()
    used = {}

    # Stub the provider so we can observe which connection was chosen without
    # making a real call.
    def fake_get_provider(pid):
        return SimpleNamespace(
            id="gmail",
            build_connection=lambda cfg: ("conn", cfg),
            build_tool=lambda conn, slug: SimpleNamespace(),
        )
    monkeypatch.setattr(action_runner, "get_provider", fake_get_provider)
    monkeypatch.setattr(action_runner, "resolve_provider_for_slug", lambda s: None)

    def fake_decode(conn_row):
        used["chosen"] = str(conn_row.id)
        return {}
    monkeypatch.setattr(action_runner, "decode_config", fake_decode)

    # Make the tool execution a no-op that returns a dict (sync — runs in thread).
    monkeypatch.setattr(action_runner, "_run_tool", lambda tool, params: {"ok": True})

    conns = [_conn(c1), _conn(c2)]
    # Bind to the SECOND connection explicitly.
    try:
        await action_runner.invoke_action(
            provider_id="gmail", action_slug="gmail_send", params={},
            workspace_connections=conns, allow_dry_run=True,
            connection_id=str(c2),
        )
    except Exception:
        pass  # we only care which connection decode_config saw

    assert used.get("chosen") == str(c2)
