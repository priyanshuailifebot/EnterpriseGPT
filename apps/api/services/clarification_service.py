"""Workflow NL clarification — public API delegates to LangGraph checkpoints (Phase 3)."""

from __future__ import annotations

import uuid
from typing import Any, NamedTuple
from uuid import UUID

from langchain_core.messages import HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession

from agents.langgraph.clarification_graph import answers_turn_message
from agents.langgraph.service import LangGraphService
from core.config import Settings
from schemas.workflow import ClarificationAnswer, ClarificationQuestion, NeedsClarificationResponse
from services.clarification_exceptions import ClarificationAccessDeniedError, ClarificationSessionNotFoundError

# Legacy constants (backward compatible for tooling / imports)
from agents.langgraph.workflow_scoping_llm import WORKFLOW_CLARIFIER_PROMPT

SESSION_KEY_PREFIX = "clarification:"
MAX_ROUNDS = 3
SESSION_TTL_SECONDS = 1800
CONFIDENCE_THRESHOLD = 0.75


def _questions_from_blob(raw: Any) -> list[ClarificationQuestion]:
    if not isinstance(raw, list):
        raise TypeError("questions payload must be a list")
    out: list[ClarificationQuestion] = []
    for item in raw:
        if isinstance(item, ClarificationQuestion):
            out.append(item)
        elif isinstance(item, dict):
            out.append(ClarificationQuestion.model_validate(item))
        else:
            raise TypeError(f"unexpected question element: {type(item)!r}")
    return out


class ClarificationReady(NamedTuple):
    augmented_prompt: str
    rounds_used: int


class ClarificationService:
    def __init__(
        self,
        settings: Settings,
        *,
        langgraph_service: LangGraphService,
    ) -> None:
        self._lg = langgraph_service

    async def resolve_workspace_for_session(self, session_id: str, user_id: UUID) -> UUID:
        meta = await self._lg.workflow_scoping_get_meta(session_id)
        if meta is None:
            raise ClarificationSessionNotFoundError(session_id)
        ws_s, uid_s = meta
        if uid_s != str(user_id):
            raise ClarificationAccessDeniedError()
        return UUID(ws_s)

    async def analyze_initial(
        self,
        _db: AsyncSession,
        prompt: str,
        available_tools: list[str],
        *,
        workspace_id: UUID,
        user_id: UUID,
    ) -> NeedsClarificationResponse | None:
        _ = _db
        sid = uuid.uuid4().hex
        blob = await self._lg.invoke_workflow_clarification(
            HumanMessage(content=prompt.strip()),
            session_id=sid,
            workspace_id=workspace_id,
            user_id=user_id,
            available_tools=available_tools,
            is_new_analysis=True,
            original_prompt=prompt.strip(),
        )
        return self._map_analyze(blob, sid, prompt.strip())

    def _map_analyze(self, blob: dict[str, Any], sid: str, original_prompt: str) -> NeedsClarificationResponse | None:
        kind = blob.get("kind")
        if kind == "analyze_none":
            return None
        if kind == "needs_clarification":
            qs = _questions_from_blob(blob["questions"])
            return NeedsClarificationResponse(
                session_id=str(blob.get("session_id") or sid),
                questions=qs,
                round_number=int(blob["round_number"]),
                original_prompt=str(blob.get("original_prompt") or original_prompt),
            )
        if kind == "error":
            raise RuntimeError(str(blob.get("message") or "clarification failed"))
        raise RuntimeError(f"unexpected clarification result: {kind!r}")

    async def submit_answers(
        self,
        _db: AsyncSession,
        session_id: str,
        answers: list[ClarificationAnswer],
        available_tools: list[str],
        *,
        user_id: UUID,
        force_proceed: bool = False,
    ) -> NeedsClarificationResponse | ClarificationReady:
        _ = _db
        blob = await self._lg.invoke_workflow_clarification(
            answers_turn_message(answers, force_proceed=force_proceed),
            session_id=session_id,
            workspace_id=None,
            user_id=None,
            available_tools=available_tools,
            is_new_analysis=False,
            original_prompt=None,
        )
        return self._map_submit(blob)

    def _map_submit(self, blob: dict[str, Any]) -> NeedsClarificationResponse | ClarificationReady:
        kind = blob.get("kind")
        if kind == "ready":
            return ClarificationReady(
                augmented_prompt=str(blob["augmented_prompt"]),
                rounds_used=int(blob["rounds_used"]),
            )
        if kind == "needs_clarification":
            qs = _questions_from_blob(blob["questions"])
            return NeedsClarificationResponse(
                session_id=str(blob["session_id"]),
                questions=qs,
                round_number=int(blob["round_number"]),
                original_prompt=str(blob["original_prompt"]),
            )
        if kind == "error":
            raise RuntimeError(str(blob.get("message") or "clarification failed"))
        raise RuntimeError(f"unexpected clarification result: {kind!r}")


__all__ = [
    "CONFIDENCE_THRESHOLD",
    "ClarificationAccessDeniedError",
    "ClarificationReady",
    "ClarificationService",
    "ClarificationSessionNotFoundError",
    "MAX_ROUNDS",
    "SESSION_KEY_PREFIX",
    "SESSION_TTL_SECONDS",
    "WORKFLOW_CLARIFIER_PROMPT",
]
