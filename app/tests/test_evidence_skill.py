"""切片 3 补：机器层 → EvidenceCard/Claim 签发（写入权真实验证）+ skill 加载/preflight。"""

from __future__ import annotations

from pathlib import Path

import pytest

from stata_agent.events.schema import (
    EVENT_RUN_REQ,
    EVENT_RUN_SUCCEEDED,
    ACTOR_ORCH,
    Event,
)
from stata_agent.skills.loader import load_skill_dir
from stata_agent.storage.sqlite_store import SQLiteStore
from stata_agent.tools.evidence_signer import sign_run_numeric_cards

APP = Path(__file__).resolve().parents[1]


def _run_succeeded(idea="i1", run="r1", machine=None):
    events = [
        Event(idea_id=idea, event_type=EVENT_RUN_REQ, actor=ACTOR_ORCH, source=ACTOR_ORCH,
              operation_id="op1", fingerprint="h", payload={"run_id": run}),
        Event(idea_id=idea, event_type=EVENT_RUN_SUCCEEDED, actor=ACTOR_ORCH, source=ACTOR_ORCH,
              operation_id="op1", payload={
                  "run_id": run,
                  "machine": machine or {"coef": -238.9, "N": 74, "r2": 0.2196},
                  "provenance": {"command_hash": "c", "data_signature": None, "env_sig": {"v": "18"}},
              }),
    ]
    return events


def test_signer_creates_cards_and_claim(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"), writer_id="a")
    for ev in _run_succeeded():
        store.append(ev)
    cards = sign_run_numeric_cards(store, "r1", claim_statement="价格随油耗显著下降")
    assert len(cards) == 3
    proj = store.project("i1")
    assert set(proj.cards) == set(cards)
    assert "claim-r1" in proj.claims
    assert ("claim-r1", "supported") in proj.summary()[3]
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
