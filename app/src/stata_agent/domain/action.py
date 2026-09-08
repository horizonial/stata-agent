"""ActionProposal 契约（DD-02 §5）：模型只产出 proposal，harness 裁决/执行。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# 模型可提议的动作类型（DD-02 §5 子集；sign_claim/mark_done 等不在内）
ACT_TYPES = frozenset({
    "propose_spec",
    "request_run",
    "interpret",
    "select_candidate",
    "ask_user",
    "read_artifact",
    "rag_search",
    "inspect_data",
    "request_robustness_variant",
    "draft_section",
})


class Act(BaseModel):
    act_type: str
    target: dict = Field(default_factory=dict)
    reason: Optional[str] = None


class ActionProposal(BaseModel):
    """模型一轮输出。harness 逐个裁决（policy）并执行；编排器才可提交状态迁移。"""

    decision_summary: str = ""
    acts: list[Act] = Field(default_factory=list)
    ask_user: Optional[str] = None
    stop_reason: Optional[Literal["gate", "budget", "need_input", "done"]] = None

    def model_dump_acts(self) -> list[dict]:
        return [a.model_dump() for a in self.acts]
