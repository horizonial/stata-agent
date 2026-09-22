"""M0-05 vertical: typed command -> domain control state -> atomic facts -> query."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stata_research_agent.application.control import (
    ActivateNextQueuedTurnCommand,
    CompleteTurnCommand,
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.domain.status import ExecutionMode, TurnStatus
from stata_research_agent.persistence.control_query import SqliteWorkspaceQuery
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.errors import CommandConflictError
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


def initialized_vertical(tmp_path: Path):
    workspace_id = WorkspaceId("ws_vertical")
    database = WorkspaceDatabase(tmp_path / "workspace", workspace_id)
    database.create()
    connection = database.open(writable=True)
    service = WorkspaceControlService(SqliteControlStore(connection), UuidIdentityGenerator())
    result = service.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_initialize"), workspace_id)
    )
    return connection, service, result


def test_workspace_initialize_is_idempotent_and_creates_one_main_path_and_scope(
    tmp_path: Path,
) -> None:
    connection, service, first = initialized_vertical(tmp_path)
    try:
        replay = service.create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_initialize"), WorkspaceId("ws_vertical"))
        )
        assert replay.replayed is True
        assert replay.main_path_id == first.main_path_id
        assert replay.main_scope_id == first.main_scope_id
        assert connection.execute("SELECT count(*) FROM research_paths").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM execution_scopes").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM workspace_commits").fetchone()[0] == 1
    finally:
        connection.close()


def test_write_turn_acquires_lane_second_write_queues_and_read_can_run(tmp_path: Path) -> None:
    connection, service, _ = initialized_vertical(tmp_path)
    try:
        first = service.submit_message(
            SubmitMessageCommand(CommandId("cmd_message_1"), "Run the first analysis")
        )
        second = service.submit_message(
            SubmitMessageCommand(CommandId("cmd_message_2"), "Queue another analysis")
        )
        read = service.submit_message(
            SubmitMessageCommand(
                CommandId("cmd_message_read"),
                "Inspect current state",
                execution_mode=ExecutionMode.READ,
            )
        )

        assert first.turn_status is TurnStatus.RUNNING
        assert second.turn_status is TurnStatus.QUEUED
        assert read.turn_status is TurnStatus.RUNNING
        lane_owner = connection.execute(
            "SELECT active_write_turn_id FROM workspace_write_lane WHERE singleton_id = 1"
        ).fetchone()[0]
        assert lane_owner == first.turn_id.value
        assert connection.execute("SELECT count(*) FROM conversations").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM completion_contracts").fetchone()[0] == 3
    finally:
        connection.close()


def test_existing_conversation_accepts_another_message_without_owning_research_facts(
    tmp_path: Path,
) -> None:
    connection, service, _ = initialized_vertical(tmp_path)
    try:
        first = service.submit_message(
            SubmitMessageCommand(CommandId("cmd_first"), "First message")
        )
        second = service.submit_message(
            SubmitMessageCommand(
                CommandId("cmd_second"),
                "Second message",
                conversation_id=first.conversation_id,
            )
        )
        assert second.conversation_id == first.conversation_id
        ordinals = connection.execute(
            "SELECT ordinal FROM messages WHERE conversation_id = ? ORDER BY ordinal",
            (first.conversation_id.value,),
        ).fetchall()
        assert [row[0] for row in ordinals] == [1, 2]
    finally:
        connection.close()


def test_message_submit_retry_returns_original_ids_without_duplicate_facts(tmp_path: Path) -> None:
    connection, service, _ = initialized_vertical(tmp_path)
    try:
        command = SubmitMessageCommand(CommandId("cmd_retry_message"), "Stable request")
        first = service.submit_message(command)
        replay = service.submit_message(command)
        assert replay.replayed is True
        assert replay.conversation_id == first.conversation_id
        assert replay.message_id == first.message_id
        assert replay.turn_id == first.turn_id
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM turns").fetchone()[0] == 1
    finally:
        connection.close()


def test_complete_write_turn_atomically_releases_lane_without_starting_queued_turn(
    tmp_path: Path,
) -> None:
    connection, service, _ = initialized_vertical(tmp_path)
    try:
        running = service.submit_message(SubmitMessageCommand(CommandId("cmd_running"), "Running"))
        queued = service.submit_message(SubmitMessageCommand(CommandId("cmd_queued"), "Queued"))
        completed = service.complete_turn(
            CompleteTurnCommand(CommandId("cmd_complete"), running.turn_id, TurnStatus.SUCCEEDED)
        )
        assert completed.terminal_status is TurnStatus.SUCCEEDED
        assert (
            connection.execute("SELECT active_write_turn_id FROM workspace_write_lane").fetchone()[
                0
            ]
            is None
        )
        queued_status = connection.execute(
            "SELECT status FROM turns WHERE turn_id = ?", (queued.turn_id.value,)
        ).fetchone()[0]
        assert queued_status == TurnStatus.QUEUED.value
    finally:
        connection.close()


def test_scheduler_atomically_activates_oldest_queued_turn_after_lane_release(
    tmp_path: Path,
) -> None:
    connection, service, _ = initialized_vertical(tmp_path)
    try:
        running = service.submit_message(
            SubmitMessageCommand(CommandId("cmd_activate_running"), "Running")
        )
        first_queued = service.submit_message(
            SubmitMessageCommand(CommandId("cmd_activate_first"), "First queued")
        )
        second_queued = service.submit_message(
            SubmitMessageCommand(CommandId("cmd_activate_second"), "Second queued")
        )
        service.complete_turn(
            CompleteTurnCommand(
                CommandId("cmd_activate_complete"), running.turn_id, TurnStatus.SUCCEEDED
            )
        )

        activated = service.activate_next_queued_turn(
            ActivateNextQueuedTurnCommand(CommandId("cmd_activate_next"))
        )

        assert activated.turn_id == first_queued.turn_id
        assert activated.turn_revision == 2
        assert (
            connection.execute("SELECT active_write_turn_id FROM workspace_write_lane").fetchone()[
                0
            ]
            == first_queued.turn_id.value
        )
        statuses = connection.execute(
            "SELECT turn_id, status FROM turns ORDER BY enqueue_ordinal"
        ).fetchall()
        assert [(row["turn_id"], row["status"]) for row in statuses] == [
            (running.turn_id.value, "succeeded"),
            (first_queued.turn_id.value, "running"),
            (second_queued.turn_id.value, "queued"),
        ]
    finally:
        connection.close()


def test_same_message_command_id_with_changed_content_is_rejected(tmp_path: Path) -> None:
    connection, service, _ = initialized_vertical(tmp_path)
    try:
        service.submit_message(SubmitMessageCommand(CommandId("cmd_collision"), "Original"))
        with pytest.raises(CommandConflictError):
            service.submit_message(SubmitMessageCommand(CommandId("cmd_collision"), "Changed"))
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    finally:
        connection.close()


def test_database_rejects_lane_owner_that_is_not_running_write_turn(tmp_path: Path) -> None:
    connection, service, _ = initialized_vertical(tmp_path)
    try:
        read = service.submit_message(
            SubmitMessageCommand(CommandId("cmd_read"), "Read", execution_mode=ExecutionMode.READ)
        )
        with pytest.raises(sqlite3.IntegrityError, match="invalid workspace write-lane owner"):
            connection.execute(
                "UPDATE workspace_write_lane SET active_write_turn_id = ? WHERE singleton_id = 1",
                (read.turn_id.value,),
            )
    finally:
        connection.close()


def test_typed_query_reads_one_consistent_authoritative_snapshot(tmp_path: Path) -> None:
    connection, service, initialized = initialized_vertical(tmp_path)
    try:
        running = service.submit_message(
            SubmitMessageCommand(CommandId("cmd_snapshot_running"), "Running")
        )
        queued = service.submit_message(
            SubmitMessageCommand(CommandId("cmd_snapshot_queued"), "Queued")
        )
        read_connection = WorkspaceDatabase(
            tmp_path / "workspace", WorkspaceId("ws_vertical")
        ).open(writable=False)
        try:
            snapshot = SqliteWorkspaceQuery(read_connection).execution_snapshot()
            assert snapshot.authoritative_revision.value == initialized.commit_revision.value + 2
            assert snapshot.active_write_turn_id == running.turn_id
            assert [turn.turn_id for turn in snapshot.turns] == [running.turn_id, queued.turn_id]
            assert [turn.status for turn in snapshot.turns] == [
                TurnStatus.RUNNING,
                TurnStatus.QUEUED,
            ]
        finally:
            read_connection.close()
    finally:
        connection.close()
