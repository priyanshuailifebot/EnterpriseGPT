"""Document upload, listing, RAG query, and workspace-scoped analytics."""

from __future__ import annotations

import contextlib
import hashlib
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import Response
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.permissions import Permission, require_permission
from core.security import get_current_active_user
from core.storage import StorageService
from models.document import Document, DocumentStatus
from models.document_chunk import DocumentChunk
from models.rag_query_log import RagQueryLog
from models.user import User
from rag.pipeline import run_document_ingestion
from rag.retrieval_service import RAGService
from rag.vector_store import QdrantService
from schemas.documents import (
    CitationOut,
    CitedAnswerOut,
    DocumentChunkOut,
    DocumentDetailOut,
    DocumentListResponse,
    DocumentQueryBody,
    DocumentStatusOut,
    DocumentSummaryOut,
    DocumentUploadResponse,
    RagAnalyticsOut,
)
from services.workflow_service import ensure_workspace_membership

router = APIRouter(prefix="/documents", tags=["documents"])

_PRE_INGEST = DocumentStatus.INDEXED, DocumentStatus.PROCESSING, DocumentStatus.PENDING


def _extension(filename: str) -> str:
    if "." not in filename:
        return "bin"
    return filename.rsplit(".", 1)[-1].lower()


@router.post(
    "/upload",
    response_model=DocumentUploadResponse,
    dependencies=[require_permission(Permission.DOCUMENT_UPLOAD)],
)
async def upload_document(
    background_tasks: BackgroundTasks,
    workspace_id: UUID = Query(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> DocumentUploadResponse:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file")
    filename = file.filename or "upload"
    file_type = _extension(filename)
    digest = hashlib.sha256(raw).hexdigest()

    stmt_existing: Select[tuple[Document]] = select(Document).where(
        Document.workspace_id == workspace_id,
        Document.content_sha256 == digest,
    )
    existing = (await db.execute(stmt_existing)).scalar_one_or_none()
    if existing is not None:
        if existing.status in _PRE_INGEST:
            return DocumentUploadResponse(
                document_id=existing.id,
                status=existing.status.value,
                deduplicated=True,
            )
        storage = StorageService()
        loc = await storage.upload_document(raw, filename, workspace_id, user.id)
        existing.minio_key = loc["key"]
        existing.filename = filename
        existing.file_type = file_type
        existing.file_size = len(raw)
        existing.status = DocumentStatus.PENDING
        existing.error_message = None
        await db.commit()
        background_tasks.add_task(run_document_ingestion, existing.id, raw, workspace_id)
        return DocumentUploadResponse(document_id=existing.id, status=existing.status.value)

    storage = StorageService()
    loc = await storage.upload_document(raw, filename, workspace_id, user.id)
    doc = Document(
        workspace_id=workspace_id,
        uploaded_by=user.id,
        filename=filename,
        file_type=file_type,
        file_size=len(raw),
        minio_key=loc["key"],
        content_sha256=digest,
        status=DocumentStatus.PENDING,
    )
    db.add(doc)
    try:
        await db.commit()
        await db.refresh(doc)
    except IntegrityError:
        await db.rollback()
        existing2 = (await db.execute(stmt_existing)).scalar_one_or_none()
        if existing2 is not None:
            return DocumentUploadResponse(
                document_id=existing2.id,
                status=existing2.status.value,
                deduplicated=True,
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Duplicate content race; retry upload",
        ) from None

    background_tasks.add_task(run_document_ingestion, doc.id, raw, workspace_id)
    return DocumentUploadResponse(document_id=doc.id, status=doc.status.value)


@router.get(
    "/analytics/rag",
    response_model=RagAnalyticsOut,
    dependencies=[require_permission(Permission.ANALYTICS_READ)],
)
async def rag_analytics(
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> RagAnalyticsOut:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    by_status: dict[str, int] = {s.value: 0 for s in DocumentStatus}
    stmt = select(Document.status, func.count()).where(Document.workspace_id == workspace_id).group_by(Document.status)
    for st, cnt in (await db.execute(stmt)).all():
        by_status[st.value] = int(cnt)
    total_chunks = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(Document.chunk_count), 0)).where(
                    Document.workspace_id == workspace_id
                )
            )
        ).scalar_one()
    )
    return RagAnalyticsOut(documents_by_status=by_status, total_chunks=total_chunks)


