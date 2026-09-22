"""Authority port for admitted application Tool execution."""

from typing import Protocol

from stata_research_agent.application.broker_execution import (
    BeginBrokerExecutionCommand,
    BrokerExecutionHandle,
    BrokerExecutionOutcome,
    CompleteBrokerExecutionCommand,
)
from stata_research_agent.domain.identifiers import OperationAttemptId


class BrokerExecutionRepository(Protocol):
    def begin(
        self, command: BeginBrokerExecutionCommand, attempt_id: OperationAttemptId
    ) -> BrokerExecutionHandle: ...

    def complete(self, command: CompleteBrokerExecutionCommand) -> BrokerExecutionOutcome: ...
