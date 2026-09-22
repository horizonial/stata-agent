"""SQLite Waiting barrier and safe user-pause convergence."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable

from stata_research_agent.application.turn_interaction import (
    AnswerWaitingCommand,
    ContinuationIdentity,
    ContinuationOutcome,
    ContinuePausedTurnCommand,
    ConvergePauseCommand,
    OpenWaitingCommand,
    PauseIdentity,
    PauseOutcome,
    RequestPauseCommand,
    WaitingAnswerIdentity,
    WaitingIdentity,
    WaitingOutcome,
)
from stata_research_agent.domain.identifiers import PauseIntentId, ToolResultId, TurnId
from stata_research_agent.domain.revisions import WorkspaceRevision

from .atomic_commit import AtomicCommitService, JournalDraft, MutationPayload, OutboxDraft


class SqliteTurnInteractionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    def open_waiting(
        self, command: OpenWaitingCommand, identity: WaitingIdentity
    ) -> WaitingOutcome:
        request = {
            "turn_id": command.turn_id.value,
            "expected_turn_revision": command.expected_turn_revision,
            "wait_reason": command.wait_reason.value,
            "prompt": command.prompt,
            "tool_call_id": None if command.tool_call_id is None else command.tool_call_id.value,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            row = connection.execute(
                "SELECT status, turn_revision FROM turns WHERE turn_id = ?",
                (command.turn_id.value,),
            ).fetchone()
            if row is None or str(row["status"]) != "running":
                raise ValueError("only a Running Turn can enter Waiting")
            if int(row["turn_revision"]) != command.expected_turn_revision:
                raise ValueError("Turn revision changed before Waiting request")
            next_turn_revision = command.expected_turn_revision + 1
            connection.execute(
                "UPDATE turns SET status = 'waiting', turn_revision = ? WHERE turn_id = ?",
                (next_turn_revision, command.turn_id.value),
            )
            connection.execute(
                """
                INSERT INTO waiting_requests VALUES (?, ?, ?, ?, ?, 'open', ?, NULL, ?, NULL)
                """,
                (
                    identity.request_id.value,
                    command.turn_id.value,
                    None if command.tool_call_id is None else command.tool_call_id.value,
                    command.wait_reason.value,
                    command.prompt,
                    next_turn_revision,
                    revision.value,
                ),
            )
            if command.tool_call_id is not None:
                status_ordinal = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(status_ordinal), 0) + 1
                        FROM tool_call_status_history WHERE tool_call_id = ?
                        """,
                        (command.tool_call_id.value,),
                    ).fetchone()[0]
                )
                cursor = connection.execute(
                    """
                    UPDATE tool_calls SET proposal_status = 'awaiting_confirmation'
                    WHERE tool_call_id = ? AND proposal_status = 'scheduled'
                    """,
                    (command.tool_call_id.value,),
                )
                if cursor.rowcount != 1:
                    raise ValueError("confirmation Tool Call is not scheduled")
                connection.execute(
                    """
                    INSERT INTO tool_call_status_history
                    VALUES (?, ?, 'awaiting_confirmation', 'waiting_request', ?)
                    """,
                    (command.tool_call_id.value, status_ordinal, revision.value),
                )
            response = {
                "waiting_request_id": identity.request_id.value,
                "turn_id": command.turn_id.value,
                "turn_revision": next_turn_revision,
                "status": "open",
            }
            return MutationPayload(
                response,
                (JournalDraft("turn.waiting", "turn", command.turn_id.value, response),),
                (OutboxDraft("turn.waiting", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="turn.waiting.open",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return WaitingOutcome(
            type(identity.request_id)(str(response["waiting_request_id"])),
            TurnId(str(response["turn_id"])),
            int(response["turn_revision"]),
            str(response["status"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def answer_waiting(
        self, command: AnswerWaitingCommand, identity: WaitingAnswerIdentity
    ) -> WaitingOutcome:
        if not command.answer.strip():
            raise ValueError("Waiting answer cannot be empty")
        request = {
            "waiting_request_id": command.waiting_request_id.value,
            "expected_turn_id": None
            if command.expected_turn_id is None
            else command.expected_turn_id.value,
            "expected_turn_revision": command.expected_turn_revision,
            "answer_sha256": hashlib.sha256(command.answer.encode("utf-8")).hexdigest(),
            "tool_decision": command.tool_decision,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            row = connection.execute(
                """
                SELECT request.turn_id, request.status, request.tool_call_id,
                       request.wait_reason, turn.turn_revision, turn.conversation_id
                FROM waiting_requests AS request
                JOIN turns AS turn ON turn.turn_id = request.turn_id
                WHERE request.waiting_request_id = ? AND turn.status = 'waiting'
                """,
                (command.waiting_request_id.value,),
            ).fetchone()
            if row is None or str(row["status"]) != "open":
                raise ValueError("Waiting request is not open")
            if (
                command.expected_turn_id is not None
                and str(row["turn_id"]) != command.expected_turn_id.value
            ):
                raise ValueError("Waiting request belongs to a different Turn")
            if (
                command.expected_turn_revision is not None
                and int(row["turn_revision"]) != command.expected_turn_revision
            ):
                raise ValueError("Turn revision changed before Waiting answer")
            tool_call_id = None if row["tool_call_id"] is None else str(row["tool_call_id"])
            if (
                str(row["wait_reason"]) == "user_confirmation"
                and tool_call_id is not None
                and command.tool_decision is None
            ):
                raise ValueError("confirmation Waiting answer requires an explicit tool decision")
            turn_revision = int(row["turn_revision"]) + 1
            message_ordinal = int(
                connection.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM messages WHERE conversation_id = ?",
                    (str(row["conversation_id"]),),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, 'user', ?, ?, ?)",
                (
                    identity.message_id.value,
                    str(row["conversation_id"]),
                    command.answer,
                    message_ordinal,
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO waiting_answers VALUES (?, ?, ?, ?, ?)",
                (
                    identity.answer_id.value,
                    command.waiting_request_id.value,
                    identity.message_id.value,
                    request["answer_sha256"],
                    revision.value,
                ),
            )
            connection.execute(
                """
                UPDATE waiting_requests
                SET status = 'answered', terminal_turn_revision = ?, terminal_revision = ?
                WHERE waiting_request_id = ?
                """,
                (turn_revision, revision.value, command.waiting_request_id.value),
            )
            connection.execute(
                "UPDATE turns SET status = 'running', turn_revision = ? WHERE turn_id = ?",
                (turn_revision, str(row["turn_id"])),
            )
            if tool_call_id is not None and command.tool_decision is not None:
                status_ordinal = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(status_ordinal), 0) + 1
                        FROM tool_call_status_history WHERE tool_call_id = ?
                        """,
                        (tool_call_id,),
                    ).fetchone()[0]
                )
                if command.tool_decision == "approve":
                    next_status = "scheduled"
                    reason_code = "user_approved"
                else:
                    next_status = "resolved"
                    reason_code = (
                        "user_denied" if command.tool_decision == "deny" else "user_cancelled"
                    )
                    result_kind = "denied" if command.tool_decision == "deny" else "cancelled"
                    body_hash = hashlib.sha256(f'{{"kind":"{result_kind}"}}'.encode()).hexdigest()
                    connection.execute(
                        """
                        INSERT INTO canonical_tool_results VALUES (
                            ?, ?, ?, '1', ?, NULL, '[]', '[]', 0, NULL, ?, ?
                        )
                        """,
                        (
                            identity.tool_result_id.value,
                            tool_call_id,
                            result_kind,
                            reason_code,
                            body_hash,
                            revision.value,
                        ),
                    )
                cursor = connection.execute(
                    """
                    UPDATE tool_calls SET proposal_status = ?
                    WHERE tool_call_id = ? AND proposal_status = 'awaiting_confirmation'
                    """,
                    (next_status, tool_call_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("confirmation Tool Call is no longer awaiting a decision")
                connection.execute(
                    "INSERT INTO tool_call_status_history VALUES (?, ?, ?, ?, ?)",
                    (
                        tool_call_id,
                        status_ordinal,
                        next_status,
                        reason_code,
                        revision.value,
                    ),
                )
            response = {
                "waiting_request_id": command.waiting_request_id.value,
                "turn_id": str(row["turn_id"]),
                "turn_revision": turn_revision,
                "status": "answered",
                "answer_message_id": identity.message_id.value,
            }
            return MutationPayload(
                response,
                (JournalDraft("waiting.answered", "turn", str(row["turn_id"]), response),),
                (OutboxDraft("waiting.answered", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="waiting.answer",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return WaitingOutcome(
            type(command.waiting_request_id)(str(response["waiting_request_id"])),
            TurnId(str(response["turn_id"])),
            int(response["turn_revision"]),
            str(response["status"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def request_pause(self, command: RequestPauseCommand, identity: PauseIdentity) -> PauseOutcome:
        request = {
            "turn_id": command.turn_id.value,
            "expected_turn_revision": command.expected_turn_revision,
            "reason": command.reason,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            row = connection.execute(
                "SELECT status, turn_revision FROM turns WHERE turn_id = ?",
                (command.turn_id.value,),
            ).fetchone()
            if row is None or str(row["status"]) not in {"running", "waiting"}:
                raise ValueError("only Running or Waiting Turn can accept pause intent")
            if int(row["turn_revision"]) != command.expected_turn_revision:
                raise ValueError("Turn revision changed before pause intent")
            next_revision = command.expected_turn_revision + 1
            connection.execute(
                "INSERT INTO pause_intents VALUES (?, ?, ?, ?, 'requested', ?, NULL)",
                (
                    identity.pause_intent_id.value,
                    command.turn_id.value,
                    next_revision,
                    command.reason,
                    revision.value,
                ),
            )
            connection.execute(
                "UPDATE turns SET turn_revision = ? WHERE turn_id = ?",
                (next_revision, command.turn_id.value),
            )
            connection.execute(
                """
                UPDATE waiting_requests
                SET status = 'closed_by_user_pause', terminal_turn_revision = ?,
                    terminal_revision = ?
                WHERE turn_id = ? AND status = 'open'
                """,
                (next_revision, revision.value, command.turn_id.value),
            )
            response = {
                "pause_intent_id": identity.pause_intent_id.value,
                "turn_id": command.turn_id.value,
                "turn_revision": next_revision,
                "converged": False,
                "active_operation_count": -1,
            }
            return MutationPayload(
                response,
                (JournalDraft("turn.pause_requested", "turn", command.turn_id.value, response),),
                (OutboxDraft("turn.pause_requested", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="turn.pause.request",
            request=request,
            mutation=mutate,
        )
        return self._pause_outcome(receipt.response, receipt.commit_revision, receipt.replayed)

    def converge_pause(
        self,
        command: ConvergePauseCommand,
        result_identity_factory: Callable[[], ToolResultId],
    ) -> PauseOutcome:
        request = {"turn_id": command.turn_id.value}

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            intent = connection.execute(
                "SELECT * FROM pause_intents WHERE turn_id = ?",
                (command.turn_id.value,),
            ).fetchone()
            if intent is None or str(intent["status"]) == "completed":
                raise ValueError("Turn has no active pause intent")
            scheduled = connection.execute(
                """
                SELECT call.tool_call_id
                FROM tool_calls AS call
                JOIN assistant_outputs AS output
                  ON output.assistant_output_id = call.assistant_output_id
                JOIN model_invocations AS invocation
                  ON invocation.model_invocation_id = output.model_invocation_id
                JOIN steps AS step ON step.step_id = invocation.step_id
                WHERE step.turn_id = ? AND call.proposal_status IN (
                    'scheduled', 'awaiting_confirmation'
                )
                """,
                (command.turn_id.value,),
            ).fetchall()
            for row in scheduled:
                result_id = result_identity_factory()
                body_hash = hashlib.sha256(b'{"kind":"cancelled_by_pause"}').hexdigest()
                connection.execute(
                    """
                    INSERT INTO canonical_tool_results VALUES (
                        ?, ?, 'cancelled', '1', 'cancelled_by_user_pause', NULL,
                        '[]', '[]', 0, NULL, ?, ?
                    )
                    """,
                    (
                        result_id.value,
                        str(row["tool_call_id"]),
                        body_hash,
                        revision.value,
                    ),
                )
                status_ordinal = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(status_ordinal), 0) + 1
                        FROM tool_call_status_history WHERE tool_call_id = ?
                        """,
                        (str(row["tool_call_id"]),),
                    ).fetchone()[0]
                )
                connection.execute(
                    "UPDATE tool_calls SET proposal_status = 'resolved' WHERE tool_call_id = ?",
                    (str(row["tool_call_id"]),),
                )
                connection.execute(
                    """
                    INSERT INTO tool_call_status_history
                    VALUES (?, ?, 'resolved', 'cancelled_by_user_pause', ?)
                    """,
                    (str(row["tool_call_id"]), status_ordinal, revision.value),
                )
            cancellable = connection.execute(
                """
                SELECT call.tool_call_id, operation.operation_id
                FROM tool_calls AS call
                JOIN assistant_outputs AS output
                  ON output.assistant_output_id = call.assistant_output_id
                JOIN model_invocations AS invocation
                  ON invocation.model_invocation_id = output.model_invocation_id
                JOIN steps AS step ON step.step_id = invocation.step_id
                JOIN operation_tool_call_links AS link ON link.tool_call_id = call.tool_call_id
                JOIN operations AS operation ON operation.operation_id = link.operation_id
                WHERE step.turn_id = ? AND call.proposal_status = 'admitted'
                  AND operation.status = 'admitted'
                  AND NOT EXISTS (
                      SELECT 1 FROM operation_attempts AS attempt
                      WHERE attempt.operation_id = operation.operation_id
                  )
                """,
                (command.turn_id.value,),
            ).fetchall()
            for row in cancellable:
                result_id = result_identity_factory()
                body_hash = hashlib.sha256(b'{"kind":"cancelled_by_pause"}').hexdigest()
                connection.execute(
                    """
                    INSERT INTO canonical_tool_results VALUES (
                        ?, ?, 'cancelled', '1', 'cancelled_by_user_pause', NULL,
                        '[]', ?, 0, NULL, ?, ?
                    )
                    """,
                    (
                        result_id.value,
                        str(row["tool_call_id"]),
                        f'["{row["operation_id"]}"]',
                        body_hash,
                        revision.value,
                    ),
                )
                connection.execute(
                    """
                    UPDATE operations SET status = 'failed', terminal_revision = ?
                    WHERE operation_id = ?
                    """,
                    (revision.value, str(row["operation_id"])),
                )
                connection.execute(
                    "UPDATE tool_calls SET proposal_status = 'resolved' WHERE tool_call_id = ?",
                    (str(row["tool_call_id"]),),
                )
                status_ordinal = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(status_ordinal), 0) + 1
                        FROM tool_call_status_history WHERE tool_call_id = ?
                        """,
                        (str(row["tool_call_id"]),),
                    ).fetchone()[0]
                )
                connection.execute(
                    """
                    INSERT INTO tool_call_status_history
                    VALUES (?, ?, 'resolved', 'cancelled_by_user_pause', ?)
                    """,
                    (str(row["tool_call_id"]), status_ordinal, revision.value),
                )
            connection.execute(
                """
                UPDATE tool_resource_leases SET lease_status = 'released', released_revision = ?
                WHERE lease_status = 'active' AND tool_admission_id IN (
                    SELECT tool_admission_id FROM tool_admissions
                    WHERE operation_id IN (
                        SELECT operation_id FROM operations
                        WHERE requested_by_turn_id = ? AND status = 'failed'
                    )
                )
                """,
                (revision.value, command.turn_id.value),
            )
            active = int(
                connection.execute(
                    """
                    SELECT count(*) FROM operations
                    WHERE requested_by_turn_id = ?
                      AND status IN ('proposed', 'authorized', 'admitted',
                                     'handoff_committed', 'interrupted')
                    """,
                    (command.turn_id.value,),
                ).fetchone()[0]
            )
            turn = connection.execute(
                "SELECT turn_revision FROM turns WHERE turn_id = ?",
                (command.turn_id.value,),
            ).fetchone()
            turn_revision = int(turn["turn_revision"])
            converged = active == 0
            if converged:
                turn_revision += 1
                connection.execute(
                    """
                    UPDATE workspace_write_lane SET active_write_turn_id = NULL,
                        lane_revision = lane_revision + 1
                    WHERE singleton_id = 1 AND active_write_turn_id = ?
                    """,
                    (command.turn_id.value,),
                )
                connection.execute(
                    "UPDATE turns SET status = 'paused', turn_revision = ? WHERE turn_id = ?",
                    (turn_revision, command.turn_id.value),
                )
                connection.execute(
                    """
                    UPDATE pause_intents SET status = 'completed', completed_revision = ?
                    WHERE turn_id = ?
                    """,
                    (revision.value, command.turn_id.value),
                )
            else:
                connection.execute(
                    "UPDATE pause_intents SET status = 'converging' WHERE turn_id = ?",
                    (command.turn_id.value,),
                )
            response = {
                "pause_intent_id": str(intent["pause_intent_id"]),
                "turn_id": command.turn_id.value,
                "turn_revision": turn_revision,
                "converged": converged,
                "active_operation_count": active,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "turn.paused" if converged else "turn.pause_converging",
                        "turn",
                        command.turn_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("turn.pause_state", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="turn.pause.converge",
            request=request,
            mutation=mutate,
        )
        return self._pause_outcome(receipt.response, receipt.commit_revision, receipt.replayed)

    def continue_paused_turn(
        self, command: ContinuePausedTurnCommand, identity: ContinuationIdentity
    ) -> ContinuationOutcome:
        if not command.message.strip():
            raise ValueError("continuation message cannot be empty")
        request = {
            "predecessor_turn_id": command.predecessor_turn_id.value,
            "message": command.message,
            "relation_kind": command.relation_kind.value,
            "contract_decision": "retain_contract_revision",
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            predecessor = connection.execute(
                """
                SELECT conversation_id, research_path_id, execution_scope_id,
                       completion_contract_revision_id, execution_mode, status
                FROM turns WHERE turn_id = ?
                """,
                (command.predecessor_turn_id.value,),
            ).fetchone()
            if predecessor is None or str(predecessor["status"]) != "paused":
                raise ValueError("only a Paused Turn can create a continuation")
            if (
                connection.execute(
                    "SELECT 1 FROM turn_continuations WHERE predecessor_turn_id = ?",
                    (command.predecessor_turn_id.value,),
                ).fetchone()
                is not None
            ):
                raise ValueError("Paused Turn already has a continuation")
            if command.relation_kind.value == "recovery_continuation":
                recovery = connection.execute(
                    """
                    SELECT 1 FROM recovery_reports
                    WHERE requested_by_turn_id = ?
                      AND classification IN (
                          'completed_unreconciled', 'outcome_unknown',
                          'integrity_violation', 'definitely_not_started'
                      )
                    LIMIT 1
                    """,
                    (command.predecessor_turn_id.value,),
                ).fetchone()
                if recovery is None:
                    raise ValueError(
                        "recovery continuation requires a Recovery Report for the Paused Turn"
                    )
            conversation_id = str(predecessor["conversation_id"])
            message_ordinal = int(
                connection.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM messages WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, 'user', ?, ?, ?)",
                (
                    identity.message_id.value,
                    conversation_id,
                    command.message,
                    message_ordinal,
                    revision.value,
                ),
            )
            lane = connection.execute(
                "SELECT active_write_turn_id FROM workspace_write_lane WHERE singleton_id = 1"
            ).fetchone()
            execution_mode = str(predecessor["execution_mode"])
            successor_status = (
                "running"
                if execution_mode == "read" or lane["active_write_turn_id"] is None
                else "queued"
            )
            enqueue_ordinal = int(
                connection.execute(
                    "SELECT COALESCE(MAX(enqueue_ordinal), 0) + 1 FROM turns"
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    identity.successor_turn_id.value,
                    conversation_id,
                    identity.message_id.value,
                    str(predecessor["research_path_id"]),
                    str(predecessor["execution_scope_id"]),
                    str(predecessor["completion_contract_revision_id"]),
                    execution_mode,
                    successor_status,
                    enqueue_ordinal,
                    revision.value,
                ),
            )
            if execution_mode == "write" and successor_status == "running":
                connection.execute(
                    """
                    UPDATE workspace_write_lane
                    SET active_write_turn_id = ?, lane_revision = lane_revision + 1
                    WHERE singleton_id = 1 AND active_write_turn_id IS NULL
                    """,
                    (identity.successor_turn_id.value,),
                )
            connection.execute(
                """
                INSERT INTO turn_continuations VALUES (
                    ?, ?, ?, ?, 'retain_contract_revision', ?, ?, ?
                )
                """,
                (
                    identity.continuation_id.value,
                    command.predecessor_turn_id.value,
                    identity.successor_turn_id.value,
                    command.relation_kind.value,
                    str(predecessor["completion_contract_revision_id"]),
                    str(predecessor["completion_contract_revision_id"]),
                    revision.value,
                ),
            )
            response = {
                "turn_continuation_id": identity.continuation_id.value,
                "predecessor_turn_id": command.predecessor_turn_id.value,
                "successor_turn_id": identity.successor_turn_id.value,
                "successor_status": successor_status,
                "relation_kind": command.relation_kind.value,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "turn.continued",
                        "turn_continuation",
                        identity.continuation_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("turn.continued", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="turn.pause.continue",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return ContinuationOutcome(
            type(identity.continuation_id)(str(response["turn_continuation_id"])),
            TurnId(str(response["predecessor_turn_id"])),
            TurnId(str(response["successor_turn_id"])),
            str(response["successor_status"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    @staticmethod
    def _pause_outcome(
        response: object, commit_revision: WorkspaceRevision, replayed: bool
    ) -> PauseOutcome:
        if not isinstance(response, dict):
            raise TypeError("pause response must be an object")
        return PauseOutcome(
            PauseIntentId(str(response["pause_intent_id"])),
            TurnId(str(response["turn_id"])),
            int(response["turn_revision"]),
            bool(response["converged"]),
            int(response["active_operation_count"]),
            commit_revision,
            replayed,
        )
