"""FastAPI dependencies shared across routers."""

from __future__ import annotations

from fastapi import Depends

from core.config import Settings, get_settings
from core.redis import get_redis
from egpt_mcp.tool_registry import ToolRegistry


def get_tool_registry(settings: Settings = Depends(get_settings)) -> ToolRegistry:
    return ToolRegistry(settings, get_redis())
