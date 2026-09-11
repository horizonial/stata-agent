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
# Context projection telemetry.  This event deliberately carries only layer
# totals/source ids; it is not a transcript or a second research ledger.
EVENT_CONTEXT_ASSEMBLED = "context.assembled"
EVENT_HEALTH = "health.probe"
EVENT_BUDGET = "budget.limit"
EVENT_PRIVACY = "privacy.mode.changed"
EVENT_FALLBACK = "provider.fallback"
# Provider lifecycle telemetry is additive operational metadata.  It never
# participates in the research reducer and never carries prompt/response
# content; the application projector may use it to explain model turns.
EVENT_PROVIDER_TURN_STARTED = "provider.turn.started"
EVENT_PROVIDER_TURN_COMPLETED = "provider.turn.completed"
EVENT_PROVIDER_TURN_FAILED = "provider.turn.failed"
EVENT_RESTORED = "system.restored"
EVENT_ARTIFACT = "artifact.stored"
EVENT_FILES_DEL = "files.delete_request"
# Attachment lifecycle events are operational metadata only.  Reducers may
# safely ignore them; the attachment table in the same SQLite database is the
# authoritative lifecycle index, while these events preserve an audit trail.
EVENT_ATTACHMENT_INTAKE_REQUESTED = "attachment.intake.requested"
EVENT_ATTACHMENT_READY = EVENT_ARTIFACT
EVENT_ATTACHMENT_QUARANTINED = "attachment.quarantined"
EVENT_ATTACHMENT_REJECTED = "attachment.rejected"
EVENT_ATTACHMENT_FAILED = "attachment.failed"
# Compatibility spellings for adapters that prefer shorter lifecycle names.
EVENT_ATTACHMENT_REQUESTED = EVENT_ATTACHMENT_INTAKE_REQUESTED
EVENT_ATTACHMENT_QUARANTINE = EVENT_ATTACHMENT_QUARANTINED
EVENT_ATTACHMENT_REJECT = EVENT_ATTACHMENT_REJECTED
EVENT_ATTACHMENT_FAILURE = EVENT_ATTACHMENT_FAILED
# Optional model-assisted memory intake.  These events are audit metadata only:
# they never participate in the research reducer and never carry model output.
EVENT_MEMORY_EXTRACTION_REQUESTED = "memory.extraction.requested"
EVENT_MEMORY_EXTRACTION_COMPLETED = "memory.extraction.completed"
EVENT_MEMORY_EXTRACTION_NOOP = "memory.extraction.noop"
EVENT_MEMORY_EXTRACTION_FAILED = "memory.extraction.failed"
EVENT_MEMORY_EXTRACTION_DENIED = "memory.extraction.denied"
# Candidate review uses a small two-phase audit protocol.  These events are
# UI metadata and remain reducer-agnostic, but named constants keep clients
# from depending on ad-hoc strings.
EVENT_MEMORY_REVIEW_REQUESTED = "memory.candidate.review.requested"
EVENT_MEMORY_REVIEW_COMPLETED = "memory.candidate.review.completed"
EVENT_MEMORY_REVIEW_FAILED = "memory.candidate.review.failed"
# Short aliases mirror the existing ``*_REQ``/``*_DONE`` naming used by
# callers while the full names remain the canonical public constants.
EVENT_MEMORY_EXTRACTION_REQ = EVENT_MEMORY_EXTRACTION_REQUESTED
EVENT_MEMORY_EXTRACTION_DONE = EVENT_MEMORY_EXTRACTION_COMPLETED

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

# 允许产出不可变证据(EvidenceCard/Claim)的来源（DD-01 §2.7 写者矩阵）。
#
# Keep the roles separate.  A string in ``Event.source`` is not a capability by
# itself, but requiring the same role in actor/source/payload gives the append
# boundary a useful defence against a model (or a stale caller) spoofing a
# validator write.  The public constants are kept for callers that used the
# original, coarser ``SIGNING_SOURCES`` contract.
CARD_SIGNING_SOURCES = frozenset({ACTOR_VALIDATOR})
CLAIM_SIGNING_SOURCES = frozenset({ACTOR_EVIDENCE})
RETRACT_SOURCES = frozenset({ACTOR_VALIDATOR, ACTOR_EVIDENCE})
SIGNING_SOURCES = frozenset({*CARD_SIGNING_SOURCES, *CLAIM_SIGNING_SOURCES})
SIGNING_KINDS = frozenset({EVENT_CARD_SIGNED, EVENT_CLAIM_SIGNED, EVENT_CLAIM_RETRACT})

# 需要 fingerprint（幂等）的事件（DD-01 §3.3）：工具执行
FINGERPRINT_KINDS = frozenset(
    {
        EVENT_TOOL_CALL,
        EVENT_RUN_REQ,
        EVENT_MEMORY_EXTRACTION_REQUESTED,
        EVENT_MEMORY_EXTRACTION_COMPLETED,
        EVENT_MEMORY_EXTRACTION_NOOP,
        EVENT_MEMORY_EXTRACTION_FAILED,
        EVENT_MEMORY_EXTRACTION_DENIED,
        EVENT_MEMORY_REVIEW_REQUESTED,
        EVENT_MEMORY_REVIEW_COMPLETED,
        EVENT_MEMORY_REVIEW_FAILED,
        EVENT_ATTACHMENT_INTAKE_REQUESTED,
        EVENT_ARTIFACT,
        EVENT_ATTACHMENT_QUARANTINED,
        EVENT_ATTACHMENT_REJECTED,
        EVENT_ATTACHMENT_FAILED,
    }
)


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
    if kind == EVENT_CARD_SIGNED:
        return source in CARD_SIGNING_SOURCES
    if kind == EVENT_CLAIM_SIGNED:
        return source in CLAIM_SIGNING_SOURCES
    if kind == EVENT_CLAIM_RETRACT:
        return source in RETRACT_SOURCES
    return True
