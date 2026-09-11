"""FakeExecutor：离线确定性版 StataExecutor（测 loop 接线/自动签卡用，不碰 Stata）。

与真 executor 同接口 execute(script, idea=..., run_id=..., spec_id=..., side_effect=...)。
机器值取 auto 回归的稳定已知结果（price vs mpg：coef -238.9, N=74, r2 .2196）。
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from ..events.schema import (
    EVENT_RUN_REQ,
    EVENT_RUN_SUCCEEDED,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    ACTOR_ORCH,
    Event,
)
from ..storage.sqlite_store import SQLiteStore
from .result_verifier import (
    ResultContract,
    canonical_contract_hash,
    canonical_machine_hash,
    semantic_input_hash,
    validate_contract,
)
from .executor import reuse_for

_MACHINE = {"coef": -238.9, "N": 74, "r2": 0.2196}


def default_test_contract() -> ResultContract:
    """Small deterministic contract used by explicit offline test fixtures."""

    return ResultContract(
        target_term="mpg",
        estimator="regress",
        dependent_variable="price",
        vce="ols",
        required_stats=["coef", "N", "r2"],
    )


class FakeExecutor:
    def __init__(self, store: SQLiteStore, *, run_root: Path | None = None):
        self._store = store
        self._run_root = Path(run_root) if run_root else Path(store._path).parent / "fake-runs"
        self._run_root.mkdir(parents=True, exist_ok=True)

    def execute(self, script: str, *, idea: str = "i1", run_id: str | None = None,
                correlation_id: str | None = None,
                spec_id: str | None = None, side_effect: str = "write",
                result_contract: ResultContract | dict | None = None) -> dict:
        contract = validate_contract(result_contract)
        run_id = run_id or f"run-fake-{uuid.uuid4().hex[:8]}"
        op = f"op-{run_id}"
        previous = self._store.project(idea).runs.values()
        attempt = max((rec.attempt_id for rec in previous), default=0) + 1
        call_id = f"call-{run_id}"
        do_file = self._run_root / f"{run_id}.do"
        do_file.write_text(script, encoding="utf-8", newline="")
        command_hash = hashlib.sha256(do_file.read_bytes()).hexdigest()
        semantic_hash = (
            semantic_input_hash(script, contract) if contract is not None
            else hashlib.sha256(script.encode("utf-8")).hexdigest()
        )
        reused = reuse_for(
            self._store,
            idea,
            semantic_hash,
            contract_hash=canonical_contract_hash(contract) if contract is not None else None,
        )
        if reused is not None:
            return reused
        machine: dict[str, object] = dict(_MACHINE)
        if contract is not None:
            machine["model"] = {
                "target_term": contract.target_term,
                "estimator": contract.estimator,
                "dependent_variable": contract.dependent_variable,
                "vce": contract.vce,
                "cluster_variables": list(contract.cluster_variables),
                "fixed_effects": list(contract.fixed_effects),
            }
            if "se" in contract.required_stats:
                machine["se"] = 40.0
        prov = {
            "kind": "test",
            "executor": "fake",
            "test_only": True,
            "do_file": str(do_file),
            "command_hash": command_hash,
            "semantic_input_hash": semantic_hash,
            "contract_hash": canonical_contract_hash(contract) if contract is not None else None,
            "data_signature": "fake",
            "env_sig": {"stata_version": "fake", "stata_flavor": "fake"},
            "machine_hash": canonical_machine_hash(machine),
        }
        # Keep the same correlated execution chain as the real executor.  The
        # call/result are synthetic and explicitly marked test-only in the
        # terminal provenance, so a fake result cannot masquerade as Stata.
        self._store.append_many([
            Event(
                idea_id=idea, event_type=EVENT_RUN_REQ, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                correlation_id=correlation_id, operation_id=op, fingerprint=semantic_hash, attempt_id=attempt,
                side_effect_state="running",
                payload={"run_id": run_id, "spec_id": spec_id, "side_effect": side_effect,
                         "semantic_input_hash": semantic_hash,
                         "result_contract": contract.model_dump() if contract is not None else None},
            ),
            Event(
                idea_id=idea, event_type=EVENT_TOOL_CALL, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                correlation_id=correlation_id, operation_id=op, fingerprint=f"fake-call-{run_id}",
                payload={"run_id": run_id, "call_id": call_id, "executor": "fake", "test_only": True},
            ),
            Event(
                idea_id=idea, event_type=EVENT_TOOL_RESULT, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                correlation_id=correlation_id, operation_id=op,
                payload={"run_id": run_id, "call_id": call_id, "rc": 0, "is_error": False,
                         "executor": "fake", "test_only": True},
            ),
            Event(
                idea_id=idea, event_type=EVENT_RUN_SUCCEEDED, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                correlation_id=correlation_id, operation_id=op, side_effect_state="committed",
                payload={"run_id": run_id, "spec_id": spec_id, "provenance": prov,
                         "machine": machine},
            ),
        ])
        return {"run_id": run_id, "machine": machine, "env": prov["env_sig"],
                "do_file": prov["do_file"], "command_hash": prov["command_hash"],
                "semantic_input_hash": semantic_hash,
                "result_contract": contract.model_dump() if contract is not None else None,
                "reused": False}
