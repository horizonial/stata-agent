"""Bounded, atomic ingestion cache and reusable hybrid index assembly."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

from .embed import Embedder, HashEmbedder
from .hybrid import HybridRetriever, VectorIndex
from .ingest import (
    DEFAULT_MAX_CHUNKS,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_PAGES,
    DEFAULT_SOURCE_ROLE,
    Chunk,
)
from .retriever import LexicalIndex

PARSER_VERSION = 3

_INDEX_CACHE: dict[tuple, tuple[tuple[str, ...], HybridRetriever]] = {}
_INDEX_CACHE_LOCK = threading.RLock()


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _atomic_write_json(path: Path, value: object) -> None:
    """Write a cache via same-directory temp file + replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def load_cached_chunks(
    cache_path: str | Path,
    directory: str | Path,
    max_files: int | None = DEFAULT_MAX_FILES,
    max_pages: int | None = DEFAULT_MAX_PAGES,
    max_chunks: int | None = DEFAULT_MAX_CHUNKS,
    source_role: str = DEFAULT_SOURCE_ROLE,
) -> list[Chunk]:
    """Load changed PDFs incrementally, atomically persist, and prune deletions."""

    cache_path = Path(cache_path)
    directory = Path(directory)
    cache: dict[str, dict] = {}
    if cache_path.exists():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cache = loaded
        except (json.JSONDecodeError, OSError):
            cache = {}

    file_limit = (
        DEFAULT_MAX_FILES
        if max_files is None
        else max(0, min(int(max_files), DEFAULT_MAX_FILES))
    )
    page_limit = (
        DEFAULT_MAX_PAGES
        if max_pages is None
        else max(0, min(int(max_pages), DEFAULT_MAX_PAGES))
    )
    chunk_limit = (
        DEFAULT_MAX_CHUNKS
        if max_chunks is None
        else max(0, min(int(max_chunks), DEFAULT_MAX_CHUNKS))
    )
    all_files = sorted(directory.glob("*.pdf"))
    files = all_files[:file_limit]
    existing_names = {f.name for f in all_files}

    chunks: list[Chunk] = []
    changed = False
    for f in files:
        if len(chunks) >= chunk_limit:
            break
        digest = _file_digest(f)
        entry = cache.get(f.name)
        if (
            isinstance(entry, dict)
            and entry.get("file_hash") == digest
            and entry.get("parser_ver") == PARSER_VERSION
            and entry.get("source_role") == source_role
            and entry.get("max_pages") == page_limit
            and entry.get("parse_max_chunks") == DEFAULT_MAX_CHUNKS
        ):
            try:
                cached = [Chunk(**c) for c in entry.get("chunks", [])]
            except (TypeError, ValueError):
                cached = []
                entry = None
            chunks.extend(cached[: max(0, chunk_limit - len(chunks))])
            if entry is not None:
                continue

        # Keep the long-standing one-argument _parse_one seam so callers and
        # tests can instrument parsing without knowing cache policy.
        try:
            parsed, meta = _parse_one(
                f,
                max_pages=page_limit,
                # Cache a complete bounded file parse; aggregate request caps
                # are applied only when returning chunks, so a later request
                # with a larger cap can reuse the same parse correctly.
                max_chunks=DEFAULT_MAX_CHUNKS,
            )
        except TypeError as error:
            # Preserve the tiny one-argument instrumentation seam used by
            # older callers while allowing the built-in parser to enforce
            # per-call resource limits.
            if "unexpected keyword" not in str(error):
                raise
            parsed, meta = _parse_one(f)
        if source_role != DEFAULT_SOURCE_ROLE:
            parsed = [replace(c, source_role=source_role) for c in parsed]
        parsed_for_request = parsed[: max(0, chunk_limit - len(chunks))]
        chunks.extend(parsed_for_request)
        cache[f.name] = {
            "file_hash": digest,
            "parser_ver": PARSER_VERSION,
            "source_role": source_role,
            "max_pages": page_limit,
            "parse_max_chunks": DEFAULT_MAX_CHUNKS,
            "chunks": [c.__dict__ for c in parsed],
            "meta": meta,
        }
        changed = True

    # Stale cache rows are never returned and are removed on the next write.
    stale_names = [name for name in cache if name not in existing_names]
    if stale_names:
        for name in stale_names:
            del cache[name]
        changed = True

    if changed or (
        cache_path.exists()
        and not cache
        and cache_path.read_text(encoding="utf-8") not in {"{}", "{\n}"}
    ):
        _atomic_write_json(cache_path, cache)
    return chunks


def _parse_one(
    path: Path,
    *,
    max_pages: int | None = DEFAULT_MAX_PAGES,
    max_chunks: int | None = DEFAULT_MAX_CHUNKS,
):
    from .ingest import ingest_pdf

    return ingest_pdf(path, max_pages=max_pages, max_chunks=max_chunks)


def build_hybrid(
    directory: str | Path,
    *,
    cache_path: str | Path,
    max_files: int | None = DEFAULT_MAX_FILES,
    max_pages: int | None = DEFAULT_MAX_PAGES,
    max_chunks: int | None = DEFAULT_MAX_CHUNKS,
    source_role: str = DEFAULT_SOURCE_ROLE,
    embedder: Embedder | None = None,
) -> HybridRetriever:
    """Build/reuse a bounded hybrid retriever for a PDF directory."""
    from .library import Library

    chunks = load_cached_chunks(
        cache_path,
        directory,
        max_files=max_files,
        max_pages=max_pages,
        max_chunks=max_chunks,
        source_role=source_role,
    )
    embedder = embedder or HashEmbedder()
    cache_key = (
        str(Path(directory).resolve()),
        str(Path(cache_path).resolve()),
        max_files,
        max_pages,
        max_chunks,
        source_role,
        type(embedder),
        getattr(embedder, "dim", None),
    )
    chunk_key = tuple(c.chunk_id for c in chunks)
    with _INDEX_CACHE_LOCK:
        cached = _INDEX_CACHE.get(cache_key)
        if cached is not None and cached[0] == chunk_key:
            return cached[1]
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
    with _INDEX_CACHE_LOCK:
        _INDEX_CACHE[cache_key] = (chunk_key, retriever)
        # Keep an accidental stream of distinct temporary directories bounded.
        if len(_INDEX_CACHE) > 32:
            _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
    return retriever
