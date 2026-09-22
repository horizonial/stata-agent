"""Closed lifecycle, control, and recovery vocabularies frozen by the ADR set."""

from enum import StrEnum


class ExecutionMode(StrEnum):
    READ = "read"
    WRITE = "write"


class TurnStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    PAUSED = "paused"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in {
            TurnStatus.SUCCEEDED,
            TurnStatus.PARTIAL,
            TurnStatus.PAUSED,
            TurnStatus.FAILED,
        }


class StopGuardDecision(StrEnum):
    CONTINUE = "continue"
    WAIT = "wait"
    TERMINATE = "terminate"


class ContinueDirective(StrEnum):
    ORDINARY = "ordinary"
    REVISE = "revise"
    REPLAN = "replan"


class WaitReason(StrEnum):
    USER_INPUT = "user_input"
    USER_CONFIRMATION = "user_confirmation"
    EXTERNAL_RESOLUTION = "external_resolution"


class TerminalDisposition(StrEnum):
    SUCCEED = "succeed"
    PARTIAL = "partial"
    PAUSE = "pause"
    FAIL = "fail"


class RecoveryClassification(StrEnum):
    DEFINITELY_NOT_STARTED = "definitely_not_started"
    OUTCOME_UNKNOWN = "outcome_unknown"
    COMPLETED_UNRECONCILED = "completed_unreconciled"
    COMMITTED = "committed"
    INTEGRITY_VIOLATION = "integrity_violation"


class TurnRelationKind(StrEnum):
    RECOVERY_CONTINUATION = "recovery_continuation"
    USER_PAUSE_CONTINUATION = "user_pause_continuation"


class ContractContinuationDecision(StrEnum):
    RETAIN_CONTRACT_REVISION = "retain_contract_revision"
    REVISE_CONTRACT = "revise_contract"
    NEW_CONTRACT = "new_contract"


class ArtifactAvailability(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    CORRUPT = "corrupt"
    DELETED = "deleted"
