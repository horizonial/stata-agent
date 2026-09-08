"""safety：预算刹车 + 健康探针（DD-02 §6/§8 + DD-01 §3.3）。

- Budget：步骤/轮次预算；耗尽落 budget.limit 事件，编排器必须停（不硬跑）。
- health_check：恢复/继续前的最小一致性检查（closure/未决），异常不许自动推进。
"""

from __future__ import annotations

from ..domain.reducers import Projection, unclosed_operations
from ..events.schema import (
    EVENT_BUDGET,
    EVENT_HEALTH,
    ACTOR_ORCH,
    Event,
)
from ..storage.sqlite_store import SQLiteStore


class Budget:
    def __init__(self, max_steps: int):
        self.max_steps = max_steps
        self.used = 0
        self._signaled = False

    def tick(self) -> bool:
        """推进一步；返回是否还能继续。"""
        self.used += 1
        return self.used < self.max_steps

    @property
    def exhausted(self) -> bool:
        return self.used >= self.max_steps

    def signal_exhausted(self, store: SQLiteStore, idea: str) -> None:
        """预算耗尽 → 落 budget.limit（只落一次），编排器总结已得即停。"""
        if self._signaled:
            return
        self._signaled = True
        store.append(Event(idea_id=idea, event_type=EVENT_BUDGET, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                           payload={"used": self.used, "max": self.max_steps}))


def estimate_tokens(text: str) -> int:
    """粗略 token 计量（决策用，非精确分词）：CJK 按 1、其余按 0.25 字估。"""
    if not text:
        return 0
    rough = 0.0
    for ch in text:
        rough += 1.0 if ord(ch) > 0x2E80 else 0.25
    return max(1, int(round(rough)))


def health_check(proj: Projection) -> list[str]:
    """最小一致性检查：未闭合执行链 / 残留 uncertain 都算不健康。"""
    issues: list[str] = []
    for op in unclosed_operations(proj):
        issues.append(f"op {op!r} 未闭合（无 terminal）")
    for rec in proj.runs.values():
        if rec.status == "uncertain":
            issues.append(f"run {rec.run_id!r} 处于 uncertain，需先 reconcile")
    return issues


def signal_health(store: SQLiteStore, idea: str, issues: list[str], *, ok: bool) -> None:
    store.append(Event(idea_id=idea, event_type=EVENT_HEALTH, actor=ACTOR_ORCH, source=ACTOR_ORCH,
                       payload={"ok": ok, "issues": issues}))
