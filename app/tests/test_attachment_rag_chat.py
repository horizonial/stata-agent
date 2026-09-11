from __future__ import annotations

import json
from pathlib import Path

from stata_agent.rag.embed import HashEmbedder
from stata_agent.rag.hybrid import HybridRetriever, VectorIndex
from stata_agent.rag.index import build_hybrid, load_cached_chunks
from stata_agent.rag.ingest import SOURCE_ROLE_CITABLE, SOURCE_ROLE_STYLE, Chunk
from stata_agent.rag.retriever import LexicalIndex


def test_attachment_filter_and_stable_tie_breaking() -> None:
    chunks = [
        Chunk("b", "attachment:b|hash|v", SOURCE_ROLE_STYLE, 1, "same policy text", attachment_id="b"),
        Chunk("a", "attachment:a|hash|v", SOURCE_ROLE_STYLE, 1, "same policy text", attachment_id="a"),
        Chunk("c", "global", SOURCE_ROLE_CITABLE, 1, "same policy text"),
    ]
    lexical = LexicalIndex()
    vector = VectorIndex(HashEmbedder())
    for chunk in chunks:
        lexical.add(chunk)
        vector.add(chunk)
    retriever = HybridRetriever(lexical, vector)
    assert [chunk.chunk_id for chunk in retriever.search("policy", top_k=10)] == ["a", "b", "c"]
    assert [chunk.attachment_id for chunk in retriever.search("policy", top_k=10, attachment_ids={"b"})] == ["b"]
    assert retriever.search("policy", top_k=0) == []


def test_cache_oversize_and_bad_document_are_bounded(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "good.pdf").write_bytes(b"%PDF-good")
    (source / "bad.pdf").write_bytes(b"%PDF-bad")
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"bad": {"chunks": ["x"]}, "good": {"chunks": ["x"]}}), encoding="utf-8")

    def parse(path, **kwargs):
        del kwargs
        if Path(path).name == "bad.pdf":
            raise RuntimeError("malformed")
        return [Chunk("g", "attachment:g|hash|v", SOURCE_ROLE_STYLE, 1, "safe", attachment_id="g")], {}

    from stata_agent.rag import index as rag_index

    monkeypatch.setattr(rag_index, "_parse_one", parse)
    chunks = load_cached_chunks(cache, source, cache_max_bytes=1024)
    assert [chunk.chunk_id for chunk in chunks] == ["g"]
    assert cache.stat().st_size <= 1024


def test_attachment_id_is_ingest_identity(tmp_path: Path) -> None:
    import fitz

    path = tmp_path / "one.pdf"
    document = fitz.open()
    document.new_page().insert_text((72, 72), "attachment text")
    document.save(str(path))
    document.close()

    retriever = build_hybrid(
        tmp_path,
        cache_path=tmp_path / "cache.json",
        attachment_ids_by_name={path.name: "opaque-1"},
        ready_attachment_ids={"opaque-1"},
    )
    found = retriever.search("attachment", top_k=3, attachment_ids={"opaque-1"})
    assert found and found[0].attachment_id == "opaque-1"
    assert "opaque-1" in found[0].doc_id and str(tmp_path) not in found[0].doc_id


def test_attachment_path_map_is_ready_only_and_opaque(tmp_path: Path) -> None:
    import fitz

    pdf = tmp_path / "object.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "object attachment")
    document.save(str(pdf))
    document.close()
    retriever = build_hybrid(
        tmp_path,
        cache_path=tmp_path / "attachment-cache.json",
        attachment_paths={"opaque-ready": pdf},
        ready_attachment_ids={"opaque-ready"},
    )
    found = retriever.search("object", attachment_ids={"opaque-ready"})
    assert found and found[0].attachment_id == "opaque-ready"
