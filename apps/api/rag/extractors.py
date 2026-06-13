"""Text extraction from supported document types."""

from __future__ import annotations

import io
from dataclasses import dataclass

import fitz  # PyMuPDF
import pandas as pd
from docx import Document as DocxDocument


@dataclass
class PageContent:
    text: str
    page_number: int


class UnsupportedFileTypeError(ValueError):
    """Raised when the file extension is not supported."""


class DocumentExtractor:
    def extract(self, file_bytes: bytes, file_type: str) -> list[PageContent]:
        ext = file_type.lower().lstrip(".")
        if ext == "pdf":
            return self._pdf_pages(file_bytes)
        if ext == "docx":
            return self._docx_paragraphs(file_bytes)
        if ext in ("txt", "md", "markdown"):
            text = file_bytes.decode("utf-8", errors="replace")
            return [PageContent(text=text, page_number=1)]
        if ext == "csv":
            return self._csv_markdown(file_bytes)
        raise UnsupportedFileTypeError(f"Unsupported file type: {ext}")

    def _pdf_pages(self, file_bytes: bytes) -> list[PageContent]:
        pages: list[PageContent] = []
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            for i in range(doc.page_count):
                page = doc.load_page(i)
                pages.append(PageContent(text=page.get_text(), page_number=i + 1))
        return pages

    def _docx_paragraphs(self, file_bytes: bytes) -> list[PageContent]:
        d = DocxDocument(io.BytesIO(file_bytes))
        parts: list[str] = []
        for p in d.paragraphs:
            if p.text.strip():
                style = getattr(p.style, "name", "") or ""
                if "Heading" in style:
                    parts.append(f"## {p.text.strip()}")
                else:
                    parts.append(p.text.strip())
        return [PageContent(text="\n\n".join(parts), page_number=1)]

    def _csv_markdown(self, file_bytes: bytes) -> list[PageContent]:
        try:
            text = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = file_bytes.decode("latin-1", errors="replace")
        df = pd.read_csv(io.StringIO(text))
        md = df.to_markdown(index=False)
        return [PageContent(text=md, page_number=1)]
