"""D-230/C-018 backup-first Workspace migration and adoption invariants."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from stata_research_agent.application.control import (
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.release_activation import IrreversibleCapability
from stata_research_agent.application.workspace_migration import (
    WorkspaceMigrationError,
    WorkspaceMigrationState,
)
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.migrations import MIGRATIONS, Migration, MigrationRunner
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.persistence.workspace_migration import (
    WorkspaceMigrationCoordinator,
)
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class RecordingGuard:
    def __init__(self) -> None:
        self.calls: list[tuple[IrreversibleCapability, str]] = []

    def before(
        self,
        capability: IrreversibleCapability,
        *,
        reference: str,
    ) -> None:
        self.calls.append((capability, reference))


def initialized_workspace(tmp_path: Path) -> WorkspaceDatabase:
    workspace_id = WorkspaceId("ws_migration_test")
    database = WorkspaceDatabase(tmp_path / "workspace", workspace_id)
    database.create()
    connection = database.open(writable=True)
    try:
        WorkspaceControlService(
            SqliteControlStore(connection), UuidIdentityGenerator()
        ).create_workspace(CreateWorkspaceCommand(CommandId("cmd_migration_init"), workspace_id))
    finally:
        connection.close()
    assert database.schema_version() == len(MIGRATIONS) == 45
    return database


def future_runner(*statements: str) -> MigrationRunner:
    return MigrationRunner(
        (
            *MIGRATIONS,
            Migration(
                len(MIGRATIONS) + 1,
                "test_future_workspace_schema",
                statements or ("CREATE TABLE future_marker(value TEXT) STRICT",),
            ),
        )
    )


def semantic_snapshot(database: WorkspaceDatabase) -> dict[str, list[tuple[object, ...]]]:
    connection = database.connection_contract.connect(database.database_path, writable=False)
    try:
        tables = (
            "research_paths",
            "path_data_adoptions",
            "path_result_adoptions",
            "path_document_adoptions",
            "path_plan_adoptions",
        )
        return {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
    finally:
        connection.close()


def test_prepare_adopt_cleanup_is_backup_first_atomic_and_path_neutral(
    tmp_path: Path,
) -> None:
    database = initialized_workspace(tmp_path)
    before = semantic_snapshot(database)
    guard = RecordingGuard()
    coordinator = WorkspaceMigrationCoordinator(
        database,
        future_runner(),
        target_release_id="release-2",
        irreversibility_guard=guard,
    )

    prepared = coordinator.prepare()
    assert prepared.state == WorkspaceMigrationState.PREPARED
    assert database.schema_version() == len(MIGRATIONS)
    backup = json.loads(prepared.backup_manifest_json or "{}")
    candidate = json.loads(prepared.candidate_manifest_json or "{}")
    assert (database.root / backup["relative_path"]).is_file()
    assert (database.root / candidate["relative_path"]).is_file()
    assert semantic_snapshot(database) == before

    adopted = coordinator.adopt(prepared.migration_attempt_id)
    assert adopted.attempt.state == WorkspaceMigrationState.ADOPTED
    assert adopted.migration_receipt_id is not None
    assert database.schema_version() == len(MIGRATIONS) + 1
    assert semantic_snapshot(database) == before
    assert guard.calls == [
        (IrreversibleCapability.MIGRATION_ADOPTION, prepared.migration_attempt_id)
    ]

    cleaned = coordinator.cleanup(prepared.migration_attempt_id)
    assert cleaned.attempt.state == WorkspaceMigrationState.CLEANUP_COMPLETE
    assert cleaned.backup_retained
    assert not (database.root / candidate["relative_path"]).exists()
    assert (database.root / backup["relative_path"]).is_file()
    assert coordinator.cleanup(prepared.migration_attempt_id) == cleaned


def test_tampered_candidate_never_changes_source_and_requires_recovery(
    tmp_path: Path,
) -> None:
    database = initialized_workspace(tmp_path)
    coordinator = WorkspaceMigrationCoordinator(
        database,
        future_runner(),
        target_release_id="release-2",
    )
    prepared = coordinator.prepare()
    candidate = json.loads(prepared.candidate_manifest_json or "{}")
    (database.root / candidate["relative_path"]).write_bytes(b"tampered")

    with pytest.raises(WorkspaceMigrationError, match="integrity"):
        coordinator.adopt(prepared.migration_attempt_id)
    outcome = coordinator.inspect(prepared.migration_attempt_id)
    assert outcome.attempt.state == WorkspaceMigrationState.RECOVERY_REQUIRED
    assert database.schema_version() == len(MIGRATIONS)
    assert outcome.backup_retained


def test_default_migration_cannot_modify_research_identity_or_adoption(
    tmp_path: Path,
) -> None:
    database = initialized_workspace(tmp_path)
    coordinator = WorkspaceMigrationCoordinator(
        database,
        future_runner(
            "UPDATE research_paths SET canonical_key = 'silently-changed'",
            "CREATE TABLE future_marker(value TEXT) STRICT",
        ),
        target_release_id="release-2",
    )
    prepared = coordinator.prepare()
    with pytest.raises(WorkspaceMigrationError, match="research identity"):
        coordinator.adopt(prepared.migration_attempt_id)
    assert database.schema_version() == len(MIGRATIONS)
    assert coordinator.inspect(prepared.migration_attempt_id).attempt.state == (
        WorkspaceMigrationState.RECOVERY_REQUIRED
    )


def test_candidate_migration_failure_leaves_original_database_and_backup(
    tmp_path: Path,
) -> None:
    database = initialized_workspace(tmp_path)
    coordinator = WorkspaceMigrationCoordinator(
        database,
        future_runner("CREATE TABLE broken("),
        target_release_id="release-2",
    )
    with pytest.raises(sqlite3.Error):
        coordinator.prepare()
    assert database.schema_version() == len(MIGRATIONS)
    connection = database.connection_contract.connect(database.database_path, writable=False)
    try:
        row = connection.execute(
            """
            SELECT migration_attempt_id, state, backup_manifest_json
            FROM workspace_migration_attempts
            """
        ).fetchone()
    finally:
        connection.close()
    assert row["state"] == WorkspaceMigrationState.RECOVERY_REQUIRED
    backup = json.loads(str(row["backup_manifest_json"]))
    assert (database.root / backup["relative_path"]).is_file()


def test_nonterminal_write_turn_blocks_migration_before_attempt_creation(
    tmp_path: Path,
) -> None:
    database = initialized_workspace(tmp_path)
    connection = database.open(writable=True)
    try:
        WorkspaceControlService(
            SqliteControlStore(connection), UuidIdentityGenerator()
        ).submit_message(
            SubmitMessageCommand(CommandId("cmd_migration_running"), "keep researching")
        )
    finally:
        connection.close()
    coordinator = WorkspaceMigrationCoordinator(
        database,
        future_runner(),
        target_release_id="release-2",
    )
    with pytest.raises(WorkspaceMigrationError, match="nonterminal"):
        coordinator.prepare()
    connection = database.connection_contract.connect(database.database_path, writable=False)
    try:
        assert (
            connection.execute("SELECT count(*) FROM workspace_migration_attempts").fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_future_registry_cannot_reinterpret_migration_control_tables() -> None:
    with pytest.raises(ValueError, match="stable migration control"):
        Migration(
            28,
            "forbidden_control_rewrite",
            ("DROP TABLE workspace_migration_attempts",),
        )
