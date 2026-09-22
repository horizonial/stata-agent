"""Atomic authoritative commit with Journal, Outbox, and idempotent Receipt."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from stata_research_agent.domain.identifiers import CommandId
from stata_research_agent.domain.revisions import WorkspaceRevision

from .errors import CommandConflictError

JsonObject = Mapping[str, Any]


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class CommitCrashPoint(StrEnum):
    BEFORE_BEGIN = "before_begin"
    AFTER_BEGIN = "after_begin"
    AFTER_WORKSPACE_COMMIT = "after_workspace_commit"
    AFTER_DOMAIN_MUTATION = "after_domain_mutation"
    AFTER_JOURNAL = "after_journal"
    AFTER_OUTBOX = "after_outbox"
    AFTER_RECEIPT = "after_receipt"
    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT = "after_commit"


@dataclass(frozen=True, slots=True)
class JournalDraft:
    event_type: str
    object_type: str
    object_id: str
    payload: JsonObject


@dataclass(frozen=True, slots=True)
class OutboxDraft:
    topic: str
    payload: JsonObject


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    command_id: CommandId
    command_type: str
    request_hash: str
    outcome: str
    response: JsonObject
    commit_revision: WorkspaceRevision
    replayed: bool


CrashInjector = Callable[[CommitCrashPoint], None]


@dataclass(frozen=True, slots=True)
class MutationPayload:
    response: JsonObject
    journal: Sequence[JournalDraft]
    outbox: Sequence[OutboxDraft]


Mutation = Callable[[sqlite3.Connection, WorkspaceRevision], MutationPayload]


class AtomicCommitService:
    """The sole M0-04 transaction that creates commit history and delivery facts."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def commit(
        self,
        *,
        command_id: CommandId,
        command_type: str,
        request: JsonObject,
        response: JsonObject,
        journal: Sequence[JournalDraft],
        outbox: Sequence[OutboxDraft],
        crash_injector: CrashInjector | None = None,
    ) -> CommandReceipt:
        def static_mutation(
            _connection: sqlite3.Connection, _revision: WorkspaceRevision
        ) -> MutationPayload:
            return MutationPayload(response=response, journal=journal, outbox=outbox)

        return self.commit_mutation(
            command_id=command_id,
            command_type=command_type,
            request=request,
            mutation=static_mutation,
            crash_injector=crash_injector,
        )

    def commit_mutation(
        self,
        *,
        command_id: CommandId,
        command_type: str,
        request: JsonObject,
        mutation: Mutation,
        crash_injector: CrashInjector | None = None,
    ) -> CommandReceipt:
        if not command_type.strip():
            raise ValueError("command_type is required")
        inject = crash_injector or (lambda _: None)
        request_hash = hashlib.sha256(
            canonical_json({"command_type": command_type, "request": request}).encode("utf-8")
        ).hexdigest()
        inject(CommitCrashPoint.BEFORE_BEGIN)
        self._connection.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            inject(CommitCrashPoint.AFTER_BEGIN)
            existing = self._connection.execute(
                """
                SELECT command_type, request_hash, outcome, response_json, commit_revision
                FROM command_receipts WHERE command_id = ?
                """,
                (command_id.value,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["command_type"]) != command_type
                    or str(existing["request_hash"]) != request_hash
                ):
                    raise CommandConflictError(
                        f"command_id {command_id.value} was reused with different content"
                    )
                self._connection.execute("ROLLBACK")
                return CommandReceipt(
                    command_id=command_id,
                    command_type=command_type,
                    request_hash=request_hash,
                    outcome=str(existing["outcome"]),
                    response=json.loads(str(existing["response_json"])),
                    commit_revision=WorkspaceRevision(int(existing["commit_revision"])),
                    replayed=True,
                )

            next_revision = int(
                self._connection.execute(
                    "SELECT COALESCE(MAX(workspace_revision), 0) + 1 FROM workspace_commits"
                ).fetchone()[0]
            )
            committed_at = datetime.now(UTC).isoformat()
            self._connection.execute(
                """
                INSERT INTO workspace_commits(workspace_revision, command_id, committed_at)
                VALUES (?, ?, ?)
                """,
                (next_revision, command_id.value, committed_at),
            )
            inject(CommitCrashPoint.AFTER_WORKSPACE_COMMIT)

            mutation_payload = mutation(self._connection, WorkspaceRevision(next_revision))
            if not mutation_payload.journal:
                raise ValueError(
                    "an authoritative command commit requires at least one Journal fact"
                )
            response_json = canonical_json(mutation_payload.response)
            inject(CommitCrashPoint.AFTER_DOMAIN_MUTATION)

            for ordinal, journal_draft in enumerate(mutation_payload.journal, start=1):
                self._connection.execute(
                    """
                    INSERT INTO journal_entries(
                        journal_entry_id, workspace_revision, ordinal,
                        event_type, object_type, object_id, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"journal_{uuid4().hex}",
                        next_revision,
                        ordinal,
                        journal_draft.event_type,
                        journal_draft.object_type,
                        journal_draft.object_id,
                        canonical_json(journal_draft.payload),
                    ),
                )
            inject(CommitCrashPoint.AFTER_JOURNAL)

            for ordinal, outbox_draft in enumerate(mutation_payload.outbox, start=1):
                self._connection.execute(
                    """
                    INSERT INTO outbox_entries(
                        outbox_entry_id, workspace_revision, ordinal, topic, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        f"outbox_{uuid4().hex}",
                        next_revision,
                        ordinal,
                        outbox_draft.topic,
                        canonical_json(outbox_draft.payload),
                    ),
                )
            inject(CommitCrashPoint.AFTER_OUTBOX)

            self._connection.execute(
                """
                INSERT INTO command_receipts(
                    command_id, command_type, request_hash, outcome,
                    response_json, commit_revision, created_at
                ) VALUES (?, ?, ?, 'committed', ?, ?, ?)
                """,
                (
                    command_id.value,
                    command_type,
                    request_hash,
                    response_json,
                    next_revision,
                    committed_at,
                ),
            )
            inject(CommitCrashPoint.AFTER_RECEIPT)
            inject(CommitCrashPoint.BEFORE_COMMIT)
            self._connection.execute("COMMIT")
            committed = True
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

        if not committed:  # defensive: every non-replay path must cross COMMIT
            raise RuntimeError("authoritative commit did not reach a terminal state")
        inject(CommitCrashPoint.AFTER_COMMIT)
        return CommandReceipt(
            command_id=command_id,
            command_type=command_type,
            request_hash=request_hash,
            outcome="committed",
            response=mutation_payload.response,
            commit_revision=WorkspaceRevision(next_revision),
            replayed=False,
        )
