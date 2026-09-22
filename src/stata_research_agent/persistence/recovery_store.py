"""SQLite Recovery UoW: immutable report plus safe control-state convergence."""

from __future__ import annotations

import sqlite3
from typing import cast

from stata_research_agent.application.recovery import (
    RecoverOperationCommand,
    RecoveryAssessment,
    RecoveryOutcome,
)
from stata_research_agent.domain.identifiers import (
    OperationAttemptId,
    OperationId,
    RecoveryReportId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision
from stata_research_agent.domain.status import RecoveryClassification

from .atomic_commit import (
    AtomicCommitService,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
    canonical_json,
)


class SqliteRecoveryRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    def record(
        self,
        command: RecoverOperationCommand,
        assessment: RecoveryAssessment,
        report_id: RecoveryReportId,
    ) -> RecoveryOutcome:
        if command.operation_id != assessment.operation_id:
            raise ValueError("Recovery Assessment targets a different Operation")
        existing = self._by_fingerprint(assessment.input_fingerprint)
        if existing is not None:
            return self._outcome(existing, replayed=True)

        request = {
            "operation_id": command.operation_id.value,
            "assessment_schema_version": assessment.schema_version,
            "input_fingerprint": assessment.input_fingerprint,
            "classification": assessment.classification.value,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            current = connection.execute(
                """
                SELECT o.status AS operation_status, o.requested_by_turn_id,
                       t.status AS turn_status, t.execution_mode,
                       a.operation_attempt_id, a.status AS attempt_status
                FROM operations AS o
                JOIN turns AS t ON t.turn_id = o.requested_by_turn_id
                LEFT JOIN operation_attempts AS a ON a.operation_id = o.operation_id
                WHERE o.operation_id = ?
                ORDER BY a.attempt_number DESC
                LIMIT 1
                """,
                (command.operation_id.value,),
            ).fetchone()
            if current is None:
                raise ValueError("Recovery target no longer exists")
            current_attempt = (
                str(current["operation_attempt_id"])
                if current["operation_attempt_id"] is not None
                else None
            )
            expected_attempt = (
                assessment.attempt_id.value if assessment.attempt_id is not None else None
            )
            if (
                str(current["operation_status"]) != assessment.observed_operation_status
                or current_attempt != expected_attempt
                or (
                    str(current["attempt_status"])
                    if current["attempt_status"] is not None
                    else None
                )
                != assessment.observed_attempt_status
                or str(current["turn_status"]) != assessment.observed_turn_status
            ):
                raise RuntimeError("Recovery Assessment is stale; scan again")
            lane = connection.execute(
                """
                SELECT active_write_turn_id, lane_revision
                FROM workspace_write_lane WHERE singleton_id = 1
                """
            ).fetchone()
            lane_owner = (
                str(lane["active_write_turn_id"])
                if lane["active_write_turn_id"] is not None
                else None
            )
            if (
                lane_owner != assessment.observed_lane_owner_turn_id
                or int(lane["lane_revision"]) != assessment.observed_lane_revision
            ):
                raise RuntimeError("Recovery Assessment lane observation is stale; scan again")

            operation_status = {
                RecoveryClassification.COMPLETED_UNRECONCILED: "completed_unreconciled",
                RecoveryClassification.OUTCOME_UNKNOWN: "outcome_unknown",
                RecoveryClassification.INTEGRITY_VIOLATION: "integrity_violation",
            }.get(assessment.classification)
            if operation_status is not None:
                connection.execute(
                    """
                    UPDATE operations SET status = ?, terminal_revision = ?
                    WHERE operation_id = ?
                    """,
                    (operation_status, revision.value, command.operation_id.value),
                )
                if expected_attempt is not None:
                    connection.execute(
                        """
                        UPDATE operation_attempts SET status = ?, terminal_revision = ?
                        WHERE operation_attempt_id = ?
                        """,
                        (operation_status, revision.value, expected_attempt),
                    )

            turn_paused = False
            lane_released = False
            turn_id = str(current["requested_by_turn_id"])
            events: list[JournalDraft] = []
            if str(current["turn_status"]) in {"running", "waiting"}:
                if str(current["execution_mode"]) == "write" and lane_owner == turn_id:
                    cursor = connection.execute(
                        """
                        UPDATE workspace_write_lane
                        SET active_write_turn_id = NULL, lane_revision = lane_revision + 1
                        WHERE singleton_id = 1 AND active_write_turn_id = ?
                          AND lane_revision = ?
                        """,
                        (turn_id, assessment.observed_lane_revision),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("Workspace write lane changed during Recovery UoW")
                    lane_released = True
                    events.append(
                        JournalDraft(
                            "workspace.write_lane.released",
                            "turn",
                            turn_id,
                            {
                                "reason": "recovery_safe_convergence",
                                "expected_lane_revision": assessment.observed_lane_revision,
                                "new_lane_revision": assessment.observed_lane_revision + 1,
                            },
                        )
                    )
                connection.execute(
                    """
                    UPDATE turns SET status = 'paused', turn_revision = turn_revision + 1
                    WHERE turn_id = ?
                    """,
                    (turn_id,),
                )
                turn_paused = True
                events.append(
                    JournalDraft(
                        "turn.paused",
                        "turn",
                        turn_id,
                        {"reason": "recovery_safe_convergence"},
                    )
                )

            connection.execute(
                """
                UPDATE tool_resource_leases
                SET lease_status = 'released', released_revision = ?
                WHERE lease_status = 'active' AND tool_admission_id IN (
                    SELECT tool_admission_id FROM tool_admissions WHERE operation_id = ?
                )
                """,
                (revision.value, command.operation_id.value),
            )
            connection.execute(
                """
                INSERT INTO recovery_reports VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    report_id.value,
                    assessment.schema_version,
                    command.operation_id.value,
                    expected_attempt,
                    turn_id,
                    assessment.classification.value,
                    assessment.observed_operation_status,
                    assessment.observed_attempt_status,
                    assessment.observed_turn_status,
                    assessment.observed_lane_owner_turn_id,
                    assessment.observed_lane_revision,
                    assessment.observed_journal_boundary,
                    assessment.manifest_id,
                    canonical_json(assessment.manifest_verification),
                    canonical_json(assessment.artifact_verification),
                    assessment.reason,
                    canonical_json(list(assessment.blocked_actions)),
                    canonical_json(list(assessment.available_user_actions)),
                    int(turn_paused),
                    int(lane_released),
                    assessment.input_fingerprint,
                    revision.value,
                ),
            )
            response = {
                "recovery_report_id": report_id.value,
                "operation_id": command.operation_id.value,
                "attempt_id": expected_attempt,
                "requested_by_turn_id": turn_id,
                "classification": assessment.classification.value,
                "turn_status": "paused" if turn_paused else str(current["turn_status"]),
                "turn_paused": turn_paused,
                "lane_released": lane_released,
                "input_fingerprint": assessment.input_fingerprint,
            }
            events.insert(
                0,
                JournalDraft(
                    "recovery.classified",
                    "recovery_report",
                    report_id.value,
                    response,
                ),
            )
            return MutationPayload(
                response,
                tuple(events),
                (OutboxDraft("recovery.attention_required", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="recovery.record",
            request=request,
            mutation=mutate,
        )
        row = self._by_fingerprint(assessment.input_fingerprint)
        if row is None:
            raise RuntimeError("Recovery Report commit was not observable")
        return self._outcome(row, replayed=receipt.replayed)

    def _by_fingerprint(self, fingerprint: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            self._connection.execute(
                """
            SELECT r.*, t.status AS current_turn_status
            FROM recovery_reports AS r
            JOIN turns AS t ON t.turn_id = r.requested_by_turn_id
            WHERE r.input_fingerprint = ?
            """,
                (fingerprint,),
            ).fetchone(),
        )

    @staticmethod
    def _outcome(row: sqlite3.Row, *, replayed: bool) -> RecoveryOutcome:
        attempt = (
            OperationAttemptId(str(row["operation_attempt_id"]))
            if row["operation_attempt_id"] is not None
            else None
        )
        return RecoveryOutcome(
            RecoveryReportId(str(row["recovery_report_id"])),
            OperationId(str(row["operation_id"])),
            attempt,
            TurnId(str(row["requested_by_turn_id"])),
            RecoveryClassification(str(row["classification"])),
            str(row["current_turn_status"]),
            bool(row["lane_released"]),
            WorkspaceRevision(int(row["created_revision"])),
            replayed,
        )
