"""压缩 = 语义 checkpoint（DD-03 §6，DD-01 #21 compaction.boundary）。

把"研究状态 + 已确认结论 + 证据索引"压成边界摘要，写进事件链；
原始事件一条不删（只影响下一轮"看什么"）。压缩后可据此重建上下文。
"""

from __future__ import annotations

from ..events.schema import (
    EVENT_COMPACTION,
    ACTOR_ORCH,
    Event,
)
from ..storage.sqlite_store import SQLiteStore


def _summary_text(proj) -> str:
    rs = proj.research_state
    lines = [
        f"阶段: {proj.phase or '(未进入阶段)'}",
        f"主 spec: {rs.current_spec_id if rs else None}",
        f"已确认结论(claims): {len(proj.claims)}",
        f"证据卡(cards): {len(proj.cards)}",
        f"运行数: {len(proj.runs)}",
    ]
    supported = [c.claim_id for c in proj.claims.values() if c.status == "supported"]
    if supported:
        lines.append("已确认 claim: " + ", ".join(sorted(supported)[:8]))
    return "\n".join(lines)


def compact(store: SQLiteStore, idea: str, *, reason: str = "phase_tail",
            phase_scope: str | None = None) -> dict:
    """写一条 compaction.boundary：range=[当前最早 seq..最新 seq]，携带语义摘要。"""
    events = list(store.scan(idea))
    if not events:
        raise ValueError("无可压缩事件")
    proj = store.project(idea)
    seq_to = events[-1].seq
    seq_from = events[0].seq
    claim_ids = sorted(proj.claims)
    summary = _summary_text(proj)
    ev = Event(idea_id=idea, event_type=EVENT_COMPACTION, actor=ACTOR_ORCH, source=ACTOR_ORCH,
               phase=proj.phase,
               payload={"from_seq": seq_from, "to_seq": seq_to, "summary": summary,
                        "claim_ids": claim_ids, "phase_scope": phase_scope, "reason": reason})
    seq = store.append(ev)
    return {"seq": seq, "from_seq": seq_from, "to_seq": seq_to, "summary": summary,
            "claim_ids": claim_ids}


def build_context_after(store: SQLiteStore, idea: str) -> str:
    """压缩后的重建上下文：取最近 boundary 摘要为 L2/L3 种子；无 boundary 则用普通投影。"""
    from .context import build_context

    boundary = None
    for e in store.scan(idea, event_types={EVENT_COMPACTION}):
        boundary = e
    proj = store.project(idea)
    if boundary is None:
        return build_context(proj)
    payload = boundary.payload or {}
    head = f"[compacted {payload.get('from_seq')}-{payload.get('to_seq')}]\n{payload.get('summary', '')}"
    tail = [f"#seq{e.seq} {e.event_type}" for e in list(store.scan(idea))[-6:]]
    return head + "\n[tail]\n" + "\n".join(tail)
