"""Authoritative accumulated active-runtime budget for resumable Turns."""

from __future__ import annotations

import sqlite3

from stata_research_agent.application.ports.identity import IdentityGenerator
from stata_research_agent.domain.identifiers import CommandId, TurnId
from stata_research_agent.domain.revisions import WorkspaceRevision

from .atomic_commit import AtomicCommitService, JournalDraft, MutationPayload, OutboxDraft


class SqliteTurnRuntimeBudgetLedger:
    def __init__(self, connection: sqlite3.Connection, identities: IdentityGenerator) -> None:
        self._connection = connection
        self._identities = identities
        self._commits = AtomicCommitService(connection)

    def remaining_seconds(
        self,
        turn_id: TurnId,
        *,
        policy_revision: str,
        configured_max_seconds: float,
    ) -> float:
        if not policy_revision.strip() or configured_max_seconds <= 0:
            raise ValueError("invalid Turn runtime budget policy")
        row = self._connection.execute(
            """
            SELECT policy_revision, max_wall_clock_seconds, consumed_wall_clock_seconds
            FROM turn_runtime_budgets WHERE turn_id = ?
            """,
            (turn_id.value,),
        ).fetchone()
        if row is None:
            self._initialize(turn_id, policy_revision, configured_max_seconds)
            return configured_max_seconds
        if str(row["policy_revision"]) != policy_revision:
            raise ValueError("Turn runtime policy cannot change while the Turn exists")
        frozen_max = float(row["max_wall_clock_seconds"])
        if abs(frozen_max - configured_max_seconds) > 1e-6:
            raise ValueError("Turn runtime maximum cannot change while the Turn exists")
        return max(0.0, frozen_max - float(row["consumed_wall_clock_seconds"]))

    def consume_seconds(self, turn_id: TurnId, elapsed_seconds: float) -> None:
        if elapsed_seconds < 0:
            raise ValueError("consumed Turn runtime cannot be negative")
        if elapsed_seconds == 0:
            return

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            row = connection.execute(
                """
                SELECT max_wall_clock_seconds, consumed_wall_clock_seconds, segment_count
                FROM turn_runtime_budgets WHERE turn_id = ?
                """,
                (turn_id.value,),
            ).fetchone()
            if row is None:
                raise ValueError("Turn runtime budget is not initialized")
            consumed = min(
                float(row["max_wall_clock_seconds"]),
                float(row["consumed_wall_clock_seconds"]) + elapsed_seconds,
            )
            segment_count = int(row["segment_count"]) + 1
            connection.execute(
                """
                UPDATE turn_runtime_budgets
                SET consumed_wall_clock_seconds = ?, segment_count = ?, updated_revision = ?
                WHERE turn_id = ?
                """,
                (consumed, segment_count, revision.value, turn_id.value),
            )
            payload = {
                "turn_id": turn_id.value,
                "consumed_wall_clock_seconds": consumed,
                "segment_count": segment_count,
            }
            return MutationPayload(
                payload,
                (JournalDraft("turn.runtime_budget_consumed", "turn", turn_id.value, payload),),
                (OutboxDraft("turn.changed", payload),),
            )

        self._commits.commit_mutation(
            command_id=self._identities.new(CommandId),
            command_type="turn.consume_runtime_budget",
            request={"turn_id": turn_id.value, "elapsed_seconds": elapsed_seconds},
            mutation=mutate,
        )

    def _initialize(
        self, turn_id: TurnId, policy_revision: str, configured_max_seconds: float
    ) -> None:
        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            connection.execute(
                """
                INSERT INTO turn_runtime_budgets(
                    turn_id, policy_revision, max_wall_clock_seconds,
                    consumed_wall_clock_seconds, segment_count, updated_revision
                ) VALUES (?, ?, ?, 0, 0, ?)
                """,
                (turn_id.value, policy_revision, configured_max_seconds, revision.value),
            )
            payload = {
                "turn_id": turn_id.value,
                "policy_revision": policy_revision,
                "max_wall_clock_seconds": configured_max_seconds,
            }
            return MutationPayload(
                payload,
                (JournalDraft("turn.runtime_budget_initialized", "turn", turn_id.value, payload),),
                (),
            )

        self._commits.commit_mutation(
            command_id=self._identities.new(CommandId),
            command_type="turn.initialize_runtime_budget",
            request={
                "turn_id": turn_id.value,
                "policy_revision": policy_revision,
                "max_wall_clock_seconds": configured_max_seconds,
            },
            mutation=mutate,
        )
