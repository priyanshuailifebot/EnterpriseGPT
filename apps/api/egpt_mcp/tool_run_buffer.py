"""Accumulates Composio tool call rows for async persistence after Dynamiq runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass
class ToolRunBuffer:
    """Buffered ``ToolExecutionLog`` payloads written after tools complete."""

    execution_id: UUID | None
    entries: list[dict[str, Any]] = field(default_factory=list)

    def append(self, row: dict[str, Any]) -> None:
        self.entries.append(row)
