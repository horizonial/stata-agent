"""SQLite handoff/finalization for admitted application Tool Operations."""

from __future__ import annotations

import hashlib
import sqlite3

from stata_research_agent.application.broker_execution import (
    BeginBrokerExecutionCommand,
    BrokerExecutionHandle,
    BrokerExecutionOutcome,
    CompleteBrokerExecutionCommand,
)
from stata_research_agent.domain.identifiers import (
    OperationAttemptId,
    OperationId,
    ToolCallId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

from .atomic_commit import (
    AtomicCommitService,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
    canonical_json,
)


class SqliteBrokerExecutionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    def begin(
        self, command: BeginBrokerExecutionCommand, attempt_id: OperationAttemptId
    ) -> BrokerExecutionHandle:
        request = {
            "turn_id": command.turn_id.value,
            "tool_call_id": command.tool_call_id.value,
            "operation_id": command.operation_id.value,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            cursor = connection.execute(
                """
                UPDATE operations SET status = 'handoff_committed', idempotency_key = ?
                WHERE operation_id = ? AND requested_by_turn_id = ?
                  AND tool_call_id = ? AND status = 'admitted'
                """,
                (
                    command.command_id.value,
                    command.operation_id.value,
                    command.turn_id.value,
                    command.tool_call_id.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("application Tool target is not an admitted Operation")
            connection.execute(
                """
                INSERT INTO operation_attempts
                VALUES (?, ?, 1, NULL, 'handoff_committed', ?, NULL)
                """,
                (attempt_id.value, command.operation_id.value, revision.value),
            )
            response = {**request, "attempt_id": attempt_id.value}
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "tool.handoff_committed",
                        "operation",
                        command.operation_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("operation.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="application_tool.handoff",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return BrokerExecutionHandle(
            OperationId(str(response["operation_id"])),
            OperationAttemptId(str(response["attempt_id"])),
            ToolCallId(str(response["tool_call_id"])),
            receipt.replayed,
        )

    def complete(self, command: CompleteBrokerExecutionCommand) -> BrokerExecutionOutcome:
        handle = command.handle
        request = {
            "operation_id": handle.operation_id.value,
            "attempt_id": handle.attempt_id.value,
            "success": command.success,
            "summary": command.summary,
            "payload": dict(command.payload),
            "artifact_references": list(command.artifact_references),
            "execution_receipt": (
                None if command.execution_receipt is None else dict(command.execution_receipt)
            ),
            "terminal_status": command.terminal_status,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            status = command.terminal_status or ("completed" if command.success else "failed")
            result_kind = "success" if command.success else "error"
            for table, column, identity in (
                ("operations", "operation_id", handle.operation_id.value),
                ("operation_attempts", "operation_attempt_id", handle.attempt_id.value),
            ):
                cursor = connection.execute(
                    f"UPDATE {table} SET status = ?, terminal_revision = ? "
                    f"WHERE {column} = ? AND status = 'handoff_committed'",
                    (status, revision.value, identity),
                )
                if cursor.rowcount != 1:
                    raise ValueError("application Tool handoff is not finalizable")
            payload_json = canonical_json(dict(command.payload))
            if command.execution_receipt is not None:
                receipt = self._validate_sandbox_receipt(
                    dict(command.execution_receipt), handle.attempt_id.value
                )
                receipt_json = canonical_json(receipt)
                connection.execute(
                    """
                    INSERT INTO sandbox_execution_receipts VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        handle.attempt_id.value,
                        handle.operation_id.value,
                        str(receipt["schema_version"]),
                        str(receipt["isolation_tier"]),
                        str(receipt["policy_sha256"]),
                        str(receipt["network_mode"]),
                        canonical_json(receipt["readonly_grants"]),
                        canonical_json(receipt["readwrite_grants"]),
                        int(bool(receipt["dacl_fallback_allowed"])),
                        str(receipt["staging_preflight"]),
                        str(receipt["staging_postflight"]),
                        int(str(receipt["exit_code"])),
                        canonical_json(receipt["output_candidates"]),
                        hashlib.sha256(receipt_json.encode("utf-8")).hexdigest(),
                        revision.value,
                    ),
                )
            result_id = f"toolresult_{handle.operation_id.value[3:]}"
            connection.execute(
                """
                INSERT INTO canonical_tool_results VALUES (
                    ?, ?, ?, '1', ?, ?, ?, ?, 0, NULL, ?, ?
                )
                """,
                (
                    result_id,
                    handle.tool_call_id.value,
                    result_kind,
                    command.summary,
                    payload_json,
                    canonical_json(list(command.artifact_references)),
                    canonical_json([handle.operation_id.value]),
                    hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                    revision.value,
                ),
            )
            connection.execute(
                "UPDATE tool_calls SET proposal_status = 'resolved' WHERE tool_call_id = ?",
                (handle.tool_call_id.value,),
            )
            ordinal = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(status_ordinal), 0) + 1
                    FROM tool_call_status_history WHERE tool_call_id = ?
                    """,
                    (handle.tool_call_id.value,),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO tool_call_status_history VALUES (?, ?, 'resolved', ?, ?)",
                (handle.tool_call_id.value, ordinal, result_kind, revision.value),
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
            response = {
                "operation_id": handle.operation_id.value,
                "attempt_id": handle.attempt_id.value,
                "status": status,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        (
                            "tool.completed"
                            if status == "completed"
                            else "tool.interrupted"
                            if status in {"outcome_unknown", "completed_unreconciled"}
                            else "tool.failed"
                        ),
                        "operation",
                        handle.operation_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("operation.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="application_tool.finalize",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return BrokerExecutionOutcome(
            OperationId(str(response["operation_id"])),
            OperationAttemptId(str(response["attempt_id"])),
            str(response["status"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    @staticmethod
    def _validate_sandbox_receipt(receipt: dict[str, object], attempt_id: str) -> dict[str, object]:
        required = {
            "schema_version",
            "attempt_id",
            "isolation_tier",
            "policy_sha256",
            "network_mode",
            "readonly_grants",
            "readwrite_grants",
            "dacl_fallback_allowed",
            "staging_preflight",
            "staging_postflight",
            "exit_code",
            "output_candidates",
        }
        if set(receipt) != required:
            raise ValueError("sandbox Execution Receipt shape is not canonical")
        if receipt["attempt_id"] != attempt_id:
            raise ValueError("sandbox Execution Receipt targets another Attempt")
        if receipt["isolation_tier"] != "base-container":
            raise ValueError("sandbox execution did not use BaseContainer")
        if receipt["dacl_fallback_allowed"] is not False:
            raise ValueError("sandbox DACL fallback must remain disabled")
        if receipt["staging_preflight"] != "passed" or receipt["staging_postflight"] != "passed":
            raise ValueError("sandbox staging validation did not pass")
        if receipt["network_mode"] not in {"block", "allow"}:
            raise ValueError("sandbox network mode is invalid")
        if not isinstance(receipt["policy_sha256"], str) or len(receipt["policy_sha256"]) != 64:
            raise ValueError("sandbox policy hash is invalid")
        if not isinstance(receipt["readonly_grants"], list) or not isinstance(
            receipt["readwrite_grants"], list
        ):
            raise ValueError("sandbox grant lists are invalid")
        if not isinstance(receipt["output_candidates"], list):
            raise ValueError("sandbox output candidates are invalid")
        if type(receipt["exit_code"]) is not int:
            raise ValueError("sandbox exit code is invalid")
        return receipt
