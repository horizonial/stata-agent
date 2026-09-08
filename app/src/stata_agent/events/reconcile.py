"""reconcile：对"不确定"写工具的处置决策（DD-01 §3.5）。

核心规则：
- read 工具超时无结果 → 可安全重试（幂等）。
- write 工具超时无结果 → 可能已提交：先查结果台账/工件（semantic_input_hash 命中
  committed 结果则复用），否则从 prepared 快照重建后重跑（不依赖内存 .dta 增量）；
  仍无法判定 → 人工。
"""

from __future__ import annotations

from dataclasses import dataclass

# 决策枚举：与 DD-01 一致（reuse/rerun/human/abort）
REUSE = "reuse"
RERUN = "rerun"
HUMAN = "human"
ABORT = "abort"


@dataclass(frozen=True)
class SideEffect:
    """工具副作用的元信息（read=安全重放 / write=可能污染内存 .dta）。"""

    kind: str  # "read" | "write"
    semantic_input_hash: str | None = None


def decide_uncertain(
    side_effect: SideEffect,
    *,
    committed_result_exists: bool,
    can_rerun: bool = True,
) -> str:
    """输入一个 side_effect_state='uncertain' 的工具/运行，返回处置决策。

    committed_result_exists：该 (operation_id / semantic_input_hash) 是否已有
    committed 结果（结果台账命中）。
    """
    if committed_result_exists:
        # 超时≠失败：结果其实已提交 → 复用
        return REUSE
    if side_effect.kind == "read":
        # 读类可安全重放
        return RERUN if can_rerun else HUMAN
    # write 类：无法确认是否已部分写盘/污染内存数据集。
    # 可重放 = 能从 prepared 快照重建（避免把失败 do 的副作用再叠一遍）
    return RERUN if can_rerun else HUMAN
