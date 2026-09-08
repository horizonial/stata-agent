"""LLM 聚合层契约（SPEC §4.2）。切片 1 只做 propose 一步。"""

from __future__ import annotations

from typing import Protocol

from ..domain.action import ActionProposal


class ProposalProvider(Protocol):
    """给出一次研究动作提议（模型侧）。实现可 mock / deepseek / 其它。"""

    def propose(self, context: str) -> ActionProposal:
        ...
