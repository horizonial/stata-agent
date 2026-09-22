"""Durable release-candidate and signing-attempt identities."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from stata_research_agent.application.release_signing import (
    ApprovedReleaseCandidate,
    ReleaseSigningError,
    SigningAttempt,
    SigningAttemptState,
)


class SqliteReleaseSigningStore:
    def __init__(self, database_path: Path) -> None:
        self._path = database_path.resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS approved_release_candidates (
                    release_id TEXT PRIMARY KEY,
                    unsigned_payload_sha256 TEXT NOT NULL UNIQUE,
                    runtime_sbom_sha256 TEXT NOT NULL,
                    build_sbom_sha256 TEXT NOT NULL,
                    gate_evidence_sha256 TEXT NOT NULL,
                    approved_at TEXT NOT NULL
                ) STRICT;

                CREATE TRIGGER IF NOT EXISTS approved_release_candidates_no_update
                BEFORE UPDATE ON approved_release_candidates
                BEGIN SELECT RAISE(ABORT, 'release candidate is immutable'); END;

                CREATE TRIGGER IF NOT EXISTS approved_release_candidates_no_delete
                BEFORE DELETE ON approved_release_candidates
                BEGIN SELECT RAISE(ABORT, 'release candidate is immutable'); END;

                CREATE TABLE IF NOT EXISTS release_signing_attempts (
                    signing_attempt_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    release_id TEXT NOT NULL
                        REFERENCES approved_release_candidates(release_id) ON DELETE RESTRICT,
                    unsigned_payload_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'REQUESTED','COMPLETED','FAILED','OUTCOME_UNKNOWN'
                    )),
                    signed_payload_sha256 TEXT,
                    failure_code TEXT,
                    requested_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(release_id, unsigned_payload_sha256, signing_attempt_id)
                ) STRICT;

                CREATE UNIQUE INDEX IF NOT EXISTS one_unresolved_signing_attempt
                ON release_signing_attempts(release_id, unsigned_payload_sha256)
                WHERE state IN ('REQUESTED','OUTCOME_UNKNOWN');
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        try:
            yield connection
        finally:
            connection.close()

    def approve(self, candidate: ApprovedReleaseCandidate) -> None:
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM approved_release_candidates WHERE release_id = ?",
                (candidate.release_id,),
            ).fetchone()
            if existing is not None:
                observed = self._candidate(existing)
                if observed != candidate:
                    raise ReleaseSigningError("release_id cannot be rebound to new evidence")
                return
            connection.execute(
                "INSERT INTO approved_release_candidates VALUES (?,?,?,?,?,?)",
                (
                    candidate.release_id,
                    candidate.unsigned_payload_sha256,
                    candidate.runtime_sbom_sha256,
                    candidate.build_sbom_sha256,
                    candidate.gate_evidence_sha256,
                    self._now(),
                ),
            )

    def request(self, release_id: str, request_id: str) -> SigningAttempt:
        if not request_id:
            raise ReleaseSigningError("signing request identity is required")
        with self._connection() as connection:
            replay = connection.execute(
                "SELECT * FROM release_signing_attempts WHERE request_id = ?", (request_id,)
            ).fetchone()
            if replay is not None:
                if replay["release_id"] != release_id:
                    raise ReleaseSigningError("signing request identity was reused")
                return self._attempt(replay)
            candidate = connection.execute(
                "SELECT * FROM approved_release_candidates WHERE release_id = ?",
                (release_id,),
            ).fetchone()
            if candidate is None:
                raise ReleaseSigningError("release candidate is not gate-approved")
            unresolved = connection.execute(
                """
                SELECT 1 FROM release_signing_attempts
                WHERE release_id = ? AND state IN ('REQUESTED','OUTCOME_UNKNOWN')
                """,
                (release_id,),
            ).fetchone()
            if unresolved is not None:
                raise ReleaseSigningError("signing outcome is unresolved; blind retry forbidden")
            attempt_id = "signing_" + secrets.token_hex(16)
            connection.execute(
                "INSERT INTO release_signing_attempts VALUES (?,?,?,?,?,NULL,NULL,?,NULL)",
                (
                    attempt_id,
                    request_id,
                    release_id,
                    candidate["unsigned_payload_sha256"],
                    SigningAttemptState.REQUESTED,
                    self._now(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM release_signing_attempts WHERE signing_attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            return self._attempt(cast(sqlite3.Row, row))

    def complete(
        self,
        signing_attempt_id: str,
        *,
        state: SigningAttemptState,
        signed_payload_sha256: str | None = None,
        failure_code: str | None = None,
    ) -> SigningAttempt:
        if state == SigningAttemptState.REQUESTED:
            raise ReleaseSigningError("signing attempt requires a terminal state")
        if state == SigningAttemptState.COMPLETED and (
            signed_payload_sha256 is None or len(signed_payload_sha256) != 64
        ):
            raise ReleaseSigningError("completed signing attempt requires signed payload hash")
        if state != SigningAttemptState.COMPLETED and not failure_code:
            raise ReleaseSigningError("non-completed signing attempt requires a safe failure code")
        if failure_code is not None and (
            len(failure_code) > 128 or "\n" in failure_code or "\r" in failure_code
        ):
            raise ReleaseSigningError("failure code must be a short single-line safe identifier")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE release_signing_attempts
                SET state = ?, signed_payload_sha256 = ?, failure_code = ?, completed_at = ?
                WHERE signing_attempt_id = ? AND state = 'REQUESTED'
                """,
                (
                    state,
                    signed_payload_sha256,
                    failure_code,
                    self._now(),
                    signing_attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ReleaseSigningError("signing attempt is not pending")
            row = connection.execute(
                "SELECT * FROM release_signing_attempts WHERE signing_attempt_id = ?",
                (signing_attempt_id,),
            ).fetchone()
            return self._attempt(cast(sqlite3.Row, row))

    @staticmethod
    def _candidate(row: sqlite3.Row) -> ApprovedReleaseCandidate:
        return ApprovedReleaseCandidate(
            str(row["release_id"]),
            str(row["unsigned_payload_sha256"]),
            str(row["runtime_sbom_sha256"]),
            str(row["build_sbom_sha256"]),
            str(row["gate_evidence_sha256"]),
        )

    @staticmethod
    def _attempt(row: sqlite3.Row) -> SigningAttempt:
        return SigningAttempt(
            str(row["signing_attempt_id"]),
            str(row["request_id"]),
            str(row["release_id"]),
            str(row["unsigned_payload_sha256"]),
            SigningAttemptState(row["state"]),
            None if row["signed_payload_sha256"] is None else str(row["signed_payload_sha256"]),
            None if row["failure_code"] is None else str(row["failure_code"]),
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()
