"""Framework-free application orchestration for Workspace control commands."""

from stata_research_agent.domain.identifiers import (
    CompletionContractId,
    CompletionContractRevisionId,
    ConversationId,
    ExecutionScopeId,
    MessageId,
    ResearchPathId,
    TurnId,
)

from .control import (
    ActivateNextQueuedTurnCommand,
    ActivateNextQueuedTurnResult,
    CompleteTurnCommand,
    CompleteTurnResult,
    CreateWorkspaceCommand,
    CreateWorkspaceResult,
    SubmitMessageCommand,
    SubmitMessageResult,
)
from .ports.control import ControlStore
from .ports.identity import IdentityGenerator
from .ports.release_activation import IrreversibilityGuard, NoopIrreversibilityGuard
from .release_activation import IrreversibleCapability


class WorkspaceControlService:
    def __init__(
        self,
        store: ControlStore,
        identities: IdentityGenerator,
        irreversibility_guard: IrreversibilityGuard | None = None,
    ) -> None:
        self._store = store
        self._identities = identities
        self._irreversibility_guard = irreversibility_guard or NoopIrreversibilityGuard()

    def create_workspace(self, command: CreateWorkspaceCommand) -> CreateWorkspaceResult:
        self._guard_mutation(command.command_id.value)
        return self._store.initialize_workspace(
            command,
            main_path_id=self._identities.new(ResearchPathId),
            main_scope_id=self._identities.new(ExecutionScopeId),
        )

    def submit_message(self, command: SubmitMessageCommand) -> SubmitMessageResult:
        self._guard_mutation(command.command_id.value)
        return self._store.submit_message(
            command,
            conversation_id=command.conversation_id or self._identities.new(ConversationId),
            message_id=self._identities.new(MessageId),
            turn_id=self._identities.new(TurnId),
            completion_contract_id=self._identities.new(CompletionContractId),
            completion_contract_revision_id=self._identities.new(CompletionContractRevisionId),
        )

    def complete_turn(self, command: CompleteTurnCommand) -> CompleteTurnResult:
        self._guard_mutation(command.command_id.value)
        return self._store.complete_turn(command)

    def activate_next_queued_turn(
        self, command: ActivateNextQueuedTurnCommand
    ) -> ActivateNextQueuedTurnResult:
        self._guard_mutation(command.command_id.value)
        return self._store.activate_next_queued_turn(command)

    def _guard_mutation(self, reference: str) -> None:
        self._irreversibility_guard.before(
            IrreversibleCapability.AUTHORITATIVE_MUTATION,
            reference=reference,
        )
