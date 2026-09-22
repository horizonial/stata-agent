"""SQLite authority for independent, advisory Skill evaluation."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from stata_research_agent.application.model_gateway import ProviderResponse
from stata_research_agent.application.skill_evaluation import (
    EvaluateSkillCommand,
    SkillEvaluationJudgment,
    SkillEvaluationOutcome,
    SkillEvaluationPreparation,
    SkillImprovementProposal,
)
from stata_research_agent.domain.identifiers import (
    CommandId,
    SkillEvaluationAttemptId,
    SkillEvaluationRunId,
    SkillImprovementProposalId,
    SkillVersionId,
)
from stata_research_agent.domain.revisions import WorkspaceRevision

from .atomic_commit import (
    AtomicCommitService,
    JournalDraft,
    MutationPayload,
    OutboxDraft,
    canonical_json,
)


class SqliteSkillEvaluationRepository:
    POLICY_REVISION = "skill-evaluation-v1"

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._commits = AtomicCommitService(connection)

    def prepare(
        self,
        *,
        command: EvaluateSkillCommand,
        run_id: SkillEvaluationRunId,
        attempt_id: SkillEvaluationAttemptId,
        provider_profile_id: str,
        credential_version_id: str,
        model_name: str,
        request_builder: Callable[[Mapping[str, Any]], str],
    ) -> SkillEvaluationPreparation:
        candidate, baseline = self._resolve_versions(command)
        source_end = int(
            self._connection.execute(
                "SELECT COALESCE(MAX(workspace_revision), 0) FROM workspace_commits"
            ).fetchone()[0]
        )
        evidence = self._evidence_packet(command.skill_name, candidate, baseline, source_end)
        request_json = request_builder(evidence)
        request = {
            "skill_name": command.skill_name,
            "candidate_skill_version_id": candidate.value,
            "baseline_skill_version_id": None if baseline is None else baseline.value,
            "source_end_revision": source_end,
            "policy_revision": self.POLICY_REVISION,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            evaluation_kind = (
                "observational_version_comparison"
                if baseline is not None
                else "observational_single_version"
            )
            evidence_json = canonical_json(evidence)
            connection.execute(
                """
                INSERT INTO skill_evaluation_runs(
                    skill_evaluation_run_id, skill_name, candidate_skill_version_id,
                    baseline_skill_version_id, evaluation_kind, policy_revision,
                    source_start_revision, source_end_revision, evidence_json,
                    status, created_revision, updated_revision
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 'evaluating', ?, ?)
                """,
                (
                    run_id.value,
                    command.skill_name,
                    candidate.value,
                    None if baseline is None else baseline.value,
                    evaluation_kind,
                    self.POLICY_REVISION,
                    source_end,
                    evidence_json,
                    revision.value,
                    revision.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO skill_evaluation_attempts(
                    skill_evaluation_attempt_id, skill_evaluation_run_id,
                    provider_profile_id, credential_version_id, model_name,
                    request_json, status, created_revision, updated_revision
                ) VALUES (?, ?, ?, ?, ?, ?, 'dispatched', ?, ?)
                """,
                (
                    attempt_id.value,
                    run_id.value,
                    provider_profile_id,
                    credential_version_id,
                    model_name,
                    request_json,
                    revision.value,
                    revision.value,
                ),
            )
            response = {
                "skill_evaluation_run_id": run_id.value,
                "skill_evaluation_attempt_id": attempt_id.value,
                "skill_name": command.skill_name,
                "candidate_skill_version_id": candidate.value,
                "baseline_skill_version_id": None if baseline is None else baseline.value,
                "evaluation_kind": evaluation_kind,
                "source_start_revision": 1,
                "source_end_revision": source_end,
                "evidence_json": evidence_json,
                "request_json": request_json,
            }
            return MutationPayload(
                response,
                (
                    JournalDraft(
                        "skill.evaluation_dispatched",
                        "skill_evaluation_run",
                        run_id.value,
                        response,
                    ),
                ),
                (OutboxDraft("skill.evaluation_changed", response),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command.command_id,
            command_type="skill.evaluation.prepare",
            request=request,
            mutation=mutate,
        )
        response = receipt.response
        return SkillEvaluationPreparation(
            SkillEvaluationRunId(str(response["skill_evaluation_run_id"])),
            SkillEvaluationAttemptId(str(response["skill_evaluation_attempt_id"])),
            str(response["skill_name"]),
            SkillVersionId(str(response["candidate_skill_version_id"])),
            None
            if response["baseline_skill_version_id"] is None
            else SkillVersionId(str(response["baseline_skill_version_id"])),
            str(response["evaluation_kind"]),
            int(response["source_start_revision"]),
            int(response["source_end_revision"]),
            str(response["evidence_json"]),
            str(response["request_json"]),
            receipt.replayed,
        )

    def finalize(
        self,
        *,
        command_id: CommandId,
        preparation: SkillEvaluationPreparation,
        response: ProviderResponse,
        judgment: SkillEvaluationJudgment,
        proposal_ids: Sequence[SkillImprovementProposalId],
        evaluator_revision: str,
    ) -> SkillEvaluationOutcome:
        if len(proposal_ids) != len(judgment.proposals):
            raise ValueError("Skill evaluation proposal identities do not match output")
        request = {
            "skill_evaluation_run_id": preparation.run_id.value,
            "skill_evaluation_attempt_id": preparation.attempt_id.value,
            "response": response.output,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            row = connection.execute(
                "SELECT status FROM skill_evaluation_runs WHERE skill_evaluation_run_id = ?",
                (preparation.run_id.value,),
            ).fetchone()
            if row is None or str(row["status"]) != "evaluating":
                raise ValueError("Skill evaluation is not awaiting finalization")
            connection.execute(
                """
                UPDATE skill_evaluation_attempts
                SET status = 'completed', response_json = ?, usage_kind = ?,
                    input_tokens = ?, output_tokens = ?, cached_input_tokens = ?,
                    uncached_input_tokens = ?, finish_reason = ?, updated_revision = ?
                WHERE skill_evaluation_attempt_id = ? AND status = 'dispatched'
                """,
                (
                    canonical_json(response.output),
                    response.usage_kind,
                    response.input_tokens,
                    response.output_tokens,
                    response.cached_input_tokens,
                    response.uncached_input_tokens,
                    response.finish_reason,
                    revision.value,
                    preparation.attempt_id.value,
                ),
            )
            connection.execute(
                """
                UPDATE skill_evaluation_runs SET status = 'completed', updated_revision = ?
                WHERE skill_evaluation_run_id = ? AND status = 'evaluating'
                """,
                (revision.value, preparation.run_id.value),
            )
            connection.execute(
                """
                INSERT INTO skill_evaluation_reports(
                    skill_evaluation_run_id, evaluator_kind, evaluator_revision,
                    verdict, rationale, limitations_json, created_revision
                ) VALUES (?, 'independent_model', ?, ?, ?, ?, ?)
                """,
                (
                    preparation.run_id.value,
                    evaluator_revision,
                    judgment.verdict,
                    judgment.rationale,
                    canonical_json(list(judgment.limitations)),
                    revision.value,
                ),
            )
            for proposal_id, proposal in zip(proposal_ids, judgment.proposals, strict=True):
                connection.execute(
                    """
                    INSERT INTO skill_improvement_proposals(
                        skill_improvement_proposal_id, skill_evaluation_run_id,
                        proposal_kind, target_skill_version_id, merge_target_skill_name,
                        title, rationale, suggested_instruction_body, created_revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal_id.value,
                        preparation.run_id.value,
                        proposal.kind,
                        preparation.candidate_skill_version_id.value,
                        proposal.merge_target_skill_name,
                        proposal.title,
                        proposal.rationale,
                        proposal.suggested_instruction_body,
                        revision.value,
                    ),
                )
            payload = {
                "skill_evaluation_run_id": preparation.run_id.value,
                "status": "completed",
                "verdict": judgment.verdict,
                "proposal_count": len(judgment.proposals),
            }
            return MutationPayload(
                payload,
                (
                    JournalDraft(
                        "skill.evaluation_completed",
                        "skill_evaluation_run",
                        preparation.run_id.value,
                        payload,
                    ),
                ),
                (OutboxDraft("skill.evaluation_changed", payload),),
            )

        receipt = self._commits.commit_mutation(
            command_id=command_id,
            command_type="skill.evaluation.finalize",
            request=request,
            mutation=mutate,
        )
        proposals = tuple(
            SkillImprovementProposal(
                proposal_id,
                draft.kind,
                draft.title,
                draft.rationale,
                draft.suggested_instruction_body,
                draft.merge_target_skill_name,
            )
            for proposal_id, draft in zip(proposal_ids, judgment.proposals, strict=True)
        )
        return SkillEvaluationOutcome(
            preparation.run_id,
            "completed",
            preparation.skill_name,
            preparation.candidate_skill_version_id,
            preparation.baseline_skill_version_id,
            preparation.evaluation_kind,
            judgment.verdict,
            judgment.rationale,
            judgment.limitations,
            proposals,
            receipt.commit_revision,
            receipt.replayed,
        )

    def mark_failed(
        self,
        *,
        command_id: CommandId,
        preparation: SkillEvaluationPreparation,
        error_code: str,
        delivery_unknown: bool,
    ) -> None:
        request = {
            "skill_evaluation_run_id": preparation.run_id.value,
            "error_code": error_code,
            "delivery_unknown": delivery_unknown,
        }

        def mutate(connection: sqlite3.Connection, revision: WorkspaceRevision) -> MutationPayload:
            status = "delivery_unknown" if delivery_unknown else "failed"
            connection.execute(
                """
                UPDATE skill_evaluation_attempts
                SET status = ?, error_code = ?, updated_revision = ?
                WHERE skill_evaluation_attempt_id = ? AND status = 'dispatched'
                """,
                (status, error_code, revision.value, preparation.attempt_id.value),
            )
            connection.execute(
                """
                UPDATE skill_evaluation_runs SET status = ?, updated_revision = ?
                WHERE skill_evaluation_run_id = ? AND status = 'evaluating'
                """,
                (status, revision.value, preparation.run_id.value),
            )
            payload = {**request, "status": status}
            return MutationPayload(
                payload,
                (
                    JournalDraft(
                        "skill.evaluation_failed",
                        "skill_evaluation_run",
                        preparation.run_id.value,
                        payload,
                    ),
                ),
                (OutboxDraft("skill.evaluation_changed", payload),),
            )

        self._commits.commit_mutation(
            command_id=command_id,
            command_type="skill.evaluation.fail",
            request=request,
            mutation=mutate,
        )

    def _resolve_versions(
        self, command: EvaluateSkillCommand
    ) -> tuple[SkillVersionId, SkillVersionId | None]:
        candidate_id = command.candidate_skill_version_id
        if candidate_id is None:
            row = self._connection.execute(
                """
                SELECT current_skill_version_id FROM skill_adoptions
                WHERE skill_name = ? AND lifecycle = 'active'
                """,
                (command.skill_name,),
            ).fetchone()
            if row is None:
                raise ValueError("Skill has no active version to evaluate")
            candidate_id = SkillVersionId(str(row[0]))
        candidate = self._version_row(candidate_id, command.skill_name)
        baseline_id = command.baseline_skill_version_id
        if baseline_id is None and candidate["predecessor_skill_version_id"] is not None:
            baseline_id = SkillVersionId(str(candidate["predecessor_skill_version_id"]))
        if baseline_id is not None:
            self._version_row(baseline_id, command.skill_name)
            if baseline_id == candidate_id:
                raise ValueError("candidate and baseline Skill versions must differ")
        return candidate_id, baseline_id

    def _version_row(self, version_id: SkillVersionId, skill_name: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM skill_versions WHERE skill_version_id = ? AND skill_name = ?",
            (version_id.value, skill_name),
        ).fetchone()
        if row is None:
            raise ValueError("Skill version does not belong to the requested Skill")
        return cast(sqlite3.Row, row)

    def _evidence_packet(
        self,
        skill_name: str,
        candidate_id: SkillVersionId,
        baseline_id: SkillVersionId | None,
        source_end_revision: int,
    ) -> Mapping[str, Any]:
        arms = [self._arm(candidate_id, "candidate", source_end_revision)]
        if baseline_id is not None:
            arms.append(self._arm(baseline_id, "baseline", source_end_revision))
        active_skills = self._connection.execute(
            """
            SELECT adoption.skill_name, version.skill_version_id,
                   version.version_label, version.content_sha256, version.skill_markdown
            FROM skill_adoptions AS adoption
            JOIN skill_versions AS version
              ON version.skill_version_id = adoption.current_skill_version_id
            WHERE adoption.lifecycle = 'active' AND adoption.skill_name != ?
            ORDER BY adoption.skill_name
            """,
            (skill_name,),
        ).fetchall()
        return {
            "skill_name": skill_name,
            "policy_revision": self.POLICY_REVISION,
            "source_window": [1, source_end_revision],
            "relationship_note": (
                "explicit feedback co-occurred with exact Skill use; this is not causal attribution"
            ),
            "arms": arms,
            "available_active_skills": [
                {
                    "skill_name": str(row["skill_name"]),
                    "skill_version_id": str(row["skill_version_id"]),
                    "version_label": str(row["version_label"]),
                    "content_sha256": str(row["content_sha256"]),
                    "skill_markdown": str(row["skill_markdown"]),
                }
                for row in active_skills
            ],
        }

    def _arm(
        self, version_id: SkillVersionId, role: str, source_end_revision: int
    ) -> Mapping[str, Any]:
        version = self._connection.execute(
            """
            SELECT version_label, content_sha256, skill_markdown
            FROM skill_versions WHERE skill_version_id = ?
            """,
            (version_id.value,),
        ).fetchone()
        if version is None:
            raise ValueError("Skill evaluation version is missing")
        uses = self._connection.execute(
            """
            SELECT DISTINCT step.turn_id
            FROM skill_context_uses AS use
            JOIN context_items AS item USING (context_item_id)
            JOIN context_manifests AS manifest USING (context_manifest_id)
            JOIN steps AS step USING (step_id)
            WHERE use.skill_version_id = ? AND use.created_revision <= ?
            ORDER BY step.turn_id
            """,
            (version_id.value, source_end_revision),
        ).fetchall()
        feedback = self._connection.execute(
            """
            SELECT DISTINCT feedback.turn_outcome_feedback_id, feedback.turn_id,
                   feedback.disposition, feedback.ratings_json,
                   feedback.issue_codes_json, feedback.comment,
                   feedback.created_revision
            FROM skill_outcome_observations AS observation
            JOIN skill_context_uses AS use USING (context_item_id)
            JOIN turn_outcome_feedback AS feedback USING (turn_outcome_feedback_id)
            WHERE use.skill_version_id = ? AND observation.created_revision <= ?
            ORDER BY feedback.created_revision, feedback.turn_outcome_feedback_id
            """,
            (version_id.value, source_end_revision),
        ).fetchall()
        dispositions = {"accepted": 0, "needs_revision": 0, "rejected": 0}
        observations: list[Mapping[str, Any]] = []
        for row in feedback:
            disposition = str(row["disposition"])
            dispositions[disposition] += 1
            observations.append(
                {
                    "feedback_id": str(row["turn_outcome_feedback_id"]),
                    "turn_id": str(row["turn_id"]),
                    "disposition": disposition,
                    "ratings": json.loads(str(row["ratings_json"])),
                    "issue_codes": json.loads(str(row["issue_codes_json"])),
                    "comment": str(row["comment"]),
                    "created_revision": int(row["created_revision"]),
                }
            )
        return {
            "role": role,
            "skill_version_id": version_id.value,
            "version_label": str(version["version_label"]),
            "content_sha256": str(version["content_sha256"]),
            "skill_markdown": str(version["skill_markdown"]),
            "exact_use_turn_ids": [str(row["turn_id"]) for row in uses],
            "exact_use_turn_count": len(uses),
            "explicit_feedback_count": len(feedback),
            "dispositions": dispositions,
            "observations": observations,
        }
