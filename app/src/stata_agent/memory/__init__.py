"""memory —— 项目记忆（约束非证据，DD-03 §8 / SPEC §4.9.1）。

与证据库(events/card/claim)严格分开：记忆只进上下文当约束，绝不当事实源。
"""

from .memstore import (
    CONFIDENCE_EXPLICIT,
    CONFIDENCE_INFERRED,
    CONFIDENCE_VERIFIED,
    GLOBAL_WORKSPACE_ID,
    KIND_CONSTRAINT,
    KIND_DECISION,
    KIND_PLAN,
    KIND_PREFERENCE,
    KIND_PROCEDURE,
    KIND_REJECTION,
    KIND_RULE,
    MemoryCandidateRejected,
    MemoryContextSelection,
    MemoryStore,
    SCOPE_GLOBAL,
    SCOPE_PROJECT,
    STATUS_ACTIVE,
    STATUS_RETRACTED,
    STATUS_SUPERSEDED,
    VALID_CONFIDENCE,
    VALID_KINDS,
    remember_decision,
)

__all__ = [
    "CONFIDENCE_EXPLICIT",
    "CONFIDENCE_INFERRED",
    "CONFIDENCE_VERIFIED",
    "GLOBAL_WORKSPACE_ID",
    "KIND_CONSTRAINT",
    "KIND_DECISION",
    "KIND_PLAN",
    "KIND_PREFERENCE",
    "KIND_PROCEDURE",
    "KIND_REJECTION",
    "KIND_RULE",
    "MemoryCandidateRejected",
    "MemoryContextSelection",
    "MemoryStore",
    "SCOPE_GLOBAL",
    "SCOPE_PROJECT",
    "STATUS_ACTIVE",
    "STATUS_RETRACTED",
    "STATUS_SUPERSEDED",
    "VALID_CONFIDENCE",
    "VALID_KINDS",
    "remember_decision",
]
