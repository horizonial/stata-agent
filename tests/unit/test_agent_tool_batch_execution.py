"""Turn Driver executes Broker batches concurrently while preserving barriers."""

from __future__ import annotations

import asyncio
from pathlib import Path

from stata_research_agent.application.model_gateway import ModelStepOutcome
from stata_research_agent.application.tool_broker import (
    DispatchPlanOutcome,
    ExecutorExceptionOutcome,
    RecordExecutorExceptionCommand,
    ScheduledToolCall,
    ToolAdmissionOutcome,
)
from stata_research_agent.application.turn_driver import (
    ToolExecutionRequest,
    ToolExecutionResult,
    TurnDriverConfig,
    TurnDriverModelConfig,
)
from stata_research_agent.domain.identifiers import (
    AssistantOutputId,
    DispatchPlanId,
    ModelInvocationId,
    OperationId,
    StepId,
    ToolAdmissionId,
    ToolCallId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision
from stata_research_agent.runtime.agent_turn_driver import AgentTurnDriver
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator

PROJECT_ROOT = Path(__file__).parents[2]
PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"


class _TwoBatchGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def execute_step(self, command) -> ModelStepOutcome:
        self.calls += 1
        if self.calls == 1:
            return ModelStepOutcome(
                StepId("step_batch_1"),
                ModelInvocationId("modelinv_batch_1"),
                (),
                AssistantOutputId("assistantout_batch_1"),
                {
                    "text": "Read in parallel, then cross the write barrier.",
                    "tool_calls": [
                        {"name": "research.read_a", "arguments": {}},
                        {"name": "research.read_b", "arguments": {}},
                        {"name": "research.write", "arguments": {}},
                    ],
                },
                "completed",
                WorkspaceRevision(1),
            )
        return ModelStepOutcome(
            StepId("step_batch_2"),
            ModelInvocationId("modelinv_batch_2"),
            (),
            None,
            None,
            "failed",
            WorkspaceRevision(2),
        )


class _BatchBroker:
    calls = (
        ToolCallId("toolcall_batch_1"),
        ToolCallId("toolcall_batch_2"),
        ToolCallId("toolcall_batch_3"),
    )

    def create_dispatch_plan(self, command) -> DispatchPlanOutcome:
        del command
        return DispatchPlanOutcome(
            dispatch_plan_id=DispatchPlanId("dispatchplan_batch_1"),
            plan_revision=1,
            scheduled_call_ids=self.calls,
            scheduled_calls=(
                ScheduledToolCall(self.calls[0], 1, 1, False, False),
                ScheduledToolCall(self.calls[1], 2, 1, False, False),
                ScheduledToolCall(self.calls[2], 3, 2, True, True),
            ),
            rejected_call_ids=(),
            batch_count=2,
            commit_revision=WorkspaceRevision(1),
            replayed=False,
        )

    def admit(self, command) -> ToolAdmissionOutcome:
        return ToolAdmissionOutcome(
            ToolAdmissionId(f"admission_{command.tool_call_id.value}"),
            OperationId(f"op_{command.tool_call_id.value}"),
            command.tool_call_id,
            "admitted",
            WorkspaceRevision(1),
            False,
        )


class _BarrierExecutor:
    def __init__(self) -> None:
        self.active_reads = 0
        self.max_active_reads = 0
        self.completed: list[str] = []

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        if request.tool_name.startswith("research.read"):
            self.active_reads += 1
            self.max_active_reads = max(self.max_active_reads, self.active_reads)
            await asyncio.sleep(0.05)
            self.active_reads -= 1
        else:
            assert self.active_reads == 0
        self.completed.append(request.tool_name)
        return ToolExecutionResult(True, request.tool_name)


class _ExceptionFeedbackGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def execute_step(self, command) -> ModelStepOutcome:
        self.calls += 1
        if self.calls == 1:
            return ModelStepOutcome(
                StepId("step_exception_1"),
                ModelInvocationId("modelinv_exception_1"),
                (),
                AssistantOutputId("assistantout_exception_1"),
                {
                    "text": "Run the post-estimation command.",
                    "tool_calls": [{"name": "research.write", "arguments": {}}],
                },
                "completed",
                WorkspaceRevision(1),
            )
        content = "\n".join(item.content for item in command.context_items)
        assert "source Run has no promoted Result" in content
        assert "do not repeat it unchanged" in content
        return ModelStepOutcome(
            StepId("step_exception_2"),
            ModelInvocationId("modelinv_exception_2"),
            (),
            None,
            None,
            "failed",
            WorkspaceRevision(2),
        )


class _ExceptionBroker:
    call_id = ToolCallId("toolcall_exception_1")
    admission_id = ToolAdmissionId("admission_exception_1")
    operation_id = OperationId("op_exception_1")

    def __init__(self) -> None:
        self.recorded: RecordExecutorExceptionCommand | None = None

    def create_dispatch_plan(self, command) -> DispatchPlanOutcome:
        del command
        return DispatchPlanOutcome(
            DispatchPlanId("dispatchplan_exception_1"),
            1,
            (self.call_id,),
            (ScheduledToolCall(self.call_id, 1, 1, True, True),),
            (),
            1,
            WorkspaceRevision(1),
            False,
        )

    def admit(self, command) -> ToolAdmissionOutcome:
        del command
        return ToolAdmissionOutcome(
            self.admission_id,
            self.operation_id,
            self.call_id,
            "admitted",
            WorkspaceRevision(1),
            False,
        )

    def record_executor_exception(
        self, command: RecordExecutorExceptionCommand
    ) -> ExecutorExceptionOutcome:
        self.recorded = command
        return ExecutorExceptionOutcome(
            self.operation_id,
            self.call_id,
            "failed",
            WorkspaceRevision(2),
            False,
        )


class _ValidationExceptionExecutor:
    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        del request
        raise ValueError("formal post-estimation source Run has no promoted Result")


class _AdmissionFeedbackGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def execute_step(self, command) -> ModelStepOutcome:
        self.calls += 1
        if self.calls == 1:
            return ModelStepOutcome(
                StepId("step_admission_1"),
                ModelInvocationId("modelinv_admission_1"),
                (),
                AssistantOutputId("assistantout_admission_1"),
                {
                    "text": "Repeat an unproductive tool.",
                    "tool_calls": [{"name": "research.write", "arguments": {}}],
                },
                "completed",
                WorkspaceRevision(1),
            )
        content = "\n".join(item.content for item in command.context_items)
        assert "Tool Admission rejected" in content
        assert "do not repeat it unchanged" in content
        return ModelStepOutcome(
            StepId("step_admission_2"),
            ModelInvocationId("modelinv_admission_2"),
            (),
            None,
            None,
            "failed",
            WorkspaceRevision(2),
        )


class _AdmissionRejectingBroker(_ExceptionBroker):
    def admit(self, command) -> ToolAdmissionOutcome:
        del command
        raise ValueError("Tool no-progress guard blocks an identical empty-success call")


def test_parallel_safe_batch_completes_before_serial_write_barrier() -> None:
    executor = _BarrierExecutor()
    driver = AgentTurnDriver(
        _TwoBatchGateway(),  # type: ignore[arg-type]
        _BatchBroker(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        executor,
        UuidIdentityGenerator(),
        PYTHON,
    )
    outcome = asyncio.run(
        driver.run(
            TurnDriverConfig(
                TurnId("turn_batch_test"),
                1,
                "ws_batch_test",
                "scope_batch_test",
                "path_batch_test",
                TurnDriverModelConfig(
                    "system-v1",
                    "system",
                    "main",
                    "skill-v1",
                    "skill",
                    "catalog-v1",
                    (
                        {"name": "research.read_a"},
                        {"name": "research.read_b"},
                        {"name": "research.write"},
                    ),
                    "permission-v1",
                    {},
                    "model-policy-v1",
                    "provider",
                    "test",
                    "model",
                    "https://provider.invalid/responses",
                    "credential://test",
                    {},
                ),
                (),
                {},
                ("pure_read", "workspace_write"),
                {},
                2,
            )
        )
    )
    assert outcome.status == "failed"
    assert outcome.tool_executions == 3
    assert executor.max_active_reads == 2
    assert executor.completed[-1] == "research.write"


def test_executor_validation_error_is_recorded_and_returned_as_actionable_context() -> None:
    broker = _ExceptionBroker()
    gateway = _ExceptionFeedbackGateway()
    driver = AgentTurnDriver(
        gateway,  # type: ignore[arg-type]
        broker,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _ValidationExceptionExecutor(),
        UuidIdentityGenerator(),
        PYTHON,
    )
    outcome = asyncio.run(
        driver.run(
            TurnDriverConfig(
                TurnId("turn_exception_test"),
                1,
                "ws_exception_test",
                "scope_exception_test",
                "path_exception_test",
                TurnDriverModelConfig(
                    "system-v1",
                    "system",
                    "main",
                    "skill-v1",
                    "skill",
                    "catalog-v1",
                    ({"name": "research.write"},),
                    "permission-v1",
                    {},
                    "model-policy-v1",
                    "provider",
                    "test",
                    "model",
                    "https://provider.invalid/responses",
                    "credential://test",
                    {},
                ),
                (),
                {},
                ("workspace_write",),
                {},
                2,
            )
        )
    )
    assert outcome.status == "failed"
    assert outcome.tool_executions == 1
    assert broker.recorded is not None
    assert broker.recorded.error_detail == (
        "formal post-estimation source Run has no promoted Result"
    )


def test_admission_rejection_returns_feedback_to_model_instead_of_aborting_turn() -> None:
    gateway = _AdmissionFeedbackGateway()
    driver = AgentTurnDriver(
        gateway,  # type: ignore[arg-type]
        _AdmissionRejectingBroker(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _BarrierExecutor(),
        UuidIdentityGenerator(),
        PYTHON,
    )
    outcome = asyncio.run(
        driver.run(
            TurnDriverConfig(
                TurnId("turn_admission_feedback"),
                1,
                "ws_admission_feedback",
                "scope_admission_feedback",
                "path_admission_feedback",
                TurnDriverModelConfig(
                    "system-v1",
                    "system",
                    "main",
                    "skill-v1",
                    "skill",
                    "catalog-v1",
                    ({"name": "research.write"},),
                    "permission-v1",
                    {},
                    "model-policy-v1",
                    "provider",
                    "test",
                    "model",
                    "https://provider.invalid/responses",
                    "credential://test",
                    {},
                ),
                (),
                {},
                ("workspace_write",),
                {},
                2,
            )
        )
    )
    assert outcome.status == "failed"
    assert outcome.tool_executions == 0
    assert gateway.calls == 2
