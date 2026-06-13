"""LLM-backed workflow scoping prompts (shared by LangGraph clarification graph)."""

from __future__ import annotations

import json
import uuid
from typing import Any

from openai import APIError as OpenAIAPIError
from openai import APITimeoutError as OpenAIAPITimeoutError
from openai import AsyncAzureOpenAI
from pydantic import ValidationError

from core.config import Settings
from schemas.workflow import ClarificationQuestion

WORKFLOW_SCOPING_INTENT = "workflow_scoping"

# Hard ceiling for a single Azure OpenAI call inside the clarification loop.
# The OpenAI SDK default (10 minutes) is far longer than our frontend axios
# timeout, so a slow Azure call ends up as an opaque "Network Error" in the
# browser. Failing fast here lets the graph fall back to a useful state.
CLARIFIER_LLM_TIMEOUT_SECONDS = 60.0

# Conceptual buckets the clarifier targets (coverage areas, not key-value parsers).
WORKFLOW_SCOPING_REQUIRED_SLOTS = ["trigger", "inputs", "outputs", "approval", "edge_cases"]

WORKFLOW_CLARIFIER_PROMPT = """You are a senior workflow analyst for a multi-agent automation platform.

The user describes a business process in natural language. Your job is to decide whether their description is SPECIFIC enough to design a reliable DAG of specialized agents (with tools, triggers, human approvals, and outputs) WITHOUT risky guesswork.

Decision rules:
1. If the request already names concrete triggers, inputs/outputs, systems involved, success criteria, approval points, and failure/edge-case handling, it is probably ready.
2. If the request is vague, underspecified, missing operational context, or could be interpreted multiple ways, it needs clarification BEFORE any workflow graph is drafted.
3. Prefer asking clarifying questions over inventing assumptions.

When clarification is needed, output 1–4 questions that cover: how the workflow is triggered; what inputs/data sources are required; what outputs/deliverables look like; where human approval is required; and important edge cases or failure modes.

Question design:
- Prefer type \"choice\" (single select) or \"multi_choice\" over free \"text\" when you can enumerate reasonable options.
- Every question MUST include a non-empty \"why_asked\" (short rationale for the builder, shown as a tooltip).
- Include an \"id\" for each question (stable kebab-case or short slug).
- \"options\" MUST be null for \"text\", and a non-empty list of short strings for \"choice\" or \"multi_choice\"
  (ids should preferably align with thematic areas when possible: trigger, inputs, outputs, approval, edge_cases).
- \"required\" defaults true; set false only for nice-to-have details.

Strict JSON output ONLY (no markdown, no fences). Use exactly this shape:
{
  \"ready\": boolean,
  \"confidence\": number between 0 and 1,
  \"reasoning\": string,
  \"questions\": [
    {
      \"id\": string,
      \"question\": string,
      \"type\": \"text\" | \"choice\" | \"multi_choice\",
      \"options\": string[] | null,
      \"why_asked\": string,
      \"required\": boolean
    }
  ]
}

When ready is true, \"questions\" should be an empty array. When ready is false, include 1–4 questions following the rules above."""


def coerce_confidence(raw: Any) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _azure_client(settings: Settings) -> AsyncAzureOpenAI:
    ep = settings.AZURE_OPENAI_ENDPOINT.strip().rstrip("/")
    key = settings.AZURE_OPENAI_API_KEY.strip()
    if not ep or not key:
        raise RuntimeError(
            "Azure OpenAI endpoint or API key missing (set AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY)."
        )
    return AsyncAzureOpenAI(
        azure_endpoint=ep,
        api_key=key,
        api_version=settings.AZURE_OPENAI_API_VERSION,
        timeout=CLARIFIER_LLM_TIMEOUT_SECONDS,
    )


