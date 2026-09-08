"""摄取缓存 + 索引装配（D，持久/幂等）。

cache.json 存 {file_hash, parser_ver, chunks[]}；重跑同 hash 直接复用，不重解析。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .embed import Embedder, HashEmbedder
from .hybrid import HybridRetriever, VectorIndex
from .ingest import Chunk, ingest_dir
from .retriever import LexicalIndex

PARSER_VERSION = 2


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def load_cached_chunks(cache_path: str | Path, directory: str | Path,
                       max_files: int | None = None) -> list[Chunk]:
    """返回 chunks：命中缓存的直接读，新增/变更文件解析后并入并写回。"""
    cache_path = Path(cache_path)
    cache: dict[str, dict] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}

    files = sorted(Path(directory).glob("*.pdf"))
    if max_files is not None:
        files = files[:max_files]

    chunks: list[Chunk] = []
    changed = False
    for f in files:
        digest = _file_digest(f)
        entry = cache.get(f.name)
        if entry and entry.get("file_hash") == digest and entry.get("parser_ver") == PARSER_VERSION:
            chunks.extend([Chunk(**c) for c in entry["chunks"]])
            continue
        parsed, meta = _parse_one(f)
        chunks.extend(parsed)
        cache[f.name] = {"file_hash": digest, "parser_ver": PARSER_VERSION,
                         "chunks": [c.__dict__ for c in parsed],
                         "meta": meta}
        changed = True

    if changed:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return chunks


def _parse_one(path: Path):
    from .ingest import ingest_pdf

    return ingest_pdf(path)


def build_hybrid(directory: str | Path, *, cache_path: str | Path,
                 max_files: int | None = None,
                 embedder: Embedder | None = None) -> HybridRetriever:
    """从文献目录建混合检索（带缓存幂等）。返回 retriever；词法/向量索引附在属性上。"""
    from .library import Library

    chunks = load_cached_chunks(cache_path, directory, max_files=max_files)
    embedder = embedder or HashEmbedder()
    lexical = LexicalIndex()
    vector = VectorIndex(embedder)
    for c in chunks:
        lexical.add(c)
        vector.add(c)
    retriever = HybridRetriever(lexical, vector)
    retriever.lexical = lexical
    retriever.vector = vector
    retriever.library = Library()
    retriever.library.add_all(chunks)
    return retriever
