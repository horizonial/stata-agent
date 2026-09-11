"""切片 3 补：机器层 → EvidenceCard/Claim 签发（写入权真实验证）+ skill 加载/preflight。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from stata_agent.events.schema import (
    EVENT_RUN_REQ,
    EVENT_RUN_SUCCEEDED,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    ACTOR_ORCH,
    Event,
)
from stata_agent.skills.loader import load_skill_dir
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.tools.evidence_signer import sign_run_numeric_cards
from stata_agent.tools.fake_executor import default_test_contract
from stata_agent.tools.result_verifier import (
    canonical_contract_hash,
    canonical_machine_hash,
    semantic_input_hash,
)

APP = Path(__file__).resolve().parents[1]


def _run_succeeded(tmp_path, idea="i1", run="r1", machine=None):
    contract = default_test_contract()
    machine = machine or {
        "coef": -238.9,
        "N": 74,
        "r2": 0.2196,
        "model": {
            "target_term": "mpg", "estimator": "regress", "dependent_variable": "price",
            "vce": "ols", "cluster_variables": [], "fixed_effects": [],
        },
    }
    do_file = tmp_path / f"{run}.do"
    do_file.write_text("reg price mpg\n", encoding="utf-8")
    command_hash = hashlib.sha256(do_file.read_bytes()).hexdigest()
    semantic_hash = semantic_input_hash("reg price mpg", contract)
    events = [
        Event(idea_id=idea, event_type=EVENT_RUN_REQ, actor=ACTOR_ORCH, source=ACTOR_ORCH,
              operation_id="op1", fingerprint=semantic_hash,
              payload={"run_id": run, "semantic_input_hash": semantic_hash,
                       "result_contract": contract.model_dump()}),
        Event(idea_id=idea, event_type=EVENT_TOOL_CALL, actor=ACTOR_ORCH, source=ACTOR_ORCH,
              operation_id="op1", payload={"run_id": run, "call_id": "call-1"}),
        Event(idea_id=idea, event_type=EVENT_TOOL_RESULT, actor=ACTOR_ORCH, source=ACTOR_ORCH,
              operation_id="op1", payload={"run_id": run, "call_id": "call-1", "rc": 0}),
        Event(idea_id=idea, event_type=EVENT_RUN_SUCCEEDED, actor=ACTOR_ORCH, source=ACTOR_ORCH,
              operation_id="op1", payload={
                  "run_id": run,
                  "machine": machine,
                  "provenance": {"kind": "test", "executor": "fake", "test_only": True,
                                  "do_file": str(do_file), "command_hash": command_hash,
                                  "semantic_input_hash": semantic_hash,
                                  "contract_hash": canonical_contract_hash(contract),
                                  "machine_hash": canonical_machine_hash(machine),
                                  "data_signature": "fake",
                                  "env_sig": {"stata_version": "fake", "stata_flavor": "fake"}},
              }),
    ]
    return events


def test_signer_creates_cards_and_claim(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    for ev in _run_succeeded(tmp_path):
        store.append(ev)
    cards = sign_run_numeric_cards(store, "r1", claim_statement="价格随油耗显著下降")
    assert len(cards) == 3
    proj = store.project("i1")
    assert set(proj.cards) == set(cards)
    first = proj.cards[cards[0]]
    assert first.locator["target_term"] == "mpg"
    assert first.locator["contract_hash"]
    assert first.locator["verification_schema_version"] == 1
    assert "claim-r1" in proj.claims
    assert ("claim-r1", "supported") in proj.summary()[3]
    store.close()


def test_signer_rejects_tampered_do_file_without_cards(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    for ev in _run_succeeded(tmp_path):
        store.append(ev)
    do_file = tmp_path / "r1.do"
    do_file.write_text("reg price mpg\n* tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="command_hash_mismatch"):
        sign_run_numeric_cards(store, "r1", claim_statement="不能签")
    assert store.project("i1").cards == {}
    assert store.project("i1").claims == {}
    store.close()


def test_signer_rejects_unknown_run(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    with pytest.raises(ValueError):
        sign_run_numeric_cards(store, "nope")  # run 不存在 → 不许签
    store.close()


def test_skill_loader_and_preflight():
    skills = load_skill_dir(APP / "skills")
    assert "causal-inference-mixtape" in skills
    sd = skills["causal-inference-mixtape"]
    assert sd.description and sd.body


def test_model_cannot_sign_claim(tmp_path):
    # 写入权：agent 来源想直接产出 claim → 被拒（append 层）
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    from stata_agent.events.append import WriterNotPermitted
    with pytest.raises(WriterNotPermitted):
        store.append(Event(idea_id="i1", event_type="claim.signed", actor="agent", source="agent",
                           payload={"claim": {"claim_id": "c", "statement": "x", "cards": []}}))
    store.close()
