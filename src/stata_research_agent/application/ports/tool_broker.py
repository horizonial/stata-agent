"""Persistence port for the open Tool Broker."""

from typing import Protocol

from stata_research_agent.application.tool_broker import (
    AdmitToolCallCommand,
    CreateDispatchPlanCommand,
    DispatchPlanIdentity,
    DispatchPlanOutcome,
    ExecutorExceptionOutcome,
    PreparedToolCall,
    RecordExecutorExceptionCommand,
    RecordToolAdmissionBlockedCommand,
    RegisteredToolContract,
    RegisterToolContractCommand,
    ToolAdmissionBlockedOutcome,
    ToolAdmissionIdentity,
    ToolAdmissionOutcome,
)
from stata_research_agent.domain.identifiers import ToolContractId


class ToolBrokerRepository(Protocol):
    def register_contract(
        self,
        command: RegisterToolContractCommand,
        tool_contract_id: ToolContractId,
        contract_sha256: str,
    ) -> RegisteredToolContract: ...

    def load_contract(self, tool_name: str) -> RegisteredToolContract | None: ...

    def commit_dispatch_plan(
        self,
        command: CreateDispatchPlanCommand,
        identity: DispatchPlanIdentity,
        prepared_calls: tuple[PreparedToolCall, ...],
        dependency_snapshot_json: str,
        dependency_snapshot_sha256: str,
    ) -> DispatchPlanOutcome: ...

    def admit(
        self,
        command: AdmitToolCallCommand,
        identity: ToolAdmissionIdentity,
        current_dependency_snapshot_json: str,
        current_dependency_snapshot_sha256: str,
    ) -> ToolAdmissionOutcome: ...

    def record_executor_exception(
        self, command: RecordExecutorExceptionCommand
    ) -> ExecutorExceptionOutcome: ...

    def record_admission_blocked(
        self, command: RecordToolAdmissionBlockedCommand
    ) -> ToolAdmissionBlockedOutcome: ...
