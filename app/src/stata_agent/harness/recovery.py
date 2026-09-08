"""recovery：断点续跑 / reconcile（DD-02 §7 + DD-01 §3.5）。

- reconcile_uncertain：恢复时处理 side_effect_state 未决/未闭合的执行链；
  按 decide_uncertain 给出 reuse/rerun/human 并落 system.restored（供审计）。
- 续跑本身 = 重开同一账本（events 是唯一真相），直接从当前投影继续 run_until_gate。
"""

from __future__ import annotations

from ..domain.reducers import Projection, unclosed_operations
from ..events.reconcile import SideEffect, decide_uncertain
from ..events.schema import (
    EVENT_RESTORED,
    ACTOR_ORCH,
    Event,
)
from ..storage.sqlite_store import SQLiteStore


def reconcile_uncertain(store: SQLiteStore, idea: str) -> list[str]:
    """对未闭合执行链逐条给 reconcile 决定并落 system.restored。

    返回形如 "op-xxx -> rerun" 的审计行。op 处于 uncertain/未闭合 = 正常分支不是崩溃。"""
    proj = store.project(idea)
    restored: dict[str, str] = {}
    for event in store.scan(idea, event_types={EVENT_RESTORED}):
        payload = event.payload or {}
        op = payload.get("operation_id")
        decision = payload.get("decision")
        if isinstance(op, str) and isinstance(decision, str):
            # The first decision is authoritative; duplicate legacy records do
            # not cause another restore event to be emitted.
            restored.setdefault(op, decision)
    decisions: list[str] = []
    for op in unclosed_operations(proj):
        if op in restored:
            decisions.append(f"{op} -> {restored[op]}")
            continue
        evs = [e for e in store.scan(idea, event_types={"run.requested", "tool.call"})
               if e.operation_id == op]
        side_effect_kind = "write"
        input_hash = None
        run_id = None
        attempt_id = 0
        if evs:
            side_effect_kind = (evs[0].payload or {}).get("side_effect", "write")
            input_hash = (evs[0].payload or {}).get("semantic_input_hash")
            run_id = (evs[0].payload or {}).get("run_id")
            attempt_id = evs[0].attempt_id
        binding = proj.run_bindings.get(op)
        if binding:
            run_id, bound_attempt = binding
            attempt_id = bound_attempt
        # A committed result from the same semantic input can be reused even
        # when it belongs to a different operation/attempt.
        committed = any(
            r.status == "succeeded"
            and (
                (input_hash and r.semantic_input_hash == input_hash)
                or (r.operation_id == op)
            )
            for r in proj.runs.values()
        )
        decision = decide_uncertain(
            SideEffect(kind=side_effect_kind, semantic_input_hash=input_hash),
            committed_result_exists=committed,
            can_rerun=True,
        )
        store.append(Event(idea_id=idea, event_type=EVENT_RESTORED, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                           payload={"operation_id": op, "decision": decision,
                                    "run_id": run_id, "attempt_id": attempt_id,
                                    "side_effect": side_effect_kind,
                                    "semantic_input_hash": input_hash}))
        decisions.append(f"{op} -> {decision}")
    return decisions
