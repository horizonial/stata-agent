"""reducer：把事件流 fold 成 Projection（DD-01 §3.3/§5）。

纯函数、只读事件；非法事件序列在这里抛错（reducer 是守门员）。
切片 0 覆盖：idea/phase/spec/card/claim/run 执行链。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..events.schema import (
    EVENT_CARD_SIGNED,
    EVENT_CLAIM_RETRACT,
    EVENT_CLAIM_SIGNED,
    EVENT_FILES_DEL,
    EVENT_IDEA,
    EVENT_MAIN_RESULT,
    EVENT_PHASE,
    EVENT_RUN_FAILED,
    EVENT_RUN_REQ,
    EVENT_RUN_SUCCEEDED,
    EVENT_RUN_UNCERTAIN,
    EVENT_SPEC_FREEZE,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    Event,
)
from ..events.append import (
    is_execution_intermediate,
    is_execution_start,
    is_execution_terminal,
)
from .models import Claim, EvidenceCard, ResearchState, RunRecord


class IllegalEventSequence(Exception):
    """非法事件序列（reducer 拒绝，不做静默修复）。"""


@dataclass
class Projection:
    """fold 结果。pending = 内部执行链状态（不进 summary/不做事实）。"""

    idea_id: str
    phase: Optional[str] = None
    research_state: Optional[ResearchState] = None
    claims: dict[str, Claim] = field(default_factory=dict)
    cards: dict[str, EvidenceCard] = field(default_factory=dict)
    runs: dict[str, RunRecord] = field(default_factory=dict)
    families: dict[str, list[str]] = field(default_factory=dict)
    # 执行链内部态：op_id -> 阶段；只用于非法序列判定，不是事实
    pending: dict[str, str] = field(default_factory=dict)

    def summary(self) -> tuple:
        """稳定摘要：用于确定性重放/审计比较（忽略 seq/created_at/pending）。"""
        claims = tuple(sorted((c.claim_id, c.status) for c in self.claims.values()))
        cards = tuple(sorted(self.cards))
        runs = tuple(sorted((r.run_id, r.status) for r in self.runs.values()))
        refs = tuple(sorted(self.research_state.evidence_refs)) if self.research_state else ()
        return (
            self.idea_id,
            self.phase,
            self.research_state.current_spec_id if self.research_state else None,
            claims,
            cards,
            runs,
            refs,
        )


def empty(idea_id: str) -> Projection:
    return Projection(
        idea_id=idea_id,
        research_state=ResearchState(idea_id=idea_id),
    )


def _require_op(proj: Projection, op: str, kind: str, *, not_done: bool = False) -> None:
    if op not in proj.pending:
        raise IllegalEventSequence(f"{kind}: 无对应 start(op={op!r})")
    if not_done and proj.pending[op] == "done":
        raise IllegalEventSequence(f"{kind}: op={op!r} 已终结，重复 terminal")


def apply(proj: Projection, ev: Event) -> Projection:
    """单步 reducer（纯函数）：返回 new Projection。未知事件类型忽略（向前兼容）。"""
    kind = ev.event_type
    p = ev.payload or {}
    rs = proj.research_state
    if rs is None:
        rs = ResearchState(idea_id=proj.idea_id)

    if kind == EVENT_IDEA:
        return Projection(idea_id=proj.idea_id, phase=rs.phase, research_state=rs, claims=proj.claims,
                          cards=proj.cards, runs=proj.runs, families=proj.families, pending=proj.pending)

    if kind == EVENT_PHASE:
        rs = rs.model_copy(update={"phase": p.get("to", p.get("phase"))})
        proj.phase = rs.phase

    elif kind == EVENT_SPEC_FREEZE:
        rs = rs.model_copy(update={"current_spec_id": p.get("spec_id") or rs.current_spec_id})

    elif kind == EVENT_MAIN_RESULT:
        fam = p.get("family_id") or rs.current_family_id
        if fam:
            members = list(proj.families.get(fam, []))
            if p.get("run_id") and p["run_id"] not in members:
                members.append(p["run_id"])
            proj.families[fam] = members

    elif kind in (EVENT_CARD_SIGNED,):
        card = EvidenceCard.model_validate(p["card"]) if "card" in p else EvidenceCard.model_validate(p)
        # 不可变：不允许覆盖已存在卡
        if card.card_id in proj.cards:
            raise IllegalEventSequence(f"card {card.card_id!r} 已存在（只 append）")
        proj.cards[card.card_id] = card

    elif kind == EVENT_CLAIM_SIGNED:
        claim = Claim.model_validate(p["claim"]) if "claim" in p else Claim(
            claim_id=p.get("claim_id", ""),
            statement=p.get("statement", ""),
            cards=list(p.get("cards", [])),
            kind=p.get("kind", "effect"),
        )
        missing = [c for c in claim.cards if c not in proj.cards]
        if missing:
            raise IllegalEventSequence(f"claim 引用不存在的 card: {missing}")
        if claim.claim_id in proj.claims:
            raise IllegalEventSequence(f"claim {claim.claim_id!r} 已存在（只 append）")
        proj.claims[claim.claim_id] = claim
        if claim.claim_id not in rs.evidence_refs:
            rs.evidence_refs = [*rs.evidence_refs, claim.claim_id]

    elif kind == EVENT_CLAIM_RETRACT:
        cid = p.get("claim_id", "")
        if cid not in proj.claims:
            raise IllegalEventSequence(f"retract 不存在的 claim {cid!r}")
        old = proj.claims[cid]
        proj.claims[cid] = old.model_copy(update={"status": "retracted", "superseded_by": p.get("superseded_by")})

    elif is_execution_start(kind):
        op = ev.operation_id or (p.get("operation_id"))
        run_id = p.get("run_id") or p.get("result_id") or op
        if kind == EVENT_RUN_REQ:
            if op is None:
                raise IllegalEventSequence("run.requested 缺 operation_id")
            if op in proj.pending and proj.pending[op] != "done":
                raise IllegalEventSequence(f"run.requested: op={op!r} 已开始未终结（双 start）")
            proj.pending[op] = "started"
            proj.runs[run_id] = RunRecord(
                run_id=run_id, operation_id=op, attempt_id=ev.attempt_id,
                semantic_input_hash=ev.fingerprint or p.get("semantic_input_hash"),
                side_effect=p.get("side_effect", "read"), status="running",
            )
        else:  # tool.call
            if op is None:
                raise IllegalEventSequence("tool.call 缺 operation_id")
            _require_op(proj, op, "tool.call")
            proj.pending[op] = "running"

    elif is_execution_intermediate(kind) or is_execution_terminal(kind):
        op = ev.operation_id or p.get("operation_id")
        if op is None:
            raise IllegalEventSequence(f"{kind} 缺 operation_id")
        _require_op(proj, op, kind, not_done=True)
        if kind != EVENT_TOOL_RESULT:
            proj.pending[op] = "done"
            # 更新 run 记录
            run_id = p.get("run_id") or p.get("result_id") or op
            if run_id in proj.runs:
                old = proj.runs[run_id]
                status = {"run.succeeded": "succeeded", "run.failed": "failed",
                          "run.uncertain": "uncertain"}[kind]
                proj.runs[run_id] = old.model_copy(update={
                    "status": status,
                    "provenance": p.get("provenance", {}),
                    "machine": p.get("machine", {}),
                })
            else:
                # 纯 tool.result 未登记 run —— 至少登记一个 closure 记录
                pass

    elif kind == EVENT_FILES_DEL:
        # 仅登记意图（显式清理），不真正删文件 —— 见 DD-01 rollback 边界
        pass

    # 组装返回（浅拷贝保持不可变风格）
    return Projection(
        idea_id=proj.idea_id,
        phase=proj.phase,
        research_state=rs,
        claims=proj.claims,
        cards=proj.cards,
        runs=proj.runs,
        families=proj.families,
        pending=proj.pending,
    )


def fold(events, idea_id: str | None = None) -> Projection:
    """把事件序列 fold 成投影；任一事件非法则抛 IllegalEventSequence。"""
    proj = empty(idea_id or (next(iter(events)).idea_id if events else "?"))
    for ev in events:
        proj = apply(proj, ev)
    return proj


def unclosed_operations(proj: Projection) -> list[str]:
    """健康探针/恢复用：找出没有 terminal 的 execution（closure 检查，DD-01 §3.3-2）。"""
    return [op for op, st in proj.pending.items() if st != "done"]
