"""Phase 5 approval-style tests — documents API & RAG (mocked backends)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import fitz
import pytest
from httpx import AsyncClient

from models.document import Document, DocumentStatus
from models.document_chunk import DocumentChunk
from models.user import UserRole
from rag.retrieval_service import Citation, CitedAnswer, RAGResult
from rag.vector_store import ScoredChunk


async def _register(
    client: AsyncClient,
    *,
    email: str,
    role: UserRole = UserRole.BUILDER,
) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "supersecret123",
            "full_name": "Phase5 User",
            "role": role.value,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _pdf_bytes(text: str = "fixture sentence one.", pages: int = 1) -> bytes:
    doc = fitz.open()
    for _ in range(pages):
        p = doc.new_page()
        p.insert_text((72, 72), text)
    out = doc.tobytes()
    doc.close()
    return out


async def _finish_ingestion_local(document_id: UUID, workspace_id: UUID) -> None:
    from core.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        doc = await session.get(Document, document_id)
        assert doc is not None
        doc.status = DocumentStatus.INDEXED
        doc.chunk_count = 1
        doc.page_count = 1
        doc.indexed_at = datetime.now(UTC)
        session.add(
            DocumentChunk(
                document_id=document_id,
                chunk_index=0,
                text="EnterpriseGPT Phase 5 revenue target is 99 million.",
                page_number=1,
                qdrant_id=uuid4(),
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_phase5_upload_pending_then_indexed_via_background(client: AsyncClient) -> None:
    body = await _register(client, email="p5-bg@example.com")
    ws_id = body["user"]["workspaces"][0]["workspace_id"]
    hdr = {"Authorization": f"Bearer {body['access_token']}"}

    async def _bg(doc_id: UUID, raw: bytes, ws: UUID) -> None:
        await _finish_ingestion_local(doc_id, ws)

    pdf = _pdf_bytes("indexed background test content.")
    with patch("routers.documents.run_document_ingestion", side_effect=_bg):
        with patch("routers.documents.StorageService") as stor:
            stor.return_value.ensure_bucket = AsyncMock(return_value=f"documents-{ws_id}")
            stor.return_value.upload_document = AsyncMock(
                return_value={"bucket": f"documents-{ws_id}", "key": "k1", "url": "http://x"}
            )
            r = await client.post(
                f"/api/v1/documents/upload?workspace_id={ws_id}",
                headers=hdr,
                files={"file": ("test.pdf", pdf, "application/pdf")},
            )
    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] == "pending"
    assert payload["deduplicated"] is False
    doc_id = payload["document_id"]
    await asyncio.sleep(0.15)
    st = await client.get(
        f"/api/v1/documents/{doc_id}/status?workspace_id={ws_id}",
        headers=hdr,
    )
    assert st.json()["status"] == "indexed"
    detail = await client.get(
        f"/api/v1/documents/{doc_id}?workspace_id={ws_id}",
        headers=hdr,
    )
    assert detail.json()["chunk_count"] > 0


@pytest.mark.asyncio
async def test_phase5_query_citations_and_unanswerable(client: AsyncClient) -> None:
    body = await _register(client, email="p5-q@example.com")
    ws_id = body["user"]["workspaces"][0]["workspace_id"]
    hdr = {"Authorization": f"Bearer {body['access_token']}"}
    ws_uuid = UUID(ws_id)
    doc_id = uuid4()
    ch = ScoredChunk(
        id="pt1",
        score=0.95,
        text="Only info about sales in Europe.",
        document_id=doc_id,
        chunk_index=0,
        page_number=3,
        document_title="Report.pdf",
        document_type="pdf",
        workspace_id=ws_uuid,
    )
    cited = CitedAnswer(
        answer="Sales grew per [1].",
        citations=[
            Citation(
                index=1,
                document_title="Report.pdf",
                page_number=3,
                chunk_index=0,
                document_id=doc_id,
            )
        ],
        confidence=0.95,
        unanswerable=False,
    )

    with patch("routers.documents.RAGService") as M:
        inst = MagicMock()
        M.return_value = inst
        inst.query = AsyncMock(return_value=RAGResult(chunks=[ch], query_embedding_time_ms=1.0))
        inst.generate_cited_answer = AsyncMock(return_value=cited)
        r = await client.post(
            f"/api/v1/documents/query?workspace_id={ws_id}",
            headers=hdr,
            json={"question": "What about sales?", "top_k": 5},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["answer"]
    assert len(data["citations"]) == 1
    assert data["citations"][0]["document_title"] == "Report.pdf"
    assert data["citations"][0]["page_number"] == 3

    un_cited = CitedAnswer(
        answer="I don't have information about this in the uploaded documents.",
        citations=[],
        confidence=0.0,
        unanswerable=True,
    )
    with patch("routers.documents.RAGService") as M:
        inst = MagicMock()
        M.return_value = inst
        inst.query = AsyncMock(return_value=RAGResult(chunks=[], query_embedding_time_ms=1.0))
        inst.generate_cited_answer = AsyncMock(return_value=un_cited)
        r2 = await client.post(
            f"/api/v1/documents/query?workspace_id={ws_id}",
            headers=hdr,
            json={"question": "What is the nuclear launch code?", "top_k": 5},
        )
    assert r2.json()["unanswerable"] is True


@pytest.mark.asyncio
async def test_phase5_cross_workspace_isolation(client: AsyncClient) -> None:
    a = await _register(client, email="p5-a@example.com")
    b = await _register(client, email="p5-b@example.com")
    ws_a = UUID(a["user"]["workspaces"][0]["workspace_id"])
    ws_b = UUID(b["user"]["workspaces"][0]["workspace_id"])
    hdr_b = {"Authorization": f"Bearer {b['access_token']}"}
    doc_a = uuid4()

    with patch("routers.documents.RAGService") as M:
        inst = MagicMock()
        M.return_value = inst

        async def query_side_effect(question: str, workspace_id: UUID, top_k: int = 8):
            if workspace_id == ws_a:
                return RAGResult(
                    chunks=[
                        ScoredChunk(
                            id="1",
                            score=0.9,
                            text="secret A",
                            document_id=doc_a,
                            chunk_index=0,
                            page_number=1,
                            document_title="A.pdf",
                            document_type="pdf",
                            workspace_id=ws_a,
                        )
                    ],
                    query_embedding_time_ms=1.0,
                )
            return RAGResult(chunks=[], query_embedding_time_ms=1.0)

        inst.query = AsyncMock(side_effect=query_side_effect)
        inst.generate_cited_answer = AsyncMock(
            side_effect=lambda q, ch: (
                CitedAnswer(answer="ok", citations=[], confidence=0.9, unanswerable=False)
                if ch
                else CitedAnswer(
                    answer="I don't have information about this in the uploaded documents.",
                    citations=[],
                    confidence=0.0,
                    unanswerable=True,
                )
            )
        )
        rb = await client.post(
            f"/api/v1/documents/query?workspace_id={ws_b}",
            headers=hdr_b,
            json={"question": "secret"},
        )
    assert rb.json()["unanswerable"] is True


@pytest.mark.asyncio
async def test_phase5_delete_calls_storage_and_qdrant(client: AsyncClient) -> None:
    body = await _register(client, email="p5-del@example.com")
    ws_id = UUID(body["user"]["workspaces"][0]["workspace_id"])
    user_id = UUID(body["user"]["id"])
    hdr = {"Authorization": f"Bearer {body['access_token']}"}

    from core.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        doc = Document(
            workspace_id=ws_id,
            uploaded_by=user_id,
            filename="x.pdf",
            file_type="pdf",
            file_size=10,
            minio_key="k-del",
            content_sha256="a" * 64,
            status=DocumentStatus.INDEXED,
            chunk_count=0,
            page_count=1,
        )
        session.add(doc)
        await session.commit()
        await session.refresh(doc)
        doc_id = doc.id

    mock_q = AsyncMock()
    mock_q.delete_document_vectors = AsyncMock()
    mock_q.close = AsyncMock()
    mock_stor = MagicMock()
    mock_stor.delete_document = AsyncMock()
    with patch("routers.documents.QdrantService", return_value=mock_q):
        with patch("routers.documents.StorageService", return_value=mock_stor):
            r = await client.delete(
                f"/api/v1/documents/{doc_id}?workspace_id={ws_id}",
                headers=hdr,
            )
    assert r.status_code == 204
    mock_q.delete_document_vectors.assert_called_once()
    mock_stor.delete_document.assert_called_once()


@pytest.mark.asyncio
async def test_phase5_large_pdf_returns_quickly(client: AsyncClient) -> None:
    body = await _register(client, email="p5-large@example.com")
    ws_id = body["user"]["workspaces"][0]["workspace_id"]
    hdr = {"Authorization": f"Bearer {body['access_token']}"}
    big = _pdf_bytes("x", pages=120)

    async def _slow_bg(*_a, **_k):
        await asyncio.sleep(0.5)

    with patch("routers.documents.run_document_ingestion", side_effect=_slow_bg):
        with patch("routers.documents.StorageService") as stor:
            stor.return_value.ensure_bucket = AsyncMock(return_value=f"documents-{ws_id}")
            stor.return_value.upload_document = AsyncMock(
                return_value={"bucket": f"documents-{ws_id}", "key": "k2", "url": "http://x"}
            )
            t0 = time.perf_counter()
            r = await client.post(
                f"/api/v1/documents/upload?workspace_id={ws_id}",
                headers=hdr,
                files={"file": ("big.pdf", big, "application/pdf")},
            )
            elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_phase5_duplicate_upload_no_second_ingestion(client: AsyncClient) -> None:
    body = await _register(client, email="p5-dup@example.com")
    ws_id = body["user"]["workspaces"][0]["workspace_id"]
    hdr = {"Authorization": f"Bearer {body['access_token']}"}
    pdf = _pdf_bytes("duplicate fingerprint test.")

    mock_ingest = AsyncMock()

    with patch("routers.documents.run_document_ingestion", mock_ingest):
        with patch("routers.documents.StorageService") as stor:
            stor.return_value.ensure_bucket = AsyncMock(return_value=f"documents-{ws_id}")
            stor.return_value.upload_document = AsyncMock(
                return_value={"bucket": f"documents-{ws_id}", "key": "k3", "url": "http://x"}
            )
            r1 = await client.post(
                f"/api/v1/documents/upload?workspace_id={ws_id}",
                headers=hdr,
                files={"file": ("t.pdf", pdf, "application/pdf")},
            )
            assert r1.status_code == 200
            doc_id = r1.json()["document_id"]
            await _finish_ingestion_local(UUID(doc_id), UUID(ws_id))
            r2 = await client.post(
                f"/api/v1/documents/upload?workspace_id={ws_id}",
                headers=hdr,
                files={"file": ("t2.pdf", pdf, "application/pdf")},
            )
    assert r2.status_code == 200
    assert r2.json()["deduplicated"] is True
    assert mock_ingest.await_count == 1


@pytest.mark.asyncio
async def test_phase5_rag_analytics_admin(client: AsyncClient) -> None:
    body = await _register(client, email="p5-adm@example.com", role=UserRole.ADMIN)
    ws_id = body["user"]["workspaces"][0]["workspace_id"]
    user_id = UUID(body["user"]["id"])
    hdr = {"Authorization": f"Bearer {body['access_token']}"}

    from core.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        session.add_all(
            [
                Document(
                    workspace_id=UUID(ws_id),
                    uploaded_by=user_id,
                    filename="a.pdf",
                    file_type="pdf",
                    file_size=1,
                    minio_key="ka",
                    content_sha256="b" * 64,
                    status=DocumentStatus.INDEXED,
                    chunk_count=2,
                    page_count=1,
                ),
                Document(
                    workspace_id=UUID(ws_id),
                    uploaded_by=user_id,
                    filename="b.pdf",
                    file_type="pdf",
                    file_size=1,
                    minio_key="kb",
                    content_sha256="c" * 64,
                    status=DocumentStatus.PENDING,
                    chunk_count=0,
                    page_count=0,
                ),
            ]
        )
        await session.commit()

    r = await client.get(f"/api/v1/documents/analytics/rag?workspace_id={ws_id}", headers=hdr)
    assert r.status_code == 200
    data = r.json()
    assert data["documents_by_status"]["indexed"] >= 1
    assert data["total_chunks"] >= 2
