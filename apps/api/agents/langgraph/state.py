"""LangGraph state schemas for Phase 3 — HITL workflows and conversational dialogs."""

from __future__ import annotations

from typing import Annotated, Any, NotRequired

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class WorkflowState(TypedDict):
    """Shared execution state for HITL graphs (serialized to checkpoints)."""

    messages: Annotated[list[AnyMessage], add_messages]
    current_agent: str
    agent_outputs: dict[str, Any]
    tool_calls: list[dict[str, Any]]
    human_feedback: str | None
    hitl_checkpoint_ref: str | None
    error: str | None
    iteration_count: int
    max_iterations: int
    execution_id: str
    user_id: str
    workspace_id: str
    confidence: float


class DialogState(TypedDict):
    """Multi-turn conversational state machine."""

    messages: Annotated[list[AnyMessage], add_messages]
    detected_intent: str | None
    required_slots: list[str]
    filled_slots: dict[str, str]
    confirmation_pending: bool
    session_id: str
    escalation_count: int
    last_activity: str | None
    workspace_id: NotRequired[str]
    dialogue_phase: NotRequired[
        str
    ]  # greeting | intent_detection | slot_filling | confirmation | action_execution | done | escalation
    confidence_scratch: NotRequired[float]


class WorkflowScopingState(TypedDict):
    """Checkpoint state for NL workflow clarification — ``workflow_scoping`` intent."""

    messages: Annotated[list[AnyMessage], add_messages]
    session_id: str
    workspace_id: str
    user_id: str
    original_prompt: str
    available_tools: list[str]
    detected_intent: str
    required_slots: list[str]
    clarification_rounds: list[dict[str, Any]]
    dialogue_phase: NotRequired[
        str
    ]  # route | merge_answers | slot_filling | confirmation | idle
    ws_await: NotRequired[str]  # questions | confirmation | idle
    pending_questions: NotRequired[list[dict[str, Any]]]
    workflow_preview: NotRequired[dict[str, Any] | None]
    max_rounds: NotRequired[int]
    confidence_threshold: NotRequired[float]
    preview_before_ready: NotRequired[bool]
    clarification_api: NotRequired[dict[str, Any]]


__all__ = ["DialogState", "WorkflowScopingState", "WorkflowState"]
