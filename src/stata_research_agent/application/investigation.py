"""Read-only fault-location projections over authoritative Agent execution facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

InvestigationLayer = Literal[
    "control",
    "context",
    "provider",
    "tool_selection",
    "tool_admission",
    "tool_execution",
    "stata_execution",
    "evaluation",
    "recovery",
]


@dataclass(frozen=True, slots=True)
class InvestigationFinding:
    layer: InvestigationLayer
    code: str
    severity: Literal["info", "warn", "error"]
    summary: str
    object_type: str
    object_id: str
    next_query: str


@dataclass(frozen=True, slots=True)
class ToolStatusObservation:
    status_ordinal: int
    proposal_status: str
    reason_code: str
    commit_revision: int


@dataclass(frozen=True, slots=True)
class ToolOperationTrace:
    operation_id: str
    operation_kind: str
    status: str
    created_revision: int
    terminal_revision: int | None
    attempts: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ToolDecisionTrace:
    turn_id: str
    step_id: str
    step_ordinal: int
    context_manifest_id: str
    model_invocation_id: str
    model_invocation_status: str
    provider_attempt_id: str
    assistant_output_id: str
    assistant_public_text: str | None
    tool_call_id: str
    call_ordinal: int
    requested_tool_name: str
    provider_tool_call_id: str | None
    proposal_status: str
    raw_arguments_text: str
    canonical_arguments: dict[str, Any] | None
    normalization_diff: dict[str, Any] | None
    tool_contract_id: str | None
    tool_version: str | None
    operation_kind: str | None
    effect_class: str | None
    execution_owner: str | None
    dispatch_plan_id: str | None
    execution_batch_ordinal: int | None
    admission_id: str | None
    admission_policy_revision: str | None
    status_history: tuple[ToolStatusObservation, ...]
    operations: tuple[ToolOperationTrace, ...]
    result_kind: str | None
    result_summary: str | None
    result_payload: Any | None
    artifact_references: tuple[str, ...]
    evaluation_findings: tuple[dict[str, Any], ...]
    journal_references: tuple[dict[str, Any], ...]
    structural_diagnosis: str


@dataclass(frozen=True, slots=True)
class TurnInvestigationSnapshot:
    turn_id: str
    turn_status: str
    turn_revision: int
    authoritative_revision: int
    last_journal_event_type: str | None
    last_workspace_revision: int | None
    findings: tuple[InvestigationFinding, ...]
    tool_call_ids: tuple[str, ...]
    investigation_note: str
