"""事件模型与常量（DD-01 §3.1/§3.2 的最小落地）。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 事件种类常量（DD-01 事件目录；切片 0 只实现子集，其余保留枚举待后续 reducer）
# ---------------------------------------------------------------------------

EVENT_IDEA = "idea.declared"
EVENT_AGENT_STEP = "agent_step"
EVENT_USER = "user.message"
EVENT_STEERING = "steering"
EVENT_APPROVAL_REQ = "approval.requested"
EVENT_APPROVAL_GRANT = "approval.granted"
EVENT_APPROVAL_REJECT = "approval.rejected"
EVENT_SPEC_PROPOSE = "spec.proposed"
EVENT_SPEC_FREEZE = "spec.frozen"
EVENT_SPEC_LOCK = "spec.locked"
EVENT_BRANCH = "branch.created"
EVENT_RUN_REQ = "run.requested"
EVENT_TOOL_CALL = "tool.call"
EVENT_TOOL_RESULT = "tool.result"
# agent loop 的"模型调工具"（区别于 Stata 执行链的 tool.call/tool.result）
EVENT_TOOL_INVOKED = "tool.invoked"
EVENT_TOOL_DONE = "tool.done"
EVENT_RUN_SUCCEEDED = "run.succeeded"
EVENT_RUN_FAILED = "run.failed"
EVENT_RUN_UNCERTAIN = "run.uncertain"
EVENT_CARD_SIGNED = "evidence.card_signed"
EVENT_CLAIM_SIGNED = "claim.signed"
EVENT_CLAIM_RETRACT = "claim.retracted"
EVENT_MAIN_RESULT = "family.main_result_selected"
EVENT_FAMILY_RUN = "family.run_registered"
EVENT_AMENDMENT = "amendment.recorded"
EVENT_PHASE = "phase.transition"
EVENT_CHECKPOINT = "checkpoint.snapshot"
EVENT_COMPACTION = "compaction.boundary"
EVENT_HEALTH = "health.probe"
EVENT_BUDGET = "budget.limit"
EVENT_PRIVACY = "privacy.mode.changed"
EVENT_FALLBACK = "provider.fallback"
EVENT_RESTORED = "system.restored"
EVENT_ARTIFACT = "artifact.stored"
EVENT_FILES_DEL = "files.delete_request"

# 执行链：run.requested → tool.call → tool.result → run.{succeeded,failed,uncertain}
CHAIN_START = frozenset({EVENT_RUN_REQ, EVENT_TOOL_CALL})
CHAIN_TERMINAL = frozenset({EVENT_RUN_SUCCEEDED, EVENT_RUN_FAILED, EVENT_RUN_UNCERTAIN})
CHAIN_INTERMEDIATE = frozenset({EVENT_TOOL_RESULT})

# 身份/来源：决定"写入权"（谁有权产出 card/claim）
ACTOR_USER = "user"
ACTOR_AGENT = "agent"
ACTOR_ORCH = "orchestrator"
ACTOR_VALIDATOR = "validator"
ACTOR_EVIDENCE = "evidence_builder"
ACTOR_STATA = "stata_mcp"
ACTOR_SYSTEM = "system"

# 允许产出不可变证据(EvidenceCard/Claim)的来源（DD-01 §2.7 写者矩阵）
SIGNING_SOURCES = frozenset({ACTOR_VALIDATOR, ACTOR_EVIDENCE})
SIGNING_KINDS = frozenset({EVENT_CARD_SIGNED, EVENT_CLAIM_SIGNED, EVENT_CLAIM_RETRACT})

# 需要 fingerprint（幂等）的事件（DD-01 §3.3）：工具执行
FINGERPRINT_KINDS = frozenset({EVENT_TOOL_CALL, EVENT_RUN_REQ})


class Event(BaseModel):
    """账本事件。id/seq/created_at 由 store 分配，外部不可填。"""

    idea_id: str
    event_type: str
    actor: str = ACTOR_ORCH
    source: str = ACTOR_ORCH
    payload: dict = Field(default_factory=dict)

    event_id: Optional[str] = None
    seq: Optional[int] = None
    branch_id: str = "main"
    prev_event_id: Optional[str] = None
    phase: Optional[str] = None
    schema_version: int = 1
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None
    operation_id: Optional[str] = None
    attempt_id: int = 0
    fingerprint: Optional[str] = None
    confidence: Literal["fact", "verified", "judgment"] = "judgment"
    side_effect_state: Optional[str] = None
    created_at: Optional[int] = None

    def kind(self) -> str:
        return self.event_type


def writer_permission(kind: str, source: str) -> bool:
    """DD-01 §3.3 不变量 5：模型/agent 无权产出 card/claim。"""
    if kind in SIGNING_KINDS:
        return source in SIGNING_SOURCES
    return True
