"""压缩(compaction.boundary) + 记忆(MemoryStore/决策记忆→上下文) —— 整体未测的两块。"""

from __future__ import annotations

from pathlib import Path

from stata_agent.domain.action import Act, ActionProposal
from stata_agent.events.schema import (
    EVENT_COMPACTION,
    EVENT_IDEA,
    EVENT_PHASE,
    ACTOR_AGENT,
    Event,
)
from stata_agent.harness.compaction import build_context_after, compact
from stata_agent.memory.memstore import MemoryStore, remember_decision
from stata_agent.phase.phasedef import GateMode
from stata_agent.providers.mock import MockReplayProvider
from stata_agent.runner import approve, cycle, run_until_gate
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.tools.fake_executor import FakeExecutor


def _seed(store):
    store.append(Event(idea_id="i1", event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"question": "q"}))
    store.append(Event(idea_id="i1", event_type=EVENT_PHASE, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"from": "IDEA", "to": "ESTIMATION"}))


def _run_until_claim(store) -> None:
    run_until_gate(store, "i1", "跑", MockReplayProvider([
        ActionProposal(decision_summary="spec", acts=[Act(act_type="propose_spec", target={"spec_id": "s1"})]),
        ActionProposal(decision_summary="run", acts=[Act(act_type="request_run", target={"spec_id": "s1"})]),
        ActionProposal(decision_summary="ask", ask_user="好了？"),
    ]), executor=FakeExecutor(store), max_steps=5)


def test_compaction_writes_boundary_and_preserves_truth(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    _seed(store)
    _run_until_claim(store)
    n_before = len(list(store.scan("i1")))
    before = store.project("i1").summary()

    out = compact(store, "i1", reason="phase_tail")
    assert out["seq"] > 0
    assert "已确认 claim" in out["summary"] or "已确认结论" in out["summary"]
    kinds = [e.event_type for e in store.scan("i1")]
    assert EVENT_COMPACTION in kinds
    assert len(list(store.scan("i1"))) == n_before + 1   # 原始事件不删，只追加 boundary
    assert store.project("i1").summary() == before        # 真相不变

    ctx = build_context_after(store, "i1")
    assert "[compacted" in ctx and "tail" in ctx
    assert "已确认结论(claims): 1" in ctx or "claims" in ctx
    store.close()


def test_memory_add_search_touch_prune(tmp_path):
    m = MemoryStore(tmp_path / "memory.json")
    a = m.add("偏好：结果表默认放 Word 而非 LaTeX", kind="preference")
    d = m.add("研究决定：用城市和年份双向固定效应", kind="decision")
    m.touch(a["id"])
    assert len(m.all()) == 2
    assert m.search("固定效应 城市")[0]["id"] == d["id"]
    assert any("研究决定" in line for line in m.to_context())
    assert m.prune_unused(min_used=1) == 1   # 只剪掉没被用过的 d
    assert len(m.all()) == 1
    # 重开可持久
    m2 = MemoryStore(tmp_path / "memory.json")
    assert len(m2.all()) == 1


def test_decision_note_flows_into_next_context(tmp_path):
    from stata_agent.domain.action import ActionProposal as AP

    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    _seed(store)
    mem = MemoryStore(tmp_path / "memory.json")

    # FORMAL 门控下 propose_spec → ASK → 批准带 note → 记成决策记忆
    res = run_until_gate(store, "i1", "定 spec", MockReplayProvider([
        AP(decision_summary="spec", acts=[Act(act_type="propose_spec", target={"spec_id": "s1"})]),
    ]), gate_mode=GateMode.FORMAL, max_steps=2)
    rid = res[-1].awaiting_approval
    approve(store, rid, decision="approve", note="用城市和年份固定效应", memory=mem)
    assert any("研究决定" in e["text"] for e in mem.all())

    # 下一轮上下文应带上该记忆（约束）
    class Cap:
        def __init__(self, p): self.p = p; self.ctx = ""
        def propose(self, ctx):
            self.ctx = ctx
            return self.p

    p = Cap(AP(decision_summary="继续", ask_user="继续？"))
    cycle(store, "i1", "继续", p, memory=mem)
    assert "研究决定：用城市和年份固定效应" in p.ctx
    store.close()
