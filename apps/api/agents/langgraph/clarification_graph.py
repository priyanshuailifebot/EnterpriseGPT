"""LangGraph for NL workflow clarification — ``workflow_scoping`` intent (Phase 3)."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.constants import END, START
from langgraph.graph import StateGraph

from agents.langgraph.state import WorkflowScopingState
from agents.langgraph.workflow_scoping_llm import (
    WORKFLOW_SCOPING_INTENT,
    WORKFLOW_SCOPING_REQUIRED_SLOTS,
    build_augmented_prompt,
    call_workflow_clarifier_async,
    coerce_confidence,
    encode_clarification_answers_payload,
    fallback_questions,
    flatten_answers,
    normalize_questions_payload,
    parse_clarification_answers_message,
)
from core.config import Settings
from schemas.workflow import ClarificationAnswer, ClarificationQuestion
from services.workflow_interpreter import WorkflowInterpretationError, WorkflowInterpreter

log = logging.getLogger(__name__)


def _last_human_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for m in reversed(messages):
        if isinstance(m, HumanMessage) and m.content:
            return str(m.content).strip()
    return ""


def _validate_required_vs_answers(questions: list[dict[str, Any]], answers: list[dict[str, Any]]) -> None:
    by_id: dict[str, Any] = {}
    for item in answers:
        if isinstance(item, dict) and item.get("question_id") is not None:
            by_id[str(item["question_id"])] = item.get("answer")
    for q in questions:
        if not q.get("required", True):
            continue
        qid = str(q.get("id") or "")
        if qid not in by_id:
            raise ValueError(f"missing answer for required question: {qid}")
        val = by_id[qid]
        if isinstance(val, str) and not val.strip():
            raise ValueError(f"empty answer for required question: {qid}")
        if isinstance(val, list) and not val:
            raise ValueError(f"empty answer for required question: {qid}")


def _preview_confirmation_questions(defn_dict: dict[str, Any]) -> list[ClarificationQuestion]:
    name = defn_dict.get("name") or "workflow"
    agents = defn_dict.get("agents") or []
    n_agents = len(agents)
    first_id = ""
    if agents and isinstance(agents[0], dict):
        first_id = str(agents[0].get("id") or "?")
    summary = f"{name}: {n_agents} agents; starts with `{first_id}`"
    return [
        ClarificationQuestion(
            id="_workflow_preview_confirm",
            question=(
                "Here is a proposed WorkflowDefinition from your clarified description "
                f"({summary}). Proceed with this design as the interpreter output?"
            ),
            type="choice",
            options=["Yes, proceed", "No, refine the scope"],
            why_asked="You confirm before we finalize this preview.",
            required=True,
        )
    ]


def _confirmation_yes(answer: Any) -> bool:
    if isinstance(answer, list):
        answer = answer[0] if answer else ""
    s = str(answer).strip().lower()
    if s in {"yes", "y", "true", "approve", "1"}:
        return True
    return "yes" in s or "proceed" in s


def _merge_terminal_ready(augmented: str, rounds_used: int) -> dict[str, Any]:
    return {
        "clarification_api": {
            "kind": "ready",
            "augmented_prompt": augmented,
            "rounds_used": rounds_used,
        },
        "messages": [AIMessage(content="Clarification complete — ready for final interpretation.")],
    }


def build_clarification_graph(settings: Settings, interpreter: WorkflowInterpreter) -> StateGraph:
    preview_default = settings.CLARIFICATION_PREVIEW_BEFORE_READY

    async def slot_filling_node(state: WorkflowScopingState) -> dict[str, Any]:
        ws_await = state.get("ws_await") or "idle"
        if ws_await == "confirmation":
            return {}

        original = state.get("original_prompt") or ""
        tools = list(state.get("available_tools") or [])
        rounds = list(state.get("clarification_rounds") or [])
        max_r = int(state.get("max_rounds") or settings.CLARIFICATION_MAX_ROUNDS)
        thresh = float(state.get("confidence_threshold") or settings.CLARIFICATION_CONFIDENCE_THRESHOLD)
        pv = state.get("preview_before_ready")
        preview_b = bool(pv if pv is not None else preview_default)

        prior = flatten_answers(rounds)
        round_number = max(1, len(rounds))

        log.debug("workflow_scoping.slot_filling round=%s", round_number)

        data = await call_workflow_clarifier_async(
            settings,
            user_input=original,
            available_tools=tools,
            round_number=round_number,
            max_rounds=max_r,
            previous_answers=prior,
        )
        ready = bool(data.get("ready"))
        confidence = coerce_confidence(data.get("confidence"))
        reasoning = str(data.get("reasoning") or "")
        augmented = build_augmented_prompt(original, rounds)

        if ready and confidence >= thresh:
            if not preview_b:
                return {
                    "clarification_api": {"kind": "analyze_none"},
                    "ws_await": "idle",
                    "pending_questions": [],
                    "dialogue_phase": "idle",
                    "messages": [
                        AIMessage(content="Interpretation-ready without extra clarification rounds.")
                    ],
                }
            try:
                defn = await interpreter.interpret(user_input=augmented, available_tools=tools)
                dumped = defn.model_dump(mode="json")
                ask = _preview_confirmation_questions(dumped)
                return {
                    "workflow_preview": dumped,
                    "ws_await": "confirmation",
                    "pending_questions": [q.model_dump(mode="json") for q in ask],
                    "dialogue_phase": "confirmation",
                    "messages": [
                        AIMessage(content=ask[0].question),
                    ],
                    "clarification_api": {
                        "kind": "needs_clarification",
                        "session_id": state.get("session_id"),
                        "questions": ask,
                        "round_number": max(1, len(rounds) + 1),
                        "original_prompt": original,
                    },
                }
            except WorkflowInterpretationError as exc:
                log.warning("workflow_scoping.preview_interpret_failed", error=str(exc))
                return {
                    "clarification_api": {"kind": "error", "message": str(exc)},
                    "messages": [AIMessage(content=f"Interpretation preview failed: {exc}")],
                }

        questions = normalize_questions_payload(data.get("questions"))
        if not questions:
            questions = fallback_questions(reasoning)

        new_round = {
            "questions": [q.model_dump(mode="json") for q in questions],
            "answers": [],
        }
        merged_rounds = rounds + [new_round]
        lines = "\n".join(f"- [{q.id}] {q.question}" for q in questions[:4])

        return {
            "clarification_rounds": merged_rounds,
            "pending_questions": [q.model_dump(mode="json") for q in questions],
            "ws_await": "questions",
            "dialogue_phase": "questions",
            "messages": [AIMessage(content=f"Workflow scoping — please answer:\n{lines}")],
            "clarification_api": {
                "kind": "needs_clarification",
                "session_id": state.get("session_id"),
                "questions": questions,
                "round_number": len(merged_rounds),
                "original_prompt": original,
            },
        }

    async def merge_answers_node(state: WorkflowScopingState) -> dict[str, Any]:
        last = _last_human_text(state.get("messages"))
        parsed = parse_clarification_answers_message(last)
        rounds = list(state.get("clarification_rounds") or [])
        max_r = int(state.get("max_rounds") or settings.CLARIFICATION_MAX_ROUNDS)
        original = state.get("original_prompt") or ""
        tools = list(state.get("available_tools") or [])
        pv = state.get("preview_before_ready")
        preview_b = bool(pv if pv is not None else preview_default)

        if parsed is None:
            raise ValueError("answers payload missing or malformed")

        raw_answers, force_proceed = parsed
        if not rounds:
            raise ValueError("no clarification rounds in session")

        latest = rounds[-1]
        questions_raw = list(latest.get("questions") or [])

        if not force_proceed:
            _validate_required_vs_answers(questions_raw, raw_answers)

        merged_latest = dict(latest)
        merged_latest["answers"] = list(raw_answers)
        merged_rounds = rounds[:-1] + [merged_latest]

        rounds_used = len(merged_rounds)
        if force_proceed or rounds_used >= max_r:
            aug = build_augmented_prompt(original, merged_rounds)
            merged_out = _merge_terminal_ready(aug, rounds_used)
            return {
                **merged_out,
                "clarification_rounds": merged_rounds,
                "pending_questions": [],
                "ws_await": "idle",
                "workflow_preview": None,
            }

        # The user has already supplied answers. If the "do we need another
        # round?" LLM call fails (e.g. Azure timeout, transient API error)
        # we should not throw away their answers — fall through to a
        # terminal-ready state and let the final interpreter take over.
        try:
            data = await call_workflow_clarifier_async(
                settings,
                user_input=original,
                available_tools=tools,
                round_number=min(rounds_used + 1, max_r),
                max_rounds=max_r,
                previous_answers=flatten_answers(merged_rounds),
            )
        except (TimeoutError, RuntimeError) as exc:
            log.warning(
                "workflow_scoping.merge_answers_clarifier_failed", extra={"error": str(exc)}
            )
            aug = build_augmented_prompt(original, merged_rounds)
            merged_out = _merge_terminal_ready(aug, rounds_used)
            return {
                **merged_out,
                "clarification_rounds": merged_rounds,
                "pending_questions": [],
                "ws_await": "idle",
                "workflow_preview": None,
            }
        ready = bool(data.get("ready"))
        confidence = coerce_confidence(data.get("confidence"))
        thresh = float(state.get("confidence_threshold") or settings.CLARIFICATION_CONFIDENCE_THRESHOLD)
        augmented = build_augmented_prompt(original, merged_rounds)

        if ready and confidence >= thresh:
            if not preview_b:
                mr = _merge_terminal_ready(augmented, rounds_used)
                return {
                    **mr,
                    "clarification_rounds": merged_rounds,
                    "ws_await": "idle",
                    "pending_questions": [],
                    "workflow_preview": None,
                }
            try:
                defn = await interpreter.interpret(user_input=augmented, available_tools=tools)
                dumped = defn.model_dump(mode="json")
                ask = _preview_confirmation_questions(dumped)
                return {
                    "clarification_rounds": merged_rounds,
                    "workflow_preview": dumped,
                    "ws_await": "confirmation",
                    "pending_questions": [q.model_dump(mode="json") for q in ask],
                    "dialogue_phase": "confirmation",
                    "messages": [AIMessage(content=ask[0].question)],
                    "clarification_api": {
                        "kind": "needs_clarification",
                        "session_id": state.get("session_id"),
                        "questions": ask,
                        "round_number": len(merged_rounds) + 1,
                        "original_prompt": original,
                    },
                }
            except WorkflowInterpretationError as exc:
                return {
                    "clarification_rounds": merged_rounds,
                    "clarification_api": {"kind": "error", "message": str(exc)},
                    "messages": [AIMessage(content=f"Interpretation preview failed: {exc}")],
                }

        next_qs = normalize_questions_payload(data.get("questions"))
        if not next_qs:
            next_qs = fallback_questions(str(data.get("reasoning") or ""))

        nr = {"questions": [q.model_dump(mode="json") for q in next_qs], "answers": []}
        final_rounds = merged_rounds + [nr]
        lines = "\n".join(f"- [{q.id}] {q.question}" for q in next_qs[:4])
        return {
            "clarification_rounds": final_rounds,
            "pending_questions": [q.model_dump(mode="json") for q in next_qs],
            "ws_await": "questions",
            "dialogue_phase": "questions",
            "messages": [AIMessage(content=f"Workflow scoping — follow-up:\n{lines}")],
            "clarification_api": {
                "kind": "needs_clarification",
                "session_id": state.get("session_id"),
                "questions": next_qs,
                "round_number": len(final_rounds),
                "original_prompt": original,
            },
        }

    async def confirmation_node(state: WorkflowScopingState) -> dict[str, Any]:
        last = _last_human_text(state.get("messages"))
        parsed = parse_clarification_answers_message(last)
        original = state.get("original_prompt") or ""
        rounds = list(state.get("clarification_rounds") or [])
        preview = state.get("workflow_preview")

        if not isinstance(preview, dict):
            return {
                "clarification_api": {"kind": "error", "message": "missing workflow preview in session"},
            }

        if parsed is None:
            ask = _preview_confirmation_questions(preview)
            return {
                "messages": [
                    AIMessage(content="Reply using the structured answers format (same envelope as rounds)."),
                ],
                "clarification_api": {
                    "kind": "needs_clarification",
                    "session_id": state.get("session_id"),
                    "questions": ask,
                    "round_number": max(1, len(rounds) + 1),
                    "original_prompt": original,
                },
            }

        raw_answers, _force = parsed
        if not raw_answers:
            return {"clarification_api": {"kind": "error", "message": "confirmation answers missing"}}

        verdict = raw_answers[0].get("answer")
        yes = _confirmation_yes(verdict)

        if yes:
            aug = build_augmented_prompt(original, rounds)
            return {
                "ws_await": "idle",
                "workflow_preview": None,
                "pending_questions": [],
                "clarification_api": {"kind": "ready", "augmented_prompt": aug, "rounds_used": len(rounds)},
                "messages": [AIMessage(content="Confirmed — clarification context finalized.")],
            }

        tools = list(state.get("available_tools") or [])
        max_r = int(state.get("max_rounds") or settings.CLARIFICATION_MAX_ROUNDS)
        rounds_used = len(rounds)
        if rounds_used >= max_r:
            aug = build_augmented_prompt(original, rounds)
            return {
                "ws_await": "idle",
                "workflow_preview": None,
                "clarification_api": {"kind": "ready", "augmented_prompt": aug, "rounds_used": rounds_used},
                "messages": [AIMessage(content="Max rounds reached; proceeding with current scope.")],
            }

        hint = (
            f"{original}\n\n(User rejected prior workflow preview — refine operational scope.)"
        )
        try:
            data = await call_workflow_clarifier_async(
                settings,
                user_input=hint,
                available_tools=tools,
                round_number=min(rounds_used + 1, max_r),
                max_rounds=max_r,
                previous_answers=flatten_answers(rounds),
            )
        except (TimeoutError, RuntimeError) as exc:
            log.warning(
                "workflow_scoping.confirmation_refine_failed",
                extra={"error": str(exc)},
            )
            aug = build_augmented_prompt(original, rounds)
            return {
                "ws_await": "idle",
                "workflow_preview": None,
                "pending_questions": [],
                "clarification_api": {
                    "kind": "ready",
                    "augmented_prompt": aug,
                    "rounds_used": len(rounds),
                },
                "messages": [
                    AIMessage(
                        content=(
                            "Refine step unavailable (LLM error); proceeding with "
                            "current scope."
                        )
                    )
                ],
            }
        qs = normalize_questions_payload(data.get("questions"))
        if not qs:
            qs = fallback_questions(str(data.get("reasoning") or "refining scope"))
        refined_round = {"questions": [q.model_dump(mode="json") for q in qs], "answers": []}
        refined = rounds + [refined_round]
        lines = "\n".join(f"- [{q.id}] {q.question}" for q in qs[:4])
        return {
            "clarification_rounds": refined,
            "workflow_preview": None,
            "ws_await": "questions",
            "pending_questions": [q.model_dump(mode="json") for q in qs],
            "dialogue_phase": "questions",
            "messages": [AIMessage(content=f"Okay — refining scope:\n{lines}")],
            "clarification_api": {
                "kind": "needs_clarification",
                "session_id": state.get("session_id"),
                "questions": qs,
                "round_number": len(refined),
                "original_prompt": original,
            },
        }

    def route_entry(state: WorkflowScopingState) -> str:
        ws_await = state.get("ws_await") or "idle"

        if ws_await == "confirmation":
            return "confirmation"

        last = _last_human_text(state.get("messages"))

        if parse_clarification_answers_message(last) is not None:
            return "merge_answers"

        return "slot_filling"

    builder = StateGraph(WorkflowScopingState)
    builder.add_node("slot_filling", slot_filling_node)
    builder.add_node("merge_answers", merge_answers_node)
    builder.add_node("confirmation", confirmation_node)

    builder.add_conditional_edges(
        START,
        route_entry,
        {
            "slot_filling": "slot_filling",
            "merge_answers": "merge_answers",
            "confirmation": "confirmation",
        },
    )
    builder.add_edge("slot_filling", END)
    builder.add_edge("merge_answers", END)
    builder.add_edge("confirmation", END)

    return builder


def initial_workflow_scoping_payload(
    *,
    session_id: str,
    workspace_id: str,
    user_id: str,
    original_prompt: str,
    available_tools: list[str],
    settings: Settings,
) -> dict[str, Any]:
    payload: WorkflowScopingState = {
        "messages": [],
        "session_id": session_id,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "original_prompt": original_prompt.strip(),
        "available_tools": list(available_tools),
        "detected_intent": WORKFLOW_SCOPING_INTENT,
        "required_slots": list(WORKFLOW_SCOPING_REQUIRED_SLOTS),
        "clarification_rounds": [],
        "dialogue_phase": "slot_filling",
        "ws_await": "idle",
        "pending_questions": [],
        "workflow_preview": None,
        "max_rounds": settings.CLARIFICATION_MAX_ROUNDS,
        "confidence_threshold": settings.CLARIFICATION_CONFIDENCE_THRESHOLD,
        "preview_before_ready": settings.CLARIFICATION_PREVIEW_BEFORE_READY,
    }
    return payload


def answers_turn_message(
    answers: list[ClarificationAnswer],
    *,
    force_proceed: bool,
) -> HumanMessage:
    payload = [{"question_id": a.question_id, "answer": a.answer} for a in answers]
    return HumanMessage(
        content=encode_clarification_answers_payload(payload, force_proceed=force_proceed)
    )


__all__ = [
    "answers_turn_message",
    "build_clarification_graph",
    "initial_workflow_scoping_payload",
]
