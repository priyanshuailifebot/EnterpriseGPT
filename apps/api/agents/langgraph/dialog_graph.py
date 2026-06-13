"""Conversational LangGraph — one ``dialog_step`` node with explicit phase machine.

Each HTTP turn appends a user message and invokes the graph; Redis/memory
checkpointer keeps ``thread_id = session_id`` across turns.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.constants import END, START
from langgraph.graph import StateGraph

from agents.langgraph.state import DialogState
from core.config import Settings

log = logging.getLogger(__name__)

_INTENTS: list[tuple[str, list[str], list[str]]] = [
    ("book_meeting", ["meet", "calendar", "schedule"], ["date", "time"]),
    ("summarize_ticket", ["ticket", "jira", "issue"], ["ticket_id"]),
]


def route_by_confidence(confidence: float) -> str:
    if confidence >= 0.8:
        return "execute"
    if confidence >= 0.5:
        return "clarify"
    return "escalate"


def build_dialog_graph(
    settings: Settings,
    *,
    on_escalate: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> StateGraph:
    """Return an uncompiled dialog graph (single step node)."""

    async def dialog_step(state: DialogState) -> dict[str, Any]:
        phase = state.get("dialogue_phase") or "greeting"
        esc = int(state.get("escalation_count") or 0)
        msgs_in = list(state.get("messages") or [])

        def last_human() -> str:
            for m in reversed(msgs_in):
                if isinstance(m, HumanMessage) and m.content:
                    return str(m.content)
            return ""

        last_u = last_human()
        low = last_u.lower()

        out_ai: list[AIMessage] = []
        updates: dict[str, Any] = {"last_activity": datetime.now(UTC).isoformat()}

        if phase == "greeting":
            out_ai.append(
                AIMessage(
                    content="Hello — what would you like to do today? "
                    "(You can ask to book a meeting or summarize a ticket.)"
                )
            )
            updates["dialogue_phase"] = "intent_detection"
            updates["escalation_count"] = esc
            phase = "intent_detection"

        if phase == "intent_detection":
            best = "unknown"
            conf = 0.3
            required: list[str] = []
            for name, hints, slots in _INTENTS:
                hits = sum(1 for h in hints if h in low)
                if hits:
                    sc = min(1.0, 0.55 + 0.15 * hits)
                    if sc > conf:
                        conf = sc
                        best = name
                        required = list(slots)
            updates["detected_intent"] = best
            updates["required_slots"] = required

            if conf >= 0.8:
                updates["dialogue_phase"] = "slot_filling"
                updates["filled_slots"] = dict(state.get("filled_slots") or {})
                out_ai.append(
                    AIMessage(
                        content=f"I understood intent `{best}`. We'll collect required fields."
                    )
                )
                return {"messages": out_ai, **updates}

            if conf >= 0.5:
                updates["dialogue_phase"] = "intent_detection"
                updates["escalation_count"] = esc + 1
                out_ai.append(
                    AIMessage(
                        content="Could you add a bit more detail about the task? "
                        f"(Clarification {esc + 1}/3)"
                    )
                )
                return {"messages": out_ai, **updates}

            if esc >= 3:
                updates["dialogue_phase"] = "escalation"
                if on_escalate:
                    await on_escalate(
                        {
                            "session_id": state.get("session_id"),
                            "workspace_id": state.get("workspace_id"),
                            "reason": "low_confidence_after_clarifications",
                            "confidence": conf,
                            "text": last_u,
                            "intent": best,
                        }
                    )
                out_ai.append(
                    AIMessage(content="I'll route this to a human teammate — escalation logged.")
                )
                return {"messages": out_ai, **updates}

            updates["dialogue_phase"] = "intent_detection"
            updates["escalation_count"] = esc + 1
            out_ai.append(
                AIMessage(
                    content="Could you add a bit more detail about the task? "
                    f"(Clarification {esc + 1}/3)"
                )
            )
            return {"messages": out_ai, **updates}

        if phase == "slot_filling":
            intent = state.get("detected_intent") or "unknown"
            required = list(state.get("required_slots") or [])
            kv = dict(state.get("filled_slots") or {})

            for part in re.split(r"[;\n]+", last_u):
                if ":" in part:
                    k_, v_ = part.split(":", 1)
                    kk, vv = k_.strip(), v_.strip()
                    if kk and vv:
                        kv[kk] = vv
            for slot in ("date", "time", "ticket_id"):
                mm = re.search(rf"{slot}\s*(is|=)\s*([^,\.\n]+)", low)
                if mm and slot not in kv:
                    kv[slot] = mm.group(2).strip()

            missing = [s for s in required if s not in kv or not kv.get(s)]
            if missing:
                updates["filled_slots"] = kv
                updates["dialogue_phase"] = "slot_filling"
                out_ai.append(AIMessage(content=f"Please provide: {', '.join(missing)}."))
                return {"messages": out_ai, **updates}

            updates["filled_slots"] = kv
            updates["dialogue_phase"] = "confirmation"
            updates["confirmation_pending"] = True
            summary = "; ".join(f"{k}:{kv[k]}" for k in sorted(kv))
            out_ai.append(
                AIMessage(
                    content=f"Summary for `{intent}`: {summary}. "
                    "Reply **YES** to run the action or **NO** to adjust."
                )
            )
            return {"messages": out_ai, **updates}

        if phase == "confirmation":
            affirm = (
                low.strip() == "yes"
                or low.strip().startswith("yes ")
                or "confirm" in low
            )
            deny = low.strip() == "no" or low.startswith("no ")
            if affirm:
                intent = state.get("detected_intent")
                payload = {
                    "intent": intent,
                    "slots": dict(state.get("filled_slots") or {}),
                }
                out_ai.append(
                    AIMessage(
                        content=f"Executing `{intent}` (stub): {json.dumps(payload)}",
                    )
                )
                updates["confirmation_pending"] = False
                updates["dialogue_phase"] = "done"
                return {"messages": out_ai, **updates}
            if deny:
                updates["dialogue_phase"] = "slot_filling"
                out_ai.append(AIMessage(content="Okay — what should we change?"))
                return {"messages": out_ai, **updates}
            updates["dialogue_phase"] = "confirmation"
            out_ai.append(AIMessage(content="Please answer YES or NO to confirm."))
            return {"messages": out_ai, **updates}

        if phase == "escalation":
            updates["dialogue_phase"] = "done"
            return {"messages": out_ai, **updates}

        if phase == "done":
            out_ai.append(AIMessage(content="Session step complete."))
            return {"messages": out_ai, **updates}

        updates["dialogue_phase"] = "intent_detection"
        return {"messages": out_ai, **updates}

    builder = StateGraph(DialogState)
    builder.add_node("dialog_step", dialog_step)
    builder.add_edge(START, "dialog_step")
    builder.add_edge("dialog_step", END)
    _ = settings
    return builder


__all__ = ["build_dialog_graph", "route_by_confidence"]
