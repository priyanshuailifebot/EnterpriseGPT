"""LLM clarifier JSON fixtures for unit tests."""

VAGUE_ROUND_1 = {
    "ready": False,
    "confidence": 0.35,
    "reasoning": "The request lacks triggers, inputs, and outputs.",
    "questions": [
        {
            "id": "trigger",
            "question": "How should this workflow start?",
            "type": "choice",
            "options": ["Manual", "Scheduled", "Event-driven"],
            "why_asked": "Triggers determine the first agent and tool wiring.",
            "required": True,
        },
        {
            "id": "deliverable",
            "question": "What is the primary deliverable?",
            "type": "text",
            "options": None,
            "why_asked": "The final agent must know what success looks like.",
            "required": True,
        },
    ],
}

STILL_AMBIGUOUS_ROUND_2 = {
    "ready": False,
    "confidence": 0.5,
    "reasoning": "Approval policy is still unclear.",
    "questions": [
        {
            "id": "approval",
            "question": "Where is human approval required?",
            "type": "choice",
            "options": ["Before send", "After draft", "None"],
            "why_asked": "Maps to human_checkpoints in the workflow graph.",
            "required": True,
        },
    ],
}

SPECIFIC_READY = {
    "ready": True,
    "confidence": 0.92,
    "reasoning": "Enough detail to model agents and dependencies.",
    "questions": [],
}
