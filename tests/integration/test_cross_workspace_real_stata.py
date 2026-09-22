"""Windows/Stata profile proof for isolated parallel Workspace execution scopes."""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import pytest

from stata_research_agent.application.control import (
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.stata_operation import (
    ArtifactOutputExpectation,
    ExecuteStataCommand,
)
from stata_research_agent.application.stata_operation_service import StataOperationService
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.artifacts.completion_manifest_store import (
    FilesystemCompletionManifestStore,
)
from stata_research_agent.artifacts.managed_store import FilesystemManagedArtifactStore
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.domain.stata_execution import StataExecutionStatus
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.execution_scope_query import (
    SqliteExecutionScopeAuthority,
)
from stata_research_agent.persistence.stata_operation_store import (
    SqliteStataOperationRepository,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator
from stata_research_agent.runtime.workspace_execution import WorkspaceExecutionPool
from stata_research_agent.stata.stdio_runtime import StdioStataRuntime

MCP_ROOT = Path.home() / "stata-mcp"
MCP_PYTHON = MCP_ROOT / ".venv-uv" / "Scripts" / "python.exe"
STATA_HOME = Path(r"C:\Program Files\Stata18")
AUTO_DATA = STATA_HOME / "auto.dta"


def running_workspace(root: Path, name: str):
    database = WorkspaceDatabase(root / name, WorkspaceId(name))
    database.create()
    connection = database.open(writable=True)
    try:
        control = WorkspaceControlService(SqliteControlStore(connection), UuidIdentityGenerator())
        control.create_workspace(
            CreateWorkspaceCommand(CommandId(f"cmd_create_{name}"), WorkspaceId(name))
        )
        turn = control.submit_message(
            SubmitMessageCommand(CommandId(f"cmd_turn_{name}"), "Inspect auto.dta")
        )
    finally:
        connection.close()
    return database, turn.turn_id


def test_two_workspaces_can_hold_isolated_real_stata_sessions_and_workdirs(
    tmp_path: Path,
) -> None:
    python_executable = MCP_PYTHON
    if not (python_executable.is_file() and AUTO_DATA.is_file()):
        pytest.skip("certified local Stata MCP environment is not installed")

    database_a, turn_a = running_workspace(tmp_path, "ws_real_parallel_a")
    database_b, turn_b = running_workspace(tmp_path, "ws_real_parallel_b")

    def factory(working_directory: Path) -> StdioStataRuntime:
        return StdioStataRuntime(
            python_executable=python_executable,
            mcp_source_root=MCP_ROOT / "src",
            working_directory=working_directory,
            stata_home=STATA_HOME,
        )

    pool = WorkspaceExecutionPool(factory)
    runtime_a = pool.runtime_for_active_write_turn(
        SqliteExecutionScopeAuthority(database_a), turn_a
    )
    runtime_b = pool.runtime_for_active_write_turn(
        SqliteExecutionScopeAuthority(database_b), turn_b
    )
    connection_a = database_a.open(writable=True)
    connection_b = database_b.open(writable=True)
    service_a = StataOperationService(
        SqliteStataOperationRepository(connection_a),
        runtime_a,
        UuidIdentityGenerator(),
        FilesystemCompletionManifestStore(
            database_a.root, execution_root=runtime_a.working_directory
        ),
        FilesystemManagedArtifactStore(database_a.root, execution_root=runtime_a.working_directory),
    )
    service_b = StataOperationService(
        SqliteStataOperationRepository(connection_b),
        runtime_b,
        UuidIdentityGenerator(),
        FilesystemCompletionManifestStore(
            database_b.root, execution_root=runtime_b.working_directory
        ),
        FilesystemManagedArtifactStore(database_b.root, execution_root=runtime_b.working_directory),
    )
    for runtime in (runtime_a, runtime_b):
        runtime.working_directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(AUTO_DATA, runtime.working_directory / "auto.dta")

    async def scenario():
        await runtime_a.prepare()
        await runtime_b.prepare()
        try:
            pwd_a, pwd_b = await asyncio.gather(
                runtime_a.execute(
                    session_id=runtime_a.session_id,
                    code='file open __scope using "scope-a.txt", write replace\n'
                    'file write __scope "A"\nfile close __scope\npwd',
                    timeout_seconds=20,
                ),
                runtime_b.execute(
                    session_id=runtime_b.session_id,
                    code='file open __scope using "scope-b.txt", write replace\n'
                    'file write __scope "B"\nfile close __scope\npwd',
                    timeout_seconds=20,
                ),
            )
            assert (runtime_a.working_directory / "scope-a.txt").is_file()
            assert (runtime_b.working_directory / "scope-b.txt").is_file()
            assert not (runtime_a.working_directory / "scope-b.txt").exists()
            assert not (runtime_b.working_directory / "scope-a.txt").exists()

            # Prove actual Stata overlap, not merely two live session identities.  Two separate
            # two-second sleeps must complete below the serialized four-second floor.
            started = time.perf_counter()
            slept_a, slept_b = await asyncio.gather(
                runtime_a.execute(
                    session_id=runtime_a.session_id,
                    code="sleep 2000",
                    timeout_seconds=20,
                ),
                runtime_b.execute(
                    session_id=runtime_b.session_id,
                    code="sleep 2000",
                    timeout_seconds=20,
                ),
            )
            elapsed = time.perf_counter() - started
            assert elapsed < 3.5
            assert slept_a.receipt.execution_status is StataExecutionStatus.SUCCEEDED
            assert slept_b.receipt.execution_status is StataExecutionStatus.SUCCEEDED

            # Repeated real regressions verify that both engines remain usable after overlap and
            # that each result is attributed to the authoritative scope session.
            regression_a, regression_b = await asyncio.gather(
                runtime_a.execute(
                    session_id=runtime_a.session_id,
                    code='use "auto.dta", clear\nregress mpg weight',
                    timeout_seconds=30,
                ),
                runtime_b.execute(
                    session_id=runtime_b.session_id,
                    code='use "auto.dta", clear\nregress price length',
                    timeout_seconds=30,
                ),
            )
            assert regression_a.structured is not None
            assert regression_b.structured is not None
            assert regression_a.receipt.session_id == runtime_a.session_id
            assert regression_b.receipt.session_id == runtime_b.session_id
            assert (
                regression_a.receipt.supervision_proof["worker_pid"]
                != regression_b.receipt.supervision_proof["worker_pid"]
            )

            for _ in range(10):
                repeated_a, repeated_b = await asyncio.gather(
                    runtime_a.execute(
                        session_id=runtime_a.session_id,
                        code="regress mpg weight",
                        timeout_seconds=30,
                    ),
                    runtime_b.execute(
                        session_id=runtime_b.session_id,
                        code="regress price length",
                        timeout_seconds=30,
                    ),
                )
                assert repeated_a.structured is not None
                assert repeated_b.structured is not None

            outputs = (
                ArtifactOutputExpectation(
                    "table.model",
                    "tables/model.rtf",
                    "table",
                    "application/rtf",
                ),
                ArtifactOutputExpectation(
                    "data.analysis",
                    "data/analysis.dta",
                    "dataset",
                    "application/x-stata-dta",
                ),
                ArtifactOutputExpectation(
                    "figure.scatter",
                    "figures/scatter.png",
                    # V0.1 has not implemented the Visual Result profile yet, so the physical
                    # graph is captured as a diagnostic Artifact rather than misrepresented as
                    # a qualified Visual Result.
                    "diagnostic",
                    "image/png",
                ),
            )

            def formal_code(dependent: str, predictor: str, label: str) -> str:
                return "\n".join(
                    (
                        "sleep 2000",
                        'use "auto.dta", clear',
                        f"regress {dependent} {predictor}",
                        'esttab using "<ATTEMPT_STAGING>/tables/model.rtf", '
                        f'replace rtf title("{label}")',
                        'save "<ATTEMPT_STAGING>/data/analysis.dta", replace',
                        f"scatter {dependent} {predictor}, name(scope_graph, replace)",
                        'graph export "<ATTEMPT_STAGING>/figures/scatter.png", replace width(800)',
                    )
                )

            # Exercise the actual durable product boundary concurrently: each Workspace commits
            # its own handoff before entering Stata, then publishes a Completion Manifest and
            # finalizes three isolated Artifact candidates in its own SQLite/managed store.
            formal_started = time.perf_counter()
            formal_a, formal_b = await asyncio.gather(
                service_a.execute(
                    ExecuteStataCommand(
                        CommandId("cmd_parallel_formal_a"),
                        turn_a,
                        runtime_a.session_id,
                        formal_code("mpg", "weight", "Workspace A"),
                        30,
                        expected_outputs=outputs,
                    )
                ),
                service_b.execute(
                    ExecuteStataCommand(
                        CommandId("cmd_parallel_formal_b"),
                        turn_b,
                        runtime_b.session_id,
                        formal_code("price", "length", "Workspace B"),
                        30,
                        expected_outputs=outputs,
                    )
                ),
            )
            formal_elapsed = time.perf_counter() - formal_started
            # The earlier sleep pair is the strict overlap proof.  This wider bound covers
            # esttab, DTA save, graph rendering, file-settle observation, hashing and two SQLite
            # finalizations while still catching an accidental fully serialized slow path.
            assert formal_elapsed < 12.0
            assert formal_a.status == "completed"
            assert formal_b.status == "completed"
            assert formal_a.manifest_id is not None
            assert formal_b.manifest_id is not None

            for connection, outcome, expected_session, workspace_root in (
                (connection_a, formal_a, runtime_a.session_id, database_a.root),
                (connection_b, formal_b, runtime_b.session_id, database_b.root),
            ):
                manifest = connection.execute(
                    """
                    SELECT session_id, execution_status, structured_result_json
                    FROM completion_manifests
                    WHERE completion_manifest_id = ?
                    """,
                    (outcome.manifest_id.value,),
                ).fetchone()
                assert manifest["session_id"] == expected_session
                assert manifest["execution_status"] == "succeeded"
                assert '"N":74.0' in manifest["structured_result_json"]
                locations = connection.execute(
                    """
                    SELECT al.managed_handle
                    FROM completion_manifest_artifacts AS cma
                    JOIN artifact_candidate_sources AS acs
                      ON acs.artifact_candidate_id = cma.artifact_candidate_id
                    JOIN artifact_locations AS al ON al.artifact_id = acs.artifact_id
                    WHERE cma.completion_manifest_id = ?
                    ORDER BY cma.output_slot
                    """,
                    (outcome.manifest_id.value,),
                ).fetchall()
                assert len(locations) == 3
                for location in locations:
                    managed_handle = Path(location["managed_handle"])
                    payload = (
                        managed_handle.resolve()
                        if managed_handle.is_absolute()
                        else (workspace_root / managed_handle).resolve()
                    )
                    assert payload.is_file()
                    assert payload.is_relative_to(workspace_root.resolve())

            # Kill Workspace A while a durable Operation is in flight.  Its worker must converge
            # to an uncertain terminal fact, while Workspace B completes through the same MCP
            # supervisor and remains usable.
            interrupted_a_task = asyncio.create_task(
                service_a.execute(
                    ExecuteStataCommand(
                        CommandId("cmd_parallel_abort_a"),
                        turn_a,
                        runtime_a.session_id,
                        "sleep 10000",
                        20,
                    )
                )
            )
            surviving_b_task = asyncio.create_task(
                service_b.execute(
                    ExecuteStataCommand(
                        CommandId("cmd_parallel_survive_b"),
                        turn_b,
                        runtime_b.session_id,
                        'sleep 1500\ndisplay "@@B_OPERATION_SURVIVED"',
                        20,
                    )
                )
            )
            for _ in range(100):
                handoff = connection_a.execute(
                    """
                    SELECT status FROM operations
                    WHERE idempotency_key = 'cmd_parallel_abort_a'
                    """
                ).fetchone()
                if handoff is not None and handoff["status"] == "handoff_committed":
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("Workspace A handoff was not committed before abort")
            await runtime_a.abort_scope(reason="real cross-Workspace failure injection")
            interrupted_a, surviving_b = await asyncio.gather(interrupted_a_task, surviving_b_task)
            assert interrupted_a.status == "outcome_unknown"
            assert interrupted_a.execution_status is StataExecutionStatus.CRASHED
            assert surviving_b.status == "completed"
            assert surviving_b.execution_status is StataExecutionStatus.SUCCEEDED

            healthy_b = await runtime_b.execute(
                session_id=runtime_b.session_id,
                code='display "@@B_STILL_HEALTHY"',
                timeout_seconds=20,
            )
            assert "@@B_STILL_HEALTHY" in healthy_b.text
            return pwd_a, pwd_b, slept_a, slept_b, regression_a, regression_b, healthy_b
        finally:
            await runtime_a.close_scope()
            await runtime_b.close_scope()
            await pool.close()

    try:
        (
            completed_a,
            completed_b,
            slept_a,
            slept_b,
            regression_a,
            regression_b,
            healthy_b,
        ) = asyncio.run(scenario())
        assert completed_a.receipt.execution_status is StataExecutionStatus.SUCCEEDED
        assert completed_b.receipt.execution_status is StataExecutionStatus.SUCCEEDED
        assert slept_a.receipt.execution_status is StataExecutionStatus.SUCCEEDED
        assert slept_b.receipt.execution_status is StataExecutionStatus.SUCCEEDED
        assert regression_a.receipt.execution_status is StataExecutionStatus.SUCCEEDED
        assert regression_b.receipt.execution_status is StataExecutionStatus.SUCCEEDED
        assert healthy_b.receipt.execution_status is StataExecutionStatus.SUCCEEDED
        # One MCP supervisor owns both sessions; isolation identity lives in the distinct
        # session/generation and worker PID, not in duplicate supervisor processes.
        assert completed_a.receipt.executor_instance_id == completed_b.receipt.executor_instance_id
        assert completed_a.receipt.session_id != completed_b.receipt.session_id
        assert completed_a.receipt.session_id == runtime_a.session_id
        assert completed_b.receipt.session_id == runtime_b.session_id
    finally:
        connection_a.close()
        connection_b.close()
