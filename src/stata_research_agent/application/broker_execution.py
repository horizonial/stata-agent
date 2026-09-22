"""Execution bridge for admitted non-Stata application Tool Operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from stata_research_agent.domain.identifiers import (
    CommandId,
    OperationAttemptId,
    OperationId,
    ToolCallId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

JsonObject = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class BeginBrokerExecutionCommand:
    command_id: CommandId
    turn_id: TurnId
    tool_call_id: ToolCallId
    operation_id: OperationId


@dataclass(frozen=True, slots=True)
class BrokerExecutionHandle:
    operation_id: OperationId
    attempt_id: OperationAttemptId
    tool_call_id: ToolCallId
    replayed: bool


@dataclass(frozen=True, slots=True)
class CompleteBrokerExecutionCommand:
    command_id: CommandId
    handle: BrokerExecutionHandle
    success: bool
    summary: str
    payload: JsonObject
    artifact_references: tuple[str, ...] = ()
    execution_receipt: JsonObject | None = None
    terminal_status: str | None = None

    def __post_init__(self) -> None:
        if self.terminal_status not in {
            None,
            "completed",
            "failed",
            "completed_unreconciled",
            "outcome_unknown",
            "integrity_violation",
        }:
            raise ValueError("invalid broker execution terminal status")
        if self.terminal_status == "completed" and not self.success:
            raise ValueError("completed broker execution must be successful")
        if (
            self.terminal_status
            in {
                "failed",
                "completed_unreconciled",
                "outcome_unknown",
                "integrity_violation",
            }
            and self.success
        ):
            raise ValueError("non-completed broker execution cannot be successful")


@dataclass(frozen=True, slots=True)
class BrokerExecutionOutcome:
    operation_id: OperationId
    attempt_id: OperationAttemptId
    status: str
    commit_revision: WorkspaceRevision
    replayed: bool
