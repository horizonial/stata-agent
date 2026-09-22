"""Real subprocess tests for the disposable, capability-isolated Turn Worker."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from stata_research_agent.runtime.ipc_contract import (
    CompletionProposal,
    EvaluationProposal,
    ModelInvocationProposal,
    PlanProposal,
    ResponseDelta,
    ToolProposal,
    WorkerBootstrapContext,
    WorkerCompletion,
    WorkerEvaluation,
    WorkerPlan,
    WorkerToolCall,
)
from stata_research_agent.runtime.turn_worker import TurnWorkerProcess, TurnWorkerProtocolError

PROJECT_ROOT = Path(__file__).parents[2]
PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
WORKER_SOURCE = PROJECT_ROOT / "src" / "stata_research_agent" / "runtime" / "worker_process.py"


def _context() -> WorkerBootstrapContext:
    return WorkerBootstrapContext(
        workspace_id="ws_worker_test",
        execution_scope_id="scope_worker_test",
        research_path_id="path_worker_test",
        plan_revision_id="planrev_worker_test",
        research_state_revision=4,
        data_version_ids=("data_worker_test",),
        artifact_refs=("artifact_worker_test",),
        tool_names=("stata.run", "artifact.read_excerpt"),
        remaining_step_budget=64,
        remaining_tool_budget=128,
    )


def test_worker_is_disposable_scrubs_credentials_and_restarts(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-worker")

    async def scenario() -> None:
        first = TurnWorkerProcess(PYTHON)
        ready = await first.start(
            turn_id="turn_worker_test",
            context_revision=7,
            context=_context(),
        )
        first_pid = first.pid
        first_session = first.worker_session_id
        assert ready.event == "worker_ready"
        assert ready.safe_code == "CAPABILITY_ISOLATED"
        assert ready.context_revision == 7
        assert first_pid is not None

        await first.terminate()
        assert first.returncode is not None

        replacement = TurnWorkerProcess(PYTHON)
        replacement_ready = await replacement.start(
            turn_id="turn_worker_test",
            context_revision=7,
            context=_context(),
        )
        assert replacement_ready.event == "worker_ready"
        assert replacement.pid != first_pid
        assert replacement.worker_session_id != first_session
        stopped = await replacement.stop()
        assert stopped.event == "worker_stopping"
        assert replacement.returncode == 0

    asyncio.run(scenario())


def test_worker_runtime_cannot_write_bytecode_back_into_the_executor() -> None:
    environment = TurnWorkerProcess._isolated_environment(Path("worker-temp"))

    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert TurnWorkerProcess._worker_arguments() == (
        "-I",
        "-B",
        "-m",
        "stata_research_agent.runtime.worker_process",
    )


def test_worker_code_has_no_authority_file_provider_or_tool_imports() -> None:
    tree = ast.parse(WORKER_SOURCE.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden = {
        "sqlite3",
        "pathlib",
        "subprocess",
        "openai",
        "anthropic",
        "mcp",
        "stata_research_agent.persistence",
        "stata_research_agent.artifacts",
        "stata_research_agent.stata",
        "stata_research_agent.documents",
    }
    assert not any(
        imported == blocked or imported.startswith(f"{blocked}.")
        for imported in imports
        for blocked in forbidden
    )


def test_worker_validates_model_output_into_versioned_proposals() -> None:
    async def scenario() -> None:
        worker = TurnWorkerProcess(PYTHON)
        await worker.start(
            turn_id="turn_worker_proposals",
            context_revision=3,
            context=_context(),
        )
        request = await worker.request_model_invocation(
            step_ordinal=1,
            trigger="initial",
            remaining_step_budget=64,
            remaining_tool_budget=128,
        )
        assert isinstance(request, ModelInvocationProposal)
        assert request.step_ordinal == 1
        messages = await worker.process_model_output(
            step_ordinal=1,
            text="I will inspect the data.",
            plan=WorkerPlan(
                summary="Inspect then estimate",
                structured_plan={"nodes": ["inspect", "estimate"]},
            ),
            tool_calls=(
                WorkerToolCall(
                    call_ordinal=1,
                    tool_name="stata.run",
                    arguments={"code": "describe"},
                ),
            ),
            evaluation=WorkerEvaluation(verdict="warn", findings=("quality_concern",)),
            completion=WorkerCompletion(disposition="succeed", summary="Candidate completion only"),
        )
        assert [type(message) for message in messages] == [
            ResponseDelta,
            PlanProposal,
            ToolProposal,
            EvaluationProposal,
            CompletionProposal,
        ]
        assert messages[2].context_revision == 3
        assert isinstance(messages[2], ToolProposal)
        assert messages[2].arguments == {"code": "describe"}
        follow_up = await worker.request_model_invocation(
            step_ordinal=2,
            trigger="tool_results_committed",
            remaining_step_budget=63,
            remaining_tool_budget=127,
        )
        assert follow_up.trigger == "tool_results_committed"
        await worker.stop()

    asyncio.run(scenario())


def test_worker_rejects_unsolicited_model_output() -> None:
    async def scenario() -> None:
        worker = TurnWorkerProcess(PYTHON)
        await worker.start(
            turn_id="turn_worker_unsolicited",
            context_revision=2,
            context=_context(),
        )
        with pytest.raises(TurnWorkerProtocolError, match="unexpected Worker lifecycle"):
            await worker.process_model_output(step_ordinal=1, text="unrequested")
        await worker.terminate()

    asyncio.run(scenario())
