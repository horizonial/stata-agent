"""Read-only authoritative trajectory queries for offline Product Evaluation."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class SqliteTrajectoryEvaluationQuery:
    """Expose stable evaluation facts without leaking SQLite into interface adapters."""

    def __init__(self, connection: sqlite3.Connection, *, owns_connection: bool = False) -> None:
        self._connection = connection
        self._owns_connection = owns_connection

    @classmethod
    def open_readonly(cls, database_path: Path) -> SqliteTrajectoryEvaluationQuery:
        uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        return cls(connection, owns_connection=True)

    def close(self) -> None:
        if self._owns_connection:
            self._connection.close()
            self._owns_connection = False

    def turn_loop_report(self) -> dict[str, Any]:
        stop = self._connection.execute(
            """
            SELECT decision, directive, terminal_disposition, reason_code, created_revision
            FROM stop_guard_decisions ORDER BY created_revision DESC LIMIT 1
            """
        ).fetchone()
        steps = self._connection.execute(
            "SELECT step_ordinal, status, created_revision, completed_revision "
            "FROM steps ORDER BY step_ordinal"
        ).fetchall()
        calls = self._connection.execute(
            "SELECT tool_call_id, proposal_status, created_revision FROM tool_calls"
        ).fetchall()
        admissions = self._connection.execute(
            "SELECT tool_call_id, operation_id, admitted_turn_revision, admitted_revision "
            "FROM tool_admissions"
        ).fetchall()
        operations = self._connection.execute(
            "SELECT operation_id, status, created_revision, terminal_revision FROM operations"
        ).fetchall()
        results = self._connection.execute(
            "SELECT tool_call_id, result_kind, created_revision FROM canonical_tool_results"
        ).fetchall()
        coverage = self._connection.execute(
            "SELECT required_total, required_satisfied, coverage_status, created_revision "
            "FROM goal_coverages ORDER BY created_revision DESC LIMIT 1"
        ).fetchone()
        return {
            "steps": [dict(row) for row in steps],
            "tool_calls": [dict(row) for row in calls],
            "admissions": [dict(row) for row in admissions],
            "operations": [dict(row) for row in operations],
            "tool_results": [dict(row) for row in results],
            "goal_coverage": dict(coverage) if coverage is not None else None,
            "stop_guard": dict(stop) if stop is not None else None,
        }

    def turn_loop_authority_facts(self) -> dict[str, Any]:
        facts = self.turn_loop_report()
        turn = self._connection.execute("SELECT status FROM turns").fetchone()
        facts["turn_status"] = turn["status"] if turn is not None else None
        return facts

    def journal_entries(self) -> tuple[dict[str, Any], ...]:
        rows = self._connection.execute(
            """
            SELECT journal_entry_id, workspace_revision, ordinal, event_type,
                   object_type, object_id, payload_json
            FROM journal_entries ORDER BY workspace_revision, ordinal
            """
        ).fetchall()
        return tuple(
            {
                "journal_entry_id": str(row["journal_entry_id"]),
                "workspace_revision": int(row["workspace_revision"]),
                "ordinal": int(row["ordinal"]),
                "event_type": str(row["event_type"]),
                "object_type": str(row["object_type"]),
                "object_id": str(row["object_id"]),
                "payload_json": str(row["payload_json"]),
            }
            for row in rows
        )
