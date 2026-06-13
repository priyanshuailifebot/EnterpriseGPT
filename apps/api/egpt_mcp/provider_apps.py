"""Maps EnterpriseGPT provider strings to Composio ``App`` enums."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def resolve_composio_app(provider: str):
    """Return ``composio.App`` member for a lowercase provider id or ``None``."""
    from egpt_mcp._composio_compat import App

    key = provider.strip().lower().replace("-", "").replace("_", "")
    aliases = {
        "gmail": "GMAIL",
        "googledrive": "GOOGLEDRIVE",
        "googlecalendar": "GOOGLECALENDAR",
        "googlesheets": "GOOGLESHEETS",
        "googlemeet": "GOOGLEMEET",
        "servicenow": "SERVICENOW",
        "jira": "JIRA",
    }
    enum_name = aliases.get(key)
    if enum_name is None:
        return None
    return getattr(App, enum_name, None)


def supported_providers() -> tuple[str, ...]:
    return (
        "gmail",
        "googledrive",
        "googlecalendar",
        "googlesheets",
        "googlemeet",
        "servicenow",
        "jira",
    )
