"""Replay committed Outbox rows as small, at-least-once durable notifications."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from stata_research_agent.application.streaming import (
    DurableNotification,
    DurableReplayBatch,
    StreamResourceRef,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

from .control_query import decode_workspace_stream_cursor, encode_workspace_stream_cursor


class StreamResyncRequiredError(ValueError):
    """The requested delivery boundary cannot be proven continuous."""


_RESOURCE_KEYS = {
    "turn_id": "turn",
    "conversation_id": "conversation",
    "message_id": "message",
    "result_id": "result",
    "stata_run_id": "stata_run",
    "run_id": "stata_run",
    "document_revision_id": "document_revision",
    "artifact_id": "artifact",
    "waiting_request_id": "waiting_request",
    "research_path_id": "research_path",
    "operation_id": "operation",
}


class SqliteOutboxStreamQuery:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def replay_after(self, cursor: str, *, limit: int = 100) -> DurableReplayBatch:
        if not 1 <= limit <= 500:
            raise ValueError("Durable replay limit must be between 1 and 500")
        try:
            boundary_revision, boundary_ordinal, boundary_id = decode_workspace_stream_cursor(
                cursor
            )
        except ValueError as error:
            raise StreamResyncRequiredError("Stream cursor is invalid") from error

        self._connection.execute("BEGIN")
        try:
            workspace_row = self._connection.execute(
                "SELECT workspace_id FROM workspace_identity WHERE singleton_id = 1"
            ).fetchone()
            if workspace_row is None:
                raise RuntimeError("Workspace identity is not initialized")
            workspace_id = str(workspace_row["workspace_id"])
            authoritative_revision = int(
                self._connection.execute(
                    "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
                ).fetchone()[0]
            )
            if (boundary_revision, boundary_ordinal, boundary_id) != (0, 0, "none"):
                anchor = self._connection.execute(
                    """
                    SELECT 1 FROM outbox_entries
                    WHERE workspace_revision = ? AND ordinal = ? AND outbox_entry_id = ?
                    """,
                    (boundary_revision, boundary_ordinal, boundary_id),
                ).fetchone()
                if anchor is None:
                    raise StreamResyncRequiredError(
                        "Stream cursor does not identify a retained Outbox boundary"
                    )
            rows = self._connection.execute(
                """
                SELECT outbox_entry_id, workspace_revision, ordinal, topic, payload_json
                FROM outbox_entries
                WHERE workspace_revision > ?
                   OR (workspace_revision = ? AND ordinal > ?)
                ORDER BY workspace_revision, ordinal
                LIMIT ?
                """,
                (boundary_revision, boundary_revision, boundary_ordinal, limit),
            ).fetchall()
            notifications = tuple(self._notification(workspace_id, row) for row in rows)
            current_row = self._connection.execute(
                """
                SELECT workspace_revision, ordinal, outbox_entry_id
                FROM outbox_entries
                ORDER BY workspace_revision DESC, ordinal DESC LIMIT 1
                """
            ).fetchone()
            current_cursor = (
                encode_workspace_stream_cursor(0, 0, "none")
                if current_row is None
                else encode_workspace_stream_cursor(
                    int(current_row["workspace_revision"]),
                    int(current_row["ordinal"]),
                    str(current_row["outbox_entry_id"]),
                )
            )
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        return DurableReplayBatch(
            workspace_id=workspace_id,
            after_cursor=cursor,
            current_cursor=current_cursor,
            authoritative_revision=WorkspaceRevision(authoritative_revision),
            notifications=notifications,
        )

    @staticmethod
    def _notification(workspace_id: str, row: sqlite3.Row) -> DurableNotification:
        payload_json = str(row["payload_json"])
        payload: dict[str, Any] = json.loads(payload_json)
        refs = tuple(
            StreamResourceRef(_RESOURCE_KEYS[key], str(value))
            for key, value in sorted(payload.items())
            if key in _RESOURCE_KEYS and isinstance(value, str)
        )
        cursor = encode_workspace_stream_cursor(
            int(row["workspace_revision"]),
            int(row["ordinal"]),
            str(row["outbox_entry_id"]),
        )
        return DurableNotification(
            event_id=str(row["outbox_entry_id"]),
            stream_cursor=cursor,
            workspace_id=workspace_id,
            workspace_revision=WorkspaceRevision(int(row["workspace_revision"])),
            event_type=str(row["topic"]),
            resource_refs=refs,
            summary_payload_json=payload_json,
        )
