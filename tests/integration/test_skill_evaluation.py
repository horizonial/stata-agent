"""Independent Skill evaluation freezes evidence and only emits advisory proposals."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from stata_research_agent.application.control import CreateWorkspaceCommand
from stata_research_agent.application.model_configuration import WorkspaceModelConfiguration
from stata_research_agent.application.model_gateway import ProviderResponse
from stata_research_agent.application.provider_credentials import ResolvedProviderCredential
from stata_research_agent.application.skill_evaluation import (
    EvaluateSkillCommand,
    SkillEvaluationJudgment,
    SkillImprovementProposalDraft,
)
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import (
    CommandId,
    SkillEvaluationAttemptId,
    SkillEvaluationRunId,
    SkillImprovementProposalId,
    SkillVersionId,
    WorkspaceId,
)
from stata_research_agent.interfaces.skill_evaluation_runner import (
    ProductionSkillEvaluationRunner,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.skill_evaluation_query import SqliteSkillEvaluationQuery
from stata_research_agent.persistence.skill_evaluation_store import (
    SqliteSkillEvaluationRepository,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


@dataclass
class _Databases:
    database_value: WorkspaceDatabase

    def database(self, _workspace_id: WorkspaceId) -> WorkspaceDatabase:
        return self.database_value


class _Configuration:
    def resolve(self, workspace_id: str) -> WorkspaceModelConfiguration:
        return WorkspaceModelConfiguration(
            workspace_id,
            "provider_test",
            "openai-compatible",
            "https://example.invalid/v1/chat/completions",
            "credential_ref",
            "test-model",
            "medium",
            "workspace_only",
            1,
        )


class _Credentials:
    def resolve_for_transport(
        self, _credential_ref: str, *, provider_profile_id: str, endpoint: str
    ) -> ResolvedProviderCredential:
        return ResolvedProviderCredential(
            provider_profile_id, "credentialversion_test", endpoint, "secret-test"
        )


class _Transport:
    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        assert endpoint.startswith("https://example.invalid")
        assert credential == "secret-test"
        request = json.loads(request_json)
        assert request["normalized_input"]["runtime"]["purpose"] == ("independent_skill_evaluation")
        return ProviderResponse(
            {
                "text": json.dumps(
                    {
                        "verdict": "insufficient_evidence",
                        "rationale": "No direct feedback exists yet.",
                        "limitations": ["Observed use is not causal evidence."],
                        "proposals": [
                            {
                                "kind": "keep_observing",
                                "title": "Collect feedback",
                                "rationale": "Wait for direct user assessment.",
                            }
                        ],
                    }
                )
            },
            "exact",
            100,
            40,
            20,
            80,
            "stop",
        )


def test_skill_evaluation_is_observational_immutable_and_advisory(tmp_path) -> None:
    workspace_id = WorkspaceId("ws_skill_evaluation")
    database = WorkspaceDatabase(tmp_path / "workspace", workspace_id)
    database.create()
    connection = database.open(writable=True)
    try:
        WorkspaceControlService(
            SqliteControlStore(connection), UuidIdentityGenerator()
        ).create_workspace(
            CreateWorkspaceCommand(CommandId("cmd_skill_eval_workspace"), workspace_id)
        )
        version_id = SkillVersionId("skillversion_candidate")
        connection.execute(
            """
            INSERT INTO skill_versions(
                skill_version_id, skill_name, version_label, content_sha256,
                skill_markdown, source_candidate_id, predecessor_skill_version_id,
                created_revision
            ) VALUES (?, 'research-style', '1.0.0', ?, ?, NULL, NULL, 1)
            """,
            (version_id.value, "a" * 64, "# Research style\n\nKeep the user in control."),
        )
        connection.execute(
            """
            INSERT INTO skill_adoptions(
                skill_name, current_skill_version_id, lifecycle,
                pointer_revision, updated_revision
            ) VALUES ('research-style', ?, 'active', 1, 1)
            """,
            (version_id.value,),
        )
        connection.commit()

        repository = SqliteSkillEvaluationRepository(connection)
        preparation = repository.prepare(
            command=EvaluateSkillCommand(CommandId("cmd_skill_eval_prepare"), "research-style"),
            run_id=SkillEvaluationRunId("skilleval_case"),
            attempt_id=SkillEvaluationAttemptId("skillevalattempt_case"),
            provider_profile_id="provider_test",
            credential_version_id="credentialversion_test",
            model_name="test-model",
            request_builder=lambda evidence: json.dumps(evidence),
        )
        frozen = json.loads(preparation.evidence_json)
        assert frozen["relationship_note"].endswith("not causal attribution")
        assert frozen["arms"][0]["exact_use_turn_count"] == 0

        outcome = repository.finalize(
            command_id=CommandId("cmd_skill_eval_finalize"),
            preparation=preparation,
            response=ProviderResponse({"text": "{}"}),
            judgment=SkillEvaluationJudgment(
                "insufficient_evidence",
                "No explicit outcome observations are available.",
                ("Historical co-occurrence cannot establish causal Skill quality.",),
                (
                    SkillImprovementProposalDraft(
                        "keep_observing",
                        "Collect direct feedback",
                        "Wait for explicit user feedback before proposing a revision.",
                    ),
                ),
            ),
            proposal_ids=(SkillImprovementProposalId("skillproposal_case"),),
            evaluator_revision="test-evaluator-v1",
        )

        assert outcome.verdict == "insufficient_evidence"
        assert (
            connection.execute(
                """
                SELECT current_skill_version_id FROM skill_adoptions
                WHERE skill_name = 'research-style'
                """
            ).fetchone()[0]
            == version_id.value
        )
        snapshot = SqliteSkillEvaluationQuery(connection).by_run(outcome.run_id)
        assert snapshot.status == "completed"
        assert snapshot.proposals[0].kind == "keep_observing"
        assert snapshot.evidence["arms"][0]["skill_version_id"] == version_id.value
    finally:
        connection.close()


def test_production_skill_evaluator_uses_independent_model_and_records_attempt(tmp_path) -> None:
    workspace_id = WorkspaceId("ws_skill_evaluator_runner")
    database = WorkspaceDatabase(tmp_path / "workspace-runner", workspace_id)
    database.create()
    connection = database.open(writable=True)
    try:
        WorkspaceControlService(
            SqliteControlStore(connection), UuidIdentityGenerator()
        ).create_workspace(CreateWorkspaceCommand(CommandId("cmd_skill_runner_ws"), workspace_id))
        connection.execute(
            """
            INSERT INTO skill_versions(
                skill_version_id, skill_name, version_label, content_sha256,
                skill_markdown, source_candidate_id, predecessor_skill_version_id,
                created_revision
            ) VALUES ('skillversion_runner', 'research-style', '1.0.0', ?, ?, NULL, NULL, 1)
            """,
            ("b" * 64, "# Research style\n\nDiscuss important choices with the user."),
        )
        connection.execute(
            """
            INSERT INTO skill_adoptions VALUES (
                'research-style', 'skillversion_runner', 'active', 1, 1
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    outcome = asyncio.run(
        ProductionSkillEvaluationRunner(
            _Databases(database),
            _Configuration(),  # type: ignore[arg-type]
            _Credentials(),  # type: ignore[arg-type]
            transport=_Transport(),  # type: ignore[arg-type]
        ).evaluate(
            workspace_id,
            EvaluateSkillCommand(CommandId("cmd_skill_runner_eval"), "research-style"),
        )
    )
    assert outcome.verdict == "insufficient_evidence"
    connection = database.open(writable=False)
    try:
        attempt = connection.execute(
            """
            SELECT status, usage_kind, input_tokens, cached_input_tokens, finish_reason
            FROM skill_evaluation_attempts
            """
        ).fetchone()
        assert tuple(attempt) == ("completed", "exact", 100, 20, "stop")
        assert (
            connection.execute("SELECT current_skill_version_id FROM skill_adoptions").fetchone()[0]
            == "skillversion_runner"
        )
    finally:
        connection.close()
