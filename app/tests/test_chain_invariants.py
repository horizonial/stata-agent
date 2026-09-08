"""L0：执行链 closure / 非法序列拒绝 / 取消带原 id / 未终结探测。"""

from __future__ import annotations

import pytest

from stata_agent.domain.reducers import IllegalEventSequence, empty, fold, unclosed_operations
from stata_agent.events.schema import (
    EVENT_RUN_FAILED,
    EVENT_RUN_REQ,
    EVENT_RUN_SUCCEEDED,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    ACTOR_ORCH,
    Event,
)


def ev(idea="i1", kind="x", **kw) -> Event:
    kw.setdefault("source", ACTOR_ORCH)
    kw.setdefault("actor", ACTOR_ORCH)
    return Event(idea_id=idea, event_type=kind, **kw)


def full_chain(op="op1", run="r1"):
    return [
        ev(kind=EVENT_RUN_REQ, operation_id=op, fingerprint=f"h-{op}",
           payload={"run_id": run, "side_effect": "write"}),
        ev(kind=EVENT_TOOL_CALL, operation_id=op, fingerprint=f"h2-{op}", payload={"run_id": run}),
        ev(kind=EVENT_TOOL_RESULT, operation_id=op, payload={"run_id": run, "rc": 0}),
        ev(kind=EVENT_RUN_SUCCEEDED, operation_id=op,
           payload={"run_id": run, "provenance": {"do_file": "a.do", "data_signature": "ds"},
                    "machine": {"coef": 0.038}}),
    ]


def test_full_chain_folds_to_success():
    proj = fold(full_chain())
    assert ("r1", "succeeded") in proj.summary()[5]  # summary: idea,phase,spec,claims,cards,runs,refs
    assert unclosed_operations(proj) == []


def test_terminal_without_start_rejected():
    bad = [ev(kind=EVENT_RUN_SUCCEEDED, operation_id="op9",
              payload={"run_id": "r9", "machine": {}})]
    with pytest.raises(IllegalEventSequence):
        fold(bad)


def test_double_start_rejected():
    bad = full_chain()[:1] + full_chain()[:1]  # 两个 run.requested 同 op
    with pytest.raises(IllegalEventSequence):
        fold(bad)


def test_double_terminal_rejected():
    two = full_chain() + [ev(kind=EVENT_RUN_SUCCEEDED, operation_id="op1",
                             payload={"run_id": "r1", "machine": {}})]
    with pytest.raises(IllegalEventSequence):
        fold(two)


def test_unclosed_operation_detected():
    proj = fold([ev(kind=EVENT_RUN_REQ, operation_id="op1", fingerprint="h",
                    payload={"run_id": "r1", "side_effect": "read"})])
    assert unclosed_operations(proj) == ["op1"]


def test_cancel_result_keeps_original_op_id():
    # 打断：tool.call 后只有 tool.result(cancelled)，无 run 终结 → 应被探测为未终结（供恢复）
    proj = fold([
        ev(kind=EVENT_RUN_REQ, operation_id="op1", fingerprint="h", payload={"run_id": "r1"}),
        ev(kind=EVENT_TOOL_CALL, operation_id="op1", payload={"run_id": "r1"}),
        ev(kind=EVENT_TOOL_RESULT, operation_id="op1", payload={"run_id": "r1", "rc": None, "cancelled": True}),
    ])
    assert unclosed_operations(proj) == ["op1"]  # 没有 run 终结 → 未闭合，恢复时需处理


def test_failed_terminal_records_failure():
    proj = fold([
        ev(kind=EVENT_RUN_REQ, operation_id="op1", fingerprint="h", payload={"run_id": "r1", "side_effect": "read"}),
        ev(kind=EVENT_RUN_FAILED, operation_id="op1", payload={"run_id": "r1", "reason": "convergence"}),
    ])
    assert ("r1", "failed") in proj.summary()[5]
