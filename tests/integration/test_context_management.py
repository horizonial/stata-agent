from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from stata_research_agent.application.context_compiler import (
    ContextBudgetExceeded,
    ContextCompiler,
    ContextSourceCandidate,
)
from stata_research_agent.application.control import (
    CreateWorkspaceCommand,
    SubmitMessageCommand,
)
from stata_research_agent.application.model_gateway import (
    ContextItemCandidate,
    ProviderResponse,
    StartModelStepCommand,
)
from stata_research_agent.application.model_gateway_service import ModelGatewayService
from stata_research_agent.application.provider_credentials import ResolvedProviderCredential
from stata_research_agent.application.turn_interaction import (
    AnswerWaitingCommand,
    OpenWaitingCommand,
)
from stata_research_agent.application.turn_interaction_service import TurnInteractionService
from stata_research_agent.application.workspace_service import WorkspaceControlService
from stata_research_agent.domain.identifiers import CommandId, TurnId, WorkspaceId
from stata_research_agent.domain.status import WaitReason
from stata_research_agent.persistence.context_authority import SqliteContextAuthorityReader
from stata_research_agent.persistence.control_store import SqliteControlStore
from stata_research_agent.persistence.model_gateway_store import SqliteModelGatewayRepository
from stata_research_agent.persistence.turn_interaction_store import (
    SqliteTurnInteractionRepository,
)
from stata_research_agent.persistence.workspace import WorkspaceDatabase
from stata_research_agent.runtime.uuid_identity import UuidIdentityGenerator


class _Credential:
    def resolve_for_transport(
        self, credential_ref: str, *, provider_profile_id: str, endpoint: str
    ) -> ResolvedProviderCredential:
        del credential_ref
        return ResolvedProviderCredential(
            provider_profile_id, "credentialversion_context", endpoint, "secret"
        )


class _Transport:
    async def send(self, *, endpoint: str, request_json: str, credential: str) -> ProviderResponse:
        del endpoint, request_json, credential
        return ProviderResponse(
            {
                "text": "I will preserve this plan for the next Step.",
                "plan": {"summary": "Inspect first", "structured_plan": {}},
                "tool_calls": [],
            },
            "exact",
            100,
            20,
            80,
            20,
        )


class _StaticReader:
    def __init__(self, candidates: tuple[ContextSourceCandidate, ...]) -> None:
        self.candidates = candidates

    def collect(self, turn_id: TurnId) -> tuple[ContextSourceCandidate, ...]:
        del turn_id
        return self.candidates


def _item(identifier: str, content: str) -> ContextItemCandidate:
    return ContextItemCandidate(
        "assistant_message",
        "assistant_output",
        identifier,
        "1",
        "remote_allowed",
        content,
    )


def test_compiler_excludes_optional_overflow_without_lossy_replacement() -> None:
    reader = _StaticReader(
        (
            ContextSourceCandidate(
                ContextItemCandidate(
                    "user_message",
                    "message",
                    "msg_current",
                    "1",
                    "remote_allowed",
                    "current request",
                ),
                0,
                "triggering_message",
                mandatory=True,
            ),
            ContextSourceCandidate(
                _item("assistant_old", "old context " * 4_000),
                2,
                "conversation_history",
                summarizable=True,
            ),
        )
    )
    compiled = ContextCompiler(reader, input_token_budget=300).compile(TurnId("turn_context"))
    assert compiled.items[0].item_kind == "user_message"
    assert len(compiled.items) == 1
    overflow = next(
        decision
        for decision in compiled.decisions
        if decision.source_object_id == "assistant_old"
    )
    assert overflow.decision_kind == "excluded"
    assert overflow.reason_code == "token_budget_external_source_retained"
    assert overflow.detail["lossy_replacement_created"] is False


def test_compiler_never_silently_drops_mandatory_context() -> None:
    reader = _StaticReader(
        (
            ContextSourceCandidate(
                ContextItemCandidate(
                    "waiting_answer",
                    "message",
                    "msg_answer",
                    "1",
                    "remote_allowed",
                    "must remain verbatim " * 500,
                ),
                0,
                "waiting_answer",
                mandatory=True,
            ),
        )
    )
    with pytest.raises(ContextBudgetExceeded):
        ContextCompiler(reader, input_token_budget=64).compile(TurnId("turn_context"))


