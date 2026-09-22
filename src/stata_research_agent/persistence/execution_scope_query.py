"""SQLite adapter for the active Workspace write Turn's Execution Scope."""

from __future__ import annotations

from stata_research_agent.application.ports.execution_scope import (
    AuthorizedExecutionScope,
)
from stata_research_agent.domain.identifiers import ExecutionScopeId, TurnId

from .workspace import WorkspaceDatabase


class SqliteExecutionScopeAuthority:
    def __init__(self, database: WorkspaceDatabase) -> None:
        self._database = database

    def active_write_scope(self, turn_id: TurnId) -> AuthorizedExecutionScope:
        connection = self._database.open(writable=False)
        try:
            row = connection.execute(
                """
                SELECT turn.execution_scope_id
                FROM turns AS turn
                JOIN workspace_write_lane AS lane
                  ON lane.singleton_id = 1
                 AND lane.active_write_turn_id = turn.turn_id
                WHERE turn.turn_id = ?
                  AND turn.execution_mode = 'write'
                  AND turn.status IN ('running', 'waiting')
                """,
                (turn_id.value,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValueError("Turn does not own the selected Workspace write lane")
        return AuthorizedExecutionScope(
            workspace_id=self._database.workspace_id,
            execution_scope_id=ExecutionScopeId(str(row["execution_scope_id"])),
            workspace_root=self._database.root.resolve(),
            active_write_turn_id=turn_id,
        )
