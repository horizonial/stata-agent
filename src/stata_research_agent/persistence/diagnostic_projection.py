"""Allowlisted Workspace projection for a non-authoritative support bundle."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from stata_research_agent.persistence.workspace import WorkspaceDatabase


class SqliteDiagnosticWorkspaceProjection:
    """Never expose Workspace payload columns to the Diagnostic Bundle."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def current_revision(self) -> int:
        return int(
            self._connection.execute(
                "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
            ).fetchone()[0]
        )

    def safe_projection(
        self,
        *,
        requested_revision: int,
        turn_id: str | None,
        operation_id: str | None,
    ) -> Mapping[str, Any]:
        current = self.current_revision()
        if requested_revision < 0 or requested_revision > current:
            raise ValueError("requested Workspace revision is unavailable")
        schema_version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(self._connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise ValueError("Workspace integrity check failed")

        counts = []
        for object_type, table, revision_column in (
            ("turn", "turns", "created_revision"),
            ("operation", "operations", "created_revision"),
            ("artifact", "artifacts", "created_revision"),
            ("result", "results", "created_revision"),
            ("evidence", "evidence_records", "created_revision"),
            ("document_revision", "document_revisions", "created_revision"),
        ):
            count = int(
                self._connection.execute(
                    f"SELECT count(*) FROM {table} WHERE {revision_column} <= ?",
                    (requested_revision,),
                ).fetchone()[0]
            )
            counts.append({"object_type": object_type, "object_count": count})

        journal_where = ["workspace_revision <= ?"]
        journal_parameters: list[object] = [requested_revision]
        if turn_id is not None:
            journal_where.append("(object_id = ? OR json_extract(payload_json, '$.turn_id') = ?)")
            journal_parameters.extend((turn_id, turn_id))
        if operation_id is not None:
            journal_where.append(
                "(object_id = ? OR json_extract(payload_json, '$.operation_id') = ?)"
            )
            journal_parameters.extend((operation_id, operation_id))
        journal_rows = self._connection.execute(
            f"""
            SELECT journal_entry_id, event_type, object_type, object_id,
                   workspace_revision, ordinal
            FROM journal_entries
            WHERE {" AND ".join(journal_where)}
            ORDER BY workspace_revision, ordinal
            """,
            tuple(journal_parameters),
        ).fetchall()

        operation_rows = self._connection.execute(
            """
            SELECT operation_id, operation_kind, created_revision, terminal_revision
            FROM operations
            WHERE created_revision <= ?
              AND (? IS NULL OR requested_by_turn_id = ?)
              AND (? IS NULL OR operation_id = ?)
            ORDER BY created_revision, operation_id
            """,
            (
                requested_revision,
                turn_id,
                turn_id,
                operation_id,
                operation_id,
            ),
        ).fetchall()
        artifacts = self._connection.execute(
            """
            SELECT artifact_id, artifact_kind, media_type, size_bytes, created_revision
            FROM artifacts WHERE created_revision <= ?
            ORDER BY created_revision, artifact_id
            """,
            (requested_revision,),
        ).fetchall()
        checkpoints = []
        for projection_name, table in (
            ("statistical_evidence", "statistical_evidence_current_states"),
            ("analysis_evidence", "analysis_evidence_current_states"),
        ):
            row = self._connection.execute(
                f"SELECT COALESCE(MAX(projection_revision), 0) FROM {table} "
                "WHERE projection_revision <= ?",
                (requested_revision,),
            ).fetchone()
            checkpoints.append(
                {
                    "object_type": "projection_checkpoint",
                    "lifecycle_state": projection_name,
                    "schema_version": int(row[0]),
                }
            )
        return {
            "schema_version": schema_version,
            "authoritative_revision": requested_revision,
            "integrity_check": integrity,
            "object_counts": counts,
            "journal_envelopes": [
                {
                    "journal_entry_id": str(row["journal_entry_id"]),
                    "event_type": str(row["event_type"]),
                    "object_type": str(row["object_type"]),
                    "object_id": str(row["object_id"]),
                    "authoritative_revision": int(row["workspace_revision"]),
                    "ordinal": int(row["ordinal"]),
                }
                for row in journal_rows
            ],
            "operations": [
                {
                    "operation_id": str(row["operation_id"]),
                    "operation_kind": str(row["operation_kind"]),
                    "created_revision": int(row["created_revision"]),
                    "terminal_revision": (
                        None
                        if row["terminal_revision"] is None
                        or int(row["terminal_revision"]) > requested_revision
                        else int(row["terminal_revision"])
                    ),
                }
                for row in operation_rows
            ],
            "artifact_manifest": [
                {
                    "artifact_id": str(row["artifact_id"]),
                    "object_type": str(row["artifact_kind"]),
                    "artifact_media_type": str(row["media_type"]),
                    "size_bytes": int(row["size_bytes"]),
                    "created_revision": int(row["created_revision"]),
                }
                for row in artifacts
            ],
            "projection_checkpoints": checkpoints,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceDiagnosticProjection:
    """Short-lived read connections for API-created frozen bundle requests."""

    database: WorkspaceDatabase

    def current_revision(self) -> int:
        connection = self.database.open(writable=False)
        try:
            return SqliteDiagnosticWorkspaceProjection(connection).current_revision()
        finally:
            connection.close()

    def safe_projection(
        self,
        *,
        requested_revision: int,
        turn_id: str | None,
        operation_id: str | None,
    ) -> Mapping[str, Any]:
        connection = self.database.open(writable=False)
        try:
            return SqliteDiagnosticWorkspaceProjection(connection).safe_projection(
                requested_revision=requested_revision,
                turn_id=turn_id,
                operation_id=operation_id,
            )
        finally:
            connection.close()
