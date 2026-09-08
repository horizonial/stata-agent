"""runner 闭环：seed 阶段→mock 提议 request_run→FakeExecutor 执行→自动签卡→回复含数字。"""

from __future__ import annotations

from stata_agent.domain.action import Act, ActionProposal
from stata_agent.events.schema import EVENT_IDEA, EVENT_PHASE, ACTOR_AGENT, Event
from stata_agent.phase.phasedef import GateMode
from stata_agent.providers.mock import MockFixedProvider, MockReplayProvider
from stata_agent.runner import approve, cycle, run_until_gate
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.tools.fake_executor import FakeExecutor


def _seed_estimation(store: SQLiteStore, idea: str = "i1") -> None:
    store.append(Event(idea_id=idea, event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"question": "跑主回归"}))
    store.append(Event(idea_id=idea, event_type=EVENT_PHASE, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"from": "IDEA", "to": "ESTIMATION"}))


def test_closed_loop_run_sign_reply(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    _seed_estimation(store)
    provider = MockReplayProvider([
        ActionProposal(decision_summary="拟跑主回归", stop_reason=None,
                       acts=[Act(act_type="request_run", target={"spec_id": "s1"}, reason="试跑主 spec")])
    ])
    out = cycle(store, "i1", "跑主回归", provider, executor=FakeExecutor(store))

    assert out.ran_run_id and out.ran_run_id.startswith("run-fake-")
    assert out.machine["N"] == 74
    assert "-239" in out.reply or "machine" in out.reply
    assert out.signed_cards  # 自动签了 numeric 卡

    proj = store.project("i1")
    assert any(rec.status == "succeeded" for rec in proj.runs.values())
    assert set(proj.cards) >= set(out.signed_cards)
    assert f"claim-{out.ran_run_id}" in proj.claims
    store.close()


def test_runner_denies_mark_done(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    _seed_estimation(store)
    provider = MockReplayProvider([
        ActionProposal(decision_summary="假装完成", stop_reason="done",
                       acts=[Act(act_type="mark_done", reason="收尾")])
    ])
    out = cycle(store, "i1", "收尾", provider, executor=FakeExecutor(store))
    assert out.ran_run_id is None           # mark_done 被 DENY，没执行任何 run
    assert "deny" in out.reply or "无条件拒绝" in out.reply
    store.close()


def test_run_until_gate_auto_chain(tmp_path):
    """spec 提议→冻结→再问模型 request_run→执行签卡→模型问用户时停下。"""
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    _seed_estimation(store)
    provider = MockReplayProvider([
        ActionProposal(decision_summary="先定主 spec", acts=[Act(act_type="propose_spec", target={"spec_id": "s1"}, reason="主 spec")]),
        ActionProposal(decision_summary="跑主回归", acts=[Act(act_type="request_run", target={"spec_id": "s1"}, reason="跑 s1")]),
        ActionProposal(decision_summary="要看结果", ask_user="要展示吗？"),
    ])
    res = run_until_gate(store, "i1", "开始", provider, executor=FakeExecutor(store), max_steps=5)
    assert [r.progressed for r in res] == [True, True, False]
    assert res[0].frozen_specs == ["s1"]
    assert res[1].ran_run_id and res[1].signed_cards
    assert res[2].asked
    proj = store.project("i1")
    assert proj.research_state.current_spec_id == "s1"
    assert any(rec.status == "succeeded" for rec in proj.runs.values())
    store.close()


def test_formal_gate_persists_approval_then_resume(tmp_path):
    from stata_agent.events.schema import EVENT_APPROVAL_GRANT, EVENT_APPROVAL_REQ

    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    _seed_estimation(store)
    # FORMAL 门控下 propose_spec（研究变更）→ ASK：落 approval.requested，spec 不冻结
    provider = MockFixedProvider(ActionProposal(
        decision_summary="定主 spec", acts=[Act(act_type="propose_spec", target={"spec_id": "s1"}, reason="主 spec")]))
    res = run_until_gate(store, "i1", "定主 spec", provider, gate_mode=GateMode.FORMAL, max_steps=3)
    assert res[-1].awaiting_approval
    rid = res[-1].awaiting_approval
    kinds = [e.event_type for e in store.scan("i1")]
    assert EVENT_APPROVAL_REQ in kinds
    assert store.project("i1").research_state.current_spec_id is None  # 未批准前不冻结

    # 学者批准 → 事件落账本 → 续跑（新的 provider 提议 request_run）
    approve(store, rid, decision="approve", note="同意")
    kinds2 = [e.event_type for e in store.scan("i1")]
    assert EVENT_APPROVAL_GRANT in kinds2
    res2 = run_until_gate(store, "i1", None, MockReplayProvider([
        ActionProposal(decision_summary="跑主回归", acts=[Act(act_type="request_run", target={"spec_id": "s1"})])
    ]), executor=FakeExecutor(store), max_steps=3)
    assert res2 and any(r.ran_run_id for r in res2)
    store.close()


def test_pause_vs_goal_mode(tmp_path):
    """默认：agent 一提问就停等（可暂停）；goal 模式：提问不硬停，继续到无推进。"""
    ask_with_run = ActionProposal(decision_summary="边问边干", ask_user="这个口径可以吗？",
                                  acts=[Act(act_type="request_run", target={"spec_id": "s1"}, reason="跑主回归")])
    ask_only = ActionProposal(decision_summary="收尾", ask_user="好了？")

    store = SQLiteStore(str(tmp_path / "a.db"), writer_id="a")
    _seed_estimation(store)
    res_pause = run_until_gate(store, "i1", "开始", MockReplayProvider([ask_with_run, ask_only]),
                               executor=FakeExecutor(store), max_steps=5, autonomous=False)
    assert len(res_pause) == 1          # 提问即停（虽然跑了一次）
    assert res_pause[0].ran_run_id

    store2 = SQLiteStore(str(tmp_path / "b.db"), writer_id="a")
    _seed_estimation(store2)
    res_goal = run_until_gate(store2, "i1", "开始", MockReplayProvider([ask_with_run, ask_only]),
                              executor=FakeExecutor(store2), max_steps=5, autonomous=True)
    assert len(res_goal) == 2           # goal 模式：提问不硬停，直到无推进
    store.close(); store2.close()


def test_auto_advance_spec_then_estimation_without_seed(tmp_path):
    """不 seed 阶段：首次定 spec 即触发编排器推进到 ESTIMATION，随后 request_run 可跑。"""
    from stata_agent.events.schema import EVENT_PHASE

    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    store.append(Event(idea_id="i1", event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"question": "主回归"}))  # 只建档，不 seed 阶段
    res = run_until_gate(store, "i1", "开始", MockReplayProvider([
        ActionProposal(decision_summary="定主 spec", acts=[Act(act_type="propose_spec", target={"spec_id": "s1"}, reason="主 spec")]),
        ActionProposal(decision_summary="跑", acts=[Act(act_type="request_run", target={"spec_id": "s1"}, reason="跑")]),
        ActionProposal(ask_user="好了？"),
    ]), executor=FakeExecutor(store), max_steps=5)
    assert res[0].frozen_specs == ["s1"]
    # 编排器自动推进，不是 seed
    phases = [e.payload["to"] for e in store.scan("i1", event_types={EVENT_PHASE})]
    assert phases[-1] == "ESTIMATION"
    assert res[1].ran_run_id and res[1].signed_cards
    assert store.project("i1").phase == "ESTIMATION"
    store.close()


def test_run_until_gate_stops_on_budget(tmp_path):
    from stata_agent.events.schema import EVENT_BUDGET, EVENT_RUN_REQ, ACTOR_ORCH

    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    _seed_estimation(store)
    # 恒提 spec（每次都有推进），靠预算强制停
    provider = MockFixedProvider(ActionProposal(
        decision_summary="定 spec", acts=[Act(act_type="propose_spec", target={"spec_id": "sX"})]))
    res = run_until_gate(store, "i1", "开始", provider, max_steps=3)
    assert len(res) == 3                       # 到预算即停，不硬跑
    kinds = [ev.event_type for ev in store.scan("i1")]
    assert EVENT_BUDGET in kinds               # budget.limit 落账本
    assert [r.progressed for r in res] == [True, True, True]


def test_run_until_gate_stops_on_health_issue(tmp_path):
    from stata_agent.events.schema import EVENT_HEALTH, EVENT_RUN_REQ, ACTOR_ORCH

    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    _seed_estimation(store)
    # 预先塞一条"未闭合"的 run.requested（无 terminal）→ 健康检查应拦住继续
    store.append(Event(idea_id="i1", event_type=EVENT_RUN_REQ, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                       operation_id="stray", fingerprint="stray", payload={"run_id": "stray"}))
    provider = MockFixedProvider(ActionProposal(
        decision_summary="定 spec", acts=[Act(act_type="propose_spec", target={"spec_id": "sZ"})]))
    res = run_until_gate(store, "i1", "开始", provider, max_steps=5)
    assert len(res) == 1                        # step0 后健康检查发现未闭合 → 停
    kinds = [ev.event_type for ev in store.scan("i1")]
    assert EVENT_HEALTH in kinds
    assert res[0].progressed
