from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from stata_research_agent.application.control import (
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.recovery import RecoverOperationCommand
from stata_research_agent.application.recovery_service import RecoveryService
from stata_research_agent.application.stata_operation import (
    ArtifactOutputExpectation,
    ExecuteStataCommand,
)
from stata_research_agent.application.stata_operation_service import StataOperationService
from stata_research_agent.application.turn_interaction import ContinuePausedTurnCommand
from stata_research_agent.application.turn_interaction_service import TurnInteractionService
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.artifacts.completion_manifest_store import (
    FilesystemCompletionManifestStore,
)
from stata_research_agent.artifacts.managed_store import FilesystemManagedArtifactStore
from stata_research_agent.domain.identifiers import CommandId, OperationId, WorkspaceId
from stata_research_agent.domain.stata_execution import (
    StataArtifactOutput,
    StataArtifactOutputRequest,
    StataExecutionReceipt,
    StataExecutionStatus,
    StataRuntimeResult,
    StataSessionCloseResult,
)
from stata_research_agent.domain.status import RecoveryClassification, TurnRelationKind
from stata_research_agent.persistence.atomic_commit import CommitCrashPoint
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.recovery_store import SqliteRecoveryRepository
from stata_research_agent.persistence.stata_operation_store import (
    SqliteStataOperationRepository,
)
from stata_research_agent.persistence.stata_recovery_scanner import (
    SqliteStataRecoveryScanner,
)
from stata_research_agent.persistence.turn_interaction_store import (
    SqliteTurnInteractionRepository,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


def runtime_result(status: StataExecutionStatus) -> StataRuntimeResult:
    return StataRuntimeResult(
        envelope_schema_version="stata-mcp.envelope/v1",
        text="Stata output",
        structured={"N": 74.0} if status is StataExecutionStatus.SUCCEEDED else None,
        receipt=StataExecutionReceipt(
            schema_version="stata.execution-receipt/v1alpha1",
            executor_instance_id="executor-test",
            session_id="scope-main",
            session_generation=3,
            exec_seq=2,
            execution_status=status,
            rc=0 if status is StataExecutionStatus.SUCCEEDED else 1,
            raw_output_status="complete",
            structured_result_status=(
                "complete" if status is StataExecutionStatus.SUCCEEDED else "not_applicable"
            ),
            command_hash="a" * 16,
            data_signature="74:12:test",
            session_reset=status
            in {StataExecutionStatus.CRASHED, StataExecutionStatus.START_FAILED},
            runtime_environment={"stata_version": "18"},
            supervision_proof={"windows_job_object_attached": True},
        ),
        is_error=status is not StataExecutionStatus.SUCCEEDED,
    )


class FakeRuntime:
    def __init__(self, result=None, error: BaseException | None = None) -> None:
        self.result = result
        self.error = error
        self.execute_count = 0
        self.closed: list[str] = []

    async def execute(
        self,
        *,
        session_id: str,
        code: str,
        timeout_seconds: float,
        operation_attempt_id: str | None = None,
        artifact_outputs: tuple[StataArtifactOutputRequest, ...] = (),
    ):
        del artifact_outputs
        self.execute_count += 1
        if self.error is not None:
            raise self.error
        return self.result

    async def close_session(self, *, session_id: str, reason: str):
        self.closed.append(session_id)
        return StataSessionCloseResult(
            "stata.session-control/v1alpha1",
            "executor-test",
            session_id,
            True,
            {"closed": True},
        )


class ProducingRuntime(FakeRuntime):
    def __init__(self, workspace_root: Path) -> None:
        super().__init__(runtime_result(StataExecutionStatus.SUCCEEDED))
        self.workspace_root = workspace_root

    async def execute(
        self,
        *,
        session_id: str,
        code: str,
        timeout_seconds: float,
        operation_attempt_id: str | None = None,
        artifact_outputs: tuple[StataArtifactOutputRequest, ...] = (),
    ):
        del artifact_outputs
        assert operation_attempt_id is not None
        self.execute_count += 1
        staging = self.workspace_root / ".stata-agent" / "staging" / operation_attempt_id / "tables"
        staging.mkdir(parents=True, exist_ok=True)
        output = staging / "baseline.rtf"
        output.write_bytes(b"{\\rtf1 baseline regression}")
        return replace(
            self.result,
            artifacts=(
                StataArtifactOutput(
                    output_slot="table.baseline",
                    source_path=str(output),
                    relative_staging_path="tables/baseline.rtf",
                    artifact_kind="table",
                    media_type="application/rtf",
                    producer_locator="stata:esttab",
                ),
            ),
        )


def initialized(tmp_path: Path, runtime: FakeRuntime):
    database = WorkspaceDatabase(tmp_path / "workspace", WorkspaceId("ws_stata_operation"))
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_stata_workspace"), WorkspaceId("ws_stata_operation"))
    )
    turn = control.submit_message(
        SubmitMessageCommand(CommandId("cmd_stata_turn"), "Run the baseline regression")
    )
    service = StataOperationService(
        SqliteStataOperationRepository(connection),
        runtime,
        identities,
        FilesystemCompletionManifestStore(tmp_path / "workspace"),
    )
    command = ExecuteStataCommand(
        CommandId("cmd_stata_execute"),
        turn.turn_id,
        "scope-main",
        "regress price mpg weight",
        30,
    )
    return connection, service, command


def test_success_commits_handoff_manifest_and_terminal_state_once(tmp_path: Path) -> None:
    runtime = FakeRuntime(runtime_result(StataExecutionStatus.SUCCEEDED))
    connection, service, command = initialized(tmp_path, runtime)
    try:
        first = asyncio.run(service.execute(command))
        replay = asyncio.run(service.execute(command))
        assert first.status == "completed"
        assert first.manifest_id is not None
        assert replay.replayed is True
        assert replay.operation_id == first.operation_id
        assert runtime.execute_count == 1
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM completion_manifests").fetchone()[0] == 1
        events = [
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM journal_entries ORDER BY workspace_revision, ordinal"
            )
        ]
        assert "tool.handoff_committed" in events
        assert "tool.completed" in events
    finally:
        connection.close()


