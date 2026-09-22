"""Durable application-control ledger for release activation and rollback."""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from stata_research_agent.application.release_activation import (
    ActivationAttempt,
    ActivationReport,
    ActivationState,
    IrreversibleCapability,
    ReleaseActivationError,
    ReleaseState,
    RollbackEligibility,
    VerifiedRelease,
)


class SqliteReleaseControlStore:
    """A control-plane database that is physically separate from every Workspace."""

    def __init__(self, database_path: Path) -> None:
        self._path = database_path.resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            self._create_schema(connection)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS releases (
                release_id TEXT PRIMARY KEY,
                semantic_version TEXT NOT NULL,
                build_id TEXT NOT NULL,
                publisher_key_id TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256) = 64),
                version_directory TEXT NOT NULL UNIQUE,
                entry_point TEXT NOT NULL,
                global_control_schema_min INTEGER NOT NULL,
                global_control_schema_max INTEGER NOT NULL,
                workspace_schema_read_min INTEGER NOT NULL,
                workspace_schema_read_max INTEGER NOT NULL,
                workspace_schema_write_min INTEGER NOT NULL,
                workspace_schema_write_max INTEGER NOT NULL,
                minimum_launcher_version TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN (
                    'PENDING_ACTIVATION','ACTIVE','RETIRED','INVALID','ACTIVATION_FAILED'
                )),
                registered_at TEXT NOT NULL
            ) STRICT;

            CREATE TABLE IF NOT EXISTS release_pointers (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                active_release_id TEXT REFERENCES releases(release_id),
                pending_release_id TEXT REFERENCES releases(release_id),
                pointer_revision INTEGER NOT NULL CHECK(pointer_revision >= 0)
            ) STRICT;

            INSERT OR IGNORE INTO release_pointers(
                singleton, active_release_id, pending_release_id, pointer_revision
            ) VALUES (1, NULL, NULL, 0);

            CREATE TABLE IF NOT EXISTS release_activation_attempts (
                activation_attempt_id TEXT PRIMARY KEY,
                candidate_release_id TEXT NOT NULL REFERENCES releases(release_id),
                previous_release_id TEXT REFERENCES releases(release_id),
                state TEXT NOT NULL CHECK(state IN (
                    'PROBING','STARTING_SAFE','ACTIVE_REVERSIBLE','IRREVERSIBLE',
                    'SUCCEEDED','FAILED'
                )),
                rollback_eligibility TEXT NOT NULL CHECK(rollback_eligibility IN (
                    'VERIFIED_REVERSIBLE','FORBIDDEN_IRREVERSIBLE','UNKNOWN'
                )),
                irreversible_reasons_json TEXT NOT NULL,
                failure_reason TEXT,
                attempt_revision INTEGER NOT NULL CHECK(attempt_revision >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            ) STRICT;

            CREATE TABLE IF NOT EXISTS release_activation_history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                activation_attempt_id TEXT NOT NULL
                    REFERENCES release_activation_attempts(activation_attempt_id),
                attempt_revision INTEGER NOT NULL,
                state TEXT NOT NULL,
                rollback_eligibility TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                UNIQUE(activation_attempt_id, attempt_revision)
            ) STRICT;

            CREATE TRIGGER IF NOT EXISTS release_activation_history_no_update
            BEFORE UPDATE ON release_activation_history
            BEGIN SELECT RAISE(ABORT, 'activation history is immutable'); END;

            CREATE TRIGGER IF NOT EXISTS release_activation_history_no_delete
            BEFORE DELETE ON release_activation_history
            BEGIN SELECT RAISE(ABORT, 'activation history is immutable'); END;

            CREATE UNIQUE INDEX IF NOT EXISTS one_open_activation_attempt
            ON release_activation_attempts((1))
            WHERE state IN ('PROBING','STARTING_SAFE','ACTIVE_REVERSIBLE','IRREVERSIBLE');
            """
        )

    def register_pending(self, release: VerifiedRelease) -> int:
        self._validate_release(release)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT manifest_sha256 FROM releases WHERE release_id = ?",
                (release.release_id,),
            ).fetchone()
            if existing is not None:
                if existing["manifest_sha256"] != release.manifest_sha256:
                    connection.rollback()
                    raise ReleaseActivationError("release identity cannot be overwritten")
            else:
                connection.execute(
                    """
                    INSERT INTO releases VALUES (
                        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                    )
                    """,
                    (
                        release.release_id,
                        release.semantic_version,
                        release.build_id,
                        release.publisher_key_id,
                        release.manifest_sha256,
                        release.version_directory,
                        release.entry_point,
                        release.global_control_schema_min,
                        release.global_control_schema_max,
                        release.workspace_schema_read_min,
                        release.workspace_schema_read_max,
                        release.workspace_schema_write_min,
                        release.workspace_schema_write_max,
                        release.minimum_launcher_version,
                        ReleaseState.PENDING_ACTIVATION,
                        self._now(),
                    ),
                )
            connection.execute(
                """
                UPDATE release_pointers
                SET pending_release_id = ?, pointer_revision = pointer_revision + 1
                WHERE singleton = 1
                """,
                (release.release_id,),
            )
            revision = int(
                connection.execute(
                    "SELECT pointer_revision FROM release_pointers WHERE singleton = 1"
                ).fetchone()[0]
            )
            connection.commit()
            return revision

    def load_release(self, release_id: str) -> VerifiedRelease | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM releases WHERE release_id = ?", (release_id,)
            ).fetchone()
        return None if row is None else self._release(row)

    def begin_attempt(self, candidate_release_id: str) -> ActivationAttempt:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            pointer = connection.execute(
                "SELECT * FROM release_pointers WHERE singleton = 1"
            ).fetchone()
            release = connection.execute(
                "SELECT state FROM releases WHERE release_id = ?", (candidate_release_id,)
            ).fetchone()
            if release is None or release["state"] != ReleaseState.PENDING_ACTIVATION:
                connection.rollback()
                raise ReleaseActivationError("release is not pending activation")
            if pointer["pending_release_id"] != candidate_release_id:
                connection.rollback()
                raise ReleaseActivationError("release is not the current pending candidate")
            attempt_id = "activation_" + secrets.token_hex(16)
            previous = pointer["active_release_id"]
            eligibility = (
                RollbackEligibility.VERIFIED_REVERSIBLE
                if previous is not None
                else RollbackEligibility.UNKNOWN
            )
            now = self._now()
            connection.execute(
                """
                INSERT INTO release_activation_attempts VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    attempt_id,
                    candidate_release_id,
                    previous,
                    ActivationState.PROBING,
                    eligibility,
                    "[]",
                    None,
                    1,
                    now,
                    now,
                ),
            )
            self._append_history(
                connection,
                attempt_id,
                1,
                ActivationState.PROBING,
                eligibility,
                "attempt_created",
            )
            connection.commit()
        return self.load_attempt(attempt_id)

    def mark_starting_safe(self, activation_attempt_id: str) -> ActivationAttempt:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._attempt_row(connection, activation_attempt_id)
            if row["state"] != ActivationState.PROBING:
                connection.rollback()
                raise ReleaseActivationError("activation probe is not current")
            self._transition(
                connection,
                row,
                ActivationState.STARTING_SAFE,
                RollbackEligibility(row["rollback_eligibility"]),
                "probe_passed",
            )
            connection.execute(
                """
                UPDATE release_pointers
                SET active_release_id = ?, pending_release_id = NULL,
                    pointer_revision = pointer_revision + 1
                WHERE singleton = 1
                """,
                (row["candidate_release_id"],),
            )
            connection.commit()
        return self.load_attempt(activation_attempt_id)

    def mark_active_reversible(self, activation_attempt_id: str) -> ActivationAttempt:
        return self._required_transition(
            activation_attempt_id,
            {ActivationState.STARTING_SAFE},
            ActivationState.ACTIVE_REVERSIBLE,
            None,
            "runtime_ready",
        )

    def succeed(self, activation_attempt_id: str) -> ActivationAttempt:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._attempt_row(connection, activation_attempt_id)
            if row["state"] not in {
                ActivationState.STARTING_SAFE,
                ActivationState.ACTIVE_REVERSIBLE,
            }:
                connection.rollback()
                raise ReleaseActivationError("activation cannot succeed from current state")
            candidate = str(row["candidate_release_id"])
            previous = row["previous_release_id"]
            self._transition(
                connection,
                row,
                ActivationState.SUCCEEDED,
                RollbackEligibility(row["rollback_eligibility"]),
                "activation_contract_passed",
            )
            connection.execute(
                "UPDATE releases SET state = ? WHERE release_id = ?",
                (ReleaseState.ACTIVE, candidate),
            )
            if previous is not None:
                connection.execute(
                    "UPDATE releases SET state = ? WHERE release_id = ?",
                    (ReleaseState.RETIRED, previous),
                )
            connection.commit()
        return self.load_attempt(activation_attempt_id)

    def fail(self, activation_attempt_id: str, reason_code: str) -> ActivationAttempt:
        self._validate_reason(reason_code)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._attempt_row(connection, activation_attempt_id)
            if row["state"] in {ActivationState.SUCCEEDED, ActivationState.FAILED}:
                connection.rollback()
                raise ReleaseActivationError("terminal activation attempt cannot fail again")
            connection.execute(
                """
                UPDATE release_activation_attempts
                SET failure_reason = ? WHERE activation_attempt_id = ?
                """,
                (reason_code, activation_attempt_id),
            )
            row = self._attempt_row(connection, activation_attempt_id)
            self._transition(
                connection,
                row,
                ActivationState.FAILED,
                RollbackEligibility(row["rollback_eligibility"]),
                reason_code,
            )
            connection.commit()
        return self.load_attempt(activation_attempt_id)

    def mark_current_irreversible(
        self,
        capability: IrreversibleCapability,
        reference: str,
    ) -> ActivationAttempt | None:
        reason = f"{capability.value}:{reference}"
        self._validate_reason(reason)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM release_activation_attempts
                WHERE state IN ('PROBING','STARTING_SAFE','ACTIVE_REVERSIBLE','IRREVERSIBLE')
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            reasons = list(json.loads(str(row["irreversible_reasons_json"])))
            if reason not in reasons:
                reasons.append(reason)
            connection.execute(
                """
                UPDATE release_activation_attempts
                SET irreversible_reasons_json = ?
                WHERE activation_attempt_id = ?
                """,
                (json.dumps(reasons, separators=(",", ":")), row["activation_attempt_id"]),
            )
            row = self._attempt_row(connection, str(row["activation_attempt_id"]))
            self._transition(
                connection,
                row,
                ActivationState.IRREVERSIBLE,
                RollbackEligibility.FORBIDDEN_IRREVERSIBLE,
                reason,
            )
            connection.commit()
            attempt_id = str(row["activation_attempt_id"])
        return self.load_attempt(attempt_id)

    def rollback(
        self,
        activation_attempt_id: str,
        *,
        previous_release_verified: bool,
    ) -> ActivationReport:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._attempt_row(connection, activation_attempt_id)
            previous = row["previous_release_id"]
            forbidden_history = connection.execute(
                """
                SELECT 1 FROM release_activation_history
                WHERE activation_attempt_id = ? AND state IN ('IRREVERSIBLE','SUCCEEDED')
                LIMIT 1
                """,
                (activation_attempt_id,),
            ).fetchone()
            allowed = (
                row["state"] == ActivationState.FAILED
                and row["rollback_eligibility"] == RollbackEligibility.VERIFIED_REVERSIBLE
                and previous is not None
                and forbidden_history is None
                and previous_release_verified
            )
            if not allowed:
                connection.rollback()
                raise ReleaseActivationError("automatic rollback is not provably reversible")
            connection.execute(
                """
                UPDATE release_pointers
                SET active_release_id = ?, pending_release_id = NULL,
                    pointer_revision = pointer_revision + 1
                WHERE singleton = 1
                """,
                (previous,),
            )
            connection.execute(
                "UPDATE releases SET state = ? WHERE release_id = ?",
                (ReleaseState.ACTIVATION_FAILED, row["candidate_release_id"]),
            )
            connection.execute(
                "UPDATE releases SET state = ? WHERE release_id = ?",
                (ReleaseState.ACTIVE, previous),
            )
            connection.commit()
        return ActivationReport(
            activation_attempt_id,
            "ROLLED_BACK",
            str(previous),
            str(row["candidate_release_id"]),
            str(previous),
            "verified_reversible_failure",
        )

    def load_attempt(self, activation_attempt_id: str) -> ActivationAttempt:
        with self._connection() as connection:
            row = self._attempt_row(connection, activation_attempt_id)
        return ActivationAttempt(
            str(row["activation_attempt_id"]),
            str(row["candidate_release_id"]),
            None if row["previous_release_id"] is None else str(row["previous_release_id"]),
            ActivationState(row["state"]),
            RollbackEligibility(row["rollback_eligibility"]),
            tuple(str(item) for item in json.loads(str(row["irreversible_reasons_json"]))),
            None if row["failure_reason"] is None else str(row["failure_reason"]),
            int(row["attempt_revision"]),
        )

    def active_release_id(self) -> str | None:
        with self._connection() as connection:
            value = connection.execute(
                "SELECT active_release_id FROM release_pointers WHERE singleton = 1"
            ).fetchone()[0]
        return None if value is None else str(value)

    def is_successfully_active(self, release_id: str) -> bool:
        with self._connection() as connection:
            return (
                connection.execute(
                    """
                    SELECT 1 FROM release_pointers AS pointer
                    JOIN releases AS release
                      ON release.release_id = pointer.active_release_id
                    JOIN release_activation_attempts AS attempt
                      ON attempt.candidate_release_id = release.release_id
                    WHERE pointer.singleton = 1
                      AND release.release_id = ?
                      AND release.state = 'ACTIVE'
                      AND attempt.state = 'SUCCEEDED'
                    """,
                    (release_id,),
                ).fetchone()
                is not None
            )

    def _required_transition(
        self,
        activation_attempt_id: str,
        allowed_states: set[ActivationState],
        state: ActivationState,
        eligibility: RollbackEligibility | None,
        reason_code: str,
    ) -> ActivationAttempt:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._attempt_row(connection, activation_attempt_id)
            if ActivationState(row["state"]) not in allowed_states:
                connection.rollback()
                raise ReleaseActivationError("activation transition is not allowed")
            self._transition(
                connection,
                row,
                state,
                eligibility or RollbackEligibility(row["rollback_eligibility"]),
                reason_code,
            )
            connection.commit()
        return self.load_attempt(activation_attempt_id)

    def _transition(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        state: ActivationState,
        eligibility: RollbackEligibility,
        reason_code: str,
    ) -> None:
        revision = int(row["attempt_revision"]) + 1
        connection.execute(
            """
            UPDATE release_activation_attempts
            SET state = ?, rollback_eligibility = ?, attempt_revision = ?, updated_at = ?
            WHERE activation_attempt_id = ? AND attempt_revision = ?
            """,
            (
                state,
                eligibility,
                revision,
                self._now(),
                row["activation_attempt_id"],
                row["attempt_revision"],
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ReleaseActivationError("activation attempt revision conflict")
        self._append_history(
            connection,
            str(row["activation_attempt_id"]),
            revision,
            state,
            eligibility,
            reason_code,
        )

    def _append_history(
        self,
        connection: sqlite3.Connection,
        activation_attempt_id: str,
        revision: int,
        state: ActivationState,
        eligibility: RollbackEligibility,
        reason_code: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO release_activation_history(
                activation_attempt_id, attempt_revision, state,
                rollback_eligibility, reason_code, recorded_at
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                activation_attempt_id,
                revision,
                state,
                eligibility,
                reason_code,
                self._now(),
            ),
        )

    @staticmethod
    def _attempt_row(connection: sqlite3.Connection, activation_attempt_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM release_activation_attempts WHERE activation_attempt_id = ?",
            (activation_attempt_id,),
        ).fetchone()
        if row is None:
            raise ReleaseActivationError("activation attempt does not exist")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _release(row: sqlite3.Row) -> VerifiedRelease:
        return VerifiedRelease(
            str(row["release_id"]),
            str(row["semantic_version"]),
            str(row["build_id"]),
            str(row["publisher_key_id"]),
            str(row["manifest_sha256"]),
            str(row["version_directory"]),
            str(row["entry_point"]),
            int(row["global_control_schema_min"]),
            int(row["global_control_schema_max"]),
            int(row["workspace_schema_read_min"]),
            int(row["workspace_schema_read_max"]),
            int(row["workspace_schema_write_min"]),
            int(row["workspace_schema_write_max"]),
            str(row["minimum_launcher_version"]),
        )

    @staticmethod
    def _validate_release(release: VerifiedRelease) -> None:
        fields = (
            release.release_id,
            release.semantic_version,
            release.build_id,
            release.publisher_key_id,
            release.manifest_sha256,
            release.version_directory,
            release.entry_point,
            release.minimum_launcher_version,
        )
        if any(not value or "\n" in value or "\r" in value for value in fields):
            raise ReleaseActivationError("release identity is invalid")
        if len(release.manifest_sha256) != 64:
            raise ReleaseActivationError("release manifest digest is invalid")
        ranges = (
            (release.global_control_schema_min, release.global_control_schema_max),
            (release.workspace_schema_read_min, release.workspace_schema_read_max),
            (release.workspace_schema_write_min, release.workspace_schema_write_max),
        )
        if any(low < 0 or high < low for low, high in ranges):
            raise ReleaseActivationError("release schema range is invalid")

    @staticmethod
    def _validate_reason(reason: str) -> None:
        if not reason or len(reason) > 512 or "\n" in reason or "\r" in reason:
            raise ReleaseActivationError("activation reason is invalid")

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()
