"""M4-05 deterministic cross-Workspace execution isolation and concurrency."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from stata_research_agent.application.control import (
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.stata_operation import StataOperationOutcome
from stata_research_agent.application.stata_tool_executor import StataToolExecutor
from stata_research_agent.application.turn_driver import ToolExecutionRequest
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import (
    CommandId,
    CompletionManifestId,
    OperationAttemptId,
    OperationId,
    ToolCallId,
    TurnId,
    WorkspaceId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision
from stata_research_agent.domain.stata_execution import (
    StataExecutionReceipt,
    StataExecutionStatus,
    StataRuntimeResult,
    StataSessionCloseResult,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.execution_scope_query import (
    SqliteExecutionScopeAuthority,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator
from stata_research_agent.runtime.workspace_execution import (
    WorkspaceExecutionContractError,
    WorkspaceExecutionPool,
)


@dataclass(slots=True)
class ConcurrencyProbe:
    active: int = 0
    maximum: int = 0
    started: int = 0


class ProbeRuntime:
    def __init__(self, working_directory: Path, probe: ConcurrencyProbe) -> None:
        self.working_directory = working_directory
        self.probe = probe
        self.closed = False

    async def start(self) -> None:
        self.working_directory.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        self.closed = True

    async def configure_session_workspace(
        self, *, session_id: str, working_directory: Path
    ) -> None:
        del session_id
        working_directory.mkdir(parents=True, exist_ok=True)

    async def execute(
        self,
        *,
        session_id: str,
        code: str,
        timeout_seconds: float,
        operation_attempt_id: str | None = None,
        artifact_outputs: tuple[object, ...] = (),
    ) -> StataRuntimeResult:
        del timeout_seconds, operation_attempt_id, artifact_outputs
        self.probe.started += 1
        self.probe.active += 1
        self.probe.maximum = max(self.probe.maximum, self.probe.active)
        try:
            await asyncio.sleep(0.03)
        finally:
            self.probe.active -= 1
        if code == "crash":
            raise RuntimeError("isolated runtime failure")
        return StataRuntimeResult(
            envelope_schema_version="stata-mcp.envelope/v1",
            text=code,
            structured=None,
            receipt=StataExecutionReceipt(
                schema_version="stata-mcp.execution-receipt/v1",
                executor_instance_id=f"executor-{session_id}",
                session_id=session_id,
                session_generation=1,
                exec_seq=self.probe.started,
                execution_status=StataExecutionStatus.SUCCEEDED,
                rc=0,
                raw_output_status="complete",
                structured_result_status="not_requested",
                command_hash=None,
                data_signature=None,
                session_reset=False,
                runtime_environment={},
                supervision_proof={},
            ),
            is_error=False,
        )

    async def close_session(self, *, session_id: str, reason: str) -> StataSessionCloseResult:
        return StataSessionCloseResult(
            "stata-mcp.session-control/v1",
            f"executor-{session_id}",
            session_id,
            True,
            {"reason": reason},
        )


def initialized_running_turn(root: Path, workspace_id: str):
    database = WorkspaceDatabase(root / workspace_id, WorkspaceId(workspace_id))
    database.create()
    connection = database.open(writable=True)
    try:
        service = WorkspaceControlService(SqliteControlStore(connection), UuidIdentityGenerator())
        service.create_workspace(
            CreateWorkspaceCommand(
                CommandId(f"cmd_create_{workspace_id}"), WorkspaceId(workspace_id)
            )
        )
        running = service.submit_message(
            SubmitMessageCommand(CommandId(f"cmd_running_{workspace_id}"), "Run the active study")
        )
        queued = service.submit_message(
            SubmitMessageCommand(
                CommandId(f"cmd_queued_{workspace_id}"), "Run after the active study"
            )
        )
    finally:
        connection.close()
    return database, running, queued


def test_cross_workspace_parallelism_keeps_scope_resources_isolated(
    tmp_path: Path,
) -> None:
    database_a, running_a, _ = initialized_running_turn(tmp_path, "ws_parallel_a")
    database_b, running_b, _ = initialized_running_turn(tmp_path, "ws_parallel_b")
    probe = ConcurrencyProbe()
    runtimes: list[ProbeRuntime] = []

    def factory(working_directory: Path) -> ProbeRuntime:
        runtime = ProbeRuntime(working_directory, probe)
        runtimes.append(runtime)
        return runtime

    pool = WorkspaceExecutionPool(factory)
    runtime_a = pool.runtime_for_active_write_turn(
        SqliteExecutionScopeAuthority(database_a), running_a.turn_id
    )
    runtime_b = pool.runtime_for_active_write_turn(
        SqliteExecutionScopeAuthority(database_b), running_b.turn_id
    )

    assert runtime_a.session_id != runtime_b.session_id
    assert runtime_a.working_directory != runtime_b.working_directory
    assert runtime_a.working_directory.is_relative_to(database_a.root.resolve())
    assert runtime_b.working_directory.is_relative_to(database_b.root.resolve())

    async def scenario() -> None:
        results = await asyncio.gather(
            runtime_a.execute(
                session_id=runtime_a.session_id, code="workspace-a", timeout_seconds=1
            ),
            runtime_b.execute(
                session_id=runtime_b.session_id, code="workspace-b", timeout_seconds=1
            ),
        )
        assert {result.text for result in results} == {"workspace-a", "workspace-b"}
        assert probe.maximum == 2

        probe.maximum = 0
        await asyncio.gather(
            runtime_a.execute(session_id=runtime_a.session_id, code="same-a-1", timeout_seconds=1),
            runtime_a.execute(session_id=runtime_a.session_id, code="same-a-2", timeout_seconds=1),
        )
        assert probe.maximum == 1

        with pytest.raises(WorkspaceExecutionContractError):
            await runtime_a.execute(
                session_id=runtime_b.session_id,
                code="cross-scope",
                timeout_seconds=1,
            )

        with pytest.raises(RuntimeError, match="isolated runtime failure"):
            await runtime_a.execute(
                session_id=runtime_a.session_id, code="crash", timeout_seconds=1
            )
        healthy = await runtime_b.execute(
            session_id=runtime_b.session_id,
            code="workspace-b-still-running",
            timeout_seconds=1,
        )
        assert healthy.text == "workspace-b-still-running"
        await pool.close()

    asyncio.run(scenario())
    assert all(runtime.closed for runtime in runtimes)


def test_only_each_workspace_write_lane_owner_can_resolve_execution_resources(
    tmp_path: Path,
) -> None:
    database_a, running_a, queued_a = initialized_running_turn(tmp_path, "ws_lane_a")
    database_b, running_b, queued_b = initialized_running_turn(tmp_path, "ws_lane_b")
    pool = WorkspaceExecutionPool(
        lambda working_directory: ProbeRuntime(working_directory, ConcurrencyProbe())
    )

    authority_a = SqliteExecutionScopeAuthority(database_a)
    authority_b = SqliteExecutionScopeAuthority(database_b)
    runtime_a = pool.runtime_for_active_write_turn(authority_a, running_a.turn_id)
    runtime_b = pool.runtime_for_active_write_turn(authority_b, running_b.turn_id)
    assert runtime_a.binding.workspace_id == database_a.workspace_id
    assert runtime_b.binding.workspace_id == database_b.workspace_id
    assert runtime_a.session_id != runtime_b.session_id

    with pytest.raises(
        WorkspaceExecutionContractError,
        match="does not own",
    ):
        pool.runtime_for_active_write_turn(authority_a, queued_a.turn_id)
    with pytest.raises(
        WorkspaceExecutionContractError,
        match="does not own",
    ):
        pool.runtime_for_active_write_turn(authority_b, queued_b.turn_id)


def test_cached_scope_runtime_revalidates_write_lane_before_handoff(
    tmp_path: Path,
) -> None:
    database, running, _ = initialized_running_turn(tmp_path, "ws_stale_grant")
    probe = ConcurrencyProbe()
    pool = WorkspaceExecutionPool(lambda working_directory: ProbeRuntime(working_directory, probe))
    runtime = pool.runtime_for_active_write_turn(
        SqliteExecutionScopeAuthority(database), running.turn_id
    )

    connection = database.open(writable=True)
    try:
        connection.execute(
            """
            UPDATE workspace_write_lane
            SET active_write_turn_id = NULL, lane_revision = lane_revision + 1
            WHERE singleton_id = 1
            """
        )
        connection.commit()
    finally:
        connection.close()

    async def scenario() -> None:
        with pytest.raises(
            WorkspaceExecutionContractError,
            match="no longer owns",
        ):
            await runtime.execute(
                session_id=runtime.session_id,
                code="must-not-run",
                timeout_seconds=1,
            )
        await pool.close()

    asyncio.run(scenario())
    assert probe.started == 0


def test_scope_bound_stata_executor_injects_authoritative_session_identity() -> None:
    service = AsyncMock()
    service.execute.return_value = StataOperationOutcome(
        OperationId("op_scope_bound"),
        OperationAttemptId("attempt_scope_bound"),
        "completed",
        StataExecutionStatus.SUCCEEDED,
        CompletionManifestId("manifest_scope_bound"),
        WorkspaceRevision(1),
        False,
    )
    executor = StataToolExecutor(
        service,
        UuidIdentityGenerator(),
        authoritative_session_id="stata_authoritative_scope",
    )
    request = ToolExecutionRequest(
        TurnId("turn_scope_bound"),
        ToolCallId("toolcall_scope_bound"),
        OperationId("op_scope_bound"),
        "stata.run",
        {"code": "describe"},
    )

    result = asyncio.run(executor.execute(request))
    assert result.success is True
    assert service.execute.await_args.args[0].session_id == "stata_authoritative_scope"

    forged = ToolExecutionRequest(
        request.turn_id,
        request.tool_call_id,
        request.operation_id,
        request.tool_name,
        {"code": "describe", "session_id": "stata_other_workspace"},
    )
    with pytest.raises(ValueError, match="authoritative Execution Scope"):
        asyncio.run(executor.execute(forged))
    assert service.execute.await_count == 1
