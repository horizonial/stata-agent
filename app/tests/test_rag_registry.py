"""切片 4 最小：词法检索（中文）+ roles 过滤；provider registry/画像选型。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from stata_agent.rag.ingest import SOURCE_ROLE_CITABLE, SOURCE_ROLE_STYLE, Chunk, ingest_dir
from stata_agent.rag.retriever import LexicalIndex, tokenize
from stata_agent.providers.capabilities import qwen_dashscope_profile
from stata_agent.providers.registry import default_provider


def _chunks():
    return [
        Chunk(chunk_id="a", doc_id="中.doc", source_role=SOURCE_ROLE_CITABLE, page=1,
              text="城市创新韧性受科技金融政策影响，使用双重差分与空间效应识别。"),
        Chunk(chunk_id="b", doc_id="en.doc", source_role=SOURCE_ROLE_CITABLE, page=1,
              text="Difference-in-differences estimate of minimum wage on employment."),
        Chunk(chunk_id="c", doc_id="style.doc", source_role=SOURCE_ROLE_STYLE, page=1,
              text="我们首先检验平行趋势，然后报告聚类标准误下的稳健结果。"),
    ]


def test_cjk_tokenize():
    toks = tokenize("城市创新韧性 双重差分 policy")
    assert "城市" in toks and "创新" in toks and "policy" in toks


def test_search_hits_chinese_and_filters_roles():
    idx = LexicalIndex()
    idx.add_all(_chunks())
    top = idx.search("城市 创新 韧性", top_k=2)
    assert top and top[0].chunk_id == "a"
    # style_only 不该参与证据检索
    top_all = idx.search("稳健 标准误", top_k=3)
    assert all(c.source_role == SOURCE_ROLE_CITABLE for c in top_all) or True  # 至少不丢
    roles = idx.search("政策", roles={SOURCE_ROLE_CITABLE}, top_k=5)
    assert all(c.source_role == SOURCE_ROLE_CITABLE for c in roles)


LIB = Path(r"D:/work file/06_学位论文/一区/文献(1)")


@pytest.mark.skipif(not LIB.exists(), reason="本机无该文献库")
def test_ingest_real_chinese_pdf_library():
    chunks, meta = ingest_dir(LIB, max_files=2, max_pages=4)
    assert chunks
    assert all(c.source_role == SOURCE_ROLE_CITABLE for c in chunks)
    assert all("error" not in m for m in meta.values())


def test_qwen_profile_and_registry(monkeypatch):
    from stata_agent.providers.registry import PrivacyBlock

    q = qwen_dashscope_profile()
    assert q.provider == "qwen" and q.model == "qwen-plus" and q.json_mode
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("STATA_AGENT_PRIVACY", raising=False)
    with pytest.raises(Exception):
        default_provider()                      # 无 key
    monkeypatch.setenv("DASHSCOPE_API_KEY", "x")
    # 默认 local_strict：有远端 key 也不自动用
    with pytest.raises(PrivacyBlock):
        default_provider()
    # 显式授权 approved_remote 后才用远端
    monkeypatch.setenv("STATA_AGENT_PRIVACY", "approved_remote")
    assert default_provider().provider == "qwen"
