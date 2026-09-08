"""L0：reconcile 决策、快照、upcaster 老事件回放。"""

from __future__ import annotations

import pytest

from stata_agent.events.reconcile import HUMAN, RERUN, REUSE, SideEffect, decide_uncertain
from stata_agent.events.schema import EVENT_SPEC_FREEZE, ACTOR_AGENT, Event
from stata_agent.events.upcast import register, upcast
from stata_agent.storage.sqlite_store import SQLiteStore


def E(idea="i1", kind="x", **kw) -> Event:
    kw.setdefault("source", ACTOR_AGENT)
    return Event(idea_id=idea, event_type=kind, **kw)


def test_reconcile_decision_table():
    read = SideEffect(kind="read")
    write = SideEffect(kind="write")
    # read 超时、结果已提交 → 复用
    assert decide_uncertain(read, committed_result_exists=True) == REUSE
    # read 超时、无结果 → 重试（可安全重放）
    assert decide_uncertain(read, committed_result_exists=False) == RERUN
    # write 超时、结果已提交 → 复用
    assert decide_uncertain(write, committed_result_exists=True) == REUSE
    # write 超时、无结果、可从 prepared 重建 → 重跑
    assert decide_uncertain(write, committed_result_exists=False, can_rerun=True) == RERUN
    # 无法重建 → 人工
    assert decide_uncertain(write, committed_result_exists=False, can_rerun=False) == HUMAN


def test_snapshot_roundtrip(tmp_path):
    s = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    s.append(E(kind=EVENT_SPEC_FREEZE, payload={"spec_id": "s1"}))
    s.append(E(kind="phase.transition", payload={"from": "IDEA", "to": "DESIGN"}))
    asof = s.save_snapshot("i1")
    snap = s.latest_snapshot("i1")
    assert snap is not None
    seq, summary = snap
    assert seq == asof == 2
    assert summary == s.project("i1").summary()
    s.close()


def test_upcast_legacy_event_replays(tmp_path):
    @register(EVENT_SPEC_FREEZE, 0)
    def _legacy(ev: Event) -> Event:
        # 老格式：payload 用 "sid"，升到 v1 用 "spec_id"
        p = dict(ev.payload)
        p["spec_id"] = p.pop("sid")
        return ev.model_copy(update={"payload": p, "schema_version": 1})

    s = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    legacy = E(kind=EVENT_SPEC_FREEZE, payload={"sid": "s9"})
    s.append(legacy.model_copy(update={"schema_version": 0}))
    proj = s.project("i1")  # 内部 upcast 后再 fold
    assert proj.summary()[2] == "s9"

    direct = upcast(legacy.model_copy(update={"schema_version": 0}))
    assert direct.schema_version == 1
    assert direct.payload["spec_id"] == "s9"
    s.close()
