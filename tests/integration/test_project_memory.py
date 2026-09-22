"""Project Memory continuity, policy, and Context-freeze safety."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from stata_research_agent.application.control import (
    CompleteTurnCommand,
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.memory import (
    ActivateMemoryCommand,
    CreateMemoryCommand,
    MemoryAccessTier,
    MemoryKind,
    MemoryOriginKind,
    MemorySource,
    MemorySourceRole,
    RecordMemoryCompactionCheckpointCommand,
    RetractMemoryCommand,
    SetConversationMemoryPolicyCommand,
    SetMemoryAccessTierCommand,
    SupersedeMemoryCommand,
)
from stata_research_agent.application.memory_service import MemoryService
from stata_research_agent.application.model_gateway import (
    ProviderResponse,
    StartModelStepCommand,
)
from stata_research_agent.application.model_gateway_service import ModelGatewayService
from stata_research_agent.application.provider_credentials import ResolvedProviderCredential
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, WorkspaceId
from stata_research_agent.domain.status import TurnStatus
from stata_research_agent.persistence.context_authority import SqliteContextAuthorityReader
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.memory_curator_store import (
    SqliteMemoryMaintenanceRepository,
)
from stata_research_agent.persistence.memory_store import SqliteMemoryRepository
from stata_research_agent.persistence.model_gateway_store import SqliteModelGatewayRepository
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class _Credential:
    def resolve_for_transport(
        self, credential_ref: str, *, provider_profile_id: str, endpoint: str
    ) -> ResolvedProviderCredential:
        del credential_ref
        return ResolvedProviderCredential(
            provider_profile_id, "credentialversion_memory", endpoint, "secret"
        )


class _Transport:
    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, request_json, credential
        raise AssertionError("stale Memory must fail before Provider dispatch")


class _SuccessfulTransport:
    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, request_json, credential
        return ProviderResponse(
            {
                "text": "A compact table may help, but this is only a suggestion.",
                "plan": None,
                "tool_calls": [],
            }
        )


def _step_command(command_id: str, turn_id, *, context_items=()) -> StartModelStepCommand:
    return StartModelStepCommand(
        command_id=CommandId(command_id),
        turn_id=turn_id,
        expected_turn_revision=1,
        system_prompt_revision="system-v1",
        system_prompt="system",
        main_skill_name="main",
        main_skill_revision="main-v1",
        main_skill_content="main skill",
        tool_catalog_revision="tools-v1",
        tool_schemas=(),
        context_items=context_items,
        build_decisions=(),
        permission_policy_revision="permission-v1",
        permissions={"workspace_read": True},
        model_policy_revision="model-v1",
        provider_profile="provider",
        provider_kind="openai-compatible",
        model_name="model",
        endpoint="https://provider.example/chat/completions",
        credential_ref="credential://memory",
        provider_policy={},
    )


def _workspace(tmp_path: Path):
    workspace_id = WorkspaceId("ws_project_memory")
    database = WorkspaceDatabase(tmp_path / "workspace", workspace_id)
    database.create()
    connection = database.open(writable=True)
    identities = UuidIdentityGenerator()
    control = WorkspaceControlService(SqliteControlStore(connection), identities)
    control.create_workspace(
        CreateWorkspaceCommand(CommandId("cmd_memory_workspace"), workspace_id)
    )
    first = control.submit_message(
        SubmitMessageCommand(CommandId("cmd_memory_first"), "Use price as the outcome.")
    )
    return connection, identities, control, first


def _source(first) -> MemorySource:
    return MemorySource(
        "message",
        first.message_id.value,
        str(first.commit_revision.value),
        MemorySourceRole.USER_STATEMENT,
    )


def test_active_memory_crosses_conversations_but_respects_read_policy(tmp_path: Path) -> None:
    connection, identities, control, first = _workspace(tmp_path)
    try:
        memory = MemoryService(SqliteMemoryRepository(connection), identities).create(
            CreateMemoryCommand(
                CommandId("cmd_memory_create"),
                MemoryKind.RESEARCH_DECISION,
                "Primary outcome",
                "The confirmed primary outcome is price.",
                MemoryOriginKind.EXPLICIT_USER,
                (_source(first),),
            )
        )
        assert memory.lifecycle.value == "active"
        control.complete_turn(
            CompleteTurnCommand(
                CommandId("cmd_memory_finish_first"), first.turn_id, TurnStatus.SUCCEEDED
            )
        )
        second = control.submit_message(
            SubmitMessageCommand(CommandId("cmd_memory_second"), "Continue the analysis.")
        )

        reader = SqliteContextAuthorityReader(connection)
        recalled = [
            candidate
            for candidate in reader.collect(second.turn_id)
            if candidate.item.source_object_type == "memory_revision"
        ]
        assert len(recalled) == 1
        assert recalled[0].item.source_object_id == memory.memory_revision_id.value
        assert "not Evidence" in recalled[0].item.content

        MemoryService(SqliteMemoryRepository(connection), identities).set_conversation_policy(
            SetConversationMemoryPolicyCommand(
                CommandId("cmd_memory_policy_off"),
                second.conversation_id,
                use_memory=False,
                contribute_memory=True,
            )
        )
        assert all(
            candidate.item.source_object_type != "memory_revision"
            for candidate in reader.collect(second.turn_id)
        )
    finally:
        connection.close()


def test_inferred_memory_requires_activation(tmp_path: Path) -> None:
    connection, identities, _control, first = _workspace(tmp_path)
    try:
        model_outcome = asyncio.run(
            ModelGatewayService(
                SqliteModelGatewayRepository(connection),
                identities,
                _Credential(),
                _SuccessfulTransport(),
            ).execute_step(_step_command("cmd_memory_inference_step", first.turn_id))
        )
        assert model_outcome.assistant_output_id is not None
        service = MemoryService(SqliteMemoryRepository(connection), identities)
        proposed = service.create(
            CreateMemoryCommand(
                CommandId("cmd_memory_inferred"),
                MemoryKind.USER_PREFERENCE,
                "Possible preference",
                "The user may prefer compact tables.",
                MemoryOriginKind.INFERRED,
                (
                    MemorySource(
                        "assistant_output",
                        model_outcome.assistant_output_id.value,
                        str(model_outcome.commit_revision.value),
                        MemorySourceRole.ASSISTANT_INFERENCE,
                    ),
                ),
            )
        )
        assert proposed.lifecycle.value == "proposed"
        assert all(
            candidate.item.source_object_type != "memory_revision"
            for candidate in SqliteContextAuthorityReader(connection).collect(first.turn_id)
        )

        active = service.activate(
            ActivateMemoryCommand(
                CommandId("cmd_memory_activate"),
                proposed.memory_item_id,
                proposed.memory_revision_id,
                proposed.pointer_revision,
            )
        )
        assert active.lifecycle.value == "active"
        assert any(
            candidate.item.source_object_id == active.memory_revision_id.value
            for candidate in SqliteContextAuthorityReader(connection).collect(first.turn_id)
        )
    finally:
        connection.close()


def test_retention_tiers_and_supersession_change_recall_without_deleting_history(
    tmp_path: Path,
) -> None:
    connection, identities, _control, first = _workspace(tmp_path)
    try:
        service = MemoryService(SqliteMemoryRepository(connection), identities)
        preference = service.create(
            CreateMemoryCommand(
                CommandId("cmd_memory_retention_preference"),
                MemoryKind.USER_PREFERENCE,
                "Writing preference",
                "Prefer concise prose.",
                MemoryOriginKind.EXPLICIT_USER,
                (_source(first),),
            )
        )
        service.set_access_tier(
            SetMemoryAccessTierCommand(
                CommandId("cmd_memory_retention_warm"),
                preference.memory_item_id,
                1,
                MemoryAccessTier.WARM,
                "Reduce automatic recall while keeping the memory searchable.",
            )
        )
        assert all(
            candidate.item.source_object_id != preference.memory_revision_id.value
            for candidate in SqliteContextAuthorityReader(connection).collect(first.turn_id)
        )

        old = service.create(
            CreateMemoryCommand(
                CommandId("cmd_memory_supersede_old"),
                MemoryKind.RESEARCH_DECISION,
                "Outcome",
                "Use price in levels.",
                MemoryOriginKind.EXPLICIT_USER,
                (_source(first),),
            )
        )
        successor = service.create(
            CreateMemoryCommand(
                CommandId("cmd_memory_supersede_new"),
                MemoryKind.RESEARCH_DECISION,
                "Outcome",
                "Use log price.",
                MemoryOriginKind.EXPLICIT_USER,
                (_source(first),),
            )
        )
        service.supersede(
            SupersedeMemoryCommand(
                CommandId("cmd_memory_supersede"),
                old.memory_item_id,
                successor.memory_item_id,
                1,
                "The later decision replaces the earlier one.",
            )
        )
        recalled = {
            candidate.item.source_object_id
            for candidate in SqliteContextAuthorityReader(connection).collect(first.turn_id)
            if candidate.item.source_object_type == "memory_revision"
        }
        assert successor.memory_revision_id.value in recalled
        assert old.memory_revision_id.value not in recalled
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_revisions WHERE memory_item_id = ?",
                (old.memory_item_id.value,),
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


def test_retention_policy_archives_only_unpinned_memory_and_keeps_history(
    tmp_path: Path,
) -> None:
    connection, identities, _control, first = _workspace(tmp_path)
    try:
        service = MemoryService(SqliteMemoryRepository(connection), identities)
        preference = service.create(
            CreateMemoryCommand(
                CommandId("cmd_memory_decay_preference"),
                MemoryKind.USER_PREFERENCE,
                "Temporary formatting preference",
                "Prefer blue preview charts.",
                MemoryOriginKind.EXPLICIT_USER,
                (_source(first),),
            )
        )
        pinned = service.create(
            CreateMemoryCommand(
                CommandId("cmd_memory_decay_constraint"),
                MemoryKind.RESEARCH_CONSTRAINT,
                "Sample rule",
                "Keep the confirmed sample restriction.",
                MemoryOriginKind.EXPLICIT_USER,
                (_source(first),),
            )
        )
        maintenance = SqliteMemoryMaintenanceRepository(connection, identities)
        for ordinal in range(3):
            assert (
                maintenance.apply_retention_policy(
                    command_id=CommandId(f"cmd_memory_decay_{ordinal}"),
                    observed_at="2100-01-01T00:00:00+00:00",
                )
                == 1
            )
        states = {
            str(row["memory_item_id"]): (str(row["access_tier"]), bool(row["pinned"]))
            for row in connection.execute(
                "SELECT memory_item_id, access_tier, pinned FROM memory_retention_states"
            ).fetchall()
        }
        assert states[preference.memory_item_id.value] == ("archived", False)
        assert states[pinned.memory_item_id.value] == ("hot", True)
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM memory_retention_history
            WHERE memory_item_id = ? AND reason_code = 'inactive_decay'
            """,
                (preference.memory_item_id.value,),
            ).fetchone()[0]
            == 3
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_revisions WHERE memory_item_id = ?",
                (preference.memory_item_id.value,),
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