def _workspace(tmp_path: Path) -> tuple[sqlite3.Connection, object]:
    workspace_id = WorkspaceId("ws_context_management")
    database = WorkspaceDatabase(tmp_path / "workspace", workspace_id)
    database.create()
    connection = database.open(writable=True)
    service = WorkspaceControlService(SqliteControlStore(connection), UuidIdentityGenerator())
    service.create_workspace(CreateWorkspaceCommand(CommandId("cmd_context_init"), workspace_id))
    turn = service.submit_message(
        SubmitMessageCommand(CommandId("cmd_context_message"), "Start with my original idea")
    )
    return connection, turn


def test_authority_reader_restores_waiting_answer_and_prior_assistant_output(
    tmp_path: Path,
) -> None:
    connection, turn = _workspace(tmp_path)
    identities = UuidIdentityGenerator()
    try:
        step = StartModelStepCommand(
            command_id=CommandId("cmd_context_step"),
            turn_id=turn.turn_id,
            expected_turn_revision=1,
            system_prompt_revision="system-v1",
            system_prompt="system",
            main_skill_name="main",
            main_skill_revision="main-v1",
            main_skill_content="main skill",
            tool_catalog_revision="tools-v1",
            tool_schemas=(),
            context_items=(),
            build_decisions=(),
            permission_policy_revision="permission-v1",
            permissions={"workspace_read": True},
            model_policy_revision="model-v1",
            provider_profile="provider",
            provider_kind="openai-compatible",
            model_name="model",
            endpoint="https://provider.example/chat/completions",
            credential_ref="credential://context",
            provider_policy={},
        )
        asyncio.run(
            ModelGatewayService(
                SqliteModelGatewayRepository(connection),
                identities,
                _Credential(),
                _Transport(),
            ).execute_step(step)
        )

        interaction = TurnInteractionService(
            SqliteTurnInteractionRepository(connection), identities
        )
        waiting = interaction.open_waiting(
            OpenWaitingCommand(
                CommandId("cmd_context_wait"),
                turn.turn_id,
                1,
                WaitReason.USER_INPUT,
                "Which variable should be used?",
            )
        )
        answered = interaction.answer_waiting(
            AnswerWaitingCommand(
                CommandId("cmd_context_answer"),
                waiting.waiting_request_id,
                "Use the alternative variable and continue.",
                expected_turn_revision=waiting.turn_revision,
                expected_turn_id=turn.turn_id,
            )
        )
        assert answered.status == "answered"
        assert (
            connection.execute(
                "SELECT status FROM turns WHERE turn_id = ?", (turn.turn_id.value,)
            ).fetchone()[0]
            == "running"
        )

        future = WorkspaceControlService(SqliteControlStore(connection), identities).submit_message(
            SubmitMessageCommand(
                CommandId("cmd_context_future"),
                "This belongs to the next queued Turn.",
                conversation_id=turn.conversation_id,
            )
        )
        assert future.turn_status.value == "queued"

        compiled = ContextCompiler(SqliteContextAuthorityReader(connection)).compile(
            turn.turn_id, input_token_budget=96_000
        )
        by_kind = {item.item_kind: item for item in compiled.items}
        assert by_kind["waiting_answer"].content == ("Use the alternative variable and continue.")
        assert "preserve this plan" in by_kind["assistant_message"].content
        assert "research_state_reference" in by_kind
        assert all(item.source_object_id != future.message_id.value for item in compiled.items)

        usage = connection.execute(
            """
            SELECT cached_input_tokens, uncached_input_tokens
            FROM provider_attempts WHERE state = 'completed'
            """
        ).fetchone()
        assert tuple(usage) == (80, 20)
        manifest = connection.execute(
            """
            SELECT input_token_estimate, reserved_output_tokens FROM context_manifests
            """
        ).fetchone()
        assert manifest[0] > 0
        assert manifest[1] == 0
    finally:
        connection.close()
