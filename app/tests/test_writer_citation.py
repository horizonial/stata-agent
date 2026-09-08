"""A2：引文接地 —— 只可引 citable 块、卡可签、marker 反查校验。"""

from __future__ import annotations

import pytest

from stata_agent.rag.ingest import SOURCE_ROLE_CITABLE, SOURCE_ROLE_STYLE, Chunk
from stata_agent.rag.library import Library
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.writer.citation import (
    citation_marker,
    cite_claim,
    sign_citation_card,
    validate_citations,
)
from stata_agent.writer.ground import render_claim_sentence


def _lib():
    lib = Library()
    lib.add(Chunk(chunk_id="d1", doc_id="AER2020.pdf", source_role=SOURCE_ROLE_CITABLE, page=12,
                  text="本文用双重差分识别政策效应，并进行平行趋势检验。"))
    lib.add(Chunk(chunk_id="s1", doc_id="style.pdf", source_role=SOURCE_ROLE_STYLE, page=1,
                  text="结果见表 3。"))
    return lib


def test_style_chunk_not_citable(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    lib = _lib()
    with pytest.raises(ValueError):
        sign_citation_card(store, lib, "s1")  # style_only 不许引用
    assert lib.is_citable("d1")
    store.close()


def test_sign_and_claim_citation(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    lib = _lib()
    sign_citation_card(store, lib, "d1")
    claim = cite_claim(store, "d1", "平行趋势是 DID 成立的前提。")
    proj = store.project("i1")
    assert "card-cit-d1" in proj.cards
    assert claim in proj.claims
    assert proj.cards["card-cit-d1"].kind == "citation"
    assert proj.cards["card-cit-d1"].locator["doc_id"] == "AER2020.pdf"
    store.close()


def test_validate_citations_clean_and_broken(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    lib = _lib()
    sign_citation_card(store, lib, "d1")
    claim = cite_claim(store, "d1", "平行趋势是 DID 成立的前提。")
    cards = [store.project("i1").cards[c] for c in store.project("i1").claims[claim].cards]

    good = "平行趋势是 DID 成立的前提。" + citation_marker("d1")
    assert validate_citations(good, cards) == []

    broken = "平行趋势是前提但没带引用。"
    assert any("缺引用 marker" in i for i in validate_citations(broken, cards))

    fake = "乱引" + citation_marker("ghost")
    assert any("无来源引用" in i for i in validate_citations(fake, cards))
    store.close()
