"""research_turn：单轮（DD-02 §6 切片 1 版）。

每轮：投影 → build_context → 模型 propose → 全落 events（agent_step + 每 act 裁决）→ 回一句。
不执行真实工具（工具 stub 到切片 3）；不提交状态迁移（编排器才可，切片 0 阶段迁移表已备）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..domain.action import ActionProposal
from ..events.schema import (
    EVENT_AGENT_STEP,
    ACTOR_AGENT,
    EVENT_IDEA,
    Event,
)
from ..phase.phasedef import GateMode
from ..policy import policy
from ..storage.sqlite_store import SQLiteStore
from ..providers.protocol import ProposalProvider
from .context import build_context, phase_of


@dataclass
class Decision:
    act: str
    verdict: str  # deny|ask|allow
    reason: str

    def line(self) -> str:
        return f"{self.act} → {self.verdict}（{self.reason}）"


@dataclass
class TurnResult:
    reply: str
    decisions: list[Decision] = field(default_factory=list)
    proposal: Optional[ActionProposal] = None
    asked: bool = False

    def __str__(self) -> str:
        return self.reply


def _compact_aware_context(store, proj, *, token_cap: int | None, reason: str = "ctx_too_long") -> str:
    """压缩感知上下文：有 boundary 就用摘要重建；超 token_cap 就再压一次再重建。"""
    from .compaction import build_context_after, compact
    from .safety import estimate_tokens

    ctx = build_context_after(store, proj.idea_id)
    if token_cap is None or estimate_tokens(ctx) <= token_cap:
        return ctx
    try:
        compact(store, proj.idea_id, reason=reason)
    except ValueError:
        pass  # 无可压缩事件（比如空账本）
    return build_context_after(store, proj.idea_id)


def research_turn(
    store: SQLiteStore,
    provider: ProposalProvider,
    *,
    idea: str = "i1",
    gate_mode: GateMode = GateMode.EXPLORE,
    privacy_mode: str = "local_strict",
    extra_context: list[str] | None = None,
    token_cap: int | None = None,
) -> TurnResult:
    """跑一轮：模型提议 → policy 逐条裁决 → 结果全落 events → 返回给用户的回话。

    extra_context：阶段相关证据/检索块（如 RAG 命中），插在系统上下文之后给模型看。
    token_cap：上下文超预算先在 propose 前做语义压缩（token=决策输入）。
    """
    proj = store.project(idea)
    phase = phase_of(proj)
    ctx = _compact_aware_context(store, proj, token_cap=token_cap)
    if extra_context:
        ctx = ctx + "\n\n" + "\n".join(extra_context)
    prop = provider.propose(ctx)

    # 模型这一步本身是"可审计决策"：记 agent_step（不存思维链，只存摘要+动作）
    store.append(Event(
        idea_id=idea, event_type=EVENT_AGENT_STEP, actor=ACTOR_AGENT, source=ACTOR_AGENT,
        phase=proj.phase,
        payload={
            "decision_summary": prop.decision_summary,
            "acts": prop.model_dump_acts(),
            "ask": prop.ask_user,
            "stop_reason": prop.stop_reason,
        },
    ))

    decisions: list[Decision] = []
    for act in prop.acts:
        verdict = policy.check_act(
            act.act_type,
            phase=phase,
            gate_mode=gate_mode,
            privacy_mode=privacy_mode,
            is_research_change=(act.act_type == "propose_spec"),
        )
        decisions.append(Decision(act.act_type, verdict.result, verdict.reason))

    asked = bool(prop.ask_user)
    if asked:
        reply = prop.ask_user
    else:
        lines = ["；".join(d.line() for d in decisions) if decisions else "本轮无可提议动作"]
        if prop.stop_reason == "done" and not decisions:
            lines = ["当前无可自动推进动作，请指示下一步（gate/预算/需输入）"]
        reply = "。".join(lines)
    return TurnResult(reply=reply, decisions=decisions, proposal=prop, asked=asked)


def bootstrap_idea(store: SQLiteStore, idea: str, question: str) -> None:
    """若还没建档，先落一条 idea.declared（DD-02 阶段 0 建档最小版）。"""
    has_idea = any(ev.event_type == EVENT_IDEA for ev in store.scan(idea))
    if not has_idea:
        store.append(Event(
            idea_id=idea, event_type=EVENT_IDEA, actor=ACTOR_AGENT, source=ACTOR_AGENT,
            payload={"question": question},
        ))
