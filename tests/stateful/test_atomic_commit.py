"""M0-04 named crash-point, atomicity, and idempotency contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.persistence.atomic_commit import (
    AtomicCommitService,
    CommitCrashPoint,
    JournalDraft,
    OutboxDraft,
)
from stata_research_agent.persistence.errors import CommandConflictError
from stata_research_agent.persistence.workspace import WorkspaceDatabase


class InjectedCrash(RuntimeError):
    pass


def workspace_connection(tmp_path: Path) -> sqlite3.Connection:
    workspace = WorkspaceDatabase(tmp_path / "ws_atomic", WorkspaceId("ws_atomic"))
    workspace.create()
    return workspace.open(writable=True)


def sample_arguments() -> dict:
    return {
        "command_id": CommandId("cmd_sample"),
        "command_type": "test.commit",
        "request": {"z": 2, "a": 1},
        "response": {"accepted": True},
        "journal": (
            JournalDraft(
                event_type="test.committed",
                object_type="test_object",
                object_id="object_1",
                payload={"value": 1},
            ),
        ),
        "outbox": (OutboxDraft(topic="workspace.changed", payload={"kind": "test"}),),
    }


def authoritative_counts(connection: sqlite3.Connection) -> tuple[int, int, int, int]:
    return tuple(
        int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        for table in (
            "workspace_commits",
            "journal_entries",
            "outbox_entries",
            "command_receipts",
        )
    )


@pytest.mark.parametrize(
    "point",
    [
        CommitCrashPoint.BEFORE_BEGIN,
        CommitCrashPoint.AFTER_BEGIN,
        CommitCrashPoint.AFTER_WORKSPACE_COMMIT,
        CommitCrashPoint.AFTER_DOMAIN_MUTATION,
        CommitCrashPoint.AFTER_JOURNAL,
        CommitCrashPoint.AFTER_OUTBOX,
        CommitCrashPoint.AFTER_RECEIPT,
        CommitCrashPoint.BEFORE_COMMIT,
    ],
)
def test_every_precommit_crash_point_rolls_back_every_authoritative_fact(
    tmp_path: Path, point: CommitCrashPoint
) -> None:
    connection = workspace_connection(tmp_path)
    try:
        service = AtomicCommitService(connection)

        def crash(current: CommitCrashPoint) -> None:
            if current == point:
                raise InjectedCrash(point)

        with pytest.raises(InjectedCrash):
            service.commit(**sample_arguments(), crash_injector=crash)
        assert authoritative_counts(connection) == (0, 0, 0, 0)

        receipt = service.commit(**sample_arguments())
        assert receipt.commit_revision.value == 1
        assert authoritative_counts(connection) == (1, 1, 1, 1)
    finally:
        connection.close()


def test_crash_after_commit_is_recovered_by_idempotent_receipt_replay(tmp_path: Path) -> None:
    connection = workspace_connection(tmp_path)
    try:
        service = AtomicCommitService(connection)

        def crash(point: CommitCrashPoint) -> None:
            if point == CommitCrashPoint.AFTER_COMMIT:
                raise InjectedCrash(point)

        with pytest.raises(InjectedCrash):
            service.commit(**sample_arguments(), crash_injector=crash)
        assert authoritative_counts(connection) == (1, 1, 1, 1)

        replay = service.commit(**sample_arguments())
        assert replay.replayed is True
        assert replay.commit_revision.value == 1
        assert authoritative_counts(connection) == (1, 1, 1, 1)
    finally:
        connection.close()


def test_same_command_id_with_different_request_fails_closed(tmp_path: Path) -> None:
    connection = workspace_connection(tmp_path)
    try:
        service = AtomicCommitService(connection)
        service.commit(**sample_arguments())
        changed = sample_arguments()
        changed["request"] = {"a": 999}
        with pytest.raises(CommandConflictError):
            service.commit(**changed)
        assert authoritative_counts(connection) == (1, 1, 1, 1)
    finally:
        connection.close()


def test_commit_requires_journal_but_allows_zero_to_many_outbox_facts(tmp_path: Path) -> None:
    connection = workspace_connection(tmp_path)
    try:
        service = AtomicCommitService(connection)
        missing_journal = sample_arguments()
        missing_journal["journal"] = ()
        with pytest.raises(ValueError, match="Journal"):
            service.commit(**missing_journal)

        no_outbox = sample_arguments()
        no_outbox["outbox"] = ()
        receipt = service.commit(**no_outbox)
        assert receipt.commit_revision.value == 1
        assert authoritative_counts(connection) == (1, 1, 0, 1)
    finally:
        connection.close()


def test_database_rejects_orphan_workspace_commit(tmp_path: Path) -> None:
    connection = workspace_connection(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                """
                INSERT INTO workspace_commits(workspace_revision, command_id, committed_at)
                VALUES (1, 'cmd_orphan', 'now')
                """
            )
    finally:
        connection.close()


def test_commits_allocate_gap_free_revision_and_stable_ordinals(tmp_path: Path) -> None:
    connection = workspace_connection(tmp_path)
    try:
        service = AtomicCommitService(connection)
        first = sample_arguments()
        first["journal"] = (
            JournalDraft("first", "test", "one", {}),
            JournalDraft("second", "test", "two", {}),
        )
        first_receipt = service.commit(**first)

        second = sample_arguments()
        second["command_id"] = CommandId("cmd_second")
        second_receipt = service.commit(**second)
        assert (first_receipt.commit_revision.value, second_receipt.commit_revision.value) == (1, 2)

        rows = connection.execute(
            "SELECT workspace_revision, ordinal, event_type "
            "FROM journal_entries ORDER BY workspace_revision, ordinal"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (1, 1, "first"),
            (1, 2, "second"),
            (2, 1, "test.committed"),
        ]
    finally:
        connection.close()


@pytest.mark.parametrize(
    "table",
    ["workspace_commits", "command_receipts", "journal_entries", "outbox_entries"],
)
def test_authoritative_history_tables_reject_update_and_delete(tmp_path: Path, table: str) -> None:
    connection = workspace_connection(tmp_path)
    try:
        AtomicCommitService(connection).commit(**sample_arguments())
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(f"DELETE FROM {table}")
    finally:
        connection.close()
