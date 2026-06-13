"""Bounded snapshotting of node inputs/outputs for the test-run inspector.

Used by both executors and the persistence layer so an SSE ``node_complete``
frame and the JSONB ``workflow_execution_steps`` row stay small and JSON-safe.

Contract (shared by every producer so demo and real runs match exactly):

* The return value is ALWAYS a JSON-serialisable ``dict`` — never ``None`` and
  never a bare scalar. This matters because the demo executor's ``_event``
  helper drops ``None`` kwargs; wrapping in a dict guarantees the
  ``input_snapshot`` / ``output_snapshot`` keys survive in every event.
* Oversized payloads are truncated to ``{"__truncated__": True, "preview": …}``
  so neither the SSE frame nor the JSONB column bloats.
"""

from __future__ import annotations

import json
from typing import Any

_MAX_CHARS = 4000


def snapshot(value: Any, *, max_chars: int = _MAX_CHARS) -> dict[str, Any]:
    """Return a small, JSON-safe dict wrapper for ``value``.

    Never raises. ``None`` → ``{"value": None}`` so the key always survives.
    """
    try:
        rendered = json.dumps(value, default=str)
    except Exception:  # noqa: BLE001 — snapshotting must never break a run
        return {"__unserialisable__": True, "preview": str(value)[:max_chars]}

    if len(rendered) > max_chars:
        return {"__truncated__": True, "preview": rendered[:max_chars]}

    # Wrap non-dict values so the snapshot is always a dict (stable column type).
    if isinstance(value, dict):
        return value
    return {"value": value}
