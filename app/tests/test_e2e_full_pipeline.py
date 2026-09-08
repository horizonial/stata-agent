"""端到端回归：一句话 → spec → run(证据) → 签卡/claim → 结果句 round-trip → .docx。

全程离线（FakeExecutor + 确定性 mock），把主链路钉死防回归。
"""

from __future__ import annotations

from io import BytesIO

from docx import Document

from stata_agent.domain.action import ActionProposal
from stata_agent.domain.action import Act
from stata_agent.events.schema import EVENT_IDEA, EVENT_PHASE, ACTOR_AGENT, Event
from stata_agent.providers.mock import MockReplayProvider
from stata_agent.rag.ingest import SOURCE_ROLE_CITABLE, Chunk
from stata_agent.rag.retriever import LexicalIndex
from stata_agent.runner import run_until_gate
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.tools.fake_executor import FakeExecutor
from stata_agent.writer.docx_out import claims_to_docx
from stata_agent.writer.ground import render_claim_sentence, validate_sentence


class RecordingProvider(MockReplayProvider):
    def __init__(self, proposals):
        super().__init__(proposals)
        self.contexts: list[str] = []

    def propose(self, context: str):
        self.contexts.append(context)
        return super().propose(context)


def _index():
    idx = LexicalIndex()
    idx.add(Chunk(chunk_id="d1", doc_id="方法论.pdf", source_role=SOURCE_ROLE_CITABLE, page=2,
                  text="双重差分需平行趋势，聚类稳健标准误报告。"))
    return idx


def test_e2e_word_to_docx(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="e2e")
    store.append(Event(idea_id="i1", event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"question": "主回归"}))
    store.append(Event(idea_id="i1", event_type=EVENT_PHASE, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"from": "IDEA", "to": "ESTIMATION"}))

    prov = RecordingProvider([
        ActionProposal(decision_summary="定 spec", acts=[Act(act_type="propose_spec", target={"spec_id": "s1"})]),
        ActionProposal(decision_summary="跑主回归", acts=[Act(act_type="request_run", target={"spec_id": "s1"}, reason="跑主回归")]),
        ActionProposal(decision_summary="出结果句", ask_user="出稿给你看？"),
    ])
    res = run_until_gate(store, "i1", "用双重差分加聚类稳健标准误，跑 price 对 mpg 主回归", prov,
                         executor=FakeExecutor(store), index=_index(), max_steps=6)

    # 1) RAG 证据进了第一轮模型上下文
    assert "方法论.pdf" in prov.contexts[0]

    # 2) 冻结 + 跑 + 签卡/claim
    assert res[0].frozen_specs == ["s1"]
    assert res[1].ran_run_id and res[1].signed_cards
    proj = store.project("i1")
    claim = next(iter(proj.claims.values()))
    cards = [proj.cards[c] for c in claim.cards]

    # 3) 结果句 round-trip：渲染出的数字全部可溯源
    sentence = render_claim_sentence(claim, proj.cards)
    assert validate_sentence(sentence, cards) == []

    # 4) 拼 .docx
    buf = claims_to_docx("主回归结果", [(sentence, claim)])
    p = tmp_path / "draft.docx"
    p.write_bytes(buf.getvalue())
    doc = Document(str(p))
    texts = [par.text for par in doc.paragraphs]
    assert any("主回归结果" in t for t in texts)
    store.close()
