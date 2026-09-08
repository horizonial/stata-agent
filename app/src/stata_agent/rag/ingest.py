"""摄取：PDF → 分块（带 doc/page/role，稳定 chunk_id）。切片 4 最小版。

中文/英文都用 PyMuPDF 抽文本；扫描件（文本过少）会标 ocr_needed 提示，
后续接 DD-03 视觉/OCR。分块先按"页内段落合并到约 ≤1200 字符"。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import fitz  # pymupdf

SOURCE_ROLE_CITABLE = "citable_evidence"
SOURCE_ROLE_STYLE = "style_only"


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    source_role: str
    page: int
    text: str


def _token_doc_id(path: Path) -> str:
    return f"{path.name}|{path.stat().st_size}"


def _segment(page_text: str, limit: int = 1200) -> list[str]:
    """把一页文本切成若干 ≤limit 的段（按段落断点，避免硬切语义）。"""
    paragraphs = [p.strip() for p in page_text.split("\n\n") if p.strip()]
    out: list[str] = []
    buf = ""
    for p in paragraphs:
        if len(buf) + len(p) > limit and buf:
            out.append(buf)
            buf = p
        else:
            buf = (buf + "\n\n" + p) if buf else p
    if buf:
        out.append(buf)
    return out


def ingest_pdf(path: str | Path, *, source_role: str = SOURCE_ROLE_CITABLE,
               max_pages: int | None = None) -> tuple[list[Chunk], dict]:
    """返回 (chunks, meta)。meta 含页数/文本量/是否疑似扫描件。"""
    p = Path(path)
    doc = fitz.open(p)
    chunks: list[Chunk] = []
    total_chars = 0
    pages = doc.page_count if max_pages is None else min(doc.page_count, max_pages)
    doc_id = _token_doc_id(p)
    for pno in range(pages):
        text = doc[pno].get_text("text") or ""
        total_chars += len(text.strip())
        for idx, seg in enumerate(_segment(text)):
            raw = f"{doc_id}|p{pno}|{idx}"
            chunk_id = hashlib.sha256(raw.encode()).hexdigest()[:20]
            chunks.append(Chunk(chunk_id=chunk_id, doc_id=doc_id, source_role=source_role,
                                page=pno + 1, text=seg))
    doc.close()
    meta = {
        "pages": pages,
        "chars": total_chars,
        "scanned_suspect": pages > 0 and total_chars < pages * 200,
    }
    return chunks, meta


def ingest_dir(directory: str | Path, *, source_role: str = SOURCE_ROLE_CITABLE,
               max_files: int | None = None, max_pages: int | None = None):
    """摄取目录下全部 pdf；返回 (chunks, doc_meta: dict[doc_id, meta])。"""
    d = Path(directory)
    files = sorted(d.glob("*.pdf"))
    if max_files is not None:
        files = files[:max_files]
    all_chunks: list[Chunk] = []
    meta: dict[str, dict] = {}
    for f in files:
        try:
            cs, m = ingest_pdf(f, source_role=source_role, max_pages=max_pages)
            all_chunks.extend(cs)
            meta[f.name] = m
        except Exception as e:  # noqa: BLE001
            meta[f.name] = {"error": str(e)}
    return all_chunks, meta
