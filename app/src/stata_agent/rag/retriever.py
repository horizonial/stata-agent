"""词法检索最小版（DD-07 §4）：先词法保精确，向量后置。中文靠 CJK 双字 + ASCII 词。"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from .ingest import Chunk

_CJK = re.compile(r"[一-鿿]")


def tokenize(text: str) -> list[str]:
    """ascii 词(小写) + 中文连续串的双字组。"""
    out: list[str] = []
    for w in re.findall(r"[A-Za-z0-9_]+", text.lower()):
        out.append(w)
    for han in re.findall(r"[一-鿿]+", text):
        if len(han) == 1:
            out.append(han)
        else:
            out += [han[i:i + 2] for i in range(len(han) - 1)]
    return out


class LexicalIndex:
    def __init__(self):
        self._chunks: dict[str, Chunk] = {}
        self._postings: dict[str, list[tuple[str, float]]] = defaultdict(list)  # term -> [(chunk_id, tf)]
        self._len: dict[str, float] = {}

    def add(self, chunk: Chunk) -> None:
        if chunk.chunk_id in self._chunks:
            return
        toks = tokenize(chunk.text)
        n = max(len(toks), 1)
        self._chunks[chunk.chunk_id] = chunk
        self._len[chunk.chunk_id] = n
        for term, cnt in Counter(toks).items():
            self._postings[term].append((chunk.chunk_id, cnt / n))

    def add_all(self, chunks: list[Chunk]) -> None:
        for c in chunks:
            self.add(c)

    def search(self, query: str, *, top_k: int = 5, roles: set[str] | None = None) -> list[Chunk]:
        q = Counter(tokenize(query))
        df = {t: len(v) for t, v in self._postings.items()}
        n_docs = max(len(self._chunks), 1)
        scores: dict[str, float] = defaultdict(float)
        for term, qf in q.items():
            tfidf_idf = 1.0 + math.log(n_docs / (1 + df.get(term, 0)))
            for cid, tf in self._postings.get(term, []):
                if roles and self._chunks[cid].source_role not in roles:
                    continue
                scores[cid] += qf * tf * tfidf_idf
        ranked = sorted(scores, key=scores.get, reverse=True)[:top_k]
        return [self._chunks[c] for c in ranked]

    def __len__(self) -> int:
        return len(self._chunks)
