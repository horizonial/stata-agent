"""阶段与运行态最小定义（DD-02 §1/§2 + SPEC v0.5 审计 D1）。

切片 0 只建枚举、迁移允许表、gate_mode；entry/exit 断言留 hook（函数谓词）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class Phase(str, Enum):
    IDEA = "IDEA"
    LITERATURE = "LITERATURE"
    DESIGN = "DESIGN"
    DATA = "DATA"
    ESTIMATION = "ESTIMATION"
    ROBUSTNESS = "ROBUSTNESS"
    WRITING = "WRITING"
    VALIDATION = "VALIDATION"
    DONE = "DONE"


PHASE_ORDER: list[str] = [p.value for p in Phase]


class RunStatus(str, Enum):
    """与 phase 正交的短期运行态（DD-02 §1）。"""

    IDLE = "idle"
    BUSY = "busy"
    AWAITING_USER = "awaiting_user"  # blocked = 等外部输入/审批，是正常态
    VALIDATING = "validating"
    FAILED = "failed"


class GateMode(str, Enum):
    """审计 D1：探索环轻门控 vs 正式门控。"""

    EXPLORE = "explore"
    FORMAL = "formal"


# 合法前进边（DD-02 §3）
_FORWARD = {
    Phase.IDEA: {Phase.LITERATURE},
    Phase.LITERATURE: {Phase.DESIGN},
    Phase.DESIGN: {Phase.DATA},
    Phase.DATA: {Phase.ESTIMATION},
    Phase.ESTIMATION: {Phase.ROBUSTNESS},
    Phase.ROBUSTNESS: {Phase.WRITING},
    Phase.WRITING: {Phase.VALIDATION},
    Phase.VALIDATION: {Phase.DONE},
}
# 受控回退边
_REDO = {
    Phase.VALIDATION: {Phase.WRITING, Phase.ESTIMATION},
    Phase.ROBUSTNESS: {Phase.DESIGN, Phase.ESTIMATION},
    Phase.ESTIMATION: {Phase.DATA},
    Phase.DATA: {Phase.DATA},
    Phase.DESIGN: {Phase.DESIGN},
}

Predicate = Callable[["PhaseDef"], bool]


@dataclass
class PhaseDef:
    """一份阶段的配置。gate_mode/entry/exit 都数据化，不写死。"""

    phase: Phase
    purpose: str = ""
    allowed_ops: list[str] = field(default_factory=list)
    gate_mode: GateMode = GateMode.EXPLORE
    max_attempts: int = 5
    entry_conditions: list[Predicate] = field(default_factory=list)
    exit_gate: str | None = None  # L-C / L-R / L-D 或 None

    def entry_ok(self) -> bool:
        return all(p(self) for p in self.entry_conditions)


def can_transition(src: Phase, dst: Phase) -> bool:
    """迁移只由编排器提交；这里给合法边。"""
    if dst in _FORWARD.get(src, set()):
        return True
    if dst in _REDO.get(src, set()):
        return True
    return False


# 默认九阶段（purpose 简写，实现时再填细节）
PHASES: dict[Phase, PhaseDef] = {
    ph: PhaseDef(phase=ph) for ph in Phase
}
