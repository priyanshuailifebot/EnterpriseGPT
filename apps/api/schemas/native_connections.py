"""Schemas for the native (non-Composio) connections API."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ProviderFieldSchema(BaseModel):
    key: str
    label: str
    type: str
    required: bool
    placeholder: str | None = None
    help_text: str | None = None


class ProviderCatalogEntry(BaseModel):
    id: str
    name: str
    category: str
    description: str
    auth_type: str
    icon: str
    docs_url: str | None = None
    tool_slugs: list[str] = Field(default_factory=list)
    fields: list[ProviderFieldSchema] = Field(default_factory=list)


class ProviderCatalogResponse(BaseModel):
    providers: list[ProviderCatalogEntry]


class ConnectionCreateRequest(BaseModel):
    provider: str
    name: str = Field(min_length=1, max_length=128)
    config: dict[str, Any] = Field(default_factory=dict)


class ConnectionPatchRequest(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    config: dict[str, Any] | None = None


class ConnectionResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    provider: str
    name: str
    auth_type: str
    status: str
    tool_slugs: list[str] = Field(default_factory=list)
    last_test_at: datetime | None = None
    last_test_error: str | None = None
    last_used_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ConnectionTestResponse(BaseModel):
    success: bool
    message: str


__all__ = [
    "ProviderFieldSchema",
    "ProviderCatalogEntry",
    "ProviderCatalogResponse",
    "ConnectionCreateRequest",
    "ConnectionPatchRequest",
    "ConnectionResponse",
    "ConnectionTestResponse",
]
