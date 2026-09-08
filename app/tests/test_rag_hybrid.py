"""D：向量嵌入(本地) / 混合检索(RRF) / 摄取缓存幂等。"""

from __future__ import annotations

import json
from pathlib import Path

from stata_agent.rag.embed import HashEmbedder, cosine
from stata_agent.rag.hybrid import HybridRetriever, VectorIndex
from stata_agent.rag.index import build_hybrid, load_cached_chunks
from stata_agent.rag.ingest import SOURCE_ROLE_CITABLE, SOURCE_ROLE_STYLE, Chunk
from stata_agent.rag.retriever import LexicalIndex

_CHUNKS = [
    Chunk(chunk_id="c1", doc_id="a", source_role=SOURCE_ROLE_CITABLE, page=1,
          text="城市创新韧性受科技金融政策影响，用双重差分识别。"),
    Chunk(chunk_id="c2", doc_id="b", source_role=SOURCE_ROLE_CITABLE, page=2,
          text="稳健性检验报告聚类标准误下的结果。"),
    Chunk(chunk_id="c3", doc_id="s", source_role=SOURCE_ROLE_STYLE, page=1,
          text="The table reports difference-in-differences estimates."),
]


def test_hash_embed_cosine_similarity():
    em = HashEmbedder()
    v1 = em.embed_text("城市创新韧性 双重差分")
    v2 = em.embed_text("创新韧性 双重差分 政策")
    v3 = em.embed_text("完全无关的海鲜烹饪方法介绍")
    assert cosine(v1, v2) > cosine(v1, v3)


def test_vector_index_and_rrf_roles_filter():
    em = HashEmbedder()
    vector = VectorIndex(em)
    lex = LexicalIndex()
    for c in _CHUNKS:
        lex.add(c)
        vector.add(c)
    hr = HybridRetriever(lex, vector)
    top = hr.search("双重差分 韧性", top_k=2, roles={SOURCE_ROLE_CITABLE})
    assert top and all(c.source_role == SOURCE_ROLE_CITABLE for c in top)
    # 语义/词法都命中的 c1 应排最前
    assert top[0].chunk_id == "c1"


def _make_pdf(path: Path) -> None:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Difference in differences estimation with clustered standard errors.")
    doc.save(str(path))
    doc.close()


def test_ingest_cache_idempotent(tmp_path, monkeypatch):
    from stata_agent.rag import index as rag_index

    index = rag_index
    src = tmp_path / "src"
    src.mkdir()
    pdf = src / "paper.pdf"
    _make_pdf(pdf)
    cache = tmp_path / "cache.json"

    parse_count = {"n": 0}
    real = index._parse_one

    def counting(path):
        parse_count["n"] += 1
        return real(path)

    monkeypatch.setattr(index, "_parse_one", counting)
    first = load_cached_chunks(cache, src)
    assert parse_count["n"] == 1 and first
    assert cache.exists()

    second = load_cached_chunks(cache, src)
    assert parse_count["n"] == 1  # 命中缓存，不重解析
    assert len(second) == len(first)
