"""Phase 4 approval-style coverage — Composio MCP layer (mocked SDK)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from models.integration import Integration, IntegrationStatus
from models.tool_execution_log import ToolExecutionLog
from models.user import UserRole
from schemas.workflow import AgentDefinition, WorkflowDefinition


async def _register(
    client: AsyncClient,
    *,
    email: str,
    role: UserRole = UserRole.BUILDER,
) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "supersecret123",
            "full_name": "Phase4 User",
            "role": role.value,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_phase4_integrations_tools_empty(client: AsyncClient) -> None:
    body = await _register(client, email="tools-empty@example.com", role=UserRole.BUILDER)
    ws_id = body["user"]["workspaces"][0]["workspace_id"]
    hdr = {"Authorization": f"Bearer {body['access_token']}"}

    resp = await client.get(f"/api/v1/integrations/tools?workspace_id={ws_id}", headers=hdr)
    assert resp.status_code == 200
    assert resp.json()["tools"] == []


@pytest.mark.asyncio
async def test_phase4_tool_cache_reduces_composio_fetch(client: AsyncClient) -> None:
    body = await _register(client, email="cache-hit@example.com", role=UserRole.BUILDER)
    ws_id = body["user"]["workspaces"][0]["workspace_id"]
    hdr = {"Authorization": f"Bearer {body['access_token']}"}

    fake_tools = [
        {
            "name": "GMAIL_SEND_EMAIL",
            "description": "send mail",
            "provider": "gmail",
            "parameters": {},
        }
    ]

    with patch(
        "egpt_mcp.tool_registry.ToolRegistry._fetch_tools_fresh",
        new_callable=AsyncMock,
        return_value=fake_tools,
    ) as fetch_mock:
        r1 = await client.get(f"/api/v1/integrations/tools?workspace_id={ws_id}", headers=hdr)
        r2 = await client.get(f"/api/v1/integrations/tools?workspace_id={ws_id}", headers=hdr)

    assert r1.status_code == 200 and r2.status_code == 200
    assert fetch_mock.await_count == 1
    names = {t["name"] for t in r2.json()["tools"]}
    assert "GMAIL_SEND_EMAIL" in names


@pytest.mark.asyncio
async def test_phase4_oauth_connect_returns_redirect(client: AsyncClient) -> None:
    body = await _register(client, email="oauth-connect@example.com", role=UserRole.ADMIN)
    ws_id = body["user"]["workspaces"][0]["workspace_id"]
    hdr = {"Authorization": f"Bearer {body['access_token']}"}

    async def _fake_initiate(self, db, *, workspace_id, user, provider):  # noqa: ARG002
        pending = Integration(
            workspace_id=workspace_id,
            user_id=user.id,
            provider=provider.strip().lower(),
            composio_entity_id=f"egpt-{workspace_id}-{user.id}",
            composio_connection_id="ca_pending",
            status=IntegrationStatus.PENDING,
            scopes=[],
        )
        db.add(pending)
        await db.flush()
        await db.commit()
        return "https://connect.example/oauth", "statestub"

    with patch(
        "routers.integrations.OAuthService.initiate_connection",
        new=_fake_initiate,
    ):
        resp = await client.post(
            f"/api/v1/integrations/gmail/connect?workspace_id={ws_id}",
            headers=hdr,
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["redirect_url"].startswith("https://connect.example")


@pytest.mark.asyncio
async def test_phase4_oauth_callback_marks_connected(client: AsyncClient, db_session) -> None:
    body = await _register(client, email="oauth-callback@example.com", role=UserRole.BUILDER)
    uid = UUID(body["user"]["id"])
    ws_id = UUID(str(body["user"]["workspaces"][0]["workspace_id"]))

    integ = Integration(
        workspace_id=ws_id,
        user_id=uid,
        provider="gmail",
        composio_entity_id=f"egpt-{ws_id}-{uid}",
        composio_connection_id="ca_old",
        status=IntegrationStatus.PENDING,
        scopes=[],
    )
    db_session.add(integ)
    await db_session.commit()
    await db_session.refresh(integ)
    integration_id = integ.id

    from core.redis import get_redis

    redis = get_redis()
    await redis.set(
        "egpt:oauth_state:teststate",
        json.dumps(
            {
                "workspace_id": str(ws_id),
                "user_id": str(uid),
                "provider": "gmail",
                "integration_id": str(integration_id),
                "entity_id": integ.composio_entity_id,
            }
        ),
        ex=600,
    )

    class _Acct:
        id = "ca_live"

    class _CA:
        def get(self, *, connection_id: str) -> _Acct:  # noqa: ARG002
            return _Acct()

    class _Client:
        connected_accounts = _CA()

    def _fake_toolset(entity_id: str) -> MagicMock:  # noqa: ARG001
        m = MagicMock()
        m.client = _Client()
        return m

    with patch("egpt_mcp.oauth_service.OAuthService._toolset", side_effect=_fake_toolset):
        resp = await client.get(
            "/api/v1/integrations/callback",
            params={
                "state": "teststate",
                "status": "success",
                "connected_account_id": "ca_live",
            },
        )

    assert resp.status_code == 200
    db_session.expire_all()
    row = (
        await db_session.execute(select(Integration).where(Integration.id == integration_id))
    ).scalar_one()
    assert row.status == IntegrationStatus.CONNECTED


@pytest.mark.asyncio
async def test_phase4_disconnect_revokes_and_clears_tool_cache(client: AsyncClient, db_session) -> None:
    body = await _register(client, email="disconnect@example.com", role=UserRole.ADMIN)
    uid = UUID(body["user"]["id"])
    ws_id = UUID(str(body["user"]["workspaces"][0]["workspace_id"]))
    hdr = {"Authorization": f"Bearer {body['access_token']}"}

    integ = Integration(
        workspace_id=ws_id,
        user_id=uid,
        provider="gmail",
        composio_entity_id=f"egpt-{ws_id}-{uid}",
        composio_connection_id="ca_x",
        status=IntegrationStatus.CONNECTED,
        scopes=[],
    )
    db_session.add(integ)
    await db_session.commit()

    from core.redis import get_redis

    redis = get_redis()
    await redis.set(f"tools:{ws_id}", json.dumps([{"name": "cached"}]))

    resp = await client.delete(
        f"/api/v1/integrations/{integ.id}",
        params={"workspace_id": str(ws_id)},
        headers=hdr,
    )
    assert resp.status_code == 204

    cached = await redis.get(f"tools:{ws_id}")
    assert cached is None


@pytest.mark.asyncio
async def test_phase4_tool_test_writes_execution_log(client: AsyncClient, db_session) -> None:
    body = await _register(client, email="tool-test@example.com", role=UserRole.BUILDER)
    ws_id = UUID(str(body["user"]["workspaces"][0]["workspace_id"]))
    hdr = {"Authorization": f"Bearer {body['access_token']}"}

    def _fake_sync_execute(
        self,  # noqa: ARG001
        *,
        integrations,
        tool_name,
        params,
        execution_id,
        tool_run_buffer,
    ):
        import time

        started = time.perf_counter()
        output = {"successfull": True, "data": {"ok": True}}
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if tool_run_buffer is not None:
            tool_run_buffer.append(
                {
                    "execution_id": execution_id,
                    "tool_name": tool_name,
                    "input_params": dict(params or {}),
                    "output_data": output,
                    "duration_ms": elapsed_ms,
                    "success": True,
                    "error_message": None,
                }
            )
        return output

    with patch(
        "egpt_mcp.tool_registry.ToolRegistry.sync_execute_action",
        new=_fake_sync_execute,
    ):
        resp = await client.post(
            f"/api/v1/integrations/tools/test?workspace_id={ws_id}",
            headers=hdr,
            json={"tool_name": "DEMO_ACTION", "params": {"a": 1}},
        )

    assert resp.status_code == 200
    logs = (await db_session.execute(select(ToolExecutionLog))).scalars().all()
    assert len(logs) >= 1
    assert logs[-1].tool_name == "DEMO_ACTION"
    assert logs[-1].duration_ms is not None


@pytest.mark.asyncio
async def test_phase4_interpreter_prompt_includes_registry_tools(client: AsyncClient) -> None:
    body = await _register(client, email="interpret-tools@example.com", role=UserRole.BUILDER)
    ws_id = UUID(str(body["user"]["workspaces"][0]["workspace_id"]))
    hdr = {"Authorization": f"Bearer {body['access_token']}"}

    fake_def = WorkflowDefinition(
        name="demo",
        description="",
        trigger="manual",
        agents=[
            AgentDefinition(
                id="a1",
                name="Agent",
                role="",
                instructions="Do thing",
                tools=[],
                depends_on=[],
                is_parallel=False,
            )
        ],
        human_checkpoints=[],
        output_format="text",
    )

    captured: dict = {}

    async def _fake_interpret(self, *, user_input: str, available_tools: list[str]):  # noqa: ARG002
        captured["tools"] = list(available_tools)
        return fake_def

    with (
        patch(
            "services.workflow_service.ToolRegistry.get_tool_names_for_prompt",
            new_callable=AsyncMock,
            return_value=["GMAIL_LIST_MESSAGES"],
        ),
        patch(
            "services.workflow_service.WorkflowInterpreter.interpret",
            new=_fake_interpret,
        ),
    ):
        resp = await client.post(
            "/api/v1/workflows/interpret",
            headers=hdr,
            json={
                "text": "Send weekly digest email",
                "workspace_id": str(ws_id),
                "skip_clarification": True,
            },
        )

    assert resp.status_code == 200
    union = set(captured.get("tools", []))
    assert "GMAIL_LIST_MESSAGES" in union


@pytest.mark.asyncio
async def test_phase4_post_connect_composio_failure_returns_503(client: AsyncClient) -> None:
    body = await _register(client, email="composio-down@example.com", role=UserRole.ADMIN)
    ws_id = body["user"]["workspaces"][0]["workspace_id"]
    hdr = {"Authorization": f"Bearer {body['access_token']}"}

    def _boom(entity_id: str) -> MagicMock:  # noqa: ARG001
        raise RuntimeError("composio unavailable")

    with patch("egpt_mcp.oauth_service.OAuthService._toolset", side_effect=_boom):
        resp = await client.post(
            f"/api/v1/integrations/gmail/connect?workspace_id={ws_id}",
            headers=hdr,
        )

    assert resp.status_code == 503