def test_retraction_between_compile_and_freeze_fails_closed(tmp_path: Path) -> None:
    connection, identities, _control, first = _workspace(tmp_path)
    try:
        service = MemoryService(SqliteMemoryRepository(connection), identities)
        memory = service.create(
            CreateMemoryCommand(
                CommandId("cmd_memory_race_create"),
                MemoryKind.RESEARCH_CONSTRAINT,
                "Sample constraint",
                "Keep foreign cars in the current exploratory sample.",
                MemoryOriginKind.EXPLICIT_USER,
                (_source(first),),
            )
        )
        stale_items = tuple(
            candidate.item
            for candidate in SqliteContextAuthorityReader(connection).collect(first.turn_id)
            if candidate.item.source_object_type == "memory_revision"
        )
        assert stale_items
        stale_summary_items = tuple(
            candidate.item
            for candidate in SqliteContextAuthorityReader(connection).collect(first.turn_id)
            if candidate.item.source_object_type == "memory_summary_projection"
        )
        assert stale_summary_items
        service.retract(
            RetractMemoryCommand(
                CommandId("cmd_memory_race_retract"),
                memory.memory_item_id,
                memory.pointer_revision,
                "User withdrew the constraint.",
            )
        )

        with pytest.raises(ValueError, match="Memory summary is stale"):
            asyncio.run(
                ModelGatewayService(
                    SqliteModelGatewayRepository(connection),
                    identities,
                    _Credential(),
                    _Transport(),
                ).execute_step(
                    _step_command(
                        "cmd_memory_summary_race_freeze",
                        first.turn_id,
                        context_items=stale_summary_items,
                    )
                )
            )

        command = _step_command("cmd_memory_race_freeze", first.turn_id, context_items=stale_items)
        with pytest.raises(ValueError, match="no longer current and active"):
            asyncio.run(
                ModelGatewayService(
                    SqliteModelGatewayRepository(connection),
                    identities,
                    _Credential(),
                    _Transport(),
                ).execute_step(command)
            )
        assert connection.execute("SELECT count(*) FROM context_manifests").fetchone()[0] == 0
    finally:
        connection.close()


