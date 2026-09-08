"""FakeExecutor：离线确定性版 StataExecutor（测 loop 接线/自动签卡用，不碰 Stata）。

与真 executor 同接口 execute(script, idea=..., run_id=..., spec_id=..., side_effect=...)。
机器值取 auto 回归的稳定已知结果（price vs mpg：coef -238.9, N=74, r2 .2196）。
"""

from __future__ import annotations

import uuid

from ..events.schema import (
    EVENT_RUN_REQ,
    EVENT_RUN_SUCCEEDED,
    ACTOR_ORCH,
    Event,
)
from ..storage.sqlite_store import SQLiteStore

_MACHINE = {"coef": -238.9, "N": 74, "r2": 0.2196}


class FakeExecutor:
    def __init__(self, store: SQLiteStore):
        self._store = store

    def execute(self, script: str, *, idea: str = "i1", run_id: str | None = None,
                spec_id: str | None = None, side_effect: str = "write") -> dict:
        run_id = run_id or f"run-fake-{uuid.uuid4().hex[:8]}"
        op = f"op-{run_id}"
        attempt = len(self._store.project(idea).runs) + 1
        self._store.append(Event(
            idea_id=idea, event_type=EVENT_RUN_REQ, actor=ACTOR_ORCH, source=ACTOR_ORCH,
            operation_id=op, fingerprint=f"fake-{run_id}", attempt_id=attempt,
            side_effect_state="running",
            payload={"run_id": run_id, "spec_id": spec_id, "side_effect": side_effect},
        ))
        prov = {"do_file": f"fake:{run_id}.do", "command_hash": f"fake-{run_id}", "data_signature": None,
                "env_sig": {"stata_version": "fake", "stata_flavor": "fake"}}
        self._store.append(Event(
            idea_id=idea, event_type=EVENT_RUN_SUCCEEDED, actor=ACTOR_ORCH, source=ACTOR_ORCH,
            operation_id=op, side_effect_state="committed",
            payload={"run_id": run_id, "spec_id": spec_id, "provenance": prov, "machine": dict(_MACHINE)},
        ))
        return {"run_id": run_id, "machine": dict(_MACHINE), "env": prov["env_sig"],
                "do_file": prov["do_file"], "command_hash": prov["command_hash"]}
