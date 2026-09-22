"""Typed commands and results for the M0 minimal control vertical."""

from __future__ import annotations

from dataclasses import dataclass

from stata_research_agent.domain.identifiers import (
    CommandId,
    ConversationId,
    ExecutionScopeId,
    MessageId,
    ResearchPathId,
    TurnId,
    WorkspaceId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision
from stata_research_agent.domain.status import ExecutionMode, TurnStatus


@dataclass(frozen=True, slots=True)
class CreateWorkspaceCommand:
    command_id: CommandId
    workspace_id: WorkspaceId


@dataclass(frozen=True, slots=True)
class CreateWorkspaceResult:
    workspace_id: WorkspaceId
    main_path_id: ResearchPathId
    main_scope_id: ExecutionScopeId
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class SubmitMessageCommand:
    command_id: CommandId
    content: str
    execution_mode: ExecutionMode = ExecutionMode.WRITE
    conversation_id: ConversationId | None = None
    research_path_id: ResearchPathId | None = None
    goal_mode: str = "research_loop"

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("message content is required")
        if self.goal_mode not in {"research_loop", "deliver_word"}:
            raise ValueError("unsupported Turn goal mode")


@dataclass(frozen=True, slots=True)
class SubmitMessageResult:
    conversation_id: ConversationId
    message_id: MessageId
    turn_id: TurnId
    research_path_id: ResearchPathId
    turn_status: TurnStatus
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class CompleteTurnCommand:
    command_id: CommandId
    turn_id: TurnId
    terminal_status: TurnStatus

    def __post_init__(self) -> None:
        if not self.terminal_status.is_terminal:
            raise ValueError("complete turn requires a terminal status")


@dataclass(frozen=True, slots=True)
class CompleteTurnResult:
    turn_id: TurnId
    terminal_status: TurnStatus
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class ActivateNextQueuedTurnCommand:
    command_id: CommandId


@dataclass(frozen=True, slots=True)
class ActivateNextQueuedTurnResult:
    turn_id: TurnId
    turn_revision: int
    commit_revision: WorkspaceRevision
    replayed: bool
