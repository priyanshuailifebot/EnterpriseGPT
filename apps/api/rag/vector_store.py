"""Qdrant vector store for workspace-scoped chunks."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from core.config import Settings, get_settings
from rag.chunker import Chunk


def collection_name(workspace_id: UUID) -> str:
    """Collection per workspace (stable, valid identifier)."""
    return f"egpt_{workspace_id.hex}"


class ScoredChunk:
    __slots__ = (
        "id",
        "score",
        "text",
        "document_id",
        "chunk_index",
        "page_number",
        "document_title",
        "document_type",
        "workspace_id",
    )

    def __init__(
        self,
        *,
        id: str | UUID,
        score: float,
        text: str,
        document_id: UUID,
        chunk_index: int,
        page_number: int,
        document_title: str,
        document_type: str,
        workspace_id: UUID,
    ) -> None:
        self.id = str(id)
        self.score = score
        self.text = text
        self.document_id = document_id
        self.chunk_index = chunk_index
        self.page_number = page_number
        self.document_title = document_title
        self.document_type = document_type
        self.workspace_id = workspace_id


class QdrantService:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = AsyncQdrantClient(
            url=self._settings.QDRANT_URL,
            api_key=self._settings.QDRANT_API_KEY or None,
        )

    async def close(self) -> None:
        await self._client.close()

    async def ensure_collection(self, workspace_id: UUID) -> str:
        """Create collection if missing; return collection name."""
        name = collection_name(workspace_id)
        if not await self._client.collection_exists(name):
            await self._client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=1536, distance=Distance.COSINE),
            )
        return name

    async def upsert_chunks(
        self,
        workspace_id: UUID,
        point_ids: list[UUID],
        embeddings: list[list[float]],
        chunks: list[Chunk],
        *,
        document_title: str,
        document_type: str,
        uploaded_by: UUID,
        created_at: datetime,
    ) -> None:
        name = await self.ensure_collection(workspace_id)
        if len(point_ids) != len(embeddings) or len(embeddings) != len(chunks):
            raise ValueError("point_ids, embeddings, and chunks must align")
        points: list[PointStruct] = []
        for pid, vec, ch in zip(point_ids, embeddings, chunks, strict=True):
            payload: dict[str, Any] = {
                "workspace_id": str(workspace_id),
                "document_id": str(ch.document_id),
                "chunk_index": ch.chunk_index,
                "text": ch.text,
                "page_number": ch.page_number,
                "document_title": document_title,
                "document_type": document_type,
                "uploaded_by": str(uploaded_by),
                "created_at": created_at.isoformat(),
            }
            points.append(PointStruct(id=str(pid), vector=vec, payload=payload))
        await self._client.upsert(collection_name=name, points=points)

    async def search(
        self,
        workspace_id: UUID,
        query_vector: list[float],
        top_k: int,
    ) -> list[ScoredChunk]:
        name = collection_name(workspace_id)
        if not await self._client.collection_exists(name):
            return []
        flt = Filter(
            must=[
                FieldCondition(
                    key="workspace_id",
                    match=MatchValue(value=str(workspace_id)),
                )
            ]
        )
        hits = await self._client.search(
            collection_name=name,
            query_vector=query_vector,
            limit=top_k,
            query_filter=flt,
            with_payload=True,
        )
        out: list[ScoredChunk] = []
        for h in hits:
            pl = h.payload or {}
            out.append(
                ScoredChunk(
                    id=h.id,
                    score=float(h.score),
                    text=str(pl.get("text") or ""),
                    document_id=UUID(str(pl["document_id"])),
                    chunk_index=int(pl.get("chunk_index", 0)),
                    page_number=int(pl.get("page_number", 0)),
                    document_title=str(pl.get("document_title") or ""),
                    document_type=str(pl.get("document_type") or ""),
                    workspace_id=UUID(str(pl["workspace_id"])),
                )
            )
        return out

    async def delete_document_vectors(self, workspace_id: UUID, document_id: UUID) -> None:
        name = collection_name(workspace_id)
        if not await self._client.collection_exists(name):
            return
        await self._client.delete(
            collection_name=name,
            points_selector=Filter(
                must=[
                    FieldCondition(
                        key="document_id",
                        match=MatchValue(value=str(document_id)),
                    )
                ]
            ),
        )

    async def count_points(self, workspace_id: UUID) -> int:
        name = collection_name(workspace_id)
        if not await self._client.collection_exists(name):
            return 0
        info = await self._client.get_collection(name)
        return int(info.points_count)
