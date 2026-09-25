"""Open Tool Contract registry, canonical calls, dispatch plans, and JIT admission DTOs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from stata_research_agent.domain.identifiers import (
    AssistantOutputId,
    BudgetUsageId,
    CanonicalArgumentsSnapshotId,
    CommandId,
    DispatchPlanEntryId,
    DispatchPlanId,
    OperationId,
    RawArgumentsSnapshotId,
    ResourceClaimId,
    ToolAdmissionId,
    ToolCallId,
    ToolContractId,
    ToolResultId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

JsonObject = Mapping[str, Any]


class ToolAdmissionBlockedError(ValueError):
    """Stable JIT admission failure that must be recorded before the loop continues."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class ResourceClaimTemplate:
    key_template: str
    access_mode: str
    identity_argument: str | None = None

    def __post_init__(self) -> None:
        if self.access_mode not in {"read", "write", "exclusive"}:
            raise ValueError("invalid resource access mode")
        if not self.key_template.strip():
            raise ValueError("resource key template is required")


@dataclass(frozen=True, slots=True)
class ToolContractDefinition:
    tool_name: str
    tool_version: str
    display_name: str
    operation_kind: str
    input_schema: JsonObject
    output_schema: JsonObject
    execution_owner: str
    effect_class: str
    concurrency_class: str
    replay_class: str
    confirmation_policy: str
    pause_behavior: str
    default_timeout_seconds: float
    max_timeout_seconds: float
    max_output_bytes: int
    resource_claim_templates: tuple[ResourceClaimTemplate, ...]
    execution_isolation: str = "none"


@dataclass(frozen=True, slots=True)
class RegisterToolContractCommand:
    command_id: CommandId
    registered_by_turn_id: TurnId
    definition: ToolContractDefinition


@dataclass(frozen=True, slots=True)
class RegisteredToolContract:
    tool_contract_id: ToolContractId
    definition: ToolContractDefinition
    contract_sha256: str
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class RawToolProposal:
    requested_tool_name: str
    raw_arguments_text: str
    provider_tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class CreateDispatchPlanCommand:
    command_id: CommandId
    assistant_output_id: AssistantOutputId
    tool_catalog_revision: str
    dependency_snapshot: JsonObject
    calls: tuple[RawToolProposal, ...]


@dataclass(frozen=True, slots=True)
class ResolvedResourceClaim:
    resource_key: str
    access_mode: str
    identity_revision: str


@dataclass(frozen=True, slots=True)
class PreparedToolCall:
    tool_call_id: ToolCallId
    raw_snapshot_id: RawArgumentsSnapshotId
    canonical_snapshot_id: CanonicalArgumentsSnapshotId | None
    rejected_result_id: ToolResultId | None
    call_ordinal: int
    proposal: RawToolProposal
    contract: RegisteredToolContract | None
    canonical_arguments_json: str | None
    arguments_sha256: str | None
    normalization_diff_json: str | None
    status: str
    reason_code: str
    entry_id: DispatchPlanEntryId | None
    resource_claims: tuple[tuple[ResourceClaimId, ResolvedResourceClaim], ...]
    execution_batch_ordinal: int | None
    barrier_before: bool
    barrier_after: bool


@dataclass(frozen=True, slots=True)
class DispatchPlanIdentity:
    dispatch_plan_id: DispatchPlanId


@dataclass(frozen=True, slots=True)
class ScheduledToolCall:
    tool_call_id: ToolCallId
    call_ordinal: int
    execution_batch_ordinal: int
    barrier_before: bool
    barrier_after: bool


@dataclass(frozen=True, slots=True)
class DispatchPlanOutcome:
    dispatch_plan_id: DispatchPlanId
    plan_revision: int
    scheduled_call_ids: tuple[ToolCallId, ...]
    scheduled_calls: tuple[ScheduledToolCall, ...]
    rejected_call_ids: tuple[ToolCallId, ...]
    batch_count: int
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class AdmitToolCallCommand:
    command_id: CommandId
    tool_call_id: ToolCallId
    expected_turn_revision: int
    admission_policy_revision: str
    allowed_effect_classes: tuple[str, ...]
    current_dependency_snapshot: JsonObject
    max_admitted_calls_per_turn: int = 128


@dataclass(frozen=True, slots=True)
class ToolAdmissionIdentity:
    admission_id: ToolAdmissionId
    operation_id: OperationId
    budget_usage_id: BudgetUsageId


@dataclass(frozen=True, slots=True)
class ToolAdmissionOutcome:
    admission_id: ToolAdmissionId
    operation_id: OperationId
    tool_call_id: ToolCallId
    status: str
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class RecordToolAdmissionBlockedCommand:
    command_id: CommandId
    turn_id: TurnId
    tool_call_id: ToolCallId
    reason_code: str


@dataclass(frozen=True, slots=True)
class ToolAdmissionBlockedOutcome:
    tool_call_id: ToolCallId
    reason_code: str
    commit_revision: WorkspaceRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class RecordExecutorExceptionCommand:
    command_id: CommandId
    turn_id: TurnId
    tool_call_id: ToolCallId
    operation_id: OperationId
    error_kind: str
    error_detail: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutorExceptionOutcome:
    operation_id: OperationId
    tool_call_id: ToolCallId
    status: str
    commit_revision: WorkspaceRevision
    replayed: bool
