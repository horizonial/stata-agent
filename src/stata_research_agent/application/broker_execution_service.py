"""Application orchestration for generic admitted Tool execution."""

from stata_research_agent.domain.identifiers import OperationAttemptId

from .broker_execution import (
    BeginBrokerExecutionCommand,
    BrokerExecutionHandle,
    BrokerExecutionOutcome,
    CompleteBrokerExecutionCommand,
)
from .ports.broker_execution import BrokerExecutionRepository
from .ports.identity import IdentityGenerator
from .ports.release_activation import IrreversibilityGuard, NoopIrreversibilityGuard
from .release_activation import IrreversibleCapability


class BrokerExecutionService:
    def __init__(
        self,
        repository: BrokerExecutionRepository,
        identities: IdentityGenerator,
        irreversibility_guard: IrreversibilityGuard | None = None,
    ) -> None:
        self._repository = repository
        self._identities = identities
        self._irreversibility_guard = irreversibility_guard or NoopIrreversibilityGuard()

    def begin(self, command: BeginBrokerExecutionCommand) -> BrokerExecutionHandle:
        self._irreversibility_guard.before(
            IrreversibleCapability.TOOL_HANDOFF,
            reference=command.operation_id.value,
        )
        return self._repository.begin(command, self._identities.new(OperationAttemptId))

    def complete(self, command: CompleteBrokerExecutionCommand) -> BrokerExecutionOutcome:
        return self._repository.complete(command)
