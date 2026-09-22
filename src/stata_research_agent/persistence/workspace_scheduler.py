"""SQLite adapter for the Workspace scheduler authority port."""

from __future__ import annotations

import sqlite3

from stata_research_agent.application.control import ActivateNextQueuedTurnCommand
from stata_research_agent.application.ports.identity import IdentityGenerator
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, TurnId

from .control_store import SqliteControlStore
from .workspace import WorkspaceDatabase


class SqliteWorkspaceTurnAuthority:
    def __init__(
        self,
        database: WorkspaceDatabase,
        identities: IdentityGenerator,
    ) -> None:
        self._database = database
        self._identities = identities

    def active_write_turn(self) -> tuple[TurnId, str] | None:
        connection = self._database.open(writable=False)
        try:
            row = connection.execute(
                """
                SELECT turn.turn_id, turn.status
                FROM workspace_write_lane AS lane
                JOIN turns AS turn ON turn.turn_id = lane.active_write_turn_id
                WHERE lane.singleton_id = 1
                """
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return TurnId(str(row["turn_id"])), str(row["status"])

    def activate_next_queued_turn(self) -> tuple[TurnId, str] | None:
        connection = self._database.open(writable=True)
        try:
            service = WorkspaceControlService(SqliteControlStore(connection), self._identities)
            try:
                activated = service.activate_next_queued_turn(
                    ActivateNextQueuedTurnCommand(self._identities.new(CommandId))
                )
            except (ValueError, sqlite3.IntegrityError):
                return None
            return activated.turn_id, "running"
        finally:
            connection.close()