async def call_workflow_clarifier_async(
    settings: Settings,
    *,
    user_input: str,
    available_tools: list[str],
    round_number: int,
    max_rounds: int,
    previous_answers: list[dict[str, Any]],
    llm_client: AsyncAzureOpenAI | None = None,
) -> dict[str, Any]:
    tools_prompt = ", ".join(available_tools) if available_tools else "(none)"
    prev_blob = json.dumps(previous_answers, ensure_ascii=False, indent=2) if previous_answers else "[]"
    user_msg = (
        f"The workflow must eventually cover these scoping dimensions: "
        f"{', '.join(WORKFLOW_SCOPING_REQUIRED_SLOTS)}.\n"
        f"Available tools: {tools_prompt}\n"
        f"Clarification round: {round_number} of {max_rounds}\n"
        f"Prior Q→A context (may be empty):\n{prev_blob}\n\n"
        f"User request:\n{user_input.strip()}"
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": WORKFLOW_CLARIFIER_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    deployment = settings.AZURE_OPENAI_DEPLOYMENT or settings.AZURE_OPENAI_DEFAULT_MODEL
    client = llm_client or _azure_client(settings)
    try:
        completion = await client.chat.completions.create(
            model=deployment,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=messages,
        )
    except OpenAIAPITimeoutError as exc:
        raise TimeoutError(
            f"Azure OpenAI clarifier call timed out after "
            f"{CLARIFIER_LLM_TIMEOUT_SECONDS:.0f}s"
        ) from exc
    except OpenAIAPIError as exc:
        raise RuntimeError(str(exc)) from exc
    content = completion.choices[0].message.content or ""
    if not content.strip():
        raise RuntimeError("clarifier returned empty content")
    trimmed = content.strip()
    try:
        data = json.loads(trimmed)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid clarifier JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


def normalize_questions_payload(raw_questions: Any) -> list[ClarificationQuestion]:
    if not isinstance(raw_questions, list):
        return []
    out: list[ClarificationQuestion] = []
    for item in raw_questions[:4]:
        if not isinstance(item, dict):
            continue
        qid = str(item.get("id") or "").strip() or uuid.uuid4().hex[:12]
        qtype = item.get("type") or "text"
        if qtype not in ("text", "choice", "multi_choice"):
            qtype = "text"
        opts = item.get("options")
        if opts is not None and not isinstance(opts, list):
            opts = None
        try:
            cq = ClarificationQuestion(
                id=qid,
                question=str(item.get("question") or "Please clarify this step."),
                type=qtype,  # type: ignore[arg-type]
                options=[str(o) for o in opts] if opts else None,
                why_asked=str(
                    item.get("why_asked") or "This reduces ambiguity in the workflow design."
                ),
                required=bool(item.get("required", True)),
            )
        except ValidationError:
            continue
        if cq.type in ("choice", "multi_choice"):
            if not cq.options:
                cq = cq.model_copy(update={"options": ["Option A", "Option B"]})
        else:
            cq = cq.model_copy(update={"options": None})
        out.append(cq)
    return out


def fallback_questions(reason: str) -> list[ClarificationQuestion]:
    return [
        ClarificationQuestion(
            id="trigger",
            question="How should this workflow be started?",
            type="choice",
            options=["Manual run", "On a schedule", "When an external event arrives"],
            why_asked=reason or "Triggers determine agents and tool wiring.",
        ),
        ClarificationQuestion(
            id="outputs",
            question="What is the primary output or deliverable?",
            type="text",
            options=None,
            why_asked="Outputs anchor the final agent responsibilities.",
        ),
    ]


def build_augmented_prompt(original_prompt: str, rounds: list[dict[str, Any]]) -> str:
    lines = [original_prompt.strip(), "", "Clarifications:"]
    for rnd in rounds:
        questions = rnd.get("questions") or []
        answers = {str(a.get("question_id")): a.get("answer") for a in (rnd.get("answers") or [])}
        for q in questions:
            qid = str(q.get("id") or "")
            qtext = str(q.get("question") or "")
            ans = answers.get(qid)
            if ans is None:
                continue
            if isinstance(ans, list):
                ans_str = ", ".join(str(x) for x in ans)
            else:
                ans_str = str(ans)
            lines.append(f"- Q: {qtext}")
            lines.append(f"  A: {ans_str}")
    return "\n".join(lines).strip()


def flatten_answers(rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for rnd in rounds:
        questions = rnd.get("questions") or []
        answers = {str(a.get("question_id")): a.get("answer") for a in (rnd.get("answers") or [])}
        for q in questions:
            qid = str(q.get("id") or "")
            qtext = str(q.get("question") or "")
            if qid not in answers:
                continue
            flat.append({"question": qtext, "answer": answers[qid]})
    return flat


ANSWERS_PREFIX = "__EGPT_CLAR_ANSWERS__:"


def encode_clarification_answers_payload(answers: list[dict[str, Any]], *, force_proceed: bool) -> str:
    return ANSWERS_PREFIX + json.dumps(
        {"answers": answers, "force_proceed": force_proceed},
        ensure_ascii=False,
    )


def parse_clarification_answers_message(text: str) -> tuple[list[dict[str, Any]], bool] | None:
    t = text.strip()
    if not t.startswith(ANSWERS_PREFIX):
        return None
    raw = t[len(ANSWERS_PREFIX) :].strip()
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(blob, dict):
        return None
    ans = blob.get("answers")
    if not isinstance(ans, list):
        return None
    fp = bool(blob.get("force_proceed", False))
    norm: list[dict[str, Any]] = []
    for item in ans:
        if not isinstance(item, dict):
            continue
        qid = str(item.get("question_id") or "").strip()
        if not qid:
            continue
        norm.append({"question_id": qid, "answer": item.get("answer")})
    return norm, fp
