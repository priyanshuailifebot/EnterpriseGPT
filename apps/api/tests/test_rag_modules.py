"""Unit tests for extractor and chunker (no external IO)."""

from __future__ import annotations

from uuid import uuid4

import fitz
import pytest

from rag.chunker import DocumentChunker
from rag.extractors import DocumentExtractor, UnsupportedFileTypeError


def _tiny_pdf(text: str = "hello world chunk boundary text. ") -> bytes:
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 72), text * 50)
    out = doc.tobytes()
    doc.close()
    return out


def test_extractor_pdf_and_chunker() -> None:
    ext = DocumentExtractor()
    pages = ext.extract(_tiny_pdf(), "pdf")
    assert len(pages) >= 1
    chunks = DocumentChunker().chunk(pages, uuid4())
    assert len(chunks) >= 1
    assert len(chunks[0].text) <= 1200


def test_extractor_unsupported() -> None:
    with pytest.raises(UnsupportedFileTypeError):
        DocumentExtractor().extract(b"x", "exe")
