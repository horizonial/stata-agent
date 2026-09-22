"""The mandatory SQLite connection contract from ADR-008."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .errors import ConnectionContractError


@dataclass(frozen=True, slots=True)
class SQLiteConnectionContract:
    busy_timeout_ms: int = 5_000
    synchronous_level: int = 2  # FULL

    def connect(self, database_path: Path, *, writable: bool) -> sqlite3.Connection:
        database_path = database_path.resolve()
        if writable:
            database_path.parent.mkdir(parents=True, exist_ok=True)
            target = str(database_path)
            uri = False
        else:
            if not database_path.is_file():
                raise ConnectionContractError(f"Workspace database does not exist: {database_path}")
            normalized = database_path.as_posix()
            target = f"file:{quote(normalized, safe='/:')}?mode=ro"
            uri = True

        connection = sqlite3.connect(
            target,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
            uri=uri,
        )
        connection.row_factory = sqlite3.Row
        try:
            self._configure(connection, writable=writable)
            self.assert_satisfied(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def _configure(self, connection: sqlite3.Connection, *, writable: bool) -> None:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute(f"PRAGMA synchronous = {self.synchronous_level}")
        connection.execute("PRAGMA trusted_schema = OFF")
        if writable:
            mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
            if mode != "wal":
                raise ConnectionContractError(f"SQLite refused WAL mode: {mode}")
        else:
            connection.execute("PRAGMA query_only = ON")

    def assert_satisfied(self, connection: sqlite3.Connection) -> None:
        values = {
            "foreign_keys": int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
            "busy_timeout": int(connection.execute("PRAGMA busy_timeout").fetchone()[0]),
            "synchronous": int(connection.execute("PRAGMA synchronous").fetchone()[0]),
            "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
        }
        expected = {
            "foreign_keys": 1,
            "busy_timeout": self.busy_timeout_ms,
            "synchronous": self.synchronous_level,
            "journal_mode": "wal",
        }
        if values != expected:
            raise ConnectionContractError(
                f"SQLite connection contract mismatch: expected={expected!r}, actual={values!r}"
            )


DEFAULT_CONNECTION_CONTRACT = SQLiteConnectionContract()
