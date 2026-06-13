"""Redis-backed conversation memory for ``MemoryNode`` instances.

Three scopes:

* ``session``  — keyed by ``session_id``. Cleared when the session expires
                 or is closed.
* ``user``     — keyed by ``user_id``. Survives across sessions for a
                 logged-in user; ignored for anonymous chats.
* ``workflow`` — keyed by ``workflow_id``. Global to the workflow.

Each scope writes to a Redis LIST keyed by ``egpt:mem:<scope>:<config_hash>:<scope_id>``
where ``config_hash`` is the MemoryNode's id (so two MemoryNodes inside the
same workflow stay isolated even if they share a scope_id). Entries are
JSON-encoded turn dicts ``{role, content, tool_calls?, tool_call_id?,
tool_name?, ts}``. We use ``LPUSH`` + ``LTRIM`` so the head is the newest
turn and the list naturally bounds at ``max_turns``.

TTL is refreshed on every write so an active conversation keeps its memory
alive even past the configured ttl_seconds.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from schemas.workflow import MemoryNode

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Turn shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Turn:
    """One conversation turn as the agent loop sees it.

    Mirrors the OpenAI tool-calling protocol so the runtime can feed turns
    straight into the LLM message array without remapping.
    """

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content, "ts": self.ts}
        if self.tool_calls is not None:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_name is not None:
            d["tool_name"] = self.tool_name
        return d

    def to_openai_message(self) -> dict[str, Any]:
        """Shape consumed directly by ``openai.chat.completions.create``."""
        if self.role == "tool":
            return {
                "role": "tool",
                "content": self.content,
                "tool_call_id": self.tool_call_id or "",
            }
        if self.role == "assistant" and self.tool_calls:
            return {
                "role": "assistant",
                "content": self.content or None,
                "tool_calls": self.tool_calls,
            }
        return {"role": self.role, "content": self.content}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Turn:
        return cls(
            role=str(d.get("role") or "user"),
            content=str(d.get("content") or ""),
            tool_calls=d.get("tool_calls"),
            tool_call_id=d.get("tool_call_id"),
            tool_name=d.get("tool_name"),
            ts=float(d.get("ts") or time.time()),
        )


# ---------------------------------------------------------------------------
# Key construction
# ---------------------------------------------------------------------------


def _scope_id_for(
    node: MemoryNode,
    *,
    session_id: UUID,
    user_id: UUID | None,
    workflow_id: UUID,
) -> str | None:
    """Resolve the scope's key suffix; ``None`` means scope can't be served.

    ``user`` scope on an anonymous session returns ``None`` — the caller
    falls back to ``session`` scope semantics for that turn.
    """
    if node.scope == "session":
        return str(session_id)
    if node.scope == "user":
        return str(user_id) if user_id else None
    if node.scope == "workflow":
        return str(workflow_id)
    return None


def _key(node: MemoryNode, scope_id: str) -> str:
    return f"egpt:mem:{node.scope}:{node.id}:{scope_id}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class MemoryStore:
    """High-level wrapper around the Redis list. Stateless; safe to instantiate per request."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def append(
        self,
        node: MemoryNode,
        turn: Turn,
        *,
        session_id: UUID,
        user_id: UUID | None,
        workflow_id: UUID,
    ) -> None:
        scope_id = _scope_id_for(
            node, session_id=session_id, user_id=user_id, workflow_id=workflow_id,
        )
        if scope_id is None:
            return  # scope not satisfiable for this caller — silently no-op
        key = _key(node, scope_id)
        payload = json.dumps(turn.to_dict(), default=str)
        # LPUSH + LTRIM keep the newest turns at the head and cap the list.
        pipe = self._redis.pipeline()
        pipe.lpush(key, payload)
        pipe.ltrim(key, 0, max(0, node.max_turns - 1))
        pipe.expire(key, node.ttl_seconds)
        await pipe.execute()

    async def read(
        self,
        node: MemoryNode,
        *,
        session_id: UUID,
        user_id: UUID | None,
        workflow_id: UUID,
    ) -> list[Turn]:
        """Returns turns in chronological order (oldest first)."""
        scope_id = _scope_id_for(
            node, session_id=session_id, user_id=user_id, workflow_id=workflow_id,
        )
        if scope_id is None:
            return []
        key = _key(node, scope_id)
        raw = await self._redis.lrange(key, 0, node.max_turns - 1)
        if not raw:
            return []
        # LPUSH stores newest first; reverse to chronological order.
        turns: list[Turn] = []
        for item in reversed(raw):
            try:
                d = json.loads(item if isinstance(item, str) else item.decode())
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                log.debug("memory_store.skip_bad_entry", error=str(exc))
                continue
            turns.append(Turn.from_dict(d))
        return turns

    async def clear(
        self,
        node: MemoryNode,
        *,
        session_id: UUID,
        user_id: UUID | None,
        workflow_id: UUID,
    ) -> int:
        scope_id = _scope_id_for(
            node, session_id=session_id, user_id=user_id, workflow_id=workflow_id,
        )
        if scope_id is None:
            return 0
        key = _key(node, scope_id)
        return int(await self._redis.delete(key) or 0)

    async def inspect(
        self,
        node: MemoryNode,
        *,
        session_id: UUID,
        user_id: UUID | None,
        workflow_id: UUID,
    ) -> dict[str, Any]:
        """Operator-facing view of one MemoryNode's current state."""
        scope_id = _scope_id_for(
            node, session_id=session_id, user_id=user_id, workflow_id=workflow_id,
        )
        if scope_id is None:
            return {"scope": node.scope, "scope_id": None, "count": 0, "ttl": -2}
        key = _key(node, scope_id)
        pipe = self._redis.pipeline()
        pipe.llen(key)
        pipe.ttl(key)
        count_raw, ttl_raw = await pipe.execute()
        return {
            "scope": node.scope,
            "scope_id": scope_id,
            "count": int(count_raw or 0),
            "ttl": int(ttl_raw),  # -1 = no expiry, -2 = key missing
            "max_turns": node.max_turns,
        }


__all__ = ["MemoryStore", "Turn"]
