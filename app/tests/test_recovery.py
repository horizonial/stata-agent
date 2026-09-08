"""断点续跑 + reconcile（第 3 项）。

A. "进程停"后重开同一账本 → 从当前投影续跑（events 是唯一真相）。
B. 未闭合/uncertain 执行链 → reconcile 落 system.restored 供审计。
"""

from __future__ import annotations

from stata_agent.domain.action import Act, ActionProposal
from stata_agent.events.schema import (
    EVENT_IDEA,
    EVENT_PHASE,
    EVENT_RESTORED,
    EVENT_RUN_REQ,
    ACTOR_AGENT,
    ACTOR_ORCH,
    Event,
)
from stata_agent.harness.recovery import reconcile_uncertain
from stata_agent.providers.mock import MockReplayProvider
from stata_agent.runner import run_until_gate
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.tools.fake_executor import FakeExecutor


def _seed(store):
    store.append(Event(idea_id="i1", event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"question": "q"}))
    store.append(Event(idea_id="i1", event_type=EVENT_PHASE, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"from": "IDEA", "to": "ESTIMATION"}))


def test_resume_after_process_stop(tmp_path):
    path = str(tmp_path / "l.db")
    # —— 进程 1：定好 spec 后"停"（模拟中断）——
    s1 = SQLiteStore(path, writer_id="p1")
    _seed(s1)
    run_until_gate(s1, "i1", "跑主回归", MockReplayProvider([
        ActionProposal(decision_summary="定 spec", acts=[Act(act_type="propose_spec", target={"spec_id": "s1"})])
    ]), max_steps=3)
    assert s1.project("i1").research_state.current_spec_id == "s1"  # 定好 spec
    s1.close()

    # —— 进程 2：新写者接管（模拟崩溃后恢复），从事件表续跑 request_run ——
    s2 = SQLiteStore(path, writer_id="p2", takeover=True)
    assert s2.project("i1").research_state.current_spec_id == "s1"   # 状态来自事件重放
    res = run_until_gate(s2, "i1", None, MockReplayProvider([
        ActionProposal(decision_summary="跑 s1", acts=[Act(act_type="request_run", target={"spec_id": "s1"}, reason="跑主回归")]),
        ActionProposal(ask_user="要展示吗？"),
    ]), executor=FakeExecutor(s2), max_steps=4)
    assert res[-1].asked
    proj = s2.project("i1")
    assert any(rec.status == "succeeded" for rec in proj.runs.values())
    assert len([c for c in proj.cards]) >= 3   # 续跑后自动签卡
    # spec 只冻结了一次（续跑不重复）
    assert len([e for e in s2.scan("i1", event_types={"spec.frozen"})]) == 1
    s2.close()


def test_reconcile_records_restored_for_unclosed(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    _seed(store)
    store.append(Event(idea_id="i1", event_type=EVENT_RUN_REQ, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                       operation_id="stray", fingerprint="stray",
                       payload={"run_id": "stray", "side_effect": "read"}))
    decisions = reconcile_uncertain(store, "i1")
    assert decisions and "stray -> " in decisions[0]
    kinds = [e.event_type for e in store.scan("i1")]
    assert EVENT_RESTORED in kinds
    store.close()
