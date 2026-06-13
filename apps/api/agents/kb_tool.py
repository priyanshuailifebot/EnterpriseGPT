"""Knowledge-base (RAG) tool for workflow agents.

Wraps the existing ``RAGService`` so an agent node that declares a
``knowledge_base`` tool can ground its answer in the workspace's uploaded
enterprise documents (policy PDFs, FAQs, SOPs) — with citations.

This is opt-in per node: only agents whose ``tools`` list contains a
KB slug call it, so it's used solely in workflows that need it. The helper
never raises — if Qdrant is unreachable or no documents are indexed, it
returns ``found=False`` and the agent simply answers without grounding.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

log = logging.getLogger(__name__)

# Tool slugs an agent can declare to opt into knowledge-base grounding.
KB_TOOL_SLUGS = {
    "knowledge_base",
    "knowledge_base_search",
    "search_knowledge_base",
    "kb_search",
    "kb",
    "rag",
}


def agent_uses_kb(tools: list[str] | None) -> bool:
    """True if the agent's tool list opts into knowledge-base grounding."""
    for t in tools or []:
        if str(t).strip().lower().replace("-", "_") in KB_TOOL_SLUGS:
            return True
    return False


async def kb_search(
    question: str,
    workspace_id: UUID | None,
    settings: Any,
    top_k: int = 5,
) -> dict[str, Any]:
    """Retrieve grounding context for ``question`` from the workspace KB.

    Returns a JSON-serialisable dict: ``{found, count, context, sources}``.
    Never raises — degrades to ``found=False`` so a demo/test run always
    completes even with an empty or unavailable knowledge base.
    """
    if not workspace_id or not (question or "").strip():
        return {"found": False, "context": "", "sources": [], "note": "no query / workspace"}
    try:
        from rag.retrieval_service import RAGService

        result = await RAGService(settings).query(question, workspace_id, top_k=top_k)
        chunks = result.chunks
    except Exception:  # noqa: BLE001 — KB must never break a run
        log.warning("kb_tool.query_failed", exc_info=True)
        return {"found": False, "context": "", "sources": [], "note": "knowledge base unavailable"}

    if not chunks:
        return {"found": False, "context": "", "sources": [], "note": "no matching documents"}

    lines: list[str] = []
    sources: list[dict[str, Any]] = []
    for i, ch in enumerate(chunks, start=1):
        lines.append(
            f"[{i}] {ch.document_title} (p.{ch.page_number}):\n{ch.text}"
        )
        sources.append(
            {
                "index": i,
                "title": ch.document_title,
                "page": ch.page_number,
                "score": round(float(ch.score), 3),
            }
        )
    return {
        "found": True,
        "count": len(chunks),
        "context": "\n\n".join(lines),
        "sources": sources,
    }


__all__ = ["KB_TOOL_SLUGS", "agent_uses_kb", "kb_search"]
