"""SQLite authority for source-aware, versioned Project Memory."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import cast

from stata_research_agent.application.memory import (
    ActivateMemoryCommand,
    ConversationMemoryPolicyOutcome,
    CreateMemoryCommand,
    MemoryAccessTier,
    MemoryCompactionCheckpointOutcome,
    MemoryEpisodeOutcome,
    MemoryIdentity,
    MemoryLifecycle,
    MemoryOriginKind,
    MemoryOutcome,
    MemoryRetentionIdentity,
    MemoryRetentionOutcome,
    MemoryRevisionIdentity,
    MemoryScopeKind,
    MemorySource,
    MemorySourceRole,
    RecordMemoryCompactionCheckpointCommand,
    RecordMemoryEpisodeCommand,
    RetractMemoryCommand,
    ReviseMemoryCommand,
    SetConversationMemoryPolicyCommand,
    SetMemoryAccessTierCommand,
    SupersedeMemoryCommand,
)
from stata_research_agent.domain.identifiers import (
    ConversationId,
    MemoryCompactionCheckpointId,
    MemoryEpisodeId,
    MemoryItemId,
    MemoryRevisionId,
    MemoryStateHistoryId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

from .atomic_commit import (
    AtomicCommitService,
    CommandReceipt,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SqliteMemoryRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    def create(self, command: CreateMemoryCommand, identity: MemoryIdentity) -> MemoryOutcome:
        lifecycle = command.requested_lifecycle or (
            MemoryLifecycle.ACTIVE
            if command.origin in {MemoryOriginKind.EXPLICIT_USER, MemoryOriginKind.CONFIRMED}
            else MemoryLifecycle.PROPOSED
        )
        request = {
            "memory_kind": command.kind.value,
            "scope_kind": command.scope_kind.value,
            "research_path_id": None
            if command.research_path_id is None
            else command.research_path_id.value,
            "title": command.title,
            "content_sha256": _sha256(command.content),
            "origin": command.origin.value,
            "lifecycle": lifecycle.value,
            "sources": [
                {
                    "object_type": source.object_type,
                    "object_id": source.object_id,
                    "object_revision": source.object_revision,
                    "role": source.role.value,
                }
                for source in command.sources
            ],
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            scope_object_id = self._resolve_scope(
                connection,
                command.scope_kind,
                command.research_path_id.value if command.research_path_id is not None else None,
            )
            connection.execute(
                "INSERT INTO memory_items VALUES (?, ?, ?, ?, ?)",
                (
                    identity.memory_item_id.value,
                    command.scope_kind.value,
                    scope_object_id,
                    command.kind.value,
                    revision.value,
                ),
            )
            self._insert_revision(
                connection,
                revision,
                memory_item_id=identity.memory_item_id.value,
                memory_revision_id=identity.memory_revision_id.value,
                revision_number=1,
                title=command.title,
                content=command.content,
                origin=command.origin,
                sources=command.sources,
                source_ids=tuple(source_id.value for source_id in identity.source_ids),
            )
            connection.execute(
                "INSERT INTO memory_current_states VALUES (?, ?, ?, 1, ?)",
                (
                    identity.memory_item_id.value,
                    identity.memory_revision_id.value,
                    lifecycle.value,
                    revision.value,
                ),
            )
            access_tier = (
                MemoryAccessTier.HOT
                if lifecycle is MemoryLifecycle.ACTIVE
                else MemoryAccessTier.WARM
            )
            pinned = int(
                command.kind.value
                in {"research_decision", "research_constraint", "unresolved_question"}
            )
            connection.execute(
                """
                INSERT INTO memory_retention_states(
                    memory_item_id, access_tier, pinned, retention_revision,
                    last_reinforced_revision, superseded_by_memory_item_id,
                    superseded_revision, policy_revision, updated_revision
                ) VALUES (?, ?, ?, 1, ?, NULL, NULL, 'memory-retention/v1', ?)
                """,
                (
                    identity.memory_item_id.value,
                    access_tier.value,
                    pinned,
                    revision.value if lifecycle is MemoryLifecycle.ACTIVE else None,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO memory_retention_history(
                    memory_retention_history_id, memory_item_id, access_tier, pinned,
                    retention_revision, superseded_by_memory_item_id, reason_code,
                    policy_revision, created_revision
                ) VALUES (?, ?, ?, ?, 1, NULL, 'memory.created',
                          'memory-retention/v1', ?)
                """,
                (
                    f"memoryretention_initial_{identity.memory_item_id.value}",
                    identity.memory_item_id.value,
                    access_tier.value,
                    pinned,
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO memory_state_history VALUES (?, ?, ?, ?, ?, ?)",
                (
                    identity.state_history_id.value,
                    identity.memory_item_id.value,
                    identity.memory_revision_id.value,
                    lifecycle.value,
                    "memory.created",
                    revision.value,
                ),
            )
            self._rebuild_summary_rows(connection, revision.value)
            response = self._response(
                identity.memory_item_id.value,
                identity.memory_revision_id.value,
                lifecycle,
                1,
            )
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "memory.created",
                        "memory_item",
                        identity.memory_item_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("memory.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="memory.create",
            request=request,
            mutation=mutate,
        )
        return self._outcome(receipt)

    def revise(
        self, command: ReviseMemoryCommand, identity: MemoryRevisionIdentity
    ) -> MemoryOutcome:
        request = {
            "memory_item_id": command.memory_item_id.value,
            "expected_pointer_revision": command.expected_pointer_revision,
            "title": command.title,
            "content_sha256": _sha256(command.content),
            "origin": command.origin.value,
            "lifecycle": command.requested_lifecycle.value,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            current = self._current(connection, command.memory_item_id)
            retention = self._retention(connection, command.memory_item_id)
            self._require_pointer(current, command.expected_pointer_revision)
            revision_number = int(
                connection.execute(
                    """
                    SELECT MAX(revision_number) + 1
                    FROM memory_revisions WHERE memory_item_id = ?
                    """,
                    (command.memory_item_id.value,),
                ).fetchone()[0]
            )
            self._insert_revision(
                connection,
                revision,
                memory_item_id=command.memory_item_id.value,
                memory_revision_id=identity.memory_revision_id.value,
                revision_number=revision_number,
                title=command.title,
                content=command.content,
                origin=command.origin,
                sources=command.sources,
                source_ids=tuple(source_id.value for source_id in identity.source_ids),
            )
            pointer_revision = command.expected_pointer_revision + 1
            cursor = connection.execute(
                """
                UPDATE memory_current_states
                SET current_revision_id = ?, lifecycle = ?, pointer_revision = ?,
                    updated_revision = ?
                WHERE memory_item_id = ? AND pointer_revision = ?
                """,
                (
                    identity.memory_revision_id.value,
                    command.requested_lifecycle.value,
                    pointer_revision,
                    revision.value,
                    command.memory_item_id.value,
                    command.expected_pointer_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("Memory pointer changed during revision")
            next_tier = (
                MemoryAccessTier.HOT
                if command.requested_lifecycle is MemoryLifecycle.ACTIVE
                else MemoryAccessTier.WARM
            )
            connection.execute(
                """
                UPDATE memory_retention_states
                SET access_tier = ?, retention_revision = retention_revision + 1,
                    last_reinforced_revision = ?, superseded_by_memory_item_id = NULL,
                    superseded_revision = NULL, updated_revision = ?
                WHERE memory_item_id = ?
                """,
                (
                    next_tier.value,
                    revision.value
                    if command.requested_lifecycle is MemoryLifecycle.ACTIVE
                    else None,
                    revision.value,
                    command.memory_item_id.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO memory_retention_history(
                    memory_retention_history_id, memory_item_id, access_tier, pinned,
                    retention_revision, superseded_by_memory_item_id, reason_code,
                    policy_revision, created_revision
                ) VALUES (?, ?, ?, ?, ?, NULL, 'memory.revised',
                          'memory-retention/v1', ?)
                """,
                (
                    f"memoryretention_revision_{identity.memory_revision_id.value}",
                    command.memory_item_id.value,
                    next_tier.value,
                    int(retention["pinned"]),
                    int(retention["retention_revision"]) + 1,
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO memory_state_history VALUES (?, ?, ?, ?, ?, ?)",
                (
                    identity.state_history_id.value,
                    command.memory_item_id.value,
                    identity.memory_revision_id.value,
                    command.requested_lifecycle.value,
                    "memory.revised",
                    revision.value,
                ),
            )
            self._rebuild_summary_rows(connection, revision.value)
            response = self._response(
                command.memory_item_id.value,
                identity.memory_revision_id.value,
                command.requested_lifecycle,
                pointer_revision,
            )
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "memory.revised", "memory_item", command.memory_item_id.value, response
                    ),
                ),
                (OutboxDraft("memory.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="memory.revise",
            request=request,
            mutation=mutate,
        )
        return self._outcome(receipt)

    def activate(
        self, command: ActivateMemoryCommand, state_history_id: MemoryStateHistoryId
    ) -> MemoryOutcome:
        request = {
            "memory_item_id": command.memory_item_id.value,
            "memory_revision_id": command.memory_revision_id.value,
            "expected_pointer_revision": command.expected_pointer_revision,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            current = self._current(connection, command.memory_item_id)
            retention = self._retention(connection, command.memory_item_id)
            self._require_pointer(current, command.expected_pointer_revision)
            if str(current["current_revision_id"]) != command.memory_revision_id.value:
                raise ValueError("only the current Memory Revision can be activated")
            pointer_revision = command.expected_pointer_revision + 1
            cursor = connection.execute(
                """
                UPDATE memory_current_states
                SET lifecycle = 'active', pointer_revision = ?, updated_revision = ?
                WHERE memory_item_id = ? AND pointer_revision = ?
                """,
                (
                    pointer_revision,
                    revision.value,
                    command.memory_item_id.value,
                    command.expected_pointer_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("Memory pointer changed during activation")
            connection.execute(
                """
                UPDATE memory_retention_states
                SET access_tier = 'hot', retention_revision = retention_revision + 1,
                    last_reinforced_revision = ?, superseded_by_memory_item_id = NULL,
                    superseded_revision = NULL, updated_revision = ?
                WHERE memory_item_id = ?
                """,
                (revision.value, revision.value, command.memory_item_id.value),
            )
            connection.execute(
                """
                INSERT INTO memory_retention_history(
                    memory_retention_history_id, memory_item_id, access_tier, pinned,
                    retention_revision, superseded_by_memory_item_id, reason_code,
                    policy_revision, created_revision
                ) VALUES (?, ?, 'hot', ?, ?, NULL, 'memory.activated',
                          'memory-retention/v1', ?)
                """,
                (
                    f"memoryretention_activation_{state_history_id.value}",
                    command.memory_item_id.value,
                    int(retention["pinned"]),
                    int(retention["retention_revision"]) + 1,
                    revision.value,
                ),
            )
            connection.execute(
                "INSERT INTO memory_state_history VALUES (?, ?, ?, 'active', ?, ?)",
                (
                    state_history_id.value,
                    command.memory_item_id.value,
                    command.memory_revision_id.value,
                    "memory.activated",
                    revision.value,
                ),
            )
            self._rebuild_summary_rows(connection, revision.value)
            response = self._response(
                command.memory_item_id.value,
                command.memory_revision_id.value,
                MemoryLifecycle.ACTIVE,
                pointer_revision,
            )
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "memory.activated",
                        "memory_item",
                        command.memory_item_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("memory.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="memory.activate",
            request=request,
            mutation=mutate,
        )
        return self._outcome(receipt)

    def retract(
        self, command: RetractMemoryCommand, state_history_id: MemoryStateHistoryId
    ) -> MemoryOutcome:
        request = {
            "memory_item_id": command.memory_item_id.value,
            "expected_pointer_revision": command.expected_pointer_revision,
            "reason": command.reason,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            current = self._current(connection, command.memory_item_id)
            retention = self._retention(connection, command.memory_item_id)
            self._require_pointer(current, command.expected_pointer_revision)
            pointer_revision = command.expected_pointer_revision + 1
            cursor = connection.execute(
                """
                UPDATE memory_current_states
                SET lifecycle = 'retracted', pointer_revision = ?, updated_revision = ?
                WHERE memory_item_id = ? AND pointer_revision = ?
                """,
                (
                    pointer_revision,
                    revision.value,
                    command.memory_item_id.value,
                    command.expected_pointer_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("Memory pointer changed during retraction")
            connection.execute(
                """
                UPDATE memory_retention_states
                SET access_tier = 'archived', retention_revision = retention_revision + 1,
                    updated_revision = ? WHERE memory_item_id = ?
                """,
                (revision.value, command.memory_item_id.value),
            )
            connection.execute(
                """
                INSERT INTO memory_retention_history(
                    memory_retention_history_id, memory_item_id, access_tier, pinned,
                    retention_revision, superseded_by_memory_item_id, reason_code,
                    policy_revision, created_revision
                ) VALUES (?, ?, 'archived', ?, ?, NULL, ?, 'memory-retention/v1', ?)
                """,
                (
                    f"memoryretention_retraction_{state_history_id.value}",
                    command.memory_item_id.value,
                    int(retention["pinned"]),
                    int(retention["retention_revision"]) + 1,
                    command.reason,
                    revision.value,
                ),
            )
            memory_revision_id = str(current["current_revision_id"])
            connection.execute(
                "INSERT INTO memory_state_history VALUES (?, ?, ?, 'retracted', ?, ?)",
                (
                    state_history_id.value,
                    command.memory_item_id.value,
                    memory_revision_id,
                    command.reason,
                    revision.value,
                ),
            )
            self._rebuild_summary_rows(connection, revision.value)
            response = self._response(
                command.memory_item_id.value,
                memory_revision_id,
                MemoryLifecycle.RETRACTED,
                pointer_revision,
            )
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "memory.retracted",
                        "memory_item",
                        command.memory_item_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("memory.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="memory.retract",
            request=request,
            mutation=mutate,
        )
        return self._outcome(receipt)

    def set_access_tier(
        self, command: SetMemoryAccessTierCommand, identity: MemoryRetentionIdentity
    ) -> MemoryRetentionOutcome:
        request = {
            "memory_item_id": command.memory_item_id.value,
            "expected_retention_revision": command.expected_retention_revision,
            "access_tier": command.access_tier.value,
            "reason": command.reason,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            current = self._retention(connection, command.memory_item_id)
            if int(current["retention_revision"]) != command.expected_retention_revision:
                raise ValueError("Memory retention pointer changed")
            if (
                current["superseded_by_memory_item_id"] is not None
                and command.access_tier is not MemoryAccessTier.ARCHIVED
            ):
                raise ValueError("superseded Memory must remain archived")
            next_revision = command.expected_retention_revision + 1
            cursor = connection.execute(
                """
                UPDATE memory_retention_states
                SET access_tier = ?, retention_revision = ?, updated_revision = ?
                WHERE memory_item_id = ? AND retention_revision = ?
                """,
                (
                    command.access_tier.value,
                    next_revision,
                    revision.value,
                    command.memory_item_id.value,
                    command.expected_retention_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("Memory retention changed concurrently")
            connection.execute(
                """
                INSERT INTO memory_retention_history(
                    memory_retention_history_id, memory_item_id, access_tier, pinned,
                    retention_revision, superseded_by_memory_item_id, reason_code,
                    policy_revision, created_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'memory-retention/v1', ?)
                """,
                (
                    identity.history_id.value,
                    command.memory_item_id.value,
                    command.access_tier.value,
                    int(current["pinned"]),
                    next_revision,
                    current["superseded_by_memory_item_id"],
                    command.reason,
                    revision.value,
                ),
            )
            self._rebuild_summary_rows(connection, revision.value)
            response = {
                "memory_item_id": command.memory_item_id.value,
                "access_tier": command.access_tier.value,
                "retention_revision": next_revision,
                "superseded_by_memory_item_id": current["superseded_by_memory_item_id"],
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "memory.access_tier_changed",
                        "memory_item",
                        command.memory_item_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("memory.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="memory.set_access_tier",
            request=request,
            mutation=mutate,
        )
        return self._retention_outcome(receipt)

    def supersede(
        self, command: SupersedeMemoryCommand, identity: MemoryRetentionIdentity
    ) -> MemoryRetentionOutcome:
        request = {
            "memory_item_id": command.memory_item_id.value,
            "successor_memory_item_id": command.successor_memory_item_id.value,
            "expected_retention_revision": command.expected_retention_revision,
            "reason": command.reason,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            current = self._retention(connection, command.memory_item_id)
            if int(current["retention_revision"]) != command.expected_retention_revision:
                raise ValueError("Memory retention pointer changed")
            pair = connection.execute(
                """
                SELECT old.scope_kind AS old_scope_kind,
                       old.scope_object_id AS old_scope_object_id,
                       old.memory_kind AS old_kind,
                       successor.scope_kind AS successor_scope_kind,
                       successor.scope_object_id AS successor_scope_object_id,
                       successor.memory_kind AS successor_kind,
                       successor_state.lifecycle AS successor_lifecycle
                FROM memory_items AS old
                JOIN memory_items AS successor ON successor.memory_item_id = ?
                JOIN memory_current_states AS successor_state
                  ON successor_state.memory_item_id = successor.memory_item_id
                WHERE old.memory_item_id = ?
                """,
                (command.successor_memory_item_id.value, command.memory_item_id.value),
            ).fetchone()
            if pair is None or str(pair["successor_lifecycle"]) != "active":
                raise ValueError("successor Memory must exist and be active")
            if (
                str(pair["old_scope_kind"]),
                str(pair["old_scope_object_id"]),
                str(pair["old_kind"]),
            ) != (
                str(pair["successor_scope_kind"]),
                str(pair["successor_scope_object_id"]),
                str(pair["successor_kind"]),
            ):
                raise ValueError("Memory supersession requires the same scope and kind")
            next_revision = command.expected_retention_revision + 1
            cursor = connection.execute(
                """
                UPDATE memory_retention_states
                SET access_tier = 'archived', retention_revision = ?,
                    superseded_by_memory_item_id = ?, superseded_revision = ?,
                    updated_revision = ?
                WHERE memory_item_id = ? AND retention_revision = ?
                """,
                (
                    next_revision,
                    command.successor_memory_item_id.value,
                    revision.value,
                    revision.value,
                    command.memory_item_id.value,
                    command.expected_retention_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("Memory retention changed concurrently")
            connection.execute(
                """
                INSERT INTO memory_retention_history(
                    memory_retention_history_id, memory_item_id, access_tier, pinned,
                    retention_revision, superseded_by_memory_item_id, reason_code,
                    policy_revision, created_revision
                ) VALUES (?, ?, 'archived', ?, ?, ?, ?, 'memory-retention/v1', ?)
                """,
                (
                    identity.history_id.value,
                    command.memory_item_id.value,
                    int(current["pinned"]),
                    next_revision,
                    command.successor_memory_item_id.value,
                    command.reason,
                    revision.value,
                ),
            )
            self._rebuild_summary_rows(connection, revision.value)
            response = {
                "memory_item_id": command.memory_item_id.value,
                "access_tier": MemoryAccessTier.ARCHIVED.value,
                "retention_revision": next_revision,
                "superseded_by_memory_item_id": command.successor_memory_item_id.value,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "memory.superseded",
                        "memory_item",
                        command.memory_item_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("memory.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="memory.supersede",
            request=request,
            mutation=mutate,
        )
        return self._retention_outcome(receipt)

    def set_conversation_policy(
        self, command: SetConversationMemoryPolicyCommand
    ) -> ConversationMemoryPolicyOutcome:
        request = {
            "conversation_id": command.conversation_id.value,
            "use_memory": command.use_memory,
            "contribute_memory": command.contribute_memory,
            "expected_policy_revision": command.expected_policy_revision,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            exists = connection.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = ?",
                (command.conversation_id.value,),
            ).fetchone()
            if exists is None:
                raise ValueError("Conversation does not exist")
            current = connection.execute(
                """
                SELECT policy_revision FROM conversation_memory_policies
                WHERE conversation_id = ?
                """,
                (command.conversation_id.value,),
            ).fetchone()
            if current is None:
                if command.expected_policy_revision not in {None, 0}:
                    raise ValueError("Conversation Memory Policy revision changed")
                policy_revision = 1
                connection.execute(
                    "INSERT INTO conversation_memory_policies VALUES (?, ?, ?, 1, ?)",
                    (
                        command.conversation_id.value,
                        int(command.use_memory),
                        int(command.contribute_memory),
                        revision.value,
                    ),
                )
            else:
                actual = int(current["policy_revision"])
                if command.expected_policy_revision != actual:
                    raise ValueError("Conversation Memory Policy revision changed")
                policy_revision = actual + 1
                connection.execute(
                    """
                    UPDATE conversation_memory_policies
                    SET use_memory = ?, contribute_memory = ?, policy_revision = ?,
                        updated_revision = ?
                    WHERE conversation_id = ?
                    """,
                    (
                        int(command.use_memory),
                        int(command.contribute_memory),
                        policy_revision,
                        revision.value,
                        command.conversation_id.value,
                    ),
                )
            response = {
                "conversation_id": command.conversation_id.value,
                "use_memory": command.use_memory,
                "contribute_memory": command.contribute_memory,
                "policy_revision": policy_revision,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "memory.policy_changed",
                        "conversation",
                        command.conversation_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("memory.policy_changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="memory.policy.set",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return ConversationMemoryPolicyOutcome(
            ConversationId(str(response["conversation_id"])),
            bool(response["use_memory"]),
            bool(response["contribute_memory"]),
            int(response["policy_revision"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    def record_episode(
        self, command: RecordMemoryEpisodeCommand, memory_episode_id: MemoryEpisodeId
    ) -> MemoryEpisodeOutcome:
        request = {
            "conversation_id": command.conversation_id.value,
            "source_start_revision": command.source_start_revision,
            "source_end_revision": command.source_end_revision,
            "summary_sha256": _sha256(command.summary),
            "extractor_kind": command.extractor_kind,
            "extractor_revision": command.extractor_revision,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            policy = connection.execute(
                """
                SELECT conversation.conversation_id,
                       COALESCE(policy.contribute_memory, 1) AS contribute_memory
                FROM conversations AS conversation
                LEFT JOIN conversation_memory_policies AS policy USING (conversation_id)
                WHERE conversation.conversation_id = ?
                """,
                (command.conversation_id.value,),
            ).fetchone()
            if policy is None:
                raise ValueError("Conversation does not exist")
            if not bool(policy["contribute_memory"]):
                raise ValueError("Conversation is not eligible to contribute Memory")
            if command.source_end_revision >= revision.value:
                raise ValueError("Memory Episode source watermark must precede its commit")
            connection.execute(
                "INSERT INTO memory_episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    memory_episode_id.value,
                    command.conversation_id.value,
                    command.source_start_revision,
                    command.source_end_revision,
                    command.summary,
                    _sha256(command.summary),
                    command.extractor_kind,
                    command.extractor_revision,
                    revision.value,
                ),
            )
            response = {"memory_episode_id": memory_episode_id.value}
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "memory.episode_recorded",
                        "memory_episode",
                        memory_episode_id.value,
                        request,
                    ),
                ),
                (OutboxDraft("memory.episode_recorded", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="memory.episode.record",
            request=request,
            mutation=mutate,
        )
        return MemoryEpisodeOutcome(
            MemoryEpisodeId(str(receipt.response["memory_episode_id"])),
            receipt.commit_revision,
            receipt.replayed,
        )

    def record_compaction_checkpoint(
        self,
        command: RecordMemoryCompactionCheckpointCommand,
        checkpoint_id: MemoryCompactionCheckpointId,
    ) -> MemoryCompactionCheckpointOutcome:
        request = {
            "conversation_id": command.conversation_id.value,
            "source_start_revision": command.source_start_revision,
            "source_end_revision": command.source_end_revision,
            "required_source_message_ids": list(command.required_source_message_ids),
            "policy_revision": command.policy_revision,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            uncovered: list[str] = []
            for message_id in command.required_source_message_ids:
                message = connection.execute(
                    """
                    SELECT created_revision FROM messages
                    WHERE message_id = ? AND conversation_id = ?
                      AND created_revision BETWEEN ? AND ?
                    """,
                    (
                        message_id,
                        command.conversation_id.value,
                        command.source_start_revision,
                        command.source_end_revision,
                    ),
                ).fetchone()
                if message is None:
                    raise ValueError("Compaction checkpoint source Message is outside its window")
                represented = connection.execute(
                    """
                    SELECT 1
                    FROM memory_revision_sources AS source
                    JOIN memory_revisions AS memory
                      ON memory.memory_revision_id = source.memory_revision_id
                    JOIN memory_current_states AS state
                      ON state.memory_item_id = memory.memory_item_id
                    WHERE source.source_object_type = 'message'
                      AND source.source_object_id = ?
                      AND state.lifecycle IN ('active', 'proposed')
                    LIMIT 1
                    """,
                    (message_id,),
                ).fetchone()
                if represented is None:
                    uncovered.append(message_id)
            disposition = "ready" if not uncovered else "blocked"
            connection.execute(
                """
                INSERT INTO memory_compaction_checkpoints VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id.value,
                    command.conversation_id.value,
                    command.source_start_revision,
                    command.source_end_revision,
                    disposition,
                    json.dumps(uncovered, separators=(",", ":")),
                    command.policy_revision,
                    revision.value,
                ),
            )
            response = {
                "memory_compaction_checkpoint_id": checkpoint_id.value,
                "disposition": disposition,
                "uncovered_source_message_ids": uncovered,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "memory.compaction_checkpointed",
                        "memory_compaction_checkpoint",
                        checkpoint_id.value,
                        response,
                    ),
                ),
                (),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="memory.compaction.checkpoint",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return MemoryCompactionCheckpointOutcome(
            MemoryCompactionCheckpointId(str(response["memory_compaction_checkpoint_id"])),
            str(response["disposition"]),
            tuple(str(value) for value in response["uncovered_source_message_ids"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    @staticmethod
    def _insert_revision(
        connection: sqlite3.Connection,
        revision: WorkspaceRevision,
        *,
        memory_item_id: str,
        memory_revision_id: str,
        revision_number: int,
        title: str,
        content: str,
        origin: MemoryOriginKind,
        sources: tuple[MemorySource, ...],
        source_ids: tuple[str, ...],
    ) -> None:
        if len(sources) != len(source_ids):
            raise ValueError("Memory source identity count mismatch")
        connection.execute(
            "INSERT INTO memory_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                memory_revision_id,
                memory_item_id,
                revision_number,
                title,
                content,
                _sha256(content),
                origin.value,
                revision.value,
            ),
        )
        for source_id, source in zip(source_ids, sources, strict=True):
            SqliteMemoryRepository._validate_source(connection, source)
            connection.execute(
                "INSERT INTO memory_revision_sources VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    memory_revision_id,
                    source.object_type,
                    source.object_id,
                    source.object_revision,
                    source.role.value,
                    revision.value,
                ),
            )

    @staticmethod
    def _validate_source(connection: sqlite3.Connection, source: MemorySource) -> None:
        if source.role in {
            MemorySourceRole.USER_STATEMENT,
            MemorySourceRole.USER_CONFIRMATION,
        }:
            if (
                source.role is MemorySourceRole.USER_CONFIRMATION
                and source.object_type == "memory_control_command"
            ):
                # The containing idempotent command receipt and Journal entry become
                # authoritative in this same UoW; no Message is fabricated for a UI edit.
                if not source.object_id.startswith("cmd_") or not source.object_revision.isdigit():
                    raise ValueError("Memory control source identity is invalid")
                return
            if source.object_type != "message":
                raise ValueError("user-backed Memory must cite a Message")
            row = connection.execute(
                "SELECT created_revision FROM messages WHERE message_id = ?",
                (source.object_id,),
            ).fetchone()
            if row is None or str(row["created_revision"]) != source.object_revision:
                raise ValueError("Memory Message source identity is invalid")
        elif source.role is MemorySourceRole.ASSISTANT_INFERENCE:
            if source.object_type != "assistant_output":
                raise ValueError("inferred Memory must cite an Assistant Output")
            row = connection.execute(
                """
                SELECT created_revision FROM assistant_outputs
                WHERE assistant_output_id = ?
                """,
                (source.object_id,),
            ).fetchone()
            if row is None or str(row["created_revision"]) != source.object_revision:
                raise ValueError("Memory Assistant Output source identity is invalid")

    @staticmethod
    def _resolve_scope(
        connection: sqlite3.Connection,
        scope_kind: MemoryScopeKind,
        research_path_id: str | None,
    ) -> str:
        if scope_kind is MemoryScopeKind.WORKSPACE:
            row = connection.execute(
                "SELECT workspace_id FROM workspace_identity WHERE singleton_id = 1"
            ).fetchone()
            if row is None:
                raise ValueError("Workspace identity is unavailable")
            return str(row["workspace_id"])
        row = connection.execute(
            """
            SELECT path.research_path_id
            FROM research_paths AS path
            JOIN research_path_profiles AS profile USING (research_path_id)
            WHERE path.research_path_id = ? AND profile.lifecycle = 'active'
            """,
            (research_path_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Research Path does not exist or is inactive")
        return str(row["research_path_id"])

    @staticmethod
    def _current(connection: sqlite3.Connection, memory_item_id: MemoryItemId) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT current_revision_id, lifecycle, pointer_revision
            FROM memory_current_states WHERE memory_item_id = ?
            """,
            (memory_item_id.value,),
        ).fetchone()
        if row is None:
            raise ValueError("Memory Item does not exist")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _retention(connection: sqlite3.Connection, memory_item_id: MemoryItemId) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT access_tier, pinned, retention_revision,
                   superseded_by_memory_item_id
            FROM memory_retention_states WHERE memory_item_id = ?
            """,
            (memory_item_id.value,),
        ).fetchone()
        if row is None:
            raise ValueError("Memory retention state does not exist")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _require_pointer(current: sqlite3.Row, expected: int) -> None:
        if int(current["pointer_revision"]) != expected:
            raise ValueError("Memory pointer revision changed")

    @staticmethod
    def _response(
        memory_item_id: str,
        memory_revision_id: str,
        lifecycle: MemoryLifecycle,
        pointer_revision: int,
    ) -> dict[str, object]:
        return {
            "memory_item_id": memory_item_id,
            "memory_revision_id": memory_revision_id,
            "lifecycle": lifecycle.value,
            "pointer_revision": pointer_revision,
        }

    @staticmethod
    def _outcome(receipt: CommandReceipt) -> MemoryOutcome:
        response = receipt.response
        return MemoryOutcome(
            MemoryItemId(str(response["memory_item_id"])),
            MemoryRevisionId(str(response["memory_revision_id"])),
            MemoryLifecycle(str(response["lifecycle"])),
            int(response["pointer_revision"]),
            receipt.commit_revision,
            receipt.replayed,
        )

    @staticmethod
    def _retention_outcome(receipt: CommandReceipt) -> MemoryRetentionOutcome:
        response = receipt.response
        successor = response.get("superseded_by_memory_item_id")
        return MemoryRetentionOutcome(
            MemoryItemId(str(response["memory_item_id"])),
            MemoryAccessTier(str(response["access_tier"])),
            int(response["retention_revision"]),
            None if successor is None else MemoryItemId(str(successor)),
            receipt.commit_revision,
            receipt.replayed,
        )

    @staticmethod
    def _rebuild_summary_rows(connection: sqlite3.Connection, source_revision: int) -> None:
        workspace_id = str(
            connection.execute(
                "SELECT workspace_id FROM workspace_identity WHERE singleton_id = 1"
            ).fetchone()[0]
        )
        scopes = [("workspace", workspace_id)]
        scopes.extend(
            (str(row["scope_kind"]), str(row["scope_object_id"]))
            for row in connection.execute(
                """
                SELECT DISTINCT item.scope_kind, item.scope_object_id
                FROM memory_items AS item
                WHERE item.scope_kind = 'research_path'
                """
            ).fetchall()
        )
        for scope_kind, scope_object_id in scopes:
            rows = connection.execute(
                """
                SELECT item.memory_kind, revision.title, revision.content
                FROM memory_items AS item
                JOIN memory_current_states AS state USING (memory_item_id)
                JOIN memory_retention_states AS retention USING (memory_item_id)
                JOIN memory_revisions AS revision
                  ON revision.memory_revision_id = state.current_revision_id
                WHERE state.lifecycle = 'active' AND item.scope_kind = ?
                  AND item.scope_object_id = ?
                  AND retention.access_tier = 'hot'
                  AND retention.superseded_by_memory_item_id IS NULL
                ORDER BY
                  CASE item.memory_kind
                    WHEN 'research_constraint' THEN 0
                    WHEN 'research_decision' THEN 1
                    WHEN 'unresolved_question' THEN 2
                    ELSE 3
                  END,
                  revision.created_revision DESC
                LIMIT 64
                """,
                (scope_kind, scope_object_id),
            ).fetchall()
            lines = ["Project Memory index (navigation only; not Evidence or current truth):"]
            for row in rows:
                line = f"- [{row['memory_kind']}] {row['title']}: {row['content']}"
                if len("\n".join([*lines, line]).encode("utf-8")) > 10_240:
                    break
                lines.append(line)
            summary = "\n".join(lines)
            prior = connection.execute(
                """
                SELECT projection_revision FROM memory_summary_projections
                WHERE scope_kind = ? AND scope_object_id = ?
                """,
                (scope_kind, scope_object_id),
            ).fetchone()
            projection_revision = 1 if prior is None else int(prior[0]) + 1
            connection.execute(
                """
                INSERT INTO memory_summary_projections VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_kind, scope_object_id) DO UPDATE SET
                  summary_text = excluded.summary_text,
                  summary_sha256 = excluded.summary_sha256,
                  source_revision = excluded.source_revision,
                  projection_revision = excluded.projection_revision
                """,
                (
                    scope_kind,
                    scope_object_id,
                    summary,
                    _sha256(summary),
                    source_revision,
                    projection_revision,
                ),
            )
