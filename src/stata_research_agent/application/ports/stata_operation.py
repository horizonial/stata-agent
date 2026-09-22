"""Authoritative persistence port for the Stata operation protocol."""

from __future__ import annotations

from typing import Protocol

from stata_research_agent.application.stata_operation import (
    ExecuteStataCommand,
    FormalSessionDataBinding,
    PlannedArtifactCandidate,
    PublishedArtifactCandidate,
    StataOperationHandle,
    StataOperationOutcome,
)
from stata_research_agent.domain.identifiers import (
    ArtifactCapturePlanId,
    CommandId,
    CompletionManifestId,
    ExecutableSourceId,
    OperationAttemptId,
    OperationId,
    TurnId,
)
from stata_research_agent.domain.stata_execution import StataRuntimeResult


class StataOperationRepository(Protocol):
    def handoff(
        self,
        command: ExecuteStataCommand,
        *,
        handoff_command_id: CommandId,
        operation_id: OperationId,
        attempt_id: OperationAttemptId,
        capture_plan_id: ArtifactCapturePlanId,
        planned_candidates: tuple[PlannedArtifactCandidate, ...],
        executable_source_id: ExecutableSourceId,
    ) -> StataOperationHandle: ...

    def existing_outcome(
        self, command: ExecuteStataCommand, handle: StataOperationHandle
    ) -> StataOperationOutcome: ...

    def finalize(
        self,
        command: ExecuteStataCommand,
        handle: StataOperationHandle,
        result: StataRuntimeResult,
        *,
        manifest_id: CompletionManifestId,
        published_candidates: tuple[PublishedArtifactCandidate, ...],
    ) -> StataOperationOutcome: ...

    def mark_transport_unknown(
        self,
        command: ExecuteStataCommand,
        handle: StataOperationHandle,
        *,
        error_kind: str,
        error_detail: str,
    ) -> StataOperationOutcome: ...

    def authorize_reconciliation(
        self,
        command: ExecuteStataCommand,
        handle: StataOperationHandle,
        *,
        authorization_command_id: CommandId,
        authorized_by_turn_id: TurnId,
    ) -> None: ...

    def session_data_binding(self, operation_id: OperationId) -> FormalSessionDataBinding: ...
