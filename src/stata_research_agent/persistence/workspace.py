"""Create and open one authoritative SQLite ledger per Workspace."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from stata_research_agent.domain.identifiers import WorkspaceId

from .connection import DEFAULT_CONNECTION_CONTRACT, SQLiteConnectionContract
from .errors import WorkspaceIdentityError
from .migrations import MigrationRunner


@dataclass(frozen=True, slots=True)
class WorkspaceDatabase:
    root: Path
    workspace_id: WorkspaceId
    connection_contract: SQLiteConnectionContract = field(
        default=DEFAULT_CONNECTION_CONTRACT, repr=False
    )
    migration_runner: MigrationRunner = field(default_factory=MigrationRunner, repr=False)

    @property
    def database_path(self) -> Path:
        return self.root.resolve() / "workspace.sqlite3"

    def create(self) -> None:
        database_path = self.database_path
        if database_path.exists():
            raise WorkspaceIdentityError(f"Workspace database already exists: {database_path}")
        connection = self.connection_contract.connect(database_path, writable=True)
        try:
            self.migration_runner.migrate(connection, self.workspace_id)
            self.migration_runner.validate(connection, self.workspace_id)
        finally:
            connection.close()

    def open(self, *, writable: bool) -> sqlite3.Connection:
        connection = self.connection_contract.connect(self.database_path, writable=writable)
        try:
            self.migration_runner.validate(connection, self.workspace_id)
        except BaseException:
            connection.close()
            raise
        return connection

    def schema_version(self) -> int:
        connection = self.connection_contract.connect(self.database_path, writable=False)
        try:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()