def test_stata_license_banner_is_sanitized_before_completion_manifest(tmp_path: Path) -> None:
    license_identity = "researcher-license-canary@example.invalid"
    result = replace(
        runtime_result(StataExecutionStatus.SUCCEEDED),
        text=f"Stata 18 MP\nLicensed to: {license_identity}\nSerial: 123456789",
        receipt=replace(
            runtime_result(StataExecutionStatus.SUCCEEDED).receipt,
            runtime_environment={
                "stata_version": "18",
                "startup_banner": f"Licensed to: {license_identity}",
            },
        ),
    )
    runtime = FakeRuntime(result)
    connection, service, command = initialized(tmp_path, runtime)
    try:
        outcome = asyncio.run(service.execute(command))
        assert outcome.status == "completed"
        completion = next(
            (tmp_path / "workspace" / ".stata-agent" / "completions").glob("*/completion.json")
        ).read_text(encoding="utf-8")
        assert license_identity not in completion
        assert "[REDACTED_SENSITIVE_OUTPUT]" in completion
        assert license_identity not in "\n".join(connection.iterdump())
    finally:
        connection.close()


def test_timeout_commits_manifest_marks_unknown_and_closes_tainted_session(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime(runtime_result(StataExecutionStatus.TIMED_OUT))
    connection, service, command = initialized(tmp_path, runtime)
    try:
        outcome = asyncio.run(service.execute(command))
        assert outcome.status == "outcome_unknown"
        assert outcome.manifest_id is not None
        assert runtime.closed == ["scope-main"]
        row = connection.execute("SELECT execution_status FROM completion_manifests").fetchone()
        assert row[0] == "timed_out"
    finally:
        connection.close()


def test_supervised_crash_with_reset_and_no_outputs_is_a_durable_failure(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime(runtime_result(StataExecutionStatus.CRASHED))
    connection, service, command = initialized(tmp_path, runtime)
    try:
        outcome = asyncio.run(service.execute(command))
        assert outcome.status == "failed"
        assert outcome.execution_status is StataExecutionStatus.CRASHED
        assert outcome.manifest_id is not None
        assert runtime.closed == ["scope-main"]
        operation = connection.execute(
            "SELECT status FROM operations WHERE operation_id = ?",
            (outcome.operation_id.value,),
        ).fetchone()
        assert operation[0] == "failed"
    finally:
        connection.close()


def test_recovery_scan_verifies_committed_state_and_detects_manifest_identity_change(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime(runtime_result(StataExecutionStatus.SUCCEEDED))
    connection, service, command = initialized(tmp_path, runtime)
    completion_store = FilesystemCompletionManifestStore(tmp_path / "workspace")
    try:
        outcome = asyncio.run(service.execute(command))
        before_revision = connection.execute(
            "SELECT max(workspace_revision) FROM workspace_commits"
        ).fetchone()[0]
        committed = SqliteStataRecoveryScanner(connection, completion_store).assess(
            outcome.operation_id
        )
        assert committed.classification is RecoveryClassification.COMMITTED
        assert (
            connection.execute("SELECT max(workspace_revision) FROM workspace_commits").fetchone()[
                0
            ]
            == before_revision
        )

        manifest_path = (
            tmp_path
            / "workspace"
            / ".stata-agent"
            / "completions"
            / outcome.attempt_id.value
            / "completion.json"
        )
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw["completion_manifest_id"] = "manifest_changed"
        manifest_path.write_text(
            json.dumps(raw, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        violated = SqliteStataRecoveryScanner(connection, completion_store).assess(
            outcome.operation_id
        )
        assert violated.classification is RecoveryClassification.INTEGRITY_VIOLATION
        recovery = RecoveryService(
            SqliteStataRecoveryScanner(connection, completion_store),
            SqliteRecoveryRepository(connection),
            UuidIdentityGenerator(),
        ).recover(
            RecoverOperationCommand(CommandId("cmd_integrity_recovery"), outcome.operation_id)
        )
        assert recovery.turn_status == "paused"
        assert (
            connection.execute(
                "SELECT status FROM operations WHERE operation_id = ?",
                (outcome.operation_id.value,),
            ).fetchone()[0]
            == "integrity_violation"
        )
        assert connection.execute("SELECT count(*) FROM results").fetchone()[0] == 0
    finally:
        connection.close()


def test_transport_failure_never_reexecutes_committed_handoff(tmp_path: Path) -> None:
    runtime = FakeRuntime(error=RuntimeError("transport lost"))
    connection, service, command = initialized(tmp_path, runtime)
    try:
        first = asyncio.run(service.execute(command))
        replay = asyncio.run(service.execute(command))
        assert first.status == "outcome_unknown"
        assert replay.replayed is True
        assert runtime.execute_count == 1
        assert connection.execute("SELECT count(*) FROM completion_manifests").fetchone()[0] == 0
        assert runtime.closed == ["scope-main"]
        interrupted = connection.execute(
            """
            SELECT payload_json FROM journal_entries
            WHERE event_type = 'tool.interrupted'
            """
        ).fetchone()[0]
        assert json.loads(interrupted)["transport_error_detail"] == "transport lost"
        recovery = RecoveryService(
            SqliteStataRecoveryScanner(
                connection, FilesystemCompletionManifestStore(tmp_path / "workspace")
            ),
            SqliteRecoveryRepository(connection),
            UuidIdentityGenerator(),
        ).recover(RecoverOperationCommand(CommandId("cmd_unknown_recovery"), first.operation_id))
        assert recovery.classification is RecoveryClassification.OUTCOME_UNKNOWN
        assert recovery.turn_status == "paused"
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0

        successor = TurnInteractionService(
            SqliteTurnInteractionRepository(connection), UuidIdentityGenerator()
        ).continue_paused_turn(
            ContinuePausedTurnCommand(
                CommandId("cmd_unknown_continue"),
                command.requested_by_turn_id,
                "I inspected the unknown outcome; continue without claiming completion.",
                TurnRelationKind.RECOVERY_CONTINUATION,
            )
        )
        with pytest.raises(ValueError, match="Recovery Report and recovery successor"):
            service.reconcile(
                command,
                authorization_command_id=CommandId("cmd_unknown_reconcile"),
                authorized_by_turn_id=successor.successor_turn_id,
            )
        assert runtime.execute_count == 1
        assert connection.execute("SELECT count(*) FROM completion_manifests").fetchone()[0] == 0
    finally:
        connection.close()


def test_finalization_response_loss_replays_committed_manifest_without_reexecution(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime(runtime_result(StataExecutionStatus.SUCCEEDED))
    connection, _, command = initialized(tmp_path, runtime)

    def crash(point: CommitCrashPoint) -> None:
        if point is CommitCrashPoint.AFTER_COMMIT:
            raise RuntimeError("finalization response lost")

    identities = UuidIdentityGenerator()
    crashing = StataOperationService(
        SqliteStataOperationRepository(connection, finalization_crash_injector=crash),
        runtime,
        identities,
        FilesystemCompletionManifestStore(tmp_path / "workspace"),
    )
    try:
        try:
            asyncio.run(crashing.execute(command))
        except RuntimeError as error:
            assert str(error) == "finalization response lost"
        else:  # pragma: no cover - proves the fault injector actually fired
            raise AssertionError("expected committed-response fault")

        recovered = StataOperationService(
            SqliteStataOperationRepository(connection),
            runtime,
            identities,
            FilesystemCompletionManifestStore(tmp_path / "workspace"),
        )
        outcome = asyncio.run(recovered.execute(command))
        assert outcome.status == "completed"
        assert outcome.replayed is True
        assert runtime.execute_count == 1
        assert connection.execute("SELECT count(*) FROM completion_manifests").fetchone()[0] == 1
    finally:
        connection.close()


def test_completion_manifest_promotes_planned_output_once(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    runtime = ProducingRuntime(workspace_root)
    connection, _, base_command = initialized(tmp_path, runtime)
    identities = UuidIdentityGenerator()
    service = StataOperationService(
        SqliteStataOperationRepository(connection),
        runtime,
        identities,
        FilesystemCompletionManifestStore(workspace_root),
        FilesystemManagedArtifactStore(workspace_root),
    )
    command = replace(
        base_command,
        command_id=CommandId("cmd_stata_output"),
        expected_outputs=(
            ArtifactOutputExpectation(
                "table.baseline",
                "tables/baseline.rtf",
                "table",
                "application/rtf",
            ),
        ),
    )
    try:
        first = asyncio.run(service.execute(command))
        replay = asyncio.run(service.execute(command))
        assert first.status == "completed"
        assert replay.replayed is True
        assert runtime.execute_count == 1
        candidate = connection.execute(
            """
            SELECT c.output_slot, c.content_hash, p.artifact_id, p.managed_payload_handle
            FROM completion_manifest_artifacts AS c
            JOIN artifact_promotions AS p USING(artifact_candidate_id)
            """
        ).fetchone()
        assert candidate["output_slot"] == "table.baseline"
        assert len(candidate["content_hash"]) == 64
        assert candidate["artifact_id"].startswith("artifact_")
        assert candidate["managed_payload_handle"].startswith(".stata-agent/objects/artifact_")
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM artifact_promotions").fetchone()[0] == 1
    finally:
        connection.close()


def test_recovery_scan_classifies_published_completion_without_finalization(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    runtime = ProducingRuntime(workspace_root)
    connection, _, base_command = initialized(tmp_path, runtime)
    completion_store = FilesystemCompletionManifestStore(workspace_root)

    def crash(point: CommitCrashPoint) -> None:
        if point is CommitCrashPoint.BEFORE_COMMIT:
            raise RuntimeError("process died before Finalization commit")

    service = StataOperationService(
        SqliteStataOperationRepository(connection, finalization_crash_injector=crash),
        runtime,
        UuidIdentityGenerator(),
        completion_store,
        FilesystemManagedArtifactStore(workspace_root),
    )
    command = replace(
        base_command,
        command_id=CommandId("cmd_stata_unreconciled"),
        expected_outputs=(
            ArtifactOutputExpectation(
                "table.baseline",
                "tables/baseline.rtf",
                "table",
                "application/rtf",
            ),
        ),
    )
    try:
        with pytest.raises(RuntimeError, match="before Finalization"):
            asyncio.run(service.execute(command))
        operation_id = connection.execute(
            "SELECT operation_id FROM stata_operation_requests WHERE request_command_id = ?",
            (command.command_id.value,),
        ).fetchone()[0]
        queued = WorkspaceControlService(
            SqliteControlStore(connection), UuidIdentityGenerator()
        ).submit_message(
            SubmitMessageCommand(
                CommandId("cmd_recovery_queued_turn"),
                "Queue this work, but do not start it during recovery.",
            )
        )
        assert queued.turn_status.value == "queued"
        before_revision = connection.execute(
            "SELECT max(workspace_revision) FROM workspace_commits"
        ).fetchone()[0]
        assessment = SqliteStataRecoveryScanner(connection, completion_store).assess(operation_id)
        after_revision = connection.execute(
            "SELECT max(workspace_revision) FROM workspace_commits"
        ).fetchone()[0]
        assert assessment.classification is RecoveryClassification.COMPLETED_UNRECONCILED
        assert assessment.manifest_id is not None
        assert before_revision == after_revision
        assert connection.execute("SELECT count(*) FROM completion_manifests").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert runtime.execute_count == 1

        recovery = RecoveryService(
            SqliteStataRecoveryScanner(connection, completion_store),
            SqliteRecoveryRepository(connection),
            UuidIdentityGenerator(),
        ).recover(
            RecoverOperationCommand(CommandId("cmd_recovery_record"), OperationId(operation_id))
        )
        assert recovery.classification is RecoveryClassification.COMPLETED_UNRECONCILED
        assert recovery.turn_status == "paused"
        assert recovery.lane_released is True
        assert (
            connection.execute(
                "SELECT status FROM turns WHERE turn_id = ?", (queued.turn_id.value,)
            ).fetchone()[0]
            == "queued"
        )
        assert connection.execute("SELECT count(*) FROM recovery_reports").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        recovered_revision = connection.execute(
            "SELECT max(workspace_revision) FROM workspace_commits"
        ).fetchone()[0]
        repeated = RecoveryService(
            SqliteStataRecoveryScanner(connection, completion_store),
            SqliteRecoveryRepository(connection),
            UuidIdentityGenerator(),
        ).recover(
            RecoverOperationCommand(CommandId("cmd_recovery_repeat"), OperationId(operation_id))
        )
        assert repeated.replayed is True
        assert repeated.recovery_report_id == recovery.recovery_report_id
        assert (
            connection.execute("SELECT max(workspace_revision) FROM workspace_commits").fetchone()[
                0
            ]
            == recovered_revision
        )
        assert connection.execute("SELECT count(*) FROM recovery_reports").fetchone()[0] == 1

        replay = StataOperationService(
            SqliteStataOperationRepository(connection),
            runtime,
            UuidIdentityGenerator(),
            completion_store,
            FilesystemManagedArtifactStore(workspace_root),
        )
        observed = asyncio.run(replay.execute(command))
        assert observed.status == "completed_unreconciled"
        assert observed.replayed is True
        assert runtime.execute_count == 1

        successor = TurnInteractionService(
            SqliteTurnInteractionRepository(connection), UuidIdentityGenerator()
        ).continue_paused_turn(
            ContinuePausedTurnCommand(
                CommandId("cmd_reconciliation_turn"),
                command.requested_by_turn_id,
                "核对刚才已完成但尚未登记的 Stata 输出，然后继续。",
                TurnRelationKind.RECOVERY_CONTINUATION,
            )
        )
        reconciled = replay.reconcile(
            command,
            authorization_command_id=CommandId("cmd_reconciliation_authorize"),
            authorized_by_turn_id=successor.successor_turn_id,
        )
        assert reconciled.status == "completed"
        assert runtime.execute_count == 1
        assert (
            connection.execute(
                "SELECT status FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()[0]
            == "completed"
        )
        assert (
            connection.execute(
                "SELECT status FROM turns WHERE turn_id = ?",
                (command.requested_by_turn_id.value,),
            ).fetchone()[0]
            == "paused"
        )
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM operation_reconciliation_authorizations"
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()
