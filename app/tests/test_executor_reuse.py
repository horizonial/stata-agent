"""B2：同输入幂等复用 —— 账本已 committed → execute 直接复用，不重跑、不新增事件。"""

from __future__ import annotations

from pathlib import Path

from stata_agent.events.schema import (
    EVENT_RUN_REQ,
    EVENT_RUN_SUCCEEDED,
    ACTOR_ORCH,
    Event,
)
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.tools.executor import StataExecutor, sha


def _seed_succeeded(store, script="X"):
    h = sha(script)
    store.append(Event(idea_id="i1", event_type=EVENT_RUN_REQ, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                       operation_id="op-seed", payload={"run_id": "r0", "side_effect": "write",
                                                        "semantic_input_hash": h}))
    store.append(Event(idea_id="i1", event_type=EVENT_RUN_SUCCEEDED, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                       operation_id="op-seed",
                       payload={"run_id": "r0", "machine": {"coef": -238.9, "N": 74, "r2": 0.2196},
                                "provenance": {"do_file": "r0.do", "env_sig": {"stata_version": "18"}}}))


def test_reuse_returns_committed_without_rerun(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    _seed_succeeded(store, script="X")
    before = len(list(store.scan("i1")))

    ex = StataExecutor(store, run_root=tmp_path / "runs")
    out = ex.execute("X", idea="i1")

    assert out["reused"] is True
    assert out["run_id"] == "r0"
    assert out["machine"]["N"] == 74
    # 没有新增 run 事件（幂等，未真跑）
    assert len(list(store.scan("i1"))) == before
    store.close()
