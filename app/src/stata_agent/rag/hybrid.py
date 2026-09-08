"""混合检索（D）：向量 top-k + 词法 top-k → RRF 合并去重。

角色过滤沿用 source_role：写作取证只放 citable。
"""

from __future__ import annotations

from .embed import Embedder, cosine
from .ingest import Chunk
from .retriever import LexicalIndex


class VectorIndex:
    """内存向量库（cosine）。量上去了再换 Chroma/Qdrant（同接口）。"""

    def __init__(self, embedder: Embedder):
        self._embedder = embedder
        self._vecs: dict[str, list[float]] = {}
        self._chunks: dict[str, Chunk] = {}

    def add(self, chunk: Chunk) -> None:
        if chunk.chunk_id not in self._vecs:
            self._vecs[chunk.chunk_id] = self._embedder.embed_text(chunk.text)
            self._chunks[chunk.chunk_id] = chunk

    def search(self, query: str, *, top_k: int = 5, roles: set[str] | None = None) -> list[Chunk]:
        q = self._embedder.embed_text(query)
        scored = []
        for cid, vec in self._vecs.items():
            chunk = self._chunks[cid]
            if roles and chunk.source_role not in roles:
                continue
            scored.append((cosine(q, vec), chunk))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [c for _, c in scored[:top_k]]

    def __len__(self) -> int:
        return len(self._vecs)


class HybridRetriever:
    """向量 + 词法，RRF 合并。"""

    def __init__(self, lexical: LexicalIndex, vector: VectorIndex, rrf_k: int = 60):
        self._lex = lexical
        self._vec = vector
        self._k = rrf_k

    def search(self, query: str, *, top_k: int = 5, roles: set[str] | None = None) -> list[Chunk]:
        scores: dict[str, float] = {}
        order: dict[str, Chunk] = {}

        for rank, c in enumerate(self._lex.search(query, top_k=top_k * 3, roles=roles)):
            scores[c.chunk_id] = scores.get(c.chunk_id, 0.0) + 1.0 / (self._k + rank + 1)
            order[c.chunk_id] = c
        for rank, c in enumerate(self._vec.search(query, top_k=top_k * 3, roles=roles)):
            scores[c.chunk_id] = scores.get(c.chunk_id, 0.0) + 1.0 / (self._k + rank + 1)
            order[c.chunk_id] = c
        ranked = sorted(scores, key=scores.get, reverse=True)[:top_k]
        return [order[cid] for cid in ranked]
