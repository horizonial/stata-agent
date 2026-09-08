"""A1：esttab/TSV 表语义 → TableModel；数字格 round-trip；Word 表。"""

from __future__ import annotations

from docx import Document

from stata_agent.domain.models import EvidenceCard
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.tools.fake_executor import FakeExecutor
from stata_agent.writer.docx_out import tables_to_docx
from stata_agent.writer.table import (
    build_run_table,
    numeric_cells,
    render_markdown,
    table_from_tsv,
    validate_cells,
)

_TSV = (
    "model\t(1) 主回归\n"
    "mpg\t-238.894\n"
    "Observations\t74\n"
    "R-squared\t0.220\n"
    "FE\tYes\n"
)


def test_table_from_tsv_and_markdown():
    model = table_from_tsv(_TSV, title="回归")
    assert model.columns == ["model", "(1) 主回归"]
    assert model.rows[0].label == "mpg"
    assert model.rows[0].cells[0].numeric == -238.894
    assert model.rows[-1].cells[0].numeric is None  # "Yes" 不是数字
    md = render_markdown(model)
    assert "| 指标 | model | (1) 主回归 |" in md and "mpg" in md


def _cards() -> list[EvidenceCard]:
    return [
        EvidenceCard(card_id="c-coef", kind="numeric",
                     locator={"run_id": "r1", "stat_type": "coef"}, value={"value": -238.894}),
        EvidenceCard(card_id="c-N", kind="numeric",
                     locator={"run_id": "r1", "stat_type": "N"}, value={"value": 74}),
        EvidenceCard(card_id="c-r2", kind="numeric",
                     locator={"run_id": "r1", "stat_type": "r2"}, value={"value": 0.2196}),
    ]


def test_numeric_cells_iteration():
    model = table_from_tsv(_TSV)
    nums = [cell for _, _, cell in numeric_cells(model)]
    assert [c.numeric for c in nums] == [-238.894, 74.0, 0.22]


def test_validate_clean_and_tampered():
    from stata_agent.writer.ground import display_for

    cards = _cards()
    good = table_from_tsv(_TSV)
    # 把我们显示策略下的格文本换成 canonical（这样数字来自卡）
    for ri, ci, cell in numeric_cells(good):
        cell.text = display_for("coef" if ci == 0 and ri == 0 else ("N" if "Observ" in good.rows[ri].label else "r2"), cell.numeric)
    assert validate_cells(good, cards) == []

    tampered = table_from_tsv(_TSV)
    tampered.rows[0].cells[0].text = "-238.800"
    assert "-238.800" in validate_cells(tampered, cards)


def test_build_run_table_and_docx(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    from stata_agent.domain.action import Act, ActionProposal
    from stata_agent.events.schema import EVENT_IDEA, EVENT_PHASE, ACTOR_AGENT, Event
    from stata_agent.providers.mock import MockReplayProvider
    from stata_agent.runner import run_until_gate

    store.append(Event(idea_id="i1", event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"question": "q"}))
    store.append(Event(idea_id="i1", event_type=EVENT_PHASE, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"from": "IDEA", "to": "ESTIMATION"}))
    run_until_gate(store, "i1", "跑", MockReplayProvider([
        ActionProposal(decision_summary="spec", acts=[Act(act_type="propose_spec", target={"spec_id": "s1"})]),
        ActionProposal(decision_summary="run", acts=[Act(act_type="request_run", target={"spec_id": "s1"})]),
        ActionProposal(ask_user="ok"),
    ]), executor=FakeExecutor(store), max_steps=5)
    proj = store.project("i1")
    run_id = next(iter(proj.runs))
    model = build_run_table(proj, run_id, title="主回归结果")

    labels = [row.label for row in model.rows]
    assert labels == ["核心系数", "样本量", "R²"]
    assert all(row.cells[0].card_id for row in model.rows)  # 每格挂卡

    cards = [proj.cards[c] for c in next(iter(proj.claims.values())).cards]
    assert validate_cells(model, cards) == []

    buf = tables_to_docx("实证初稿", [model])
    p = tmp_path / "t.docx"
    p.write_bytes(buf.getvalue())
    doc = Document(str(p))
    texts = [para.text for para in doc.paragraphs]
    # Word 表格行文本在 cell 里；粗查标题在
    assert any("主回归结果" in t for t in texts)
    # 表格单元格里至少含系数值
    allcell = " | ".join(c.text for row in doc.tables[0].rows for c in row.cells)
    assert "核心系数" in allcell and "样本量" in allcell
    store.close()
