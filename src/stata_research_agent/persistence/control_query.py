"""SQLite authoritative Query adapter; it does not infer facts from projections."""

import base64
import sqlite3
from collections.abc import Callable

from stata_research_agent.application.queries import (
    AttentionRef,
    ConversationSummary,
    MessageSummary,
    PauseIntentSummary,
    ProjectionWatermark,
    ResearchPathSummary,
    TurnSummary,
    WaitingRequestSummary,
    WorkspaceAttentionSnapshot,
    WorkspaceBootstrapSnapshot,
    WorkspaceExecutionSnapshot,
)
from stata_research_agent.domain.identifiers import (
    ConversationId,
    MessageId,
    ResearchPathId,
    TurnId,
)
from stata_research_agent.domain.revisions import (
    ControlRevision,
    EntityRevision,
    Ordinal,
    WorkspaceRevision,
)
from stata_research_agent.domain.status import ExecutionMode, TurnStatus


class SqliteWorkspaceQuery:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        during_bootstrap_snapshot_hook: Callable[[], None] | None = None,
    ) -> None:
        self._connection = connection
        self._during_bootstrap_snapshot_hook = during_bootstrap_snapshot_hook

    def execution_snapshot(self) -> WorkspaceExecutionSnapshot:
        self._connection.execute("BEGIN")
        try:
            authoritative_revision = int(
                self._connection.execute(
                    "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
                ).fetchone()[0]
            )
            lane = self._connection.execute(
                """
                SELECT active_write_turn_id, lane_revision
                FROM workspace_write_lane WHERE singleton_id = 1
                """
            ).fetchone()
            if lane is None:
                raise RuntimeError("Workspace control state is not initialized")
            rows = self._connection.execute(
                """
                SELECT turn_id, conversation_id, execution_mode, status,
                       turn_revision, enqueue_ordinal
                FROM turns ORDER BY enqueue_ordinal
                """
            ).fetchall()
            turns = tuple(
                TurnSummary(
                    turn_id=TurnId(str(row["turn_id"])),
                    conversation_id=ConversationId(str(row["conversation_id"])),
                    execution_mode=ExecutionMode(str(row["execution_mode"])),
                    status=TurnStatus(str(row["status"])),
                    turn_revision=EntityRevision(int(row["turn_revision"])),
                    enqueue_ordinal=Ordinal(int(row["enqueue_ordinal"])),
                )
                for row in rows
            )
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        owner = None if lane["active_write_turn_id"] is None else TurnId(str(lane[0]))
        return WorkspaceExecutionSnapshot(
            authoritative_revision=WorkspaceRevision(authoritative_revision),
            lane_revision=ControlRevision(int(lane["lane_revision"])),
            active_write_turn_id=owner,
            turns=turns,
        )

    def attention_snapshot(self) -> WorkspaceAttentionSnapshot:
        """Read the minimal cross-Workspace-safe action summary."""

        self._connection.execute("BEGIN")
        try:
            workspace_id = str(
                self._connection.execute(
                    "SELECT workspace_id FROM workspace_identity WHERE singleton_id = 1"
                ).fetchone()[0]
            )
            authoritative_revision = int(
                self._connection.execute(
                    "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
                ).fetchone()[0]
            )
            lane = self._connection.execute(
                """
                SELECT lane.active_write_turn_id, turn.status
                FROM workspace_write_lane AS lane
                LEFT JOIN turns AS turn ON turn.turn_id = lane.active_write_turn_id
                WHERE lane.singleton_id = 1
                """
            ).fetchone()
            queued_write_count = int(
                self._connection.execute(
                    """
                    SELECT count(*) FROM turns
                    WHERE execution_mode = 'write' AND status = 'queued'
                    """
                ).fetchone()[0]
            )
            active_read_count = int(
                self._connection.execute(
                    """
                    SELECT count(*) FROM turns
                    WHERE execution_mode = 'read' AND status IN ('running', 'waiting')
                    """
                ).fetchone()[0]
            )
            refs: list[AttentionRef] = []
            for row in self._connection.execute(
                """
                SELECT waiting_request_id, turn_id FROM waiting_requests WHERE status = 'open'
                """
            ).fetchall():
                refs.append(
                    AttentionRef(
                        "waiting_for_user",
                        "ACTION_REQUIRED",
                        "waiting_request",
                        str(row["waiting_request_id"]),
                    )
                )
            for row in self._connection.execute(
                """
                SELECT pause_intent_id FROM pause_intents
                WHERE status IN ('requested', 'converging')
                """
            ).fetchall():
                refs.append(
                    AttentionRef(
                        "pause_converging",
                        "INFO",
                        "pause_intent",
                        str(row["pause_intent_id"]),
                    )
                )
            severity_by_operation = {
                "completed_unreconciled": "ACTION_REQUIRED",
                "outcome_unknown": "ACTION_REQUIRED",
                "integrity_violation": "CRITICAL",
            }
            for row in self._connection.execute(
                """
                SELECT operation_id, status FROM operations
                WHERE status IN (
                    'completed_unreconciled', 'outcome_unknown', 'integrity_violation'
                )
                ORDER BY created_revision, operation_id
                """
            ).fetchall():
                status = str(row["status"])
                refs.append(
                    AttentionRef(
                        status,
                        severity_by_operation[status],
                        "operation",
                        str(row["operation_id"]),
                    )
                )
            for row in self._connection.execute(
                """
                SELECT turn_id, status FROM turns WHERE status IN ('paused', 'failed', 'partial')
                ORDER BY enqueue_ordinal
                """
            ).fetchall():
                refs.append(
                    AttentionRef(
                        f"turn_{row['status']}",
                        "ACTION_REQUIRED" if str(row["status"]) != "partial" else "INFO",
                        "turn",
                        str(row["turn_id"]),
                    )
                )
            highest = (
                "CRITICAL"
                if any(ref.severity == "CRITICAL" for ref in refs)
                else "ACTION_REQUIRED"
                if any(ref.severity == "ACTION_REQUIRED" for ref in refs)
                else "INFO"
            )
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        active_turn_id = (
            None
            if lane is None or lane["active_write_turn_id"] is None
            else TurnId(str(lane["active_write_turn_id"]))
        )
        return WorkspaceAttentionSnapshot(
            workspace_id=workspace_id,
            authoritative_revision=WorkspaceRevision(authoritative_revision),
            active_write_turn_id=active_turn_id,
            active_write_turn_status=None
            if lane is None or lane["status"] is None
            else str(lane["status"]),
            queued_write_count=queued_write_count,
            active_read_count=active_read_count,
            requires_action=any(ref.severity != "INFO" for ref in refs),
            highest_severity=highest,
            attention_refs=tuple(refs),
        )

    def bootstrap_snapshot(self) -> WorkspaceBootstrapSnapshot:
        """Read Bootstrap data and its durable cursor from one SQLite snapshot."""

        self._connection.execute("BEGIN")
        try:
            workspace_id = str(
                self._connection.execute(
                    "SELECT workspace_id FROM workspace_identity WHERE singleton_id = 1"
                ).fetchone()[0]
            )
            authoritative_revision = int(
                self._connection.execute(
                    "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
                ).fetchone()[0]
            )
            if self._during_bootstrap_snapshot_hook is not None:
                self._during_bootstrap_snapshot_hook()

            lane = self._connection.execute(
                """
                SELECT active_write_turn_id, lane_revision
                FROM workspace_write_lane WHERE singleton_id = 1
                """
            ).fetchone()
            if lane is None:
                raise RuntimeError("Workspace control state is not initialized")

            turn_rows = self._connection.execute(
                """
                SELECT turn_id, conversation_id, execution_mode, status,
                       turn_revision, enqueue_ordinal
                FROM turns ORDER BY enqueue_ordinal
                """
            ).fetchall()
            turns = tuple(self._turn_summary(row) for row in turn_rows)

            conversation_rows = self._connection.execute(
                """
                SELECT c.conversation_id, c.created_revision,
                       m.message_id AS latest_message_id,
                       m.content AS latest_message_content,
                       m.ordinal AS latest_message_ordinal
                FROM conversations AS c
                LEFT JOIN messages AS m
                  ON m.conversation_id = c.conversation_id
                 AND m.ordinal = (
                     SELECT MAX(inner_message.ordinal)
                     FROM messages AS inner_message
                     WHERE inner_message.conversation_id = c.conversation_id
                 )
                ORDER BY COALESCE(m.created_revision, c.created_revision) DESC,
                         c.conversation_id
                """
            ).fetchall()
            conversations = tuple(
                ConversationSummary(
                    ConversationId(str(row["conversation_id"])),
                    WorkspaceRevision(int(row["created_revision"])),
                    None
                    if row["latest_message_id"] is None
                    else MessageId(str(row["latest_message_id"])),
                    None
                    if row["latest_message_content"] is None
                    else str(row["latest_message_content"])[:160],
                    None
                    if row["latest_message_ordinal"] is None
                    else Ordinal(int(row["latest_message_ordinal"])),
                )
                for row in conversation_rows
            )

            active_owner = (
                None
                if lane["active_write_turn_id"] is None
                else TurnId(str(lane["active_write_turn_id"]))
            )
            active_conversation_id = next(
                (turn.conversation_id for turn in turns if turn.turn_id == active_owner),
                conversations[0].conversation_id if conversations else None,
            )
            recent_messages = self._recent_messages(active_conversation_id)
            waiting = self._open_waiting_request(active_owner)
            pause_intent = self._active_pause_intent(active_owner)

            research_paths = tuple(
                ResearchPathSummary(
                    ResearchPathId(str(row["research_path_id"])),
                    str(row["canonical_key"]),
                    WorkspaceRevision(int(row["created_revision"])),
                )
                for row in self._connection.execute(
                    """
                    SELECT research_path_id, canonical_key, created_revision
                    FROM research_paths ORDER BY created_revision, research_path_id
                    """
                ).fetchall()
            )
            projection_watermarks = tuple(
                ProjectionWatermark(
                    "evidence_current_state",
                    WorkspaceRevision(int(row["projection_revision"])),
                )
                for row in self._connection.execute(
                    """
                    SELECT projection_revision
                    FROM evidence_projection_checkpoints
                    WHERE projection_name = 'evidence_current_state'
                    """
                ).fetchall()
            )
            outbox = self._connection.execute(
                """
                SELECT workspace_revision, ordinal, outbox_entry_id
                FROM outbox_entries
                ORDER BY workspace_revision DESC, ordinal DESC LIMIT 1
                """
            ).fetchone()
            cursor = encode_workspace_stream_cursor(
                0 if outbox is None else int(outbox["workspace_revision"]),
                0 if outbox is None else int(outbox["ordinal"]),
                "none" if outbox is None else str(outbox["outbox_entry_id"]),
            )
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

        return WorkspaceBootstrapSnapshot(
            workspace_id=workspace_id,
            authoritative_revision=WorkspaceRevision(authoritative_revision),
            durable_stream_cursor=cursor,
            lane_revision=ControlRevision(int(lane["lane_revision"])),
            active_write_turn_id=active_owner,
            conversations=conversations,
            active_conversation_id=active_conversation_id,
            recent_messages=recent_messages,
            turns=turns,
            open_waiting_request=waiting,
            active_pause_intent=pause_intent,
            research_paths=research_paths,
            projection_watermarks=projection_watermarks,
        )

    @staticmethod
    def _turn_summary(row: sqlite3.Row) -> TurnSummary:
        return TurnSummary(
            turn_id=TurnId(str(row["turn_id"])),
            conversation_id=ConversationId(str(row["conversation_id"])),
            execution_mode=ExecutionMode(str(row["execution_mode"])),
            status=TurnStatus(str(row["status"])),
            turn_revision=EntityRevision(int(row["turn_revision"])),
            enqueue_ordinal=Ordinal(int(row["enqueue_ordinal"])),
        )

    def _recent_messages(
        self, conversation_id: ConversationId | None
    ) -> tuple[MessageSummary, ...]:
        if conversation_id is None:
            return ()
        rows = self._connection.execute(
            """
            SELECT message_id, conversation_id, role, content, ordinal, created_revision
            FROM messages WHERE conversation_id = ?
            ORDER BY ordinal DESC LIMIT 20
            """,
            (conversation_id.value,),
        ).fetchall()
        return tuple(
            MessageSummary(
                MessageId(str(row["message_id"])),
                ConversationId(str(row["conversation_id"])),
                str(row["role"]),
                str(row["content"]),
                Ordinal(int(row["ordinal"])),
                WorkspaceRevision(int(row["created_revision"])),
            )
            for row in reversed(rows)
        )

    def _open_waiting_request(self, active_owner: TurnId | None) -> WaitingRequestSummary | None:
        if active_owner is None:
            return None
        row = self._connection.execute(
            """
            SELECT waiting_request_id, turn_id, wait_reason, prompt, status,
                   created_turn_revision
            FROM waiting_requests
            WHERE turn_id = ? AND status = 'open'
            """,
            (active_owner.value,),
        ).fetchone()
        if row is None:
            return None
        return WaitingRequestSummary(
            waiting_request_id=str(row["waiting_request_id"]),
            turn_id=TurnId(str(row["turn_id"])),
            wait_reason=str(row["wait_reason"]),
            prompt=str(row["prompt"]),
            status=str(row["status"]),
            created_turn_revision=EntityRevision(int(row["created_turn_revision"])),
        )

    def _active_pause_intent(self, active_owner: TurnId | None) -> PauseIntentSummary | None:
        if active_owner is None:
            return None
        row = self._connection.execute(
            """
            SELECT pause_intent_id, turn_id, requested_turn_revision, reason, status
            FROM pause_intents
            WHERE turn_id = ? AND status IN ('requested', 'converging')
            """,
            (active_owner.value,),
        ).fetchone()
        if row is None:
            return None
        return PauseIntentSummary(
            pause_intent_id=str(row["pause_intent_id"]),
            turn_id=TurnId(str(row["turn_id"])),
            requested_turn_revision=EntityRevision(int(row["requested_turn_revision"])),
            reason=str(row["reason"]),
            status=str(row["status"]),
        )


def encode_workspace_stream_cursor(
    workspace_revision: int, ordinal: int, outbox_entry_id: str
) -> str:
    """Create a namespaced opaque cursor from persisted Outbox ordering identity."""

    raw = f"{workspace_revision}:{ordinal}:{outbox_entry_id}".encode()
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"wsc1_{token}"


def decode_workspace_stream_cursor(cursor: str) -> tuple[int, int, str]:
    """Server-side decoder used by stream/replay contracts; clients must not parse it."""

    if not cursor.startswith("wsc1_"):
        raise ValueError("workspace stream cursor namespace is invalid")
    token = cursor.removeprefix("wsc1_")
    try:
        padded = token + "=" * (-len(token) % 4)
        revision_text, ordinal_text, outbox_entry_id = (
            base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8").split(":", 2)
        )
        revision = int(revision_text)
        ordinal = int(ordinal_text)
    except (UnicodeError, ValueError) as error:
        raise ValueError("workspace stream cursor is malformed") from error
    if revision < 0 or ordinal < 0 or not outbox_entry_id:
        raise ValueError("workspace stream cursor values are invalid")
    return revision, ordinal, outbox_entry_id
