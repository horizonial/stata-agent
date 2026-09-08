"""B3：fork 记录 / 按分支切片 / 父链 / 换 spec 失败尝试留在原分支。"""

from __future__ import annotations

from stata_agent.events.schema import (
    EVENT_IDEA,
    EVENT_PHASE,
    EVENT_SPEC_FREEZE,
    EVENT_RUN_FAILED,
    EVENT_RUN_REQ,
    ACTOR_AGENT,
    ACTOR_ORCH,
    Event,
)
from stata_agent.harness.branch import events_in, fork, parent_of
from stata_agent.storage.sqlite_store import SQLiteStore


def _seed_then_fork(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    evs = [
        Event(idea_id="i1", event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT, payload={"question": "q"}),
        Event(idea_id="i1", event_type=EVENT_PHASE, actor=ACTOR_ORCH, source=ACTOR_ORCH,
              payload={"from": "IDEA", "to": "ESTIMATION"}),
        # 原分支一次失败的尝试
        Event(idea_id="i1", event_type=EVENT_RUN_REQ, actor=ACTOR_ORCH, source=ACTOR_ORCH,
              operation_id="op-old", fingerprint="old", payload={"run_id": "r-old", "side_effect": "read"}),
        Event(idea_id="i1", event_type=EVENT_RUN_FAILED, actor=ACTOR_ORCH, source=ACTOR_ORCH,
              operation_id="op-old", payload={"run_id": "r-old", "reason": "换 FE 前的旧 spec"}),
    ]
    for e in evs:
        store.append(e)
    fork(store, "i1", reason="改双向 FE 后重跑")
    # 新分支：新 spec + 新 run（失败尝试不覆盖，留在原分支）
    store.append(Event(idea_id="i1", event_type=EVENT_SPEC_FREEZE, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                       payload={"spec_id": "s-new"}))
    return store


def test_fork_slices_events_to_branches(tmp_path):
    store = _seed_then_fork(tmp_path)
    main = events_in(store, "i1", "main")
    branch_ids = [e.payload.get("branch_id") for e in store.scan("i1") if e.event_type == "branch.created"]
    b1 = events_in(store, "i1", branch_ids[0])

    # 原分支：只有 fork 之前的 4 条（idea/phase/run.req/run.failed）
    assert len(main) == 4
    # 新分支：fork 之后的 spec.freeze
    assert len(b1) == 1 and b1[0].event_type == EVENT_SPEC_FREEZE
    # 失败尝试仍在原分支可查（不覆盖）
    assert any(e.operation_id == "op-old" for e in main)
    assert not any(e.operation_id == "op-old" for e in b1)
    store.close()


def test_parent_chain(tmp_path):
    store = _seed_then_fork(tmp_path)
    branch_ids = [e.payload.get("branch_id") for e in store.scan("i1") if e.event_type == "branch.created"]
    assert parent_of(list(store.scan("i1")), branch_ids[0]) == "main"
    store.close()
