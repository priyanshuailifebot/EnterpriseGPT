"""API shapes for document upload, listing, RAG query, and analytics."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class DocumentUploadResponse(BaseModel):
    document_id: UUID
    status: str
    deduplicated: bool = False


class DocumentSummaryOut(BaseModel):
    id: UUID
    filename: str
    file_type: str
    status: str
    chunk_count: int
    page_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentDetailOut(BaseModel):
    id: UUID
    workspace_id: UUID
    filename: str
    file_type: str
    file_size: int
    status: str
    chunk_count: int
    page_count: int
    minio_key: str
    error_message: str | None
    created_at: datetime
    indexed_at: datetime | None

    model_config = {"from_attributes": True}


class DocumentStatusOut(BaseModel):
    document_id: UUID
    status: str
    chunk_count: int
    error_message: str | None
    indexed_at: datetime | None


class DocumentChunkOut(BaseModel):
    id: UUID
    chunk_index: int
    page_number: int
    text: str

    model_config = {"from_attributes": True}


class DocumentQueryBody(BaseModel):
    question: str = Field(..., min_length=1)
    top_k: int = Field(default=8, ge=1, le=50)


class CitationOut(BaseModel):
    index: int
    document_title: str
    page_number: int
    chunk_index: int
    document_id: UUID


class CitedAnswerOut(BaseModel):
    answer: str
    citations: list[CitationOut]
    confidence: float
    unanswerable: bool


class RagAnalyticsOut(BaseModel):
    documents_by_status: dict[str, int]
    total_chunks: int


class DocumentListResponse(BaseModel):
    items: list[DocumentSummaryOut]
    total: int
    page: int
    page_size: int
