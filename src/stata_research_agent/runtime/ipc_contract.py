"""Versioned Worker IPC envelopes for stdin/stdout NDJSON transport."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


class IpcModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkerBootstrapContext(IpcModel):
    workspace_id: str = Field(pattern=r"^ws_.+")
    execution_scope_id: str = Field(pattern=r"^scope_.+")
    research_path_id: str = Field(pattern=r"^path_.+")
    plan_revision_id: str | None = Field(default=None, pattern=r"^planrev_.+")
    research_state_revision: int = Field(ge=0)
    data_version_ids: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    remaining_step_budget: int = Field(ge=0)
    remaining_tool_budget: int = Field(ge=0)

    @field_validator("data_version_ids")
    @classmethod
    def validate_data_version_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.startswith("data_") for value in values):
            raise ValueError("Worker context accepts only DataVersion IDs")
        return values

    @field_validator("artifact_refs")
    @classmethod
    def validate_artifact_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.startswith("artifact_") for value in values):
            raise ValueError("Worker context accepts only Artifact IDs")
        return values

    @field_validator("tool_names")
    @classmethod
    def validate_tool_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not value
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in value
            )
            for value in values
        ):
            raise ValueError("Worker tool names must use the canonical safe alphabet")
        return values


class WorkerBootstrap(IpcModel):
    protocol_version: Literal["2"]
    message_type: Literal["worker_bootstrap"]
    message_id: str
    worker_session_id: str = Field(pattern=r"^worker_.+")
    turn_id: str = Field(pattern=r"^turn_.+")
    context_revision: int = Field(ge=1)
    context: WorkerBootstrapContext


class WorkerShutdown(IpcModel):
    protocol_version: Literal["2"]
    message_type: Literal["worker_shutdown"]
    message_id: str
    worker_session_id: str = Field(pattern=r"^worker_.+")
    turn_id: str = Field(pattern=r"^turn_.+")
    context_revision: int = Field(ge=1)


class WorkerAdvance(IpcModel):
    """Authorize the disposable Worker to request one logical model invocation."""

    protocol_version: Literal["2"]
    message_type: Literal["worker_advance"]
    message_id: str
    worker_session_id: str = Field(pattern=r"^worker_.+")
    turn_id: str = Field(pattern=r"^turn_.+")
    context_revision: int = Field(ge=1)
    step_ordinal: int = Field(ge=1)
    trigger: Literal[
        "initial", "tool_results_committed", "stop_guard_continue", "driver_feedback"
    ]
    remaining_step_budget: int = Field(ge=1)
    remaining_tool_budget: int = Field(ge=0)


class WorkerToolCall(IpcModel):
    call_ordinal: int = Field(ge=1)
    tool_name: str
    arguments: dict[str, Any]


class WorkerPlan(IpcModel):
    summary: str
    structured_plan: dict[str, Any]


class WorkerCompletion(IpcModel):
    disposition: Literal["succeed", "partial", "pause", "fail"]
    summary: str


class WorkerEvaluation(IpcModel):
    verdict: Literal["pass", "warn", "fail", "unknown"]
    findings: tuple[str, ...] = ()


class WorkerWaiting(IpcModel):
    reason: Literal["user_input", "user_confirmation", "external_resolution"]
    prompt: str = Field(min_length=1, max_length=20_000)


class WorkerModelOutput(IpcModel):
    protocol_version: Literal["2"]
    message_type: Literal["worker_model_output"]
    message_id: str
    worker_session_id: str = Field(pattern=r"^worker_.+")
    turn_id: str = Field(pattern=r"^turn_.+")
    context_revision: int = Field(ge=1)
    step_ordinal: int = Field(ge=1)
    text: str = ""
    plan: WorkerPlan | None = None
    tool_calls: tuple[WorkerToolCall, ...] = ()
    evaluation: WorkerEvaluation | None = None
    waiting: WorkerWaiting | None = None
    completion: WorkerCompletion | None = None

    @field_validator("tool_calls")
    @classmethod
    def validate_tool_call_ordinals(
        cls, values: tuple[WorkerToolCall, ...]
    ) -> tuple[WorkerToolCall, ...]:
        if [value.call_ordinal for value in values] != list(range(1, len(values) + 1)):
            raise ValueError("Worker Tool Calls must use contiguous one-based ordinals")
        return values


class ResponseDelta(IpcModel):
    protocol_version: Literal["2"]
    message_type: Literal["response_delta"]
    message_id: str
    turn_id: str = Field(pattern=r"^turn_.+")
    context_revision: int = Field(ge=1)
    text: str


class PlanProposal(IpcModel):
    protocol_version: Literal["2"]
    message_type: Literal["plan_proposal"]
    message_id: str
    turn_id: str = Field(pattern=r"^turn_.+")
    context_revision: int = Field(ge=1)
    summary: str
    structured_plan: dict[str, Any]


class ToolProposal(IpcModel):
    protocol_version: Literal["2"]
    message_type: Literal["tool_proposal"]
    message_id: str
    turn_id: str = Field(pattern=r"^turn_.+")
    context_revision: int = Field(ge=1)
    call_ordinal: int = Field(ge=1)
    tool_name: str
    arguments: dict[str, Any]


class ContextRequest(IpcModel):
    protocol_version: Literal["2"]
    message_type: Literal["context_request"]
    message_id: str
    turn_id: str = Field(pattern=r"^turn_.+")
    context_revision: int = Field(ge=1)
    request_kind: Literal["artifact_excerpt", "research_fact", "tool_schema"]
    reference_id: str


class EvaluationProposal(IpcModel):
    protocol_version: Literal["2"]
    message_type: Literal["evaluation_proposal"]
    message_id: str
    turn_id: str = Field(pattern=r"^turn_.+")
    context_revision: int = Field(ge=1)
    verdict: Literal["pass", "warn", "fail", "unknown"]
    findings: tuple[str, ...]


class WaitingProposal(IpcModel):
    protocol_version: Literal["2"]
    message_type: Literal["waiting_proposal"]
    message_id: str
    turn_id: str = Field(pattern=r"^turn_.+")
    context_revision: int = Field(ge=1)
    reason: Literal["user_input", "user_confirmation", "external_resolution"]
    prompt: str = Field(min_length=1, max_length=20_000)


class CompletionProposal(IpcModel):
    protocol_version: Literal["2"]
    message_type: Literal["completion_proposal"]
    message_id: str
    turn_id: str = Field(pattern=r"^turn_.+")
    context_revision: int = Field(ge=1)
    disposition: Literal["succeed", "partial", "pause", "fail"]
    summary: str


class LifecycleEvent(IpcModel):
    protocol_version: Literal["2"]
    message_type: Literal["lifecycle_event"]
    message_id: str
    worker_session_id: str = Field(pattern=r"^worker_.+")
    turn_id: str = Field(pattern=r"^turn_.+")
    context_revision: int = Field(ge=1)
    event: Literal["worker_ready", "step_output_processed", "worker_stopping", "worker_failed"]
    safe_code: str | None = None


class ModelInvocationProposal(IpcModel):
    """A credential-free request from the Worker for the next frozen Step."""

    protocol_version: Literal["2"]
    message_type: Literal["model_invocation_proposal"]
    message_id: str
    turn_id: str = Field(pattern=r"^turn_.+")
    context_revision: int = Field(ge=1)
    step_ordinal: int = Field(ge=1)
    trigger: Literal[
        "initial", "tool_results_committed", "stop_guard_continue", "driver_feedback"
    ]
    remaining_step_budget: int = Field(ge=1)
    remaining_tool_budget: int = Field(ge=0)


AgentIpcMessage = Annotated[
    ResponseDelta
    | PlanProposal
    | ToolProposal
    | ContextRequest
    | EvaluationProposal
    | WaitingProposal
    | CompletionProposal
    | ModelInvocationProposal
    | LifecycleEvent,
    Field(discriminator="message_type"),
]

WorkerIpcInput = Annotated[
    WorkerBootstrap | WorkerAdvance | WorkerModelOutput | WorkerShutdown,
    Field(discriminator="message_type"),
]

IPC_ADAPTER: TypeAdapter[AgentIpcMessage] = TypeAdapter(AgentIpcMessage)
WORKER_INPUT_ADAPTER: TypeAdapter[WorkerIpcInput] = TypeAdapter(WorkerIpcInput)


def parse_ndjson_line(line: str) -> AgentIpcMessage:
    """Validate one complete line; transport framing never accepts partial JSON."""

    if "\n" in line or "\r" in line:
        raise ValueError("IPC parser accepts exactly one newline-free JSON object")
    return IPC_ADAPTER.validate_json(line)


def encode_ndjson_line(message: AgentIpcMessage) -> str:
    payload = message.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"


def parse_worker_input_line(line: str) -> WorkerIpcInput:
    if "\n" in line or "\r" in line:
        raise ValueError("IPC parser accepts exactly one newline-free JSON object")
    return WORKER_INPUT_ADAPTER.validate_json(line)


def encode_worker_input_line(message: WorkerIpcInput) -> str:
    payload = message.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
