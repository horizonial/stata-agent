"""Library：按 chunk_id 可查、带 source_role 的文献库（引文接地用）。

摄取产物（ingest）进内存便于引用校验；持久化/增量留给 D 组。
"""

from __future__ import annotations

from .ingest import SOURCE_ROLE_CITABLE, Chunk, chunk_text_digest


class Library:
    def __init__(self):
        self._chunks: dict[str, Chunk] = {}
        self._docs: dict[str, str] = {}  # doc_id -> title/first-line

    def add(self, chunk: Chunk) -> None:
        self._chunks[chunk.chunk_id] = chunk

    def add_all(self, chunks: list[Chunk]) -> None:
        for c in chunks:
            self.add(c)

    def get(self, chunk_id: str) -> Chunk | None:
        return self._chunks.get(chunk_id)

    def role(self, chunk_id: str) -> str | None:
        chunk = self._chunks.get(chunk_id)
        return chunk.source_role if chunk else None

    def is_citable(self, chunk_id: str) -> bool:
        return (
            self._chunks.get(chunk_id) is not None
            and self._chunks[chunk_id].source_role == SOURCE_ROLE_CITABLE
        )

    def attachment_id(self, chunk_id: str) -> str | None:
        chunk = self._chunks.get(chunk_id)
        return chunk.attachment_id if chunk else None

    def by_attachment(self, attachment_id: str) -> list[Chunk]:
        if not isinstance(attachment_id, str) or not attachment_id:
            return []
        return sorted(
            (chunk for chunk in self._chunks.values() if chunk.attachment_id == attachment_id),
            key=lambda chunk: (chunk.page, chunk.chunk_id),
        )

    def text_digest(self, chunk_id: str) -> str | None:
        chunk = self._chunks.get(chunk_id)
        if chunk is None:
            return None
        # Recompute from the current text.  ``Chunk.text_digest`` records the
        # ingest-time value for cache compatibility; using it here would let a
        # mutated in-memory chunk pass citation validation unchanged.
        return chunk_text_digest(chunk.text)

    def __len__(self) -> int:
        return len(self._chunks)


def build_from(chunks: list[Chunk]) -> Library:
    lib = Library()
    lib.add_all(chunks)
    return lib
