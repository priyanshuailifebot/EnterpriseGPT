"""End-to-end ingestion: extract → chunk → embed → Qdrant → Postgres metadata."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import structlog
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings, get_settings
from core.redis import get_redis
from models.document import Document, DocumentStatus
from models.document_chunk import DocumentChunk
from rag.chunker import DocumentChunker
from rag.embeddings import EmbeddingService
from rag.extractors import DocumentExtractor, UnsupportedFileTypeError
from rag.vector_store import QdrantService

log = structlog.get_logger("enterprisegpt.rag.pipeline")


class IngestionPipeline:
    def __init__(
        self,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()

    async def ingest(
        self,
        session: AsyncSession,
        document_id: UUID,
        file_bytes: bytes,
        workspace_id: UUID,
    ) -> None:
        doc = await session.get(Document, document_id)
        if doc is None:
            log.warning("ingest.document_missing", document_id=str(document_id))
            return

        qsvc = QdrantService(self._settings)
        try:
            doc.status = DocumentStatus.PROCESSING
            doc.error_message = None
            await session.flush()
            await qsvc.delete_document_vectors(workspace_id, document_id)
            await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
            await session.commit()

            doc = await session.get(Document, document_id)
            if doc is None:
                return

            extractor = DocumentExtractor()
            try:
                pages = extractor.extract(file_bytes, doc.file_type)
            except UnsupportedFileTypeError:
                doc.status = DocumentStatus.ERROR
                doc.error_message = "Unsupported file type"
                await session.commit()
                return

            doc.page_count = len(pages) if pages else 0
            chunker = DocumentChunker(
                chunk_size=self._settings.RAG_CHUNK_SIZE,
                chunk_overlap=self._settings.RAG_CHUNK_OVERLAP,
            )
            chunks = chunker.chunk(pages, document_id)
            if not chunks:
                doc.status = DocumentStatus.INDEXED
                doc.chunk_count = 0
                doc.indexed_at = datetime.now(UTC)
                await session.commit()
                return

            redis = get_redis()
            embedder = EmbeddingService(self._settings, redis)
            vectors = await embedder.embed_texts([c.text for c in chunks])

            point_ids = [uuid4() for _ in chunks]
            created_at = doc.created_at if doc.created_at else datetime.now(UTC)
            await qsvc.upsert_chunks(
                workspace_id,
                point_ids,
                vectors,
                chunks,
                document_title=doc.filename,
                document_type=doc.file_type,
                uploaded_by=doc.uploaded_by,
                created_at=created_at,
            )

            for pid, ch in zip(point_ids, chunks, strict=True):
                session.add(
                    DocumentChunk(
                        document_id=document_id,
                        chunk_index=ch.chunk_index,
                        text=ch.text,
                        page_number=ch.page_number,
                        qdrant_id=pid,
                    )
                )

            doc.chunk_count = len(chunks)
            doc.status = DocumentStatus.INDEXED
            doc.indexed_at = datetime.now(UTC)
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            log.exception("ingest.failed", document_id=str(document_id), error=str(exc))
            await session.rollback()
            doc = await session.get(Document, document_id)
            if doc:
                doc.status = DocumentStatus.ERROR
                doc.error_message = str(exc)[:2000]
                await session.commit()
        finally:
            await qsvc.close()


async def run_document_ingestion(document_id: UUID, file_bytes: bytes, workspace_id: UUID) -> None:
    """Background entrypoint using a fresh DB session."""
    from core.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        pipeline = IngestionPipeline()
        await pipeline.ingest(session, document_id, file_bytes, workspace_id)
