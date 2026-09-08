"""C 组长会话：token 计量、压缩感知上下文(token_cap 触发)、runner 自动压缩。"""

from __future__ import annotations

from stata_agent.domain.action import Act, ActionProposal
from stata_agent.events.schema import (
    EVENT_COMPACTION,
    EVENT_IDEA,
    ACTOR_AGENT,
    Event,
)
from stata_agent.harness.safety import estimate_tokens
from stata_agent.providers.mock import MockFixedProvider
from stata_agent.runner import run_until_gate
from stata_agent.storage.sqlite_store import SQLiteStore


def _idea(store):
    store.append(Event(idea_id="i1", event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
                       payload={"question": "q"}))


def test_estimate_tokens():
    cjk = estimate_tokens("双重差分" * 10)
    ascii_t = estimate_tokens("hello world " * 10)
    assert cjk > ascii_t
    assert estimate_tokens("") == 0


def test_research_turn_compacts_over_token_cap(tmp_path):
    from stata_agent.harness.research_turn import research_turn

    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    _idea(store)

    class Cap:
        def __init__(self): self.ctx = ""
        def propose(self, ctx):
            self.ctx = ctx
            return ActionProposal(decision_summary="x", ask_user="ok")

    provider = Cap()
    # token_cap=1 → 任何非空上下文都会先压缩（摘要进 boundary）再给模型
    research_turn(store, provider, token_cap=1)
    assert "[compacted" in provider.ctx
    kinds = [e.event_type for e in store.scan("i1")]
    assert EVENT_COMPACTION in kinds
    store.close()


def test_runner_auto_compacts_and_keeps_truth(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    _idea(store)
    provider = MockFixedProvider(ActionProposal(
        decision_summary="定 spec", acts=[Act(act_type="propose_spec", target={"spec_id": "sA"})]))
    before_truth = None
    run_until_gate(store, "i1", "跑", provider, max_steps=12, auto_compact_every=3)
    kinds = [e.event_type for e in store.scan("i1")]
    assert kinds.count(EVENT_COMPACTION) >= 1
    # 压缩不破坏真相：折叠出来的状态与"只回放原始事件"一致（boundary 不影响 reducer）
    assert store.project("i1").phase == "ESTIMATION"   # spec 定稿自动推进仍在
    assert store.project("i1").research_state.current_spec_id == "sA"
    store.close()
