"""build_context 最小版（DD-03 §2/§3 L1+L2）：只放研究状态摘要，非历史。"""

from __future__ import annotations

from ..domain.reducers import Projection
from ..phase.phasedef import Phase


def build_context(proj: Projection) -> str:
    """把当前投影折叠成给模型的一句话上下文（确定性）。切片 1 最小实现。"""
    rs = proj.research_state
    lines: list[str] = []
    lines.append(f"[L2] idea={proj.idea_id}")
    lines.append(f"[L2] phase={proj.phase or '(未建档阶段)'}")
    lines.append(f"[L2] current_spec={rs.current_spec_id if rs else None}")
    lines.append(f"[L3] 已确认 claims={len(proj.claims)}; evidence_refs={len(rs.evidence_refs) if rs else 0}")
    recent = sorted(proj.runs.items())[-3:]
    if recent:
        lines.append("[L4] 最近 runs: " + ", ".join(f"{rid}={rec.status}" for rid, rec in recent))
    else:
        lines.append("[L4] (暂无 run)")
    lines.append(f"[L1] 可提议动作见 policy；禁止 mark_done/delete_file/sign_claim/phase.transition")
    return "\n".join(lines)


def phase_of(proj: Projection) -> Phase | None:
    """把投影里的阶段字符串映射成枚举（未知返回 None → policy 按最严处理）。"""
    if proj.phase is None:
        return None
    try:
        return Phase(proj.phase)
    except ValueError:
        return None
