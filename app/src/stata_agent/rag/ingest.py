"""PDF ingestion with bounded, content-addressed chunks.

The ingestion boundary is intentionally conservative: the source role is
preserved on every chunk, document/chunk identifiers include content hashes,
and both page and chunk counts have hard defaults.  This keeps an accidentally
large library from turning a single request into an unbounded parse or prompt.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import fitz  # pymupdf

SOURCE_ROLE_CITABLE = "citable_evidence"
SOURCE_ROLE_STYLE = "style_only"
# A PDF is not citable merely because it was found in a directory.  Callers
# must opt in with ``source_role=SOURCE_ROLE_CITABLE`` for evidence use.
DEFAULT_SOURCE_ROLE = SOURCE_ROLE_STYLE

MAX_CHUNK_CHARS = 1200
DEFAULT_MAX_FILES = 256
DEFAULT_MAX_PAGES = 64
DEFAULT_MAX_CHUNKS = 10_000


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    source_role: str
    page: int
    text: str


def _content_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _token_doc_id(path: Path, content_hash: str | None = None) -> str:
    """Return a stable identity that changes when same-sized content changes."""

    return f"{path.name}|{content_hash or _content_digest(path)}"


def _segment(page_text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split a page into chunks, never exceeding the hard character limit."""

    limit = max(1, min(int(limit), MAX_CHUNK_CHARS))
    paragraphs = [p.strip() for p in page_text.split("\n\n") if p.strip()]
    out: list[str] = []
    buf = ""
    for p in paragraphs:
        # A single PDF paragraph can be much larger than the nominal limit.
        # Flush the semantic buffer first, then hard-split that paragraph so
        # the limit remains an actual invariant rather than a best effort.
        if len(p) > limit:
            if buf:
                out.append(buf)
                buf = ""
            out.extend(p[start:start + limit] for start in range(0, len(p), limit))
            continue
        separator = "\n\n" if buf else ""
        if buf and len(buf) + len(separator) + len(p) > limit:
            out.append(buf)
            buf = p
        else:
            buf = f"{buf}{separator}{p}"
    if buf:
        out.append(buf)
    return out


def ingest_pdf(path: str | Path, *, source_role: str = DEFAULT_SOURCE_ROLE,
               max_pages: int | None = DEFAULT_MAX_PAGES,
               max_chunks: int | None = DEFAULT_MAX_CHUNKS) -> tuple[list[Chunk], dict]:
    """Return ``(chunks, meta)`` with bounded pages/chunks and source role."""

    if not isinstance(source_role, str) or not source_role.strip():
        raise ValueError("source_role must be a non-empty string")
    p = Path(path)
    content_hash = _content_digest(p)
    doc = fitz.open(p)
    chunks: list[Chunk] = []
    total_chars = 0
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
    pages = min(doc.page_count, page_limit)
    doc_id = _token_doc_id(p, content_hash)
    try:
        for pno in range(pages):
            text = doc[pno].get_text("text") or ""
            total_chars += len(text.strip())
            for idx, seg in enumerate(_segment(text)):
                if len(chunks) >= chunk_limit:
                    break
                raw = f"{doc_id}|p{pno + 1}|{idx}|{hashlib.sha256(seg.encode('utf-8')).hexdigest()}"
                chunk_id = hashlib.sha256(raw.encode()).hexdigest()[:32]
                chunks.append(Chunk(chunk_id=chunk_id, doc_id=doc_id, source_role=source_role,
                                    page=pno + 1, text=seg))
            if len(chunks) >= chunk_limit:
                break
    finally:
        doc.close()
    meta = {
        "pages": pages,
        "chars": total_chars,
        "scanned_suspect": pages > 0 and total_chars < pages * 200,
        "content_hash": content_hash,
        "source_role": source_role,
        "chunk_limit": chunk_limit,
    }
    return chunks, meta


def ingest_dir(directory: str | Path, *, source_role: str = DEFAULT_SOURCE_ROLE,
               max_files: int | None = DEFAULT_MAX_FILES,
               max_pages: int | None = DEFAULT_MAX_PAGES,
               max_chunks: int | None = DEFAULT_MAX_CHUNKS):
    """Ingest a bounded PDF directory and return ``(chunks, doc_meta)``."""
    d = Path(directory)
    files = sorted(d.glob("*.pdf"))
    file_limit = (
        DEFAULT_MAX_FILES
        if max_files is None
        else max(0, min(int(max_files), DEFAULT_MAX_FILES))
    )
    files = files[:file_limit]
    chunk_limit = (
        DEFAULT_MAX_CHUNKS
        if max_chunks is None
        else max(0, min(int(max_chunks), DEFAULT_MAX_CHUNKS))
    )
    all_chunks: list[Chunk] = []
    meta: dict[str, dict] = {}
    for f in files:
        if len(all_chunks) >= chunk_limit:
            break
        try:
            cs, m = ingest_pdf(
                f,
                source_role=source_role,
                max_pages=max_pages,
                max_chunks=chunk_limit - len(all_chunks),
            )
            all_chunks.extend(cs)
            meta[f.name] = m
        except Exception as e:  # noqa: BLE001
            meta[f.name] = {"error": str(e)}
    return all_chunks, meta
