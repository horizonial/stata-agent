"""Inward-facing contracts for the deterministic Agent Turn Driver."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from stata_research_agent.domain.identifiers import (
    CompletionObligationId,
    OperationId,
    ToolCallId,
    TurnId,
)

from .evaluation import StopGuardOutcome
from .model_gateway import ContextItemCandidate

JsonObject = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class TurnDriverModelConfig:
    system_prompt_revision: str
    system_prompt: str
    main_skill_name: str
    main_skill_revision: str
    main_skill_content: str
    tool_catalog_revision: str
    tool_schemas: tuple[JsonObject, ...]
    permission_policy_revision: str
    permissions: JsonObject
    model_policy_revision: str
    provider_profile: str
    provider_kind: str
    model_name: str
    endpoint: str
    credential_ref: str
    provider_policy: JsonObject
    remote_provider: bool = True
    context_window_tokens: int = 128_000
    max_output_tokens: int = 8_192
    reserved_runtime_tokens: int = 4_096


@dataclass(frozen=True, slots=True)
class TurnDriverConfig:
    turn_id: TurnId
    initial_turn_revision: int
    workspace_id: str
    execution_scope_id: str
    research_path_id: str
    model: TurnDriverModelConfig
    initial_context: tuple[ContextItemCandidate, ...]
    dependency_snapshot: JsonObject
    allowed_effect_classes: tuple[str, ...]
    obligation_by_tool_name: Mapping[str, tuple[CompletionObligationId, ...]]
    max_loop_steps: int = 64
    max_tool_admissions: int = 128
    max_wall_clock_seconds: float = 3600.0
    runtime_policy_revision: str = "turn-runtime-v1"


@dataclass(frozen=True, slots=True)
class ToolExecutionRequest:
    turn_id: TurnId
    tool_call_id: ToolCallId
    operation_id: OperationId
    tool_name: str
    arguments: JsonObject
    remaining_time_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    success: bool
    context_text: str


class AdmittedToolExecutor(Protocol):
    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult: ...


class RetrievalSessionFinalizer(Protocol):
    def conclude_open_sessions(self, turn_id: TurnId) -> tuple[str, ...]: ...


class TurnRuntimeBudgetLedger(Protocol):
    def remaining_seconds(
        self,
        turn_id: TurnId,
        *,
        policy_revision: str,
        configured_max_seconds: float,
    ) -> float: ...

    def consume_seconds(self, turn_id: TurnId, elapsed_seconds: float) -> None: ...


@dataclass(frozen=True, slots=True)
class TurnDriverOutcome:
    turn_id: TurnId
    status: str
    executed_steps: int
    tool_executions: int
    stop_guard: StopGuardOutcome | None
    failure_code: str | None = None
