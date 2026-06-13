"""Stub shims for the legacy ``composio`` SDK surface.

The codebase was written against ``composio < 1.0`` which exposed
``ComposioToolSet`` and an ``App`` enum. The current ``composio`` package
on PyPI is a different SDK (``Composio`` class, toolkit-name strings).
Until the integration layer is migrated, these shims let the modules
import so the rest of the API can boot; any composio-backed endpoint
will raise ``ComposioMigrationError`` if actually invoked.
"""

from __future__ import annotations

from typing import Any


class ComposioMigrationError(RuntimeError):
    """Raised when legacy Composio surface is invoked before migration."""

    def __init__(self) -> None:
        super().__init__(
            "Composio integration uses the legacy ToolSet API which is no "
            "longer available in composio>=1.0. Migrate egpt_mcp to the new "
            "Composio() SDK before calling this code path."
        )


class ComposioToolSet:  # noqa: N801 — mirror legacy name
    def __init__(self, *_: Any, **__: Any) -> None:
        raise ComposioMigrationError()


class _AppStub:
    """Stand-in for the legacy ``composio.App`` enum.

    ``getattr(App, "GMAIL")`` returns the literal string ``"GMAIL"`` so
    callers that only treat values as opaque identifiers keep working.
    """

    def __getattr__(self, name: str) -> str:
        return name


App = _AppStub()

__all__ = ["App", "ComposioMigrationError", "ComposioToolSet"]
