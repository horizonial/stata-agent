"""SQLite implementation of the minimal Workspace control application port."""

from __future__ import annotations

import sqlite3

from stata_research_agent.application.control import (
    ActivateNextQueuedTurnCommand,
    ActivateNextQueuedTurnResult,
    CompleteTurnCommand,
    CompleteTurnResult,
    CreateWorkspaceCommand,
    CreateWorkspaceResult,
    SubmitMessageCommand,
    SubmitMessageResult,
)
from stata_research_agent.domain.identifiers import (
    CompletionContractId,
    CompletionContractRevisionId,
    ConversationId,
    ExecutionScopeId,
    MessageId,
    ResearchPathId,
    TurnId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision
from stata_research_agent.domain.status import ExecutionMode, TurnStatus

from .atomic_commit import (
    AtomicCommitService,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
)


class SqliteControlStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    def initialize_workspace(
        self,
        command: CreateWorkspaceCommand,
        *,
        main_path_id: ResearchPathId,
        main_scope_id: ExecutionScopeId,
    ) -> CreateWorkspaceResult:
        request = {"workspace_id": command.workspace_id.value}

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            connection.execute(
                "INSERT INTO research_paths VALUES (?, 'main', ?)",
                (main_path_id.value, revision.value),
            )
            connection.execute(
                """
                INSERT INTO research_path_profiles
                VALUES (?, 'Main', 'workspace_main', 'active', NULL, ?)
                """,
                (main_path_id.value, revision.value),
            )
            connection.execute(
                "INSERT INTO execution_scopes VALUES (?, 'main', ?)",
                (main_scope_id.value, revision.value),
            )
            connection.execute("INSERT INTO workspace_write_lane VALUES (1, NULL, 0)")
            response = {
                "workspace_id": command.workspace_id.value,
                "main_path_id": main_path_id.value,
                "main_scope_id": main_scope_id.value,
            }
            return MutationPayload(
                response=response,
                journal=(
                    JournalDraft(
                        "workspace.created", "workspace", command.workspace_id.value, response
                    ),
                ),
                outbox=(OutboxDraft("workspace.changed", {"revision": revision.value}),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="workspace.create",
            request=request,
            mutation=mutate,
        )
        return CreateWorkspaceResult(
            workspace_id=command.workspace_id,
            main_path_id=ResearchPathId(str(receipt.response["main_path_id"])),
            main_scope_id=ExecutionScopeId(str(receipt.response["main_scope_id"])),
            commit_revision=receipt.commit_revision,
            replayed=receipt.replayed,
        )

    def submit_message(
        self,
        command: SubmitMessageCommand,
        *,
        conversation_id: ConversationId,
        message_id: MessageId,
        turn_id: TurnId,
        completion_contract_id: CompletionContractId,
        completion_contract_revision_id: CompletionContractRevisionId,
    ) -> SubmitMessageResult:
        request = {
            "content": command.content,
            "conversation_id": None
            if command.conversation_id is None
            else command.conversation_id.value,
            "execution_mode": command.execution_mode.value,
            "research_path_id": None
            if command.research_path_id is None
            else command.research_path_id.value,
            "goal_mode": command.goal_mode,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            existing_conversation = connection.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = ?",
                (conversation_id.value,),
            ).fetchone()
            if command.conversation_id is None:
                connection.execute(
                    "INSERT INTO conversations(conversation_id, created_revision) VALUES (?, ?)",
                    (conversation_id.value, revision.value),
                )
            elif existing_conversation is None:
                raise ValueError(f"unknown conversation: {conversation_id.value}")

            message_ordinal = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(ordinal), 0) + 1 FROM messages
                    WHERE conversation_id = ?
                    """,
                    (conversation_id.value,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO messages(
                    message_id, conversation_id, role, content, ordinal, created_revision
                ) VALUES (?, ?, 'user', ?, ?, ?)
                """,
                (
                    message_id.value,
                    conversation_id.value,
                    command.content,
                    message_ordinal,
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO completion_contracts VALUES (?, ?, ?)",
                (completion_contract_id.value, message_id.value, revision.value),
            )
            connection.execute(
                """
                INSERT INTO completion_contract_revisions VALUES (?, ?, 1, 'intake', 1, ?, ?)
                """,
                (
                    completion_contract_revision_id.value,
                    completion_contract_id.value,
                    message_id.value,
                    revision.value,
                ),
            )
            if command.research_path_id is None:
                target_path_id = str(
                    connection.execute(
                        """
                        SELECT research_path_id FROM research_paths
                        WHERE canonical_key = 'main'
                        """
                    ).fetchone()[0]
                )
            else:
                path_row = connection.execute(
                    """
                    SELECT p.research_path_id
                    FROM research_paths AS p
                    JOIN research_path_profiles AS profile
                      ON profile.research_path_id = p.research_path_id
                    WHERE p.research_path_id = ? AND profile.lifecycle = 'active'
                    """,
                    (command.research_path_id.value,),
                ).fetchone()
                if path_row is None:
                    raise ValueError(
                        f"unknown active Research Path: {command.research_path_id.value}"
                    )
                target_path_id = str(path_row["research_path_id"])
            main_scope_id = str(
                connection.execute(
                    "SELECT execution_scope_id FROM execution_scopes WHERE canonical_key = 'main'"
                ).fetchone()[0]
            )
            enqueue_ordinal = int(
                connection.execute(
                    "SELECT COALESCE(MAX(enqueue_ordinal), 0) + 1 FROM turns"
                ).fetchone()[0]
            )
            lane_owner = connection.execute(
                "SELECT active_write_turn_id FROM workspace_write_lane WHERE singleton_id = 1"
            ).fetchone()[0]
            status = (
                TurnStatus.RUNNING
                if command.execution_mode is ExecutionMode.READ or lane_owner is None
                else TurnStatus.QUEUED
            )
            connection.execute(
                """
                INSERT INTO turns(
                    turn_id, conversation_id, triggering_message_id, research_path_id,
                    execution_scope_id, completion_contract_revision_id, execution_mode,
                    status, turn_revision, enqueue_ordinal, created_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    turn_id.value,
                    conversation_id.value,
                    message_id.value,
                    target_path_id,
                    main_scope_id,
                    completion_contract_revision_id.value,
                    command.execution_mode.value,
                    status.value,
                    enqueue_ordinal,
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO turn_goal_policies VALUES (?, ?, ?)",
                (turn_id.value, command.goal_mode, revision.value),
            )
            if command.execution_mode is ExecutionMode.WRITE and status is TurnStatus.RUNNING:
                cursor = connection.execute(
                    """
                    UPDATE workspace_write_lane
                    SET active_write_turn_id = ?, lane_revision = lane_revision + 1
                    WHERE singleton_id = 1 AND active_write_turn_id IS NULL
                    """,
                    (turn_id.value,),
                )
                if cursor.rowcount != 1:
                    raise sqlite3.IntegrityError("failed to acquire Workspace write lane")

            response = {
                "conversation_id": conversation_id.value,
                "message_id": message_id.value,
                "turn_id": turn_id.value,
                "research_path_id": target_path_id,
                "turn_status": status.value,
            }
            return MutationPayload(
                response=response,
                journal=(
                    JournalDraft("message.recorded", "message", message_id.value, {}),
                    JournalDraft(
                        "turn.started" if status is TurnStatus.RUNNING else "turn.queued",
                        "turn",
                        turn_id.value,
                        {"execution_mode": command.execution_mode.value},
                    ),
                ),
                outbox=(
                    OutboxDraft(
                        "workspace.changed",
                        {"turn_id": turn_id.value, "turn_status": status.value},
                    ),
                ),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="message.submit",
            request=request,
            mutation=mutate,
        )
        return SubmitMessageResult(
            conversation_id=ConversationId(str(receipt.response["conversation_id"])),
            message_id=MessageId(str(receipt.response["message_id"])),
            turn_id=TurnId(str(receipt.response["turn_id"])),
            research_path_id=ResearchPathId(str(receipt.response["research_path_id"])),
            turn_status=TurnStatus(str(receipt.response["turn_status"])),
            commit_revision=receipt.commit_revision,
            replayed=receipt.replayed,
        )

    def complete_turn(self, command: CompleteTurnCommand) -> CompleteTurnResult:
        request = {
            "turn_id": command.turn_id.value,
            "terminal_status": command.terminal_status.value,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            row = connection.execute(
                "SELECT execution_mode, status FROM turns WHERE turn_id = ?",
                (command.turn_id.value,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown turn: {command.turn_id.value}")
            if str(row["status"]) not in {TurnStatus.RUNNING.value, TurnStatus.WAITING.value}:
                raise ValueError(f"turn is not active: {command.turn_id.value}")
            if str(row["execution_mode"]) == ExecutionMode.WRITE.value:
                cursor = connection.execute(
                    """
                    UPDATE workspace_write_lane
                    SET active_write_turn_id = NULL, lane_revision = lane_revision + 1
                    WHERE singleton_id = 1 AND active_write_turn_id = ?
                    """,
                    (command.turn_id.value,),
                )
                if cursor.rowcount != 1:
                    raise sqlite3.IntegrityError("active write Turn does not own the lane")
            connection.execute(
                "UPDATE turns SET status = ?, turn_revision = turn_revision + 1 WHERE turn_id = ?",
                (command.terminal_status.value, command.turn_id.value),
            )
            response = {
                "turn_id": command.turn_id.value,
                "terminal_status": command.terminal_status.value,
            }
            return MutationPayload(
                response=response,
                journal=(JournalDraft("turn.terminated", "turn", command.turn_id.value, response),),
                outbox=(OutboxDraft("workspace.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="turn.complete",
            request=request,
            mutation=mutate,
        )
        return CompleteTurnResult(
            turn_id=command.turn_id,
            terminal_status=command.terminal_status,
            commit_revision=receipt.commit_revision,
            replayed=receipt.replayed,
        )

    def activate_next_queued_turn(
        self, command: ActivateNextQueuedTurnCommand
    ) -> ActivateNextQueuedTurnResult:
        """Atomically claim the oldest queued write Turn for an empty Workspace lane."""

        request: dict[str, object] = {}

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            lane = connection.execute(
                "SELECT active_write_turn_id FROM workspace_write_lane WHERE singleton_id = 1"
            ).fetchone()
            if lane is None or lane["active_write_turn_id"] is not None:
                raise ValueError("Workspace write lane is not empty")
            queued = connection.execute(
                """
                SELECT turn_id, turn_revision
                FROM turns
                WHERE execution_mode = 'write' AND status = 'queued'
                ORDER BY enqueue_ordinal, turn_id
                LIMIT 1
                """
            ).fetchone()
            if queued is None:
                raise ValueError("Workspace has no queued write Turn")
            turn_id = str(queued["turn_id"])
            turn_revision = int(queued["turn_revision"]) + 1
            cursor = connection.execute(
                """
                UPDATE turns SET status = 'running', turn_revision = ?
                WHERE turn_id = ? AND status = 'queued'
                """,
                (turn_revision, turn_id),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("queued Turn changed before activation")
            cursor = connection.execute(
                """
                UPDATE workspace_write_lane
                SET active_write_turn_id = ?, lane_revision = lane_revision + 1
                WHERE singleton_id = 1 AND active_write_turn_id IS NULL
                """,
                (turn_id,),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("failed to claim empty Workspace write lane")
            response = {
                "turn_id": turn_id,
                "turn_revision": turn_revision,
            }
            return MutationPayload(
                response=response,
                journal=(
                    JournalDraft(
                        "turn.started",
                        "turn",
                        turn_id,
                        {"activation_reason": "workspace_queue_head"},
                    ),
                ),
                outbox=(OutboxDraft("workspace.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="turn.queue.activate_next",
            request=request,
            mutation=mutate,
        )
        return ActivateNextQueuedTurnResult(
            turn_id=TurnId(str(receipt.response["turn_id"])),
            turn_revision=int(receipt.response["turn_revision"]),
            commit_revision=receipt.commit_revision,
            replayed=receipt.replayed,
        )
