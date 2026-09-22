"""Background Memory Curator lifecycle and deterministic consolidation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from stata_research_agent.application.control import (
    CompleteTurnCommand,
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.memory import (
    CreateMemoryCommand,
    MemoryKind,
    MemoryOriginKind,
    MemorySource,
    MemorySourceRole,
)
from stata_research_agent.application.memory_service import MemoryService
from stata_research_agent.application.model_configuration import WorkspaceModelConfiguration
from stata_research_agent.application.model_gateway import ProviderResponse
from stata_research_agent.application.provider_credentials import ResolvedProviderCredential
from stata_research_agent.application.skill_evolution import (
    ApproveSkillEvolutionCommand,
    DeactivateSkillCommand,
    RollbackSkillCommand,
)
from stata_research_agent.application.skill_evolution_service import SkillEvolutionService
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import (
    CommandId,
    SkillEvolutionCandidateId,
    SkillEvolutionStateHistoryId,
    WorkspaceId,
)
from stata_research_agent.domain.status import TurnStatus
from stata_research_agent.interfaces.memory_maintenance_runner import (
    ProductionMemoryMaintenanceRunner,
)
from stata_research_agent.interfaces.workspace_skill_publisher import WorkspaceSkillPublisher
from stata_research_agent.interfaces.workspace_skills import (
    FilesystemMainSkillCatalog,
    MainSkillLoadError,
)
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.memory_curator_store import (
    SqliteMemoryMaintenanceRepository,
)
from stata_research_agent.persistence.memory_query import SqliteMemoryQuery
from stata_research_agent.persistence.memory_store import SqliteMemoryRepository
from stata_research_agent.persistence.skill_evolution_query import SqliteSkillEvolutionQuery
from stata_research_agent.persistence.skill_evolution_store import (
    SqliteSkillEvolutionRepository,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class _Databases:
    def __init__(self, database: WorkspaceDatabase) -> None:
        self._database = database

    def database(self, workspace_id: WorkspaceId) -> WorkspaceDatabase:
        assert workspace_id == self._database.workspace_id
        return self._database


class _Configuration:
    def resolve(self, workspace_id: str) -> WorkspaceModelConfiguration:
        return WorkspaceModelConfiguration(
            workspace_id,
            "provider_memory",
            "openai-compatible",
            "https://provider.example/chat/completions",
            "credential://memory",
            "memory-model",
            "low",
            "workspace_only",
            1,
        )


class _Credentials:
    def resolve_for_transport(
        self, credential_ref: str, *, provider_profile_id: str, endpoint: str
    ) -> ResolvedProviderCredential:
        assert credential_ref == "credential://memory"
        return ResolvedProviderCredential(
            provider_profile_id,
            "credentialversion_memory",
            endpoint,
            "secret-memory-key",
        )


class _Transport:
    def __init__(self, message_id: str) -> None:
        self._message_id = message_id

    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        assert endpoint.startswith("https://")
        assert credential == "secret-memory-key"
        assert self._message_id in request_json
        extraction = {
            "episode_summary": (
                "The user fixed the outcome and expressed a possible style preference."
            ),
            "candidates": [
                {
                    "source_message_id": self._message_id,
                    "kind": "research_decision",
                    "title": "Primary outcome",
                    "content": "Price is the primary outcome.",
                    "supporting_quote": "Price is the primary outcome.",
                    "suggested_lifecycle": "active",
                },
                {
                    "source_message_id": self._message_id,
                    "kind": "user_preference",
                    "title": "Compact tables",
                    "content": "The user may prefer compact tables.",
                    "supporting_quote": "I may prefer compact tables.",
                    "suggested_lifecycle": "active",
                },
            ],
        }
        return ProviderResponse(
            {"text": json.dumps(extraction), "tool_calls": []},
            "exact",
            120,
            80,
        )


class _SkillTransport:
    def __init__(self, message_id: str, source_ids: tuple[str, str]) -> None:
        self._message_id = message_id
        self._source_ids = source_ids

    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        assert all(item in request_json for item in self._source_ids)
        extraction = {
            "episode_summary": "The user continued after stable working preferences were recorded.",
            "candidates": [],
            "skill_candidates": [
                {
                    "skill_name": "compact-result-review",
                    "description": "Use when presenting empirical results for review.",
                    "instruction_body": (
                        "Keep result tables compact. Explain every consequential data change "
                        "before presenting the interpretation."
                    ),
                    "rationale": "Two active preferences define a recurring review pattern.",
                    "source_memory_item_ids": list(self._source_ids),
                }
            ],
        }
        return ProviderResponse(
            {"text": json.dumps(extraction), "tool_calls": []}, "exact", 100, 60
        )


def test_curator_records_attempts_and_only_auto_activates_exact_safe_kinds(tmp_path: Path) -> None:
    workspace_id = WorkspaceId("ws_memory_curator")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    connection = database.open(writable=True)
    try:
        identities = UuidIdentityGenerator()
        control = WorkspaceControlService(SqliteControlStore(connection), identities)
        control.create_workspace(CreateWorkspaceCommand(CommandId("cmd_curator_ws"), workspace_id))
        submitted = control.submit_message(
            SubmitMessageCommand(
                CommandId("cmd_curator_message"),
                "Price is the primary outcome. I may prefer compact tables.",
            )
        )
        control.complete_turn(
            CompleteTurnCommand(
                CommandId("cmd_curator_complete"), submitted.turn_id, TurnStatus.SUCCEEDED
            )
        )
    finally:
        connection.close()

    outcome = asyncio.run(
        ProductionMemoryMaintenanceRunner(
            _Databases(database),  # type: ignore[arg-type]
            _Configuration(),  # type: ignore[arg-type]
            _Credentials(),  # type: ignore[arg-type]
            transport=_Transport(submitted.message_id.value),  # type: ignore[arg-type]
        ).run_once(workspace_id)
    )
    assert outcome is not None
    assert outcome.status == "completed"
    assert outcome.created_active == 1
    assert outcome.created_proposed == 1

    connection = database.open(writable=False)
    try:
        snapshot = SqliteMemoryQuery(connection).index()
        assert [item.lifecycle for item in snapshot.items] == ["proposed", "active"]
        assert snapshot.items[1].content == "Price is the primary outcome."
        assert snapshot.summaries
        assert "Primary outcome" in snapshot.summaries[0][2]
        assert "Compact tables" not in snapshot.summaries[0][2]
        attempt = connection.execute(
            """
            SELECT status, credential_version_id, request_json, response_json
            FROM memory_provider_attempts
            """
        ).fetchone()
        assert attempt["status"] == "completed"
        assert attempt["credential_version_id"] == "credentialversion_memory"
        assert "secret-memory-key" not in str(attempt["request_json"])
        assert "secret-memory-key" not in str(attempt["response_json"])
    finally:
        connection.close()

    connection = database.open(writable=True)
    try:
        connection.execute("DELETE FROM memory_summary_projections")
        SqliteMemoryMaintenanceRepository(connection, UuidIdentityGenerator()).rebuild_summaries()
        rebuilt = SqliteMemoryQuery(connection).index()
        assert "Primary outcome" in rebuilt.summaries[0][2]
    finally:
        connection.close()


def test_stable_memory_proposes_reviewable_skill_and_user_activation_installs_it(
    tmp_path: Path,
) -> None:
    workspace_id = WorkspaceId("ws_memory_skill_evolution")
    database = WorkspaceDatabase(tmp_path / workspace_id.value, workspace_id)
    database.create()
    connection = database.open(writable=True)
    try:
        identities = UuidIdentityGenerator()
        control = WorkspaceControlService(SqliteControlStore(connection), identities)
        control.create_workspace(CreateWorkspaceCommand(CommandId("cmd_skill_ws"), workspace_id))
        submitted = control.submit_message(
            SubmitMessageCommand(
                CommandId("cmd_skill_message"),
                "Keep tables compact and explain consequential data changes.",
            )
        )
        control.complete_turn(
            CompleteTurnCommand(
                CommandId("cmd_skill_complete"), submitted.turn_id, TurnStatus.SUCCEEDED
            )
        )
        memory = MemoryService(SqliteMemoryRepository(connection), identities)
        compact_source = (
            MemorySource(
                "message",
                submitted.message_id.value,
                str(submitted.commit_revision.value),
                MemorySourceRole.USER_STATEMENT,
            ),
        )
        followup = control.submit_message(
            SubmitMessageCommand(
                CommandId("cmd_skill_message_followup"),
                "Also keep explanations of consequential data changes explicit.",
            )
        )
        control.complete_turn(
            CompleteTurnCommand(
                CommandId("cmd_skill_complete_followup"),
                followup.turn_id,
                TurnStatus.SUCCEEDED,
            )
        )
        explain_source = (
            MemorySource(
                "message",
                followup.message_id.value,
                str(followup.commit_revision.value),
                MemorySourceRole.USER_STATEMENT,
            ),
        )
        compact = memory.create(
            CreateMemoryCommand(
                CommandId("cmd_skill_memory_compact"),
                MemoryKind.USER_PREFERENCE,
                "Compact tables",
                "Keep result tables compact.",
                MemoryOriginKind.EXPLICIT_USER,
                compact_source,
            )
        )
        explain = memory.create(
            CreateMemoryCommand(
                CommandId("cmd_skill_memory_explain"),
                MemoryKind.FEEDBACK,
                "Explain changes",
                "Explain consequential data changes before interpretation.",
                MemoryOriginKind.EXPLICIT_USER,
                explain_source,
            )
        )
    finally:
        connection.close()

    source_ids = (compact.memory_item_id.value, explain.memory_item_id.value)
    outcome = asyncio.run(
        ProductionMemoryMaintenanceRunner(
            _Databases(database),  # type: ignore[arg-type]
            _Configuration(),  # type: ignore[arg-type]
            _Credentials(),  # type: ignore[arg-type]
            transport=_SkillTransport(followup.message_id.value, source_ids),  # type: ignore[arg-type]
        ).run_once(workspace_id)
    )
    assert outcome is not None
    assert outcome.skill_candidates_proposed == 1

    connection = database.open(writable=True)
    try:
        revision, candidates = SqliteSkillEvolutionQuery(connection).index()
        assert revision > 0
        candidate = candidates[0]
        assert candidate.lifecycle == "proposed"
        assert candidate.validation_status == "passed"
        assert set(candidate.source_memory_item_ids) == set(source_ids)
        repository = SqliteSkillEvolutionRepository(connection)
        identities = UuidIdentityGenerator()
        connection.execute(
            """
            UPDATE memory_retention_states SET access_tier = 'archived'
            WHERE memory_item_id = ?
            """,
            (source_ids[0],),
        )
        with pytest.raises(ValueError, match="no longer current and active"):
            repository.approve(
                ApproveSkillEvolutionCommand(
                    CommandId("cmd_skill_archived_source_rejected"),
                    SkillEvolutionCandidateId(candidate.candidate_id),
                    candidate.pointer_revision,
                ),
                identities.new(SkillEvolutionStateHistoryId),
            )
        connection.execute(
            """
            UPDATE memory_retention_states SET access_tier = 'hot'
            WHERE memory_item_id = ?
            """,
            (source_ids[0],),
        )
        approved_plan = repository.approve(
            ApproveSkillEvolutionCommand(
                CommandId("cmd_skill_activate"),
                SkillEvolutionCandidateId(candidate.candidate_id),
                candidate.pointer_revision,
            ),
            identities.new(SkillEvolutionStateHistoryId),
        )
        WorkspaceSkillPublisher().publish(database.root, approved_plan)
        # Simulate a crash after the filesystem side effect but before Finalization.
        activated = SkillEvolutionService(
            repository,
            WorkspaceSkillPublisher(),
            identities,
            database.root,
        ).approve_and_activate(
            ApproveSkillEvolutionCommand(
                CommandId("cmd_skill_resume_activate"),
                SkillEvolutionCandidateId(candidate.candidate_id),
                approved_plan.pointer_revision,
            )
        )
        assert activated.lifecycle == "activated"
        assert activated.activation_manifest_id is not None
        assert activated.skill_version_id is not None
        assert activated.adoption_pointer_revision == 1
        adoption = connection.execute(
            """
            SELECT lifecycle, current_skill_version_id, pointer_revision
            FROM skill_adoptions WHERE skill_name = 'compact-result-review'
            """
        ).fetchone()
        assert adoption["current_skill_version_id"] == activated.skill_version_id.value

        # Seed a later immutable version to exercise pointer rollback without
        # manufacturing a second model-generated proposal in this lifecycle test.
        v2_markdown = loaded_v2 = (
            "---\nname: compact-result-review\nversion: 2.0.0\n"
            "description: Revised review guidance.\n---\nKeep tables concise.\n"
        )
        v2_hash = hashlib.sha256(v2_markdown.encode("utf-8")).hexdigest()
        current_revision = int(
            connection.execute(
                "SELECT MAX(workspace_revision) FROM workspace_commits"
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO skill_versions VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                "skillversion_seeded_v2",
                "compact-result-review",
                "2.0.0",
                v2_hash,
                v2_markdown,
                activated.skill_version_id.value,
                current_revision,
            ),
        )
        connection.execute(
            """
            UPDATE skill_adoptions
            SET current_skill_version_id = 'skillversion_seeded_v2',
                pointer_revision = 2
            WHERE skill_name = 'compact-result-review'
            """
        )
        skill_path = database.root / "skills" / "compact-result-review" / "SKILL.md"
        skill_path.write_text(loaded_v2, encoding="utf-8", newline="\n")
        evolution = SkillEvolutionService(
            repository,
            WorkspaceSkillPublisher(),
            identities,
            database.root,
        )
        rollback_command = RollbackSkillCommand(
            CommandId("cmd_skill_rollback"),
            "compact-result-review",
            activated.skill_version_id,
            2,
            "The prior version produced clearer reviews.",
        )
        rolled_back = evolution.rollback(rollback_command)
        assert rolled_back.lifecycle == "active"
        assert rolled_back.current_skill_version_id == activated.skill_version_id
        assert rolled_back.pointer_revision == 3
        assert evolution.rollback(rollback_command).replayed

        deactivate_command = DeactivateSkillCommand(
            CommandId("cmd_skill_deactivate"),
            "compact-result-review",
            3,
            "Disable the evolved guidance while reviewing its outcomes.",
        )
        deactivated = evolution.deactivate(deactivate_command)
        assert deactivated.lifecycle == "deactivated"
        assert deactivated.current_skill_version_id is None
        assert not skill_path.exists()
        assert evolution.deactivate(deactivate_command).replayed
        catalog = FilesystemMainSkillCatalog(
            Path(__file__).parents[2]
            / "src"
            / "stata_research_agent"
            / "resources"
            / "skills"
        )
        with pytest.raises(MainSkillLoadError):
            catalog.load_specialized(database.root, "compact-result-review")
        reactivated = evolution.rollback(
            RollbackSkillCommand(
                CommandId("cmd_skill_reactivate"),
                "compact-result-review",
                activated.skill_version_id,
                4,
                "Resume the reviewed version.",
            )
        )
        assert reactivated.lifecycle == "active"
        assert reactivated.pointer_revision == 5
    finally:
        connection.close()

    catalog = FilesystemMainSkillCatalog(
        Path(__file__).parents[2]
        / "src"
        / "stata_research_agent"
        / "resources"
        / "skills"
    )
    loaded = catalog.load_specialized(database.root, "compact-result-review")
    assert loaded.revision.startswith("1.0.0+sha256.")
    assert "Explain every consequential data change" in loaded.content
