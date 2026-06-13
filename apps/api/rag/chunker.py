"""Split pages into overlapping chunks for embedding."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from langchain_text_splitters import RecursiveCharacterTextSplitter


@dataclass
class Chunk:
    text: str
    page_number: int
    chunk_index: int
    document_id: UUID


class DocumentChunker:
    def __init__(
        self,
        *,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
        separators: list[str] | None = None,
    ) -> None:
        seps = separators or ["\n\n", "\n", ". "]
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=seps,
            length_function=len,
        )

    def chunk(self, pages: list, document_id: UUID) -> list[Chunk]:
        """``pages`` are :class:`PageContent` instances from :class:`DocumentExtractor`."""
        out: list[Chunk] = []
        idx = 0
        for page in pages:
            pieces = self._splitter.split_text(page.text)
            for piece in pieces:
                if not piece.strip():
                    continue
                out.append(
                    Chunk(
                        text=piece,
                        page_number=page.page_number,
                        chunk_index=idx,
                        document_id=document_id,
                    )
                )
                idx += 1
        return out
