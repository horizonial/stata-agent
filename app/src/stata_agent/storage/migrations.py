"""Small, explicit and transactional SQLite schema migrations.

The application intentionally does not depend on Alembic.  A migration is a
plain callable and the runner records its version in the same transaction as
the schema changes.  This keeps the storage package usable by the desktop
bundle while still giving upgrades a durable, inspectable history.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

MigrationFn = Callable[[sqlite3.Connection], None]


class MigrationError(RuntimeError):
    """Raised when migration input or transaction state is invalid."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One ordered schema change.

    ``apply`` must only use the supplied connection.  The runner owns the
    transaction, so migration functions must not commit or rollback.
    """

    version: int
    name: str
    apply: MigrationFn


def _create_memory_schema(connection: sqlite3.Connection) -> None:
    """Create the first durable memory schema.

    ``execute`` is deliberately used once per statement instead of
    ``executescript``: Python's ``executescript`` may implicitly commit before
    executing SQL, which would defeat the runner's rollback guarantee.
    """

    statements = (
        """
        CREATE TABLE IF NOT EXISTS memory_records (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            scope TEXT NOT NULL CHECK (scope IN ('project', 'global')),
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'retracted')),
            confidence TEXT NOT NULL CHECK (confidence IN ('explicit', 'verified', 'inferred')),
            source_ids TEXT NOT NULL DEFAULT '[]',
            fingerprint TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            last_used_at INTEGER,
            use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0),
            supersedes TEXT,
            superseded_by TEXT,
            expires_at,
            sensitive INTEGER NOT NULL DEFAULT 0 CHECK (sensitive IN (0, 1)),
            quarantined INTEGER NOT NULL DEFAULT 0 CHECK (quarantined IN (0, 1)),
            schema_version INTEGER NOT NULL DEFAULT 2
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS memory_candidates (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            scope TEXT NOT NULL CHECK (scope IN ('project', 'global')),
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('candidate', 'accepted', 'rejected')),
            confidence TEXT NOT NULL CHECK (confidence IN ('explicit', 'verified', 'inferred')),
            source_ids TEXT NOT NULL DEFAULT '[]',
            fingerprint TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            last_used_at INTEGER,
            use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0),
            supersedes TEXT,
            expires_at,
            sensitive INTEGER NOT NULL DEFAULT 0 CHECK (sensitive IN (0, 1)),
            quarantined INTEGER NOT NULL DEFAULT 0 CHECK (quarantined IN (0, 1)),
            schema_version INTEGER NOT NULL DEFAULT 2,
            decided_at INTEGER,
            decision_reason TEXT,
            accepted_record_id TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS workspace_registry (
            workspace_id TEXT PRIMARY KEY,
            root TEXT,
            name TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 1
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_records_fingerprint
        ON memory_records(workspace_id, scope, fingerprint)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_memory_records_workspace_status
        ON memory_records(workspace_id, scope, status, updated_at)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_candidates_pending_fingerprint
        ON memory_candidates(workspace_id, scope, fingerprint)
        WHERE status = 'candidate'
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_memory_candidates_workspace_status
        ON memory_candidates(workspace_id, scope, status, updated_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_workspace_registry_updated
        ON workspace_registry(updated_at)
        """,
    )
    for statement in statements:
        connection.execute(statement)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "memory_storage_v1", _create_memory_schema),
)
LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version


class MigrationRunner:
    """Apply ordered migrations and record each version atomically."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        migrations: Iterable[Migration] = MIGRATIONS,
    ) -> None:
        self._connection = connection
        self._migrations = tuple(migrations)
        self._validate_migrations()

    def _validate_migrations(self) -> None:
        previous = 0
        for migration in self._migrations:
            if migration.version <= previous:
                raise MigrationError("migration versions must be strictly increasing")
            if migration.version <= 0 or not migration.name.strip():
                raise MigrationError("migration versions must be positive and named")
            previous = migration.version

    @property
    def current_version(self) -> int:
        """Return the highest recorded version, or zero for a new database."""

        table = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if table is None:
            return 0
        row = self._connection.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
        return int(row["version"] if isinstance(row, sqlite3.Row) else row[0]) if row and row[0] is not None else 0

    def applied(self) -> list[dict[str, object]]:
        """Return migration records without changing the database."""

        table = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if table is None:
            return []
        rows = self._connection.execute(
            "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        return [
            {"version": int(row[0]), "name": str(row[1]), "applied_at": int(row[2])}
            for row in rows
        ]

    def run(self, *, target_version: int | None = None) -> int:
        """Apply pending migrations and return the resulting schema version.

        The migration table, migration DDL and version rows share one
        ``BEGIN IMMEDIATE`` transaction.  Any exception rolls back all schema
        changes from this invocation, including the migration table itself on
        a brand-new database.
        """

        latest = self._migrations[-1].version if self._migrations else 0
        target = latest if target_version is None else int(target_version)
        if target < 0 or target > latest:
            raise MigrationError(f"target schema version {target} is outside 0..{latest}")
        if self._connection.in_transaction:
            raise MigrationError("cannot start migrations inside an existing transaction")

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at INTEGER NOT NULL)"
            )
            applied = {
                int(row[0]): str(row[1])
                for row in self._connection.execute(
                    "SELECT version, name FROM schema_migrations ORDER BY version"
                )
            }
            for migration in self._migrations:
                if migration.version > target or migration.version in applied:
                    continue
                migration.apply(self._connection)
                self._connection.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.name, int(time.time())),
                )
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise
        return self.current_version


def run_migrations(
    connection: sqlite3.Connection,
    *,
    target_version: int | None = None,
    migrations: Iterable[Migration] = MIGRATIONS,
) -> int:
    """Convenience wrapper around :class:`MigrationRunner`."""

    return MigrationRunner(connection, migrations).run(target_version=target_version)


__all__ = [
    "LATEST_SCHEMA_VERSION",
    "MIGRATIONS",
    "Migration",
    "MigrationError",
    "MigrationRunner",
    "run_migrations",
]
