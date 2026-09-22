"""Typed Waiting and user-pause control commands."""

from dataclasses import dataclass

from stata_research_agent.domain.identifiers import (
    CommandId,
    MessageId,
    PauseIntentId,
    ToolCallId,
    ToolResultId,
    TurnContinuationId,
    TurnId,
    WaitingAnswerId,
    WaitingRequestId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision
from stata_research_agent.domain.status import TurnRelationKind, WaitReason


@dataclass(frozen=True, slots=True)
class OpenWaitingCommand:
    command_id: CommandId
    turn_id: TurnId
    expected_turn_revision: int
    wait_reason: WaitReason
    prompt: str
    tool_call_id: ToolCallId | None = None


@dataclass(frozen=True, slots=True)
class AnswerWaitingCommand:
    command_id: CommandId
    waiting_request_id: WaitingRequestId
    answer: str
    tool_decision: str | None = None
    expected_turn_revision: int | None = None
    expected_turn_id: TurnId | None = None

    def __post_init__(self) -> None:
        if self.tool_decision not in {None, "approve", "deny", "cancel"}:
            raise ValueError("invalid Waiting tool decision")


@dataclass(frozen=True, slots=True)
class RequestPauseCommand:
    command_id: CommandId
    turn_id: TurnId
    expected_turn_revision: int
    reason: str


@dataclass(frozen=True, slots=True)
class ConvergePauseCommand:
    command_id: CommandId
    turn_id: TurnId


@dataclass(frozen=True, slots=True)
class ContinuePausedTurnCommand:
    command_id: CommandId
    predecessor_turn_id: TurnId
    message: str
    relation_kind: TurnRelationKind = TurnRelationKind.USER_PAUSE_CONTINUATION


@dataclass(frozen=True, slots=True)
class WaitingOutcome:
    waiting_request_id: WaitingRequestId
    turn_id: TurnId
    turn_revision: int
    status: str
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class PauseOutcome:
    pause_intent_id: PauseIntentId
    turn_id: TurnId
    turn_revision: int
    converged: bool
    active_operation_count: int
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class WaitingIdentity:
    request_id: WaitingRequestId


@dataclass(frozen=True, slots=True)
class WaitingAnswerIdentity:
    answer_id: WaitingAnswerId
    message_id: MessageId
    tool_result_id: ToolResultId


@dataclass(frozen=True, slots=True)
class PauseIdentity:
    pause_intent_id: PauseIntentId
    cancelled_result_ids: tuple[ToolResultId, ...]


@dataclass(frozen=True, slots=True)
class ContinuationIdentity:
    continuation_id: TurnContinuationId
    successor_turn_id: TurnId
    message_id: MessageId


@dataclass(frozen=True, slots=True)
class ContinuationOutcome:
    continuation_id: TurnContinuationId
    predecessor_turn_id: TurnId
    successor_turn_id: TurnId
    successor_status: str
    commit_revision: WorkspaceRevision
    replayed: bool
