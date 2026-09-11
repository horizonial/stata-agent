"""Bounded, atomic ingestion cache and reusable hybrid index assembly."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Mapping
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
CACHE_SCHEMA_VERSION = 1
MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_CACHE_ENTRIES = DEFAULT_MAX_FILES
MAX_CACHE_FIELD_CHARS = 4096

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
            # Compact output keeps the derived cache within the same byte
            # budget used by the bounded reader.
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
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
    attachment_ids_by_name: Mapping[str, str] | None = None,
    ready_attachment_ids: set[str] | None = None,
    attachment_paths: Mapping[str, str | Path] | None = None,
    cache_max_bytes: int = MAX_CACHE_BYTES,
) -> list[Chunk]:
    """Load changed PDFs incrementally with bounded, validated cache data.

    Legacy cache dictionaries remain readable.  A malformed/oversized cache
    is treated as a miss and rebuilt per document; one bad document is skipped
    without poisoning healthy entries.
    """

    cache_path = Path(cache_path)
    directory = Path(directory)
    cache: dict[str, dict] = {}
    cache_needs_rewrite = False
    selected_cache_max = max(2, min(int(cache_max_bytes), MAX_CACHE_BYTES))
    if cache_path.exists():
        try:
            if cache_path.stat().st_size > selected_cache_max:
                cache_needs_rewrite = True
            else:
                with cache_path.open("rb") as stream:
                    raw_cache = stream.read(selected_cache_max + 1)
                if len(raw_cache) > selected_cache_max:
                    cache_needs_rewrite = True
                    raw_cache = b""
                loaded = json.loads(raw_cache.decode("utf-8")) if raw_cache else {}
                if isinstance(loaded, dict) and loaded.get("schema_version") == CACHE_SCHEMA_VERSION and isinstance(loaded.get("entries"), dict):
                    loaded = loaded["entries"]
                if isinstance(loaded, dict):
                    # Bound top-level materialization before accepting entries.
                    cache = {
                        str(name): entry
                        for name, entry in list(loaded.items())[:MAX_CACHE_ENTRIES]
                        if isinstance(name, str) and isinstance(entry, dict)
                    }
                    cache_needs_rewrite = len(cache) != len(loaded)
                else:
                    cache_needs_rewrite = True
        except (json.JSONDecodeError, OSError, UnicodeError, ValueError):
            cache = {}
            cache_needs_rewrite = True

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
    if attachment_paths is not None:
        file_items = []
        for attachment_id, raw_path in sorted(attachment_paths.items(), key=lambda item: str(item[0])):
            if (
                not isinstance(attachment_id, str)
                or not 1 <= len(attachment_id) <= 256
                or any(char in attachment_id for char in ("/", "\\"))
            ):
                continue
            file_items.append((f"attachment:{attachment_id}", Path(raw_path), attachment_id))
        file_items = file_items[:file_limit]
        existing_names = {name for name, _, _ in file_items}
    else:
        all_files = sorted(directory.glob("*.pdf"))
        file_items = [(f.name, f, (attachment_ids_by_name or {}).get(f.name)) for f in all_files[:file_limit]]
        existing_names = {f.name for f in all_files}

    chunks: list[Chunk] = []
    changed = cache_needs_rewrite
    for cache_name, f, attachment_id in file_items:
        if len(chunks) >= chunk_limit:
            break
        try:
            if f.is_symlink() or not f.is_file():
                continue
            digest = _file_digest(f)
        except OSError:
            continue
        entry = cache.get(cache_name)
        if ready_attachment_ids is not None and attachment_id not in ready_attachment_ids:
            # A caller with lifecycle metadata can make non-ready documents
            # contribute zero chunks without teaching the RAG layer SQLite.
            continue
        if (
            isinstance(entry, dict)
            and entry.get("file_hash") == digest
            and entry.get("parser_ver") == PARSER_VERSION
            and entry.get("source_role") == source_role
            and entry.get("max_pages") == page_limit
            and entry.get("parse_max_chunks") == DEFAULT_MAX_CHUNKS
            and entry.get("attachment_id") == attachment_id
        ):
            cached = _validated_cached_chunks(
                entry.get("chunks"),
                source_role=source_role,
                attachment_id=attachment_id,
                max_chunks=min(DEFAULT_MAX_CHUNKS, chunk_limit - len(chunks)),
            )
            if cached is not None:
                chunks.extend(cached)
                continue
            entry = None

        # Keep the long-standing one-argument _parse_one seam so callers and
        # tests can instrument parsing without knowing cache policy.
        try:
            try:
                parsed, meta = _parse_one(
                    f,
                    max_pages=page_limit,
                    max_chunks=DEFAULT_MAX_CHUNKS,
                    attachment_id=attachment_id,
                )
            except TypeError as error:
                # Preserve the tiny one-argument instrumentation seam used by
                # older callers while allowing the built-in parser to enforce
                # per-call resource limits.
                if "unexpected keyword" not in str(error):
                    raise
                try:
                    parsed, meta = _parse_one(
                        f,
                        max_pages=page_limit,
                        max_chunks=DEFAULT_MAX_CHUNKS,
                    )
                except TypeError as nested:
                    if "unexpected keyword" not in str(nested):
                        raise
                    parsed, meta = _parse_one(f)
        except Exception:
            # A malformed/hostile file is isolated to this document.  Do not
            # cache raw exception text or stop indexing other PDFs.
            continue
        try:
            parsed_items = []
            parsed_iterator = iter(parsed)
            for _ in range(DEFAULT_MAX_CHUNKS + 1):
                try:
                    parsed_items.append(next(parsed_iterator))
                except StopIteration:
                    break
            if len(parsed_items) > DEFAULT_MAX_CHUNKS:
                continue
            parsed = parsed_items
            if source_role != DEFAULT_SOURCE_ROLE:
                parsed = [replace(c, source_role=source_role) for c in parsed]
            if attachment_id:
                parsed = [replace(c, attachment_id=attachment_id) for c in parsed]
            serialised = [_chunk_to_dict(c) for c in parsed[:DEFAULT_MAX_CHUNKS]]
        except Exception:
            # A malformed parser result is isolated to its document and never
            # becomes an unbounded/partially valid cache entry.
            continue
        parsed_for_request = parsed[: max(0, chunk_limit - len(chunks))]
        chunks.extend(parsed_for_request)
        cache[cache_name] = {
            "file_hash": digest,
            "parser_ver": PARSER_VERSION,
            "source_role": source_role,
            "max_pages": page_limit,
            "parse_max_chunks": DEFAULT_MAX_CHUNKS,
            "attachment_id": attachment_id,
            "chunks": serialised,
            "meta": _safe_cache_meta(meta),
        }
        changed = True

    # Stale cache rows are never returned and are removed on the next write.
    stale_names = [name for name in cache if name not in existing_names]
    if stale_names:
        for name in stale_names:
            del cache[name]
        changed = True

    if changed or (cache_path.exists() and not cache):
        cache = _fit_cache_to_bytes(cache, selected_cache_max)
        _atomic_write_json(cache_path, _cache_payload(cache))
    return chunks


def _chunk_to_dict(chunk: Chunk) -> dict[str, object]:
    """Serialise only bounded scalar chunk metadata for the derived cache."""

    if (
        not isinstance(chunk.chunk_id, str) or not 1 <= len(chunk.chunk_id) <= 256
        or not isinstance(chunk.doc_id, str) or not 1 <= len(chunk.doc_id) <= MAX_CACHE_FIELD_CHARS
        or not isinstance(chunk.source_role, str) or not 1 <= len(chunk.source_role) <= 64
        or isinstance(chunk.page, bool) or not isinstance(chunk.page, int) or not 1 <= chunk.page <= DEFAULT_MAX_PAGES
        or not isinstance(chunk.text, str) or not 1 <= len(chunk.text) <= 1200
        or chunk.text_digest is not None and (not isinstance(chunk.text_digest, str) or len(chunk.text_digest) != 64)
        or isinstance(chunk.parser_version, bool) or not isinstance(chunk.parser_version, (str, int))
        or not 1 <= len(str(chunk.parser_version)) <= 64
        or chunk.attachment_id is not None and (not isinstance(chunk.attachment_id, str) or not 1 <= len(chunk.attachment_id) <= 256)
    ):
        raise ValueError("parser returned an out-of-bounds chunk")

    return {
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "source_role": chunk.source_role,
        "page": chunk.page,
        "text": chunk.text,
        "text_digest": chunk.text_digest,
        "parser_version": str(chunk.parser_version),
        "attachment_id": chunk.attachment_id,
    }


def _validated_cached_chunks(
    raw: object,
    *,
    source_role: str,
    attachment_id: str | None,
    max_chunks: int,
) -> list[Chunk] | None:
    if not isinstance(raw, list) or max_chunks < 0 or len(raw) > DEFAULT_MAX_CHUNKS:
        return None
    result: list[Chunk] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            return None
        required = {"chunk_id", "doc_id", "source_role", "page", "text"}
        if not required.issubset(item):
            return None
        chunk_id, doc_id, role, page, text = item["chunk_id"], item["doc_id"], item["source_role"], item["page"], item["text"]
        text_digest = item.get("text_digest")
        parser_version = item.get("parser_version", "ingest-v1")
        cached_attachment = item.get("attachment_id")
        if (
            not isinstance(chunk_id, str) or not 1 <= len(chunk_id) <= 256 or chunk_id in seen
            or not isinstance(doc_id, str) or not 1 <= len(doc_id) <= MAX_CACHE_FIELD_CHARS
            or not isinstance(role, str) or role != source_role or len(role) > 64
            or isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= DEFAULT_MAX_PAGES
            or not isinstance(text, str) or not 1 <= len(text) <= 1200
            or text_digest is not None and (not isinstance(text_digest, str) or len(text_digest) != 64)
            or isinstance(parser_version, bool) or not isinstance(parser_version, (str, int))
            or not 1 <= len(str(parser_version)) <= 64
            or cached_attachment != attachment_id
            or cached_attachment is not None and (not isinstance(cached_attachment, str) or not 1 <= len(cached_attachment) <= 256 or any(char in cached_attachment for char in ("/", "\\")))
        ):
            return None
        seen.add(chunk_id)
        result.append(Chunk(
            chunk_id=chunk_id,
            doc_id=doc_id,
            source_role=role,
            page=page,
            text=text,
            text_digest=text_digest,
            parser_version=str(parser_version),
            attachment_id=cached_attachment,
        ))
    return result[:max_chunks]


def _safe_cache_meta(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for key in ("pages", "chars", "content_hash", "source_role", "chunk_limit", "parser_version", "attachment_id"):
        child = value.get(key)
        if isinstance(child, (str, int, float, bool)) or child is None:
            result[key] = child if not isinstance(child, str) else child[:MAX_CACHE_FIELD_CHARS]
    return result


def _fit_cache_to_bytes(cache: Mapping[str, dict], maximum: int) -> dict[str, dict]:
    """Keep derived cache writes bounded even when every PDF hits hard caps."""

    result: dict[str, dict] = {}
    for name in sorted(cache):
        candidate = {name: cache[name]}
        trial = json.dumps(_cache_payload({**result, **candidate}), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(trial) > maximum:
            break
        result[name] = cache[name]
    return result


def _cache_payload(entries: Mapping[str, dict]) -> object:
    if not entries:
        # Preserve the old empty-cache representation and its deletion tests.
        return {}
    return {"schema_version": CACHE_SCHEMA_VERSION, "entries": dict(entries)}


def _parse_one(
    path: Path,
    *,
    max_pages: int | None = DEFAULT_MAX_PAGES,
    max_chunks: int | None = DEFAULT_MAX_CHUNKS,
    attachment_id: str | None = None,
):
    from .ingest import ingest_pdf

    return ingest_pdf(
        path,
        max_pages=max_pages,
        max_chunks=max_chunks,
        attachment_id=attachment_id,
    )


def build_hybrid(
    directory: str | Path,
    *,
    cache_path: str | Path,
    max_files: int | None = DEFAULT_MAX_FILES,
    max_pages: int | None = DEFAULT_MAX_PAGES,
    max_chunks: int | None = DEFAULT_MAX_CHUNKS,
    source_role: str = DEFAULT_SOURCE_ROLE,
    embedder: Embedder | None = None,
    attachment_ids_by_name: Mapping[str, str] | None = None,
    ready_attachment_ids: set[str] | None = None,
    attachment_paths: Mapping[str, str | Path] | None = None,
    cache_max_bytes: int = MAX_CACHE_BYTES,
) -> HybridRetriever:
    """Build/reuse a bounded hybrid retriever for a PDF directory.

    ``ready_attachment_ids`` and ``attachment_ids_by_name`` are optional
    lifecycle metadata supplied by the attachment service adapter.  When
    present, non-ready files are excluded before parsing; global/legacy
    directories remain compatible when omitted.
    """
    from .library import Library

    chunks = load_cached_chunks(
        cache_path,
        directory,
        max_files=max_files,
        max_pages=max_pages,
        max_chunks=max_chunks,
        source_role=source_role,
        attachment_ids_by_name=attachment_ids_by_name,
        ready_attachment_ids=ready_attachment_ids,
        attachment_paths=attachment_paths,
        cache_max_bytes=cache_max_bytes,
    )
    embedder = embedder or HashEmbedder()
    cache_key = (
        str(Path(directory).resolve()),
        str(Path(cache_path).resolve()),
        max_files,
        max_pages,
        max_chunks,
        source_role,
        tuple(sorted((attachment_ids_by_name or {}).items())),
        tuple(sorted(ready_attachment_ids)) if ready_attachment_ids is not None else None,
        tuple(sorted((str(key), str(Path(value).resolve(strict=False))) for key, value in (attachment_paths or {}).items())),
        cache_max_bytes,
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
