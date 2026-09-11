"""端到端回归：一句话 → spec → run(证据) → 签卡/claim → 结果句 round-trip → .docx。

全程离线（FakeExecutor + 确定性 mock），把主链路钉死防回归。
"""

from __future__ import annotations

from io import BytesIO
import base64
import json
from zipfile import ZipFile

from docx import Document

from stata_agent.domain.action import ActionProposal
from stata_agent.domain.action import Act
from stata_agent.events.schema import EVENT_IDEA, EVENT_PHASE, ACTOR_AGENT, Event
from stata_agent.providers.mock import MockReplayProvider
from stata_agent.rag.ingest import SOURCE_ROLE_CITABLE, Chunk
from stata_agent.rag.library import Library
from stata_agent.rag.retriever import LexicalIndex
from stata_agent.runner import run_until_gate
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.tools.fake_executor import FakeExecutor
from stata_agent.writer.docx_out import claims_to_docx
from stata_agent.writer.citation import cite_claim, citation_marker, sign_citation_card
from stata_agent.writer.draft_multi import build_draft_package
from stata_agent.writer.figure import sign_figure_card
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


def test_e2e_mixed_evidence_roundtrip_manifest(tmp_path):
    store = SQLiteStore(str(tmp_path / "mixed.sqlite3"), writer_id="mixed")
    store.append(Event(idea_id="i1", event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"question": "主回归"}))
    store.append(Event(idea_id="i1", event_type=EVENT_PHASE, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"from": "IDEA", "to": "ESTIMATION"}))
    run_until_gate(
        store,
        "i1",
        "跑主回归",
        MockReplayProvider([
            ActionProposal(decision_summary="定 spec", acts=[Act(act_type="propose_spec", target={"spec_id": "s1"})]),
            ActionProposal(decision_summary="跑", acts=[Act(act_type="request_run", target={"spec_id": "s1"})]),
            ActionProposal(ask_user="ok"),
        ]),
        executor=FakeExecutor(store),
        max_steps=5,
    )
    run_id = next(iter(store.project("i1").runs))
    from stata_agent.tools.evidence_signer import sign_run_numeric_cards

    numeric_ids = sign_run_numeric_cards(store, run_id, claim_statement="主回归结果")
    library = Library()
    chunk = _index().search("双重差分", top_k=1, roles={SOURCE_ROLE_CITABLE})[0]
    library.add(chunk)
    sign_citation_card(store, library, chunk.chunk_id)
    cite_claim(store, chunk.chunk_id, "方法需要平行趋势。", extra_cards=numeric_ids[:1])

    image = tmp_path / "coef.png"
    image.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8A"
        "AusB9Y9ZQmcAAAAASUVORK5CYII="
    ))
    figure_id = sign_figure_card(store, run_id, image, caption="事件研究")
    data, manifest = build_draft_package(
        store.project("i1"),
        library=library,
        figure_items=[{
            "object_id": "figure:event-study",
            "path": str(image),
            "card_id": figure_id,
            "run_id": run_id,
            "caption": "事件研究",
        }],
    )

    assert manifest.kind == "evidence-manifest.v1"
    assert manifest.delivery_digest
    assert any(obj["object_type"] == "figure" for obj in manifest.objects)
    assert "card-cit-" in json.dumps(manifest.to_dict(), ensure_ascii=False)
    with ZipFile(BytesIO(data)) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
    assert citation_marker(chunk.chunk_id) in document_xml
    assert figure_id in document_xml
    store.close()
