"""Backup-first, staged Workspace migration with one atomic adoption UoW."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from stata_research_agent.application.ports.release_activation import (
    IrreversibilityGuard,
    NoopIrreversibilityGuard,
)
from stata_research_agent.application.release_activation import IrreversibleCapability
from stata_research_agent.application.workspace_migration import (
    WorkspaceMigrationAttempt,
    WorkspaceMigrationError,
    WorkspaceMigrationOutcome,
    WorkspaceMigrationState,
)

from .migrations import MigrationRunner
from .workspace import WorkspaceDatabase


class WorkspaceMigrationCoordinator:
    def __init__(
        self,
        database: WorkspaceDatabase,
        target_runner: MigrationRunner,
        *,
        target_release_id: str,
        irreversibility_guard: IrreversibilityGuard | None = None,
    ) -> None:
        if not target_release_id or "\n" in target_release_id or "\r" in target_release_id:
            raise ValueError("target release identity is invalid")
        self._database = database
        self._target_runner = target_runner
        self._target_release_id = target_release_id
        self._guard = irreversibility_guard or NoopIrreversibilityGuard()
        self._migration_root = database.root.resolve() / ".stata-agent" / "migration"

    def prepare(self) -> WorkspaceMigrationAttempt:
        connection = self._raw_connection(writable=True)
        attempt_id = "migration_" + secrets.token_hex(16)
        try:
            source_version = self._source_version(connection)
            target_version = self._target_runner.current_version
            if source_version >= target_version:
                raise WorkspaceMigrationError("Workspace does not require a forward migration")
            self._preflight(connection, source_version, target_version)
            self._insert_attempt(connection, attempt_id, source_version, target_version)
            backup_path, backup_manifest = self._backup(
                connection, attempt_id, source_version, target_version
            )
            self._transition(
                connection,
                attempt_id,
                WorkspaceMigrationState.PREPARING,
                "backup_verified",
                backup_manifest=backup_manifest,
            )
            _candidate_path, candidate_manifest = self._candidate(
                backup_path, attempt_id, source_version, target_version
            )
            self._transition(
                connection,
                attempt_id,
                WorkspaceMigrationState.PREPARED,
                "prepare_verified",
                backup_manifest=backup_manifest,
                candidate_manifest=candidate_manifest,
            )
            return self._load_attempt(connection, attempt_id)
        except BaseException as error:
            if self._attempt_exists(connection, attempt_id):
                self._mark_recovery_required(connection, attempt_id, type(error).__name__)
            raise
        finally:
            connection.close()

    def adopt(self, migration_attempt_id: str) -> WorkspaceMigrationOutcome:
        connection = self._raw_connection(writable=True)
        try:
            attempt = self._load_attempt(connection, migration_attempt_id)
            if attempt.state != WorkspaceMigrationState.PREPARED:
                raise WorkspaceMigrationError("migration attempt is not prepared")
            backup = self._manifest(attempt.backup_manifest_json)
            candidate = self._manifest(attempt.candidate_manifest_json)
            self._verify_manifest(backup)
            self._verify_manifest(candidate)
            semantic_before = self._research_semantic_fingerprint(connection)
            self._guard.before(
                IrreversibleCapability.MIGRATION_ADOPTION,
                reference=migration_attempt_id,
            )
            self._transition(
                connection,
                migration_attempt_id,
                WorkspaceMigrationState.ADOPTING,
                "adoption_started",
            )
            receipt_id = "migrationreceipt_" + secrets.token_hex(16)

            def adoption_hook(
                adopted: sqlite3.Connection, source_version: int, target_version: int
            ) -> None:
                if source_version != attempt.source_schema_version:
                    raise WorkspaceMigrationError("migration source schema changed")
                if target_version != attempt.target_schema_version:
                    raise WorkspaceMigrationError("migration target schema changed")
                if self._research_semantic_fingerprint(adopted) != semantic_before:
                    raise WorkspaceMigrationError(
                        "default migration attempted to change research identity or adoption"
                    )
                row = self._attempt_row(adopted, migration_attempt_id)
                revision = int(row["attempt_revision"]) + 1
                now = self._now()
                adopted.execute(
                    """
                    INSERT INTO workspace_migration_receipts VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        receipt_id,
                        migration_attempt_id,
                        source_version,
                        target_version,
                        self._target_release_id,
                        str(backup["sha256"]),
                        str(candidate["sha256"]),
                        1,
                        1,
                        now,
                    ),
                )
                adopted.execute(
                    """
                    UPDATE workspace_migration_attempts
                    SET state = 'ADOPTED', attempt_revision = ?, updated_at = ?
                    WHERE migration_attempt_id = ? AND state = 'ADOPTING'
                    """,
                    (revision, now, migration_attempt_id),
                )
                if adopted.execute("SELECT changes()").fetchone()[0] != 1:
                    raise WorkspaceMigrationError("migration attempt adoption state changed")
                adopted.execute(
                    """
                    INSERT INTO workspace_migration_attempt_history VALUES (?,?,?,?,?)
                    """,
                    (
                        migration_attempt_id,
                        revision,
                        WorkspaceMigrationState.ADOPTED,
                        "schema_and_identity_adopted",
                        now,
                    ),
                )

            self._target_runner.migrate_in_uow(
                connection, self._database.workspace_id, adoption_hook
            )
            return WorkspaceMigrationOutcome(
                self._load_attempt(connection, migration_attempt_id),
                receipt_id,
                True,
            )
        except BaseException as error:
            if self._attempt_exists(connection, migration_attempt_id):
                self._mark_recovery_required(connection, migration_attempt_id, type(error).__name__)
            raise
        finally:
            connection.close()

    def cleanup(self, migration_attempt_id: str) -> WorkspaceMigrationOutcome:
        connection = self._raw_connection(writable=True)
        try:
            attempt = self._load_attempt(connection, migration_attempt_id)
            if attempt.state not in {
                WorkspaceMigrationState.ADOPTED,
                WorkspaceMigrationState.CLEANUP_COMPLETE,
            }:
                raise WorkspaceMigrationError("only an adopted migration can be cleaned")
            candidate = self._manifest(attempt.candidate_manifest_json)
            candidate_path = self._resolve_manifest_path(candidate)
            if candidate_path.exists():
                candidate_path.unlink()
            if attempt.state != WorkspaceMigrationState.CLEANUP_COMPLETE:
                self._transition(
                    connection,
                    migration_attempt_id,
                    WorkspaceMigrationState.CLEANUP_COMPLETE,
                    "candidate_cleanup_complete",
                )
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        """
                        UPDATE workspace_migration_lock
                        SET migration_attempt_id = NULL, acquired_at = NULL
                        WHERE singleton_id = 1 AND migration_attempt_id = ?
                        """,
                        (migration_attempt_id,),
                    )
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
            receipt = connection.execute(
                """
                SELECT migration_receipt_id FROM workspace_migration_receipts
                WHERE migration_attempt_id = ?
                """,
                (migration_attempt_id,),
            ).fetchone()
            return WorkspaceMigrationOutcome(
                self._load_attempt(connection, migration_attempt_id),
                None if receipt is None else str(receipt[0]),
                self._resolve_manifest_path(self._manifest(attempt.backup_manifest_json)).is_file(),
            )
        finally:
            connection.close()

    def inspect(self, migration_attempt_id: str) -> WorkspaceMigrationOutcome:
        connection = self._raw_connection(writable=False)
        try:
            attempt = self._load_attempt(connection, migration_attempt_id)
            receipt = connection.execute(
                """
                SELECT migration_receipt_id FROM workspace_migration_receipts
                WHERE migration_attempt_id = ?
                """,
                (migration_attempt_id,),
            ).fetchone()
            backup_retained = False
            if attempt.backup_manifest_json is not None:
                try:
                    backup_retained = self._resolve_manifest_path(
                        self._manifest(attempt.backup_manifest_json)
                    ).is_file()
                except WorkspaceMigrationError:
                    backup_retained = False
            return WorkspaceMigrationOutcome(
                attempt,
                None if receipt is None else str(receipt[0]),
                backup_retained,
            )
        finally:
            connection.close()

    def _raw_connection(self, *, writable: bool) -> sqlite3.Connection:
        return self._database.connection_contract.connect(
            self._database.database_path, writable=writable
        )

    @staticmethod
    def _source_version(connection: sqlite3.Connection) -> int:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def _preflight(
        self, connection: sqlite3.Connection, source_version: int, target_version: int
    ) -> None:
        if str(connection.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
            raise WorkspaceMigrationError("Workspace quick_check failed")
        active = int(
            connection.execute(
                """
                SELECT count(*) FROM turns
                WHERE execution_mode = 'write' AND status IN ('queued','running','waiting')
                """
            ).fetchone()[0]
        )
        operations = int(
            connection.execute(
                """
                SELECT count(*) FROM operations
                WHERE status NOT IN ('completed','failed','integrity_violation')
                """
            ).fetchone()[0]
        )
        lane = connection.execute(
            "SELECT active_write_turn_id FROM workspace_write_lane WHERE singleton_id = 1"
        ).fetchone()[0]
        if active or operations or lane is not None:
            raise WorkspaceMigrationError("Workspace has nonterminal write execution")
        required = self._database.database_path.stat().st_size * 3 + 16 * 1024 * 1024
        if shutil.disk_usage(self._database.root.resolve()).free < required:
            raise WorkspaceMigrationError("insufficient disk space for migration staging")
        if source_version < 26 or target_version <= source_version:
            raise WorkspaceMigrationError("Workspace migration control schema is unavailable")

    def _insert_attempt(
        self,
        connection: sqlite3.Connection,
        attempt_id: str,
        source_version: int,
        target_version: int,
    ) -> None:
        now = self._now()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                INSERT INTO workspace_migration_attempts VALUES (
                    ?,?,?,?,'PREPARING',NULL,NULL,NULL,1,?,?
                )
                """,
                (
                    attempt_id,
                    source_version,
                    target_version,
                    self._target_release_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO workspace_migration_attempt_history VALUES (?,?,?,?,?)",
                (attempt_id, 1, WorkspaceMigrationState.PREPARING, "attempt_created", now),
            )
            connection.execute(
                """
                UPDATE workspace_migration_lock
                SET migration_attempt_id = ?, acquired_at = ?
                WHERE singleton_id = 1 AND migration_attempt_id IS NULL
                """,
                (attempt_id, now),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise WorkspaceMigrationError("Workspace migration lock is held")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def _backup(
        self,
        connection: sqlite3.Connection,
        attempt_id: str,
        source_version: int,
        target_version: int,
    ) -> tuple[Path, str]:
        root = self._migration_root / "backups"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{attempt_id}.sqlite3"
        backup = sqlite3.connect(path)
        try:
            connection.backup(backup)
            backup.commit()
        finally:
            backup.close()
        self._fsync(path)
        return path, self._manifest_json(path, source_version, target_version, "backup")

    def _candidate(
        self,
        backup_path: Path,
        attempt_id: str,
        source_version: int,
        target_version: int,
    ) -> tuple[Path, str]:
        root = self._migration_root / "staging"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{attempt_id}.candidate.sqlite3"
        shutil.copyfile(backup_path, path)
        candidate = self._database.connection_contract.connect(path, writable=True)
        try:
            self._target_runner.migrate(candidate, self._database.workspace_id)
            self._target_runner.validate(candidate, self._database.workspace_id)
        finally:
            candidate.close()
        self._fsync(path)
        return path, self._manifest_json(path, source_version, target_version, "candidate")

    def _manifest_json(
        self, path: Path, source_version: int, target_version: int, kind: str
    ) -> str:
        payload = path.read_bytes()
        return json.dumps(
            {
                "kind": kind,
                "relative_path": path.relative_to(self._database.root.resolve()).as_posix(),
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "source_schema_version": source_version,
                "target_schema_version": target_version,
                "target_release_id": self._target_release_id,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    def _transition(
        self,
        connection: sqlite3.Connection,
        attempt_id: str,
        state: WorkspaceMigrationState,
        reason: str,
        *,
        backup_manifest: str | None = None,
        candidate_manifest: str | None = None,
        failure_code: str | None = None,
    ) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._attempt_row(connection, attempt_id)
            revision = int(row["attempt_revision"]) + 1
            connection.execute(
                """
                UPDATE workspace_migration_attempts
                SET state = ?, backup_manifest_json = COALESCE(?, backup_manifest_json),
                    candidate_manifest_json = COALESCE(?, candidate_manifest_json),
                    failure_code = COALESCE(?, failure_code),
                    attempt_revision = ?, updated_at = ?
                WHERE migration_attempt_id = ? AND attempt_revision = ?
                """,
                (
                    state,
                    backup_manifest,
                    candidate_manifest,
                    failure_code,
                    revision,
                    self._now(),
                    attempt_id,
                    row["attempt_revision"],
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise WorkspaceMigrationError("migration attempt revision conflict")
            connection.execute(
                "INSERT INTO workspace_migration_attempt_history VALUES (?,?,?,?,?)",
                (attempt_id, revision, state, reason, self._now()),
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def _mark_recovery_required(
        self, connection: sqlite3.Connection, attempt_id: str, failure_code: str
    ) -> None:
        try:
            attempt = self._load_attempt(connection, attempt_id)
            if attempt.state not in {
                WorkspaceMigrationState.ADOPTED,
                WorkspaceMigrationState.CLEANUP_COMPLETE,
                WorkspaceMigrationState.RECOVERY_REQUIRED,
            }:
                self._transition(
                    connection,
                    attempt_id,
                    WorkspaceMigrationState.RECOVERY_REQUIRED,
                    "migration_failed",
                    failure_code=failure_code[:128],
                )
        except (sqlite3.Error, WorkspaceMigrationError):
            return

    def _load_attempt(
        self, connection: sqlite3.Connection, attempt_id: str
    ) -> WorkspaceMigrationAttempt:
        row = self._attempt_row(connection, attempt_id)
        return WorkspaceMigrationAttempt(
            str(row["migration_attempt_id"]),
            int(row["source_schema_version"]),
            int(row["target_schema_version"]),
            str(row["target_release_id"]),
            WorkspaceMigrationState(row["state"]),
            None if row["backup_manifest_json"] is None else str(row["backup_manifest_json"]),
            None if row["candidate_manifest_json"] is None else str(row["candidate_manifest_json"]),
            None if row["failure_code"] is None else str(row["failure_code"]),
            int(row["attempt_revision"]),
        )

    @staticmethod
    def _attempt_row(connection: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM workspace_migration_attempts WHERE migration_attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise WorkspaceMigrationError("migration attempt does not exist")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _attempt_exists(connection: sqlite3.Connection, attempt_id: str) -> bool:
        try:
            return (
                connection.execute(
                    "SELECT 1 FROM workspace_migration_attempts WHERE migration_attempt_id = ?",
                    (attempt_id,),
                ).fetchone()
                is not None
            )
        except sqlite3.Error:
            return False

    def _manifest(self, value: str | None) -> dict[str, Any]:
        if value is None:
            raise WorkspaceMigrationError("migration manifest is unavailable")
        try:
            manifest = json.loads(value)
        except json.JSONDecodeError as error:
            raise WorkspaceMigrationError("migration manifest is invalid") from error
        required = {
            "kind",
            "relative_path",
            "size",
            "sha256",
            "source_schema_version",
            "target_schema_version",
            "target_release_id",
        }
        if not isinstance(manifest, dict) or set(manifest) != required:
            raise WorkspaceMigrationError("migration manifest shape is invalid")
        return manifest

    def _verify_manifest(self, manifest: dict[str, Any]) -> None:
        path = self._resolve_manifest_path(manifest)
        if not path.is_file() or path.is_symlink():
            raise WorkspaceMigrationError("migration payload is missing or aliased")
        payload = path.read_bytes()
        if (
            type(manifest["size"]) is not int
            or len(payload) != manifest["size"]
            or hashlib.sha256(payload).hexdigest() != manifest["sha256"]
            or manifest["target_release_id"] != self._target_release_id
        ):
            raise WorkspaceMigrationError("migration payload integrity failed")

    def _resolve_manifest_path(self, manifest: dict[str, Any]) -> Path:
        relative = manifest.get("relative_path")
        if not isinstance(relative, str) or not relative:
            raise WorkspaceMigrationError("migration manifest path is invalid")
        root = self._database.root.resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise WorkspaceMigrationError("migration manifest path escaped Workspace")
        return path

    @staticmethod
    def _research_semantic_fingerprint(connection: sqlite3.Connection) -> str:
        tables = (
            "research_paths",
            "path_data_slots",
            "path_data_adoptions",
            "result_slots",
            "path_result_adoptions",
            "document_slots",
            "path_document_adoptions",
            "path_plan_adoptions",
        )
        snapshot: dict[str, list[list[Any]]] = {}
        for table in tables:
            columns = [
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            ]
            order = ",".join(f'"{column}"' for column in columns)
            rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}').fetchall()
            snapshot[table] = [list(row) for row in rows]
        encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _fsync(path: Path) -> None:
        with path.open("r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()