@router.get(
    "",
    response_model=DocumentListResponse,
    dependencies=[require_permission(Permission.DOCUMENT_READ)],
)
async def list_documents(
    workspace_id: UUID = Query(...),
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> DocumentListResponse:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    filters = [Document.workspace_id == workspace_id]
    if status_filter:
        try:
            st = DocumentStatus(status_filter)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status"
            ) from None
        filters.append(Document.status == st)

    total = int((await db.execute(select(func.count()).select_from(Document).where(*filters))).scalar_one())
    stmt = (
        select(Document).where(*filters).order_by(Document.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return DocumentListResponse(
        items=[DocumentSummaryOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{document_id}",
    response_model=DocumentDetailOut,
    dependencies=[require_permission(Permission.DOCUMENT_READ)],
)
async def get_document(
    document_id: UUID,
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> DocumentDetailOut:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    doc = await db.get(Document, document_id)
    if doc is None or doc.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return DocumentDetailOut.model_validate(doc)


@router.get(
    "/{document_id}/status",
    response_model=DocumentStatusOut,
    dependencies=[require_permission(Permission.DOCUMENT_READ)],
)
async def get_document_status(
    document_id: UUID,
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> DocumentStatusOut:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    doc = await db.get(Document, document_id)
    if doc is None or doc.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return DocumentStatusOut(
        document_id=doc.id,
        status=doc.status.value,
        chunk_count=doc.chunk_count,
        error_message=doc.error_message,
        indexed_at=doc.indexed_at,
    )


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[require_permission(Permission.DOCUMENT_UPLOAD)],
)
async def delete_document(
    document_id: UUID,
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> Response:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    doc = await db.get(Document, document_id)
    if doc is None or doc.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    qsvc = QdrantService()
    try:
        await qsvc.delete_document_vectors(workspace_id, document_id)
    finally:
        await qsvc.close()
    storage = StorageService()
    bucket = StorageService.bucket_name_for_workspace(workspace_id)
    with contextlib.suppress(Exception):
        await storage.delete_document(bucket, doc.minio_key)
    await db.delete(doc)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/query",
    response_model=CitedAnswerOut,
    dependencies=[require_permission(Permission.DOCUMENT_READ)],
)
async def query_documents(
    body: DocumentQueryBody,
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> CitedAnswerOut:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    rag = RAGService()
    result = await rag.query(body.question, workspace_id, top_k=body.top_k)
    cited = await rag.generate_cited_answer(body.question, result.chunks)
    top_doc = cited.citations[0].document_id if cited.citations else None
    if top_doc is not None:
        cited_row = await db.get(Document, top_doc)
        if cited_row is None or cited_row.workspace_id != workspace_id:
            top_doc = None
    excerpt = body.question.strip()[:512]
    db.add(
        RagQueryLog(
            workspace_id=workspace_id,
            user_id=user.id,
            confidence=cited.confidence,
            unanswerable=cited.unanswerable,
            citation_count=len(cited.citations),
            top_document_id=top_doc,
            question_excerpt=excerpt or None,
        )
    )
    await db.commit()
    return CitedAnswerOut(
        answer=cited.answer,
        citations=[
            CitationOut(
                index=c.index,
                document_title=c.document_title,
                page_number=c.page_number,
                chunk_index=c.chunk_index,
                document_id=c.document_id,
            )
            for c in cited.citations
        ],
        confidence=cited.confidence,
        unanswerable=cited.unanswerable,
    )


@router.get(
    "/{document_id}/chunks",
    response_model=list[DocumentChunkOut],
    dependencies=[require_permission(Permission.DOCUMENT_READ)],
)
async def list_document_chunks(
    document_id: UUID,
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> list[DocumentChunkOut]:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    doc = await db.get(Document, document_id)
    if doc is None or doc.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    stmt = (
        select(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.chunk_index)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return [DocumentChunkOut.model_validate(r) for r in rows]
