"""Integration / Composio MCP API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class IntegrationResponse(BaseModel):
    id: UUID
    provider: str
    status: str
    scopes: list[str]
    connected_at: datetime | None
    last_used: datetime | None
    available_tools: list[str] = Field(default_factory=list)


class ConnectIntegrationResponse(BaseModel):
    redirect_url: str
    state_token: str


class ToolDefinition(BaseModel):
    name: str
    description: str = ""
    provider: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolsListResponse(BaseModel):
    tools: list[ToolDefinition]


class ToolTestRequest(BaseModel):
    tool_name: str
    params: dict[str, Any] = Field(default_factory=dict)


class ToolTestResponse(BaseModel):
    result: dict[str, Any]


class OAuthCallbackResponse(BaseModel):
    status: str
    integration_id: UUID
