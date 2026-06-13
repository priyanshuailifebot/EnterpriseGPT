"""Retrieve chunks and generate grounded answers with citations."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from uuid import UUID

from langfuse import get_client
from openai import AsyncAzureOpenAI

from core.config import Settings, get_settings
from core.redis import get_redis
from core.tracing import observe
from rag.embeddings import EmbeddingService
from rag.vector_store import QdrantService, ScoredChunk


@dataclass
class Citation:
    index: int
    document_title: str
    page_number: int
    chunk_index: int
    document_id: UUID


@dataclass
class CitedAnswer:
    answer: str
    citations: list[Citation]
    confidence: float
    unanswerable: bool


@dataclass
class RAGResult:
    chunks: list[ScoredChunk]
    query_embedding_time_ms: float


class RAGService:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @staticmethod
    def _dedupe_by_page(chunks: list[ScoredChunk]) -> list[ScoredChunk]:
        best: dict[tuple[UUID, int], ScoredChunk] = {}
        for ch in chunks:
            k = (ch.document_id, ch.page_number)
            if k not in best or ch.score > best[k].score:
                best[k] = ch
        return sorted(best.values(), key=lambda x: -x.score)

    async def query(self, question: str, workspace_id: UUID, top_k: int = 8) -> RAGResult:
        t0 = time.perf_counter()
        redis = get_redis()
        embedder = EmbeddingService(self._settings, redis)
        qv = await embedder.embed_query(question)
        qsvc = QdrantService(self._settings)
        try:
            raw = await qsvc.search(workspace_id, qv, top_k=max(top_k * 4, top_k))
            deduped = self._dedupe_by_page(raw)[:top_k]
            elapsed_ms = (time.perf_counter() - t0) * 1000
            return RAGResult(chunks=deduped, query_embedding_time_ms=elapsed_ms)
        finally:
            await qsvc.close()

    @observe()
    async def generate_cited_answer(
        self,
        question: str,
        chunks: list[ScoredChunk],
    ) -> CitedAnswer:
        threshold = self._settings.RAG_MIN_SIMILARITY_SCORE
        if not chunks or max(c.score for c in chunks) < threshold:
            return CitedAnswer(
                answer="I don't have information about this in the uploaded documents.",
                citations=[],
                confidence=0.0,
                unanswerable=True,
            )

        lines: list[str] = []
        for i, ch in enumerate(chunks, start=1):
            lines.append(
                f"[{i}] Title: {ch.document_title} | page {ch.page_number} | "
                f"chunk {ch.chunk_index}\n{ch.text}"
            )
        context = "\n\n".join(lines)

        ep = self._settings.AZURE_OPENAI_ENDPOINT.strip().rstrip("/")
        key = self._settings.AZURE_OPENAI_API_KEY.strip()
        if not ep or not key:
            raise RuntimeError(
                "Azure OpenAI not configured (AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY)."
            )

        client = AsyncAzureOpenAI(
            azure_endpoint=ep,
            api_key=key,
            api_version=self._settings.AZURE_OPENAI_API_VERSION,
        )
        try:
            completion = await client.chat.completions.create(
                model=self._settings.AZURE_OPENAI_DEPLOYMENT,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Answer based ONLY on the provided context. "
                            "If the answer isn't in context, say exactly: "
                            "'I don't have information about this in the uploaded documents.' "
                            "Include citations as [1], [2] referring to the numbered sources below."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Question:\n{question}\n\nContext:\n{context}",
                    },
                ],
                temperature=0.2,
            )
            if completion.usage is not None:
                ud = {
                    k: int(v)
                    for k, v in {
                        "input": completion.usage.prompt_tokens,
                        "output": completion.usage.completion_tokens,
                        "total": completion.usage.total_tokens,
                    }.items()
                    if v is not None
                }
                if ud:
                    try:
                        get_client().update_current_generation(
                            model=self._settings.AZURE_OPENAI_DEPLOYMENT,
                            usage_details=ud,
                        )
                    except Exception:  # noqa: BLE001
                        pass
            answer = (completion.choices[0].message.content or "").strip()
        finally:
            await client.close()

        refs = [int(x) for x in re.findall(r"\[(\d+)\]", answer)]
        citations: list[Citation] = []
        seen: set[int] = set()
        for idx in refs:
            if idx in seen or idx < 1 or idx > len(chunks):
                continue
            seen.add(idx)
            ch = chunks[idx - 1]
            citations.append(
                Citation(
                    index=idx,
                    document_title=ch.document_title,
                    page_number=ch.page_number,
                    chunk_index=ch.chunk_index,
                    document_id=ch.document_id,
                )
            )

        conf = float(max(c.score for c in chunks))
        unans = "i don't have information about this in the uploaded documents" in answer.lower()
        return CitedAnswer(
            answer=answer,
            citations=citations,
            confidence=conf,
            unanswerable=unans,
        )
