"""SQLite authority for explicit user outcome feedback."""

from __future__ import annotations

import sqlite3

from stata_research_agent.application.outcome_feedback import (
    OutcomeDisposition,
    RecordTurnOutcomeFeedbackCommand,
    TurnOutcomeFeedbackOutcome,
)
from stata_research_agent.domain.identifiers import TurnId, TurnOutcomeFeedbackId
from stata_research_agent.domain.revisions import WorkspaceRevision

from .atomic_commit import (
    AtomicCommitService,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
    canonical_json,
)


class SqliteTurnOutcomeFeedbackRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._commits = AtomicCommitService(connection)

    def record(
        self,
        command: RecordTurnOutcomeFeedbackCommand,
        feedback_id: TurnOutcomeFeedbackId,
    ) -> TurnOutcomeFeedbackOutcome:
        request = {
            "turn_id": command.turn_id.value,
            "disposition": command.disposition.value,
            "ratings": [
                {"dimension": rating.dimension, "score": rating.score} for rating in command.ratings
            ],
            "issue_codes": list(command.issue_codes),
            "comment": command.comment,
            "policy_revision": command.policy_revision,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            turn = connection.execute(
                "SELECT status FROM turns WHERE turn_id = ?",
                (command.turn_id.value,),
            ).fetchone()
            if turn is None:
                raise ValueError("Turn does not exist")
            if str(turn["status"]) in {"queued", "running", "waiting"}:
                raise ValueError("outcome feedback requires a terminal Turn")
            connection.execute(
                """
                INSERT INTO turn_outcome_feedback(
                    turn_outcome_feedback_id, turn_id, disposition, ratings_json,
                    issue_codes_json, comment, source, policy_revision, created_revision
                ) VALUES (?, ?, ?, ?, ?, ?, 'explicit_user', ?, ?)
                """,
                (
                    feedback_id.value,
                    command.turn_id.value,
                    command.disposition.value,
                    canonical_json(request["ratings"]),
                    canonical_json(request["issue_codes"]),
                    command.comment,
                    command.policy_revision,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO skill_outcome_observations(
                    turn_outcome_feedback_id, context_item_id,
                    relationship_kind, created_revision
                )
                SELECT ?, use.context_item_id, 'co_occurrence_not_causation', ?
                FROM skill_context_uses AS use
                JOIN context_items AS item USING (context_item_id)
                JOIN context_manifests AS manifest USING (context_manifest_id)
                JOIN steps AS step USING (step_id)
                WHERE step.turn_id = ?
                """,
                (feedback_id.value, revision.value, command.turn_id.value),
            )
            response = {
                "feedback_id": feedback_id.value,
                "turn_id": command.turn_id.value,
                "disposition": command.disposition.value,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "turn.outcome_feedback.recorded",
                        "turn_outcome_feedback",
                        feedback_id.value,
                        {
                            **response,
                            "issue_codes": list(command.issue_codes),
                            "policy_revision": command.policy_revision,
                        },
                    ),
                ),
                (OutboxDraft("workspace.changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="turn.outcome_feedback.record",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return TurnOutcomeFeedbackOutcome(
            TurnOutcomeFeedbackId(str(response["feedback_id"])),
            TurnId(str(response["turn_id"])),
            OutcomeDisposition(str(response["disposition"])),
            receipt.commit_revision,
            receipt.replayed,
        )
