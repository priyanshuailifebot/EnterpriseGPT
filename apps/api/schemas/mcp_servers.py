"""Pydantic schemas for the per-workspace MCP server registry."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl

MCPTransport = Literal["streamable-http", "sse"]


class MCPServerCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    url: HttpUrl
    transport: MCPTransport = "streamable-http"
    # Bearer-style auth: the header *name* is server-specific (Composio uses
    # ``X-CONSUMER-API-KEY``, others use ``Authorization`` etc.). We let the
    # operator specify both so the form works for any MCP server.
    auth_header_name: str = Field(default="X-CONSUMER-API-KEY", max_length=128)
    auth_header_value: str = Field(..., min_length=1, max_length=4096)
    extra_headers: dict[str, str] = Field(default_factory=dict)


class MCPServerPatchRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    url: HttpUrl | None = None
    transport: MCPTransport | None = None
    auth_header_name: str | None = Field(None, max_length=128)
    auth_header_value: str | None = Field(None, min_length=1, max_length=4096)
    extra_headers: dict[str, str] | None = None


class MCPServerResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    url: str
    transport: MCPTransport
    status: str
    auth_header_name: str
    last_test_at: datetime | None
    last_test_error: str | None
    last_tool_count: int | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MCPServerTestResponse(BaseModel):
    success: bool
    message: str
    tool_count: int = 0
    sample_tool_names: list[str] = Field(default_factory=list)


__all__ = [
    "MCPServerCreateRequest",
    "MCPServerPatchRequest",
    "MCPServerResponse",
    "MCPServerTestResponse",
    "MCPTransport",
]
