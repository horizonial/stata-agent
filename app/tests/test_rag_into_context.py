"""RAG → runner context：文献命中要进模型上下文（LITERATURE/DESIGN 取证）。"""

from __future__ import annotations

from stata_agent.domain.action import ActionProposal
from stata_agent.events.schema import EVENT_IDEA, EVENT_PHASE, ACTOR_AGENT, Event
from stata_agent.rag.ingest import SOURCE_ROLE_CITABLE, Chunk
from stata_agent.rag.retriever import LexicalIndex
from stata_agent.runner import cycle, literature_context
from stata_agent.storage.sqlite_store import SQLiteStore


class RecordingProvider:
    """记录最后一次 context，便于断言检索进了 prompt。"""

    def __init__(self, proposal: ActionProposal):
        self._p = proposal
        self.last_context = ""

    def propose(self, context: str):
        self.last_context = context
        return self._p


def _index():
    idx = LexicalIndex()
    idx.add(Chunk(chunk_id="d1", doc_id="文献DID.pdf", source_role=SOURCE_ROLE_CITABLE, page=3,
                  text="双重差分需平行趋势检验，事件研究作图，再以聚类稳健标准误报告。"))
    return idx


def test_literature_context_returns_citable_chunk():
    lines = literature_context("双重差分 平行趋势 怎么做", _index())
    assert lines and "文献DID.pdf" in lines[0] and "双重差分" in lines[0]


def test_cycle_passes_retrieval_into_model_context(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    store.append(Event(idea_id="i1", event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"question": "q"}))
    store.append(Event(idea_id="i1", event_type=EVENT_PHASE, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"from": "IDEA", "to": "DESIGN"}))
    prov = RecordingProvider(ActionProposal(decision_summary="查文献再答", ask_user="我看到做法了，要不要补数据"))
    cycle(store, "i1", "双重差分怎么做？", prov, index=_index())
    assert "文献DID.pdf" in prov.last_context
    assert "双重差分" in prov.last_context
    store.close()
