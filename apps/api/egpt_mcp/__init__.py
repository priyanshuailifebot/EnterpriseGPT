"""Composio integration layer (package name ``egpt_mcp`` avoids shadowing PyPI ``mcp`` used by Dynamiq)."""

from egpt_mcp.oauth_service import OAuthService, OAuthStateError
from egpt_mcp.provider_apps import resolve_composio_app, supported_providers
from egpt_mcp.tool_registry import ToolExecutionError, ToolRegistry
from egpt_mcp.tool_run_buffer import ToolRunBuffer

__all__ = [
    "OAuthService",
    "OAuthStateError",
    "resolve_composio_app",
    "supported_providers",
    "ToolExecutionError",
    "ToolRegistry",
    "ToolRunBuffer",
]
