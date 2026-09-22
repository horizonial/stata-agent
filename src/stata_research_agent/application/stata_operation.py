"""Commands and results for one supervised Stata side-effect operation."""

from __future__ import annotations

import re
from dataclasses import dataclass

from stata_research_agent.domain.identifiers import (
    ArtifactCandidateId,
    ArtifactCapturePlanId,
    ArtifactId,
    ArtifactLocationId,
    ArtifactPromotionId,
    ArtifactStateObservationId,
    ArtifactVerificationReceiptId,
    CommandId,
    CompletionManifestId,
    DataVersionId,
    ExecutableSourceId,
    OperationAttemptId,
    OperationId,
    ToolCallId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision
from stata_research_agent.domain.stata_execution import StataExecutionStatus

_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True, slots=True)
class ArtifactOutputExpectation:
    output_slot: str
    relative_staging_path: str
    artifact_kind: str
    media_type: str
    required: bool = True

    def __post_init__(self) -> None:
        for value in (
            self.output_slot,
            self.relative_staging_path,
            self.artifact_kind,
            self.media_type,
        ):
            if not value.strip():
                raise ValueError("artifact output expectation fields are required")
        parts = re.split(r"[/\\]+", self.relative_staging_path)
        if self.relative_staging_path.startswith(("/", "\\")) or ".." in parts:
            raise ValueError("artifact output path must be a safe relative path")


@dataclass(frozen=True, slots=True)
class PlannedArtifactCandidate:
    candidate_id: ArtifactCandidateId
    artifact_id: ArtifactId
    promotion_id: ArtifactPromotionId
    state_observation_id: ArtifactStateObservationId
    location_id: ArtifactLocationId
    expectation: ArtifactOutputExpectation


@dataclass(frozen=True, slots=True)
class PublishedArtifactCandidate:
    candidate_id: ArtifactCandidateId
    artifact_id: ArtifactId
    promotion_id: ArtifactPromotionId
    state_observation_id: ArtifactStateObservationId
    location_id: ArtifactLocationId
    output_slot: str
    relative_staging_path: str
    artifact_kind: str
    media_type: str
    producer_locator: str
    expected: bool
    managed_handle: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ExecuteStataCommand:
    command_id: CommandId
    requested_by_turn_id: TurnId
    session_id: str
    code: str
    timeout_seconds: float = 300.0
    tool_call_id: ToolCallId | None = None
    admitted_operation_id: OperationId | None = None
    expected_outputs: tuple[ArtifactOutputExpectation, ...] = ()
    input_data_version_id: DataVersionId | None = None
    input_data_slot_key: str | None = None
    input_verification_receipt_id: ArtifactVerificationReceiptId | None = None
    execution_purpose: str = "general"
    source_data_state_operation_id: OperationId | None = None
    expected_data_state_token: str | None = None
    expected_session_generation: int | None = None

    def __post_init__(self) -> None:
        if not _SESSION_ID.fullmatch(self.session_id):
            raise ValueError("invalid Stata session_id")
        if not self.code.strip():
            raise ValueError("Stata code is required")
        if not 0.1 <= self.timeout_seconds <= 3600:
            raise ValueError("timeout_seconds must be between 0.1 and 3600")
        if (self.admitted_operation_id is None) != (self.tool_call_id is None):
            raise ValueError("admitted_operation_id and tool_call_id must be provided together")
        slots = [output.output_slot for output in self.expected_outputs]
        if len(slots) != len(set(slots)):
            raise ValueError("artifact output slots must be unique")
        input_fields = (
            self.input_data_version_id,
            self.input_data_slot_key,
            self.input_verification_receipt_id,
        )
        if any(value is not None for value in input_fields) and not all(
            value is not None for value in input_fields
        ):
            raise ValueError("formal data input binding fields must be provided together")
        if self.execution_purpose not in {
            "general",
            "data_load",
            "data_step",
            "formal_estimation",
            "formal_post_estimation",
        }:
            raise ValueError("unsupported Stata execution purpose")
        if self.execution_purpose == "general" and any(value is not None for value in input_fields):
            raise ValueError("a Data Version binding requires a data execution purpose")
        if self.execution_purpose == "data_load" and not all(
            value is not None for value in input_fields
        ):
            raise ValueError("data_load requires a complete Data Version binding")
        formal_state = (
            self.source_data_state_operation_id,
            self.expected_data_state_token,
            self.expected_session_generation,
        )
        if self.execution_purpose in {
            "data_step",
            "formal_estimation",
            "formal_post_estimation",
        } and not (
            all(value is not None for value in input_fields)
            and all(value is not None for value in formal_state)
        ):
            raise ValueError(f"{self.execution_purpose} requires a frozen session data binding")
        if self.execution_purpose not in {
            "data_step",
            "formal_estimation",
            "formal_post_estimation",
        } and any(value is not None for value in formal_state):
            raise ValueError("session data binding is only valid for transformation or estimation")


@dataclass(frozen=True, slots=True)
class StataOperationHandle:
    operation_id: OperationId
    attempt_id: OperationAttemptId
    status: str
    replayed: bool
    capture_plan_id: ArtifactCapturePlanId | None = None
    planned_candidates: tuple[PlannedArtifactCandidate, ...] = ()
    executable_source_id: ExecutableSourceId | None = None


@dataclass(frozen=True, slots=True)
class StataOperationOutcome:
    operation_id: OperationId
    attempt_id: OperationAttemptId
    status: str
    execution_status: StataExecutionStatus | None
    manifest_id: CompletionManifestId | None
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class FormalSessionDataBinding:
    source_data_state_operation_id: OperationId
    data_version_id: DataVersionId
    session_id: str
    session_generation: int
    data_state_token: str
