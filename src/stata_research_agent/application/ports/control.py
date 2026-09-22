"""Application port for the atomic Workspace control store."""

from typing import Protocol

from stata_research_agent.application.control import (
    ActivateNextQueuedTurnCommand,
    ActivateNextQueuedTurnResult,
    CompleteTurnCommand,
    CompleteTurnResult,
    CreateWorkspaceCommand,
    CreateWorkspaceResult,
    SubmitMessageCommand,
    SubmitMessageResult,
)
from stata_research_agent.domain.identifiers import (
    CompletionContractId,
    CompletionContractRevisionId,
    ConversationId,
    ExecutionScopeId,
    MessageId,
    ResearchPathId,
    TurnId,
)


class ControlStore(Protocol):
    def initialize_workspace(
        self,
        command: CreateWorkspaceCommand,
        *,
        main_path_id: ResearchPathId,
        main_scope_id: ExecutionScopeId,
    ) -> CreateWorkspaceResult: ...

    def submit_message(
        self,
        command: SubmitMessageCommand,
        *,
        conversation_id: ConversationId,
        message_id: MessageId,
        turn_id: TurnId,
        completion_contract_id: CompletionContractId,
        completion_contract_revision_id: CompletionContractRevisionId,
    ) -> SubmitMessageResult: ...

    def complete_turn(self, command: CompleteTurnCommand) -> CompleteTurnResult: ...

    def activate_next_queued_turn(
        self, command: ActivateNextQueuedTurnCommand
    ) -> ActivateNextQueuedTurnResult: ...
