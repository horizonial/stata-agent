"""Library：按 chunk_id 可查、带 source_role 的文献库（引文接地用）。

摄取产物（ingest）进内存便于引用校验；持久化/增量留给 D 组。
"""

from __future__ import annotations

from .ingest import SOURCE_ROLE_CITABLE, SOURCE_ROLE_STYLE, Chunk


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
        return self._chunks.get(chunk_id) is not None and self._chunks[chunk_id].source_role == SOURCE_ROLE_CITABLE

    def __len__(self) -> int:
        return len(self._chunks)


def build_from(chunks: list[Chunk]) -> Library:
    lib = Library()
    lib.add_all(chunks)
    return lib