def test_memory_redacts_generic_secrets_before_persistence(tmp_path: Path) -> None:
    connection, identities, _control, first = _workspace(tmp_path)
    try:
        MemoryService(SqliteMemoryRepository(connection), identities).create(
            CreateMemoryCommand(
                CommandId("cmd_memory_secret"),
                MemoryKind.FEEDBACK,
                "Do not retain credentials",
                "A pasted credential was sk-thismustnotpersist12345.",
                MemoryOriginKind.EXPLICIT_USER,
                (_source(first),),
            )
        )
        stored = str(connection.execute("SELECT content FROM memory_revisions").fetchone()[0])
        assert "sk-thismustnotpersist12345" not in stored
        assert "[REDACTED_SENSITIVE_OUTPUT]" in stored
    finally:
        connection.close()


def test_compaction_checkpoint_blocks_until_required_message_has_durable_memory(
    tmp_path: Path,
) -> None:
    connection, identities, _control, first = _workspace(tmp_path)
    try:
        service = MemoryService(SqliteMemoryRepository(connection), identities)
        blocked = service.record_compaction_checkpoint(
            RecordMemoryCompactionCheckpointCommand(
                CommandId("cmd_memory_checkpoint_blocked"),
                first.conversation_id,
                first.commit_revision.value,
                first.commit_revision.value,
                (first.message_id.value,),
            )
        )
        assert blocked.disposition == "blocked"
        assert blocked.uncovered_source_message_ids == (first.message_id.value,)

        service.create(
            CreateMemoryCommand(
                CommandId("cmd_memory_checkpoint_source"),
                MemoryKind.RESEARCH_DECISION,
                "Primary outcome",
                "Use price as the outcome.",
                MemoryOriginKind.EXPLICIT_USER,
                (_source(first),),
            )
        )
        ready = service.record_compaction_checkpoint(
            RecordMemoryCompactionCheckpointCommand(
                CommandId("cmd_memory_checkpoint_ready"),
                first.conversation_id,
                first.commit_revision.value,
                first.commit_revision.value,
                (first.message_id.value,),
                "memory-compaction-v2",
            )
        )
        assert ready.disposition == "ready"
        assert ready.uncovered_source_message_ids == ()
    finally:
        connection.close()
