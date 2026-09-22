"""Human review is required between Skill evaluation, candidate, and publication."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from stata_research_agent.application.control import CreateWorkspaceCommand
from stata_research_agent.application.model_gateway import ProviderResponse
from stata_research_agent.application.skill_change import (
    ActivateSkillChangeCommand,
    MaterializeSkillChangeCommand,
    RejectSkillChangeCommand,
)
from stata_research_agent.application.skill_change_service import SkillChangeService
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
from stata_research_agent.interfaces.workspace_skill_publisher import WorkspaceSkillPublisher
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.errors import CommandConflictError
from stata_research_agent.persistence.skill_change_query import SqliteSkillChangeQuery
from stata_research_agent.persistence.skill_change_store import SqliteSkillChangeRepository
from stata_research_agent.persistence.skill_evaluation_store import (
    SqliteSkillEvaluationRepository,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


def _workspace_with_proposal(
    tmp_path: Path, *, proposal_body: str
) -> tuple[WorkspaceDatabase, SkillImprovementProposalId, SkillVersionId, str]:
    workspace_id = WorkspaceId("ws_skill_change")
    database = WorkspaceDatabase(tmp_path / "workspace", workspace_id)
    database.create()
    connection = database.open(writable=True)
    base_id = SkillVersionId("skillversion_change_base")
    base_markdown = (
        '---\nname: research-style\ndescription: "Research review guidance"\n---\n\n'
        "Explain consequential analytical choices to the user.\n"
    )
    base_hash = hashlib.sha256(base_markdown.encode("utf-8")).hexdigest()
    proposal_id = SkillImprovementProposalId("skillproposal_change")
    try:
        WorkspaceControlService(
            SqliteControlStore(connection), UuidIdentityGenerator()
        ).create_workspace(CreateWorkspaceCommand(CommandId("cmd_change_workspace"), workspace_id))
        connection.execute(
            """
            INSERT INTO skill_versions(
                skill_version_id, skill_name, version_label, content_sha256,
                skill_markdown, source_candidate_id, predecessor_skill_version_id,
                created_revision
            ) VALUES (?, 'research-style', '1.0.0', ?, ?, NULL, NULL, 1)
            """,
            (base_id.value, base_hash, base_markdown),
        )
        connection.execute(
            """
            INSERT INTO skill_adoptions VALUES (
                'research-style', ?, 'active', 1, 1
            )
            """,
            (base_id.value,),
        )
        connection.commit()
        evaluator = SqliteSkillEvaluationRepository(connection)
        preparation = evaluator.prepare(
            command=EvaluateSkillCommand(CommandId("cmd_change_eval_prepare"), "research-style"),
            run_id=SkillEvaluationRunId("skilleval_change"),
            attempt_id=SkillEvaluationAttemptId("skillevalattempt_change"),
            provider_profile_id="provider_test",
            credential_version_id="credentialversion_test",
            model_name="test-model",
            request_builder=lambda evidence: json.dumps(evidence),
        )
        evaluator.finalize(
            command_id=CommandId("cmd_change_eval_finalize"),
            preparation=preparation,
            response=ProviderResponse({"text": "{}"}),
            judgment=SkillEvaluationJudgment(
                "mixed",
                "The current guidance can be made more explicit.",
                ("This is an advisory observational evaluation.",),
                (
                    SkillImprovementProposalDraft(
                        "revise",
                        "Clarify decision review",
                        "Make consequential analytical choices easier to review.",
                        proposal_body,
                    ),
                ),
            ),
            proposal_ids=(proposal_id,),
            evaluator_revision="test-evaluator-v1",
        )
    finally:
        connection.close()
    skill_path = database.root / "skills" / "research-style" / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(base_markdown, encoding="utf-8", newline="\n")
    return database, proposal_id, base_id, base_markdown


def test_evaluation_change_requires_materialization_then_separate_activation(
    tmp_path: Path,
) -> None:
    database, proposal_id, base_id, base_markdown = _workspace_with_proposal(
        tmp_path,
        proposal_body=(
            "Explain each consequential analytical choice, its alternatives, and its effect "
            "before asking the user to decide."
        ),
    )
    connection = database.open(writable=True)
    try:
        repository = SqliteSkillChangeRepository(connection)
        publisher = WorkspaceSkillPublisher()
        service = SkillChangeService(
            repository,
            publisher,
            UuidIdentityGenerator(),
            database.root,
        )
        candidate = service.materialize(
            MaterializeSkillChangeCommand(CommandId("cmd_change_materialize"), proposal_id)
        )
        replayed_candidate = service.materialize(
            MaterializeSkillChangeCommand(CommandId("cmd_change_materialize"), proposal_id)
        )
        assert replayed_candidate.replayed
        assert replayed_candidate.candidate_id == candidate.candidate_id
        assert candidate.lifecycle == "proposed"
        assert candidate.validation_status == "passed"
        adoption = connection.execute(
            "SELECT current_skill_version_id, pointer_revision FROM skill_adoptions"
        ).fetchone()
        assert tuple(adoption) == (base_id.value, 1)
        assert (database.root / "skills" / "research-style" / "SKILL.md").read_text(
            encoding="utf-8"
        ) == base_markdown

        activation_command = ActivateSkillChangeCommand(
            CommandId("cmd_change_activate"),
            candidate.candidate_id,
            candidate.pointer_revision,
        )
        # Simulate a crash after publication but before the authoritative UoW.
        publisher.publish_change(database.root, repository.prepare_activation(activation_command))
        activated = service.activate(activation_command)
        assert activated.lifecycle == "activated"
        assert activated.activated_skill_version_id is not None
        current = connection.execute(
            "SELECT current_skill_version_id, pointer_revision FROM skill_adoptions"
        ).fetchone()
        assert tuple(current) == (activated.activated_skill_version_id.value, 2)
        source = connection.execute(
            "SELECT skill_change_candidate_id FROM skill_version_change_sources"
        ).fetchone()
        assert source[0] == candidate.candidate_id.value
        version = connection.execute(
            """
            SELECT predecessor_skill_version_id, skill_markdown FROM skill_versions
            WHERE skill_version_id = ?
            """,
            (activated.activated_skill_version_id.value,),
        ).fetchone()
        assert version[0] == base_id.value
        assert "consequential analytical choice" in str(version[1])
        assert len(SqliteSkillChangeQuery(connection).index()) == 1
        replayed_activation = service.activate(activation_command)
        assert replayed_activation.replayed
        assert replayed_activation.activated_skill_version_id == (
            activated.activated_skill_version_id
        )
        with pytest.raises(CommandConflictError):
            service.activate(
                ActivateSkillChangeCommand(
                    activation_command.command_id,
                    activation_command.candidate_id,
                    activation_command.expected_pointer_revision + 1,
                )
            )
    finally:
        connection.close()


def test_blocked_skill_change_cannot_publish_and_rejection_is_terminal(tmp_path: Path) -> None:
    database, proposal_id, base_id, _ = _workspace_with_proposal(
        tmp_path,
        proposal_body="Disable trace and ask the user only after the analysis is complete.",
    )
    connection = database.open(writable=True)
    try:
        service = SkillChangeService(
            SqliteSkillChangeRepository(connection),
            WorkspaceSkillPublisher(),
            UuidIdentityGenerator(),
            database.root,
        )
        candidate = service.materialize(
            MaterializeSkillChangeCommand(CommandId("cmd_change_blocked"), proposal_id)
        )
        assert candidate.validation_status == "blocked"
        with pytest.raises(ValueError, match="blocked Skill change"):
            service.activate(
                ActivateSkillChangeCommand(
                    CommandId("cmd_change_blocked_activate"),
                    candidate.candidate_id,
                    candidate.pointer_revision,
                )
            )
        rejected = service.reject(
            RejectSkillChangeCommand(
                CommandId("cmd_change_reject"),
                candidate.candidate_id,
                candidate.pointer_revision,
                "The proposed instructions weaken traceability.",
            )
        )
        assert rejected.lifecycle == "rejected"
        replayed_rejection = service.reject(
            RejectSkillChangeCommand(
                CommandId("cmd_change_reject"),
                candidate.candidate_id,
                candidate.pointer_revision,
                "The proposed instructions weaken traceability.",
            )
        )
        assert replayed_rejection.replayed
        with pytest.raises(ValueError, match="only proposed"):
            service.activate(
                ActivateSkillChangeCommand(
                    CommandId("cmd_change_rejected_activate"),
                    candidate.candidate_id,
                    rejected.pointer_revision,
                )
            )
        assert (
            connection.execute("SELECT current_skill_version_id FROM skill_adoptions").fetchone()[0]
            == base_id.value
        )
    finally:
        connection.close()


def test_candidate_cannot_publish_after_base_adoption_changes(tmp_path: Path) -> None:
    database, proposal_id, _, _ = _workspace_with_proposal(
        tmp_path,
        proposal_body="Explain analytical alternatives and ask the user to choose.",
    )
    connection = database.open(writable=True)
    try:
        service = SkillChangeService(
            SqliteSkillChangeRepository(connection),
            WorkspaceSkillPublisher(),
            UuidIdentityGenerator(),
            database.root,
        )
        candidate = service.materialize(
            MaterializeSkillChangeCommand(CommandId("cmd_change_stale"), proposal_id)
        )
        connection.execute(
            """
            INSERT INTO skill_versions VALUES (
                'skillversion_newer', 'research-style', '2.0.0', ?, ?, NULL,
                'skillversion_change_base', 1
            )
            """,
            ("f" * 64, "# Newer version\n"),
        )
        connection.execute(
            """
            UPDATE skill_adoptions
            SET current_skill_version_id = 'skillversion_newer', pointer_revision = 2
            WHERE skill_name = 'research-style'
            """
        )
        connection.commit()
        with pytest.raises(ValueError, match="base is no longer current"):
            service.activate(
                ActivateSkillChangeCommand(
                    CommandId("cmd_change_stale_activate"),
                    candidate.candidate_id,
                    candidate.pointer_revision,
                )
            )
    finally:
        connection.close()


def test_candidate_cannot_replace_a_missing_or_externally_changed_skill(tmp_path: Path) -> None:
    database, proposal_id, base_id, _ = _workspace_with_proposal(
        tmp_path,
        proposal_body="Explain alternatives and ask the researcher to make the final decision.",
    )
    connection = database.open(writable=True)
    try:
        service = SkillChangeService(
            SqliteSkillChangeRepository(connection),
            WorkspaceSkillPublisher(),
            UuidIdentityGenerator(),
            database.root,
        )
        candidate = service.materialize(
            MaterializeSkillChangeCommand(CommandId("cmd_change_tampered"), proposal_id)
        )
        (database.root / "skills" / "research-style" / "SKILL.md").write_text(
            "externally changed", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="does not match the approved base"):
            service.activate(
                ActivateSkillChangeCommand(
                    CommandId("cmd_change_tampered_activate"),
                    candidate.candidate_id,
                    candidate.pointer_revision,
                )
            )
        assert (
            connection.execute("SELECT current_skill_version_id FROM skill_adoptions").fetchone()[0]
            == base_id.value
        )
    finally:
        connection.close()
