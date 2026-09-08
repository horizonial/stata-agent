"""A3：figure 证据卡 → Word 插图。"""

from __future__ import annotations

import base64
import io

import pytest
from docx import Document

from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.writer.figure import figure_docx, sign_figure_card

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8A"
    "AusB9Y9ZQmcAAAAASUVORK5CYII="
)


def _run_store(tmp_path):
    from stata_agent.domain.action import Act, ActionProposal
    from stata_agent.events.schema import EVENT_IDEA, EVENT_PHASE, ACTOR_AGENT, Event
    from stata_agent.providers.mock import MockReplayProvider
    from stata_agent.runner import run_until_gate
    from stata_agent.tools.fake_executor import FakeExecutor

    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    store.append(Event(idea_id="i1", event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"question": "q"}))
    store.append(Event(idea_id="i1", event_type=EVENT_PHASE, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"from": "IDEA", "to": "ESTIMATION"}))
    run_until_gate(store, "i1", "跑", MockReplayProvider([
        ActionProposal(decision_summary="spec", acts=[Act(act_type="propose_spec", target={"spec_id": "s1"})]),
        ActionProposal(decision_summary="run", acts=[Act(act_type="request_run", target={"spec_id": "s1"})]),
        ActionProposal(ask_user="ok"),
    ]), executor=FakeExecutor(store), max_steps=5)
    return store


def test_sign_figure_requires_success_and_file(tmp_path):
    store = _run_store(tmp_path)
    run_id = next(iter(store.project("i1").runs))
    missing = tmp_path / "no.png"
    with pytest.raises(ValueError):
        sign_figure_card(store, run_id, missing)

    img = tmp_path / "es.png"
    img.write_bytes(PNG)
    card_id = sign_figure_card(store, run_id, img, caption="事件研究：政策前后系数")
    proj = store.project("i1")
    assert card_id in proj.cards
    assert proj.cards[card_id].kind == "figure"
    assert proj.cards[card_id].locator["run_id"] == run_id
    store.close()


def test_figure_docx_embeds_image(tmp_path):
    store = _run_store(tmp_path)
    run_id = next(iter(store.project("i1").runs))
    img = tmp_path / "coef.png"
    img.write_bytes(PNG)
    card_id = sign_figure_card(store, run_id, img, caption="coefplot")

    data = figure_docx("实证图", [{"path": str(img), "caption": "coefplot", "card_id": card_id}])
    p = tmp_path / "fig.docx"
    p.write_bytes(data)
    doc = Document(str(p))
    assert len(doc.inline_shapes) >= 1          # 真的嵌了图
    texts = [para.text for para in doc.paragraphs]
    assert any("coefplot" in t and card_id in t for t in texts)
    store.close()
