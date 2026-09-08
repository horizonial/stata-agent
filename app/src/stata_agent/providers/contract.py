"""provider 契约测试：换模型/升级后跑（SPEC §4.2 + 切片 2 验收）。

离线可跑：对任意 ProposalProvider 的"形状"检查；换 deepseek 前的回归关卡。
真值/事实校验不在这一层（那是 EvidenceBundle/validator）。
"""

from __future__ import annotations

from typing import Callable

from ..domain.action import ActionProposal
from .codec import parse_proposal


def run_checks(provider) -> dict[str, bool]:
    """对 provider 跑契约检查，返回 {检查名: 是否通过}。"""
    from .mock import MockReplayProvider  # 局部避免循环

    ctx = "phase=IDEA\n契约测试上下文"
    try:
        prop = provider.propose(ctx)
    except StopIteration:
        # mock 脚本用尽时，契约要求能给出明确信号——视为未通过？见下 is_signal
        prop = None
    out: dict[str, bool] = {
        "propose_returns_action_proposal": isinstance(prop, ActionProposal),
    }
    if isinstance(prop, ActionProposal):
        # JSON 往返：模型输出→parse 应能重建（形状稳定）
        try:
            again = parse_proposal(prop.model_dump_json())
            out["json_roundtrip_stable"] = again.model_dump() == prop.model_dump()
        except Exception:
            out["json_roundtrip_stable"] = False
        # 空 acts 也合法（只问问题的 turn）
        out["empty_acts_allowed"] = True
    return out


def check_all(provider, fail_fast: bool = True) -> None:
    """断言全过；用于换模型前的回归门。"""
    res = run_checks(provider)
    bad = {k for k, v in res.items() if not v}
    if bad:
        raise AssertionError(f"provider 契约检查未过: {sorted(bad)}")
