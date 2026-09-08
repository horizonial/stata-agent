"""切片 1：research_turn（单轮）——确定性、禁 act 被拒、ask 路径、全落 events。"""

from __future__ import annotations

from stata_agent.domain.action import Act, ActionProposal
from stata_agent.events.schema import EVENT_AGENT_STEP, EVENT_USER, ACTOR_USER, Event
from stata_agent.harness.research_turn import bootstrap_idea, research_turn
from stata_agent.providers.mock import MockReplayProvider
from stata_agent.storage.sqlite_store import SQLiteStore


def turn_store(tmp_path, name: str) -> SQLiteStore:
    return SQLiteStore(str(tmp_path / name), writer_id="t")


def test_ask_path_records_step_and_replies(tmp_path):
    s = turn_store(tmp_path, "a.db")
    provider = MockReplayProvider([
        ActionProposal(decision_summary="澄清样本", ask_user="用什么样本？")
    ])
    res = research_turn(s, provider)
    assert res.asked and res.reply == "用什么样本？"
    steps = [ev for ev in s.scan("i1") if ev.event_type == EVENT_AGENT_STEP]
    assert len(steps) == 1
    assert steps[0].payload["ask"] == "用什么样本？"
    s.close()


def test_forbidden_act_denied_and_no_terminal(tmp_path):
    s = turn_store(tmp_path, "a.db")
    provider = MockReplayProvider([
        ActionProposal(decision_summary="假装完成", stop_reason="done",
                       acts=[Act(act_type="mark_done", reason="模型想收尾")])
    ])
    res = research_turn(s, provider)
    assert len(res.decisions) == 1
    d = res.decisions[0]
    assert d.act == "mark_done" and d.verdict == "deny" and "无条件拒绝" in d.reason
    # 没有 phase 迁移到 DONE 之类的事件
    assert not any(ev.event_type == "phase.transition" for ev in s.scan("i1"))
    s.close()


def test_deterministic_replay_identical_steps(tmp_path):
    script = [
        ActionProposal(decision_summary="看数据", acts=[Act(act_type="inspect_data", target={})]),
        ActionProposal(ask_user="变量口径？"),
    ]
    out_a, out_b = [], []
    for name in ("a.db", "b.db"):
        s = turn_store(tmp_path, name)
        for prop_script in (script[0], script[1]):
            res = research_turn(s, MockReplayProvider([prop_script]))
            steps = [ev.payload for ev in s.scan("i1") if ev.event_type == EVENT_AGENT_STEP]
            out_a.append((steps, res.reply)) if name == "a.db" else out_b.append((steps, res.reply))
        s.close()
    assert out_a == out_b


def test_user_message_and_bootstrap_land_events(tmp_path):
    s = turn_store(tmp_path, "a.db")
    bootstrap_idea(s, "i1", "最低工资是否影响就业")
    s.append(Event(idea_id="i1", event_type=EVENT_USER, actor=ACTOR_USER, source=ACTOR_USER,
                   payload={"text": "数据在 data.csv"}))
    kinds = [ev.event_type for ev in s.scan("i1")]
    assert "idea.declared" in kinds and EVENT_USER in kinds
    s.close()
