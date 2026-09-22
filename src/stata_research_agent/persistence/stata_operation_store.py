"""SQLite implementation of Handoff and Completion Finalization UoWs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from stata_research_agent.application.stata_operation import (
    ArtifactOutputExpectation,
    ExecuteStataCommand,
    FormalSessionDataBinding,
    PlannedArtifactCandidate,
    PublishedArtifactCandidate,
    StataOperationHandle,
    StataOperationOutcome,
)
from stata_research_agent.domain.identifiers import (
    ArtifactCandidateId,
    ArtifactCapturePlanId,
    ArtifactId,
    ArtifactLocationId,
    ArtifactPromotionId,
    ArtifactStateObservationId,
    CommandId,
    CompletionManifestId,
    DataVersionId,
    ExecutableSourceId,
    OperationAttemptId,
    OperationId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision
from stata_research_agent.domain.serialization import to_primitive
from stata_research_agent.domain.stata_execution import (
    StataExecutionStatus,
    StataRuntimeResult,
)

from .atomic_commit import (
    AtomicCommitService,
    CrashInjector,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
    canonical_json,
)


class SqliteStataOperationRepository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        finalization_crash_injector: CrashInjector | None = None,
    ) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)
        self._finalization_crash_injector = finalization_crash_injector

    @staticmethod
    def _request(command: ExecuteStataCommand) -> dict[str, object]:
        return {
            "requested_by_turn_id": command.requested_by_turn_id.value,
            "tool_call_id": (
                command.tool_call_id.value if command.tool_call_id is not None else None
            ),
            "admitted_operation_id": (
                command.admitted_operation_id.value
                if command.admitted_operation_id is not None
                else None
            ),
            "session_id": command.session_id,
            "code": command.code,
            "timeout_seconds": command.timeout_seconds,
            "expected_outputs": [
                {
                    "output_slot": output.output_slot,
                    "relative_staging_path": output.relative_staging_path,
                    "artifact_kind": output.artifact_kind,
                    "media_type": output.media_type,
                    "required": output.required,
                }
                for output in command.expected_outputs
            ],
            "input_data_version_id": (
                command.input_data_version_id.value
                if command.input_data_version_id is not None
                else None
            ),
            "input_data_slot_key": command.input_data_slot_key,
            "input_verification_receipt_id": (
                command.input_verification_receipt_id.value
                if command.input_verification_receipt_id is not None
                else None
            ),
            "execution_purpose": command.execution_purpose,
            "source_data_state_operation_id": (
                command.source_data_state_operation_id.value
                if command.source_data_state_operation_id is not None
                else None
            ),
            "expected_data_state_token": command.expected_data_state_token,
            "expected_session_generation": command.expected_session_generation,
        }

    def handoff(
        self,
        command: ExecuteStataCommand,
        *,
        handoff_command_id: CommandId,
        operation_id: OperationId,
        attempt_id: OperationAttemptId,
        capture_plan_id: ArtifactCapturePlanId,
        planned_candidates: tuple[PlannedArtifactCandidate, ...],
        executable_source_id: ExecutableSourceId,
    ) -> StataOperationHandle:
        existing = self._connection.execute(
            """
            SELECT r.operation_id, r.operation_attempt_id, o.status,
                   p.artifact_capture_plan_id, b.executable_source_id
            FROM stata_operation_requests AS r
            JOIN operations AS o ON o.operation_id = r.operation_id
            LEFT JOIN artifact_capture_plans AS p
              ON p.operation_attempt_id = r.operation_attempt_id
            LEFT JOIN stata_operation_input_bindings AS b
              ON b.operation_attempt_id = r.operation_attempt_id
            WHERE r.request_command_id = ?
            """,
            (command.command_id.value,),
        ).fetchone()
        if existing is not None:
            existing_planned = self._load_planned_candidates(
                str(existing["artifact_capture_plan_id"])
            )
            return StataOperationHandle(
                OperationId(str(existing["operation_id"])),
                OperationAttemptId(str(existing["operation_attempt_id"])),
                str(existing["status"]),
                True,
                ArtifactCapturePlanId(str(existing["artifact_capture_plan_id"])),
                existing_planned,
                ExecutableSourceId(str(existing["executable_source_id"])),
            )

        request = self._request(command)

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            if command.admitted_operation_id is None:
                connection.execute(
                    """
                    INSERT INTO operations(
                        operation_id, operation_kind, requested_by_turn_id, tool_call_id,
                        status, idempotency_key, created_revision, terminal_revision
                    ) VALUES (?, 'stata.execute', ?, ?, 'handoff_committed', ?, ?, NULL)
                    """,
                    (
                        operation_id.value,
                        command.requested_by_turn_id.value,
                        None,
                        command.command_id.value,
                        revision.value,
                    ),
                )
            else:
                assert command.tool_call_id is not None
                cursor = connection.execute(
                    """
                    UPDATE operations
                    SET status = 'handoff_committed', idempotency_key = ?
                    WHERE operation_id = ?
                      AND operation_kind = 'stata.execute'
                      AND requested_by_turn_id = ?
                      AND tool_call_id = ?
                      AND status = 'admitted'
                    """,
                    (
                        command.command_id.value,
                        operation_id.value,
                        command.requested_by_turn_id.value,
                        command.tool_call_id.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Stata execution target is not the admitted Tool Operation")
            connection.execute(
                """
                INSERT INTO operation_attempts(
                    operation_attempt_id, operation_id, attempt_number,
                    session_generation, status, created_revision, terminal_revision
                ) VALUES (?, ?, 1, NULL, 'handoff_committed', ?, NULL)
                """,
                (attempt_id.value, operation_id.value, revision.value),
            )
            if command.input_data_version_id is not None:
                assert command.input_verification_receipt_id is not None
                verified = connection.execute(
                    """
                    SELECT al.managed_handle
                    FROM data_versions AS d
                    JOIN artifact_verification_receipts AS v
                      ON v.artifact_id = d.canonical_artifact_id
                    JOIN artifact_locations AS al
                      ON al.artifact_id = d.canonical_artifact_id
                    WHERE d.data_version_id = ?
                      AND v.verification_receipt_id = ?
                      AND v.verification_purpose = 'formal_run_input'
                      AND v.verdict = 'verified'
                    """,
                    (
                        command.input_data_version_id.value,
                        command.input_verification_receipt_id.value,
                    ),
                ).fetchone()
                if verified is None:
                    raise ValueError("formal Stata input requires a verified Data Version receipt")
                if command.execution_purpose == "data_load":
                    expected_load = f'use "{verified["managed_handle"]}", clear'
                    if command.code.strip() != expected_load:
                        raise ValueError(
                            "data_load must use the exact managed Data Version payload"
                        )
                if command.execution_purpose in {
                    "data_step",
                    "formal_estimation",
                    "formal_post_estimation",
                }:
                    assert command.source_data_state_operation_id is not None
                    source_state = connection.execute(
                        """
                        SELECT json_extract(m.receipt_json, '$.data_signature')
                                   AS data_signature,
                               json_extract(m.receipt_json, '$.exec_seq') AS exec_seq,
                               m.session_generation,
                               b.execution_purpose,
                               EXISTS (
                                   SELECT 1
                                   FROM stata_runs AS source_run
                                   JOIN results AS source_result
                                     ON source_result.producing_stata_run_id =
                                        source_run.stata_run_id
                                   WHERE source_run.operation_id = o.operation_id
                               ) AS has_formal_result
                        FROM operations AS o
                        JOIN operation_attempts AS a ON a.operation_id = o.operation_id
                        JOIN completion_manifests AS m
                          ON m.operation_attempt_id = a.operation_attempt_id
                        JOIN stata_operation_requests AS r ON r.operation_id = o.operation_id
                        JOIN stata_operation_input_bindings AS b
                          ON b.operation_attempt_id = a.operation_attempt_id
                        WHERE o.operation_id = ? AND o.status = 'completed'
                          AND m.execution_status = 'succeeded'
                          AND r.session_id = ?
                          AND b.execution_purpose IN (
                              'data_load', 'data_step', 'formal_estimation',
                              'formal_post_estimation'
                          )
                          AND b.input_data_version_id = ?
                        """,
                        (
                            command.source_data_state_operation_id.value,
                            command.session_id,
                            command.input_data_version_id.value,
                        ),
                    ).fetchone()
                    if (
                        source_state is None
                        or str(source_state["data_signature"]) != command.expected_data_state_token
                        or int(source_state["session_generation"])
                        != command.expected_session_generation
                    ):
                        raise ValueError(
                            f"{command.execution_purpose} session state does not match "
                            "its declared predecessor"
                        )
                    if command.execution_purpose == "formal_post_estimation" and (
                        str(source_state["execution_purpose"]) != "formal_estimation"
                        or int(source_state["has_formal_result"]) != 1
                        or source_state["exec_seq"] is None
                    ):
                        raise ValueError(
                            "formal_post_estimation requires an immediately preceding "
                            "promoted formal estimation"
                        )
            connection.execute(
                """
                INSERT INTO stata_operation_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id.value,
                    command.command_id.value,
                    attempt_id.value,
                    command.session_id,
                    command.code,
                    hashlib.sha256(command.code.encode("utf-8")).hexdigest(),
                    command.timeout_seconds,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO artifact_capture_plans VALUES (?, ?, 1, 'reject', ?)
                """,
                (capture_plan_id.value, attempt_id.value, revision.value),
            )
            command_sha256 = hashlib.sha256(command.code.encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT INTO executable_sources VALUES (
                    ?, ?, 'direct_command', ?, ?, ?
                )
                """,
                (
                    executable_source_id.value,
                    attempt_id.value,
                    command.code,
                    command_sha256,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO stata_operation_input_bindings VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    attempt_id.value,
                    executable_source_id.value,
                    command.execution_purpose,
                    command.input_data_version_id.value
                    if command.input_data_version_id is not None
                    else None,
                    command.input_data_slot_key,
                    command.input_verification_receipt_id.value
                    if command.input_verification_receipt_id is not None
                    else None,
                    command.source_data_state_operation_id.value
                    if command.source_data_state_operation_id is not None
                    else None,
                    command.expected_data_state_token,
                    command.expected_session_generation,
                    revision.value,
                ),
            )
            for planned in planned_candidates:
                expectation = planned.expectation
                connection.execute(
                    """
                    INSERT INTO artifact_capture_plan_outputs VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        planned.candidate_id.value,
                        planned.artifact_id.value,
                        planned.promotion_id.value,
                        planned.state_observation_id.value,
                        planned.location_id.value,
                        capture_plan_id.value,
                        expectation.output_slot,
                        expectation.relative_staging_path,
                        expectation.artifact_kind,
                        expectation.media_type,
                        int(expectation.required),
                    ),
                )
            response = {
                "operation_id": operation_id.value,
                "attempt_id": attempt_id.value,
                "status": "handoff_committed",
            }
            return MutationPayload(
                response=response,
                journal=(
                    JournalDraft(
                        "tool.handoff_committed",
                        "operation",
                        operation_id.value,
                        {"attempt_id": attempt_id.value, "session_id": command.session_id},
                    ),
                ),
                outbox=(OutboxDraft("operation.changed", response),),
            )

        self._commits.commit_mutation(
            command_id=handoff_command_id,
            command_type="stata.operation.handoff",
            request=request,
            mutation=mutate,
        )
        return StataOperationHandle(
            operation_id,
            attempt_id,
            "handoff_committed",
            False,
            capture_plan_id,
            planned_candidates,
            executable_source_id,
        )

    def _load_planned_candidates(
        self, capture_plan_id: str
    ) -> tuple[PlannedArtifactCandidate, ...]:
        rows = self._connection.execute(
            """
            SELECT artifact_candidate_id, reserved_artifact_id,
                   reserved_promotion_id, reserved_state_observation_id,
                   reserved_location_id, output_slot, relative_staging_path,
                   artifact_kind, media_type, required
            FROM artifact_capture_plan_outputs
            WHERE artifact_capture_plan_id = ?
            ORDER BY output_slot
            """,
            (capture_plan_id,),
        ).fetchall()
        return tuple(
            PlannedArtifactCandidate(
                ArtifactCandidateId(str(row["artifact_candidate_id"])),
                ArtifactId(str(row["reserved_artifact_id"])),
                ArtifactPromotionId(str(row["reserved_promotion_id"])),
                ArtifactStateObservationId(str(row["reserved_state_observation_id"])),
                ArtifactLocationId(str(row["reserved_location_id"])),
                ArtifactOutputExpectation(
                    output_slot=str(row["output_slot"]),
                    relative_staging_path=str(row["relative_staging_path"]),
                    artifact_kind=str(row["artifact_kind"]),
                    media_type=str(row["media_type"]),
                    required=bool(row["required"]),
                ),
            )
            for row in rows
        )

    def existing_outcome(
        self, command: ExecuteStataCommand, handle: StataOperationHandle
    ) -> StataOperationOutcome:
        receipt = self._connection.execute(
            "SELECT response_json, commit_revision FROM command_receipts WHERE command_id = ?",
            (command.command_id.value,),
        ).fetchone()
        if receipt is None:
            handoff = self._connection.execute(
                "SELECT handoff_revision FROM stata_operation_requests WHERE operation_id = ?",
                (handle.operation_id.value,),
            ).fetchone()
            if handoff is None:
                raise RuntimeError("Stata operation request disappeared after handoff")
            # Recovery verification/reconciliation is a separate explicit command. Merely
            # observing a prior committed handoff must neither re-execute nor mutate it.
            return StataOperationOutcome(
                operation_id=handle.operation_id,
                attempt_id=handle.attempt_id,
                status=handle.status,
                execution_status=None,
                manifest_id=None,
                commit_revision=WorkspaceRevision(int(handoff["handoff_revision"])),
                replayed=True,
            )
        response = json.loads(str(receipt["response_json"]))
        return self._outcome(
            response,
            commit_revision=int(receipt["commit_revision"]),
            replayed=True,
        )

    def finalize(
        self,
        command: ExecuteStataCommand,
        handle: StataOperationHandle,
        result: StataRuntimeResult,
        *,
        manifest_id: CompletionManifestId,
        published_candidates: tuple[PublishedArtifactCandidate, ...],
    ) -> StataOperationOutcome:
        receipt = to_primitive(result.receipt)
        status = result.receipt.execution_status
        if status is StataExecutionStatus.SUCCEEDED:
            operation_status = "completed"
            event_type = "tool.completed"
        elif status is StataExecutionStatus.COMMAND_FAILED or (
            result.receipt.session_reset
            and not command.expected_outputs
            and not published_candidates
            and not bool(result.receipt.supervision_proof.get("scope_abort_requested"))
        ):
            # A supervised local failure with a reset session and no controlled output has
            # reached a durable negative outcome.  The failed code and receipt remain in Trace,
            # but there is no external Result/Artifact to reconcile before the Agent may retry.
            operation_status = "failed"
            event_type = "tool.failed"
        else:
            operation_status = "outcome_unknown"
            event_type = "tool.interrupted"
        request = {
            **self._request(command),
            "operation_id": handle.operation_id.value,
            "attempt_id": handle.attempt_id.value,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            observed_at = datetime.now(UTC).isoformat()
            connection.execute(
                """
                INSERT INTO completion_manifests VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    manifest_id.value,
                    handle.operation_id.value,
                    handle.attempt_id.value,
                    result.envelope_schema_version,
                    result.receipt.schema_version,
                    result.receipt.executor_instance_id,
                    result.receipt.session_id,
                    result.receipt.session_generation,
                    status.value,
                    result.receipt.rc,
                    result.receipt.raw_output_status,
                    result.receipt.structured_result_status,
                    canonical_json(receipt),
                    canonical_json(dict(result.structured))
                    if result.structured is not None
                    else None,
                    result.text,
                    revision.value,
                ),
            )
            for candidate in published_candidates:
                connection.execute(
                    """
                    INSERT INTO completion_manifest_artifacts VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'captured', ?
                    )
                    """,
                    (
                        candidate.candidate_id.value,
                        manifest_id.value,
                        handle.attempt_id.value,
                        candidate.output_slot,
                        candidate.relative_staging_path,
                        int(candidate.expected),
                        candidate.artifact_kind,
                        candidate.media_type,
                        candidate.size_bytes,
                        candidate.sha256,
                        candidate.producer_locator,
                        revision.value,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO artifacts VALUES (
                        ?, ?, ?, ?, 'sha256', ?, ?, 'artifact_candidate', ?
                    )
                    """,
                    (
                        candidate.artifact_id.value,
                        candidate.artifact_kind,
                        candidate.media_type,
                        candidate.size_bytes,
                        candidate.sha256,
                        handle.attempt_id.value,
                        revision.value,
                    ),
                )
                connection.execute(
                    "INSERT INTO artifact_candidate_sources VALUES (?, ?)",
                    (candidate.artifact_id.value, candidate.candidate_id.value),
                )
                connection.execute(
                    """
                    INSERT INTO artifact_state_history VALUES (
                        ?, ?, 'available', 'candidate_promotion_verified', ?, ?, ?, ?
                    )
                    """,
                    (
                        candidate.state_observation_id.value,
                        candidate.artifact_id.value,
                        candidate.size_bytes,
                        candidate.sha256,
                        observed_at,
                        revision.value,
                    ),
                )
                connection.execute(
                    "INSERT INTO artifact_states VALUES (?, ?, 'available', ?, ?)",
                    (
                        candidate.artifact_id.value,
                        candidate.state_observation_id.value,
                        observed_at,
                        revision.value,
                    ),
                )
                connection.execute(
                    "INSERT INTO artifact_location_history VALUES (?, ?, 1, 'installed', ?, ?)",
                    (
                        candidate.location_id.value,
                        candidate.artifact_id.value,
                        candidate.managed_handle,
                        revision.value,
                    ),
                )
                connection.execute(
                    "INSERT INTO artifact_locations VALUES (?, ?, 1, ?, ?)",
                    (
                        candidate.artifact_id.value,
                        candidate.location_id.value,
                        candidate.managed_handle,
                        revision.value,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO artifact_promotions VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate.promotion_id.value,
                        handle.attempt_id.value,
                        candidate.candidate_id.value,
                        candidate.artifact_id.value,
                        candidate.managed_handle,
                        command.command_id.value,
                        revision.value,
                    ),
                )
            connection.execute(
                """
                UPDATE operations SET status = ?, terminal_revision = ?
                WHERE operation_id = ?
                  AND status IN ('handoff_committed', 'completed_unreconciled')
                """,
                (operation_status, revision.value, handle.operation_id.value),
            )
            connection.execute(
                """
                UPDATE operation_attempts
                SET status = ?, session_generation = ?, terminal_revision = ?
                WHERE operation_attempt_id = ?
                  AND status IN ('handoff_committed', 'completed_unreconciled')
                """,
                (
                    operation_status,
                    result.receipt.session_generation,
                    revision.value,
                    handle.attempt_id.value,
                ),
            )
            tool_payload: dict[str, Any] = {
                "operation_id": handle.operation_id.value,
                "operation_attempt_id": handle.attempt_id.value,
                "completion_manifest_id": manifest_id.value,
                "execution_status": status.value,
                "structured": dict(result.structured)
                if result.structured is not None
                else None,
            }
            if result.structured is None or command.execution_purpose == "data_step":
                # Diagnostic and exploratory Stata calls often use display/list/tab output
                # whose meaning is not represented by the final r()/e() catalog. Preserve
                # the full output in the Completion Manifest, but give the Agent a bounded
                # tail so it can see the actual diagnostic result instead of repeating the
                # same command blindly. Formal result Calls stay catalog-only to keep their
                # evidence context compact and unambiguous.
                tool_payload["raw_output_excerpt"] = result.text[-8_000:]
            self._resolve_broker_call(
                connection,
                revision,
                command,
                handle,
                result_kind="success" if operation_status == "completed" else "error",
                summary=(
                    "Stata execution completed"
                    if operation_status == "completed"
                    else f"Stata execution ended as {operation_status}"
                ),
                structured_payload=tool_payload,
                artifact_ids=tuple(
                    candidate.artifact_id.value for candidate in published_candidates
                ),
            )
            response = {
                "operation_id": handle.operation_id.value,
                "attempt_id": handle.attempt_id.value,
                "status": operation_status,
                "execution_status": status.value,
                "manifest_id": manifest_id.value,
            }
            artifact_events = tuple(
                JournalDraft(
                    "artifact.promoted",
                    "artifact",
                    candidate.artifact_id.value,
                    {"artifact_candidate_id": candidate.candidate_id.value},
                )
                for candidate in published_candidates
            )
            return MutationPayload(
                response=response,
                journal=(
                    JournalDraft(
                        event_type,
                        "operation",
                        handle.operation_id.value,
                        {
                            "attempt_id": handle.attempt_id.value,
                            "manifest_id": manifest_id.value,
                            "execution_status": status.value,
                        },
                    ),
                )
                + artifact_events,
                outbox=(OutboxDraft("operation.changed", response),),
            )

        committed = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="stata.operation.finalize",
            request=request,
            mutation=mutate,
            crash_injector=self._finalization_crash_injector,
        )
        return self._outcome(
            committed.response,
            commit_revision=committed.commit_revision.value,
            replayed=committed.replayed,
        )

    def mark_transport_unknown(
        self,
        command: ExecuteStataCommand,
        handle: StataOperationHandle,
        *,
        error_kind: str,
        error_detail: str,
    ) -> StataOperationOutcome:
        request = {
            **self._request(command),
            "operation_id": handle.operation_id.value,
            "attempt_id": handle.attempt_id.value,
            "transport_error_kind": error_kind,
            "transport_error_detail": error_detail,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            for table, id_column, identity in (
                ("operations", "operation_id", handle.operation_id.value),
                (
                    "operation_attempts",
                    "operation_attempt_id",
                    handle.attempt_id.value,
                ),
            ):
                connection.execute(
                    f"UPDATE {table} SET status = 'outcome_unknown', terminal_revision = ? "
                    f"WHERE {id_column} = ? AND status = 'handoff_committed'",
                    (revision.value, identity),
                )
            self._resolve_broker_call(
                connection,
                revision,
                command,
                handle,
                result_kind="error",
                summary="Stata transport outcome is unknown",
                structured_payload={
                    "operation_id": handle.operation_id.value,
                    "operation_attempt_id": handle.attempt_id.value,
                    "recovery_classification": "outcome_unknown",
                    "error_kind": error_kind,
                    "error_detail": error_detail,
                },
                artifact_ids=(),
            )
            response = {
                "operation_id": handle.operation_id.value,
                "attempt_id": handle.attempt_id.value,
                "status": "outcome_unknown",
                "execution_status": None,
                "manifest_id": None,
            }
            return MutationPayload(
                response=response,
                journal=(
                    JournalDraft(
                        "tool.interrupted",
                        "operation",
                        handle.operation_id.value,
                        {
                            "attempt_id": handle.attempt_id.value,
                            "transport_error_kind": error_kind,
                            "transport_error_detail": error_detail,
                        },
                    ),
                ),
                outbox=(OutboxDraft("operation.changed", response),),
            )

        committed = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="stata.operation.transport_unknown",
            request=request,
            mutation=mutate,
        )
        return self._outcome(
            committed.response,
            commit_revision=committed.commit_revision.value,
            replayed=committed.replayed,
        )

    @staticmethod
    def _resolve_broker_call(
        connection: sqlite3.Connection,
        revision: WorkspaceRevision,
        command: ExecuteStataCommand,
        handle: StataOperationHandle,
        *,
        result_kind: str,
        summary: str,
        structured_payload: dict[str, Any],
        artifact_ids: tuple[str, ...],
    ) -> None:
        if command.tool_call_id is None:
            return
        payload_json = canonical_json(structured_payload)
        tool_result_id = f"toolresult_{handle.operation_id.value[3:]}"
        connection.execute(
            """
            INSERT INTO canonical_tool_results VALUES (
                ?, ?, ?, '1', ?, ?, ?, ?, 0, NULL, ?, ?
            )
            """,
            (
                tool_result_id,
                command.tool_call_id.value,
                result_kind,
                summary,
                payload_json,
                canonical_json(list(artifact_ids)),
                canonical_json([handle.operation_id.value]),
                hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                revision.value,
            ),
        )
        connection.execute(
            "UPDATE tool_calls SET proposal_status = 'resolved' WHERE tool_call_id = ?",
            (command.tool_call_id.value,),
        )
        ordinal = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(status_ordinal), 0) + 1
                FROM tool_call_status_history WHERE tool_call_id = ?
                """,
                (command.tool_call_id.value,),
            ).fetchone()[0]
        )
        connection.execute(
            "INSERT INTO tool_call_status_history VALUES (?, ?, 'resolved', ?, ?)",
            (
                command.tool_call_id.value,
                ordinal,
                result_kind,
                revision.value,
            ),
        )
        connection.execute(
            """
            UPDATE tool_resource_leases
            SET lease_status = 'released', released_revision = ?
            WHERE tool_admission_id = (
                SELECT tool_admission_id FROM tool_admissions WHERE operation_id = ?
            ) AND lease_status = 'active'
            """,
            (revision.value, handle.operation_id.value),
        )

    def authorize_reconciliation(
        self,
        command: ExecuteStataCommand,
        handle: StataOperationHandle,
        *,
        authorization_command_id: CommandId,
        authorized_by_turn_id: TurnId,
    ) -> None:
        request = {
            "operation_id": handle.operation_id.value,
            "attempt_id": handle.attempt_id.value,
            "authorized_by_turn_id": authorized_by_turn_id.value,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            continuation = connection.execute(
                """
                SELECT 1
                FROM turn_continuations AS continuation
                JOIN recovery_reports AS report
                  ON report.requested_by_turn_id = continuation.predecessor_turn_id
                WHERE continuation.successor_turn_id = ?
                  AND continuation.predecessor_turn_id = ?
                  AND continuation.relation_kind = 'recovery_continuation'
                  AND report.operation_id = ?
                  AND report.operation_attempt_id = ?
                  AND report.classification = 'completed_unreconciled'
                LIMIT 1
                """,
                (
                    authorized_by_turn_id.value,
                    command.requested_by_turn_id.value,
                    handle.operation_id.value,
                    handle.attempt_id.value,
                ),
            ).fetchone()
            if continuation is None:
                raise ValueError(
                    "reconciliation requires a Recovery Report and recovery successor Turn"
                )
            connection.execute(
                """
                INSERT INTO operation_reconciliation_authorizations
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    authorization_command_id.value,
                    handle.operation_id.value,
                    handle.attempt_id.value,
                    authorized_by_turn_id.value,
                    revision.value,
                ),
            )
            return MutationPayload(
                response=request,
                journal=(
                    JournalDraft(
                        "reconciliation.authorized",
                        "operation",
                        handle.operation_id.value,
                        request,
                    ),
                ),
                outbox=(OutboxDraft("operation.changed", request),),
            )

        self._commits.commit_mutation(
            command_id=authorization_command_id,
            command_type="stata.operation.reconciliation.authorize",
            request=request,
            mutation=mutate,
        )

    def session_data_binding(self, operation_id: OperationId) -> FormalSessionDataBinding:
        row = self._connection.execute(
            """
            SELECT b.input_data_version_id, r.session_id,
                   m.session_generation,
                   json_extract(m.receipt_json, '$.data_signature') AS data_signature
            FROM operations AS o
            JOIN operation_attempts AS a ON a.operation_id = o.operation_id
            JOIN stata_operation_input_bindings AS b
              ON b.operation_attempt_id = a.operation_attempt_id
            JOIN stata_operation_requests AS r ON r.operation_id = o.operation_id
            JOIN completion_manifests AS m
              ON m.operation_attempt_id = a.operation_attempt_id
            WHERE o.operation_id = ? AND o.status = 'completed'
              AND b.execution_purpose IN (
                  'data_load', 'data_step', 'formal_estimation',
                  'formal_post_estimation'
              )
              AND m.execution_status = 'succeeded'
            """,
            (operation_id.value,),
        ).fetchone()
        if row is None or row["data_signature"] is None:
            raise ValueError("operation is not a completed formal Stata data state")
        return FormalSessionDataBinding(
            operation_id,
            DataVersionId(str(row["input_data_version_id"])),
            str(row["session_id"]),
            int(row["session_generation"]),
            str(row["data_signature"]),
        )

    @staticmethod
    def _outcome(response: Any, *, commit_revision: int, replayed: bool) -> StataOperationOutcome:
        execution = response.get("execution_status")
        manifest = response.get("manifest_id")
        return StataOperationOutcome(
            operation_id=OperationId(str(response["operation_id"])),
            attempt_id=OperationAttemptId(str(response["attempt_id"])),
            status=str(response["status"]),
            execution_status=(
                StataExecutionStatus(str(execution)) if execution is not None else None
            ),
            manifest_id=CompletionManifestId(str(manifest)) if manifest else None,
            commit_revision=WorkspaceRevision(commit_revision),
            replayed=replayed,
        )
