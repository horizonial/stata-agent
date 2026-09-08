"""EvidenceCard/Claim 签发（写入权分离的真实验证）。

DD-01 §2.6/§2.7 + SPEC N11：模型只能提议；EvidenceCard 只能由 validator 从
"Stata 机器层 + provenance"确定性签发；Claim 只能由 evidence_builder 从卡合成。
本模块扮演 validator/evidence_builder 的确定性角色（source=validator/evidence_builder）。
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from ..domain.models import Claim, EvidenceCard
from ..events.schema import (
    EVENT_CARD_SIGNED,
    EVENT_CLAIM_SIGNED,
    ACTOR_EVIDENCE,
    ACTOR_VALIDATOR,
    Event,
)
from ..storage.sqlite_store import SQLiteStore


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _trusted_provenance(provenance: dict) -> str:
    """Return ``real``/``test`` for provenance accepted by the validator.

    A succeeded event alone is not an attestation.  Real Stata evidence must
    identify the executor, carry the executor's attestation, command hash,
    reproducible do-file, data signature field, and environment signature.
    FakeExecutor is accepted only as an explicitly test-only backend so the
    offline test suite can exercise the full chain; it is never labelled real.
    """
    if not isinstance(provenance, dict):
        raise ValueError("run provenance 缺失或格式错误，拒绝签证据")
    kind = provenance.get("kind")
    executor = provenance.get("executor")
    env_sig = provenance.get("env_sig")
    if not isinstance(provenance.get("command_hash"), str) or not provenance["command_hash"]:
        raise ValueError("run provenance 缺 command_hash，拒绝签证据")
    if not isinstance(provenance.get("do_file"), str) or not provenance["do_file"]:
        raise ValueError("run provenance 缺 do_file，拒绝签证据")
    if "data_signature" not in provenance:
        raise ValueError("run provenance 缺 data_signature，拒绝签证据")
    if not isinstance(env_sig, dict) or not env_sig:
        raise ValueError("run provenance 缺 env_sig，拒绝签证据")
    if kind == "real":
        if executor != "stata-mcp" or provenance.get("attested") is not True:
            raise ValueError("run provenance 未通过 Stata executor attestation，拒绝签证据")
        if not {"stata_version", "stata_flavor"}.issubset(env_sig):
            raise ValueError("run provenance env_sig 不完整，拒绝签证据")
        if not Path(provenance["do_file"]).is_file():
            raise ValueError("run provenance do_file 不存在，拒绝签证据")
        return "real"
    if kind == "test":
        if executor != "fake" or provenance.get("test_only") is not True:
            raise ValueError("test provenance 必须明确 executor=fake/test_only=true")
        return "test"
    raise ValueError("run provenance kind 未知或不可信，拒绝签证据")


def validate_run_provenance(provenance: dict) -> str:
    """Public validator shared by numeric/figure evidence writers."""
    return _trusted_provenance(provenance)


def sign_run_numeric_cards(
    store: SQLiteStore,
    run_id: str,
    *,
    idea: str = "i1",
    claim_statement: str | None = None,
) -> list[str]:
    """对一次 succeeded run 的机器层逐值签 numeric EvidenceCard；可选再合成一条 Claim。

    返回签出的 card_id 列表。失败（run 不存在/未成功/无机器层）抛 ValueError。
    """
    proj = store.project(idea)
    rec = proj.runs.get(run_id)
    if rec is None:
        raise ValueError(f"run 不存在: {run_id}")
    if rec.status != "succeeded":
        raise ValueError(f"run 未成功(status={rec.status})：不许对失败结果签证据")
    provenance = rec.provenance or {}
    provenance_kind = _trusted_provenance(provenance)
    if rec.semantic_input_hash and provenance.get("command_hash") != rec.semantic_input_hash:
        raise ValueError("run provenance command_hash 与 requested input 不一致，拒绝签证据")
    machine = rec.machine or {}
    numeric = {
        k: v for k, v in machine.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))
    }
    if not numeric:
        raise ValueError("run 无机器层数值，无法签 numeric 卡")

    cards: list[str] = []
    all_cards: list[str] = []
    machine_hash = _sha(machine)
    existing = set(proj.cards)
    for stat_type, value in numeric.items():
        card_id = f"card-{run_id}-{stat_type}"
        if card_id in existing:
            all_cards.append(card_id)
            continue  # 幂等：已签过的卡不重复签
        card = EvidenceCard(
            card_id=card_id,
            kind="numeric",
            locator={"run_id": run_id, "stat_type": stat_type,
                     "provenance_kind": provenance_kind},
            value={"value": value},
            machine_hash=machine_hash,
            signed_by=ACTOR_VALIDATOR,
        )
        store.append(Event(
            idea_id=idea, event_type=EVENT_CARD_SIGNED, actor=ACTOR_VALIDATOR,
            source=ACTOR_VALIDATOR,
            payload={"card": card.model_dump()},
        ))
        cards.append(card.card_id)
        all_cards.append(card.card_id)
        existing.add(card.card_id)

    if claim_statement and all_cards and f"claim-{run_id}" not in proj.claims:
        claim = Claim(claim_id=f"claim-{run_id}", statement=claim_statement, cards=all_cards,
                      written_by=ACTOR_EVIDENCE)
        store.append(Event(
            idea_id=idea, event_type=EVENT_CLAIM_SIGNED, actor=ACTOR_EVIDENCE,
            source=ACTOR_EVIDENCE,
            payload={"claim": claim.model_dump()},
        ))
    return cards
