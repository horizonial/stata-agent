"""Typed recovery assessment and safe-convergence contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from stata_research_agent.domain.identifiers import (
    CommandId,
    OperationAttemptId,
    OperationId,
    RecoveryReportId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision
from stata_research_agent.domain.status import RecoveryClassification


@dataclass(frozen=True, slots=True)
class RecoverOperationCommand:
    command_id: CommandId
    operation_id: OperationId


@dataclass(frozen=True, slots=True)
class RecoveryAssessment:
    """Read-only proposal. It is not authoritative until the Recovery UoW commits."""

    operation_id: OperationId
    attempt_id: OperationAttemptId | None
    requested_by_turn_id: TurnId
    classification: RecoveryClassification
    manifest_id: str | None
    reason: str
    observed_operation_status: str
    observed_attempt_status: str | None
    observed_turn_status: str
    observed_lane_owner_turn_id: str | None
    observed_lane_revision: int
    observed_journal_boundary: str
    manifest_verification: Mapping[str, Any]
    artifact_verification: Mapping[str, Any]
    blocked_actions: tuple[str, ...]
    available_user_actions: tuple[str, ...]
    input_fingerprint: str
    schema_version: str = "recovery-assessment/v1"


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    recovery_report_id: RecoveryReportId
    operation_id: OperationId
    attempt_id: OperationAttemptId | None
    requested_by_turn_id: TurnId
    classification: RecoveryClassification
    turn_status: str
    lane_released: bool
    commit_revision: WorkspaceRevision
    replayed: bool
