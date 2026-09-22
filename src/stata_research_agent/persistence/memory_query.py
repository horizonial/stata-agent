"""Read model for Project Memory inspection and user control."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MemorySourceSnapshot:
    object_type: str
    object_id: str
    object_revision: str
    role: str


@dataclass(frozen=True, slots=True)
class MemoryItemSnapshot:
    memory_item_id: str
    memory_revision_id: str
    pointer_revision: int
    lifecycle: str
    access_tier: str
    pinned: bool
    retention_revision: int
    superseded_by_memory_item_id: str | None
    scope_kind: str
    scope_object_id: str
    kind: str
    title: str
    content: str
    origin: str
    created_revision: int
    updated_revision: int
    last_used_revision: int | None
    recall_count: int
    quality_flags: tuple[str, ...]
    sources: tuple[MemorySourceSnapshot, ...]


@dataclass(frozen=True, slots=True)
class ConversationMemoryPolicySnapshot:
    conversation_id: str
    use_memory: bool
    contribute_memory: bool
    policy_revision: int


@dataclass(frozen=True, slots=True)
class MemoryIndexSnapshot:
    authoritative_revision: int
    items: tuple[MemoryItemSnapshot, ...]
    summaries: tuple[tuple[str, str, str, int, int], ...]


class SqliteMemoryQuery:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def index(
        self,
        *,
        lifecycle: str | None = None,
        search: str | None = None,
        research_path_id: str | None = None,
    ) -> MemoryIndexSnapshot:
        if lifecycle is not None and lifecycle not in {"active", "proposed", "retracted"}:
            raise ValueError("invalid Memory lifecycle filter")
        clauses: list[str] = []
        parameters: list[object] = []
        if lifecycle is not None:
            clauses.append("state.lifecycle = ?")
            parameters.append(lifecycle)
        if research_path_id is not None:
            clauses.append(
                "(item.scope_kind = 'workspace' OR "
                "(item.scope_kind = 'research_path' AND item.scope_object_id = ?))"
            )
            parameters.append(research_path_id)
        if search is not None and search.strip():
            escaped = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append(
                "(revision.title LIKE ? ESCAPE '\\' OR revision.content LIKE ? ESCAPE '\\')"
            )
            parameters.extend((f"%{escaped}%", f"%{escaped}%"))
        where = "" if not clauses else "WHERE " + " AND ".join(clauses)
        rows = self._connection.execute(
            f"""
            SELECT item.memory_item_id, state.current_revision_id, state.pointer_revision,
                   state.lifecycle, item.scope_kind, item.scope_object_id, item.memory_kind,
                   retention.access_tier, retention.pinned,
                   retention.retention_revision,
                   retention.superseded_by_memory_item_id,
                   revision.title, revision.content, revision.origin_kind,
                   revision.created_revision, state.updated_revision,
                   MAX(use.created_revision) AS last_used_revision,
                   COUNT(DISTINCT use.context_item_id) AS recall_count
            FROM memory_items AS item
            JOIN memory_current_states AS state USING (memory_item_id)
            JOIN memory_retention_states AS retention USING (memory_item_id)
            JOIN memory_revisions AS revision
              ON revision.memory_revision_id = state.current_revision_id
            LEFT JOIN memory_context_uses AS use USING (memory_item_id)
            {where}
            GROUP BY item.memory_item_id
            ORDER BY
              CASE state.lifecycle WHEN 'proposed' THEN 0 WHEN 'active' THEN 1 ELSE 2 END,
              state.updated_revision DESC, item.memory_item_id
            """,
            tuple(parameters),
        ).fetchall()
        items: list[MemoryItemSnapshot] = []
        for row in rows:
            source_rows = self._connection.execute(
                """
                SELECT source_object_type, source_object_id, source_object_revision, source_role
                FROM memory_revision_sources WHERE memory_revision_id = ?
                ORDER BY memory_revision_source_id
                """,
                (str(row["current_revision_id"]),),
            ).fetchall()
            conflict = self._connection.execute(
                """
                SELECT 1
                FROM memory_items AS other
                JOIN memory_current_states AS other_state USING (memory_item_id)
                JOIN memory_revisions AS other_revision
                  ON other_revision.memory_revision_id = other_state.current_revision_id
                WHERE other.memory_item_id != ?
                  AND other.memory_kind = ?
                  AND other.scope_kind = ?
                  AND other.scope_object_id = ?
                  AND lower(trim(other_revision.title)) = lower(trim(?))
                  AND other_state.lifecycle IN ('active', 'proposed')
                  AND lower(trim(other_revision.content)) != lower(trim(?))
                LIMIT 1
                """,
                (
                    str(row["memory_item_id"]),
                    str(row["memory_kind"]),
                    str(row["scope_kind"]),
                    str(row["scope_object_id"]),
                    str(row["title"]),
                    str(row["content"]),
                ),
            ).fetchone()
            flags: list[str] = []
            if str(row["lifecycle"]) == "proposed":
                flags.append("awaiting_confirmation")
            if str(row["lifecycle"]) == "active" and int(row["recall_count"]) == 0:
                flags.append("never_recalled")
            if conflict is not None:
                flags.append("potential_conflict")
            if not source_rows:
                flags.append("source_revision_unavailable")
            items.append(
                MemoryItemSnapshot(
                    str(row["memory_item_id"]),
                    str(row["current_revision_id"]),
                    int(row["pointer_revision"]),
                    str(row["lifecycle"]),
                    str(row["access_tier"]),
                    bool(row["pinned"]),
                    int(row["retention_revision"]),
                    None
                    if row["superseded_by_memory_item_id"] is None
                    else str(row["superseded_by_memory_item_id"]),
                    str(row["scope_kind"]),
                    str(row["scope_object_id"]),
                    str(row["memory_kind"]),
                    str(row["title"]),
                    str(row["content"]),
                    str(row["origin_kind"]),
                    int(row["created_revision"]),
                    int(row["updated_revision"]),
                    None if row["last_used_revision"] is None else int(row["last_used_revision"]),
                    int(row["recall_count"]),
                    tuple(flags),
                    tuple(
                        MemorySourceSnapshot(
                            str(source["source_object_type"]),
                            str(source["source_object_id"]),
                            str(source["source_object_revision"]),
                            str(source["source_role"]),
                        )
                        for source in source_rows
                    ),
                )
            )
        summaries = tuple(
            (
                str(row["scope_kind"]),
                str(row["scope_object_id"]),
                str(row["summary_text"]),
                int(row["source_revision"]),
                int(row["projection_revision"]),
            )
            for row in self._connection.execute(
                """
                SELECT scope_kind, scope_object_id, summary_text,
                       source_revision, projection_revision
                FROM memory_summary_projections ORDER BY scope_kind, scope_object_id
                """
            ).fetchall()
        )
        authoritative = int(
            self._connection.execute(
                "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
            ).fetchone()[0]
        )
        return MemoryIndexSnapshot(authoritative, tuple(items), summaries)

    def conversation_policy(self, conversation_id: str) -> ConversationMemoryPolicySnapshot:
        row = self._connection.execute(
            """
            SELECT conversation.conversation_id,
                   COALESCE(policy.use_memory, 1) AS use_memory,
                   COALESCE(policy.contribute_memory, 1) AS contribute_memory,
                   COALESCE(policy.policy_revision, 0) AS policy_revision
            FROM conversations AS conversation
            LEFT JOIN conversation_memory_policies AS policy USING (conversation_id)
            WHERE conversation.conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Conversation does not exist")
        return ConversationMemoryPolicySnapshot(
            str(row["conversation_id"]),
            bool(row["use_memory"]),
            bool(row["contribute_memory"]),
            int(row["policy_revision"]),
        )
