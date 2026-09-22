"""Durable immutable Release Candidate and sequential G0-G7 gate ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from stata_research_agent.application.release_candidate import (
    FounderAcceptanceInput,
    FounderDecision,
    GateDecision,
    ReleaseCandidateDefinition,
    ReleaseCandidateError,
    ReleaseCandidateState,
    ReleaseGate,
    ReleaseTestOutcome,
    ReleaseTestResultInput,
)
from stata_research_agent.application.release_signing import ApprovedReleaseCandidate

_GATES = tuple(ReleaseGate)


class SqliteReleaseCandidateStore:
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
            CREATE TABLE IF NOT EXISTS release_candidates (
                release_id TEXT PRIMARY KEY,
                unsigned_payload_sha256 TEXT NOT NULL UNIQUE,
                release_manifest_sha256 TEXT NOT NULL UNIQUE,
                runtime_sbom_sha256 TEXT NOT NULL,
                build_sbom_sha256 TEXT NOT NULL,
                verification_manifest_sha256 TEXT NOT NULL,
                source_revision TEXT NOT NULL,
                applicable_set_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            ) STRICT;

            CREATE TABLE IF NOT EXISTS release_applicable_tests (
                release_id TEXT NOT NULL REFERENCES release_candidates(release_id),
                test_case_id TEXT NOT NULL,
                gate TEXT NOT NULL CHECK(gate IN ('G0','G1','G2','G3','G4','G5','G6','G7')),
                applicability_fingerprint TEXT NOT NULL,
                PRIMARY KEY(release_id, test_case_id)
            ) STRICT;

            CREATE TABLE IF NOT EXISTS release_candidate_states (
                release_id TEXT PRIMARY KEY REFERENCES release_candidates(release_id),
                state TEXT NOT NULL CHECK(state IN (
                    'CREATED','TESTING','READY_FOR_FOUNDER','APPROVED_FOR_SIGNING','REJECTED'
                )),
                state_revision INTEGER NOT NULL CHECK(state_revision >= 1),
                updated_at TEXT NOT NULL
            ) STRICT;

            CREATE TABLE IF NOT EXISTS release_candidate_state_history (
                release_id TEXT NOT NULL REFERENCES release_candidates(release_id),
                state_revision INTEGER NOT NULL,
                state TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY(release_id, state_revision)
            ) STRICT;

            CREATE TABLE IF NOT EXISTS release_test_results (
                test_result_id TEXT PRIMARY KEY,
                release_id TEXT NOT NULL REFERENCES release_candidates(release_id),
                test_case_id TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN (
                    'PASSED','FAILED','INCONCLUSIVE','OPEN_NOT_RUN'
                )),
                applicability_fingerprint TEXT NOT NULL,
                evidence_sha256 TEXT NOT NULL,
                environment_sha256 TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY(release_id, test_case_id)
                    REFERENCES release_applicable_tests(release_id, test_case_id)
            ) STRICT;

            CREATE TABLE IF NOT EXISTS release_gate_decisions (
                release_id TEXT NOT NULL REFERENCES release_candidates(release_id),
                gate TEXT NOT NULL,
                passed INTEGER NOT NULL CHECK(passed IN (0,1)),
                reason_codes_json TEXT NOT NULL,
                decision_revision INTEGER NOT NULL CHECK(decision_revision >= 1),
                decided_at TEXT NOT NULL,
                PRIMARY KEY(release_id, gate),
                UNIQUE(release_id, decision_revision)
            ) STRICT;

            CREATE TABLE IF NOT EXISTS founder_acceptance_receipts (
                founder_acceptance_id TEXT PRIMARY KEY,
                release_id TEXT NOT NULL REFERENCES release_candidates(release_id),
                test_case_id TEXT NOT NULL,
                scenario_id TEXT NOT NULL,
                scenario_version INTEGER NOT NULL CHECK(scenario_version >= 1),
                scenario_sha256 TEXT NOT NULL,
                journey_audit_sha256 TEXT NOT NULL,
                decision TEXT NOT NULL CHECK(decision IN ('ACCEPT','REJECT')),
                notes TEXT NOT NULL,
                issue_references_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                UNIQUE(release_id, scenario_id, scenario_version),
                FOREIGN KEY(release_id, test_case_id)
                    REFERENCES release_applicable_tests(release_id, test_case_id)
            ) STRICT;
            """
        )
        for table in (
            "release_candidates",
            "release_applicable_tests",
            "release_candidate_state_history",
            "release_test_results",
            "release_gate_decisions",
            "founder_acceptance_receipts",
        ):
            connection.executescript(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS {table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END;
                """
            )

    def create(self, candidate: ReleaseCandidateDefinition) -> None:
        applicable = [
            {
                "test_case_id": item.test_case_id,
                "gate": item.gate.value,
                "applicability_fingerprint": item.applicability_fingerprint,
            }
            for item in sorted(candidate.applicable_tests, key=lambda item: item.test_case_id)
        ]
        set_hash = hashlib.sha256(
            json.dumps(applicable, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM release_candidates WHERE release_id = ?",
                (candidate.release_id,),
            ).fetchone()
            if existing is not None:
                if self._candidate_tuple(existing) != self._definition_tuple(candidate, set_hash):
                    connection.rollback()
                    raise ReleaseCandidateError("release identity cannot be rebound")
                connection.commit()
                return
            now = self._now()
            connection.execute(
                "INSERT INTO release_candidates VALUES (?,?,?,?,?,?,?,?,?)",
                (*self._definition_tuple(candidate, set_hash), now),
            )
            connection.executemany(
                "INSERT INTO release_applicable_tests VALUES (?,?,?,?)",
                (
                    (
                        candidate.release_id,
                        item.test_case_id,
                        item.gate.value,
                        item.applicability_fingerprint,
                    )
                    for item in candidate.applicable_tests
                ),
            )
            connection.execute(
                "INSERT INTO release_candidate_states VALUES (?, 'CREATED', 1, ?)",
                (candidate.release_id, now),
            )
            connection.execute(
                """
                INSERT INTO release_candidate_state_history
                VALUES (?,1,'CREATED','candidate_created',?)
                """,
                (candidate.release_id, now),
            )
            connection.commit()

    def record_result(self, result: ReleaseTestResultInput) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute(
                "SELECT * FROM release_test_results WHERE test_result_id = ?",
                (result.test_result_id,),
            ).fetchone()
            if replay is not None:
                if self._result_tuple(replay) != self._input_result_tuple(result):
                    connection.rollback()
                    raise ReleaseCandidateError("test result identity cannot be rebound")
                connection.commit()
                return
            state = self._state_row(connection, result.release_id)
            if state["state"] in {"REJECTED", "APPROVED_FOR_SIGNING"}:
                connection.rollback()
                raise ReleaseCandidateError("terminal candidate cannot accept test results")
            case = connection.execute(
                """
                SELECT gate FROM release_applicable_tests
                WHERE release_id = ? AND test_case_id = ?
                """,
                (result.release_id, result.test_case_id),
            ).fetchone()
            if case is None:
                connection.rollback()
                raise ReleaseCandidateError("test case is outside the frozen Applicable Test Set")
            if connection.execute(
                "SELECT 1 FROM release_gate_decisions WHERE release_id = ? AND gate = ?",
                (result.release_id, case["gate"]),
            ).fetchone():
                connection.rollback()
                raise ReleaseCandidateError("decided gate cannot accept new evidence")
            connection.execute(
                "INSERT INTO release_test_results VALUES (?,?,?,?,?,?,?,?)",
                (*self._input_result_tuple(result), self._now()),
            )
            if state["state"] == "CREATED":
                self._transition(
                    connection,
                    result.release_id,
                    ReleaseCandidateState.TESTING,
                    "testing_started",
                )
            connection.commit()

    def record_founder_acceptance(self, receipt: FounderAcceptanceInput) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._state_row(connection, receipt.release_id)
            if state["state"] != ReleaseCandidateState.READY_FOR_FOUNDER:
                connection.rollback()
                raise ReleaseCandidateError("candidate is not ready for Founder Acceptance")
            case = connection.execute(
                "SELECT gate, applicability_fingerprint FROM release_applicable_tests "
                "WHERE release_id = ? AND test_case_id = ?",
                (receipt.release_id, receipt.test_case_id),
            ).fetchone()
            if case is None or case["gate"] != ReleaseGate.G7:
                connection.rollback()
                raise ReleaseCandidateError("Founder Scenario must bind an applicable G7 case")
            existing = connection.execute(
                "SELECT * FROM founder_acceptance_receipts WHERE founder_acceptance_id = ?",
                (receipt.founder_acceptance_id,),
            ).fetchone()
            if existing is not None:
                observed = (
                    str(existing["release_id"]),
                    str(existing["test_case_id"]),
                    str(existing["scenario_id"]),
                    int(existing["scenario_version"]),
                    str(existing["scenario_sha256"]),
                    str(existing["journey_audit_sha256"]),
                    str(existing["decision"]),
                    str(existing["notes"]),
                    tuple(json.loads(str(existing["issue_references_json"]))),
                )
                proposed = (
                    receipt.release_id,
                    receipt.test_case_id,
                    receipt.scenario_id,
                    receipt.scenario_version,
                    receipt.scenario_sha256,
                    receipt.journey_audit_sha256,
                    receipt.decision.value,
                    receipt.notes,
                    receipt.issue_references,
                )
                if observed != proposed:
                    connection.rollback()
                    raise ReleaseCandidateError("Founder Acceptance identity cannot be rebound")
                connection.commit()
                return
            now = self._now()
            connection.execute(
                "INSERT INTO founder_acceptance_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt.founder_acceptance_id,
                    receipt.release_id,
                    receipt.test_case_id,
                    receipt.scenario_id,
                    receipt.scenario_version,
                    receipt.scenario_sha256,
                    receipt.journey_audit_sha256,
                    receipt.decision.value,
                    receipt.notes,
                    json.dumps(receipt.issue_references, separators=(",", ":")),
                    now,
                ),
            )
            evidence_hash = hashlib.sha256(
                json.dumps(
                    {
                        "founder_acceptance_id": receipt.founder_acceptance_id,
                        "scenario_sha256": receipt.scenario_sha256,
                        "journey_audit_sha256": receipt.journey_audit_sha256,
                        "decision": receipt.decision.value,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            result = ReleaseTestResultInput(
                "founder-result:" + receipt.founder_acceptance_id,
                receipt.release_id,
                receipt.test_case_id,
                ReleaseTestOutcome.PASSED
                if receipt.decision is FounderDecision.ACCEPT
                else ReleaseTestOutcome.FAILED,
                str(case["applicability_fingerprint"]),
                evidence_hash,
                receipt.journey_audit_sha256,
            )
            connection.execute(
                "INSERT INTO release_test_results VALUES (?,?,?,?,?,?,?,?)",
                (*self._input_result_tuple(result), now),
            )
            if receipt.decision is FounderDecision.REJECT:
                self._transition(
                    connection,
                    receipt.release_id,
                    ReleaseCandidateState.REJECTED,
                    "founder_rejected",
                )
            connection.commit()

    def evaluate_gate(self, release_id: str, gate: ReleaseGate) -> GateDecision:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute(
                "SELECT * FROM release_gate_decisions WHERE release_id = ? AND gate = ?",
                (release_id, gate.value),
            ).fetchone()
            if replay is not None:
                connection.commit()
                return self._gate(replay)
            state = self._state_row(connection, release_id)
            if state["state"] == ReleaseCandidateState.REJECTED:
                connection.rollback()
                raise ReleaseCandidateError("rejected candidate is terminal")
            expected_index = int(
                connection.execute(
                    """
                    SELECT count(*) FROM release_gate_decisions
                    WHERE release_id = ? AND passed = 1
                    """,
                    (release_id,),
                ).fetchone()[0]
            )
            if gate is not _GATES[expected_index]:
                connection.rollback()
                raise ReleaseCandidateError("release gates must be evaluated in G0-G7 order")
            cases = connection.execute(
                "SELECT * FROM release_applicable_tests WHERE release_id = ? AND gate = ?",
                (release_id, gate.value),
            ).fetchall()
            reasons: list[str] = []
            if not cases:
                reasons.append("APPLICABLE_TEST_SET_EMPTY")
            for case in cases:
                results = connection.execute(
                    "SELECT * FROM release_test_results WHERE release_id = ? AND test_case_id = ?",
                    (release_id, case["test_case_id"]),
                ).fetchall()
                fresh = [
                    row
                    for row in results
                    if row["applicability_fingerprint"] == case["applicability_fingerprint"]
                ]
                prefix = str(case["test_case_id"])
                if not fresh:
                    reasons.append(
                        f"{prefix}:STALE_EVIDENCE" if results else f"{prefix}:OPEN_NOT_RUN"
                    )
                    continue
                bad = sorted({str(row["outcome"]) for row in fresh if row["outcome"] != "PASSED"})
                if bad:
                    reasons.extend(f"{prefix}:{outcome}" for outcome in bad)
                if not any(row["outcome"] == "PASSED" for row in fresh):
                    reasons.append(f"{prefix}:NO_FRESH_PASS")
            passed = not reasons
            decision_revision = expected_index + 1
            connection.execute(
                "INSERT INTO release_gate_decisions VALUES (?,?,?,?,?,?)",
                (
                    release_id,
                    gate.value,
                    int(passed),
                    json.dumps(reasons, separators=(",", ":")),
                    decision_revision,
                    self._now(),
                ),
            )
            if not passed:
                self._transition(
                    connection,
                    release_id,
                    ReleaseCandidateState.REJECTED,
                    f"{gate.value}_failed",
                )
            elif gate is ReleaseGate.G6:
                self._transition(
                    connection,
                    release_id,
                    ReleaseCandidateState.READY_FOR_FOUNDER,
                    "G0_G6_passed",
                )
            elif gate is ReleaseGate.G7:
                self._transition(
                    connection,
                    release_id,
                    ReleaseCandidateState.APPROVED_FOR_SIGNING,
                    "G0_G7_passed",
                )
            connection.commit()
            return GateDecision(release_id, gate, passed, tuple(reasons), decision_revision)

    def approved_signing_candidate(self, release_id: str) -> ApprovedReleaseCandidate:
        with self._connection() as connection:
            state = self._state_row(connection, release_id)
            if state["state"] != ReleaseCandidateState.APPROVED_FOR_SIGNING:
                raise ReleaseCandidateError("candidate has not passed G0-G7")
            row = connection.execute(
                "SELECT * FROM release_candidates WHERE release_id = ?", (release_id,)
            ).fetchone()
            assert row is not None
            return ApprovedReleaseCandidate(
                release_id,
                str(row["unsigned_payload_sha256"]),
                str(row["runtime_sbom_sha256"]),
                str(row["build_sbom_sha256"]),
                self._gate_evidence_hash(connection, release_id),
            )

    def candidate_for_payload(self, candidate_sha256: str) -> tuple[str, str, str] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT candidate.release_id, candidate.release_manifest_sha256, state.state
                FROM release_candidates AS candidate
                JOIN release_candidate_states AS state USING(release_id)
                WHERE candidate.unsigned_payload_sha256 = ?
                """,
                (candidate_sha256,),
            ).fetchone()
        if row is None:
            return None
        return str(row["release_id"]), str(row["release_manifest_sha256"]), str(row["state"])

    def state(self, release_id: str) -> ReleaseCandidateState:
        with self._connection() as connection:
            return ReleaseCandidateState(self._state_row(connection, release_id)["state"])

    @staticmethod
    def _candidate_tuple(row: sqlite3.Row) -> tuple[str, ...]:
        return tuple(
            str(row[key])
            for key in (
                "release_id",
                "unsigned_payload_sha256",
                "release_manifest_sha256",
                "runtime_sbom_sha256",
                "build_sbom_sha256",
                "verification_manifest_sha256",
                "source_revision",
                "applicable_set_sha256",
            )
        )

    @staticmethod
    def _definition_tuple(
        candidate: ReleaseCandidateDefinition, applicable_set_sha256: str
    ) -> tuple[str, ...]:
        return (
            candidate.release_id,
            candidate.unsigned_payload_sha256,
            candidate.release_manifest_sha256,
            candidate.runtime_sbom_sha256,
            candidate.build_sbom_sha256,
            candidate.verification_manifest_sha256,
            candidate.source_revision,
            applicable_set_sha256,
        )

    @staticmethod
    def _input_result_tuple(result: ReleaseTestResultInput) -> tuple[str, ...]:
        return (
            result.test_result_id,
            result.release_id,
            result.test_case_id,
            result.outcome.value,
            result.applicability_fingerprint,
            result.evidence_sha256,
            result.environment_sha256,
        )

    @staticmethod
    def _result_tuple(row: sqlite3.Row) -> tuple[str, ...]:
        return tuple(
            str(row[key])
            for key in (
                "test_result_id",
                "release_id",
                "test_case_id",
                "outcome",
                "applicability_fingerprint",
                "evidence_sha256",
                "environment_sha256",
            )
        )

    @staticmethod
    def _state_row(connection: sqlite3.Connection, release_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM release_candidate_states WHERE release_id = ?", (release_id,)
        ).fetchone()
        if row is None:
            raise ReleaseCandidateError("unknown Release Candidate")
        return cast(sqlite3.Row, row)

    def _transition(
        self,
        connection: sqlite3.Connection,
        release_id: str,
        state: ReleaseCandidateState,
        reason_code: str,
    ) -> None:
        current = self._state_row(connection, release_id)
        revision = int(current["state_revision"]) + 1
        now = self._now()
        connection.execute(
            "UPDATE release_candidate_states SET state = ?, state_revision = ?, updated_at = ? "
            "WHERE release_id = ?",
            (state.value, revision, now, release_id),
        )
        connection.execute(
            "INSERT INTO release_candidate_state_history VALUES (?,?,?,?,?)",
            (release_id, revision, state.value, reason_code, now),
        )

    @staticmethod
    def _gate(row: sqlite3.Row) -> GateDecision:
        return GateDecision(
            str(row["release_id"]),
            ReleaseGate(row["gate"]),
            bool(row["passed"]),
            tuple(json.loads(str(row["reason_codes_json"]))),
            int(row["decision_revision"]),
        )

    @staticmethod
    def _gate_evidence_hash(connection: sqlite3.Connection, release_id: str) -> str:
        payload = {
            "release_id": release_id,
            "gates": [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT gate, passed, reason_codes_json, decision_revision
                    FROM release_gate_decisions WHERE release_id = ?
                    ORDER BY decision_revision
                    """,
                    (release_id,),
                )
            ],
            "results": [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT test_result_id, test_case_id, outcome,
                           applicability_fingerprint, evidence_sha256,
                           environment_sha256
                    FROM release_test_results WHERE release_id = ?
                    ORDER BY test_result_id
                    """,
                    (release_id,),
                )
            ],
            "founder_acceptance": [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT founder_acceptance_id, test_case_id, scenario_id,
                           scenario_version, scenario_sha256,
                           journey_audit_sha256, decision
                    FROM founder_acceptance_receipts WHERE release_id = ?
                    ORDER BY founder_acceptance_id
                    """,
                    (release_id,),
                )
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()
